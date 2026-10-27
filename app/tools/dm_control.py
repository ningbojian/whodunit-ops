"""DM 场控：阶段状态机、场控码、运行令牌、硬件调用与降级、审计事件。

硬边界：
  - 倒计时只用于展示/提醒，服务端没有定时推进；
  - 只有 DM 显式 action 才能改变阶段；
  - 先落库阶段状态，再调用硬件；硬件失败不回滚、不阻塞；
  - 同一场次最多一个未结束 run，同一 DM 最多一个进行中 run。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import uuid
from typing import Any, Iterable

import pandas as pd

from app.integrations.hardware import (
    HardwareAdapter,
    HardwareResult,
    build_hardware_adapter,
)
from app.tools.frame import coerce_frame, json_safe
from app.tools.frame_specs import (
    DM_RUNS_COLUMNS,
    DM_RUNS_SPEC,
    DM_RUN_EVENTS_COLUMNS,
    DM_RUN_EVENTS_SPEC,
    SCRIPT_STAGES_SPEC,
)
from app.tools.resources import load_assignments, load_dms, load_rooms
from app.tools.scripts import load_scripts, normalize_script_name, split_aliases
from app.tools.stages import (
    StageTemplateError,
    parse_hardware_scene,
    snapshot_for_run,
)
from app.tools.storage import (
    STATUS_CANCELLED,
    is_missing_value,
    load_sessions_with_counts,
    store_lock,
)

RUN_STATUSES = {"pending", "active", "paused", "completed", "abandoned"}
LIVE_STATUSES = {"pending", "active", "paused"}
OPEN_STATUSES = {"active", "paused"}
MODES = {"software", "hardware"}
ACTION_TYPES = {
    "start": "run_started",
    "next": "stage_next",
    "previous": "stage_previous",
    "pause": "stage_paused",
    "resume": "stage_resumed",
    "extend": "stage_extended",
    "finish": "run_finished",
    "mode": "mode_changed",
    "hardware_retry": "hardware_sent",
}
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
_UNSET = object()


class DMControlError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# ---------------------------------------------------------------------------
# 时间与序列化
# ---------------------------------------------------------------------------


def _utc_now(now: Any = None) -> pd.Timestamp:
    if now is None:
        return pd.Timestamp.now(tz="UTC").tz_localize(None).floor("s")
    moment = pd.Timestamp(now)
    if moment.tzinfo is not None:
        moment = moment.tz_convert("UTC").tz_localize(None)
    return moment.floor("s")


def _iso(value: Any) -> str | None:
    if is_missing_value(value):
        return None
    moment = pd.Timestamp(value)
    if moment.tzinfo is not None:
        moment = moment.tz_convert("UTC").tz_localize(None)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _local_text(value: Any) -> str | None:
    if is_missing_value(value):
        return None
    return pd.Timestamp(value).strftime("%Y-%m-%d %H:%M")


def _text(value: Any) -> str:
    if is_missing_value(value):
        return ""
    return str(value).strip()


def _int(value: Any, default: int = 0) -> int:
    if is_missing_value(value):
        return default
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _run_payload(row: dict[str, Any]) -> dict[str, Any]:
    return {column: json_safe(row.get(column)) for column in DM_RUNS_COLUMNS}


def _event_payload(row: dict[str, Any]) -> dict[str, Any]:
    payload = {column: json_safe(row.get(column)) for column in DM_RUN_EVENTS_COLUMNS}
    for key in ("at", "created_at"):
        payload[key] = _iso(row.get(key))
    return payload


def _json_text(value: Any, limit: int = 4096) -> str | None:
    if value is None:
        return None
    text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return text[:limit]


# ---------------------------------------------------------------------------
# 密钥、场控码与 DM token
# ---------------------------------------------------------------------------


def _secret(secret: str | None = None) -> str:
    value = (secret if secret is not None else os.getenv("WEB_SESSION_SECRET", "")).strip()
    if not value:
        raise DMControlError(
            "server_not_configured", "缺少 WEB_SESSION_SECRET，无法签发场控码。"
        )
    return value


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def normalize_code(code: Any) -> str:
    return "".join(str(code or "").strip().upper().split())


def generate_access_code() -> str:
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(8))


def hash_access_code(code: str, *, secret: str | None = None) -> str:
    key = _secret(secret).encode("utf-8")
    return hmac.new(key, normalize_code(code).encode("utf-8"), hashlib.sha256).hexdigest()


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def issue_dm_token(
    store: Any,
    run: dict[str, Any],
    *,
    secret: str | None = None,
    ttl_hours: float | None = None,
    now: Any = None,
) -> tuple[str, pd.Timestamp]:
    moment = _utc_now(now)
    hours = (
        ttl_hours
        if ttl_hours is not None
        else _float_env("DM_TOKEN_HOURS", 12)
    )
    hours = max(1.0, min(float(hours), 24.0))
    expires = moment + pd.Timedelta(hours=hours)
    tenant = _text(getattr(store, "tenant_id", "")) or _text(run.get("tenant_id")) or "local"
    payload = {
        "v": 1,
        "t": "dm",
        "tenant": tenant,
        "run": _text(run.get("run_id")),
        "dm": _text(run.get("dm_id")) or None,
        "tv": _int(run.get("token_version")),
        "exp": int(expires.timestamp()),
    }
    body = _b64(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    )
    signature = hmac.new(
        _secret(secret).encode("utf-8"), body.encode("ascii"), hashlib.sha256
    ).digest()
    return f"dm1.{body}.{_b64(signature)}", expires


def parse_dm_token(
    token: str,
    *,
    secret: str | None = None,
    now: Any = None,
) -> dict[str, Any]:
    moment = _utc_now(now)
    raw = str(token or "").strip()
    parts = raw.split(".")
    if len(parts) != 3 or parts[0] != "dm1":
        raise DMControlError("token_invalid", "场控令牌无效。")
    body, signature = parts[1], parts[2]
    expected = hmac.new(
        _secret(secret).encode("utf-8"), body.encode("ascii"), hashlib.sha256
    ).digest()
    try:
        actual = _unb64(signature)
    except (ValueError, TypeError):
        raise DMControlError("token_invalid", "场控令牌无效。") from None
    if not hmac.compare_digest(expected, actual):
        raise DMControlError("token_invalid", "场控令牌无效。")
    try:
        payload = json.loads(_unb64(body).decode("utf-8"))
    except (ValueError, TypeError, UnicodeDecodeError):
        raise DMControlError("token_invalid", "场控令牌无效。") from None
    if (
        not isinstance(payload, dict)
        or payload.get("t") != "dm"
        or not payload.get("run")
        or not payload.get("tenant")
    ):
        raise DMControlError("token_invalid", "场控令牌无效。")
    if int(payload.get("exp") or 0) <= int(moment.timestamp()):
        raise DMControlError("token_invalid", "场控令牌已过期。")
    return payload


def verify_dm_token(
    store: Any,
    token: str,
    *,
    run_id: str | None = None,
    secret: str | None = None,
    now: Any = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    payload = parse_dm_token(token, secret=secret, now=now)
    expected_run = _text(run_id)
    if expected_run and payload.get("run") != expected_run:
        raise DMControlError("not_your_run", "这个令牌不属于该场控。")
    if payload.get("tenant") != (
        _text(getattr(store, "tenant_id", "")) or "local"
    ):
        raise DMControlError("not_your_run", "这个令牌不属于该租户。")
    frame = _load_runs(store)
    run = _find_run(frame, payload["run"])
    if run is None:
        raise DMControlError("run_not_found", "场控不存在。")
    if _int(payload.get("tv")) != _int(run.get("token_version")):
        raise DMControlError("token_invalid", "场控令牌已被撤销。")
    if _text(run.get("status")) in {"completed", "abandoned"}:
        raise DMControlError("run_finished", "场控已经结束。")
    return payload, run


# ---------------------------------------------------------------------------
# 读取与表操作
# ---------------------------------------------------------------------------


def _load_runs(store: Any) -> pd.DataFrame:
    return coerce_frame(store.load_dm_runs(), DM_RUNS_SPEC)


def _load_events(store: Any) -> pd.DataFrame:
    return coerce_frame(store.load_dm_run_events(), DM_RUN_EVENTS_SPEC)


def _find_run(frame: pd.DataFrame, run_id: str) -> dict[str, Any] | None:
    if frame.empty:
        return None
    hit = frame[frame["run_id"].astype("string").fillna("") == _text(run_id)]
    return hit.iloc[0].to_dict() if not hit.empty else None


def _find_event(frame: pd.DataFrame, run_id: str, event_id: str) -> dict[str, Any] | None:
    if frame.empty:
        return None
    hit = frame[
        (frame["run_id"].astype("string").fillna("") == _text(run_id))
        & (frame["event_id"].astype("string").fillna("") == _text(event_id))
    ]
    return hit.iloc[0].to_dict() if not hit.empty else None


def _save_frame(store: Any, frame: pd.DataFrame, kind: str) -> None:
    if kind == "runs":
        store.save_dm_runs(coerce_frame(frame, DM_RUNS_SPEC))
    else:
        store.save_dm_run_events(coerce_frame(frame, DM_RUN_EVENTS_SPEC))


def _event_row(
    *,
    run_id: str,
    event_type: str,
    stage_id: str | None,
    stage_seq: int | None,
    actor: str,
    at: pd.Timestamp,
    now: pd.Timestamp,
    event_id: str | None = None,
    payload: Any = None,
) -> dict[str, Any]:
    return {
        "event_id": event_id or f"evt-{uuid.uuid4().hex[:16]}",
        "run_id": run_id,
        "event_type": event_type,
        "stage_id": stage_id,
        "stage_seq": stage_seq,
        "actor": actor,
        "at": at,
        "payload": _json_text(payload),
        "created_at": now,
    }


def _append_events(store: Any, rows: Iterable[dict[str, Any]]) -> None:
    events = list(rows)
    if not events:
        return
    frame = _load_events(store)
    existing_ids = set(frame["event_id"].astype("string").fillna("")) if not frame.empty else set()
    fresh = [row for row in events if _text(row.get("event_id")) not in existing_ids]
    if not fresh:
        return
    # 先把新行按 spec 整形再拼接，避免「全空列」触发 pandas 的 dtype 推断告警。
    merged = pd.concat(
        [frame, coerce_frame(pd.DataFrame(fresh), DM_RUN_EVENTS_SPEC)],
        ignore_index=True,
    )
    _save_frame(store, merged, "events")


def _persist_run_change(
    store: Any,
    run: dict[str, Any],
    values: dict[str, Any],
    event_rows: Iterable[dict[str, Any]],
    *,
    expected_version: int | None = None,
) -> bool:
    """写状态 + 事件。返回 False 表示 CAS 失败（stale_state）。"""
    run_id = _text(run.get("run_id"))
    allowed = {key: value for key, value in values.items() if key in DM_RUNS_COLUMNS}
    updater = getattr(store, "update_dm_run", None)
    appender = getattr(store, "append_dm_run_event", None)
    if callable(updater):
        ok = updater(run_id, allowed, expected_state_version=expected_version)
        if not ok:
            return False
        if callable(appender):
            for event in event_rows:
                appender(event)
        else:
            _append_events(store, event_rows)
        return True

    frame = _load_runs(store)
    hit = frame["run_id"].astype("string").fillna("") == run_id
    if not hit.any():
        return False
    index = frame.index[hit][0]
    if expected_version is not None and _int(frame.at[index, "state_version"]) != int(
        expected_version
    ):
        return False
    for key, value in allowed.items():
        frame.at[index, key] = value
    _save_frame(store, frame, "runs")
    _append_events(store, event_rows)
    return True


def _clamp_at(run: dict[str, Any], value: Any, now: pd.Timestamp) -> tuple[pd.Timestamp, list[str]]:
    warnings: list[str] = []
    if is_missing_value(value):
        return now, warnings
    try:
        moment = _utc_now(value)
    except (TypeError, ValueError, OverflowError):
        return now, ["操作时间无法解析，已使用服务端时间。"]
    lower = run.get("created_at")
    if not is_missing_value(lower):
        lower_ts = _utc_now(lower)
        if moment < lower_ts:
            warnings.append("操作时间早于场控创建时间，已夹到创建时间。")
            moment = lower_ts
    if moment > now:
        warnings.append("操作时间晚于服务器当前时间，已使用服务器时间。")
        moment = now
    return moment, warnings


# ---------------------------------------------------------------------------
# 模板、房间、场次上下文
# ---------------------------------------------------------------------------


def _session_row(store: Any, session_id: str) -> dict[str, Any] | None:
    frame = load_sessions_with_counts(store=store)
    if frame.empty:
        return None
    hit = frame[frame["session_id"].astype("string").fillna("") == _text(session_id)]
    return hit.iloc[0].to_dict() if not hit.empty else None


def _script_for_session(store: Any, session: dict[str, Any]) -> dict[str, Any] | None:
    scripts = load_scripts(store=store)
    if scripts.empty:
        return None
    key = normalize_script_name(session.get("script_name")).casefold()
    if not key:
        return None
    for _, row in scripts.iterrows():
        if normalize_script_name(row.get("script_name")).casefold() == key:
            return row.to_dict()
    for _, row in scripts.iterrows():
        if any(
            normalize_script_name(alias).casefold() == key
            for alias in split_aliases(row.get("aliases"))
        ):
            return row.to_dict()
    return None


def _template_rows(store: Any, script_id: str) -> tuple[pd.DataFrame, int]:
    frame = coerce_frame(store.load_script_stages(), SCRIPT_STAGES_SPEC)
    if frame.empty:
        return frame, 0
    hit = frame[
        (frame["script_id"].astype("string").fillna("") == _text(script_id))
        & (frame["active"].fillna(False))
    ].sort_values("seq")
    version = 0
    if not hit.empty:
        version = max(_int(value) for value in hit["version"].tolist())
    return hit, version


def _template_snapshot(store: Any, session: dict[str, Any]) -> tuple[dict[str, Any], int]:
    script = _script_for_session(store, session)
    if script is None:
        raise DMControlError("template_missing", "场次对应的剧本不在剧本库中。")
    script_id = _text(script.get("script_id"))
    rows, version = _template_rows(store, script_id)
    if rows.empty:
        raise DMControlError("template_missing", "该剧本还没有启用的阶段模板。")
    snapshot = snapshot_for_run(rows, version=version)
    if not snapshot.get("stages"):
        raise DMControlError("template_missing", "该剧本还没有启用的阶段模板。")
    return snapshot, version


def _parse_snapshot(run: dict[str, Any]) -> dict[str, Any]:
    raw = _text(run.get("template_snapshot"))
    if not raw:
        return {"version": None, "stages": []}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {"version": None, "stages": []}
    if not isinstance(parsed, dict) or not isinstance(parsed.get("stages"), list):
        return {"version": None, "stages": []}
    return parsed


def _stage_at(snapshot: dict[str, Any], seq: int | None) -> dict[str, Any] | None:
    if seq is None:
        return None
    for stage in snapshot.get("stages", []):
        if _int(stage.get("seq")) == int(seq):
            return stage
    return None


def _room_assignment(store: Any, session_id: str) -> tuple[str | None, str | None]:
    assignments = load_assignments(store=store)
    if assignments.empty:
        return None, None
    hit = assignments[
        (assignments["session_id"].astype("string").fillna("") == _text(session_id))
        & (assignments["resource_kind"].astype("string").fillna("") == "room")
    ]
    if hit.empty:
        return None, None
    room_id = _text(hit.iloc[0].get("resource_id")) or None
    rooms = load_rooms(store=store)
    room_name = None
    if room_id and not rooms.empty:
        room = rooms[rooms["room_id"].astype("string").fillna("") == room_id]
        if not room.empty:
            room_name = _text(room.iloc[0].get("name")) or None
    return room_id, room_name


def _dm_name(store: Any, dm_id: str) -> str | None:
    if not dm_id:
        return None
    dms = load_dms(store=store)
    if dms.empty:
        return None
    hit = dms[dms["dm_id"].astype("string").fillna("") == dm_id]
    return _text(hit.iloc[0].get("name")) or None if not hit.empty else None


def _redact_stage(stage: dict[str, Any]) -> dict[str, Any]:
    return {
        key: stage.get(key)
        for key in (
            "stage_id",
            "seq",
            "name",
            "stage_type",
            "duration_minutes",
            "warn_before_seconds",
        )
    }


# ---------------------------------------------------------------------------
# 只读视图
# ---------------------------------------------------------------------------


def run_view(
    store: Any,
    run: dict[str, Any],
    *,
    for_dm: bool = True,
    now: Any = None,
) -> dict[str, Any]:
    moment = _utc_now(now)
    session = _session_row(store, _text(run.get("session_id")))
    warnings: list[str] = []
    status = _text(run.get("status")) or "pending"
    preview = status == "pending"
    snapshot = _parse_snapshot(run)
    if preview or not snapshot.get("stages"):
        if session is not None:
            try:
                current_snapshot, _ = _template_snapshot(store, session)
                if not snapshot.get("stages"):
                    snapshot = current_snapshot
            except DMControlError:
                pass

    stages: list[dict[str, Any]] = []
    current_seq = _int(run.get("current_seq"), 0) or None
    current_stage: dict[str, Any] | None = None
    for stage in snapshot.get("stages", []):
        seq = _int(stage.get("seq"))
        if current_seq is None:
            state = "upcoming"
        elif seq < current_seq:
            state = "completed"
        elif seq == current_seq:
            state = "current"
        else:
            state = "upcoming"
        item = dict(stage) if for_dm else _redact_stage(stage)
        item["state"] = state
        item["is_current"] = seq == current_seq
        if item["is_current"]:
            current_stage = item
        stages.append(item)

    remaining = None
    overtime = 0
    paused = status == "paused"
    if status == "active":
        ends = run.get("stage_ends_at")
        if not is_missing_value(ends):
            delta = (_utc_now(ends) - moment).total_seconds()
            remaining = max(0, int(delta))
            overtime = max(0, int(-delta))
    elif status == "paused":
        remaining = max(0, _int(run.get("remaining_seconds_at_pause")))
        paused_at = run.get("paused_at")
        ends = run.get("stage_ends_at")
        if not is_missing_value(paused_at) and not is_missing_value(ends):
            overtime = max(0, int((_utc_now(paused_at) - _utc_now(ends)).total_seconds()))
    elif status == "completed":
        remaining = 0
        overtime = 0

    if session is None:
        warnings.append("场次已不存在，仍可查看和结束本场场控。")
        session_payload = None
    else:
        if _text(session.get("status")) == STATUS_CANCELLED:
            warnings.append("场次已取消，场控不会自动结束。")
        session_payload = {
            "session_id": _text(session.get("session_id")),
            "store_name": _text(session.get("store_name")) or None,
            "script_name": _text(session.get("script_name")) or None,
            "script_type": _text(session.get("script_type")) or None,
            "start_time": _local_text(session.get("start_time")),
            "status": _text(session.get("status")) or None,
            "booked_seats": _int(session.get("booked_seats")),
            "total_seats": _int(session.get("total_seats")) or None,
        }

    current_room, current_room_name = _room_assignment(store, _text(run.get("session_id")))
    if run.get("room_id") and current_room and _text(run.get("room_id")) != current_room:
        warnings.append(
            f"排期已换到 {current_room_name or current_room}，本场控仍按开场时快照显示。"
        )

    if run.get("hardware_degraded") is True:
        warnings.append("硬件已降级，后续阶段只显示文字提示。")

    run_payload = {
        "run_id": _text(run.get("run_id")),
        "session_id": _text(run.get("session_id")),
        "dm_id": _text(run.get("dm_id")) or None,
        "dm_name": _text(run.get("dm_name")) or None,
        "room_id": _text(run.get("room_id")) or current_room,
        "room_name": _text(run.get("room_name")) or current_room_name,
        "status": status,
        "mode": _text(run.get("mode")) or "software",
        "template_version": _int(run.get("template_version")) or None,
        "current_stage_id": _text(run.get("current_stage_id")) or None,
        "current_seq": current_seq,
        "stage_started_at": _iso(run.get("stage_started_at")),
        "stage_ends_at": _iso(run.get("stage_ends_at")),
        "paused_at": _iso(run.get("paused_at")),
        "paused_total_seconds": _int(run.get("paused_total_seconds")),
        "remaining_seconds": remaining,
        "overtime_seconds": overtime,
        "paused": paused,
        "hardware_degraded": bool(run.get("hardware_degraded") is True),
        "state_version": _int(run.get("state_version")),
        "last_activity_at": _iso(run.get("last_activity_at")),
        "created_at": _iso(run.get("created_at")),
        "updated_at": _iso(run.get("updated_at")),
        "finished_at": _iso(run.get("finished_at")),
    }
    return {
        "run": run_payload,
        "session": session_payload,
        "stages": stages,
        "current_stage": current_stage,
        "server_now": _iso(moment),
        "template_preview": preview,
        "warnings": warnings,
    }


def get_run(
    store: Any,
    run_id: str,
    *,
    for_dm: bool = True,
    now: Any = None,
) -> dict[str, Any]:
    run = _find_run(_load_runs(store), run_id)
    if run is None:
        raise DMControlError("run_not_found", "场控不存在。")
    return run_view(store, run, for_dm=for_dm, now=now)


# ---------------------------------------------------------------------------
# 商家侧：创建、发码、撤销、只读
# ---------------------------------------------------------------------------


def _live_same_session(runs: pd.DataFrame, session_id: str, exclude: str = "") -> dict[str, Any] | None:
    if runs.empty:
        return None
    hit = runs[
        (runs["session_id"].astype("string").fillna("") == _text(session_id))
        & (runs["status"].astype("string").fillna("").isin(LIVE_STATUSES))
        & (runs["run_id"].astype("string").fillna("") != _text(exclude))
    ]
    return hit.iloc[0].to_dict() if not hit.empty else None


def _dm_has_live(runs: pd.DataFrame, dm_id: str, exclude: str = "") -> bool:
    if not dm_id or runs.empty:
        return False
    hit = runs[
        (runs["dm_id"].astype("string").fillna("") == _text(dm_id))
        & (runs["status"].astype("string").fillna("").isin(OPEN_STATUSES))
        & (runs["run_id"].astype("string").fillna("") != _text(exclude))
    ]
    return not hit.empty


def _new_run_row(
    *,
    session_id: str,
    dm_id: str | None,
    dm_name: str | None,
    mode: str,
    created_by: str,
    now: pd.Timestamp,
    code_hash: str,
    code_expires: pd.Timestamp,
) -> dict[str, Any]:
    return {
        "run_id": f"run-{uuid.uuid4().hex[:12]}",
        "session_id": session_id,
        "dm_id": dm_id,
        "dm_name": dm_name,
        "room_id": None,
        "room_name": None,
        "status": "pending",
        "mode": mode,
        "template_version": None,
        "template_snapshot": None,
        "current_stage_id": None,
        "current_seq": None,
        "stage_started_at": None,
        "stage_ends_at": None,
        "paused_at": None,
        "paused_total_seconds": 0,
        "remaining_seconds_at_pause": None,
        "last_activity_at": None,
        "hardware_degraded": False,
        "access_code_hash": code_hash,
        "access_code_expires_at": code_expires,
        "access_code_redeemed_at": None,
        "token_version": 0,
        "state_version": 0,
        "created_by": created_by,
        "created_at": now,
        "updated_at": now,
        "finished_at": None,
    }


def _new_code(store: Any, runs: pd.DataFrame, *, secret: str | None, ttl_minutes: float, now: pd.Timestamp) -> tuple[str, str, pd.Timestamp]:
    existing = set()
    if not runs.empty:
        existing = set(runs["access_code_hash"].astype("string").fillna(""))
    for _ in range(20):
        code = generate_access_code()
        code_hash = hash_access_code(code, secret=secret)
        if code_hash not in existing:
            return code, code_hash, now + pd.Timedelta(minutes=ttl_minutes)
    raise DMControlError("server_error", "场控码生成失败，请重试。")


def _hardware_adapter(hardware: HardwareAdapter | None) -> HardwareAdapter:
    return hardware if hardware is not None else build_hardware_adapter()


def create_run(
    store: Any,
    *,
    session_id: str,
    created_by: str,
    dm_id: str | None = None,
    dm_name: str | None = None,
    mode: str = "software",
    now: Any = None,
    secret: str | None = None,
    code_ttl_minutes: float | None = None,
    hardware: HardwareAdapter | None = None,
) -> dict[str, Any]:
    moment = _utc_now(now)
    mode = _text(mode) or "software"
    if mode not in MODES:
        raise DMControlError("invalid_action", "场控模式不合法。")
    adapter = _hardware_adapter(hardware)
    if mode == "hardware" and not adapter.configured:
        raise DMControlError(
            "hardware_not_configured", "尚未配置硬件 Skill，只能使用软件模式。"
        )
    ttl = (
        code_ttl_minutes
        if code_ttl_minutes is not None
        else _float_env("DM_CODE_TTL_MINUTES", 30)
    )
    ttl = max(5.0, min(float(ttl), 120.0))
    with store_lock(store):
        session = _session_row(store, session_id)
        if session is None:
            raise DMControlError("session_missing", "场次不存在。")
        if _text(session.get("status")) == STATUS_CANCELLED:
            raise DMControlError("session_cancelled", "场次已取消，不能创建场控。")
        _template_snapshot(store, session)
        runs = _load_runs(store)
        if _live_same_session(runs, session_id) is not None:
            raise DMControlError("run_active", "该场次已有未结束的场控。")
        chosen_dm = _text(dm_id) or None
        if chosen_dm:
            dms = load_dms(store=store)
            hit = (
                dms[dms["dm_id"].astype("string").fillna("") == chosen_dm]
                if not dms.empty
                else dms
            )
            if hit.empty:
                raise DMControlError("resource_not_found", "DM 不存在。")
            snapshot_name = _text(dm_name) or _text(hit.iloc[0].get("name")) or None
        else:
            snapshot_name = _text(dm_name) or None
        code, code_hash, expires = _new_code(
            store, runs, secret=secret, ttl_minutes=ttl, now=moment
        )
        row = _new_run_row(
            session_id=session_id,
            dm_id=chosen_dm,
            dm_name=snapshot_name,
            mode=mode,
            created_by=created_by,
            now=moment,
            code_hash=code_hash,
            code_expires=expires,
        )
        merged = pd.concat(
            [runs, coerce_frame(pd.DataFrame([row]), DM_RUNS_SPEC)],
            ignore_index=True,
        )
        _save_frame(store, merged, "runs")
        _append_events(
            store,
            [
                _event_row(
                    run_id=row["run_id"],
                    event_type="run_created",
                    stage_id=None,
                    stage_seq=None,
                    actor="merchant",
                    at=moment,
                    now=moment,
                    payload={"mode": mode, "dm_id": chosen_dm},
                ),
                _event_row(
                    run_id=row["run_id"],
                    event_type="code_issued",
                    stage_id=None,
                    stage_seq=None,
                    actor="merchant",
                    at=moment,
                    now=moment,
                ),
            ],
        )
        view = run_view(store, row, for_dm=False, now=moment)
        return {
            "run": view["run"],
            "session": view["session"],
            "stages": view["stages"],
            "server_now": view["server_now"],
            "warnings": view["warnings"],
            "code": code,
            "code_expires_at": _iso(expires),
            "template_preview": True,
        }


def find_run_by_code_hash(store: Any, code_hash: str) -> dict[str, Any] | None:
    finder = getattr(store, "find_dm_run_by_code_hash", None)
    if callable(finder):
        return finder(code_hash)
    runs = _load_runs(store)
    if runs.empty:
        return None
    hit = runs[
        runs["access_code_hash"].astype("string").fillna("") == _text(code_hash)
    ]
    return hit.iloc[0].to_dict() if not hit.empty else None


def redeem_code(
    store: Any,
    *,
    code: str,
    device_id: str | None = None,
    now: Any = None,
    secret: str | None = None,
    token_ttl_hours: float | None = None,
) -> dict[str, Any]:
    moment = _utc_now(now)
    code_hash = hash_access_code(code, secret=secret)
    with store_lock(store):
        run = find_run_by_code_hash(store, code_hash)
        if run is None:
            raise DMControlError("code_invalid", "场控码无效、已过期或已使用。")
        status = _text(run.get("status"))
        expires = run.get("access_code_expires_at")
        redeemed = run.get("access_code_redeemed_at")
        if status not in LIVE_STATUSES:
            raise DMControlError("code_invalid", "场控码无效、已过期或已使用。")
        if is_missing_value(expires) or _utc_now(expires) <= moment:
            raise DMControlError("code_invalid", "场控码无效、已过期或已使用。")
        if not is_missing_value(redeemed):
            raise DMControlError("code_invalid", "场控码无效、已过期或已使用。")
        values = {
            "access_code_redeemed_at": moment,
            "updated_at": moment,
            "last_activity_at": moment,
            "state_version": _int(run.get("state_version")) + 1,
        }
        expected = _int(run.get("state_version"))
        event = _event_row(
            run_id=_text(run.get("run_id")),
            event_type="code_redeemed",
            stage_id=_text(run.get("current_stage_id")) or None,
            stage_seq=_int(run.get("current_seq")) or None,
            actor="dm",
            at=moment,
            now=moment,
            payload={
                "device": (
                    hashlib.sha256(_text(device_id).encode("utf-8")).hexdigest()[:16]
                    if _text(device_id)
                    else None
                )
            },
        )
        if not _persist_run_change(store, run, values, [event], expected_version=expected):
            raise DMControlError("stale_state", "场控状态已变化，请重新兑换。")
        run.update(values)
        token, token_expires = issue_dm_token(
            store,
            run,
            secret=secret,
            ttl_hours=token_ttl_hours,
            now=moment,
        )
        view = run_view(store, run, for_dm=True, now=moment)
        return {
            "token": token,
            "token_expires_at": _iso(token_expires),
            "run": view["run"],
            "session": view["session"],
            "stages": view["stages"],
            "server_now": view["server_now"],
            "warnings": view["warnings"],
            "template_preview": view["template_preview"],
        }


def _issue_run_code_impl(
    store: Any,
    *,
    run: dict[str, Any],
    dm_id: Any = _UNSET,
    dm_name: Any = _UNSET,
    now: pd.Timestamp,
    secret: str | None,
    ttl_minutes: float,
) -> dict[str, Any]:
    status = _text(run.get("status"))
    if status not in LIVE_STATUSES:
        raise DMControlError("run_finished", "场控已经结束。")
    runs = _load_runs(store)
    chosen_dm = _text(run.get("dm_id")) or None
    chosen_name = _text(run.get("dm_name")) or None
    if dm_id is not _UNSET:
        chosen_dm = _text(dm_id) or None
        if chosen_dm:
            dms = load_dms(store=store)
            hit = (
                dms[dms["dm_id"].astype("string").fillna("") == chosen_dm]
                if not dms.empty
                else dms
            )
            if hit.empty:
                raise DMControlError("resource_not_found", "DM 不存在。")
            chosen_name = (
                _text(dm_name)
                or _text(hit.iloc[0].get("name"))
                or chosen_name
            )
        else:
            chosen_name = _text(dm_name) or None
    if _dm_has_live(runs, chosen_dm, exclude=_text(run.get("run_id"))):
        raise DMControlError("dm_busy", "该 DM 还有未结束的场控。")

    code, code_hash, expires = _new_code(
        store, runs, secret=secret, ttl_minutes=ttl_minutes, now=now
    )
    values = {
        "dm_id": chosen_dm,
        "dm_name": chosen_name,
        "access_code_hash": code_hash,
        "access_code_expires_at": expires,
        "access_code_redeemed_at": None,
        "token_version": _int(run.get("token_version")) + 1,
        "state_version": _int(run.get("state_version")) + 1,
        "updated_at": now,
        "last_activity_at": now,
    }
    expected = _int(run.get("state_version"))
    stage_id = _text(run.get("current_stage_id")) or None
    stage_seq = _int(run.get("current_seq")) or None
    events = [
        _event_row(
            run_id=_text(run.get("run_id")),
            event_type="token_revoked",
            stage_id=stage_id,
            stage_seq=stage_seq,
            actor="merchant",
            at=now,
            now=now,
        ),
        _event_row(
            run_id=_text(run.get("run_id")),
            event_type="code_issued",
            stage_id=stage_id,
            stage_seq=stage_seq,
            actor="merchant",
            at=now,
            now=now,
            payload={"dm_id": chosen_dm},
        ),
    ]
    if not _persist_run_change(store, run, values, events, expected_version=expected):
        raise DMControlError("stale_state", "场控状态已变化，请重试。")
    run.update(values)
    view = run_view(store, run, for_dm=False, now=now)
    return {
        "run": view["run"],
        "code": code,
        "code_expires_at": _iso(expires),
        "server_now": view["server_now"],
    }


def issue_run_code(
    store: Any,
    *,
    run_id: str,
    dm_id: Any = _UNSET,
    dm_name: Any = _UNSET,
    now: Any = None,
    secret: str | None = None,
    code_ttl_minutes: float | None = None,
) -> dict[str, Any]:
    moment = _utc_now(now)
    ttl = (
        code_ttl_minutes
        if code_ttl_minutes is not None
        else _float_env("DM_CODE_TTL_MINUTES", 30)
    )
    ttl = max(5.0, min(float(ttl), 120.0))
    with store_lock(store):
        run = _find_run(_load_runs(store), run_id)
        if run is None:
            raise DMControlError("run_not_found", "场控不存在。")
        return _issue_run_code_impl(
            store,
            run=run,
            dm_id=dm_id,
            dm_name=dm_name,
            now=moment,
            secret=secret,
            ttl_minutes=ttl,
        )


def revoke_run_token(
    store: Any,
    *,
    run_id: str,
    now: Any = None,
) -> dict[str, Any]:
    moment = _utc_now(now)
    with store_lock(store):
        run = _find_run(_load_runs(store), run_id)
        if run is None:
            raise DMControlError("run_not_found", "场控不存在。")
        if _text(run.get("status")) not in LIVE_STATUSES:
            raise DMControlError("run_finished", "场控已经结束。")
        values = {
            "token_version": _int(run.get("token_version")) + 1,
            "state_version": _int(run.get("state_version")) + 1,
            "access_code_hash": None,
            "access_code_expires_at": None,
            "access_code_redeemed_at": None,
            "updated_at": moment,
            "last_activity_at": moment,
        }
        event = _event_row(
            run_id=run_id,
            event_type="token_revoked",
            stage_id=_text(run.get("current_stage_id")) or None,
            stage_seq=_int(run.get("current_seq")) or None,
            actor="merchant",
            at=moment,
            now=moment,
        )
        if not _persist_run_change(
            store,
            run,
            values,
            [event],
            expected_version=_int(run.get("state_version")),
        ):
            raise DMControlError("stale_state", "场控状态已变化，请重试。")
        run.update(values)
        view = run_view(store, run, for_dm=False, now=moment)
        return {"run": view["run"], "server_now": view["server_now"]}


def abandon_run(
    store: Any,
    *,
    run_id: str,
    now: Any = None,
) -> dict[str, Any]:
    moment = _utc_now(now)
    with store_lock(store):
        run = _find_run(_load_runs(store), run_id)
        if run is None:
            raise DMControlError("run_not_found", "场控不存在。")
        if _text(run.get("status")) != "pending":
            raise DMControlError("invalid_action", "只能撤销尚未开始的场控。")
        values = {
            "status": "abandoned",
            "token_version": _int(run.get("token_version")) + 1,
            "state_version": _int(run.get("state_version")) + 1,
            "access_code_hash": None,
            "access_code_expires_at": None,
            "access_code_redeemed_at": None,
            "updated_at": moment,
            "finished_at": moment,
            "last_activity_at": moment,
        }
        event = _event_row(
            run_id=run_id,
            event_type="run_abandoned",
            stage_id=None,
            stage_seq=None,
            actor="merchant",
            at=moment,
            now=moment,
        )
        if not _persist_run_change(
            store,
            run,
            values,
            [event],
            expected_version=_int(run.get("state_version")),
        ):
            raise DMControlError("stale_state", "场控状态已变化，请重试。")
        run.update(values)
        view = run_view(store, run, for_dm=False, now=moment)
        return {"run": view["run"], "server_now": view["server_now"]}


# ---------------------------------------------------------------------------
# DM 状态机
# ---------------------------------------------------------------------------


def _error(code: str, message: str, *, run: dict[str, Any] | None = None, store: Any = None, now: Any = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"ok": False, "code": code, "error": message}
    if run is not None and store is not None:
        payload["run"] = run_view(store, run, for_dm=True, now=now)["run"]
        payload["server_now"] = _iso(_utc_now(now))
    return payload


def _hardware_view(result: HardwareResult) -> dict[str, Any]:
    return result.to_dict()


def _software_skipped() -> dict[str, Any]:
    return {
        "ok": False,
        "status": "skipped",
        "status_code": None,
        "latency_ms": None,
        "message": "软件模式只显示文字提示。",
    }


def _scene_for(stage: dict[str, Any] | None) -> dict[str, Any] | None:
    if not stage:
        return None
    scene = stage.get("hardware_scene")
    if isinstance(scene, dict):
        return scene or None
    try:
        return parse_hardware_scene(scene)
    except StageTemplateError:
        return None


def _finish_hardware(
    store: Any,
    *,
    response: dict[str, Any],
    request: dict[str, Any],
    result: HardwareResult,
    clear_on_success: bool,
    now: pd.Timestamp,
) -> dict[str, Any]:
    degraded = bool(result.status == "failed")
    values: dict[str, Any] = {"updated_at": now, "last_activity_at": now}
    if degraded:
        values["hardware_degraded"] = True
    elif clear_on_success and result.ok:
        values["hardware_degraded"] = False
    event_type = "hardware_ok" if result.ok else "hardware_failed"
    event = _event_row(
        run_id=request["run_id"],
        event_type=event_type,
        stage_id=request["stage"].get("stage_id"),
        stage_seq=request["stage"].get("seq"),
        actor="system",
        at=now,
        now=now,
        payload={
            "status": result.status,
            "status_code": result.status_code,
            "latency_ms": result.latency_ms,
            "message": result.message,
        },
    )
    runs = _load_runs(store)
    run = _find_run(runs, request["run_id"])
    if run is None:
        response["hardware"] = _hardware_view(result)
        return response
    _persist_run_change(store, run, values, [event])
    run.update(values)
    response["hardware"] = _hardware_view(result)
    response["run"] = run_view(store, run, for_dm=True, now=now)["run"]
    return response


def _apply_impl(
    store: Any,
    *,
    run: dict[str, Any],
    action: str,
    client_event_id: str,
    expected_version: int | None,
    at: Any,
    now: pd.Timestamp,
    minutes: int | None,
    mode: str | None,
    replay: bool,
    hardware: HardwareAdapter,
    actor: str = "dm",
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    run_id = _text(run.get("run_id"))
    events = _load_events(store)
    if client_event_id and _find_event(events, run_id, client_event_id) is not None:
        view = run_view(store, run, for_dm=True, now=now)
        return (
            {
                "ok": True,
                "duplicate": True,
                "run": view["run"],
                "session": view["session"],
                "stages": view["stages"],
                "current_stage": view["current_stage"],
                "server_now": view["server_now"],
                "warnings": view["warnings"],
                "hardware": None,
            },
            None,
        )
    current_version = _int(run.get("state_version"))
    if expected_version is not None and int(expected_version) != current_version:
        view = run_view(store, run, for_dm=True, now=now)
        return (
            {
                "ok": False,
                "code": "stale_state",
                "error": "场控状态已在另一台设备更新，请重新同步。",
                "run": view["run"],
                "session": view["session"],
                "stages": view["stages"],
                "current_stage": view["current_stage"],
                "server_now": view["server_now"],
                "warnings": view["warnings"],
            },
            None,
        )

    status = _text(run.get("status"))
    event_at, warnings = _clamp_at(run, at, now)
    event_type = ACTION_TYPES.get(action)
    if event_type is None:
        return _error("invalid_action", "未知的场控动作。", run=run, store=store, now=now), None

    values: dict[str, Any] = {
        "updated_at": now,
        "last_activity_at": event_at,
        "state_version": current_version + 1,
    }
    snapshot = _parse_snapshot(run)
    stages = snapshot.get("stages", [])
    current_seq = _int(run.get("current_seq"), 0) or None
    current_stage = _stage_at(snapshot, current_seq)
    hardware_request: dict[str, Any] | None = None
    payload: dict[str, Any] = {}
    hardware_status: dict[str, Any] | None = None
    replay_event: dict[str, Any] | None = None

    if action == "start":
        if status != "pending":
            return _error("invalid_action", "场控已经开始或结束。", run=run, store=store, now=now), None
        session = _session_row(store, _text(run.get("session_id")))
        if session is None:
            return _error("session_missing", "场次已不存在，不能开始。", run=run, store=store, now=now), None
        if _text(session.get("status")) == STATUS_CANCELLED:
            return _error("session_cancelled", "场次已取消，不能开始。", run=run, store=store, now=now), None
        if run.get("dm_id") and _dm_has_live(
            _load_runs(store), _text(run.get("dm_id")), exclude=run_id
        ):
            return _error("dm_busy", "该 DM 还有未结束的场控。", run=run, store=store, now=now), None
        try:
            fresh_snapshot, version = _template_snapshot(store, session)
        except DMControlError as exc:
            return _error(exc.code, exc.message, run=run, store=store, now=now), None
        stages = fresh_snapshot["stages"]
        if not stages:
            return _error("template_missing", "该剧本没有启用的阶段模板。", run=run, store=store, now=now), None
        first = stages[0]
        room_id, room_name = _room_assignment(store, _text(run.get("session_id")))
        started = event_at
        ends = started + pd.Timedelta(minutes=_int(first.get("duration_minutes"), 240))
        values.update(
            {
                "status": "active",
                "template_version": version,
                "template_snapshot": _json_text(fresh_snapshot, 65536),
                "current_stage_id": _text(first.get("stage_id")),
                "current_seq": 1,
                "stage_started_at": started,
                "stage_ends_at": ends,
                "paused_at": None,
                "paused_total_seconds": 0,
                "remaining_seconds_at_pause": None,
                "room_id": room_id,
                "room_name": room_name,
            }
        )
        snapshot = fresh_snapshot
        current_stage = first
        current_seq = 1
        payload = {"template_version": version}
        if _text(run.get("mode")) == "hardware":
            scene = _scene_for(first)
            if not scene:
                hardware_status = {"ok": False, "status": "skipped", "message": "第一幕没有配置硬件场景。"}
            elif run.get("hardware_degraded") is True:
                hardware_status = {"ok": False, "status": "skipped", "message": "硬件已降级，使用文字提示。"}
            elif replay:
                replay_event = _event_row(
                    run_id=run_id,
                    event_type="hardware_skipped_replay",
                    stage_id=_text(first.get("stage_id")),
                    stage_seq=1,
                    actor="system",
                    at=event_at,
                    now=now,
                )
                hardware_status = {"ok": False, "status": "skipped", "message": "离线补传不补发硬件指令。"}
            else:
                hardware_request = {
                    "run_id": run_id,
                    "session_id": _text(run.get("session_id")),
                    "room_id": room_id,
                    "stage": first,
                    "event_id": client_event_id,
                }
        else:
            hardware_status = _software_skipped()

    elif action in {"next", "previous"}:
        if status == "paused":
            return _error("run_paused", "场控已暂停，请先继续。", run=run, store=store, now=now), None
        if status != "active":
            return _error("invalid_action", "只有进行中的场控可以切幕。", run=run, store=store, now=now), None
        if not stages or current_seq is None:
            return _error("template_missing", "本场控没有可用的阶段快照。", run=run, store=store, now=now), None
        target_seq = current_seq + (1 if action == "next" else -1)
        if target_seq > len(stages):
            return _error("at_last_stage", "已经是最后一幕，如需结束请点结束场控。", run=run, store=store, now=now), None
        if target_seq < 1:
            return _error("at_first_stage", "已经是第一幕。", run=run, store=store, now=now), None
        target = _stage_at(snapshot, target_seq)
        if target is None:
            return _error("template_missing", "阶段快照不完整。", run=run, store=store, now=now), None
        started = event_at
        ends = started + pd.Timedelta(minutes=_int(target.get("duration_minutes"), 240))
        values.update(
            {
                "current_stage_id": _text(target.get("stage_id")),
                "current_seq": target_seq,
                "stage_started_at": started,
                "stage_ends_at": ends,
                "paused_at": None,
                "remaining_seconds_at_pause": None,
            }
        )
        payload = {"from_seq": current_seq, "to_seq": target_seq}
        current_stage = target
        current_seq = target_seq
        if _text(run.get("mode")) == "hardware":
            scene = _scene_for(target)
            if not scene:
                hardware_status = {"ok": False, "status": "skipped", "message": "当前阶段没有配置硬件场景。"}
            elif run.get("hardware_degraded") is True:
                hardware_status = {"ok": False, "status": "skipped", "message": "硬件已降级，使用文字提示。"}
            elif replay:
                replay_event = _event_row(
                    run_id=run_id,
                    event_type="hardware_skipped_replay",
                    stage_id=_text(target.get("stage_id")),
                    stage_seq=target_seq,
                    actor="system",
                    at=event_at,
                    now=now,
                )
                hardware_status = {"ok": False, "status": "skipped", "message": "离线补传不补发硬件指令。"}
            else:
                hardware_request = {
                    "run_id": run_id,
                    "session_id": _text(run.get("session_id")),
                    "room_id": _text(run.get("room_id")) or None,
                    "stage": target,
                    "event_id": client_event_id,
                }
        else:
            hardware_status = _software_skipped()

    elif action == "pause":
        if status != "active":
            return _error("invalid_action", "只有进行中的场控可以暂停。", run=run, store=store, now=now), None
        ends = run.get("stage_ends_at")
        remaining = 0
        if not is_missing_value(ends):
            remaining = max(0, int((_utc_now(ends) - event_at).total_seconds()))
        values.update(
            {
                "status": "paused",
                "paused_at": event_at,
                "remaining_seconds_at_pause": remaining,
            }
        )
        payload = {"remaining_seconds": remaining}

    elif action == "resume":
        if status != "paused":
            return _error("invalid_action", "只有已暂停的场控可以继续。", run=run, store=store, now=now), None
        paused_at = run.get("paused_at")
        paused_seconds = 0
        if not is_missing_value(paused_at):
            paused_seconds = max(0, int((event_at - _utc_now(paused_at)).total_seconds()))
        remaining = max(0, _int(run.get("remaining_seconds_at_pause")))
        values.update(
            {
                "status": "active",
                "stage_ends_at": event_at + pd.Timedelta(seconds=remaining),
                "paused_at": None,
                "paused_total_seconds": _int(run.get("paused_total_seconds")) + paused_seconds,
                "remaining_seconds_at_pause": None,
            }
        )
        payload = {"remaining_seconds": remaining, "paused_seconds": paused_seconds}

    elif action == "extend":
        if status not in OPEN_STATUSES:
            return _error("invalid_action", "只有进行中或暂停的场控可以延长。", run=run, store=store, now=now), None
        minutes_value = _int(minutes, 0)
        if minutes_value < 1 or minutes_value > 20:
            return _error("invalid_action", "延长分钟数必须在 1..20。", run=run, store=store, now=now), None
        if status == "active":
            ends = run.get("stage_ends_at")
            base = event_at if is_missing_value(ends) else _utc_now(ends)
            values["stage_ends_at"] = base + pd.Timedelta(minutes=minutes_value)
        else:
            values["remaining_seconds_at_pause"] = max(
                0, _int(run.get("remaining_seconds_at_pause"))
            ) + minutes_value * 60
        payload = {"minutes": minutes_value}

    elif action == "finish":
        if status not in OPEN_STATUSES:
            return _error("invalid_action", "只有进行中或暂停的场控可以结束。", run=run, store=store, now=now), None
        values.update(
            {
                "status": "completed",
                "finished_at": event_at,
                "access_code_hash": None,
                "access_code_expires_at": None,
                "access_code_redeemed_at": None,
                "token_version": _int(run.get("token_version")) + 1,
            }
        )
        payload = {"current_seq": current_seq}

    elif action == "mode":
        if status not in LIVE_STATUSES:
            return _error("invalid_action", "场控已经结束。", run=run, store=store, now=now), None
        target_mode = _text(mode)
        if target_mode not in MODES:
            return _error("invalid_action", "场控模式不合法。", run=run, store=store, now=now), None
        if target_mode == "hardware" and not hardware.configured:
            return _error("hardware_not_configured", "尚未配置硬件 Skill。", run=run, store=store, now=now), None
        values["mode"] = target_mode
        if target_mode == "software":
            values["hardware_degraded"] = False
        payload = {"mode": target_mode}

    elif action == "hardware_retry":
        if status not in OPEN_STATUSES:
            return _error("invalid_action", "只有进行中或暂停的场控可以重试硬件。", run=run, store=store, now=now), None
        if _text(run.get("mode")) != "hardware":
            return _error("invalid_action", "当前不是硬件模式。", run=run, store=store, now=now), None
        if not hardware.configured:
            return _error("hardware_not_configured", "尚未配置硬件 Skill。", run=run, store=store, now=now), None
        scene = _scene_for(current_stage)
        if not current_stage or not scene:
            return _error("invalid_action", "当前阶段没有配置硬件场景。", run=run, store=store, now=now), None
        payload = {"retry": True}
        if replay:
            replay_event = _event_row(
                run_id=run_id,
                event_type="hardware_skipped_replay",
                stage_id=_text(current_stage.get("stage_id")),
                stage_seq=current_seq,
                actor="system",
                at=event_at,
                now=now,
            )
            hardware_status = {"ok": False, "status": "skipped", "message": "离线补传不补发硬件指令。"}
        else:
            hardware_request = {
                "run_id": run_id,
                "session_id": _text(run.get("session_id")),
                "room_id": _text(run.get("room_id")) or None,
                "stage": current_stage,
                "event_id": client_event_id,
            }

    event = _event_row(
        run_id=run_id,
        event_type=event_type,
        stage_id=_text(values.get("current_stage_id", run.get("current_stage_id"))) or None,
        stage_seq=_int(values.get("current_seq", run.get("current_seq"))) or None,
        actor=actor,
        at=event_at,
        now=now,
        event_id=client_event_id or None,
        payload=payload or None,
    )
    event_rows = [event]
    if replay_event is not None:
        event_rows.append(replay_event)

    if not _persist_run_change(
        store,
        run,
        values,
        event_rows,
        expected_version=current_version,
    ):
        current = _find_run(_load_runs(store), run_id)
        view = run_view(store, current or run, for_dm=True, now=now)
        return (
            {
                "ok": False,
                "code": "stale_state",
                "error": "场控状态已更新，请重新同步。",
                "run": view["run"],
                "server_now": view["server_now"],
            },
            None,
        )

    run.update(values)
    view = run_view(store, run, for_dm=True, now=now)
    response: dict[str, Any] = {
        "ok": True,
        "duplicate": False,
        "run": view["run"],
        "session": view["session"],
        "stages": view["stages"],
        "current_stage": view["current_stage"],
        "event": _event_payload(event),
        "server_now": view["server_now"],
        "warnings": view["warnings"] + warnings,
        "hardware": hardware_status,
    }
    return response, hardware_request


def apply_action(
    store: Any,
    *,
    run_id: str,
    action: str,
    client_event_id: str | None = None,
    expected_version: int | None = None,
    at: Any = None,
    minutes: int | None = None,
    mode: str | None = None,
    replay: bool = False,
    actor: str = "dm",
    hardware: HardwareAdapter | None = None,
    now: Any = None,
) -> dict[str, Any]:
    moment = _utc_now(now)
    event_id = _text(client_event_id) or f"evt-{uuid.uuid4().hex[:16]}"
    adapter = _hardware_adapter(hardware)
    with store_lock(store):
        run = _find_run(_load_runs(store), run_id)
        if run is None:
            return _error("run_not_found", "场控不存在。", now=moment)
        status = _text(run.get("status"))
        if status in {"completed", "abandoned"}:
            return _error("run_finished", "场控已经结束。", run=run, store=store, now=moment)
        response, request = _apply_impl(
            store,
            run=run,
            action=action,
            client_event_id=event_id,
            expected_version=expected_version,
            at=at,
            now=moment,
            minutes=minutes,
            mode=mode,
            replay=replay,
            hardware=adapter,
            actor=actor,
        )

    if request is None:
        return response

    result = adapter.send_scene(
        run_id=request["run_id"],
        session_id=request["session_id"],
        room_id=request.get("room_id"),
        stage=request["stage"],
        event_id=request.get("event_id"),
    )
    with store_lock(store):
        return _finish_hardware(
            store,
            response=response,
            request=request,
            result=result,
            clear_on_success=(action == "hardware_retry"),
            now=moment,
        )


def sync_actions(
    store: Any,
    *,
    run_id: str,
    actions: Iterable[dict[str, Any]],
    now: Any = None,
) -> dict[str, Any]:
    """离线队列按顺序补传；逐条 CAS，冲突即停止。"""
    moment = _utc_now(now)
    results: list[dict[str, Any]] = []
    with store_lock(store):
        run = _find_run(_load_runs(store), run_id)
        if run is None:
            return {"ok": False, "code": "run_not_found", "error": "场控不存在。", "results": []}
        for index, item in enumerate(actions):
            if not isinstance(item, dict):
                results.append(
                    {"ok": False, "code": "invalid_action", "error": "动作必须是对象。", "index": index}
                )
                break
            action = _text(item.get("action"))
            event_id = _text(item.get("client_event_id")) or f"evt-{uuid.uuid4().hex[:16]}"
            response, _request = _apply_impl(
                store,
                run=run,
                action=action,
                client_event_id=event_id,
                expected_version=item.get("expected_version"),
                at=item.get("at"),
                now=moment,
                minutes=item.get("minutes"),
                mode=item.get("mode"),
                replay=True,
                hardware=_hardware_adapter(None),
                actor="dm",
            )
            results.append(response)
            run = _find_run(_load_runs(store), run_id)
            if run is None or not response.get("ok"):
                break
        current = _find_run(_load_runs(store), run_id)
        view = run_view(store, current, for_dm=True, now=moment) if current else None
        return {
            "ok": bool(results) and all(item.get("ok") for item in results),
            "results": results,
            "run": view["run"] if view else None,
            "server_now": _iso(moment),
        }


# ---------------------------------------------------------------------------
# 列表、事件与复盘
# ---------------------------------------------------------------------------


def list_events(
    store: Any,
    *,
    run_id: str,
    after: Any = None,
    include_payload: bool = False,
) -> list[dict[str, Any]]:
    frame = _load_events(store)
    if frame.empty:
        return []
    hit = frame[frame["run_id"].astype("string").fillna("") == _text(run_id)].copy()
    if after is not None and not is_missing_value(after):
        cutoff = _utc_now(after)
        hit = hit[pd.to_datetime(hit["at"], errors="coerce") >= cutoff]
    hit = hit.sort_values(["at", "event_id"])
    result: list[dict[str, Any]] = []
    for row in hit.to_dict("records"):
        item = _event_payload(row)
        if not include_payload:
            item.pop("payload", None)
        result.append(item)
    return result


def list_runs_today(
    store: Any,
    *,
    day: Any = None,
    now: Any = None,
) -> list[dict[str, Any]]:
    moment = _utc_now(now)
    target = pd.Timestamp(day).date() if day is not None else pd.Timestamp.now().date()
    sessions = load_sessions_with_counts(store=store)
    starts: dict[str, pd.Timestamp] = {}
    if not sessions.empty:
        for row in sessions.to_dict("records"):
            starts[_text(row.get("session_id"))] = pd.Timestamp(row.get("start_time"))
    runs = _load_runs(store)
    if runs.empty:
        return []
    items: list[dict[str, Any]] = []
    for row in runs.to_dict("records"):
        status = _text(row.get("status"))
        session_start = starts.get(_text(row.get("session_id")))
        same_day = session_start is not None and session_start.date() == target
        if status not in OPEN_STATUSES and not same_day:
            continue
        view = run_view(store, row, for_dm=False, now=moment)
        items.append(
            {
                "run": view["run"],
                "session": view["session"],
                "server_now": view["server_now"],
                "warnings": view["warnings"],
            }
        )
    items.sort(key=lambda item: item["run"].get("created_at") or "")
    return items


def run_report(store: Any, *, run_id: str, now: Any = None) -> dict[str, Any]:
    moment = _utc_now(now)
    run = _find_run(_load_runs(store), run_id)
    if run is None:
        raise DMControlError("run_not_found", "场控不存在。")
    if _text(run.get("status")) != "completed":
        raise DMControlError("run_active", "场控还没有结束，暂不生成复盘。")
    snapshot = _parse_snapshot(run)
    planned = {_int(stage.get("seq")): stage for stage in snapshot.get("stages", [])}
    events = _load_events(store)
    if not events.empty:
        events = events[
            (events["run_id"].astype("string").fillna("") == _text(run_id))
        ].sort_values(["at", "event_id"])
    starts: dict[int, pd.Timestamp] = {}
    durations: dict[int, int] = {}
    pause_count = 0
    hardware_failed = 0
    current = _int(run.get("current_seq"), 0) or None
    started_at = run.get("stage_started_at")
    if current is not None and not is_missing_value(started_at):
        starts[current] = _utc_now(started_at)
    for row in events.to_dict("records") if not events.empty else []:
        event_type = _text(row.get("event_type"))
        at = _utc_now(row.get("at"))
        if event_type in {"stage_next", "stage_previous", "run_started"}:
            target = _int(row.get("stage_seq"), 0) or None
            if current is not None and current in starts:
                durations[current] = max(
                    0, int((at - starts[current]).total_seconds())
                )
            if target is not None:
                current = target
                starts[target] = at
        elif event_type == "stage_paused":
            pause_count += 1
        elif event_type == "hardware_failed":
            hardware_failed += 1
        elif event_type == "run_finished":
            if current is not None and current in starts:
                durations[current] = max(
                    0, int((at - starts[current]).total_seconds())
                )
    total = 0
    started = None
    if not events.empty:
        started_events = events[
            events["event_type"].astype("string").fillna("") == "run_started"
        ]
        if not started_events.empty:
            started = started_events.iloc[0].get("at")
    if started is None:
        started = run.get("created_at")
    if started is not None and not is_missing_value(started) and run.get("finished_at") and not is_missing_value(run.get("finished_at")):
        total = max(
            0,
            int((_utc_now(run.get("finished_at")) - _utc_now(started)).total_seconds()),
        )
    stage_rows = []
    for seq, stage in sorted(planned.items()):
        actual = durations.get(seq, 0)
        planned_seconds = _int(stage.get("duration_minutes")) * 60
        stage_rows.append(
            {
                "seq": seq,
                "name": stage.get("name"),
                "planned_seconds": planned_seconds,
                "actual_seconds": actual,
                "over_planned": actual > planned_seconds if actual else False,
            }
        )
    view = run_view(store, run, for_dm=False, now=moment)
    return {
        "run": view["run"],
        "session": view["session"],
        "total_seconds": total,
        "paused_total_seconds": _int(run.get("paused_total_seconds")),
        "pause_count": pause_count,
        "hardware_failed_count": hardware_failed,
        "stages": stage_rows,
    }


__all__ = [
    "ACTION_TYPES",
    "CODE_ALPHABET",
    "DMControlError",
    "LIVE_STATUSES",
    "MODES",
    "OPEN_STATUSES",
    "RUN_STATUSES",
    "abandon_run",
    "apply_action",
    "create_run",
    "find_run_by_code_hash",
    "generate_access_code",
    "get_run",
    "hash_access_code",
    "issue_dm_token",
    "issue_run_code",
    "list_events",
    "list_runs_today",
    "normalize_code",
    "parse_dm_token",
    "redeem_code",
    "revoke_run_token",
    "run_report",
    "run_view",
    "sync_actions",
    "verify_dm_token",
]

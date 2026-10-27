"""场次增量录入（硬必填齐全当场建场）与已保存场次的修正。

硬必填只有「剧本全名」和「开场时间」。两项齐全时直接建场，由 assignment
模块自动排房/排 DM；不齐时只返回追问问题，不落任何数据（没有草稿概念）。
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

import pandas as pd

from app.tools.assignment import (
    _duration_for,
    auto_assign,
    buffered_overlap_hits,
    load_schedule_context,
)
from app.tools.frame import (
    coerce_frame,
    json_safe,
)
from app.tools.resources import ASSIGNMENTS_SPEC
from app.tools.scripts import (
    load_scripts,
    normalize_script_name,
    split_aliases,
)
from app.tools.storage import (
    BOOKING_ACTIVE,
    STATUS_PENDING,
    VALID_STATUS,
    _upsert_sessions_impl,
    coerce_sessions,
    is_missing_value,
    load_bookings,
    load_sessions,
    load_sessions_with_counts,
    make_booking_id,
    make_session_id,
    save_bookings,
    save_sessions,
    store_lock,
)

HARD_REQUIRED = ("script_name", "start_time")
RECOMMENDED_FIELDS = ("total_seats", "list_price", "script_type")
ALLOWED_FIELDS = {
    "store_name",
    "script_name",
    "script_type",
    "start_time",
    "start_time_text",
    "total_seats",
    "list_price",
    "notes",
}
UPDATE_FIELDS = ALLOWED_FIELDS | {"status"}
_PHONE_RE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
_MAX_SOURCE_TEXT = 500
_MAX_CANDIDATES = 5


class SessionIntakeError(ValueError):
    """可直接返回给调用方的业务失败。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _now(now: Any = None) -> pd.Timestamp:
    if now is None:
        return pd.Timestamp.now().floor("s")
    moment = pd.Timestamp(now)
    if moment.tzinfo is not None:
        moment = moment.tz_convert("Asia/Shanghai").tz_localize(None)
    return moment.floor("s")


def _text(value: Any) -> str:
    if is_missing_value(value):
        return ""
    return " ".join(str(value).strip().split())


def _as_text(value: Any) -> str | None:
    text = _text(value)
    return text or None


def _to_int(value: Any) -> int | None:
    if is_missing_value(value):
        return None
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _to_price(value: Any) -> float | None:
    if is_missing_value(value):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def _parse_start(value: Any) -> pd.Timestamp | None:
    if is_missing_value(value):
        return None
    try:
        moment = pd.Timestamp(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if pd.isna(moment):
        return None
    if moment.tzinfo is not None:
        moment = moment.tz_convert("Asia/Shanghai").tz_localize(None)
    return moment.floor("min")


def _redact_source(value: Any) -> str | None:
    text = _text(value)
    if not text:
        return None
    text = _PHONE_RE.sub("[手机号]", text)
    return text[:_MAX_SOURCE_TEXT]


def _script_rows(store: Any, public: pd.DataFrame | None = None) -> pd.DataFrame:
    """私有库 + 公共库遮蔽合并（public 为空时仅私有）。

    懒导入：public_library 依赖 scripts 模块，顶层互导会成环。
    """
    frame = load_scripts(store=store)
    if public is None or getattr(public, "empty", True):
        return frame
    from app.tools.public_library import merge_with_shadow

    return merge_with_shadow(frame, public)


def _match_script(
    name: str,
    scripts: pd.DataFrame,
) -> tuple[str, dict[str, Any] | None, str, list[dict[str, Any]]]:
    """返回 (match_type, 命中档案, 规范名, 候选)。"""
    key = normalize_script_name(name).casefold()
    if not key or scripts.empty:
        return "none", None, name, []

    names = scripts["script_name"].astype("string").fillna("")
    for index, value in names.items():
        if normalize_script_name(value).casefold() == key:
            row = scripts.loc[index].to_dict()
            return "exact", row, _text(row.get("script_name")) or name, []

    for index, row in scripts.iterrows():
        for alias in split_aliases(row.get("aliases")):
            if normalize_script_name(alias).casefold() == key:
                record = row.to_dict()
                return "alias", record, _text(record.get("script_name")) or name, []

    candidates: list[tuple[int, int, dict[str, Any]]] = []
    if len(key) >= 2:
        for _, row in scripts.iterrows():
            full = normalize_script_name(row.get("script_name"))
            full_key = full.casefold()
            if not full_key:
                continue
            alias_keys = [
                normalize_script_name(alias).casefold()
                for alias in split_aliases(row.get("aliases"))
            ]
            if key in full_key or full_key in key or any(
                key in alias or alias in key for alias in alias_keys if alias
            ):
                distance = abs(len(full_key) - len(key))
                candidates.append((distance, len(full_key), row.to_dict()))
    candidates.sort(key=lambda item: (item[0], item[1]))
    picked = [item[2] for item in candidates[:_MAX_CANDIDATES]]
    return ("candidate" if picked else "none"), None, name, picked


def _candidate_dict(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "script_id": json_safe(record.get("script_id")),
        "script_name": json_safe(record.get("script_name")),
        "script_type": json_safe(record.get("script_type")),
        "total_seats": json_safe(record.get("total_seats")),
        "list_price": json_safe(record.get("list_price")),
    }


def _fields_payload(
    record: dict[str, Any], *, inferred: list[str] | None = None
) -> dict[str, Any]:
    return {
        "store_name": json_safe(record.get("store_name")),
        "script_name": json_safe(record.get("script_name")),
        "script_id": json_safe(record.get("script_id")),
        "script_type": json_safe(record.get("script_type")),
        "start_time": (
            pd.Timestamp(record["start_time"]).strftime("%Y-%m-%d %H:%M")
            if not is_missing_value(record.get("start_time"))
            else None
        ),
        "start_time_text": json_safe(record.get("start_time_text")),
        "total_seats": json_safe(record.get("total_seats")),
        "list_price": json_safe(record.get("list_price")),
        "notes": json_safe(record.get("notes")),
    }


def _question(
    *,
    script_name: str,
    start: pd.Timestamp | None,
    candidates: list[dict[str, Any]],
    fields: dict[str, Any],
) -> str:
    if candidates:
        names = "、".join(
            f"《{_text(item.get('script_name'))}》" for item in candidates
        )
        return f"本地库里找到了几个可能的本：{names}，是哪一个？"
    if not script_name:
        if start is not None:
            return f"时间我先记下 {start.strftime('%m月%d日 %H:%M')}；这场开哪个本？"
        return "这场开哪个本？"
    if start is None:
        return f"已经记下《{script_name}》，还差开场时间；这场定在几点？"
    seats = fields.get("total_seats")
    price = fields.get("list_price")
    bits = ""
    if seats:
        bits += f"，{int(seats)} 人"
    if price is not None:
        bits += f"，{float(price):g} 元"
    return (
        f"我理解为 {start.strftime('%m月%d日 %H:%M')}《{script_name}》{bits}；"
        "确认无误就建场。"
    )


def _existing_session(store: Any, session_id: str) -> dict[str, Any] | None:
    frame = load_sessions(store=store)
    if frame.empty:
        return None
    hit = frame[frame["session_id"].astype("string").fillna("") == session_id]
    return hit.iloc[0].to_dict() if not hit.empty else None


def _promote(
    *,
    store: Any,
    record: dict[str, Any],
    now: pd.Timestamp,
) -> tuple[dict[str, Any], dict[str, Any]]:
    store_name = _text(record.get("store_name"))
    script_name = _text(record.get("script_name"))
    start = _parse_start(record.get("start_time"))
    if not store_name or not script_name or start is None:
        raise SessionIntakeError("incomplete_session", "剧本全名与开场时间都齐全后才能建场。")

    total_seats = _to_int(record.get("total_seats"))
    price = record.get("list_price")
    price = None if is_missing_value(price) else float(price)

    row = {
        "store_name": store_name,
        "script_name": script_name,
        "script_type": _text(record.get("script_type")) or None,
        "start_time": start,
        "total_seats": total_seats,
        "list_price": price,
        "status": STATUS_PENDING,
        "source_text": _redact_source(record.get("source_text")),
    }
    session_id = make_session_id(store_name, script_name, start)

    existing = _existing_session(store, session_id)
    if existing is not None:
        bookings = load_bookings(store=store)
        booked = int(
            bookings.loc[
                (bookings["session_id"] == session_id)
                & (bookings["status"] == BOOKING_ACTIVE),
                "seats",
            ]
            .fillna(0)
            .sum()
        )
        if total_seats is not None and booked > total_seats:
            raise SessionIntakeError("capacity_exceeded", "新人数小于已约人数。")
    _upsert_sessions_impl([row], store=store)
    session = _existing_session(store, session_id) or {}
    return session, {"session_id": session_id}


def _create_session_impl(
    *,
    fields: dict[str, Any] | None,
    source_text: str | None = None,
    default_store: str = "迷雾剧场",
    path: Path | None = None,
    store: Any = None,
    now: Any = None,
    public: pd.DataFrame | None = None,
) -> dict[str, Any]:
    moment = _now(now)
    payload = dict(fields or {})
    if "room_id" in payload or "dm_id" in payload:
        raise SessionIntakeError(
            "room_not_allowed",
            "场次录入不接受房间或 DM，建场后由系统自动分配。",
        )
    meaningful = False
    for key in list(payload):
        if key not in ALLOWED_FIELDS:
            payload.pop(key)
            continue
        if key != "start_time_text" and not is_missing_value(payload.get(key)):
            meaningful = True
    if not meaningful and not _text(source_text):
        raise SessionIntakeError("bad_request", "没有可识别的场次字段。")

    record: dict[str, Any] = {"store_name": default_store or "迷雾剧场"}

    inferred: list[str] = []
    for key, value in payload.items():
        if key == "start_time":
            parsed = _parse_start(value)
            if parsed is not None:
                record["start_time"] = parsed
            elif not is_missing_value(value):
                record["start_time"] = pd.NaT
                record["start_time_text"] = _text(value)
            continue
        if key == "start_time_text":
            text = _text(value)
            if text:
                record["start_time_text"] = text
                if is_missing_value(record.get("start_time")):
                    parsed = _parse_start(text)
                    if parsed is not None:
                        record["start_time"] = parsed
                        inferred.append("start_time")
            continue
        if key == "script_name":
            text = _text(value)
            if text:
                record["script_name"] = text
                record["script_id"] = None
            continue
        if key in {"total_seats", "list_price", "script_type", "store_name", "notes"}:
            if is_missing_value(value):
                continue
            if key == "total_seats":
                number = _to_int(value)
                if number is None:
                    continue
                record[key] = number
            elif key == "list_price":
                number = _to_price(value)
                if number is None:
                    continue
                record[key] = number
            else:
                record[key] = _text(value)

    if not _text(record.get("store_name")):
        record["store_name"] = default_store or "迷雾剧场"

    scripts = _script_rows(store, public)
    script_name = _text(record.get("script_name"))
    match_type, script, canonical_name, candidates = _match_script(
        script_name, scripts
    )
    warnings: list[str] = []
    script_source = ""
    # 唯一候选快速通道（WP12）：候选恰好一个时视同命中，免去一轮确认；
    # 一个名字对上多个本（≥2 候选）仍走 needs_confirmation。
    if match_type == "candidate" and len(candidates) == 1:
        script = candidates[0]
        canonical_name = _text(script.get("script_name")) or script_name
        match_type = "unique"
        warnings.append(f"唯一匹配自动确认，请核对剧本全名《{canonical_name}》")
    if match_type in {"exact", "alias", "unique"} and script is not None:
        if canonical_name and canonical_name != script_name:
            record["script_name"] = canonical_name
            script_name = canonical_name
        from app.tools.public_library import is_public_script_id

        if is_public_script_id(script.get("script_id")):
            # 场次永不指向公共行：只借用元数据，script_id 保持 None。
            record["script_id"] = None
            script_source = "公共库"
            warnings.append("剧本信息来自公共库，请核对")
        else:
            record["script_id"] = _text(script.get("script_id")) or None
            script_source = "私有"
        if is_missing_value(record.get("script_type")) and not is_missing_value(
            script.get("script_type")
        ):
            record["script_type"] = _text(script.get("script_type"))
            inferred.append("script_type")
        if is_missing_value(record.get("total_seats")) and not is_missing_value(
            script.get("total_seats")
        ):
            record["total_seats"] = _to_int(script.get("total_seats"))
            if record["total_seats"] is not None:
                inferred.append("total_seats")
        if is_missing_value(record.get("list_price")) and not is_missing_value(
            script.get("list_price")
        ):
            record["list_price"] = _to_price(script.get("list_price"))
            if record["list_price"] is not None:
                inferred.append("list_price")
    else:
        record["script_id"] = None

    start = _parse_start(record.get("start_time"))
    if start is not None:
        record["start_time"] = start
    else:
        record["start_time"] = pd.NaT
    if source_text:
        record["source_text"] = _redact_source(source_text)

    missing_required: list[str] = []
    if not _text(record.get("script_name")) or match_type == "candidate":
        missing_required.append("script_name")
    if start is None:
        missing_required.append("start_time")
    missing_recommended = [
        field for field in RECOMMENDED_FIELDS if is_missing_value(record.get(field))
    ]

    result = {
        "ok": True,
        "status": "incomplete",
        "fields": _fields_payload(record, inferred=inferred),
        "missing_required": missing_required,
        "missing_recommended": missing_recommended,
        "script_match": match_type,
        "ambiguities": (
            [{"field": "script_name", "candidates": [_candidate_dict(item) for item in candidates]}]
            if match_type == "candidate"
            else []
        ),
        "inferred": inferred,
        "warnings": warnings,
        "script_source": script_source,
        "session": None,
        "auto_assign": None,
        "suggested_question": "",
    }

    if match_type == "candidate":
        # ≥2 候选：与 incomplete 区分开，chat_choices 才能渲染候选按钮。
        result["status"] = "needs_confirmation"
        result["suggested_question"] = _question(
            script_name=script_name,
            start=start,
            candidates=candidates,
            fields=record,
        )
        return result

    if missing_required:
        result["suggested_question"] = _question(
            script_name=script_name,
            start=start,
            candidates=[],
            fields=record,
        )
        return result

    session, _ = _promote(store=store, record=record, now=moment)

    auto = auto_assign(
        store,
        session_ids=[_text(session.get("session_id"))],
        now=moment,
        trigger="session_promoted",
    )
    result.update(
        {
            "status": "promoted",
            "missing_required": [],
            "session": _session_payload(session),
            "auto_assign": auto,
            "suggested_question": "",
        }
    )
    return result


def create_session_from_fields(
    *,
    fields: dict[str, Any] | None,
    source_text: str | None = None,
    default_store: str = "迷雾剧场",
    path: Path | None = None,
    store: Any = None,
    now: Any = None,
    public: pd.DataFrame | None = None,
) -> dict[str, Any]:
    with store_lock(store):
        return _create_session_impl(
            fields=fields,
            source_text=source_text,
            default_store=default_store,
            path=path,
            store=store,
            now=now,
            public=public,
        )


def _session_payload(row: dict[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for key, value in row.items():
        if key in {"booked_seats", "remaining_seats"}:
            payload[key] = json_safe(value)
        elif key in {"start_time", "updated_at"}:
            payload[key] = (
                pd.Timestamp(value).strftime("%Y-%m-%d %H:%M")
                if not is_missing_value(value)
                else None
            )
        else:
            payload[key] = json_safe(value)
    return payload


def _update_session_impl(
    *,
    session_id: str,
    fields: dict[str, Any] | None,
    path: Path | None = None,
    store: Any = None,
    now: Any = None,
) -> dict[str, Any]:
    moment = _now(now)
    payload = dict(fields or {})
    # PATCH body 顶层键：店长在看板二次确认后的「强制改期」——跳过下方的
    # 改期撞档预检（手工分配保留但已知冲突）。不是业务字段，先弹出再过白名单。
    force = bool(payload.pop("__force__", False))
    if "room_id" in payload or "dm_id" in payload:
        raise SessionIntakeError(
            "room_not_allowed",
            "场次修正不接受房间或 DM，请使用排期的换房/换 DM。",
        )
    if "status" in payload and not is_missing_value(payload.get("status")):
        # 状态看板（WP11）拖拽改状态走这里；值必须是三态之一，
        # 避免任意字符串把场次写成不可见状态。显式 null 视为未提供。
        if _text(payload.get("status")) not in VALID_STATUS:
            raise SessionIntakeError(
                "status_invalid",
                "状态只能是待开场、已开场、已取消。",
            )
    for key in list(payload):
        if key not in UPDATE_FIELDS:
            payload.pop(key)

    sessions = load_sessions_with_counts(store=store)
    if sessions.empty:
        raise SessionIntakeError("session_not_found", "场次不存在。")
    hit = sessions["session_id"].astype("string").fillna("") == _text(session_id)
    if not hit.any():
        raise SessionIntakeError("session_not_found", "场次不存在。")
    index = sessions.index[hit][0]
    old = sessions.loc[index].to_dict()
    old_id = _text(old.get("session_id"))

    merged: dict[str, Any] = dict(old)
    if "start_time" in payload and not is_missing_value(payload["start_time"]):
        parsed = _parse_start(payload["start_time"])
        if parsed is None:
            raise SessionIntakeError("start_time_invalid", "开场时间无法解析。")
        merged["start_time"] = parsed
    for key in ("store_name", "script_name", "script_type", "notes"):
        if key in payload and not is_missing_value(payload[key]):
            merged[key] = _text(payload[key])
    if "total_seats" in payload:
        number = _to_int(payload["total_seats"])
        merged["total_seats"] = number
    if "list_price" in payload:
        number = _to_price(payload["list_price"])
        merged["list_price"] = number
    if "status" in payload and not is_missing_value(payload["status"]):
        # 合法性已在入口校验（status_invalid）；这里只负责合并。
        merged["status"] = _text(payload["status"])

    store_name = _text(merged.get("store_name"))
    script_name = _text(merged.get("script_name"))
    start = _parse_start(merged.get("start_time"))
    if not store_name or not script_name or start is None:
        raise SessionIntakeError("incomplete_session", "门店、剧本和开场时间不能为空。")
    merged["store_name"] = store_name
    merged["script_name"] = script_name
    merged["start_time"] = start

    new_id = make_session_id(store_name, script_name, start)
    changed = new_id != old_id
    if changed:
        exists = sessions["session_id"].astype("string").fillna("") == new_id
        if exists.any():
            raise SessionIntakeError("duplicate_session", "目标场次已经存在，不能自动合并。")

    total = _to_int(merged.get("total_seats"))
    booked = int(old.get("booked_seats") or 0) if not is_missing_value(old.get("booked_seats")) else 0
    if total is not None and total < booked:
        raise SessionIntakeError("capacity_exceeded", "新人数小于已约人数。")

    warnings: list[str] = []
    bookings = load_bookings(store=store)
    assignments = store.load_assignments()
    refs = store.load_booking_refs()
    runs = store.load_dm_runs()

    # 改期撞档预检：本场保留下来的 manual 房间/DM 分配放到新时间后，
    # 不得与他场静默双订（含房间缓冲与 DM 休整缓冲）。只在开场时间真的
    # 移动时检查；状态修正、__force__ 强制改期不拦。
    time_moved = start != _parse_start(old.get("start_time"))
    if time_moved and not force:
        context = load_schedule_context(store)
        hits = buffered_overlap_hits(
            context,
            old_id,
            start,
            duration_minutes=_duration_for(context, script_name),
        )
        if hits:
            kind_label = {"room": "房间", "dm": "DM"}
            details = "；".join(
                f"{kind_label.get(_text(hit.get('resource_kind')), _text(hit.get('resource_kind')))}"
                f"与 {hit.get('conflict_session_id')}"
                f"（{hit.get('conflict_start')}）撞档"
                for hit in hits
            )
            raise SessionIntakeError(
                "assignment_conflict",
                f"新时间与现有分配冲突：{details}。"
                "请先换房 / 换 DM，或在排期看板确认后强制改期。",
            )

    if changed:
        active_runs = runs[
            (runs["session_id"].astype("string").fillna("") == old_id)
            & (runs["status"].astype("string").fillna("").isin(["pending", "active", "paused"]))
        ]
        if not active_runs.empty:
            raise SessionIntakeError(
                "run_active",
                "该场还有未结束的 DM 场控，不能修改门店、剧本或开场时间。",
            )

        booking_map: dict[str, str] = {}
        if not bookings.empty:
            for idx, row in bookings.iterrows():
                if _text(row.get("session_id")) != old_id:
                    continue
                old_booking_id = _text(row.get("booking_id"))
                new_booking_id = make_booking_id(
                    new_id, _text(row.get("player_name"))
                )
                booking_map[old_booking_id] = new_booking_id
                bookings.at[idx, "session_id"] = new_id
                bookings.at[idx, "booking_id"] = new_booking_id
                bookings.at[idx, "updated_at"] = moment

        auto_removed = 0
        manual_rows: list[dict[str, Any]] = []
        if not assignments.empty:
            for row in assignments.to_dict("records"):
                if _text(row.get("session_id")) != old_id:
                    manual_rows.append(row)
                    continue
                if _text(row.get("source")) == "manual":
                    updated = dict(row)
                    updated["session_id"] = new_id
                    updated["updated_at"] = moment
                    manual_rows.append(updated)
                else:
                    auto_removed += 1
            assignments = coerce_frame(pd.DataFrame(manual_rows), ASSIGNMENTS_SPEC)

        if not refs.empty:
            for idx, row in refs.iterrows():
                if _text(row.get("session_id")) != old_id:
                    continue
                refs.at[idx, "session_id"] = new_id
                old_booking_id = _text(row.get("booking_id"))
                if old_booking_id in booking_map:
                    refs.at[idx, "booking_id"] = booking_map[old_booking_id]

        if not runs.empty:
            mask = runs["session_id"].astype("string").fillna("") == old_id
            runs.loc[mask, "session_id"] = new_id

        old_frame = sessions[sessions["session_id"].astype("string").fillna("") != old_id].copy()
        new_row = dict(old)
        new_row.update(merged)
        new_row["session_id"] = new_id
        new_row["updated_at"] = moment
        new_frame = coerce_sessions(
            pd.concat([old_frame, pd.DataFrame([new_row])], ignore_index=True)
        )
        save_sessions(new_frame, path, store=store)
        save_bookings(bookings, store=store)
        store.save_assignments(assignments)
        store.save_booking_refs(refs)
        if not runs.empty:
            store.save_dm_runs(runs)
        if auto_removed:
            warnings.append("原自动分配已清除，正在按新时间重新排期。")
    else:
        new_row = dict(old)
        new_row.update(merged)
        new_row["updated_at"] = moment
        # trust_status：店长显式 PATCH 状态是权威动作，必须落盘。
        # 文案抽取等其他 upsert 路径保持默认 False（防"已取消被复活"）。
        _upsert_sessions_impl([new_row], path, store=store, trust_status=True)

    refreshed = load_sessions_with_counts(store=store)
    session_row = refreshed[
        refreshed["session_id"].astype("string").fillna("") == new_id
    ]
    auto = auto_assign(
        store,
        session_ids=[new_id],
        now=moment,
        trigger="session_updated",
    )
    return {
        "previous_session_id": old_id,
        "session_id_changed": changed,
        "session": _session_payload(session_row.iloc[0].to_dict()),
        "auto_assign": auto,
        "warnings": warnings,
    }


def update_session(
    session_id: str,
    *,
    fields: dict[str, Any] | None,
    path: Path | None = None,
    store: Any = None,
    now: Any = None,
) -> dict[str, Any]:
    with store_lock(store):
        return _update_session_impl(
            session_id=session_id,
            fields=fields,
            path=path,
            store=store,
            now=now,
        )


__all__ = [
    "ALLOWED_FIELDS",
    "SessionIntakeError",
    "UPDATE_FIELDS",
    "create_session_from_fields",
    "update_session",
]

"""门店资源：房间、DM、班次与场次分配。

资源是排期的输入，不是场次的必填字段。店长可以先只记场次，系统再按
容量、技能、班次和占用情况自动分配；资源不足时只给建议，不擅自改场次。
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

import pandas as pd

from app.config import (
    ASSIGNMENTS_XLSX,
    DM_LEAVES_XLSX,
    DM_SHIFTS_XLSX,
    DMS_XLSX,
    ROOMS_XLSX,
)
from app.tools.frame import (
    FrameSpec,
    coerce_frame,
    empty_frame,
    json_safe,
    load_frame,
    save_frame,
)
from app.tools.storage import is_missing_value, store_lock

ROOMS_COLUMNS = [
    "room_id",
    "name",
    "capacity",
    "tags",
    "hourly_cost",
    "buffer_minutes",
    "active",
    "notes",
    "source_text",
    "created_at",
    "updated_at",
]
DMS_COLUMNS = [
    "dm_id",
    "name",
    "skills",
    "hourly_cost",
    "max_sessions_per_day",
    "active",
    "notes",
    "source_text",
    "created_at",
    "updated_at",
]
SHIFTS_COLUMNS = [
    "shift_id",
    "dm_id",
    "weekday",
    "start_minute",
    "end_minute",
    "active",
    "updated_at",
]
DM_LEAVES_COLUMNS = [
    "leave_id",
    "dm_id",
    "day",
    "note",
    "created_at",
    "updated_at",
]
ASSIGNMENTS_COLUMNS = [
    "assignment_id",
    "session_id",
    "resource_kind",
    "resource_id",
    "role",
    "source",
    "assigned_at",
    "updated_at",
]

ROOMS_SPEC = FrameSpec(
    name="房间表",
    columns=ROOMS_COLUMNS,
    numerics=("hourly_cost",),
    integers=("capacity", "buffer_minutes"),
    datetimes=("created_at", "updated_at"),
    booleans=("active",),
    strings=("room_id", "name", "tags", "notes", "source_text"),
    defaults={"buffer_minutes": 15, "active": True},
)
DMS_SPEC = FrameSpec(
    name="DM 表",
    columns=DMS_COLUMNS,
    numerics=("hourly_cost",),
    integers=("max_sessions_per_day",),
    datetimes=("created_at", "updated_at"),
    booleans=("active",),
    strings=("dm_id", "name", "skills", "notes", "source_text"),
    defaults={"active": True},
)
SHIFTS_SPEC = FrameSpec(
    name="DM 班次表",
    columns=SHIFTS_COLUMNS,
    integers=("weekday", "start_minute", "end_minute"),
    datetimes=("updated_at",),
    booleans=("active",),
    strings=("shift_id", "dm_id"),
    defaults={"active": True},
)
DM_LEAVES_SPEC = FrameSpec(
    name="DM 请假表",
    columns=DM_LEAVES_COLUMNS,
    datetimes=("created_at", "updated_at"),
    strings=("leave_id", "dm_id", "day", "note"),
)
ASSIGNMENTS_SPEC = FrameSpec(
    name="场次分配表",
    columns=ASSIGNMENTS_COLUMNS,
    datetimes=("assigned_at", "updated_at"),
    strings=(
        "assignment_id",
        "session_id",
        "resource_kind",
        "resource_id",
        "role",
        "source",
    ),
)

_WEEKDAYS = {
    "周一": 0,
    "周二": 1,
    "周三": 2,
    "周四": 3,
    "周五": 4,
    "周六": 5,
    "周日": 6,
    "周天": 6,
    "一": 0,
    "二": 1,
    "三": 2,
    "四": 3,
    "五": 4,
    "六": 5,
    "日": 6,
    "天": 6,
}
_TIME_RE = re.compile(r"^\s*(\d{1,2})(?:\s*[:：]\s*(\d{1,2}))?\s*(?:点|时)?\s*$")
_NAME_KEY_RE = re.compile(r"\s+")


def _now() -> pd.Timestamp:
    return pd.Timestamp.now().floor("s")


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def _text(value: Any) -> str:
    if is_missing_value(value):
        return ""
    return " ".join(str(value).strip().split())


def _name_key(value: Any) -> str:
    return _NAME_KEY_RE.sub("", _text(value)).casefold()


def _int_or_none(value: Any) -> int | None:
    if is_missing_value(value):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _float_or_none(value: Any) -> float | None:
    if is_missing_value(value):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_weekday(value: Any) -> int:
    if isinstance(value, bool):
        raise ValueError("weekday 不合法")
    if isinstance(value, (int, float)) and not is_missing_value(value):
        number = int(value)
        if 0 <= number <= 6:
            return number
        if 1 <= number <= 7:
            return number - 1
        raise ValueError(f"weekday 需在 0..6：{value!r}")
    text = _text(value)
    if text in _WEEKDAYS:
        return _WEEKDAYS[text]
    if text.isdigit():
        return parse_weekday(int(text))
    raise ValueError(f"weekday 需为 0..6 或周一..周日：{value!r}")


def parse_minute(value: Any) -> int:
    """把 18:00 / 18点 / 1080 归一成当天分钟数。"""
    if isinstance(value, bool):
        raise ValueError("时间不合法")
    if isinstance(value, (int, float)) and not is_missing_value(value):
        return max(0, min(1440, int(value)))
    text = _text(value)
    match = _TIME_RE.fullmatch(text)
    if not match:
        raise ValueError(f"时间需为 HH:MM 或 HH点：{value!r}")
    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    if hour == 24 and minute == 0:
        return 1440
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"时间超出范围：{value!r}")
    return hour * 60 + minute


def rooms_to_dict(record: Any) -> dict[str, Any]:
    getter = record.get if hasattr(record, "get") else (lambda _: None)
    return {
        "room_id": _text(getter("room_id")),
        "name": _text(getter("name")),
        "capacity": _int_or_none(getter("capacity")),
        "tags": _text(getter("tags")),
        "hourly_cost": _float_or_none(getter("hourly_cost")),
        "buffer_minutes": _int_or_none(getter("buffer_minutes")) or 15,
        "active": bool(getter("active")) if not is_missing_value(getter("active")) else True,
        "notes": _text(getter("notes")),
        "updated_at": json_safe(getter("updated_at")),
    }


def dms_to_dict(record: Any) -> dict[str, Any]:
    getter = record.get if hasattr(record, "get") else (lambda _: None)
    return {
        "dm_id": _text(getter("dm_id")),
        "name": _text(getter("name")),
        "skills": _text(getter("skills")),
        "hourly_cost": _float_or_none(getter("hourly_cost")),
        "max_sessions_per_day": _int_or_none(getter("max_sessions_per_day")),
        "active": bool(getter("active")) if not is_missing_value(getter("active")) else True,
        "notes": _text(getter("notes")),
        "updated_at": json_safe(getter("updated_at")),
    }


def shifts_to_dict(record: Any) -> dict[str, Any]:
    getter = record.get if hasattr(record, "get") else (lambda _: None)
    return {
        "shift_id": _text(getter("shift_id")),
        "dm_id": _text(getter("dm_id")),
        "weekday": _int_or_none(getter("weekday")),
        "start_minute": _int_or_none(getter("start_minute")),
        "end_minute": _int_or_none(getter("end_minute")),
        "active": bool(getter("active")) if not is_missing_value(getter("active")) else True,
    }


def assignments_to_dict(record: Any) -> dict[str, Any]:
    getter = record.get if hasattr(record, "get") else (lambda _: None)
    return {
        "assignment_id": _text(getter("assignment_id")),
        "session_id": _text(getter("session_id")),
        "resource_kind": _text(getter("resource_kind")),
        "resource_id": _text(getter("resource_id")),
        "role": _text(getter("role")),
        "source": _text(getter("source")),
        "assigned_at": json_safe(getter("assigned_at")),
        "updated_at": json_safe(getter("updated_at")),
    }


# ---------------------------------------------------------------------------
# 读取与写入
# ---------------------------------------------------------------------------


def load_rooms(path: Path | None = None, *, store: Any = None) -> pd.DataFrame:
    if store is not None:
        return store.load_rooms()
    return load_frame(path, ROOMS_XLSX, ROOMS_SPEC)


def save_rooms(df: pd.DataFrame, path: Path | None = None, *, store: Any = None) -> None:
    if store is not None:
        store.save_rooms(df)
        return
    save_frame(df, path, ROOMS_XLSX, ROOMS_SPEC)


def load_dms(path: Path | None = None, *, store: Any = None) -> pd.DataFrame:
    if store is not None:
        return store.load_dms()
    return load_frame(path, DMS_XLSX, DMS_SPEC)


def save_dms(df: pd.DataFrame, path: Path | None = None, *, store: Any = None) -> None:
    if store is not None:
        store.save_dms(df)
        return
    save_frame(df, path, DMS_XLSX, DMS_SPEC)


def load_dm_shifts(path: Path | None = None, *, store: Any = None) -> pd.DataFrame:
    if store is not None:
        return store.load_dm_shifts()
    return load_frame(path, DM_SHIFTS_XLSX, SHIFTS_SPEC)


def save_dm_shifts(df: pd.DataFrame, path: Path | None = None, *, store: Any = None) -> None:
    if store is not None:
        store.save_dm_shifts(df)
        return
    save_frame(df, path, DM_SHIFTS_XLSX, SHIFTS_SPEC)


def load_dm_leaves(path: Path | None = None, *, store: Any = None) -> pd.DataFrame:
    if store is not None:
        return store.load_dm_leaves()
    return load_frame(path, DM_LEAVES_XLSX, DM_LEAVES_SPEC)


def save_dm_leaves(df: pd.DataFrame, path: Path | None = None, *, store: Any = None) -> None:
    if store is not None:
        store.save_dm_leaves(df)
        return
    save_frame(df, path, DM_LEAVES_XLSX, DM_LEAVES_SPEC)


def load_assignments(path: Path | None = None, *, store: Any = None) -> pd.DataFrame:
    if store is not None:
        return store.load_assignments()
    return load_frame(path, ASSIGNMENTS_XLSX, ASSIGNMENTS_SPEC)


def save_assignments(df: pd.DataFrame, path: Path | None = None, *, store: Any = None) -> None:
    if store is not None:
        store.save_assignments(df)
        return
    save_frame(df, path, ASSIGNMENTS_XLSX, ASSIGNMENTS_SPEC)


def coerce_rooms(df: pd.DataFrame) -> pd.DataFrame:
    return coerce_frame(df, ROOMS_SPEC)


def coerce_dms(df: pd.DataFrame) -> pd.DataFrame:
    return coerce_frame(df, DMS_SPEC)


def coerce_dm_shifts(df: pd.DataFrame) -> pd.DataFrame:
    return coerce_frame(df, SHIFTS_SPEC)


def coerce_dm_leaves(df: pd.DataFrame) -> pd.DataFrame:
    return coerce_frame(df, DM_LEAVES_SPEC)


def coerce_assignments(df: pd.DataFrame) -> pd.DataFrame:
    return coerce_frame(df, ASSIGNMENTS_SPEC)


def empty_rooms() -> pd.DataFrame:
    return empty_frame(ROOMS_SPEC)


def empty_dms() -> pd.DataFrame:
    return empty_frame(DMS_SPEC)


def empty_dm_shifts() -> pd.DataFrame:
    return empty_frame(SHIFTS_SPEC)


def empty_dm_leaves() -> pd.DataFrame:
    return empty_frame(DM_LEAVES_SPEC)


def empty_assignments() -> pd.DataFrame:
    return empty_frame(ASSIGNMENTS_SPEC)


# ---------------------------------------------------------------------------
# 增量更新
# ---------------------------------------------------------------------------


def _merge_non_empty(row: dict[str, Any], incoming: dict[str, Any], fields: Iterable[str]) -> None:
    for field in fields:
        value = incoming.get(field)
        if not is_missing_value(value):
            row[field] = value


def _upsert_rooms_impl(
    rows: Iterable[dict[str, Any]],
    *,
    path: Path | None = None,
    store: Any = None,
    now: Any = None,
) -> tuple[pd.DataFrame, list[str]]:
    frame = load_rooms(path, store=store)
    existing = {_text(row.get("room_id")): index for index, row in frame.iterrows()}
    by_name = {
        _name_key(row.get("name")): index
        for index, row in frame.iterrows()
        if _name_key(row.get("name"))
    }
    ts = pd.Timestamp(now) if now is not None else _now()
    warnings: list[str] = []
    records = list(frame.to_dict("records"))

    for item in rows:
        if not isinstance(item, dict):
            warnings.append("跳过一条不是对象的房间记录。")
            continue
        name = _text(item.get("name"))
        if not name:
            warnings.append("跳过一条缺少房间名的记录。")
            continue
        room_id = _text(item.get("room_id"))
        index = existing.get(room_id) if room_id else None
        if index is None:
            index = by_name.get(_name_key(name))
        if index is None:
            room_id = room_id or _new_id("room")
            records.append(
                {
                    "room_id": room_id,
                    "name": name,
                    "capacity": item.get("capacity"),
                    "tags": item.get("tags"),
                    "hourly_cost": item.get("hourly_cost"),
                    "buffer_minutes": item.get("buffer_minutes", 15),
                    "active": item.get("active", True),
                    "notes": item.get("notes"),
                    "source_text": item.get("source_text"),
                    "created_at": ts,
                    "updated_at": ts,
                }
            )
            existing[room_id] = len(records) - 1
            by_name[_name_key(name)] = len(records) - 1
            continue

        row = records[index]
        _merge_non_empty(
            row,
            item,
            ("capacity", "tags", "hourly_cost", "buffer_minutes", "active", "notes", "source_text"),
        )
        if name:
            row["name"] = name
        row["updated_at"] = ts

    frame = coerce_frame(pd.DataFrame(records), ROOMS_SPEC)
    save_rooms(frame, path, store=store)
    return frame, warnings


def upsert_rooms(
    rows: Iterable[dict[str, Any]],
    *,
    path: Path | None = None,
    store: Any = None,
    now: Any = None,
) -> tuple[pd.DataFrame, list[str]]:
    with store_lock(store):
        return _upsert_rooms_impl(rows, path=path, store=store, now=now)


def _upsert_dms_impl(
    rows: Iterable[dict[str, Any]],
    *,
    path: Path | None = None,
    store: Any = None,
    now: Any = None,
) -> tuple[pd.DataFrame, list[str]]:
    frame = load_dms(path, store=store)
    existing = {_text(row.get("dm_id")): index for index, row in frame.iterrows()}
    by_name = {
        _name_key(row.get("name")): index
        for index, row in frame.iterrows()
        if _name_key(row.get("name"))
    }
    ts = pd.Timestamp(now) if now is not None else _now()
    warnings: list[str] = []
    records = list(frame.to_dict("records"))

    for item in rows:
        if not isinstance(item, dict):
            warnings.append("跳过一条不是对象的 DM 记录。")
            continue
        name = _text(item.get("name"))
        if not name:
            warnings.append("跳过一条缺少 DM 名的记录。")
            continue
        dm_id = _text(item.get("dm_id"))
        index = existing.get(dm_id) if dm_id else None
        if index is None:
            index = by_name.get(_name_key(name))
        if index is None:
            dm_id = dm_id or _new_id("dm")
            records.append(
                {
                    "dm_id": dm_id,
                    "name": name,
                    "skills": item.get("skills"),
                    "hourly_cost": item.get("hourly_cost"),
                    "max_sessions_per_day": item.get("max_sessions_per_day"),
                    "active": item.get("active", True),
                    "notes": item.get("notes"),
                    "source_text": item.get("source_text"),
                    "created_at": ts,
                    "updated_at": ts,
                }
            )
            existing[dm_id] = len(records) - 1
            by_name[_name_key(name)] = len(records) - 1
            continue

        row = records[index]
        _merge_non_empty(
            row,
            item,
            ("skills", "hourly_cost", "max_sessions_per_day", "active", "notes", "source_text"),
        )
        if name:
            row["name"] = name
        row["updated_at"] = ts

    frame = coerce_frame(pd.DataFrame(records), DMS_SPEC)
    save_dms(frame, path, store=store)
    return frame, warnings


def upsert_dms(
    rows: Iterable[dict[str, Any]],
    *,
    path: Path | None = None,
    store: Any = None,
    now: Any = None,
) -> tuple[pd.DataFrame, list[str]]:
    with store_lock(store):
        return _upsert_dms_impl(rows, path=path, store=store, now=now)


def replace_dm_shifts(
    dm_id: str,
    shifts: Iterable[dict[str, Any]],
    *,
    path: Path | None = None,
    store: Any = None,
    now: Any = None,
) -> pd.DataFrame:
    """替换一个 DM 的班次；其他 DM 的班次保留。"""
    target = _text(dm_id)
    if not target:
        raise ValueError("dm_id 不能为空")
    with store_lock(store):
        frame = load_dm_shifts(path, store=store)
        kept = frame[frame["dm_id"].astype("string").fillna("") != target]
        ts = pd.Timestamp(now) if now is not None else _now()
        records: list[dict[str, Any]] = []
        for item in shifts:
            if not isinstance(item, dict):
                raise ValueError("班次必须是对象数组")
            weekday = parse_weekday(item.get("weekday"))
            start = parse_minute(item.get("start_minute", item.get("start")))
            end = parse_minute(item.get("end_minute", item.get("end")))
            if end <= start:
                raise ValueError("班次结束时间必须晚于开始时间")
            records.append(
                {
                    "shift_id": _text(item.get("shift_id")) or _new_id("shift"),
                    "dm_id": target,
                    "weekday": weekday,
                    "start_minute": start,
                    "end_minute": end,
                    "active": item.get("active", True),
                    "updated_at": ts,
                }
            )
        merged = coerce_frame(
            pd.concat([kept, pd.DataFrame(records)], ignore_index=True)
            if records
            else kept,
            SHIFTS_SPEC,
        )
        save_dm_shifts(merged, path, store=store)
        return merged


def upsert_dm_leave(
    dm_id: str,
    day: Any,
    *,
    note: str = "",
    path: Path | None = None,
    store: Any = None,
    now: Any = None,
) -> dict[str, Any]:
    """登记一条 DM 请假；同一 (dm_id, day) 幂等（更新备注，不重复建行）。"""
    target = _text(dm_id)
    if not target:
        raise ValueError("dm_id 不能为空")
    moment = pd.Timestamp(now) if now is not None else _now()
    day_text = pd.Timestamp(day).strftime("%Y-%m-%d")
    with store_lock(store):
        frame = load_dm_leaves(path, store=store)
        existing = frame[
            (frame["dm_id"].astype("string").fillna("") == target)
            & (frame["day"].astype("string").fillna("") == day_text)
        ]
        if not existing.empty:
            row = existing.iloc[0]
            keep = frame[frame["leave_id"].astype("string").fillna("")
                         != _text(row["leave_id"])]
            updated = {
                "leave_id": _text(row["leave_id"]),
                "dm_id": target,
                "day": day_text,
                "note": _text(note) or _text(row.get("note")),
                "created_at": row.get("created_at") or moment,
                "updated_at": moment,
            }
            merged = coerce_frame(
                pd.concat([keep, pd.DataFrame([updated])], ignore_index=True),
                DM_LEAVES_SPEC,
            )
            save_dm_leaves(merged, path, store=store)
            return {"leave_id": updated["leave_id"], "created": False}
        record = {
            "leave_id": _new_id("leave"),
            "dm_id": target,
            "day": day_text,
            "note": _text(note),
            "created_at": moment,
            "updated_at": moment,
        }
        merged = coerce_frame(
            pd.concat([frame, pd.DataFrame([record])], ignore_index=True),
            DM_LEAVES_SPEC,
        )
        save_dm_leaves(merged, path, store=store)
        return {"leave_id": record["leave_id"], "created": True}


def delete_dm_leave(
    leave_id: str,
    *,
    path: Path | None = None,
    store: Any = None,
) -> dict[str, Any]:
    """删除一条请假；不存在时返回 removed=False（幂等）。"""
    target = _text(leave_id)
    if not target:
        raise ValueError("leave_id 不能为空")
    with store_lock(store):
        frame = load_dm_leaves(path, store=store)
        kept = frame[frame["leave_id"].astype("string").fillna("") != target]
        if len(kept) == len(frame):
            return {"leave_id": target, "removed": False}
        save_dm_leaves(coerce_dm_leaves(kept), path, store=store)
        return {"leave_id": target, "removed": True}


def dm_script_history(
    *,
    path: Path | None = None,
    store: Any = None,
) -> dict[str, dict[str, Any]]:
    """从 dm_runs ⋈ sessions 派生 DM 带本历史（已取消场次不计）。

    返回 {dm_id: {past_runs, distinct_scripts, recent[≤8]}}。
    script_counts 是 name_key → 带本次数，仅供排期熟悉度排序，不进 API 载荷。
    """
    from app.tools.scripts import normalize_script_name
    from app.tools.storage import STATUS_CANCELLED

    runs = store.load_dm_runs() if store is not None else pd.DataFrame()
    sessions = store.load_sessions() if store is not None else pd.DataFrame()
    if runs is None or runs.empty or sessions is None or sessions.empty:
        return {}
    session_by_id = {
        _text(row.get("session_id")): row
        for row in sessions.to_dict("records")
    }
    history: dict[str, dict[str, Any]] = {}

    def _bucket(dm_id: str) -> dict[str, Any]:
        return history.setdefault(
            dm_id,
            {
                "past_runs": 0,
                "distinct_scripts": 0,
                "recent": [],
                "script_counts": {},
            },
        )

    records = sorted(
        runs.to_dict("records"),
        key=lambda row: _text(row.get("started_at") or row.get("created_at") or ""),
        reverse=True,
    )
    for run in records:
        dm_id = _text(run.get("dm_id"))
        if not dm_id:
            continue
        session = session_by_id.get(_text(run.get("session_id")))
        if session is None:
            continue
        if _text(session.get("status")) == STATUS_CANCELLED:
            continue
        name = _text(session.get("script_name"))
        if not name:
            continue
        bucket = _bucket(dm_id)
        key = normalize_script_name(name)
        bucket["script_counts"][key] = bucket["script_counts"].get(key, 0) + 1
        if len(bucket["recent"]) < 8:
            bucket["recent"].append(
                {
                    "script_name": name,
                    "start_time": json_safe(session.get("start_time")),
                }
            )
    for bucket in history.values():
        bucket["past_runs"] = sum(bucket["script_counts"].values())
        bucket["distinct_scripts"] = len(bucket["script_counts"])
    return history


def schedule_dm_inputs(
    *,
    path: Path | None = None,
    store: Any = None,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, int]]]:
    """排期三处调用方共用：请假行 + 熟悉度表（dm_id → name_key → 次数）。"""
    leaves = load_dm_leaves(path, store=store)
    leave_rows: list[dict[str, Any]] = []
    if leaves is not None and not leaves.empty:
        leave_rows = leaves.to_dict("records")
    history = dm_script_history(path=path, store=store)
    familiarities = {
        dm_id: dict(bucket.get("script_counts") or {})
        for dm_id, bucket in history.items()
    }
    return leave_rows, familiarities


def replace_assignments(
    session_ids: Iterable[str],
    assignments: Iterable[dict[str, Any]],
    *,
    path: Path | None = None,
    store: Any = None,
    now: Any = None,
) -> pd.DataFrame:
    """只替换指定场次的分配，其他场次原样保留。"""
    targets = {_text(item) for item in session_ids if _text(item)}
    with store_lock(store):
        frame = load_assignments(path, store=store)
        kept = frame[~frame["session_id"].astype("string").fillna("").isin(targets)]
        ts = pd.Timestamp(now) if now is not None else _now()
        records: list[dict[str, Any]] = []
        seen: set[tuple[str, str, str]] = set()
        for item in assignments:
            if not isinstance(item, dict):
                raise ValueError("分配必须是对象数组")
            session_id = _text(item.get("session_id"))
            kind = _text(item.get("resource_kind"))
            resource_id = _text(item.get("resource_id"))
            if not session_id or kind not in {"room", "dm"} or not resource_id:
                raise ValueError("分配需要 session_id、resource_kind(room/dm) 与 resource_id")
            key = (session_id, kind, resource_id)
            if key in seen:
                continue
            seen.add(key)
            records.append(
                {
                    "assignment_id": _text(item.get("assignment_id")) or _new_id("assign"),
                    "session_id": session_id,
                    "resource_kind": kind,
                    "resource_id": resource_id,
                    "role": _text(item.get("role")) or "main",
                    "source": _text(item.get("source")) or "auto",
                    "assigned_at": item.get("assigned_at") or ts,
                    "updated_at": ts,
                }
            )
        merged = coerce_frame(
            pd.concat([kept, pd.DataFrame(records)], ignore_index=True)
            if records
            else kept,
            ASSIGNMENTS_SPEC,
        )
        save_assignments(merged, path, store=store)
        return merged


def prune_orphan_assignments(
    sessions: pd.DataFrame,
    assignments: pd.DataFrame,
) -> pd.DataFrame:
    known = set(sessions["session_id"].astype("string").fillna("")) if not sessions.empty else set()
    if assignments.empty:
        return coerce_frame(assignments, ASSIGNMENTS_SPEC)
    keep = assignments["session_id"].astype("string").fillna("").isin(known)
    return coerce_frame(assignments[keep], ASSIGNMENTS_SPEC)


# ---------------------------------------------------------------------------
# 缺口清单
# ---------------------------------------------------------------------------


def setup_checklist(
    *,
    rooms: pd.DataFrame,
    dms: pd.DataFrame,
    shifts: pd.DataFrame,
    scripts: pd.DataFrame,
    stages: pd.DataFrame | None = None,
    sessions: pd.DataFrame | None = None,
) -> dict[str, Any]:
    active_rooms = rooms[rooms["active"].fillna(False)] if not rooms.empty else rooms
    active_dms = dms[dms["active"].fillna(False)] if not dms.empty else dms
    room_missing_capacity = (
        int(active_rooms["capacity"].isna().sum()) if not active_rooms.empty else 0
    )
    dm_missing_skills = (
        int(active_dms["skills"].astype("string").fillna("").str.strip().eq("").sum())
        if not active_dms.empty
        else 0
    )
    dm_missing_shifts = 0
    if not active_dms.empty:
        with_shifts = set(shifts["dm_id"].astype("string").fillna("")) if not shifts.empty else set()
        dm_missing_shifts = int(
            sum(1 for value in active_dms["dm_id"] if str(value) not in with_shifts)
        )
    script_missing_duration = 0
    if scripts is not None and not scripts.empty:
        script_missing_duration = int(scripts["duration_minutes"].isna().sum())
    scripts_without_stages = 0
    if scripts is not None and not scripts.empty:
        stage_scripts = (
            set(stages["script_id"].astype("string").fillna(""))
            if stages is not None and not stages.empty
            else set()
        )
        scripts_without_stages = int(
            sum(
                1
                for value in scripts["script_id"]
                if str(value) not in stage_scripts
            )
        )

    items: list[dict[str, Any]] = []
    if active_rooms.empty:
        items.append({"code": "no_rooms", "message": "还没有可用房间，请先建档或载入演示资源。"})
    if active_dms.empty:
        items.append({"code": "no_dms", "message": "还没有可用 DM，请先建档或载入演示资源。"})
    if room_missing_capacity:
        items.append(
            {
                "code": "room_capacity_missing",
                "message": f"{room_missing_capacity} 间房缺容量，排期时不会自动使用。",
            }
        )
    if dm_missing_skills:
        items.append(
            {
                "code": "dm_skills_missing",
                "message": f"{dm_missing_skills} 位 DM 缺技能标签，排期时按不限处理。",
            }
        )
    if dm_missing_shifts:
        items.append(
            {
                "code": "dm_shifts_missing",
                "message": f"{dm_missing_shifts} 位 DM 没有班次，排期时按全天可用处理。",
            }
        )
    if script_missing_duration:
        items.append(
            {
                "code": "script_duration_missing",
                "message": f"{script_missing_duration} 个剧本缺时长，排期按默认 240 分钟估算。",
            }
        )
    if scripts_without_stages:
        items.append(
            {
                "code": "stages_missing",
                "message": f"{scripts_without_stages} 个剧本还没有阶段模板，DM 场控无法启动。",
            }
        )

    return {
        "rooms": int(len(rooms)),
        "active_rooms": int(len(active_rooms)),
        "dms": int(len(dms)),
        "active_dms": int(len(active_dms)),
        "shifts": int(len(shifts)),
        "scripts": int(len(scripts)) if scripts is not None else 0,
        "sessions": int(len(sessions)) if sessions is not None else 0,
        "room_missing_capacity": room_missing_capacity,
        "dm_missing_skills": dm_missing_skills,
        "dm_missing_shifts": dm_missing_shifts,
        "script_missing_duration": script_missing_duration,
        "scripts_without_stages": scripts_without_stages,
        "items": items,
    }


__all__ = [
    "ASSIGNMENTS_COLUMNS",
    "DMS_COLUMNS",
    "DM_LEAVES_COLUMNS",
    "DM_LEAVES_SPEC",
    "ROOMS_COLUMNS",
    "SHIFTS_COLUMNS",
    "assignments_to_dict",
    "coerce_assignments",
    "coerce_dm_leaves",
    "coerce_dm_shifts",
    "coerce_dms",
    "coerce_rooms",
    "delete_dm_leave",
    "dms_to_dict",
    "dm_script_history",
    "empty_assignments",
    "empty_dm_leaves",
    "empty_dm_shifts",
    "empty_dms",
    "empty_rooms",
    "load_assignments",
    "load_dm_leaves",
    "load_dm_shifts",
    "load_dms",
    "load_rooms",
    "parse_minute",
    "parse_weekday",
    "prune_orphan_assignments",
    "replace_assignments",
    "replace_dm_shifts",
    "rooms_to_dict",
    "save_assignments",
    "save_dm_leaves",
    "save_dm_shifts",
    "save_dms",
    "save_rooms",
    "schedule_dm_inputs",
    "setup_checklist",
    "shifts_to_dict",
    "upsert_dm_leave",
    "upsert_dms",
    "upsert_rooms",
]

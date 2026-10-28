"""把排期结果安全地写回 assignment 表。

排期引擎是纯函数；这里负责：
  - 只给目标场次自动分配，不覆盖已有 manual assignment；
  - 冲突时场次照常保留，只返回可选项；
  - 重复触发保持幂等。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable

import pandas as pd

from app.tools.frame import json_safe
from app.tools.resources import (
    assignments_to_dict,
    load_assignments,
    load_dm_shifts,
    load_dms,
    load_rooms,
    replace_assignments,
    schedule_dm_inputs,
)
from app.tools.scheduling import ScheduleParams, build_schedule
from app.tools.scripts import load_scripts
from app.tools.storage import (
    STATUS_PENDING,
    is_missing_value,
    load_sessions_with_counts,
    store_lock,
)

RESOURCE_KINDS = {"room", "dm"}


@dataclass(frozen=True, slots=True)
class ScheduleContext:
    sessions: pd.DataFrame
    rooms: pd.DataFrame
    dms: pd.DataFrame
    shifts: pd.DataFrame
    assignments: pd.DataFrame
    scripts: pd.DataFrame


def load_schedule_context(store: Any) -> ScheduleContext:
    return ScheduleContext(
        sessions=load_sessions_with_counts(store=store),
        rooms=load_rooms(store=store),
        dms=load_dms(store=store),
        shifts=load_dm_shifts(store=store),
        assignments=load_assignments(store=store),
        scripts=load_scripts(store=store),
    )


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
    return str(value).strip()


def _start_text(value: Any) -> str:
    """场次开始时间 → 「YYYY-MM-DD HH:MM」（与 shortage.start 同格式）。"""
    if is_missing_value(value):
        return ""
    moment = pd.Timestamp(value)
    if pd.isna(moment):
        return ""
    return moment.strftime("%Y-%m-%d %H:%M")


def _future_pending(sessions: pd.DataFrame, now: pd.Timestamp) -> pd.DataFrame:
    if sessions.empty:
        return sessions
    frame = sessions.copy()
    frame["start_time"] = pd.to_datetime(frame["start_time"], errors="coerce")
    return frame[
        (frame["status"].astype("string").fillna("") == STATUS_PENDING)
        & (frame["start_time"] >= now)
    ]


def _assignment_frame_for(
    session_id: str,
    room_id: str | None,
    dm_id: str | None,
    *,
    source: str,
    now: pd.Timestamp,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for kind, resource_id in (("room", room_id), ("dm", dm_id)):
        if not resource_id:
            continue
        rows.append(
            {
                "assignment_id": f"assign-{uuid.uuid4().hex[:12]}",
                "session_id": session_id,
                "resource_kind": kind,
                "resource_id": resource_id,
                "role": "main",
                "source": source,
                "assigned_at": now,
                "updated_at": now,
            }
        )
    return rows


def _conflict_dict(shortage) -> dict[str, Any]:
    item = shortage.to_dict()
    return {
        "session_id": item["session_id"],
        "script_name": item["script_name"],
        "start": item["start"],
        "reason": item["reason"],
        "message": item["message"],
        "options": item["options"],
    }


def _auto_assign_impl(
    store: Any,
    *,
    session_ids: Iterable[str] | None = None,
    now: Any = None,
    trigger: str = "manual",
    dry_run: bool = False,
) -> dict[str, Any]:
    moment = _now(now)
    context = load_schedule_context(store)
    sessions = _future_pending(context.sessions, moment)

    if sessions.empty:
        return {
            "status": "unchanged",
            "assigned": [],
            "conflicts": [],
            "warnings": ["没有待开场的未来场次。"],
            "trigger": trigger,
            "as_of": moment.strftime("%Y-%m-%d %H:%M"),
            "preview": dry_run,
            "partial": [],
            "unchanged": [],
        }

    known = set(sessions["session_id"].astype("string").fillna(""))
    if session_ids is not None:
        requested = {_text(item) for item in session_ids if _text(item)}
        missing = sorted(requested - known)
        targets = sessions[
            sessions["session_id"].astype("string").fillna("").isin(requested)
        ]
    else:
        requested = set()
        missing = []
        targets = sessions

    assigned_ids = set(
        context.assignments["session_id"].astype("string").fillna("")
    )
    unchanged = targets[
        targets["session_id"].astype("string").fillna("").isin(assigned_ids)
    ]
    pending = targets[
        ~targets["session_id"].astype("string").fillna("").isin(assigned_ids)
    ]
    pending_ids = pending["session_id"].astype("string").fillna("").tolist()

    # partial：目标场次里恰好只分配了房间或只分配了 DM 的场次（语义上仍算
    # unchanged——有分配行就不自动动它），在预览里单列出来提示用优化方案处理。
    partial: list[dict[str, Any]] = []
    if not targets.empty and not context.assignments.empty:
        target_ids = set(targets["session_id"].astype("string").fillna(""))
        kinds_by_session: dict[str, set[str]] = {}
        for row in context.assignments.to_dict("records"):
            sid = _text(row.get("session_id"))
            if sid not in target_ids:
                continue
            kind = _text(row.get("resource_kind"))
            if kind in RESOURCE_KINDS:
                kinds_by_session.setdefault(sid, set()).add(kind)
        meta = {
            _text(row.get("session_id")): row for row in targets.to_dict("records")
        }
        for sid in sorted(kinds_by_session):
            kinds = kinds_by_session[sid]
            if len(kinds) != 1:
                continue
            row = meta.get(sid) or {}
            partial.append(
                {
                    "session_id": sid,
                    "script_name": _text(row.get("script_name")),
                    "start": _start_text(row.get("start_time")),
                    "missing": "dm" if "room" in kinds else "room",
                }
            )

    conflicts: list[dict[str, Any]] = [
        {
            "session_id": item,
            "reason": "session_missing",
            "message": "场次不存在或已不在待开场状态。",
            "options": [],
        }
        for item in missing
    ]

    if not pending_ids:
        warnings = []
        if not context.rooms.empty and context.dms.empty:
            warnings.append("还没有 DM 档案；排期只能保留场次，不能自动分配。")
        return {
            "status": "unchanged",
            "assigned": [],
            "conflicts": conflicts,
            "warnings": warnings,
            "partial": partial,
            "unchanged": unchanged["session_id"].astype("string").fillna("").tolist(),
            "trigger": trigger,
            "as_of": moment.strftime("%Y-%m-%d %H:%M"),
            "preview": dry_run,
        }

    if context.rooms.empty or context.dms.empty:
        return {
            "status": "resources_not_configured",
            "assigned": [],
            "conflicts": conflicts,
            "warnings": [
                "还没有房间或 DM 档案，场次已保存但未自动排期；"
                "请先到资源页建档或载入演示资源。"
            ],
            "partial": partial,
            "unchanged": unchanged["session_id"].astype("string").fillna("").tolist(),
            "trigger": trigger,
            "as_of": moment.strftime("%Y-%m-%d %H:%M"),
            "preview": dry_run,
        }

    leave_rows, familiarities = schedule_dm_inputs(store=store)
    result = build_schedule(
        sessions=context.sessions,
        rooms=context.rooms,
        dms=context.dms,
        shifts=context.shifts,
        assignments=context.assignments,
        scripts=context.scripts,
        now=moment,
        decision_session_ids=pending_ids,
        lock_existing=True,
        dm_leaves=leave_rows,
        familiarities=familiarities,
    )

    target_set = set(pending_ids)
    chosen = [
        item for item in result.assignments if item.session_id in target_set
    ]
    conflicts.extend(
        _conflict_dict(item)
        for item in result.shortages
        if item.session_id in target_set
    )

    if chosen and not dry_run:
        rows: list[dict[str, Any]] = []
        for item in chosen:
            rows.extend(
                _assignment_frame_for(
                    item.session_id,
                    item.room_id,
                    item.dm_id,
                    source="auto",
                    now=moment,
                )
            )
        replace_assignments(
            [item.session_id for item in chosen],
            rows,
            store=store,
            now=moment,
        )

    status = "conflict" if conflicts else "assigned"

    # assigned 每项补 script_name / start（从 sessions 帧取），前端预览可直接标注。
    sessions_meta = {
        _text(row.get("session_id")): row for row in targets.to_dict("records")
    }
    assigned_items: list[dict[str, Any]] = []
    for item in chosen:
        data = item.to_dict()
        meta = sessions_meta.get(item.session_id) or {}
        data["script_name"] = _text(meta.get("script_name"))
        data["start"] = _start_text(meta.get("start_time"))
        assigned_items.append(data)

    return {
        "status": status,
        "assigned": assigned_items,
        "conflicts": conflicts,
        "warnings": list(result.warnings),
        "partial": partial,
        "unchanged": unchanged["session_id"].astype("string").fillna("").tolist(),
        "optimal": result.optimal,
        "trigger": trigger,
        "as_of": result.as_of,
        "preview": dry_run,
    }


def auto_assign(
    store: Any,
    *,
    session_ids: Iterable[str] | None = None,
    now: Any = None,
    trigger: str = "manual",
    dry_run: bool = False,
) -> dict[str, Any]:
    with store_lock(store):
        return _auto_assign_impl(
            store,
            session_ids=session_ids,
            now=now,
            trigger=trigger,
            dry_run=dry_run,
        )


def _duration_for(context: ScheduleContext, script_name: str) -> int:
    duration = 240
    scripts = context.scripts
    if scripts.empty or not script_name:
        return duration
    matched = scripts[
        scripts["script_name"].astype("string").fillna("") == script_name
    ]
    if matched.empty:
        return duration
    value = matched.iloc[0].get("duration_minutes")
    if is_missing_value(value):
        return duration
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return duration
    return parsed if parsed > 0 else duration


def _buffer_minutes(context: ScheduleContext, kind: str, resource_id: str) -> int:
    """占用缓冲：房间取自身 buffer_minutes（缺省 15），DM 用排期休整缓冲（默认 30）。

    与 build_schedule / operations._apply_assignments_impl 的缓冲口径保持一致，
    避免同一对场次在「换房校验」与「排期求解」里一个放行一个拒绝。
    """
    if kind == "dm":
        return int(ScheduleParams().dm_rest_buffer_minutes)
    frame = context.rooms
    if frame.empty or not resource_id:
        return 15
    hit = frame[frame["room_id"].astype("string").fillna("") == resource_id]
    if hit.empty:
        return 15
    value = hit.iloc[0].get("buffer_minutes")
    if is_missing_value(value):
        return 15
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 15
    return max(parsed, 0)


def _overlap_conflict(
    context: ScheduleContext,
    session_id: str,
    kind: str,
    resource_id: str,
) -> str | None:
    """检查某个资源在目标场次时间段是否已被其他场次占用。"""
    if context.sessions.empty or context.assignments.empty:
        return None
    sessions = context.sessions.copy()
    sessions["start_time"] = pd.to_datetime(sessions["start_time"], errors="coerce")
    hit = sessions[sessions["session_id"] == session_id]
    if hit.empty:
        return "session_missing"
    row = hit.iloc[0]
    start = pd.Timestamp(row["start_time"])
    duration = _duration_for(context, _text(row.get("script_name")))
    end = start + pd.Timedelta(minutes=duration)
    buffer_min = _buffer_minutes(context, kind, resource_id)

    others = context.assignments[
        (context.assignments["resource_kind"] == kind)
        & (context.assignments["resource_id"].astype("string").fillna("") == resource_id)
        & (context.assignments["session_id"].astype("string").fillna("") != session_id)
    ]
    for other in others.to_dict("records"):
        other_id = _text(other.get("session_id"))
        other_hit = sessions[sessions["session_id"] == other_id]
        if other_hit.empty:
            continue
        other_row = other_hit.iloc[0]
        # 已取消场次不占资源（与求解器一致）；残留的分配行不应挡住换房/换 DM。
        if _text(other_row.get("status")) == "已取消":
            continue
        other_start = pd.Timestamp(other_row["start_time"])
        other_duration = _duration_for(context, _text(other_row.get("script_name")))
        # 双侧都带缓冲：各自占用区间 = [start, end + buffer)。
        if other_start < end + pd.Timedelta(minutes=buffer_min) and start < (
            other_start + pd.Timedelta(minutes=other_duration + buffer_min)
        ):
            return "schedule_conflict"
    return None


def buffered_overlap_hits(
    context: ScheduleContext,
    session_id: str,
    new_start: Any,
    duration_minutes: int | None = None,
) -> list[dict[str, Any]]:
    """改期预检：本场现有 manual 分配放到 new_start 后是否与他人撞档（含缓冲）。

    只读、无写操作；只查 source=="manual" 的分配——auto 分配在改期时本就会
    被丢弃重算（session_intake._update_session_impl）。返回撞档明细列表：
    [{"resource_kind", "resource_id", "conflict_session_id", "conflict_start"}]。
    """
    hits: list[dict[str, Any]] = []
    if context.sessions.empty or context.assignments.empty:
        return hits
    own = context.assignments[
        (context.assignments["session_id"].astype("string").fillna("") == session_id)
        & (context.assignments["source"].astype("string").fillna("") == "manual")
    ]
    if own.empty:
        return hits
    sessions = context.sessions.copy()
    sessions["start_time"] = pd.to_datetime(sessions["start_time"], errors="coerce")
    if duration_minutes is None:
        own_row = sessions[sessions["session_id"].astype("string").fillna("") == session_id]
        script_name = "" if own_row.empty else _text(own_row.iloc[0].get("script_name"))
        duration_minutes = _duration_for(context, script_name)
    start = pd.Timestamp(new_start)
    if pd.isna(start):
        return hits
    end = start + pd.Timedelta(minutes=duration_minutes)
    for row in own.to_dict("records"):
        kind = _text(row.get("resource_kind"))
        resource_id = _text(row.get("resource_id"))
        if kind not in RESOURCE_KINDS or not resource_id:
            continue
        buffer_min = _buffer_minutes(context, kind, resource_id)
        span_end = end + pd.Timedelta(minutes=buffer_min)
        others = context.assignments[
            (context.assignments["resource_kind"] == kind)
            & (context.assignments["resource_id"].astype("string").fillna("") == resource_id)
            & (context.assignments["session_id"].astype("string").fillna("") != session_id)
        ]
        for other in others.to_dict("records"):
            other_id = _text(other.get("session_id"))
            other_hit = sessions[
                sessions["session_id"].astype("string").fillna("") == other_id
            ]
            if other_hit.empty:
                continue
            # 已取消场次不占资源；改期预检也不应被残留分配误报。
            if _text(other_hit.iloc[0].get("status")) == "已取消":
                continue
            other_start = pd.Timestamp(other_hit.iloc[0]["start_time"])
            if pd.isna(other_start):
                continue
            other_duration = _duration_for(
                context, _text(other_hit.iloc[0].get("script_name"))
            )
            other_end = other_start + pd.Timedelta(
                minutes=other_duration + buffer_min
            )
            if other_start < span_end and start < other_end:
                hits.append(
                    {
                        "resource_kind": kind,
                        "resource_id": resource_id,
                        "conflict_session_id": other_id,
                        "conflict_start": other_start.strftime("%Y-%m-%d %H:%M"),
                    }
                )
    return hits


def _resolve_impl(
    store: Any,
    *,
    session_id: str,
    choice: str,
    target_id: str | None = None,
    now: Any = None,
) -> dict[str, Any]:
    moment = _now(now)
    context = load_schedule_context(store)
    if session_id not in set(context.sessions["session_id"].astype("string").fillna("")):
        return {
            "status": "conflict",
            "code": "session_not_found",
            "error": "场次不存在。",
            "options": [],
        }

    existing = context.assignments[
        context.assignments["session_id"].astype("string").fillna("") == session_id
    ]
    current_room = None
    current_dm = None
    for row in existing.to_dict("records"):
        if _text(row.get("resource_kind")) == "room":
            current_room = _text(row.get("resource_id"))
        elif _text(row.get("resource_kind")) == "dm":
            current_dm = _text(row.get("resource_id"))

    if choice == "keep_unassigned":
        return {
            "status": "kept",
            "session_id": session_id,
            "message": "已保持未分配，稍后可在排期页处理。",
        }

    if choice in {"shift_time", "cancel_or_waitlist", "add_resource"}:
        labels = {
            "shift_time": "改期需要店长确认，系统不会自动修改开场时间。",
            "cancel_or_waitlist": "取消/候补需要店长确认，系统不会自动取消场次。",
            "add_resource": "请先到资源页补房间、DM、容量、技能或班次。",
        }
        return {
            "status": "requires_confirmation",
            "session_id": session_id,
            "choice": choice,
            "message": labels[choice],
        }

    if choice == "switch_room":
        room_id = _text(target_id)
        rooms = context.rooms
        hit = rooms[rooms["room_id"].astype("string").fillna("") == room_id]
        if hit.empty:
            return {
                "status": "conflict",
                "code": "resource_not_found",
                "error": "目标房间不存在。",
            }
        room = hit.iloc[0]
        session_row = context.sessions[
            context.sessions["session_id"] == session_id
        ].iloc[0]
        total = session_row.get("total_seats")
        capacity = room.get("capacity")
        if not is_missing_value(total) and not is_missing_value(capacity):
            if int(capacity) < int(total):
                return {
                    "status": "conflict",
                    "code": "room_capacity",
                    "error": "目标房间容量不足。",
                }
        conflict = _overlap_conflict(context, session_id, "room", room_id)
        if conflict:
            return {
                "status": "conflict",
                "code": conflict,
                "error": "该房间在这个时段已被占用。",
            }
        rows = _assignment_frame_for(
            session_id, room_id, current_dm, source="manual", now=moment
        )
        replace_assignments([session_id], rows, store=store, now=moment)
        return {
            "status": "applied",
            "session_id": session_id,
            "room_id": room_id,
            "dm_id": current_dm,
            "message": "已换房。",
        }

    if choice == "switch_dm":
        dm_id = _text(target_id)
        dms = context.dms
        hit = dms[dms["dm_id"].astype("string").fillna("") == dm_id]
        if hit.empty:
            return {
                "status": "conflict",
                "code": "resource_not_found",
                "error": "目标 DM 不存在。",
            }
        conflict = _overlap_conflict(context, session_id, "dm", dm_id)
        if conflict:
            return {
                "status": "conflict",
                "code": conflict,
                "error": "该 DM 在这个时段已有场次。",
            }
        rows = _assignment_frame_for(
            session_id, current_room, dm_id, source="manual", now=moment
        )
        replace_assignments([session_id], rows, store=store, now=moment)
        return {
            "status": "applied",
            "session_id": session_id,
            "room_id": current_room,
            "dm_id": dm_id,
            "message": "已换 DM。",
        }

    return {
        "status": "bad_request",
        "code": "invalid_choice",
        "error": "未知的冲突处理方式。",
    }


def resolve_conflict(
    store: Any,
    *,
    session_id: str,
    choice: str,
    target_id: str | None = None,
    now: Any = None,
) -> dict[str, Any]:
    with store_lock(store):
        return _resolve_impl(
            store,
            session_id=session_id,
            choice=choice,
            target_id=target_id,
            now=now,
        )


def list_schedule(
    store: Any,
    *,
    start_from: Any = None,
    start_to: Any = None,
    now: Any = None,
    context: "ScheduleContext | None" = None,
) -> dict[str, Any]:
    """按窗口列出场次及其房间/DM 分配。

    ``context`` 允许调用方传入已经加载好的帧：``schedule_view`` 需要房间、
    DM、剧本的完整数据，如果每次都让这里再从库里读一遍，同一个请求内
    rooms/dms/scripts 会被各读两遍（跨境部署每条 55ms）。
    """
    moment = _now(now)
    context = load_schedule_context(store) if context is None else context
    sessions = context.sessions.copy()
    sessions["start_time"] = pd.to_datetime(sessions["start_time"], errors="coerce")
    if start_from is not None:
        sessions = sessions[sessions["start_time"] >= pd.Timestamp(start_from)]
    if start_to is not None:
        sessions = sessions[sessions["start_time"] <= pd.Timestamp(start_to)]
    sessions = sessions.sort_values(["start_time", "session_id"])

    assignments = context.assignments
    items: list[dict[str, Any]] = []
    for row in sessions.to_dict("records"):
        # 场次行里带 updated_at 等时间列，直接返回会导致 Web JSON 序列化失败。
        session = {key: json_safe(value) for key, value in row.items()}
        session_id = _text(row.get("session_id"))
        session["start_time"] = pd.Timestamp(row["start_time"]).strftime("%Y-%m-%d %H:%M")
        booked = row.get("booked_seats")
        session["booked_seats"] = (
            0 if is_missing_value(booked) else int(booked)
        )
        matching = assignments[
            assignments["session_id"].astype("string").fillna("") == session_id
        ]
        for item in matching.to_dict("records"):
            kind = _text(item.get("resource_kind"))
            session[f"{kind}_id"] = _text(item.get("resource_id"))
            session[f"{kind}_source"] = _text(item.get("source"))
        items.append(session)
    return {
        "sessions": items,
        "as_of": moment.strftime("%Y-%m-%d %H:%M"),
    }


__all__ = [
    "RESOURCE_KINDS",
    "ScheduleContext",
    "auto_assign",
    "list_schedule",
    "load_schedule_context",
    "resolve_conflict",
]

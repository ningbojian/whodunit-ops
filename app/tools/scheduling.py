"""固定场次的资源排期引擎。

输入是已存在的场次、房间、DM、班次和当前分配；输出是房间/DM 分配建议
与缺口说明。这里不修改开场时间、不合并场次、不取消场次，也不读写数据库。

求解思路：
  1. 先过滤出待排场次，按时间重叠拆成互不影响的连通分量；
  2. 每个分量用确定性 DFS + 分支限界；目标先最大化已分配场次数，
     再最大化预计贡献；
  3. 节点数到上限就停止，并如实返回 optimal=false。
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Iterable

import pandas as pd

from app.tools.scripts import normalize_script_name, split_aliases

BUSINESS_DAY_START_HOUR = 6
DEFAULT_DURATION_MINUTES = 240
_ASSIGN_WEIGHT = 1_000_000_000.0
_EPS = 1e-6


@dataclass(frozen=True, slots=True)
class ScheduleParams:
    default_duration_minutes: int = DEFAULT_DURATION_MINUTES
    room_buffer_minutes: int = 15
    dm_rest_buffer_minutes: int = 30
    fill_ratio: float = 0.35
    marginal_cost_per_seat: float = 0.0
    max_shift_minutes: int = 120
    shift_step_minutes: int = 30
    max_search_nodes: int = 50_000


@dataclass(frozen=True, slots=True)
class RoomSpec:
    room_id: str
    name: str
    capacity: int | None
    hourly_cost: float
    buffer_minutes: int
    active: bool


@dataclass(frozen=True, slots=True)
class DMSpec:
    dm_id: str
    name: str
    skills: frozenset[str]
    hourly_cost: float
    max_sessions_per_day: int | None
    active: bool


@dataclass(frozen=True, slots=True)
class ShiftSpec:
    dm_id: str
    weekday: int
    start_minute: int
    end_minute: int
    active: bool


@dataclass(frozen=True, slots=True)
class SessionSpec:
    session_id: str
    script_name: str
    script_type: str
    start: pd.Timestamp
    duration_minutes: int
    total_seats: int | None
    booked_seats: int
    list_price: float
    status: str
    room_id: str | None = None
    dm_id: str | None = None
    assignment_source: str | None = None
    locked: bool = False
    duration_source: str = "script"
    price_source: str = "session"

    @property
    def end(self) -> pd.Timestamp:
        return self.start + pd.Timedelta(minutes=self.duration_minutes)


@dataclass(frozen=True, slots=True)
class AssignmentSpec:
    session_id: str
    room_id: str
    dm_id: str
    room_name: str
    dm_name: str
    source: str = "auto"
    contribution: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "room_id": self.room_id,
            "dm_id": self.dm_id,
            "room_name": self.room_name,
            "dm_name": self.dm_name,
            "source": self.source,
            "expected_contribution": round(self.contribution, 2),
        }


@dataclass(frozen=True, slots=True)
class Suggestion:
    kind: str
    label: str
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"type": self.kind, "label": self.label, **self.details}


@dataclass(frozen=True, slots=True)
class Shortage:
    session_id: str
    script_name: str
    start: str
    total_seats: int | None
    booked_seats: int
    reason: str
    message: str
    options: tuple[Suggestion, ...] = ()
    booked_revenue_at_risk: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "script_name": self.script_name,
            "start": self.start,
            "total_seats": self.total_seats,
            "booked_seats": self.booked_seats,
            "reason": self.reason,
            "message": self.message,
            "booked_revenue_at_risk": round(self.booked_revenue_at_risk, 2),
            "options": [option.to_dict() for option in self.options],
        }


@dataclass(slots=True)
class ScheduleResult:
    assignments: list[AssignmentSpec]
    shortages: list[Shortage]
    warnings: list[str]
    optimal: bool
    stats: dict[str, Any]
    as_of: str
    booked_revenue_at_risk: float
    full_house_value: float

    @property
    def search_truncated(self) -> bool:
        return bool(self.stats.get("search_truncated"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "assignments": [item.to_dict() for item in self.assignments],
            "shortages": [item.to_dict() for item in self.shortages],
            "warnings": list(self.warnings),
            "optimal": self.optimal,
            "search_truncated": self.search_truncated,
            "stats": dict(self.stats),
            "as_of": self.as_of,
            "booked_revenue_at_risk": round(self.booked_revenue_at_risk, 2),
            "full_house_value": round(self.full_house_value, 2),
        }


@dataclass(frozen=True, slots=True)
class _Candidate:
    room: RoomSpec
    dm: DMSpec
    contribution: float


@dataclass(frozen=True, slots=True)
class _Interval:
    start: pd.Timestamp
    end: pd.Timestamp


def _text(value: Any) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    try:
        if pd.isna(value):
            return ""
    except (TypeError, ValueError):
        pass
    return str(value).strip()


def _to_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _to_float(value: Any, default: float = 0.0) -> float:
    if value is None:
        return default
    try:
        if pd.isna(value):
            return default
    except (TypeError, ValueError):
        pass
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_bool(value: Any, default: bool = True) -> bool:
    if value is None:
        return default
    try:
        if pd.isna(value):
            return default
    except (TypeError, ValueError):
        pass
    if isinstance(value, bool):
        return value
    text = str(value).strip().casefold()
    if text in {"1", "true", "yes", "y", "是", "启用", "有效"}:
        return True
    if text in {"0", "false", "no", "n", "否", "停用", "无效"}:
        return False
    return default


def _as_naive(value: Any) -> pd.Timestamp:
    moment = pd.Timestamp(value)
    if moment.tzinfo is not None:
        moment = moment.tz_convert("Asia/Shanghai").tz_localize(None)
    return moment


def _parse_skills(value: Any) -> frozenset[str]:
    text = _text(value)
    if not text:
        return frozenset()
    parts = re.split(r"[/,，、|;；\s]+", text)
    return frozenset(part.casefold() for part in parts if part)


def _business_day(moment: pd.Timestamp) -> str:
    return (moment - pd.Timedelta(hours=BUSINESS_DAY_START_HOUR)).date().isoformat()


def _overlaps(left: _Interval, right: _Interval) -> bool:
    return left.start < right.end and right.start < left.end


def _free(intervals: list[_Interval], candidate: _Interval) -> bool:
    return all(not _overlaps(item, candidate) for item in intervals)


def _shift_covers(
    shifts: list[ShiftSpec],
    start: pd.Timestamp,
    end: pd.Timestamp,
) -> bool:
    cursor = start
    while cursor < end:
        day_end = cursor.normalize() + pd.Timedelta(days=1)
        segment_end = min(end, day_end)
        weekday = int(cursor.weekday())
        start_minute = cursor.hour * 60 + cursor.minute
        if segment_end == day_end:
            end_minute = 1440
        else:
            end_minute = segment_end.hour * 60 + segment_end.minute
        covered = any(
            item.active
            and item.weekday == weekday
            and item.start_minute <= start_minute
            and item.end_minute >= end_minute
            for item in shifts
        )
        if not covered:
            return False
        cursor = segment_end
    return True


class _ScriptLookup:
    def __init__(self, scripts: pd.DataFrame | None) -> None:
        self.by_name: dict[str, tuple[dict[str, Any], str]] = {}
        self.by_alias: dict[str, tuple[dict[str, Any], str]] = {}
        if scripts is None or scripts.empty:
            return
        for row in scripts.to_dict("records"):
            record = {key: row.get(key) for key in row}
            name_key = normalize_script_name(record.get("script_name")).casefold()
            if name_key and name_key not in self.by_name:
                self.by_name[name_key] = (record, "script")
            for alias in split_aliases(record.get("aliases")):
                alias_key = normalize_script_name(alias).casefold()
                if alias_key and alias_key not in self.by_alias:
                    self.by_alias[alias_key] = (record, "alias")

    def find(self, name: Any) -> tuple[dict[str, Any] | None, str]:
        key = normalize_script_name(name).casefold()
        if not key:
            return None, "none"
        found = self.by_name.get(key)
        if found:
            return found[0], "script"
        found = self.by_alias.get(key)
        if found:
            return found[0], "alias"
        return None, "none"


class _Occupancy:
    def __init__(self) -> None:
        self.rooms: dict[str, list[_Interval]] = {}
        self.dms: dict[str, list[_Interval]] = {}
        self.dm_days: dict[tuple[str, str], int] = {}

    def add(
        self,
        room_id: str,
        dm_id: str,
        room_interval: _Interval,
        dm_interval: _Interval,
        day: str,
    ) -> None:
        self.rooms.setdefault(room_id, []).append(room_interval)
        self.dms.setdefault(dm_id, []).append(dm_interval)
        key = (dm_id, day)
        self.dm_days[key] = self.dm_days.get(key, 0) + 1

    def remove(
        self,
        room_id: str,
        dm_id: str,
        room_interval: _Interval,
        dm_interval: _Interval,
        day: str,
    ) -> None:
        self.rooms.get(room_id, []).remove(room_interval)
        self.dms.get(dm_id, []).remove(dm_interval)
        key = (dm_id, day)
        remaining = self.dm_days.get(key, 0) - 1
        if remaining > 0:
            self.dm_days[key] = remaining
        else:
            self.dm_days.pop(key, None)


def _build_specs(
    sessions: pd.DataFrame,
    scripts: pd.DataFrame | None,
) -> tuple[list[SessionSpec], list[str]]:
    warnings: list[str] = []
    lookup = _ScriptLookup(scripts)
    specs: list[SessionSpec] = []
    for row in sessions.to_dict("records"):
        session_id = _text(row.get("session_id"))
        if not session_id:
            continue
        script_name = _text(row.get("script_name"))
        script, match = lookup.find(script_name)
        duration = _to_int(script.get("duration_minutes")) if script else None
        duration_source = "script" if duration else "default_fallback"
        if not duration or duration <= 0:
            duration = DEFAULT_DURATION_MINUTES
            duration_source = "default_fallback"
            warnings.append(
                f"{script_name or session_id} 缺剧本时长，按默认 {DEFAULT_DURATION_MINUTES} 分钟估算。"
            )
        total_seats = _to_int(row.get("total_seats"))
        if total_seats is None and script is not None:
            total_seats = _to_int(script.get("total_seats"))
        booked = max(0, _to_int(row.get("booked_seats")) or 0)
        if total_seats is None and booked > 0:
            # 只知道已约人数时，把它当作容量下限；不猜测还有多少空位。
            total_seats = booked
            warnings.append(
                f"{script_name or session_id} 缺总人数，按已约 {booked} 人作为排房下限。"
            )
        price = _to_float(row.get("list_price"), default=0.0)
        price_source = "session"
        if price <= 0 and script is not None:
            price = _to_float(script.get("list_price"), default=0.0)
            price_source = "script" if price > 0 else "missing"
        elif price <= 0:
            price_source = "missing"
        script_type = _text(row.get("script_type")) or (
            _text(script.get("script_type")) if script else ""
        )
        specs.append(
            SessionSpec(
                session_id=session_id,
                script_name=script_name,
                script_type=script_type,
                start=_as_naive(row.get("start_time")),
                duration_minutes=duration,
                total_seats=total_seats,
                booked_seats=booked,
                list_price=price,
                status=_text(row.get("status")),
                duration_source=duration_source,
                price_source=price_source,
            )
        )
    return specs, warnings


def _build_rooms(rooms: pd.DataFrame | None) -> list[RoomSpec]:
    result: list[RoomSpec] = []
    if rooms is None or rooms.empty:
        return result
    for row in rooms.to_dict("records"):
        room_id = _text(row.get("room_id"))
        if not room_id:
            continue
        buffer_minutes = _to_int(row.get("buffer_minutes"))
        result.append(
            RoomSpec(
                room_id=room_id,
                name=_text(row.get("name")) or room_id,
                capacity=_to_int(row.get("capacity")),
                hourly_cost=_to_float(row.get("hourly_cost")),
                buffer_minutes=max(0, min(120, buffer_minutes or 0)),
                active=_as_bool(row.get("active"), True),
            )
        )
    return result


def _build_dms(dms: pd.DataFrame | None) -> list[DMSpec]:
    result: list[DMSpec] = []
    if dms is None or dms.empty:
        return result
    for row in dms.to_dict("records"):
        dm_id = _text(row.get("dm_id"))
        if not dm_id:
            continue
        maximum = _to_int(row.get("max_sessions_per_day"))
        result.append(
            DMSpec(
                dm_id=dm_id,
                name=_text(row.get("name")) or dm_id,
                skills=_parse_skills(row.get("skills")),
                hourly_cost=_to_float(row.get("hourly_cost")),
                max_sessions_per_day=maximum if maximum and maximum > 0 else None,
                active=_as_bool(row.get("active"), True),
            )
        )
    return result


def _build_shifts(shifts: pd.DataFrame | None) -> list[ShiftSpec]:
    result: list[ShiftSpec] = []
    if shifts is None or shifts.empty:
        return result
    for row in shifts.to_dict("records"):
        dm_id = _text(row.get("dm_id"))
        weekday = _to_int(row.get("weekday"))
        start_minute = _to_int(row.get("start_minute"))
        end_minute = _to_int(row.get("end_minute"))
        if (
            not dm_id
            or weekday is None
            or start_minute is None
            or end_minute is None
            or not (0 <= weekday <= 6)
            or end_minute <= start_minute
        ):
            continue
        result.append(
            ShiftSpec(
                dm_id=dm_id,
                weekday=weekday,
                start_minute=max(0, start_minute),
                end_minute=min(1440, end_minute),
                active=_as_bool(row.get("active"), True),
            )
        )
    return result


def _attach_assignments(
    specs: list[SessionSpec],
    assignments: pd.DataFrame | None,
    *,
    lock_existing: bool,
    warnings: list[str],
) -> list[SessionSpec]:
    if assignments is None or assignments.empty:
        return specs
    by_id = {spec.session_id: spec for spec in specs}
    room_by_session: dict[str, str] = {}
    dm_by_session: dict[str, str] = {}
    source_by_session: dict[str, str] = {}
    for row in assignments.to_dict("records"):
        session_id = _text(row.get("session_id"))
        if session_id not in by_id:
            # 已结束或不在本次窗口内的场次不在 specs 里，这里只忽略；
            # 真正的孤儿清理由资源保存路径负责。
            continue
        kind = _text(row.get("resource_kind"))
        resource_id = _text(row.get("resource_id"))
        if not resource_id:
            continue
        source = _text(row.get("source")) or "auto"
        if kind == "room":
            if session_id in room_by_session:
                warnings.append(f"{session_id} 有多个房间分配，只采用第一条。")
                continue
            room_by_session[session_id] = resource_id
        elif kind == "dm":
            if session_id in dm_by_session:
                warnings.append(f"{session_id} 有多个 DM 分配，只采用第一条。")
                continue
            dm_by_session[session_id] = resource_id
        else:
            warnings.append(f"{session_id} 有未知资源类型：{kind}，已忽略。")
            continue
        source_by_session[session_id] = source

    result: list[SessionSpec] = []
    for spec in specs:
        room_id = room_by_session.get(spec.session_id)
        dm_id = dm_by_session.get(spec.session_id)
        source = source_by_session.get(spec.session_id)
        locked = bool(
            room_id
            and dm_id
            and (lock_existing or source == "manual")
        )
        result.append(
            replace(
                spec,
                room_id=room_id,
                dm_id=dm_id,
                assignment_source=source,
                locked=locked,
            )
        )
    return result


def _duration_hours(spec: SessionSpec) -> float:
    return spec.duration_minutes / 60.0


def _contribution(
    spec: SessionSpec,
    room: RoomSpec,
    dm: DMSpec,
    params: ScheduleParams,
) -> float:
    price = max(0.0, spec.list_price)
    total = max(0, spec.total_seats or 0)
    booked = min(max(0, spec.booked_seats), total) if total else max(0, spec.booked_seats)
    expected_extra = max(total - booked, 0) * params.fill_ratio
    expected_revenue = (booked + expected_extra) * price
    hours = _duration_hours(spec)
    resource_cost = (room.hourly_cost + dm.hourly_cost) * hours
    variable_cost = params.marginal_cost_per_seat * (booked + expected_extra)
    return expected_revenue - resource_cost - variable_cost


def _candidate_rooms(spec: SessionSpec, rooms: list[RoomSpec]) -> list[RoomSpec]:
    seats = spec.total_seats
    if seats is None or seats <= 0:
        return []
    return [
        room
        for room in rooms
        if room.active and room.capacity is not None and room.capacity >= seats
    ]


def _skill_ok(spec: SessionSpec, dm: DMSpec) -> bool:
    if not spec.script_type or not dm.skills:
        return True
    return spec.script_type.casefold() in dm.skills


def _candidate_pairs(
    spec: SessionSpec,
    rooms: list[RoomSpec],
    dms: list[DMSpec],
    shifts: list[ShiftSpec],
    params: ScheduleParams,
    *,
    has_shifts: set[str],
    leave_days: frozenset[tuple[str, str]] | None = None,
    familiarities: dict[str, dict[str, int]] | None = None,
) -> list[_Candidate]:
    result: list[_Candidate] = []
    business_day = _business_day(spec.start)
    name_key = normalize_script_name(spec.script_name).casefold()
    for room in _candidate_rooms(spec, rooms):
        for dm in dms:
            if not dm.active or not _skill_ok(spec, dm):
                continue
            # 请假的 DM 当天不参与自动分配（WP13B）。
            if leave_days and (dm.dm_id, business_day) in leave_days:
                continue
            dm_end = spec.end + pd.Timedelta(minutes=params.dm_rest_buffer_minutes)
            if dm.dm_id in has_shifts and not _shift_covers(shifts, spec.start, dm_end):
                continue
            result.append(
                _Candidate(
                    room=room,
                    dm=dm,
                    contribution=_contribution(spec, room, dm, params),
                )
            )
    # 熟悉度只作次级键：同收益时优先带过该本的 DM，不翻盘经济最优。
    def _familiarity(item: _Candidate) -> int:
        if not familiarities:
            return 0
        return familiarities.get(item.dm.dm_id, {}).get(name_key, 0)

    result.sort(
        key=lambda item: (
            -item.contribution,
            -_familiarity(item),
            item.room.capacity if item.room.capacity is not None else 10**9,
            item.room.room_id,
            item.dm.dm_id,
        )
    )
    return result


def _intervals_for(
    spec: SessionSpec,
    room: RoomSpec,
    dm: DMSpec,
    params: ScheduleParams,
) -> tuple[_Interval, _Interval]:
    room_end = spec.end + pd.Timedelta(minutes=room.buffer_minutes)
    dm_end = spec.end + pd.Timedelta(minutes=params.dm_rest_buffer_minutes)
    return (
        _Interval(spec.start, room_end),
        _Interval(spec.start, dm_end),
    )


def _feasible(
    occupancy: _Occupancy,
    candidate: _Candidate,
    spec: SessionSpec,
    room_interval: _Interval,
    dm_interval: _Interval,
) -> bool:
    if not _free(occupancy.rooms.get(candidate.room.room_id, []), room_interval):
        return False
    if not _free(occupancy.dms.get(candidate.dm.dm_id, []), dm_interval):
        return False
    maximum = candidate.dm.max_sessions_per_day
    if maximum is not None:
        used = occupancy.dm_days.get(
            (candidate.dm.dm_id, _business_day(spec.start)),
            0,
        )
        if used >= maximum:
            return False
    return True


def _build_components(
    specs: list[SessionSpec],
    rooms: list[RoomSpec],
    params: ScheduleParams,
    *,
    group_by_day: bool = False,
) -> list[list[SessionSpec]]:
    if not specs:
        return []
    max_buffer = max(
        [params.dm_rest_buffer_minutes]
        + [room.buffer_minutes for room in rooms]
    )
    ordered = sorted(specs, key=lambda item: (item.start, item.session_id))
    parent = list(range(len(ordered)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left: int, right: int) -> None:
        root_left, root_right = find(left), find(right)
        if root_left != root_right:
            parent[root_right] = root_left

    windows = [
        (
            item.start,
            item.end + pd.Timedelta(minutes=max_buffer),
        )
        for item in ordered
    ]
    for index in range(1, len(ordered)):
        cursor = index - 1
        while cursor >= 0 and windows[cursor][1] > windows[index][0]:
            union(cursor, index)
            cursor -= 1

    if group_by_day:
        # DM 每日场次上限会跨时间段耦合，同一业务日不能拆成独立分量。
        by_day: dict[str, int] = {}
        for index, spec in enumerate(ordered):
            day = _business_day(spec.start)
            if day in by_day:
                union(by_day[day], index)
            else:
                by_day[day] = index

    groups: dict[int, list[SessionSpec]] = {}
    for index, spec in enumerate(ordered):
        groups.setdefault(find(index), []).append(spec)
    return [groups[key] for key in sorted(groups)]


def _locked_occupancy(
    specs: list[SessionSpec],
    rooms_by_id: dict[str, RoomSpec],
    dms_by_id: dict[str, DMSpec],
    params: ScheduleParams,
    warnings: list[str],
) -> tuple[_Occupancy, dict[str, _Candidate]]:
    occupancy = _Occupancy()
    locked: dict[str, _Candidate] = {}
    for spec in specs:
        if not spec.locked or not spec.room_id or not spec.dm_id:
            continue
        room = rooms_by_id.get(spec.room_id)
        dm = dms_by_id.get(spec.dm_id)
        if room is None or dm is None:
            warnings.append(
                f"{spec.session_id} 的手工分配引用了不存在的房间或 DM，已改为自动排期。"
            )
            continue
        room_interval, dm_interval = _intervals_for(spec, room, dm, params)
        occupancy.add(
            room.room_id,
            dm.dm_id,
            room_interval,
            dm_interval,
            _business_day(spec.start),
        )
        contribution = _contribution(spec, room, dm, params)
        locked[spec.session_id] = _Candidate(room, dm, contribution)
        if room.capacity is None or room.capacity < (spec.total_seats or 0):
            warnings.append(f"{spec.session_id} 的手工房间容量不足。")
        maximum = dm.max_sessions_per_day
        if maximum is not None:
            used = occupancy.dm_days.get((dm.dm_id, _business_day(spec.start)), 0)
            if used > maximum:
                warnings.append(f"{spec.session_id} 使 {dm.name} 超过每日场次上限。")
    return occupancy, locked


def _solve_component(
    component: list[SessionSpec],
    candidates: dict[str, list[_Candidate]],
    occupancy: _Occupancy,
    locked: dict[str, _Candidate],
    params: ScheduleParams,
    node_budget: list[int],
) -> tuple[dict[str, _Candidate], bool]:
    order = sorted(
        [item for item in component if not item.locked],
        key=lambda item: (
            len(candidates.get(item.session_id, [])),
            -item.start.value,
            item.session_id,
        ),
    )
    if not order:
        return {}, True

    suffix = [0.0] * (len(order) + 1)
    for index in range(len(order) - 1, -1, -1):
        spec = order[index]
        best_value = max(
            (candidate.contribution for candidate in candidates.get(spec.session_id, [])),
            default=0.0,
        )
        suffix[index] = suffix[index + 1] + (
            (_ASSIGN_WEIGHT + best_value)
            if candidates.get(spec.session_id)
            else 0.0
        )

    best_score = 0.0
    best: dict[str, _Candidate] = {}
    chosen: dict[str, _Candidate] = {}
    truncated = [False]

    def dfs(index: int, score: float) -> None:
        nonlocal best_score, best
        if node_budget[0] <= 0:
            truncated[0] = True
            return
        node_budget[0] -= 1
        if index >= len(order):
            if score > best_score + _EPS:
                best_score = score
                best = dict(chosen)
            return
        if score + suffix[index] <= best_score + _EPS:
            return

        spec = order[index]
        for candidate in candidates.get(spec.session_id, []):
            room_interval, dm_interval = _intervals_for(
                spec, candidate.room, candidate.dm, params
            )
            if not _feasible(occupancy, candidate, spec, room_interval, dm_interval):
                continue
            occupancy.add(
                candidate.room.room_id,
                candidate.dm.dm_id,
                room_interval,
                dm_interval,
                _business_day(spec.start),
            )
            chosen[spec.session_id] = candidate
            dfs(index + 1, score + _ASSIGN_WEIGHT + candidate.contribution)
            chosen.pop(spec.session_id, None)
            occupancy.remove(
                candidate.room.room_id,
                candidate.dm.dm_id,
                room_interval,
                dm_interval,
                _business_day(spec.start),
            )
            if truncated[0]:
                return
        dfs(index + 1, score)

    dfs(0, 0.0)
    return best, not truncated[0]


def _reason_for(
    spec: SessionSpec,
    rooms: list[RoomSpec],
    dms: list[DMSpec],
    shifts: list[ShiftSpec],
    has_shifts: set[str],
    params: ScheduleParams,
    occupancy: _Occupancy,
) -> str:
    if spec.total_seats is None or spec.total_seats <= 0:
        if spec.booked_seats <= 0:
            return "missing_total_seats"
    active_rooms = [room for room in rooms if room.active]
    if not active_rooms:
        return "no_active_room"
    if all(room.capacity is None for room in active_rooms):
        return "missing_room_capacity"
    if not _candidate_rooms(spec, rooms):
        return "room_capacity"
    active_dms = [dm for dm in dms if dm.active]
    if not active_dms:
        return "no_active_dm"
    if not any(_skill_ok(spec, dm) for dm in active_dms):
        return "dm_skill"
    if not any(
        _skill_ok(spec, dm)
        and (
            dm.dm_id not in has_shifts
            or _shift_covers(
                shifts,
                spec.start,
                spec.end + pd.Timedelta(minutes=params.dm_rest_buffer_minutes),
            )
        )
        for dm in active_dms
    ):
        return "dm_unavailable"
    return "resource_overlap"


def _suggestions(
    spec: SessionSpec,
    reason: str,
    rooms: list[RoomSpec],
    dms: list[DMSpec],
    shifts: list[ShiftSpec],
    has_shifts: set[str],
    params: ScheduleParams,
    occupancy: _Occupancy,
    assigned_specs: dict[str, SessionSpec],
    assigned: dict[str, _Candidate],
    *,
    leave_days: frozenset[tuple[str, str]] | None = None,
    familiarities: dict[str, dict[str, int]] | None = None,
) -> tuple[Suggestion, ...]:
    options: list[Suggestion] = []
    seats = spec.total_seats or spec.booked_seats
    if reason in {"no_active_room", "room_capacity", "missing_room_capacity"}:
        options.append(
            Suggestion(
                "add_resource",
                "补一间容量足够的房间，或把这场的人数改小。",
                {"resource": "room"},
            )
        )
    if reason in {"no_active_dm", "dm_skill", "dm_unavailable"}:
        options.append(
            Suggestion(
                "add_resource",
                "补 DM、补技能标签，或补该时段的班次。",
                {"resource": "dm"},
            )
        )

    if reason != "missing_total_seats":
        target_room_interval = None
        candidates = _candidate_pairs(
            spec, rooms, dms, shifts, params, has_shifts=has_shifts,
            leave_days=leave_days, familiarities=familiarities,
        )
        if candidates:
            # 直接换到空闲组合
            for candidate in candidates:
                room_interval, dm_interval = _intervals_for(
                    spec, candidate.room, candidate.dm, params
                )
                if _feasible(occupancy, candidate, spec, room_interval, dm_interval):
                    options.append(
                        Suggestion(
                            "switch_room",
                            f"可直接排到 {candidate.room.name} / {candidate.dm.name}。",
                            {
                                "room_id": candidate.room.room_id,
                                "dm_id": candidate.dm.dm_id,
                                "target_id": candidate.room.room_id,
                            },
                        )
                    )
                    break
            else:
                # 找同房间冲突，尝试把对方挪走
                for candidate in candidates:
                    room_interval, _ = _intervals_for(
                        spec, candidate.room, candidate.dm, params
                    )
                    for other_id, other_candidate in assigned.items():
                        if other_candidate.room.room_id != candidate.room.room_id:
                            continue
                        other_spec = assigned_specs.get(other_id)
                        if other_spec is None:
                            continue
                        other_room_interval, _ = _intervals_for(
                            other_spec,
                            other_candidate.room,
                            other_candidate.dm,
                            params,
                        )
                        if not _overlaps(room_interval, other_room_interval):
                            continue
                        for alternate in _candidate_rooms(other_spec, rooms):
                            if alternate.room_id == other_candidate.room.room_id:
                                continue
                            alt_interval, _ = _intervals_for(
                                other_spec, alternate, other_candidate.dm, params
                            )
                            if _free(
                                occupancy.rooms.get(alternate.room_id, []),
                                alt_interval,
                            ):
                                options.append(
                                    Suggestion(
                                        "switch_room",
                                        f"把 {other_spec.script_name} 换到 {alternate.name}，"
                                        f"给这场腾出 {candidate.room.name}。",
                                        {
                                            "room_id": alternate.room_id,
                                            "move_session_id": other_id,
                                            "target_id": alternate.room_id,
                                        },
                                    )
                                )
                                break
                        if len(options) >= 2:
                            break
                    if len(options) >= 2:
                        break

        # 换 DM：目标场次由另一个空闲 DM 承接
        room_options = _candidate_rooms(spec, rooms)
        for dm in dms:
            if not dm.active or not _skill_ok(spec, dm):
                continue
            if dm.dm_id in has_shifts and not _shift_covers(
                shifts,
                spec.start,
                spec.end + pd.Timedelta(minutes=params.dm_rest_buffer_minutes),
            ):
                continue
            for room in room_options:
                candidate = _Candidate(room, dm, _contribution(spec, room, dm, params))
                room_interval, dm_interval = _intervals_for(
                    spec, room, dm, params
                )
                if _feasible(occupancy, candidate, spec, room_interval, dm_interval):
                    options.append(
                        Suggestion(
                            "switch_dm",
                            f"可换到 {dm.name}（{room.name}）。",
                            {
                                "dm_id": dm.dm_id,
                                "room_id": room.room_id,
                                "target_id": dm.dm_id,
                            },
                        )
                    )
                    break
            if len(options) >= 3:
                break

        # 改期：±120 分钟，30 分钟步长
        for offset in range(
            -params.max_shift_minutes,
            params.max_shift_minutes + 1,
            params.shift_step_minutes,
        ):
            if offset == 0:
                continue
            shifted_start = spec.start + pd.Timedelta(minutes=offset)
            shifted = replace(spec, start=shifted_start)
            found = None
            for room in room_options:
                for dm in dms:
                    if not dm.active or not _skill_ok(shifted, dm):
                        continue
                    if dm.dm_id in has_shifts and not _shift_covers(
                        shifts,
                        shifted.start,
                        shifted.end
                        + pd.Timedelta(minutes=params.dm_rest_buffer_minutes),
                    ):
                        continue
                    candidate = _Candidate(
                        room, dm, _contribution(shifted, room, dm, params)
                    )
                    room_interval, dm_interval = _intervals_for(
                        shifted, room, dm, params
                    )
                    if _feasible(
                        occupancy, candidate, shifted, room_interval, dm_interval
                    ):
                        found = (shifted_start, room, dm)
                        break
                if found:
                    break
            if found:
                shifted_start, room, dm = found
                options.append(
                    Suggestion(
                        "shift_time",
                        f"改到 {shifted_start.strftime('%H:%M')} 可排 "
                        f"{room.name} / {dm.name}（只建议，不自动改）。",
                        {
                            "new_start": shifted_start.strftime("%Y-%m-%d %H:%M"),
                            "room_id": room.room_id,
                            "dm_id": dm.dm_id,
                        },
                    )
                )
                break

        # 取消/候补：找出与目标冲突、贡献最低的已排场次
        conflicts: list[tuple[float, str, str]] = []
        for other_id, other_candidate in assigned.items():
            other_spec = assigned_specs.get(other_id)
            if other_spec is None:
                continue
            if not _overlaps(
                _Interval(spec.start, spec.end),
                _Interval(other_spec.start, other_spec.end),
            ):
                continue
            conflicts.append(
                (
                    other_candidate.contribution,
                    other_id,
                    other_spec.script_name,
                )
            )
        conflicts.sort()
        if conflicts:
            _, other_id, other_name = conflicts[0]
            options.append(
                Suggestion(
                    "cancel_or_waitlist",
                    f"若必须保这场，可让《{other_name}》改期或候补（需人工确认）。",
                    {"session_id": other_id},
                )
            )

    options.append(
        Suggestion(
            "keep_unassigned",
            "先保存场次、暂不排资源，之后再处理。",
            {},
        )
    )
    return tuple(options[:3])


def build_schedule(
    *,
    sessions: pd.DataFrame,
    rooms: pd.DataFrame | None = None,
    dms: pd.DataFrame | None = None,
    shifts: pd.DataFrame | None = None,
    assignments: pd.DataFrame | None = None,
    scripts: pd.DataFrame | None = None,
    public: pd.DataFrame | None = None,
    params: ScheduleParams | None = None,
    now: datetime | pd.Timestamp | None = None,
    start_from: Any = None,
    start_to: Any = None,
    target_session_ids: Iterable[str] | None = None,
    decision_session_ids: Iterable[str] | None = None,
    lock_existing: bool = True,
    dm_leaves: Iterable[dict[str, Any]] | None = None,
    familiarities: dict[str, dict[str, int]] | None = None,
) -> ScheduleResult:
    """生成排期结果；不写任何表。

    dm_leaves：[{dm_id, day}] 请假名单；当天落地的场次不自动分给请假 DM。
    familiarities：{dm_id: {剧本名key: 带本次数}}；同收益时优先带过的 DM，
    并对没带过的组合给出提醒 warning。
    """
    started = time.perf_counter()
    params = params or ScheduleParams()
    warnings: list[str] = []
    # 请假名单：(dm_id, 业务日) 集合；None/空时行为与旧行为完全一致。
    leave_pairs: set[tuple[str, str]] = set()
    for item in dm_leaves or ():
        leave_dm = str((item or {}).get("dm_id") or "").strip()
        leave_day = str((item or {}).get("day") or "").strip()
        if leave_dm and leave_day:
            leave_pairs.add((leave_dm, leave_day[:10]))
    leave_days = frozenset(leave_pairs) or None

    if sessions is None or sessions.empty:
        return ScheduleResult(
            assignments=[],
            shortages=[],
            warnings=["没有可排期的场次。"],
            optimal=True,
            stats={
                "nodes": 0,
                "clusters": 0,
                "elapsed_ms": 0,
                "search_truncated": False,
            },
            as_of=_as_naive(now or pd.Timestamp.now()).strftime("%Y-%m-%d %H:%M"),
            booked_revenue_at_risk=0.0,
            full_house_value=0.0,
        )

    frame = sessions.copy()
    for column, default in (
        ("booked_seats", 0),
        ("status", ""),
    ):
        if column not in frame.columns:
            frame[column] = default
    lookup_scripts = scripts
    if public is not None and not getattr(public, "empty", True):
        # 公共库接入只读元数据（时长/人数/价格）；同 name_key 私有遮蔽公共。
        from app.tools.public_library import merge_with_shadow
        from app.tools.scripts import coerce_scripts

        private = (
            coerce_scripts(scripts)
            if scripts is not None
            else coerce_scripts(pd.DataFrame())
        )
        lookup_scripts = merge_with_shadow(private, public)
    specs, spec_warnings = _build_specs(frame, lookup_scripts)
    warnings.extend(spec_warnings)

    now_ts = _as_naive(now or pd.Timestamp.now())
    window_start = _as_naive(start_from) if start_from is not None else None
    window_end = _as_naive(start_to) if start_to is not None else None
    selected: list[SessionSpec] = []
    targets = {str(item) for item in target_session_ids} if target_session_ids else None
    decisions = (
        {str(item) for item in decision_session_ids}
        if decision_session_ids is not None
        else None
    )
    for spec in specs:
        if spec.status and spec.status != "待开场":
            continue
        if spec.start < now_ts - pd.Timedelta(minutes=1):
            continue
        if window_start is not None and spec.start < window_start:
            continue
        if window_end is not None and spec.start >= window_end:
            continue
        if targets is not None and spec.session_id not in targets:
            continue
        selected.append(spec)

    if not selected:
        return ScheduleResult(
            assignments=[],
            shortages=[],
            warnings=warnings + ["所选范围内没有待开场场次。"],
            optimal=True,
            stats={
                "nodes": 0,
                "clusters": 0,
                "elapsed_ms": int((time.perf_counter() - started) * 1000),
                "search_truncated": False,
            },
            as_of=now_ts.strftime("%Y-%m-%d %H:%M"),
            booked_revenue_at_risk=0.0,
            full_house_value=0.0,
        )

    room_specs = _build_rooms(rooms)
    dm_specs = _build_dms(dms)
    shift_specs = _build_shifts(shifts)
    rooms_by_id = {item.room_id: item for item in room_specs}
    dms_by_id = {item.dm_id: item for item in dm_specs}
    has_shifts = {item.dm_id for item in shift_specs if item.active}
    if dm_specs and not has_shifts:
        warnings.append("所有 DM 都没有班次记录，按全天可排处理。")

    lock_existing = True if lock_existing else False
    selected = _attach_assignments(
        selected,
        assignments,
        lock_existing=lock_existing,
        warnings=warnings,
    )

    occupancy, locked = _locked_occupancy(
        selected,
        rooms_by_id,
        dms_by_id,
        params,
        warnings,
    )

    candidates: dict[str, list[_Candidate]] = {}
    for spec in selected:
        if spec.locked:
            continue
        if decisions is not None and spec.session_id not in decisions:
            continue
        pairs = _candidate_pairs(
            spec,
            room_specs,
            dm_specs,
            shift_specs,
            params,
            has_shifts=has_shifts,
            leave_days=leave_days,
            familiarities=familiarities,
        )
        candidates[spec.session_id] = pairs
        if not pairs:
            active_rooms = [room for room in room_specs if room.active]
            if spec.total_seats is None and spec.booked_seats <= 0:
                warnings.append(
                    f"{spec.session_id} 缺人数，未自动排房；请先补人数。"
                )
            elif not active_rooms:
                warnings.append(
                    f"{spec.session_id} 没有可用房间，场次已保留但未排房。"
                )
            elif all(room.capacity is None for room in active_rooms):
                warnings.append(
                    f"{spec.session_id} 的房间都缺容量，未自动排房；请到资源页补容量。"
                )
            elif not any(dm.active for dm in dm_specs):
                warnings.append(f"{spec.session_id} 没有可用 DM，场次已保留但未排 DM。")

    decision_specs = (
        [
            spec
            for spec in selected
            if not spec.locked
            and (decisions is None or spec.session_id in decisions)
        ]
    )
    components = _build_components(
        decision_specs,
        room_specs,
        params,
        group_by_day=any(dm.max_sessions_per_day is not None for dm in dm_specs),
    )
    node_budget = [params.max_search_nodes]
    chosen: dict[str, _Candidate] = {}
    truncated = False
    for component in components:
        solved, complete = _solve_component(
            component,
            candidates,
            occupancy,
            locked,
            params,
            node_budget,
        )
        chosen.update(solved)
        if not complete:
            truncated = True
        if node_budget[0] <= 0:
            truncated = True
            break

    specs_by_id = {spec.session_id: spec for spec in selected}
    assigned: dict[str, _Candidate] = dict(locked)
    assigned.update(chosen)
    final_occupancy = _Occupancy()
    for session_id, candidate in assigned.items():
        spec = specs_by_id[session_id]
        room_interval, dm_interval = _intervals_for(
            spec, candidate.room, candidate.dm, params
        )
        final_occupancy.add(
            candidate.room.room_id,
            candidate.dm.dm_id,
            room_interval,
            dm_interval,
            _business_day(spec.start),
        )
    assignment_list: list[AssignmentSpec] = []
    for session_id, candidate in sorted(
        assigned.items(),
        key=lambda item: (specs_by_id[item[0]].start, item[0]),
    ):
        spec = specs_by_id[session_id]
        assignment_list.append(
            AssignmentSpec(
                session_id=session_id,
                room_id=candidate.room.room_id,
                dm_id=candidate.dm.dm_id,
                room_name=candidate.room.name,
                dm_name=candidate.dm.name,
                source="manual" if spec.locked else "auto",
                contribution=candidate.contribution,
            )
        )
        # 没带过的本分给了 DM：提醒店长人工确认（WP13B）。
        if familiarities is not None and not spec.locked:
            name_key = normalize_script_name(spec.script_name).casefold()
            if familiarities.get(candidate.dm.dm_id, {}).get(name_key, 0) == 0:
                warnings.append(
                    f"DM {candidate.dm.name} 没带过《{spec.script_name}》，请注意。"
                )

    shortages: list[Shortage] = []
    booked_at_risk = 0.0
    full_house = 0.0
    for spec in selected:
        seats_for_value = spec.total_seats if spec.total_seats is not None else spec.booked_seats
        full_house += max(0, seats_for_value or 0) * max(0.0, spec.list_price)
        if spec.session_id in assigned:
            continue
        if decisions is not None and spec.session_id not in decisions:
            continue
        reason = _reason_for(
            spec,
            room_specs,
            dm_specs,
            shift_specs,
            has_shifts,
            params,
            occupancy,
        )
        if reason == "missing_total_seats" and spec.booked_seats > 0:
            reason = "resource_overlap"
        risk = max(0, spec.booked_seats) * max(0.0, spec.list_price)
        booked_at_risk += risk
        messages = {
            "no_active_room": "没有可用房间。",
            "missing_room_capacity": "房间都缺容量，无法自动匹配。",
            "room_capacity": "现有房间容量不足。",
            "no_active_dm": "没有可用 DM。",
            "dm_skill": "没有技能匹配的 DM。",
            "dm_unavailable": "没有班次覆盖该时段的 DM。",
            "missing_total_seats": "这场还没有人数，无法判断需要多大房间。",
            "resource_overlap": "该时段房间或 DM 已被占用。",
            "locked_conflict": "手工分配与现有排期冲突。",
        }
        options = _suggestions(
            spec,
            reason,
            room_specs,
            dm_specs,
            shift_specs,
            has_shifts,
            params,
            final_occupancy,
            specs_by_id,
            assigned,
            leave_days=leave_days,
            familiarities=familiarities,
        )
        shortages.append(
            Shortage(
                session_id=spec.session_id,
                script_name=spec.script_name,
                start=spec.start.strftime("%Y-%m-%d %H:%M"),
                total_seats=spec.total_seats,
                booked_seats=spec.booked_seats,
                reason=reason,
                message=messages.get(reason, "暂时排不开。"),
                options=options,
                booked_revenue_at_risk=risk,
            )
        )

    elapsed_ms = int((time.perf_counter() - started) * 1000)
    return ScheduleResult(
        assignments=assignment_list,
        shortages=shortages,
        warnings=list(dict.fromkeys(warnings)),
        optimal=not truncated,
        stats={
            "nodes": params.max_search_nodes - max(0, node_budget[0]),
            "clusters": len(components),
            "elapsed_ms": elapsed_ms,
            "search_truncated": truncated,
            "fill_ratio": params.fill_ratio,
        },
        as_of=now_ts.strftime("%Y-%m-%d %H:%M"),
        booked_revenue_at_risk=booked_at_risk,
        full_house_value=full_house,
    )


__all__ = [
    "AssignmentSpec",
    "DEFAULT_DURATION_MINUTES",
    "DMSpec",
    "RoomSpec",
    "ScheduleParams",
    "ScheduleResult",
    "SessionSpec",
    "ShiftSpec",
    "Shortage",
    "Suggestion",
    "build_schedule",
]

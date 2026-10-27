"""演示资源与阶段模板。

只在新租户或资源页显式点击“载入演示资源”时使用。已有资源不会被覆盖。
"""

from __future__ import annotations

from typing import Any

from app.tools.assignment import auto_assign
from app.tools.resources import (
    load_dms,
    load_rooms,
    replace_dm_shifts,
    upsert_dms,
    upsert_rooms,
)
from app.tools.scripts import load_scripts
from app.tools.stages import apply_sample_stages, get_stages

DEMO_ROOMS: tuple[dict[str, Any], ...] = (
    {
        "name": "A 房",
        "capacity": 6,
        "tags": "推理/小房",
        "hourly_cost": 0,
        "buffer_minutes": 15,
        "notes": "演示房间",
    },
    {
        "name": "B 房",
        "capacity": 8,
        "tags": "情感/大房",
        "hourly_cost": 0,
        "buffer_minutes": 15,
        "notes": "演示房间",
    },
    {
        "name": "C 房",
        "capacity": 10,
        "tags": "机制/大厅",
        "hourly_cost": 0,
        "buffer_minutes": 20,
        "notes": "演示房间",
    },
)

DEMO_DMS: tuple[dict[str, Any], ...] = (
    {
        "name": "小北",
        "skills": "推理/机制",
        "max_sessions_per_day": 3,
        "notes": "演示 DM",
    },
    {
        "name": "阿岚",
        "skills": "情感/恐怖",
        "max_sessions_per_day": 3,
        "notes": "演示 DM",
    },
    {
        "name": "小满",
        "skills": "情感/欢乐/机制",
        "max_sessions_per_day": 3,
        "notes": "演示 DM",
    },
)

# 门店常见的 20:00 场，4 小时本会跨到次日 00:30；22:00 场加翻台/休息缓冲
# 会到次日 02:30。班次必须覆盖到凌晨，否则演示种子自己的晚间场都排不出去。
# 跨夜拆成当天 12:00–24:00 与次日 00:00–04:00 两行（weekday 仍按自然日）。
DEMO_SHIFTS: tuple[dict[str, Any], ...] = tuple(
    shift
    for weekday in range(7)
    for shift in (
        {"weekday": weekday, "start": "12:00", "end": "24:00"},
        {"weekday": weekday, "start": "00:00", "end": "04:00"},
    )
)


def _resources_empty(store: Any) -> bool:
    rooms = load_rooms(store=store)
    dms = load_dms(store=store)
    return rooms.empty and dms.empty


def seed_demo_resources(
    store: Any,
    *,
    now: Any = None,
    only_if_empty: bool = True,
    assign: bool = True,
) -> dict[str, Any]:
    """写入演示房间、DM 和班次；默认只在资源为空时执行。"""
    if only_if_empty and not _resources_empty(store):
        return {
            "seeded": False,
            "reason": "resources_exist",
            "message": "已有房间或 DM，未覆盖。",
            "rooms": len(load_rooms(store=store)),
            "dms": len(load_dms(store=store)),
        }

    with store.write_lock():
        rooms, room_warnings = upsert_rooms(list(DEMO_ROOMS), store=store, now=now)
        dms, dm_warnings = upsert_dms(list(DEMO_DMS), store=store, now=now)
        for dm in dms.to_dict("records"):
            if str(dm.get("name")) in {item["name"] for item in DEMO_DMS}:
                replace_dm_shifts(
                    str(dm["dm_id"]), list(DEMO_SHIFTS), store=store, now=now
                )
        assigned = None
        if assign:
            assigned = auto_assign(store, now=now, trigger="demo_seed")

    return {
        "seeded": True,
        "reason": None,
        "rooms": len(rooms),
        "dms": len(dms),
        "warnings": list(room_warnings) + list(dm_warnings),
        "auto_assign": assigned,
    }


def seed_demo_stages(
    store: Any,
    *,
    now: Any = None,
    only_missing: bool = True,
) -> dict[str, Any]:
    """给没有模板的演示剧本套用示例阶段。"""
    scripts = load_scripts(store=store)
    applied: list[dict[str, Any]] = []
    skipped: list[str] = []
    if scripts.empty:
        return {"seeded": False, "applied": [], "skipped": [], "message": "还没有剧本档案。"}
    with store.write_lock():
        for row in scripts.to_dict("records"):
            script_id = str(row.get("script_id") or "")
            script_name = str(row.get("script_name") or "")
            if not script_id:
                continue
            existing = get_stages(store, script_id=script_id)
            if only_missing and existing.get("stages"):
                skipped.append(script_name)
                continue
            result = apply_sample_stages(store, script_id=script_id, now=now)
            applied.append(
                {
                    "script_id": script_id,
                    "script_name": script_name,
                    "version": result.get("version"),
                    "stages": len(result.get("stages") or []),
                }
            )
    return {"seeded": bool(applied), "applied": applied, "skipped": skipped}


def seed_demo_store(store: Any, *, now: Any = None) -> dict[str, Any]:
    """新租户播种：资源、班次、模板、演示分配。"""
    resources = seed_demo_resources(store, now=now, only_if_empty=True, assign=True)
    stages = seed_demo_stages(store, now=now, only_missing=True)
    return {"resources": resources, "stages": stages}


__all__ = [
    "DEMO_DMS",
    "DEMO_ROOMS",
    "DEMO_SHIFTS",
    "seed_demo_resources",
    "seed_demo_stages",
    "seed_demo_store",
]

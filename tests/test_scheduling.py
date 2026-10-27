"""固定场次排期引擎的纯函数测试。

不读写数据库、不联网：所有断言都直接构造 DataFrame，验证硬约束、
确定性、目标函数和“如实标记最优性”。
"""

from __future__ import annotations

import pandas as pd

from app.tools.scheduling import ScheduleParams, build_schedule

NOW = pd.Timestamp("2026-10-20 12:00")
DAY = "2026-10-21"  # 周三


def session(
    session_id: str,
    *,
    start: str = f"{DAY} 19:00",
    script: str = "雾隐山庄",
    seats: int | None = 6,
    booked: int = 0,
    price: float | None = 168,
    script_type: str = "推理",
    status: str = "待开场",
) -> dict:
    return {
        "session_id": session_id,
        "store_name": "迷雾剧场",
        "script_name": script,
        "script_type": script_type,
        "start_time": start,
        "total_seats": seats,
        "booked_seats": booked,
        "list_price": price,
        "status": status,
    }


def room(
    room_id: str,
    *,
    capacity: int | None = 8,
    buffer: int = 15,
    active: bool = True,
    name: str | None = None,
    hourly_cost: float = 0,
) -> dict:
    return {
        "room_id": room_id,
        "name": name or room_id,
        "capacity": capacity,
        "buffer_minutes": buffer,
        "active": active,
        "hourly_cost": hourly_cost,
    }


def dm(
    dm_id: str,
    *,
    skills: str = "",
    active: bool = True,
    max_sessions: int | None = None,
    name: str | None = None,
    hourly_cost: float = 0,
) -> dict:
    return {
        "dm_id": dm_id,
        "name": name or dm_id,
        "skills": skills,
        "active": active,
        "max_sessions_per_day": max_sessions,
        "hourly_cost": hourly_cost,
    }


def shift(dm_id: str, start: int, end: int, *, weekday: int = 2) -> dict:
    return {
        "dm_id": dm_id,
        "weekday": weekday,
        "start_minute": start,
        "end_minute": end,
        "active": True,
    }


def schedule(sessions, rooms=None, dms=None, shifts=None, assignments=None, **kwargs):
    return build_schedule(
        sessions=pd.DataFrame(sessions),
        rooms=pd.DataFrame(rooms) if rooms is not None else None,
        dms=pd.DataFrame(dms) if dms is not None else None,
        shifts=pd.DataFrame(shifts) if shifts is not None else None,
        assignments=pd.DataFrame(assignments) if assignments is not None else None,
        now=NOW,
        **kwargs,
    )


def by_session(result):
    return {item.session_id: item for item in result.assignments}


# ---------------------------------------------------------------------------
# 基本分配与硬约束
# ---------------------------------------------------------------------------


def test_two_sessions_get_distinct_rooms_and_dms():
    result = schedule(
        [session("s-a"), session("s-b")],
        [room("r1"), room("r2")],
        [dm("d1"), dm("d2")],
    )
    assert result.optimal is True
    assert not result.shortages
    assigned = by_session(result)
    assert set(assigned) == {"s-a", "s-b"}
    assert assigned["s-a"].room_id != assigned["s-b"].room_id
    assert assigned["s-a"].dm_id != assigned["s-b"].dm_id
    assert all(item.source == "auto" for item in result.assignments)


def test_room_capacity_is_a_hard_constraint():
    result = schedule(
        [session("s-a", seats=8)],
        [room("r1", capacity=6)],
        [dm("d1")],
    )
    assert not result.assignments
    shortage = result.shortages[0]
    assert shortage.reason == "room_capacity"
    assert shortage.booked_revenue_at_risk == 0
    kinds = [option.kind for option in shortage.options]
    assert "add_resource" in kinds
    assert "keep_unassigned" in kinds


def test_missing_room_capacity_is_not_guessed():
    result = schedule(
        [session("s-a", seats=6)],
        [room("r1", capacity=None)],
        [dm("d1")],
    )
    assert not result.assignments
    assert result.shortages[0].reason == "missing_room_capacity"


def test_missing_total_seats_blocks_auto_room():
    result = schedule(
        [session("s-a", seats=None, booked=0)],
        [room("r1", capacity=8)],
        [dm("d1")],
    )
    assert not result.assignments
    assert result.shortages[0].reason == "missing_total_seats"
    assert any("缺人数" in warning for warning in result.warnings)


def test_missing_total_seats_uses_booked_as_floor():
    result = schedule(
        [session("s-a", seats=None, booked=4)],
        [room("r1", capacity=4)],
        [dm("d1")],
    )
    assert by_session(result)["s-a"].room_id == "r1"
    assert any("按已约 4 人" in warning for warning in result.warnings)


def test_room_buffer_prevents_overlap_but_allows_adjacent():
    sessions = [session("s-a", start=f"{DAY} 19:00"), session("s-b", start=f"{DAY} 23:15")]
    assigned = schedule(
        sessions,
        [room("r1", buffer=15)],
        [dm("d1"), dm("d2")],
    )
    assert set(by_session(assigned)) == {"s-a", "s-b"}

    tight = schedule(
        sessions,
        [room("r1", buffer=30)],
        [dm("d1"), dm("d2")],
    )
    # 同一间房翻台不够，只能排进一场；具体保住哪场由目标函数决定。
    assert len(by_session(tight)) == 1
    assert tight.shortages[0].reason == "resource_overlap"


def test_dm_rest_buffer_prevents_back_to_back():
    sessions = [session("s-a", start=f"{DAY} 19:00"), session("s-b", start=f"{DAY} 23:15")]
    tight = schedule(sessions, [room("r1"), room("r2")], [dm("d1")])
    # DM 休息缓冲不够，只能带一场。
    assert len(by_session(tight)) == 1
    assert tight.shortages[0].reason == "resource_overlap"

    ok = schedule(
        [session("s-a", start=f"{DAY} 19:00"), session("s-b", start=f"{DAY} 23:30")],
        [room("r1"), room("r2")],
        [dm("d1")],
    )
    assert set(by_session(ok)) == {"s-a", "s-b"}


def test_dm_skill_mismatch_is_rejected():
    result = schedule(
        [session("s-a", script_type="推理")],
        [room("r1")],
        [dm("d1", skills="情感")],
    )
    assert not result.assignments
    assert result.shortages[0].reason == "dm_skill"

    matched = schedule(
        [session("s-a", script_type="推理")],
        [room("r1")],
        [dm("d1", skills="情感/推理")],
    )
    assert by_session(matched)["s-a"].dm_id == "d1"


def test_empty_skills_are_treated_as_unrestricted():
    result = schedule(
        [session("s-a", script_type="机制")],
        [room("r1")],
        [dm("d1", skills="")],
    )
    assert by_session(result)["s-a"].dm_id == "d1"


def test_shift_must_cover_session_plus_rest():
    result = schedule(
        [session("s-a", start=f"{DAY} 19:00")],
        [room("r1")],
        [dm("d1")],
        shifts=[shift("d1", 18 * 60, 21 * 60)],
    )
    assert not result.assignments
    assert result.shortages[0].reason == "dm_unavailable"

    covered = schedule(
        [session("s-a", start=f"{DAY} 19:00")],
        [room("r1")],
        [dm("d1")],
        shifts=[shift("d1", 18 * 60, 23 * 60 + 30)],
    )
    assert by_session(covered)["s-a"].dm_id == "d1"


def test_dm_without_shifts_is_all_day_with_warning():
    result = schedule(
        [session("s-a")],
        [room("r1")],
        [dm("d1")],
    )
    assert by_session(result)["s-a"].dm_id == "d1"
    assert any("没有班次" in warning for warning in result.warnings)


def test_inactive_resources_are_not_used():
    no_room = schedule(
        [session("s-a")],
        [room("r1", active=False)],
        [dm("d1")],
    )
    assert not no_room.assignments
    assert no_room.shortages[0].reason == "no_active_room"

    no_dm = schedule(
        [session("s-a")],
        [room("r1")],
        [dm("d1", active=False)],
    )
    assert not no_dm.assignments
    assert no_dm.shortages[0].reason == "no_active_dm"


def test_daily_session_cap_is_enforced():
    result = schedule(
        [
            session("s-a", start=f"{DAY} 10:00"),
            session("s-b", start=f"{DAY} 19:00"),
        ],
        [room("r1"), room("r2")],
        [dm("d1", max_sessions=1)],
    )
    assert len(result.assignments) == 1
    assert result.shortages
    assert result.shortages[0].reason == "resource_overlap"


# ---------------------------------------------------------------------------
# 目标函数与确定性
# ---------------------------------------------------------------------------


def test_prefers_higher_contribution_when_resources_are_scarce():
    result = schedule(
        [
            session("s-cheap", price=100, seats=6),
            session("s-rich", price=300, seats=6),
        ],
        [room("r1")],
        [dm("d1")],
    )
    assert [item.session_id for item in result.assignments] == ["s-rich"]
    assert result.shortages[0].session_id == "s-cheap"


def test_multiple_assignments_are_chosen_when_feasible():
    # 三场不冲突且资源够：目标先最大化场次数，三场都应落位。
    result = schedule(
        [
            session("s-rich", start=f"{DAY} 19:00", price=1000),
            session("s-low-a", start=f"{DAY} 10:00", price=10),
            session("s-low-b", start=f"{DAY} 10:00", price=10),
        ],
        [room("r1"), room("r2")],
        [dm("d1"), dm("d2")],
    )
    assert set(by_session(result)) == {"s-low-a", "s-low-b", "s-rich"}


def test_result_is_deterministic():
    kwargs = dict(
        sessions=[session("s-a"), session("s-b")],
        rooms=[room("r1"), room("r2")],
        dms=[dm("d1"), dm("d2")],
    )
    first = schedule(**kwargs).to_dict()
    second = schedule(**kwargs).to_dict()
    first.pop("as_of")
    second.pop("as_of")
    for payload in (first, second):
        payload["stats"].pop("elapsed_ms", None)
    assert first == second


def test_node_limit_marks_optimal_false():
    result = schedule(
        [session("s-a"), session("s-b")],
        [room("r1"), room("r2")],
        [dm("d1"), dm("d2")],
        params=ScheduleParams(max_search_nodes=1),
    )
    assert result.optimal is False
    assert result.search_truncated is True


# ---------------------------------------------------------------------------
# 剧本时长与锁定分配
# ---------------------------------------------------------------------------


def test_script_alias_duration_is_used():
    scripts = pd.DataFrame(
        [
            {
                "script_id": "script-a",
                "script_name": "雾隐山庄",
                "aliases": "WYSZ",
                "duration_minutes": 180,
            }
        ]
    )
    result = schedule(
        [session("s-a", script="WYSZ", start=f"{DAY} 19:00")],
        [room("r1")],
        [dm("d1")],
        shifts=[shift("d1", 18 * 60, 22 * 60 + 30)],
        scripts=scripts,
    )
    assert by_session(result)["s-a"].dm_id == "d1"
    assert not any("默认 240" in warning for warning in result.warnings)


def test_missing_script_duration_falls_back_with_warning():
    result = schedule(
        [session("s-a", script="不存在的本")],
        [room("r1")],
        [dm("d1")],
    )
    assert by_session(result)["s-a"]
    assert any("默认 240" in warning for warning in result.warnings)


def test_manual_assignment_is_locked_and_kept():
    assignments = pd.DataFrame(
        [
            {
                "session_id": "s-a",
                "resource_kind": "room",
                "resource_id": "r1",
                "source": "manual",
            },
            {
                "session_id": "s-a",
                "resource_kind": "dm",
                "resource_id": "d1",
                "source": "manual",
            },
        ]
    )
    result = schedule(
        [session("s-a"), session("s-b")],
        [room("r1"), room("r2")],
        [dm("d1"), dm("d2")],
        assignments=assignments,
    )
    assigned = by_session(result)
    assert assigned["s-a"].room_id == "r1"
    assert assigned["s-a"].dm_id == "d1"
    assert assigned["s-a"].source == "manual"
    assert assigned["s-b"].source == "auto"


def test_no_sessions_returns_empty_result():
    result = build_schedule(sessions=pd.DataFrame(), now=NOW)
    assert result.assignments == []
    assert result.shortages == []
    assert result.optimal is True
    assert any("没有可排期" in warning for warning in result.warnings)


# ---------------------------------------------------------------------------
# 公共库接入：时长来源与私有遮蔽
# ---------------------------------------------------------------------------


def _public_frame(*rows):
    from app.tools.public_library import coerce_public_scripts, make_public_script_id

    records = []
    for row in rows:
        records.append(
            {
                "script_id": make_public_script_id(row["script_name"]),
                "script_name": row["script_name"],
                "script_type": row.get("script_type"),
                "total_seats": row.get("total_seats"),
                "duration_minutes": row.get("duration_minutes"),
                "list_price": row.get("list_price"),
                "notes": "",
                "source_text": "",
                "created_at": NOW,
                "updated_at": NOW,
                "origin": "策展",
                "imported_at": NOW,
            }
        )
    return coerce_public_scripts(pd.DataFrame(records))


def _bare_session(script):
    return session("s-1", script=script, seats=6, price=None, script_type=None)


def _resources():
    return [room("r-1")], [dm("d-1", skills="")], [shift("d-1", 9 * 60, 23 * 60)]


def test_public_duration_suppresses_default_fallback_warning():
    rooms, dms, shifts = _resources()
    public = _public_frame(
        {"script_name": "云使", "script_type": "推理", "total_seats": 6, "duration_minutes": 270}
    )
    result = schedule(
        [_bare_session("云使")],
        rooms=rooms,
        dms=dms,
        shifts=shifts,
        public=public,
    )
    assert not [w for w in result.warnings if "缺剧本时长" in w]


def test_missing_duration_without_public_still_warns():
    rooms, dms, shifts = _resources()
    result = schedule([_bare_session("查无此本")], rooms=rooms, dms=dms, shifts=shifts)
    assert any("缺剧本时长" in w for w in result.warnings)


def test_private_shadows_public_duration():
    """私有同名行（缺时长）遮蔽公共行（有时长）→ 仍按私有缺时长告警。"""
    from app.tools.scripts import coerce_scripts, make_script_id

    rooms, dms, shifts = _resources()
    private = coerce_scripts(
        pd.DataFrame(
            [
                {
                    "script_id": make_script_id("雾隐山庄"),
                    "script_name": "雾隐山庄",
                    "script_type": "推理",
                    "total_seats": 6,
                    "duration_minutes": None,
                    "list_price": None,
                    "notes": "",
                    "source_text": "",
                    "created_at": NOW,
                    "updated_at": NOW,
                }
            ]
        )
    )
    public = _public_frame(
        {"script_name": "雾隐山庄", "script_type": "情感", "total_seats": 7, "duration_minutes": 500}
    )
    result = schedule(
        [_bare_session("雾隐山庄")],
        rooms=rooms,
        dms=dms,
        shifts=shifts,
        scripts=private,
        public=public,
    )
    assert any("缺剧本时长" in w for w in result.warnings)


# ---------------------------------------------------------------------------
# WP13B：请假过滤 + 熟悉度次级键 + 没带过提醒
# ---------------------------------------------------------------------------


def test_leave_blocks_dm_on_that_day():
    result = schedule(
        [session("s1")],
        rooms=[room("r1", capacity=8)],
        dms=[dm("dm1"), dm("dm2")],
        dm_leaves=[{"dm_id": "dm1", "day": DAY}],
    )
    assigned = by_session(result)
    # dm1 请假那天不参与；dm2 正常顶上。
    assert assigned["s1"].dm_id == "dm2"


def test_leave_on_other_day_does_not_block():
    result = schedule(
        [session("s1")],
        rooms=[room("r1", capacity=8)],
        dms=[dm("dm1"), dm("dm2")],
        dm_leaves=[{"dm_id": "dm1", "day": "2026-10-22"}],
    )
    # 次日请假不影响当天；dm1 仍是首选（id 排序在前）。
    assert by_session(result)["s1"].dm_id == "dm1"


def test_familiarity_breaks_tie_at_equal_contribution():
    # 两个 DM 各项成本相同 → 贡献相同；带过《雾隐山庄》的 dm2 应优先。
    result = schedule(
        [session("s1")],
        rooms=[room("r1", capacity=8)],
        dms=[dm("dm1"), dm("dm2")],
        familiarities={"dm2": {"雾隐山庄": 3}},
    )
    assert by_session(result)["s1"].dm_id == "dm2"


def test_familiarity_does_not_overturn_better_contribution():
    # 技能不进目标函数（只做资格过滤）；用真实成本差拉开收益：
    # dm2 时薪更高 → 该组合贡献更低；即使 dm2 带过本也不翻盘。
    result = schedule(
        [session("s1")],
        rooms=[room("r1", capacity=8)],
        dms=[dm("dm1", hourly_cost=0), dm("dm2", hourly_cost=100)],
        familiarities={"dm2": {"雾隐山庄": 5}},
    )
    assert by_session(result)["s1"].dm_id == "dm1"


def test_no_familiarities_kwarg_means_no_warning():
    result = schedule(
        [session("s1")],
        rooms=[room("r1", capacity=8)],
        dms=[dm("dm1")],
    )
    assert not any("没带过" in warning for warning in result.warnings)


def test_unfamiliar_assignment_warns():
    result = schedule(
        [session("s1")],
        rooms=[room("r1", capacity=8)],
        dms=[dm("dm1")],
        familiarities={},  # 显式传空表：dm1 没带过 → 提醒
    )
    assert any(
        "dm1 没带过《雾隐山庄》" in warning for warning in result.warnings
    )

"""房间 / DM / 班次 / 分配工具的单元测试。

同一组断言分别跑 ExcelStore 和 SqlStore，避免两种存储后端语义漂移。
"""

from __future__ import annotations

import pandas as pd
import pytest

from app.store.engine import create_database_engine, ensure_schema
from app.store.excel import ExcelStore
from app.store.sql import SqlStore
from app.tools.demo import seed_demo_resources
from app.tools.resources import (
    empty_dms,
    empty_rooms,
    load_assignments,
    load_dm_shifts,
    load_dms,
    load_rooms,
    parse_minute,
    parse_weekday,
    prune_orphan_assignments,
    replace_assignments,
    replace_dm_shifts,
    setup_checklist,
    upsert_dms,
    upsert_rooms,
)
from app.tools.storage import coerce_sessions, upsert_sessions

NOW = pd.Timestamp("2026-10-20 12:00")


@pytest.fixture(params=["excel", "sql"])
def store(request, tmp_path):
    if request.param == "excel":
        yield ExcelStore(
            sessions_path=tmp_path / "sessions.xlsx",
            bookings_path=tmp_path / "bookings.xlsx",
            scripts_path=tmp_path / "scripts.xlsx",
        )
        return
    engine = create_database_engine(sqlite_path=tmp_path / "store.sqlite3")
    ensure_schema(engine)
    value = SqlStore(engine, "test-resources")
    value.ensure_tenant()
    yield value
    engine.dispose()


# ---------------------------------------------------------------------------
# 解析
# ---------------------------------------------------------------------------


def test_parse_weekday_accepts_numbers_and_chinese():
    assert parse_weekday(0) == 0
    assert parse_weekday(6) == 6
    assert parse_weekday("周一") == 0
    assert parse_weekday("周日") == 6
    assert parse_weekday("周天") == 6
    assert parse_weekday("5") == 5
    # 兼容把周日写成 7 的习惯：7 -> 6。
    assert parse_weekday(7) == 6
    with pytest.raises(ValueError):
        parse_weekday(8)
    with pytest.raises(ValueError):
        parse_weekday("周末")


def test_parse_minute_accepts_clock_and_chinese_forms():
    assert parse_minute("18:00") == 18 * 60
    assert parse_minute("18点") == 18 * 60
    assert parse_minute("18：30") == 18 * 60 + 30
    assert parse_minute(1080) == 1080
    assert parse_minute("24:00") == 1440
    with pytest.raises(ValueError):
        parse_minute("25:00")
    with pytest.raises(ValueError):
        parse_minute("晚八点")


# ---------------------------------------------------------------------------
# 房间
# ---------------------------------------------------------------------------


def test_upsert_room_creates_stable_id_and_defaults(store):
    frame, warnings = upsert_rooms(
        [{"name": "A 房", "capacity": 6, "hourly_cost": 40}],
        store=store,
        now=NOW,
    )
    assert warnings == []
    assert len(frame) == 1
    room = frame.iloc[0]
    assert room["room_id"].startswith("room-")
    assert room["capacity"] == 6
    assert room["buffer_minutes"] == 15
    assert bool(room["active"]) is True

    # 只按名称再次写入时更新同一行，不新建。
    frame, _ = upsert_rooms(
        [{"name": "A 房", "tags": "推理/大房"}],
        store=store,
        now=NOW,
    )
    assert len(frame) == 1
    assert frame.iloc[0]["capacity"] == 6
    assert frame.iloc[0]["tags"] == "推理/大房"


def test_upsert_room_empty_value_does_not_erase(store):
    frame, _ = upsert_rooms(
        [{"name": "B 房", "capacity": 8, "notes": "靠窗"}],
        store=store,
        now=NOW,
    )
    room_id = frame.iloc[0]["room_id"]

    frame, _ = upsert_rooms(
        [{"room_id": room_id, "name": "B 房", "capacity": None, "notes": ""}],
        store=store,
        now=NOW,
    )
    assert frame.iloc[0]["capacity"] == 8
    assert frame.iloc[0]["notes"] == "靠窗"


def test_upsert_room_can_deactivate(store):
    frame, _ = upsert_rooms([{"name": "C 房", "capacity": 4}], store=store, now=NOW)
    room_id = frame.iloc[0]["room_id"]

    frame, _ = upsert_rooms(
        [{"room_id": room_id, "name": "C 房", "active": False}],
        store=store,
        now=NOW,
    )
    assert bool(frame.iloc[0]["active"]) is False


def test_upsert_room_skips_rows_without_name(store):
    frame, warnings = upsert_rooms(
        [{"capacity": 6}, {"name": "D 房", "capacity": 5}],
        store=store,
        now=NOW,
    )
    assert len(frame) == 1
    assert frame.iloc[0]["name"] == "D 房"
    assert any("缺少房间名" in item for item in warnings)


# ---------------------------------------------------------------------------
# DM
# ---------------------------------------------------------------------------


def test_upsert_dm_is_idempotent_by_name(store):
    frame, _ = upsert_dms(
        [{"name": "小北", "skills": "推理/机制", "max_sessions_per_day": 3}],
        store=store,
        now=NOW,
    )
    assert len(frame) == 1
    assert frame.iloc[0]["dm_id"].startswith("dm-")
    assert frame.iloc[0]["max_sessions_per_day"] == 3

    frame, _ = upsert_dms(
        [{"name": "小北", "skills": "推理"}],
        store=store,
        now=NOW,
    )
    assert len(frame) == 1
    assert frame.iloc[0]["skills"] == "推理"
    assert frame.iloc[0]["max_sessions_per_day"] == 3


def test_upsert_dm_empty_does_not_clear_skills(store):
    frame, _ = upsert_dms([{"name": "阿岚", "skills": "情感"}], store=store, now=NOW)
    dm_id = frame.iloc[0]["dm_id"]
    frame, _ = upsert_dms(
        [{"dm_id": dm_id, "name": "阿岚", "skills": ""}], store=store, now=NOW
    )
    assert frame.iloc[0]["skills"] == "情感"


# ---------------------------------------------------------------------------
# 班次与分配
# ---------------------------------------------------------------------------


def test_replace_dm_shifts_only_touches_target_dm(store):
    dms, _ = upsert_dms(
        [{"name": "小北"}, {"name": "阿岚"}], store=store, now=NOW
    )
    xiaobei = dms[dms["name"] == "小北"].iloc[0]["dm_id"]
    alan = dms[dms["name"] == "阿岚"].iloc[0]["dm_id"]

    replace_dm_shifts(
        xiaobei,
        [{"weekday": "周一", "start": "18:00", "end": "23:00"}],
        store=store,
        now=NOW,
    )
    replace_dm_shifts(
        alan,
        [{"weekday": 5, "start_minute": 600, "end_minute": 1320}],
        store=store,
        now=NOW,
    )

    shifts = load_dm_shifts(store=store)
    assert len(shifts) == 2
    first = shifts[shifts["dm_id"] == xiaobei].iloc[0]
    assert first["weekday"] == 0
    assert first["start_minute"] == 1080
    assert first["end_minute"] == 1380

    # 再替换小北的班次，不应影响阿岚。
    replace_dm_shifts(
        xiaobei,
        [{"weekday": "周二", "start": "17:00", "end": "22:00"}],
        store=store,
        now=NOW,
    )
    shifts = load_dm_shifts(store=store)
    assert len(shifts) == 2
    assert shifts[shifts["dm_id"] == xiaobei].iloc[0]["weekday"] == 1
    assert shifts[shifts["dm_id"] == alan].iloc[0]["weekday"] == 5


def test_replace_dm_shifts_rejects_invalid_window(store):
    with pytest.raises(ValueError):
        replace_dm_shifts(
            "dm-x",
            [{"weekday": 0, "start": "22:00", "end": "20:00"}],
            store=store,
            now=NOW,
        )
    assert load_dm_shifts(store=store).empty


def test_replace_assignments_only_replaces_given_sessions(store):
    replace_assignments(
        ["s-1"],
        [
            {"session_id": "s-1", "resource_kind": "room", "resource_id": "room-a"},
            {"session_id": "s-1", "resource_kind": "dm", "resource_id": "dm-a"},
        ],
        store=store,
        now=NOW,
    )
    replace_assignments(
        ["s-2"],
        [{"session_id": "s-2", "resource_kind": "room", "resource_id": "room-b"}],
        store=store,
        now=NOW,
    )

    replace_assignments(
        ["s-1"],
        [{"session_id": "s-1", "resource_kind": "room", "resource_id": "room-c"}],
        store=store,
        now=NOW,
    )
    frame = load_assignments(store=store).sort_values(
        ["session_id", "resource_kind"]
    )
    rows = frame[["session_id", "resource_kind", "resource_id"]].to_dict("records")
    assert rows == [
        {"session_id": "s-1", "resource_kind": "room", "resource_id": "room-c"},
        {"session_id": "s-2", "resource_kind": "room", "resource_id": "room-b"},
    ]


def test_replace_assignments_deduplicates_identical_rows(store):
    frame = replace_assignments(
        ["s-1"],
        [
            {"session_id": "s-1", "resource_kind": "room", "resource_id": "room-a"},
            {"session_id": "s-1", "resource_kind": "room", "resource_id": "room-a"},
        ],
        store=store,
        now=NOW,
    )
    assert len(frame) == 1
    assert frame.iloc[0]["role"] == "main"
    assert frame.iloc[0]["source"] == "auto"


def test_replace_assignments_rejects_bad_kind(store):
    with pytest.raises(ValueError):
        replace_assignments(
            ["s-1"],
            [{"session_id": "s-1", "resource_kind": "chef", "resource_id": "x"}],
            store=store,
            now=NOW,
        )


def test_demo_shifts_cover_late_session(store):
    # 20:00 开场的 4 小时本是门店最常见场次，结束会被翻台/休息缓冲
    # 推到次日 00:30；22:00 场会到 02:30。演示班次必须覆盖跨夜，
    # 否则种子演示场也排不出去这些晚间场。
    upsert_sessions(
        [
            {
                "store_name": "迷雾剧场",
                "script_name": "雾隐山庄",
                "start_time": "2026-10-21 20:00",
                "total_seats": 6,
                "status": "待开场",
            },
            {
                "store_name": "迷雾剧场",
                "script_name": "年轮",
                "start_time": "2026-10-21 22:00",
                "total_seats": 6,
                "status": "待开场",
            },
        ],
        store=store,
    )
    result = seed_demo_resources(store, now=NOW)
    assert result["seeded"] is True

    assignments = load_assignments(store=store)
    assert not assignments.empty
    assert set(assignments["resource_kind"]) == {"room", "dm"}
    assert result["auto_assign"]["status"] == "assigned"
    # 两场晚间场都要真的排上，而不是只排其中一场。
    assert len(set(assignments["session_id"])) == 2


def test_prune_orphan_assignments_keeps_known_sessions():
    sessions = coerce_sessions(
        pd.DataFrame(
            [
                {
                    "session_id": "s-1",
                    "store_name": "迷雾剧场",
                    "script_name": "雾隐山庄",
                    "start_time": "2026-10-21 19:00",
                    "status": "待开场",
                }
            ]
        )
    )
    assignments = pd.DataFrame(
        [
            {
                "assignment_id": "a-1",
                "session_id": "s-1",
                "resource_kind": "room",
                "resource_id": "room-a",
                "role": "main",
                "source": "auto",
            },
            {
                "assignment_id": "a-2",
                "session_id": "s-gone",
                "resource_kind": "room",
                "resource_id": "room-b",
                "role": "main",
                "source": "auto",
            },
        ]
    )
    pruned = prune_orphan_assignments(sessions, assignments)
    assert pruned["session_id"].tolist() == ["s-1"]


# ---------------------------------------------------------------------------
# 缺口清单
# ---------------------------------------------------------------------------


def test_setup_checklist_reports_counts_and_gaps():
    from app.tools.resources import coerce_dms, coerce_rooms

    rooms = coerce_rooms(
        pd.DataFrame(
            [
                {"room_id": "room-a", "name": "A 房", "capacity": 6, "active": True},
                {"room_id": "room-b", "name": "B 房", "capacity": None, "active": True},
                {"room_id": "room-c", "name": "C 房", "capacity": 8, "active": False},
            ]
        )
    )
    dms = coerce_dms(
        pd.DataFrame(
            [
                {"dm_id": "dm-a", "name": "小北", "skills": "", "active": True},
                {"dm_id": "dm-b", "name": "阿岚", "skills": "情感", "active": False},
            ]
        )
    )
    shifts = pd.DataFrame(
        [
            {
                "shift_id": "shift-a",
                "dm_id": "dm-a",
                "weekday": 0,
                "start_minute": 1080,
                "end_minute": 1380,
                "active": True,
            }
        ]
    )
    scripts = pd.DataFrame(
        [
            {
                "script_id": "script-a",
                "script_name": "雾隐山庄",
                "duration_minutes": 240,
            },
            {
                "script_id": "script-b",
                "script_name": "年轮",
                "duration_minutes": None,
            },
        ]
    )
    stages = pd.DataFrame(
        [{"script_id": "script-a", "stage_id": "stage-1", "seq": 1}]
    )

    checklist = setup_checklist(
        rooms=rooms, dms=dms, shifts=shifts, scripts=scripts, stages=stages
    )
    assert checklist["rooms"] == 3
    assert checklist["active_rooms"] == 2
    assert checklist["dms"] == 2
    assert checklist["active_dms"] == 1
    assert checklist["room_missing_capacity"] == 1
    assert checklist["dm_missing_skills"] == 1
    assert checklist["dm_missing_shifts"] == 0
    assert checklist["script_missing_duration"] == 1
    assert checklist["scripts_without_stages"] == 1
    codes = {item["code"] for item in checklist["items"]}
    assert {
        "room_capacity_missing",
        "dm_skills_missing",
        "script_duration_missing",
        "stages_missing",
    } <= codes


def test_setup_checklist_flags_missing_resources():
    checklist = setup_checklist(
        rooms=empty_rooms(),
        dms=empty_dms(),
        shifts=pd.DataFrame(),
        scripts=pd.DataFrame(),
        stages=pd.DataFrame(),
        sessions=pd.DataFrame(),
    )
    codes = {item["code"] for item in checklist["items"]}
    assert "no_rooms" in codes
    assert "no_dms" in codes


def test_loading_empty_resources_returns_stable_columns(store):
    assert len(load_rooms(store=store)) == 0
    assert len(load_dms(store=store)) == 0
    assert len(load_dm_shifts(store=store)) == 0
    assert len(load_assignments(store=store)) == 0


# ---------------------------------------------------------------------------
# WP13B：dm_leaves 读写 + 带本历史派生
# ---------------------------------------------------------------------------


def test_dm_leaves_in_all_tables():
    from app.store.schema import ALL_TABLES

    assert "dm_leaves" in [table.name for table in ALL_TABLES]


def test_dm_leave_roundtrip_and_idempotent_upsert(store):
    from app.tools.resources import load_dm_leaves, upsert_dm_leave

    first = upsert_dm_leave("dm1", "2026-10-21", note="事假", store=store, now=NOW)
    assert first["created"] is True

    frame = load_dm_leaves(store=store)
    assert len(frame) == 1
    assert frame.iloc[0]["leave_id"] == first["leave_id"]
    assert frame.iloc[0]["day"] == "2026-10-21"
    assert frame.iloc[0]["note"] == "事假"

    # 同 (dm_id, day) 再登记：幂等，只更新备注，不新增行。
    second = upsert_dm_leave("dm1", "2026-10-21", note="病假", store=store, now=NOW)
    assert second["created"] is False
    assert second["leave_id"] == first["leave_id"]

    frame = load_dm_leaves(store=store)
    assert len(frame) == 1
    assert frame.iloc[0]["note"] == "病假"

    # 不同日期互不影响。
    third = upsert_dm_leave("dm1", "2026-10-22", store=store, now=NOW)
    assert third["created"] is True
    assert len(load_dm_leaves(store=store)) == 2


def test_delete_dm_leave_is_idempotent(store):
    from app.tools.resources import delete_dm_leave, load_dm_leaves, upsert_dm_leave

    created = upsert_dm_leave("dm1", "2026-10-21", store=store, now=NOW)
    first = delete_dm_leave(created["leave_id"], store=store)
    assert first["removed"] is True
    assert load_dm_leaves(store=store).empty

    again = delete_dm_leave(created["leave_id"], store=store)
    assert again["removed"] is False


def test_dm_script_history_excludes_cancelled_sessions(store):
    from app.tools.resources import dm_script_history

    upsert_sessions(
        [
            {
                "session_id": "ses-live",
                "store_name": "迷雾剧场",
                "script_name": "雾隐山庄",
                "start_time": "2026-10-18 19:00",
                "status": "待开场",
            },
            {
                "session_id": "ses-cancel",
                "store_name": "迷雾剧场",
                "script_name": "长夜将明",
                "start_time": "2026-10-19 14:00",
                "status": "已取消",
            },
        ],
        store=store,
    )
    store.save_dm_runs(
        pd.DataFrame(
            [
                {
                    "run_id": "run-a",
                    "session_id": "ses-live",
                    "dm_id": "dm1",
                    "created_at": NOW,
                    "updated_at": NOW,
                },
                {
                    "run_id": "run-b",
                    "session_id": "ses-cancel",
                    "dm_id": "dm1",
                    "created_at": NOW,
                    "updated_at": NOW,
                },
            ]
        )
    )

    history = dm_script_history(store=store)
    bucket = history["dm1"]
    # 已取消场次不计。
    assert bucket["past_runs"] == 1
    assert bucket["distinct_scripts"] == 1
    assert bucket["recent"][0]["script_name"] == "雾隐山庄"
    from app.tools.scripts import normalize_script_name

    assert bucket["script_counts"][normalize_script_name("雾隐山庄")] == 1


def test_schedule_dm_inputs_wires_leaves_and_familiarities(store):
    from app.tools.resources import schedule_dm_inputs, upsert_dm_leave
    from app.tools.scripts import normalize_script_name

    upsert_dm_leave("dm1", "2026-10-21", store=store, now=NOW)
    upsert_sessions(
        [
            {
                "session_id": "ses-1",
                "store_name": "迷雾剧场",
                "script_name": "雾隐山庄",
                "start_time": "2026-10-18 19:00",
                "status": "待开场",
            }
        ],
        store=store,
    )
    store.save_dm_runs(
        pd.DataFrame(
            [
                {
                    "run_id": "run-1",
                    "session_id": "ses-1",
                    "dm_id": "dm1",
                    "created_at": NOW,
                    "updated_at": NOW,
                }
            ]
        )
    )

    leave_rows, familiarities = schedule_dm_inputs(store=store)
    assert len(leave_rows) == 1
    assert leave_rows[0]["dm_id"] == "dm1"
    assert familiarities["dm1"][normalize_script_name("雾隐山庄")] == 1

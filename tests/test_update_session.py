"""已保存场次的修正：重算 ID、搬移关联数据、失败不留半套。"""

from __future__ import annotations

import pandas as pd
import pytest

from app.store.engine import create_database_engine, ensure_schema
from app.store.excel import ExcelStore
from app.store.sql import SqlStore
from app.tools.resources import (
    load_assignments,
    load_dms,
    load_rooms,
    replace_assignments,
    upsert_dms,
    upsert_rooms,
)
from app.tools.session_intake import (
    SessionIntakeError,
    create_session_from_fields,
    update_session,
)
from app.tools.storage import (
    load_bookings,
    load_sessions,
    make_booking_id,
    make_session_id,
    upsert_booking,
)

NOW = pd.Timestamp("2026-10-20 12:00")
START = "2026-10-21 20:00"
NEW_START = "2026-10-21 21:00"


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
    value = SqlStore(engine, "test-update")
    value.ensure_tenant()
    yield value
    engine.dispose()


def promote(store, script="雾隐山庄", start=START, **extra):
    # 默认给明确人数，自动分配才有房间容量可校验。硬必填齐全直接建场。
    fields = {"script_name": script, "start_time": start, "total_seats": 6, **extra}
    result = create_session_from_fields(
        fields=fields,
        store=store,
        now=NOW,
    )
    assert result["status"] == "promoted"
    return result


def add_resources(store):
    upsert_rooms(
        [{"name": "A 房", "capacity": 8, "buffer_minutes": 15}],
        store=store,
        now=NOW,
    )
    upsert_dms([{"name": "小北", "skills": ""}], store=store, now=NOW)


def find_session(frame, session_id):
    return frame[frame["session_id"].astype("string").fillna("") == session_id]


# ---------------------------------------------------------------------------
# 原地更新
# ---------------------------------------------------------------------------


def test_seats_and_price_update_keeps_id(store):
    session_id = promote(store)["session"]["session_id"]
    result = update_session(
        session_id,
        fields={"total_seats": 7, "list_price": 199},
        store=store,
        now=NOW,
    )
    assert result["session_id_changed"] is False
    assert result["session"]["session_id"] == session_id
    assert result["session"]["total_seats"] == 7
    assert result["session"]["list_price"] == 199


def test_time_change_recalculates_id(store):
    old_id = promote(store)["session"]["session_id"]
    result = update_session(
        old_id, fields={"start_time": NEW_START}, store=store, now=NOW
    )
    new_id = result["session"]["session_id"]
    assert result["session_id_changed"] is True
    assert result["previous_session_id"] == old_id
    assert new_id == make_session_id("迷雾剧场", "雾隐山庄", NEW_START)

    sessions = load_sessions(store=store)
    assert len(sessions) == 1
    assert sessions.iloc[0]["session_id"] == new_id


def test_script_change_recalculates_id(store):
    old_id = promote(store)["session"]["session_id"]
    result = update_session(
        old_id, fields={"script_name": "年轮"}, store=store, now=NOW
    )
    assert result["session_id_changed"] is True
    assert result["session"]["script_name"] == "年轮"


def test_store_change_recalculates_id(store):
    old_id = promote(store)["session"]["session_id"]
    result = update_session(
        old_id, fields={"store_name": "星辰馆"}, store=store, now=NOW
    )
    assert result["session_id_changed"] is True
    assert result["session"]["store_name"] == "星辰馆"


# ---------------------------------------------------------------------------
# 关联数据搬移
# ---------------------------------------------------------------------------


def test_bookings_follow_the_new_session_id(store):
    old_id = promote(store)["session"]["session_id"]
    upsert_booking(old_id, "阿青", seats=2, now=NOW, store=store)

    result = update_session(
        old_id, fields={"start_time": NEW_START}, store=store, now=NOW
    )
    new_id = result["session"]["session_id"]

    bookings = load_bookings(store=store)
    assert len(bookings) == 1
    row = bookings.iloc[0]
    assert row["session_id"] == new_id
    assert row["booking_id"] == make_booking_id(new_id, "阿青")
    assert row["player_name"] == "阿青"


def test_assignments_follow_and_auto_is_recomputed(store):
    add_resources(store)
    old_id = promote(store)["session"]["session_id"]
    before = load_assignments(store=store)
    assert set(before["session_id"]) == {old_id}

    result = update_session(
        old_id, fields={"start_time": NEW_START}, store=store, now=NOW
    )
    new_id = result["session"]["session_id"]
    after = load_assignments(store=store)
    assert set(after["session_id"]) == {new_id}
    assert set(after["resource_kind"]) == {"room", "dm"}
    assert any("自动分配已清除" in warning for warning in result["warnings"])


def test_booking_refs_follow(store):
    old_id = promote(store)["session"]["session_id"]
    upsert_booking(old_id, "阿青", seats=2, now=NOW, store=store)
    booking_id = load_bookings(store=store).iloc[0]["booking_id"]

    store.save_booking_refs(
        pd.DataFrame(
            [
                {
                    "ref_id": "ref-1",
                    "source": "dianping",
                    "external_id": "DP-1",
                    "booking_id": booking_id,
                    "session_id": old_id,
                    "seats": 2,
                    "status": "active",
                    "data_as_of": NOW,
                    "imported_at": NOW,
                    "raw_ref": "脱敏摘要",
                }
            ]
        )
    )

    result = update_session(
        old_id, fields={"start_time": NEW_START}, store=store, now=NOW
    )
    new_id = result["session"]["session_id"]
    refs = store.load_booking_refs()
    assert refs.iloc[0]["session_id"] == new_id
    assert refs.iloc[0]["booking_id"] == make_booking_id(new_id, "阿青")


def test_completed_run_follows_but_active_run_blocks(store):
    old_id = promote(store)["session"]["session_id"]
    store.save_dm_runs(
        pd.DataFrame(
            [
                {
                    "run_id": "run-done",
                    "session_id": old_id,
                    "status": "completed",
                    "mode": "software",
                    "created_by": "tenant",
                    "created_at": NOW,
                    "updated_at": NOW,
                }
            ]
        )
    )
    update_session(old_id, fields={"start_time": NEW_START}, store=store, now=NOW)
    new_id = make_session_id("迷雾剧场", "雾隐山庄", NEW_START)
    runs = store.load_dm_runs()
    assert runs.iloc[0]["session_id"] == new_id

    # 再建一个进行中的 run，修改身份必须被拒绝。
    store.save_dm_runs(
        pd.DataFrame(
            [
                {
                    "run_id": "run-live",
                    "session_id": new_id,
                    "status": "active",
                    "mode": "software",
                    "created_by": "tenant",
                    "created_at": NOW,
                    "updated_at": NOW,
                }
            ]
        )
    )
    with pytest.raises(SessionIntakeError) as excinfo:
        update_session(
            new_id,
            fields={"start_time": "2026-10-21 22:00"},
            store=store,
            now=NOW,
        )
    assert excinfo.value.code == "run_active"

    sessions = load_sessions(store=store)
    assert sessions.iloc[0]["session_id"] == new_id
    assert set(sessions["session_id"]) == {new_id}


# ---------------------------------------------------------------------------
# 失败路径
# ---------------------------------------------------------------------------


def test_duplicate_target_is_rejected(store):
    first = promote(store, script="雾隐山庄")["session"]["session_id"]
    promote(store, script="年轮")
    # 把《雾隐山庄》改到《年轮》的身份会撞已有 ID。
    with pytest.raises(SessionIntakeError) as excinfo:
        update_session(
            first,
            fields={"script_name": "年轮"},
            store=store,
            now=NOW,
        )
    assert excinfo.value.code == "duplicate_session"
    assert len(load_sessions(store=store)) == 2


def test_capacity_below_booked_is_rejected(store):
    session_id = promote(store, total_seats=6)["session"]["session_id"]
    upsert_booking(session_id, "阿青", seats=4, now=NOW, store=store)

    with pytest.raises(SessionIntakeError) as excinfo:
        update_session(
            session_id,
            fields={"total_seats": 3},
            store=store,
            now=NOW,
        )
    assert excinfo.value.code == "capacity_exceeded"
    sessions = load_sessions(store=store)
    assert sessions.iloc[0]["total_seats"] == 6
    assert len(load_bookings(store=store)) == 1


def test_invalid_start_time_is_rejected(store):
    session_id = promote(store)["session"]["session_id"]
    with pytest.raises(SessionIntakeError) as excinfo:
        update_session(
            session_id,
            fields={"start_time": "下周三晚上"},
            store=store,
            now=NOW,
        )
    assert excinfo.value.code == "start_time_invalid"


def test_room_and_dm_fields_are_rejected(store):
    session_id = promote(store)["session"]["session_id"]
    with pytest.raises(SessionIntakeError) as excinfo:
        update_session(
            session_id,
            fields={"room_id": "room-a"},
            store=store,
            now=NOW,
        )
    assert excinfo.value.code == "room_not_allowed"


def test_unknown_session_is_rejected(store):
    with pytest.raises(SessionIntakeError) as excinfo:
        update_session(
            "s-missing",
            fields={"total_seats": 6},
            store=store,
            now=NOW,
        )
    assert excinfo.value.code == "session_not_found"


# ---------------------------------------------------------------------------
# 改期撞档预检（手工分配保留时不得静默双订）
# ---------------------------------------------------------------------------


def _manual_pair(store, *, kinds=("room",)):
    """B 场手工分配到 A 场同资源、时间重叠；返回 (a_id, b_id)。

    A 场保持自动分配即可——预检只看本场 manual，对方占用不分来源。
    B 场开场 23:00 与 A 场 20:00（240 分钟）重叠，转正时必然冲突、没有自动分配。
    """
    add_resources(store)
    a_id = promote(store, script="雾隐山庄", start=START)["session"]["session_id"]
    b_id = promote(store, script="年轮", start="2026-10-21 23:00")["session"][
        "session_id"
    ]
    room_id = load_rooms(store=store).iloc[0]["room_id"]
    dm_id = load_dms(store=store).iloc[0]["dm_id"]
    resource_of = {"room": room_id, "dm": dm_id}
    replace_assignments(
        [b_id],
        [
            {
                "session_id": b_id,
                "resource_kind": kind,
                "resource_id": resource_of[kind],
                "source": "manual",
            }
            for kind in kinds
        ],
        store=store,
        now=NOW,
    )
    return a_id, b_id


def test_manual_assignment_conflict_blocks_reschedule(store):
    a_id, b_id = _manual_pair(store, kinds=("room",))
    with pytest.raises(SessionIntakeError) as excinfo:
        update_session(
            b_id, fields={"start_time": "2026-10-21 21:00"}, store=store, now=NOW
        )
    assert excinfo.value.code == "assignment_conflict"
    message = str(excinfo.value)
    assert a_id in message
    assert "房间" in message
    # 场次未被改动。
    sessions = load_sessions(store=store)
    row = sessions[sessions["session_id"] == b_id].iloc[0]
    assert pd.Timestamp(row["start_time"]) == pd.Timestamp("2026-10-21 23:00")


def test_manual_dm_assignment_conflict_blocks_reschedule(store):
    a_id, b_id = _manual_pair(store, kinds=("dm",))
    with pytest.raises(SessionIntakeError) as excinfo:
        update_session(
            b_id, fields={"start_time": "2026-10-21 21:00"}, store=store, now=NOW
        )
    assert excinfo.value.code == "assignment_conflict"
    message = str(excinfo.value)
    assert a_id in message
    assert "DM" in message


def test_force_flag_allows_conflicting_reschedule(store):
    _a_id, b_id = _manual_pair(store, kinds=("room",))
    result = update_session(
        b_id,
        fields={"start_time": "2026-10-21 21:00", "__force__": True},
        store=store,
        now=NOW,
    )
    assert result["session_id_changed"] is True
    sessions = load_sessions(store=store)
    row = sessions[sessions["session_id"] == result["session"]["session_id"]].iloc[0]
    assert pd.Timestamp(row["start_time"]) == pd.Timestamp("2026-10-21 21:00")


def test_auto_assignments_do_not_trigger_precheck(store):
    add_resources(store)
    promote(store, script="雾隐山庄", start=START)
    b_id = promote(store, script="年轮", start="2026-10-22 10:00")["session"][
        "session_id"
    ]
    # 两场都是自动分配；改到与 A 重叠的时段不触发预检（auto 本就会被丢弃重算）。
    result = update_session(
        b_id, fields={"start_time": "2026-10-21 21:00"}, store=store, now=NOW
    )
    assert result["session_id_changed"] is True


def test_non_time_update_skips_precheck(store):
    _a_id, b_id = _manual_pair(store, kinds=("room", "dm"))
    # 手工分配在现时段已重叠（构造态），但只改价格不应触发预检。
    result = update_session(b_id, fields={"list_price": 188}, store=store, now=NOW)
    assert result["session"]["list_price"] == 188


def test_precheck_respects_room_buffer(store):
    _a_id, b_id = _manual_pair(store, kinds=("room",))
    # A 场占到 24:00，房缓冲 15 分钟 → 00:10 仍撞、00:20 放行。
    with pytest.raises(SessionIntakeError) as excinfo:
        update_session(
            b_id, fields={"start_time": "2026-10-22 00:10"}, store=store, now=NOW
        )
    assert excinfo.value.code == "assignment_conflict"
    result = update_session(
        b_id, fields={"start_time": "2026-10-22 00:20"}, store=store, now=NOW
    )
    assert result["session_id_changed"] is True


# ---------------------------------------------------------------------------
# WP11 状态看板：状态三态流转与非法状态拒绝
# ---------------------------------------------------------------------------


def test_status_transitions_between_three_states(store):
    """店长纠错权：待开场 ↔ 已开场，任意态可取消，已取消可拖回待开场。"""
    session_id = promote(store)["session"]["session_id"]

    started = update_session(
        session_id, fields={"status": "已开场"}, store=store, now=NOW
    )
    assert started["session_id_changed"] is False
    assert started["session"]["status"] == "已开场"

    back = update_session(
        session_id, fields={"status": "待开场"}, store=store, now=NOW
    )
    assert back["session"]["status"] == "待开场"

    cancelled = update_session(
        session_id, fields={"status": "已取消"}, store=store, now=NOW
    )
    assert cancelled["session"]["status"] == "已取消"

    restored = update_session(
        session_id, fields={"status": "待开场"}, store=store, now=NOW
    )
    assert restored["session"]["status"] == "待开场"

    # 身份字段没动过：始终是同一个场次。
    sessions = load_sessions(store=store)
    assert len(sessions) == 1
    assert sessions.iloc[0]["session_id"] == session_id


def test_invalid_status_is_rejected(store):
    session_id = promote(store)["session"]["session_id"]
    with pytest.raises(SessionIntakeError) as excinfo:
        update_session(
            session_id, fields={"status": "进行中"}, store=store, now=NOW
        )
    assert excinfo.value.code == "status_invalid"
    sessions = load_sessions(store=store)
    assert sessions.iloc[0]["status"] == "待开场"

"""建场（硬必填齐全当场 promote）后的自动分配、冲突返回与手动处理。"""

from __future__ import annotations

import pandas as pd
import pytest

from app.store.engine import create_database_engine, ensure_schema
from app.store.excel import ExcelStore
from app.store.sql import SqlStore
from app.tools.assignment import (
    auto_assign,
    buffered_overlap_hits,
    load_schedule_context,
    resolve_conflict,
)
from app.tools.resources import (
    load_assignments,
    replace_assignments,
    upsert_dms,
    upsert_rooms,
)
from app.tools.session_intake import create_session_from_fields
from app.tools.storage import load_sessions, upsert_sessions

NOW = pd.Timestamp("2026-10-20 12:00")
START = "2026-10-21 20:00"


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
    value = SqlStore(engine, "test-assign")
    value.ensure_tenant()
    yield value
    engine.dispose()


def add_resource(store, *, rooms=1, dms=1):
    upsert_rooms(
        [
            {"name": f"房{i + 1}", "capacity": 8, "buffer_minutes": 15}
            for i in range(rooms)
        ],
        store=store,
        now=NOW,
    )
    upsert_dms(
        [{"name": f"DM{i + 1}", "skills": ""} for i in range(dms)],
        store=store,
        now=NOW,
    )


def promote(store, script="雾隐山庄", start=START):
    # 硬必填（剧本全名 + 开场时间）齐全即当场建场；人数缺失时排期不会猜
    # 容量，因此这里给出明确人数。
    result = create_session_from_fields(
        fields={"script_name": script, "start_time": start, "total_seats": 6},
        store=store,
        now=NOW,
    )
    assert result["status"] == "promoted"
    return result


# ---------------------------------------------------------------------------
# 自动分配
# ---------------------------------------------------------------------------


def test_promotion_assigns_when_resources_exist(store):
    add_resource(store)
    result = promote(store)
    auto = result["auto_assign"]
    assert auto["status"] == "assigned"
    assert auto["conflicts"] == []
    assert len(auto["assigned"]) == 1

    assignments = load_assignments(store=store)
    session_id = result["session"]["session_id"]
    rows = assignments[assignments["session_id"] == session_id]
    assert set(rows["resource_kind"]) == {"room", "dm"}
    assert set(rows["source"]) == {"auto"}


def test_conflict_saves_session_but_no_assignment(store):
    add_resource(store, rooms=1, dms=1)
    promote(store, script="雾隐山庄")
    second = promote(store, script="年轮")

    assert second["auto_assign"]["status"] == "conflict"
    conflicts = second["auto_assign"]["conflicts"]
    assert conflicts
    assert conflicts[0]["session_id"] == second["session"]["session_id"]
    assert conflicts[0]["reason"] == "resource_overlap"
    kinds = {option["type"] for option in conflicts[0]["options"]}
    assert "keep_unassigned" in kinds

    # 场次照常保存；冲突场次没有 assignment。
    sessions = load_sessions(store=store)
    assert len(sessions) == 2
    assignments = load_assignments(store=store)
    assert second["session"]["session_id"] not in set(assignments["session_id"])


def test_resources_not_configured_does_not_fail(store):
    result = promote(store)
    auto = result["auto_assign"]
    assert auto["status"] == "resources_not_configured"
    assert auto["assigned"] == []
    assert len(load_sessions(store=store)) == 1


def test_auto_assign_is_idempotent(store):
    add_resource(store)
    session_id = promote(store)["session"]["session_id"]

    first = auto_assign(store, session_ids=[session_id], now=NOW)
    assert first["status"] == "unchanged"
    assert first["unchanged"] == [session_id]

    second = auto_assign(store, session_ids=[session_id], now=NOW)
    assert second["status"] == "unchanged"
    assignments = load_assignments(store=store)
    assert len(assignments) == 2  # 一房一 DM，没有重复落库


def test_manual_assignment_is_not_overwritten(store):
    add_resource(store, rooms=2, dms=2)
    first = promote(store, script="雾隐山庄")
    session_id = first["session"]["session_id"]

    replace_assignments(
        [session_id],
        [
            {
                "session_id": session_id,
                "resource_kind": "room",
                "resource_id": "room-manual",
                "source": "manual",
            },
            {
                "session_id": session_id,
                "resource_kind": "dm",
                "resource_id": "dm-manual",
                "source": "manual",
            },
        ],
        store=store,
        now=NOW,
    )
    result = auto_assign(store, session_ids=[session_id], now=NOW)
    assert result["status"] == "unchanged"
    rows = load_assignments(store=store)
    assert set(rows["resource_id"]) == {"room-manual", "dm-manual"}
    assert set(rows["source"]) == {"manual"}


def test_unknown_session_id_returns_conflict(store):
    add_resource(store)
    upsert_sessions(
        [
            {
                "session_id": "s-1",
                "store_name": "迷雾剧场",
                "script_name": "雾隐山庄",
                "start_time": START,
                "total_seats": 6,
                "status": "待开场",
            }
        ],
        store=store,
    )
    result = auto_assign(store, session_ids=["s-1", "s-missing"], now=NOW)
    assert result["status"] == "conflict"
    reasons = {item["reason"] for item in result["conflicts"]}
    assert "session_missing" in reasons
    assert [item["session_id"] for item in result["assigned"]] == ["s-1"]


def test_no_future_sessions_returns_unchanged(store):
    add_resource(store)
    result = auto_assign(store, now=NOW)
    assert result["status"] == "unchanged"
    assert any("没有待开场" in warning for warning in result["warnings"])


# ---------------------------------------------------------------------------
# WP16 干跑预览：preview / partial 报告
# ---------------------------------------------------------------------------


def test_dry_run_previews_without_writing(store):
    # 先无资源建场：自动分配停在 resources_not_configured，不落库。
    no_resource = promote(store)
    assert no_resource["auto_assign"]["status"] == "resources_not_configured"
    assert load_assignments(store=store).empty

    add_resource(store)
    preview = auto_assign(store, dry_run=True, now=NOW)
    assert preview["preview"] is True
    assert preview["status"] == "assigned"
    assert len(preview["assigned"]) == 1
    assert preview["assigned"][0]["script_name"] == "雾隐山庄"
    assert preview["assigned"][0]["start"] == START
    # 干跑只返回预览，不写 assignment 表。
    assert load_assignments(store=store).empty

    written = auto_assign(store, dry_run=False, now=NOW)
    assert written["status"] == "assigned"
    rows = load_assignments(store=store)
    assert len(rows) == 2  # 一房一 DM
    assert set(rows["resource_kind"]) == {"room", "dm"}


def test_preview_reports_partial_sessions_without_moving_them(store):
    add_resource(store)
    auto = promote(store)["auto_assign"]
    session_id = auto["assigned"][0]["session_id"]
    room_id = auto["assigned"][0]["room_id"]

    # 换成只有房间的手工分配：语义上仍未完成，但预览不得动它。
    replace_assignments(
        [session_id],
        [
            {
                "session_id": session_id,
                "resource_kind": "room",
                "resource_id": room_id,
                "role": "main",
                "source": "manual",
            }
        ],
        store=store,
        now=NOW,
    )

    preview = auto_assign(store, dry_run=True, now=NOW)
    assert preview["preview"] is True
    assert preview["partial"] == [
        {
            "session_id": session_id,
            "script_name": "雾隐山庄",
            "start": START,
            "missing": "dm",
        }
    ]
    assert session_id in preview["unchanged"]
    assert preview["assigned"] == []

    rows = load_assignments(store=store)
    assert len(rows) == 1
    assert set(rows["resource_kind"]) == {"room"}  # 没有补 DM 行
    assert rows.iloc[0]["resource_id"] == room_id
    assert rows.iloc[0]["source"] == "manual"


def test_preview_does_not_change_start_time(store):
    add_resource(store)
    session_id = promote(store)["session"]["session_id"]

    before = load_sessions(store=store)
    original = before[before["session_id"] == session_id].iloc[0]["start_time"]
    before_rows = load_assignments(store=store)

    preview = auto_assign(store, dry_run=True, now=NOW)
    assert preview["preview"] is True

    after = load_sessions(store=store)
    row = after[after["session_id"] == session_id].iloc[0]
    assert row["start_time"] == original
    # 预览同样不得改写已有分配行。
    pd.testing.assert_frame_equal(load_assignments(store=store), before_rows)


# ---------------------------------------------------------------------------
# 冲突后的选择
# ---------------------------------------------------------------------------


def _conflict_pair(store):
    add_resource(store, rooms=1, dms=1)
    promote(store, script="雾隐山庄")
    second = promote(store, script="年轮")
    assert second["auto_assign"]["status"] == "conflict"
    return second["session"]["session_id"]


def test_resolve_switch_room_applies(store):
    session_id = _conflict_pair(store)
    upsert_rooms(
        [{"name": "新房", "capacity": 8, "buffer_minutes": 15}],
        store=store,
        now=NOW,
    )
    # 通过 store 读取房间 ID，避免依赖具体后端。
    frame = store.load_rooms()
    target = frame[frame["name"] == "新房"].iloc[0]["room_id"]
    result = resolve_conflict(
        store,
        session_id=session_id,
        choice="switch_room",
        target_id=target,
        now=NOW,
    )
    assert result["status"] == "applied"
    assignments = load_assignments(store=store)
    rows = assignments[assignments["session_id"] == session_id]
    assert rows.iloc[0]["resource_id"] == target
    assert rows.iloc[0]["source"] == "manual"


def test_resolve_switch_dm_applies(store):
    session_id = _conflict_pair(store)
    upsert_dms([{"name": "新 DM", "skills": ""}], store=store, now=NOW)
    frame = store.load_dms()
    target = frame[frame["name"] == "新 DM"].iloc[0]["dm_id"]
    result = resolve_conflict(
        store,
        session_id=session_id,
        choice="switch_dm",
        target_id=target,
        now=NOW,
    )
    assert result["status"] == "applied"
    assignments = load_assignments(store=store)
    rows = assignments[assignments["session_id"] == session_id]
    assert rows.iloc[0]["resource_kind"] == "dm"
    assert rows.iloc[0]["resource_id"] == target


def test_resolve_shift_time_only_returns_confirmation(store):
    session_id = _conflict_pair(store)
    before = load_sessions(store=store)
    original = before[before["session_id"] == session_id].iloc[0]["start_time"]

    result = resolve_conflict(
        store,
        session_id=session_id,
        choice="shift_time",
        now=NOW,
    )
    assert result["status"] == "requires_confirmation"
    after = load_sessions(store=store)
    assert after[after["session_id"] == session_id].iloc[0]["start_time"] == original


def test_resolve_cancel_or_waitlist_does_not_cancel(store):
    session_id = _conflict_pair(store)
    result = resolve_conflict(
        store,
        session_id=session_id,
        choice="cancel_or_waitlist",
        now=NOW,
    )
    assert result["status"] == "requires_confirmation"
    sessions = load_sessions(store=store)
    assert (sessions["session_id"] == session_id).any()
    status = sessions[sessions["session_id"] == session_id].iloc[0]["status"]
    assert status == "待开场"


def test_resolve_keep_unassigned(store):
    session_id = _conflict_pair(store)
    result = resolve_conflict(
        store,
        session_id=session_id,
        choice="keep_unassigned",
        now=NOW,
    )
    assert result["status"] == "kept"
    assignments = load_assignments(store=store)
    assert session_id not in set(assignments["session_id"])


def test_resolve_unknown_session(store):
    result = resolve_conflict(
        store,
        session_id="s-missing",
        choice="switch_room",
        target_id="r1",
        now=NOW,
    )
    assert result["code"] == "session_not_found"


# ---------------------------------------------------------------------------
# 改期预检 buffered_overlap_hits（纯函数）
# ---------------------------------------------------------------------------


def _seed_manual_overlap(store, *, kind="room"):
    """A 场 20:00 自动占用资源；B 场手工占用同一资源、时间错开到次日 10:00。"""
    add_resource(store, rooms=1, dms=1)
    upsert_sessions(
        [
            {
                "session_id": "s-a",
                "store_name": "迷雾剧场",
                "script_name": "雾隐山庄",
                "start_time": START,
                "total_seats": 6,
                "status": "待开场",
            },
            {
                "session_id": "s-b",
                "store_name": "迷雾剧场",
                "script_name": "年轮",
                "start_time": "2026-10-22 10:00",
                "total_seats": 6,
                "status": "待开场",
            },
        ],
        store=store,
    )
    rooms = store.load_rooms()
    dms = store.load_dms()
    resource_id = (
        rooms.iloc[0]["room_id"] if kind == "room" else dms.iloc[0]["dm_id"]
    )
    # A 场自动占用（对方占用不分来源）；B 场手工占用。
    replace_assignments(
        ["s-a"],
        [
            {
                "session_id": "s-a",
                "resource_kind": kind,
                "resource_id": resource_id,
                "source": "auto",
            }
        ],
        store=store,
        now=NOW,
    )
    replace_assignments(
        ["s-b"],
        [
            {
                "session_id": "s-b",
                "resource_kind": kind,
                "resource_id": resource_id,
                "source": "manual",
            }
        ],
        store=store,
        now=NOW,
    )
    return load_schedule_context(store)


def test_buffered_overlap_hits_room_uses_room_buffer(store):
    context = _seed_manual_overlap(store, kind="room")
    # A 场占到 24:00，房缓冲 15 → 00:10 撞、00:20 放行。
    hits = buffered_overlap_hits(context, "s-b", "2026-10-22 00:10")
    assert [hit["resource_kind"] for hit in hits] == ["room"]
    assert hits[0]["conflict_session_id"] == "s-a"
    assert buffered_overlap_hits(context, "s-b", "2026-10-22 00:20") == []


def test_buffered_overlap_hits_dm_uses_rest_buffer(store):
    context = _seed_manual_overlap(store, kind="dm")
    # DM 休整缓冲 30 → 00:20 撞、00:35 放行。
    hits = buffered_overlap_hits(context, "s-b", "2026-10-22 00:20")
    assert [hit["resource_kind"] for hit in hits] == ["dm"]
    assert buffered_overlap_hits(context, "s-b", "2026-10-22 00:35") == []


def test_buffered_overlap_hits_other_auto_assignment_counts(store):
    context = _seed_manual_overlap(store, kind="room")
    # 对方是 auto 分配也照样占用；本场改到重叠时段即撞。
    hits = buffered_overlap_hits(context, "s-b", "2026-10-21 21:00")
    assert [hit["conflict_session_id"] for hit in hits] == ["s-a"]


# ---------------------------------------------------------------------------
# WP13B 接线：请假与熟悉度从 store 进入 auto_assign
# ---------------------------------------------------------------------------


def test_auto_assign_skips_dm_on_leave(store):
    from app.tools.resources import load_dms, upsert_dm_leave

    add_resource(store, rooms=1, dms=2)
    dms = load_dms(store=store)
    # 让 DM1 技能更匹配，若无请假它应当是首选；请假后应落到 DM2。
    dms.loc[dms["name"] == "DM1", "skills"] = "推理"
    from app.tools.resources import save_dms

    save_dms(dms, store=store)
    target = load_dms(store=store)
    leave_id = target[target["name"] == "DM1"].iloc[0]["dm_id"]

    upsert_dm_leave(leave_id, START[:10], store=store, now=NOW)
    result = promote(store)
    assert result["auto_assign"]["status"] == "assigned"

    assignments = load_assignments(store=store)
    dm_rows = assignments[assignments["resource_kind"] == "dm"]
    assigned_ids = set(dm_rows["resource_id"].astype("string").fillna(""))
    assert leave_id not in assigned_ids
    assert assigned_ids  # 另一个 DM 正常顶上

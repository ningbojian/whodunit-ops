"""DM 场控状态机：开始、切幕、暂停、延长、结束、幂等与「到点不推进」。"""

from __future__ import annotations

import pandas as pd
import pytest

from app.integrations.hardware import HardwareResult
from app.store.engine import create_database_engine, ensure_schema
from app.store.excel import ExcelStore
from app.store.sql import SqlStore
from app.tools.dm_control import (
    DMControlError,
    abandon_run,
    apply_action,
    create_run,
    get_run,
    issue_run_code,
    list_events,
    list_runs_today,
    redeem_code,
    revoke_run_token,
    run_report,
    sync_actions,
    verify_dm_token,
)
from app.tools.resources import replace_assignments, upsert_dms, upsert_rooms
from app.tools.scripts import upsert_script
from app.tools.stages import save_stages
from app.tools.storage import load_sessions, save_sessions, upsert_sessions

SECRET = "test-session-secret"
NOW = pd.Timestamp("2026-10-20 12:00")
START = "2026-10-20 20:00"


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
    value = SqlStore(engine, "test-dm")
    value.ensure_tenant()
    yield value
    engine.dispose()


class RecordingHardware:
    name = "recording"
    configured = True

    def __init__(self, result: HardwareResult | None = None) -> None:
        self.calls: list[dict] = []
        self.result = result or HardwareResult(
            ok=True, status="ok", status_code=200, latency_ms=3, message=None
        )

    def send_scene(self, **kwargs):
        self.calls.append(kwargs)
        return self.result


def add_session(store, session_id="s-1", start=START, status="待开场"):
    upsert_sessions(
        [
            {
                "session_id": session_id,
                "store_name": "迷雾剧场",
                "script_name": "雾隐山庄",
                "script_type": "推理",
                "start_time": start,
                "total_seats": 6,
                "list_price": 168,
                "status": status,
            }
        ],
        store=store,
    )


def build_world(store):
    upsert_script(
        script_name="雾隐山庄",
        aliases="WYSZ",
        script_type="推理",
        total_seats=6,
        duration_minutes=45,
        list_price=168,
        store=store,
        now=NOW,
    )
    upsert_rooms(
        [{"name": "A 房", "capacity": 8, "buffer_minutes": 15}],
        store=store,
        now=NOW,
    )
    upsert_dms([{"name": "小北", "skills": "推理"}], store=store, now=NOW)
    save_stages(
        store,
        script_name="雾隐山庄",
        stages=[
            {
                "name": "开场",
                "stage_type": "opening",
                "duration_minutes": 10,
                "warn_before_seconds": 60,
                "cue_text": "宣读背景",
                "hardware_scene": {"light": "warm_low", "sound": "intro_01"},
            },
            {
                "name": "搜证",
                "stage_type": "search",
                "duration_minutes": 20,
                "warn_before_seconds": 300,
                "cue_text": "控制节奏",
                "hardware_scene": {"light": "cool", "volume": 40},
            },
            {
                "name": "复盘",
                "stage_type": "reveal",
                "duration_minutes": 15,
                "warn_before_seconds": 60,
                "cue_text": "按时间线复盘",
            },
        ],
        now=NOW,
    )
    add_session(store)
    rooms = store.load_rooms()
    room_id = str(rooms[rooms["name"] == "A 房"].iloc[0]["room_id"])
    dms = store.load_dms()
    dm_id = str(dms[dms["name"] == "小北"].iloc[0]["dm_id"])
    replace_assignments(
        ["s-1"],
        [
            {
                "session_id": "s-1",
                "resource_kind": "room",
                "resource_id": room_id,
                "source": "manual",
            },
            {
                "session_id": "s-1",
                "resource_kind": "dm",
                "resource_id": dm_id,
                "source": "manual",
            },
        ],
        store=store,
        now=NOW,
    )
    return {"room_id": room_id, "dm_id": dm_id}


def create_pending(
    store,
    world,
    *,
    session_id="s-1",
    mode="software",
    use_dm=True,
    hardware=None,
    now=NOW,
):
    return create_run(
        store,
        session_id=session_id,
        created_by="tenant-dm-test",
        dm_id=world["dm_id"] if use_dm else None,
        mode=mode,
        now=now,
        secret=SECRET,
        hardware=hardware,
    )


def redeem(store, created, *, now=NOW):
    return redeem_code(
        store, code=created["code"], device_id="device-1", now=now, secret=SECRET
    )


def version(store, run_id, *, now=NOW):
    return get_run(store, run_id, now=now)["run"]["state_version"]


def act(
    store,
    run_id,
    action,
    *,
    event_id=None,
    expected=None,
    at=None,
    now=NOW,
    minutes=None,
    mode=None,
    hardware=None,
    replay=False,
):
    if expected is None:
        expected = version(store, run_id, now=now)
    return apply_action(
        store,
        run_id=run_id,
        action=action,
        client_event_id=event_id,
        expected_version=expected,
        at=at,
        now=now,
        minutes=minutes,
        mode=mode,
        hardware=hardware,
        replay=replay,
    )


def start_run(store, world, *, mode="software", hardware=None, now=NOW):
    created = create_pending(store, world, mode=mode, hardware=hardware, now=now)
    redeemed = redeem(store, created, now=now)
    started = act(
        store,
        redeemed["run"]["run_id"],
        "start",
        event_id="evt-start",
        expected=redeemed["run"]["state_version"],
        at=now,
        now=now,
        hardware=hardware,
    )
    assert started["ok"] is True, started
    return created, redeemed, started


def event_types(store, run_id):
    return [item["event_type"] for item in list_events(store, run_id=run_id)]


# ---------------------------------------------------------------------------
# 创建、兑换与开始
# ---------------------------------------------------------------------------


def test_create_run_is_pending_with_preview(store):
    world = build_world(store)
    created = create_pending(store, world)

    assert len(created["code"]) == 8
    assert created["run"]["status"] == "pending"
    assert created["run"]["current_seq"] is None
    assert created["run"]["template_version"] is None
    assert created["template_preview"] is True
    assert [item["name"] for item in created["stages"]] == ["开场", "搜证", "复盘"]

    runs = store.load_dm_runs()
    assert len(runs) == 1
    assert runs.iloc[0]["access_code_hash"] != created["code"]
    assert created["code"] not in runs.to_string()
    assert set(event_types(store, created["run"]["run_id"])) == {
        "run_created",
        "code_issued",
    }


def test_redeem_does_not_start_the_run(store):
    world = build_world(store)
    created = create_pending(store, world)
    redeemed = redeem(store, created)

    assert redeemed["token"].startswith("dm1.")
    assert redeemed["run"]["status"] == "pending"
    assert redeemed["run"]["current_seq"] is None
    assert redeemed["template_preview"] is True
    payload, run = verify_dm_token(
        store, redeemed["token"], secret=SECRET, now=NOW
    )
    assert payload["run"] == created["run"]["run_id"]
    assert run["status"] == "pending"
    assert "code_redeemed" in event_types(store, run["run_id"])


def test_start_snapshots_first_stage_and_room(store):
    world = build_world(store)
    _, redeemed, started = start_run(store, world)
    run = started["run"]

    assert run["status"] == "active"
    assert run["current_seq"] == 1
    assert run["template_version"] == 1
    assert run["stage_started_at"] == "2026-10-20T12:00:00Z"
    assert run["stage_ends_at"] == "2026-10-20T12:10:00Z"
    assert run["room_id"] == world["room_id"]
    assert run["room_name"] == "A 房"
    assert run["dm_name"] == "小北"
    assert started["current_stage"]["name"] == "开场"
    assert started["hardware"]["status"] == "skipped"
    assert "run_started" in event_types(store, run["run_id"])


def test_pending_preview_tracks_template_edits(store):
    world = build_world(store)
    created = create_pending(store, world)
    save_stages(
        store,
        script_name="雾隐山庄",
        stages=[{"name": "新版开场", "duration_minutes": 5}],
        now=NOW,
    )
    preview = get_run(store, created["run"]["run_id"], now=NOW)
    assert preview["template_preview"] is True
    assert [item["name"] for item in preview["stages"]] == ["新版开场"]


def test_start_uses_snapshot_even_if_template_changes_after(store):
    world = build_world(store)
    _, redeemed, started = start_run(store, world)
    run_id = started["run"]["run_id"]

    save_stages(
        store,
        script_name="雾隐山庄",
        stages=[{"name": "改后一", "duration_minutes": 5}, {"name": "改后二", "duration_minutes": 5}],
        now=NOW,
    )
    view = get_run(store, run_id, now=NOW)
    assert [item["name"] for item in view["stages"]] == ["开场", "搜证", "复盘"]
    assert view["run"]["template_version"] == 1

    moved = act(store, run_id, "next", event_id="evt-next-1", now=NOW)
    assert moved["ok"] is True
    assert moved["current_stage"]["name"] == "搜证"


def test_create_requires_template(store):
    upsert_script(
        script_name="雾隐山庄", aliases="WYSZ", store=store, now=NOW
    )
    add_session(store)
    with pytest.raises(DMControlError) as excinfo:
        create_run(
            store,
            session_id="s-1",
            created_by="tenant",
            now=NOW,
            secret=SECRET,
        )
    assert excinfo.value.code == "template_missing"


def test_create_rejects_cancelled_session(store):
    world = build_world(store)
    add_session(store, session_id="s-2", start="2026-10-21 20:00", status="已取消")
    with pytest.raises(DMControlError) as excinfo:
        create_run(
            store,
            session_id="s-2",
            created_by="tenant",
            mode="software",
            now=NOW,
            secret=SECRET,
        )
    assert excinfo.value.code == "session_cancelled"
    assert world  # 资源已建好，仍不能对取消场次发码


def test_multiple_pending_runs_for_same_dm_are_allowed(store):
    world = build_world(store)
    add_session(store, session_id="s-2", start="2026-10-21 20:00")
    first = create_pending(store, world)
    second = create_pending(store, world, session_id="s-2")
    assert first["run"]["run_id"] != second["run"]["run_id"]
    assert first["run"]["status"] == second["run"]["status"] == "pending"


# ---------------------------------------------------------------------------
# 切幕
# ---------------------------------------------------------------------------


def test_next_and_previous_move_stages(store):
    world = build_world(store)
    _, _, started = start_run(store, world)
    run_id = started["run"]["run_id"]

    moved = act(
        store,
        run_id,
        "next",
        event_id="evt-next-1",
        at=NOW + pd.Timedelta(minutes=5),
        now=NOW + pd.Timedelta(minutes=5),
    )
    assert moved["ok"] is True
    assert moved["run"]["current_seq"] == 2
    assert moved["run"]["stage_started_at"] == "2026-10-20T12:05:00Z"
    assert moved["run"]["stage_ends_at"] == "2026-10-20T12:25:00Z"

    back = act(store, run_id, "previous", event_id="evt-prev-1", now=NOW + pd.Timedelta(minutes=6))
    assert back["ok"] is True
    assert back["run"]["current_seq"] == 1
    assert back["current_stage"]["name"] == "开场"
    assert event_types(store, run_id).count("stage_next") == 1
    assert event_types(store, run_id).count("stage_previous") == 1


def test_previous_at_first_stage_is_rejected(store):
    world = build_world(store)
    _, _, started = start_run(store, world)
    result = act(store, started["run"]["run_id"], "previous", event_id="evt-prev-1")
    assert result["ok"] is False
    assert result["code"] == "at_first_stage"
    assert result["run"]["current_seq"] == 1


def test_next_at_last_stage_does_not_finish(store):
    world = build_world(store)
    _, _, started = start_run(store, world)
    run_id = started["run"]["run_id"]
    act(store, run_id, "next", event_id="evt-n1")
    act(store, run_id, "next", event_id="evt-n2")

    blocked = act(store, run_id, "next", event_id="evt-n3")
    assert blocked["ok"] is False
    assert blocked["code"] == "at_last_stage"
    assert blocked["run"]["status"] == "active"
    assert blocked["run"]["current_seq"] == 3

    finished = act(store, run_id, "finish", event_id="evt-finish")
    assert finished["ok"] is True
    assert finished["run"]["status"] == "completed"


# ---------------------------------------------------------------------------
# 暂停、继续与延长
# ---------------------------------------------------------------------------


def test_pause_freezes_remaining_and_resume_recomputes(store):
    world = build_world(store)
    _, _, started = start_run(store, world)
    run_id = started["run"]["run_id"]

    paused = act(
        store,
        run_id,
        "pause",
        event_id="evt-pause",
        at=NOW + pd.Timedelta(minutes=4),
        now=NOW + pd.Timedelta(minutes=4),
    )
    assert paused["ok"] is True
    assert paused["run"]["status"] == "paused"
    assert paused["run"]["remaining_seconds"] == 360
    paused_version = paused["run"]["state_version"]
    paused_events = len(list_events(store, run_id=run_id))

    # 暂停期间时间流逝不推进阶段，也不产生超时。
    later = get_run(store, run_id, now=NOW + pd.Timedelta(minutes=30))
    assert later["run"]["status"] == "paused"
    assert later["run"]["current_seq"] == 1
    assert later["run"]["remaining_seconds"] == 360
    assert later["run"]["overtime_seconds"] == 0
    assert later["run"]["state_version"] == paused_version
    assert len(list_events(store, run_id=run_id)) == paused_events

    blocked = act(
        store,
        run_id,
        "next",
        event_id="evt-next-paused",
        now=NOW + pd.Timedelta(minutes=30),
    )
    assert blocked["ok"] is False
    assert blocked["code"] == "run_paused"

    resumed = act(
        store,
        run_id,
        "resume",
        event_id="evt-resume",
        now=NOW + pd.Timedelta(minutes=30),
    )
    assert resumed["ok"] is True
    assert resumed["run"]["status"] == "active"
    assert resumed["run"]["stage_ends_at"] == "2026-10-20T12:36:00Z"
    assert resumed["run"]["paused_total_seconds"] == 1560

    one_minute_later = get_run(store, run_id, now=NOW + pd.Timedelta(minutes=31))
    assert one_minute_later["run"]["remaining_seconds"] == 300


def test_extend_bounds_and_effects(store):
    world = build_world(store)
    _, _, started = start_run(store, world)
    run_id = started["run"]["run_id"]

    too_long = act(store, run_id, "extend", event_id="evt-x1", minutes=21)
    assert too_long["ok"] is False
    assert too_long["code"] == "invalid_action"
    too_short = act(store, run_id, "extend", event_id="evt-x2", minutes=0)
    assert too_short["ok"] is False

    extended = act(
        store,
        run_id,
        "extend",
        event_id="evt-x3",
        minutes=5,
        at=NOW + pd.Timedelta(minutes=2),
        now=NOW + pd.Timedelta(minutes=2),
    )
    assert extended["ok"] is True
    assert extended["run"]["stage_ends_at"] == "2026-10-20T12:15:00Z"

    paused = act(
        store,
        run_id,
        "pause",
        event_id="evt-pause",
        at=NOW + pd.Timedelta(minutes=3),
        now=NOW + pd.Timedelta(minutes=3),
    )
    assert paused["run"]["remaining_seconds"] == 720

    paused_extend = act(
        store,
        run_id,
        "extend",
        event_id="evt-x4",
        minutes=3,
        now=NOW + pd.Timedelta(minutes=4),
    )
    assert paused_extend["ok"] is True
    assert paused_extend["run"]["remaining_seconds"] == 900

    resumed = act(
        store,
        run_id,
        "resume",
        event_id="evt-resume",
        now=NOW + pd.Timedelta(minutes=30),
    )
    assert resumed["run"]["stage_ends_at"] == "2026-10-20T12:45:00Z"


# ---------------------------------------------------------------------------
# 结束、撤销与并发
# ---------------------------------------------------------------------------


def test_finish_completes_and_blocks_further_actions(store):
    world = build_world(store)
    _, redeemed, started = start_run(store, world)
    run_id = started["run"]["run_id"]

    finished = act(
        store,
        run_id,
        "finish",
        event_id="evt-finish",
        at=NOW + pd.Timedelta(minutes=6),
        now=NOW + pd.Timedelta(minutes=6),
    )
    assert finished["ok"] is True
    assert finished["run"]["status"] == "completed"
    assert finished["run"]["finished_at"] == "2026-10-20T12:06:00Z"

    again = act(store, run_id, "next", event_id="evt-after-finish")
    assert again["ok"] is False
    assert again["code"] == "run_finished"

    with pytest.raises(DMControlError) as excinfo:
        verify_dm_token(store, redeemed["token"], secret=SECRET, now=NOW)
    assert excinfo.value.code == "token_invalid"

    with pytest.raises(DMControlError) as excinfo:
        issue_run_code(store, run_id=run_id, now=NOW, secret=SECRET)
    assert excinfo.value.code == "run_finished"


def test_stale_version_returns_current_run(store):
    world = build_world(store)
    _, _, started = start_run(store, world)
    run_id = started["run"]["run_id"]
    current = started["run"]["state_version"]

    stale = act(
        store,
        run_id,
        "next",
        event_id="evt-stale",
        expected=current - 1,
    )
    assert stale["ok"] is False
    assert stale["code"] == "stale_state"
    assert stale["run"]["current_seq"] == 1
    assert stale["run"]["state_version"] == current
    assert "stage_next" not in event_types(store, run_id)

    retried = act(store, run_id, "next", event_id="evt-fresh", expected=current)
    assert retried["ok"] is True
    assert retried["run"]["current_seq"] == 2


def test_duplicate_event_id_is_ignored(store):
    world = build_world(store)
    _, _, started = start_run(store, world)
    run_id = started["run"]["run_id"]

    first = act(store, run_id, "next", event_id="evt-once")
    assert first["ok"] is True and first["duplicate"] is False
    count = event_types(store, run_id).count("stage_next")

    again = act(store, run_id, "next", event_id="evt-once", expected=first["run"]["state_version"])
    assert again["ok"] is True
    assert again["duplicate"] is True
    assert again["run"]["current_seq"] == 2
    assert event_types(store, run_id).count("stage_next") == count


def test_unknown_action_is_rejected(store):
    world = build_world(store)
    _, _, started = start_run(store, world)
    result = act(store, started["run"]["run_id"], "dance", event_id="evt-bad")
    assert result["ok"] is False
    assert result["code"] == "invalid_action"


def test_unknown_run_is_reported(store):
    world = build_world(store)
    assert world
    result = apply_action(store, run_id="run-missing", action="start", now=NOW)
    assert result["ok"] is False
    assert result["code"] == "run_not_found"

    with pytest.raises(DMControlError) as excinfo:
        get_run(store, "run-missing", now=NOW)
    assert excinfo.value.code == "run_not_found"


def test_one_live_run_per_session(store):
    world = build_world(store)
    created = create_pending(store, world)
    with pytest.raises(DMControlError) as excinfo:
        create_pending(store, world)
    assert excinfo.value.code == "run_active"

    abandoned = abandon_run(store, run_id=created["run"]["run_id"], now=NOW)
    assert abandoned["run"]["status"] == "abandoned"
    assert store.load_dm_runs().iloc[0]["access_code_hash"] is None or pd.isna(
        store.load_dm_runs().iloc[0]["access_code_hash"]
    )
    with pytest.raises(DMControlError):
        redeem(store, created)
    with pytest.raises(DMControlError) as excinfo:
        abandon_run(store, run_id=created["run"]["run_id"], now=NOW)
    assert excinfo.value.code == "invalid_action"

    second = create_pending(store, world)
    assert second["run"]["status"] == "pending"


def test_one_active_run_per_dm(store):
    world = build_world(store)
    add_session(store, session_id="s-2", start="2026-10-21 20:00")
    first = create_pending(store, world)
    second = create_pending(store, world, session_id="s-2")

    first_r = redeem(store, first)
    started = act(
        store,
        first_r["run"]["run_id"],
        "start",
        event_id="evt-start-1",
        expected=first_r["run"]["state_version"],
    )
    assert started["ok"] is True

    second_r = redeem(store, second)
    blocked = act(
        store,
        second_r["run"]["run_id"],
        "start",
        event_id="evt-start-2",
        expected=second_r["run"]["state_version"],
    )
    assert blocked["ok"] is False
    assert blocked["code"] == "dm_busy"

    act(store, first_r["run"]["run_id"], "finish", event_id="evt-finish-1")
    allowed = act(
        store,
        second_r["run"]["run_id"],
        "start",
        event_id="evt-start-2b",
    )
    assert allowed["ok"] is True
    assert allowed["run"]["status"] == "active"


def test_frozen_time_never_advances_stage(store):
    world = build_world(store)
    _, _, started = start_run(store, world)
    run_id = started["run"]["run_id"]
    before = get_run(store, run_id, now=NOW)
    events_before = list_events(store, run_id=run_id)

    far_later = NOW + pd.Timedelta(hours=1)
    after = get_run(store, run_id, now=far_later)
    assert after["run"]["current_seq"] == 1
    assert after["run"]["state_version"] == before["run"]["state_version"]
    assert after["run"]["status"] == "active"
    assert after["run"]["remaining_seconds"] == 0
    assert after["run"]["overtime_seconds"] == 3000
    assert list_events(store, run_id=run_id) == events_before


# ---------------------------------------------------------------------------
# 离线补传
# ---------------------------------------------------------------------------


def test_sync_applies_actions_in_order_and_skips_hardware(store):
    world = build_world(store)
    hardware = RecordingHardware()
    _, _, started = start_run(store, world, mode="hardware", hardware=hardware)
    run_id = started["run"]["run_id"]
    assert started["hardware"]["status"] == "ok"
    assert len(hardware.calls) == 1

    result = sync_actions(
        store,
        run_id=run_id,
        actions=[
            {
                "action": "next",
                "client_event_id": "sync-1",
                "expected_version": started["run"]["state_version"],
                "at": "2026-10-20T12:05:00Z",
            },
            {
                "action": "next",
                "client_event_id": "sync-2",
                "expected_version": started["run"]["state_version"] + 1,
                "at": "2026-10-20T12:06:00Z",
            },
        ],
        now=NOW + pd.Timedelta(minutes=7),
    )
    assert result["ok"] is True
    assert [item["ok"] for item in result["results"]] == [True, True]
    assert result["run"]["current_seq"] == 3
    # 离线补传不调用硬件；第二幕有硬件场景，记录一次 skipped_replay。
    # 第三幕本身没有场景，属于正常 skipped，不写硬件失败/补传事件。
    assert len(hardware.calls) == 1
    types = event_types(store, run_id)
    assert types.count("hardware_skipped_replay") == 1
    assert "hardware_sent" not in types
    assert "hardware_failed" not in types


def test_sync_stops_at_stale_version(store):
    world = build_world(store)
    _, _, started = start_run(store, world)
    run_id = started["run"]["run_id"]
    result = sync_actions(
        store,
        run_id=run_id,
        actions=[
            {
                "action": "next",
                "client_event_id": "sync-a",
                "expected_version": started["run"]["state_version"],
            },
            {
                "action": "next",
                "client_event_id": "sync-b",
                "expected_version": started["run"]["state_version"],
            },
        ],
        now=NOW,
    )
    assert result["ok"] is False
    assert result["results"][0]["ok"] is True
    assert result["results"][1]["code"] == "stale_state"
    assert result["run"]["current_seq"] == 2
    assert event_types(store, run_id).count("stage_next") == 1


def test_sync_rejects_non_object_action(store):
    world = build_world(store)
    _, _, started = start_run(store, world)
    result = sync_actions(
        store, run_id=started["run"]["run_id"], actions=["next"], now=NOW
    )
    assert result["ok"] is False
    assert result["results"][0]["code"] == "invalid_action"


def test_client_time_is_clamped(store):
    world = build_world(store)
    _, _, started = start_run(store, world)
    run_id = started["run"]["run_id"]

    early = act(
        store,
        run_id,
        "next",
        event_id="evt-early",
        at=NOW - pd.Timedelta(hours=2),
        now=NOW,
    )
    assert early["ok"] is True
    assert early["run"]["stage_started_at"] == "2026-10-20T12:00:00Z"
    assert any("早于场控创建时间" in warning for warning in early["warnings"])

    future = act(
        store,
        run_id,
        "next",
        event_id="evt-future",
        at=NOW + pd.Timedelta(hours=2),
        now=NOW,
    )
    assert future["ok"] is True
    assert future["run"]["stage_started_at"] == "2026-10-20T12:00:00Z"
    assert any("晚于服务器当前时间" in warning for warning in future["warnings"])


# ---------------------------------------------------------------------------
# 交接、只读视图与复盘
# ---------------------------------------------------------------------------


def test_revoke_invalidates_token_and_handoff_keeps_state(store):
    world = build_world(store)
    _, redeemed, started = start_run(store, world)
    run_id = started["run"]["run_id"]
    act(store, run_id, "next", event_id="evt-n1")

    revoked = revoke_run_token(store, run_id=run_id, now=NOW)
    assert revoked["run"]["status"] == "active"
    assert revoked["run"]["current_seq"] == 2
    with pytest.raises(DMControlError) as excinfo:
        verify_dm_token(store, redeemed["token"], secret=SECRET, now=NOW)
    assert excinfo.value.code == "token_invalid"

    handoff = issue_run_code(store, run_id=run_id, now=NOW, secret=SECRET)
    assert handoff["code"]
    new_redeemed = redeem(store, handoff, now=NOW)
    assert new_redeemed["run"]["status"] == "active"
    assert new_redeemed["run"]["current_seq"] == 2
    assert new_redeemed["template_preview"] is False
    payload, run = verify_dm_token(
        store, new_redeemed["token"], secret=SECRET, now=NOW
    )
    assert payload["run"] == run_id
    moved = act(
        store,
        run_id,
        "next",
        event_id="evt-n2",
        expected=new_redeemed["run"]["state_version"],
    )
    assert moved["ok"] is True
    assert moved["run"]["current_seq"] == 3


def test_room_change_is_a_warning_not_a_reset(store):
    world = build_world(store)
    _, _, started = start_run(store, world)
    run_id = started["run"]["run_id"]

    upsert_rooms(
        [{"name": "B 房", "capacity": 8, "buffer_minutes": 15}],
        store=store,
        now=NOW,
    )
    rooms = store.load_rooms()
    new_room = str(rooms[rooms["name"] == "B 房"].iloc[0]["room_id"])
    replace_assignments(
        ["s-1"],
        [
            {
                "session_id": "s-1",
                "resource_kind": "room",
                "resource_id": new_room,
                "source": "manual",
            }
        ],
        store=store,
        now=NOW,
    )

    view = get_run(store, run_id, now=NOW)
    assert view["run"]["room_id"] == world["room_id"]
    assert any("排期已换到" in warning for warning in view["warnings"])
    assert view["run"]["current_seq"] == 1


def test_cancelled_session_warning_does_not_end_run(store):
    world = build_world(store)
    _, _, started = start_run(store, world)
    run_id = started["run"]["run_id"]
    # 直接改状态：这里的 s-1 是测试用的手动 ID，走 update_session 会被重算 ID。
    frame = load_sessions(store=store)
    frame.loc[frame["session_id"] == "s-1", "status"] = "已取消"
    save_sessions(frame, store=store)

    view = get_run(store, run_id, now=NOW)
    assert view["run"]["status"] == "active"
    assert any("场次已取消" in warning for warning in view["warnings"])


def test_merchant_view_redacts_cue_text(store):
    world = build_world(store)
    _, _, started = start_run(store, world)
    run_id = started["run"]["run_id"]

    dm_view = get_run(store, run_id, for_dm=True, now=NOW)
    assert dm_view["stages"][0]["cue_text"] == "宣读背景"

    merchant_view = get_run(store, run_id, for_dm=False, now=NOW)
    for stage in merchant_view["stages"]:
        assert "cue_text" not in stage
        assert "hardware_scene" not in stage
        assert "notes" not in stage
    assert merchant_view["run"]["current_seq"] == 1


def test_run_report_after_finish(store):
    world = build_world(store)
    _, _, started = start_run(store, world)
    run_id = started["run"]["run_id"]
    act(
        store,
        run_id,
        "next",
        event_id="evt-n1",
        at=NOW + pd.Timedelta(minutes=5),
        now=NOW + pd.Timedelta(minutes=5),
    )
    act(
        store,
        run_id,
        "finish",
        event_id="evt-finish",
        at=NOW + pd.Timedelta(minutes=10),
        now=NOW + pd.Timedelta(minutes=10),
    )

    report = run_report(store, run_id=run_id, now=NOW + pd.Timedelta(minutes=11))
    assert report["total_seconds"] == 600
    assert report["pause_count"] == 0
    assert report["hardware_failed_count"] == 0
    assert [row["actual_seconds"] for row in report["stages"]] == [300, 300, 0]
    assert report["run"]["status"] == "completed"


def test_run_report_rejects_live_run(store):
    world = build_world(store)
    _, _, started = start_run(store, world)
    with pytest.raises(DMControlError) as excinfo:
        run_report(store, run_id=started["run"]["run_id"], now=NOW)
    assert excinfo.value.code == "run_active"


def test_list_runs_today(store):
    world = build_world(store)
    created = create_pending(store, world)
    listed = list_runs_today(store, day="2026-10-20", now=NOW)
    assert [item["run"]["run_id"] for item in listed] == [created["run"]["run_id"]]

    other_day = list_runs_today(store, day="2026-10-21", now=NOW)
    assert other_day == []

    redeemed = redeem(store, created)
    started = act(
        store,
        created["run"]["run_id"],
        "start",
        event_id="evt-start",
        expected=redeemed["run"]["state_version"],
    )
    assert started["run"]["status"] == "active"
    still_listed = list_runs_today(store, day="2026-10-21", now=NOW)
    assert [item["run"]["run_id"] for item in still_listed] == [created["run"]["run_id"]]

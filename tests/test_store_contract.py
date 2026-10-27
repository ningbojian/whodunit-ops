"""ExcelStore / SqlStore 契约测试：同一组业务操作得到同样的业务结果。"""

from __future__ import annotations

import pandas as pd
import pytest

from app.store.engine import create_database_engine, ensure_schema
from app.store.excel import ExcelStore
from app.store.sql import SqlStore
from app.tools.frame import coerce_frame, json_safe
from app.tools.frame_specs import (
    BOOKING_REFS_SPEC,
    DM_RUNS_SPEC,
    DM_RUN_EVENTS_SPEC,
    SCRIPT_STAGES_SPEC,
)
from app.tools.resources import (
    ASSIGNMENTS_SPEC,
    DMS_SPEC,
    ROOMS_SPEC,
    SHIFTS_SPEC,
    save_assignments,
    save_dm_shifts,
    save_dms,
    save_rooms,
)
from app.tools.scripts import load_scripts, script_to_dict, upsert_script
from app.tools.storage import (
    cancel_booking,
    load_bookings,
    load_sessions_with_counts,
    upsert_booking,
    upsert_sessions,
)

NOW = pd.Timestamp("2026-10-20 12:00")


@pytest.fixture()
def stores(tmp_path):
    excel = ExcelStore(
        sessions_path=tmp_path / "sessions.xlsx",
        bookings_path=tmp_path / "bookings.xlsx",
        scripts_path=tmp_path / "scripts.xlsx",
    )
    engine = create_database_engine(sqlite_path=tmp_path / "store.sqlite3")
    ensure_schema(engine)
    sql = SqlStore(engine, "test-contract")
    sql.ensure_tenant()
    yield excel, sql
    engine.dispose()


def _run(store):
    upsert_sessions(
        [
            {
                "session_id": "s-1",
                "store_name": "迷雾剧场",
                "script_name": "雾隐山庄",
                "script_type": "推理",
                "start_time": "2026-10-21 19:00",
                "total_seats": 6,
                "list_price": 168,
                "status": "待开场",
            },
            {
                "session_id": "s-2",
                "store_name": "迷雾剧场",
                "script_name": "长夜将明",
                "script_type": "情感",
                "start_time": "2026-10-22 14:00",
                "total_seats": 7,
                "list_price": 188,
                "status": "待开场",
            },
        ],
        store=store,
    )
    upsert_sessions(
        [{"session_id": "s-1", "total_seats": 8, "list_price": 0}],
        store=store,
    )
    upsert_booking("s-1", "阿青", seats=2, now=NOW, store=store)
    upsert_booking("s-1", "阿青", seats=3, now=NOW, store=store)
    cancel_booking("s-2", "不存在", now=NOW, store=store)
    upsert_script(script_name="雾隐", store=store, now=NOW)
    upsert_script(
        script_name="雾隐山庄",
        aliases="WYSZ",
        script_type="推理",
        total_seats=8,
        duration_minutes=240,
        list_price=168,
        store=store,
        now=NOW,
    )


def test_same_session_and_booking_results(stores):
    excel, sql = stores
    _run(excel)
    _run(sql)

    excel_sessions = load_sessions_with_counts(store=excel).reset_index(drop=True)
    sql_sessions = load_sessions_with_counts(store=sql).reset_index(drop=True)
    columns = [
        "session_id",
        "store_name",
        "script_name",
        "script_type",
        "start_time",
        "total_seats",
        "list_price",
        "status",
        "booked_seats",
        "remaining_seats",
    ]
    assert excel_sessions[columns].to_dict("records") == sql_sessions[columns].to_dict(
        "records"
    )

    excel_bookings = load_bookings(store=excel)
    sql_bookings = load_bookings(store=sql)
    booking_columns = ["booking_id", "session_id", "player_name", "seats", "status"]
    assert excel_bookings[booking_columns].to_dict(
        "records"
    ) == sql_bookings[booking_columns].to_dict("records")


def test_same_script_results(stores):
    excel, sql = stores
    _run(excel)
    _run(sql)

    excel_scripts = [
        script_to_dict(row) for _, row in load_scripts(store=excel).iterrows()
    ]
    sql_scripts = [
        script_to_dict(row) for _, row in load_scripts(store=sql).iterrows()
    ]
    columns = [
        "script_name",
        "aliases",
        "script_type",
        "total_seats",
        "duration_minutes",
        "list_price",
        "profile_status",
    ]
    assert [
        {key: item[key] for key in columns} for item in excel_scripts
    ] == [{key: item[key] for key in columns} for item in sql_scripts]


def _new_table_rows(now: pd.Timestamp) -> dict[str, list[dict]]:
    return {
        "rooms": [
            {
                "room_id": "room-a",
                "name": "A 房",
                "capacity": 6,
                "tags": "推理/大房",
                "buffer_minutes": 15,
                "active": True,
                "notes": "靠窗",
                "created_at": now,
                "updated_at": now,
            }
        ],
        "dms": [
            {
                "dm_id": "dm-a",
                "name": "小北",
                "skills": "推理/情感",
                "max_sessions_per_day": 3,
                "active": True,
                "created_at": now,
                "updated_at": now,
            }
        ],
        "shifts": [
            {
                "shift_id": "shift-a",
                "dm_id": "dm-a",
                "weekday": 1,
                "start_minute": 720,
                "end_minute": 1410,
                "active": True,
                "updated_at": now,
            }
        ],
        "assignments": [
            {
                "assignment_id": "assign-a",
                "session_id": "s-1",
                "resource_kind": "room",
                "resource_id": "room-a",
                "role": "main",
                "source": "auto",
                "assigned_at": now,
                "updated_at": now,
            }
        ],
        "refs": [
            {
                "ref_id": "ref-a",
                "source": "dianping",
                "external_id": "DP-1",
                "booking_id": "booking-a",
                "session_id": "s-1",
                "seats": 2,
                "status": "active",
                "data_as_of": now,
                "imported_at": now,
            }
        ],
        "stages": [
            {
                "stage_id": "stage-a",
                "script_id": "script-a",
                "script_name": "雾隐山庄",
                "seq": 1,
                "name": "开场",
                "stage_type": "opening",
                "duration_minutes": 30,
                "warn_before_seconds": 60,
                "cue_text": "宣读背景",
                "hardware_scene": '{"light":"warm_low"}',
                "active": True,
                "version": 1,
                "created_at": now,
                "updated_at": now,
            }
        ],
        "runs": [
            {
                "run_id": "run-a",
                "session_id": "s-1",
                "dm_id": "dm-a",
                "dm_name": "小北",
                "status": "pending",
                "mode": "software",
                "template_snapshot": '{"stages":[]}',
                "state_version": 0,
                "token_version": 0,
                "hardware_degraded": False,
                "access_code_hash": "hash-a",
                "created_by": "dev",
                "created_at": now,
                "updated_at": now,
            }
        ],
        "events": [
            {
                "event_id": "evt-a",
                "run_id": "run-a",
                "event_type": "run_created",
                "actor": "merchant",
                "at": now,
                "payload": '{"mode":"software"}',
                "created_at": now,
            }
        ],
    }


def _seed_new_tables(store) -> None:
    rows = _new_table_rows(NOW)
    save_rooms(coerce_frame(pd.DataFrame(rows["rooms"]), ROOMS_SPEC), store=store)
    save_dms(coerce_frame(pd.DataFrame(rows["dms"]), DMS_SPEC), store=store)
    save_dm_shifts(
        coerce_frame(pd.DataFrame(rows["shifts"]), SHIFTS_SPEC), store=store
    )
    save_assignments(
        coerce_frame(pd.DataFrame(rows["assignments"]), ASSIGNMENTS_SPEC),
        store=store,
    )
    store.save_booking_refs(
        coerce_frame(pd.DataFrame(rows["refs"]), BOOKING_REFS_SPEC)
    )
    store.save_script_stages(
        coerce_frame(pd.DataFrame(rows["stages"]), SCRIPT_STAGES_SPEC)
    )
    store.save_dm_runs(coerce_frame(pd.DataFrame(rows["runs"]), DM_RUNS_SPEC))
    store.save_dm_run_events(
        coerce_frame(pd.DataFrame(rows["events"]), DM_RUN_EVENTS_SPEC)
    )


def _plain(frame: pd.DataFrame, columns: list[str]) -> list[dict]:
    records = frame[columns].to_dict("records")
    return [
        {key: json_safe(value) for key, value in item.items()}
        for item in records
    ]


def test_new_tables_match_between_excel_and_sql(stores):
    excel, sql = stores
    _seed_new_tables(excel)
    _seed_new_tables(sql)

    cases = [
        (
            excel.load_rooms(),
            sql.load_rooms(),
            ["room_id", "name", "capacity", "buffer_minutes", "active", "notes"],
        ),
        (
            excel.load_dms(),
            sql.load_dms(),
            ["dm_id", "name", "skills", "max_sessions_per_day", "active"],
        ),
        (
            excel.load_dm_shifts(),
            sql.load_dm_shifts(),
            ["shift_id", "dm_id", "weekday", "start_minute", "end_minute", "active"],
        ),
        (
            excel.load_assignments(),
            sql.load_assignments(),
            ["assignment_id", "session_id", "resource_kind", "resource_id", "source"],
        ),
        (
            excel.load_booking_refs(),
            sql.load_booking_refs(),
            ["ref_id", "source", "external_id", "booking_id", "seats", "data_as_of"],
        ),
        (
            excel.load_script_stages(),
            sql.load_script_stages(),
            [
                "stage_id",
                "script_id",
                "seq",
                "name",
                "duration_minutes",
                "warn_before_seconds",
                "hardware_scene",
                "active",
                "version",
            ],
        ),
        (
            excel.load_dm_runs(),
            sql.load_dm_runs(),
            [
                "run_id",
                "session_id",
                "status",
                "mode",
                "template_snapshot",
                "hardware_degraded",
                "state_version",
            ],
        ),
        (
            excel.load_dm_run_events(),
            sql.load_dm_run_events(),
            ["event_id", "run_id", "event_type", "actor", "at", "payload"],
        ),
    ]
    for excel_frame, sql_frame, columns in cases:
        assert _plain(excel_frame, columns) == _plain(sql_frame, columns)

"""Excel/旧状态库迁移与导出的离线测试。"""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime, timezone

import pandas as pd
import pytest

from app.store.engine import create_database_engine, ensure_schema
from app.store.export import export_tenant
from app.store.migrate import main
from app.store.sql import SqlStore
from app.tools.frame import coerce_frame
from app.tools.frame_specs import (
    DM_RUNS_SPEC,
    DM_RUN_EVENTS_SPEC,
    SCRIPT_STAGES_SPEC,
)
from app.tools.resources import upsert_rooms
from app.tools.scripts import load_scripts, save_scripts, upsert_script
from app.tools.storage import (
    STATUS_PENDING,
    load_bookings,
    load_sessions,
    upsert_booking,
    upsert_sessions,
)
from app.web.access import AccessStore
from app.web.usage import UsageStore

UTC = timezone.utc


def _make_source(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    upsert_sessions(
        [
            {
                "store_name": "迷雾剧场",
                "script_name": "雾隐山庄",
                "script_type": "推理",
                "start_time": "2026-10-21 19:00",
                "total_seats": 6,
                "list_price": 168,
                "status": STATUS_PENDING,
            },
            {
                "store_name": "迷雾剧场",
                "script_name": "长夜将明",
                "script_type": "情感",
                "start_time": "2026-10-22 14:00",
                "total_seats": 7,
                "list_price": 188,
                "status": STATUS_PENDING,
            },
        ],
        path=source / "sessions.xlsx",
    )
    sessions = load_sessions(source / "sessions.xlsx")
    upsert_booking(
        str(sessions.iloc[0]["session_id"]),
        "阿青",
        seats=2,
        path=source / "bookings.xlsx",
    )
    upsert_script(
        script_name="雾隐山庄",
        script_type="推理",
        total_seats=6,
        duration_minutes=240,
        list_price=168,
        path=source / "scripts.xlsx",
    )
    return source


def _query(sqlite_path, sql, params=()):
    conn = sqlite3.connect(str(sqlite_path))
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def test_dry_run_writes_no_rows(tmp_path, capsys):
    source = _make_source(tmp_path)
    db = tmp_path / "migrate.sqlite3"

    code = main(
        [
            "--source",
            str(source),
            "--tenant",
            "local",
            "--sqlite",
            str(db),
            "--dry-run",
        ]
    )

    assert code == 0
    assert "dry-run" in capsys.readouterr().out
    assert _query(db, "SELECT COUNT(*) FROM sessions") == [(0,)]


def test_import_is_idempotent_and_verifies(tmp_path):
    source = _make_source(tmp_path)
    db = tmp_path / "migrate.sqlite3"
    args = ["--source", str(source), "--tenant", "local", "--sqlite", str(db)]

    assert main(args) == 0
    assert main(args) == 0

    engine = create_database_engine(sqlite_path=db)
    ensure_schema(engine)
    store = SqlStore(engine, "local")
    assert len(store.load_sessions()) == 2
    assert len(store.load_bookings()) == 1
    assert len(store.load_scripts()) == 1
    assert _query(db, "SELECT COUNT(*) FROM import_runs WHERE status='success'") == [(2,)]
    engine.dispose()


def test_force_overwrites_only_tables_present_in_source(tmp_path):
    source = _make_source(tmp_path)
    # 只保留场次文件，验证不会误删剧本库。
    (source / "bookings.xlsx").unlink()
    (source / "scripts.xlsx").unlink()
    db = tmp_path / "migrate.sqlite3"
    args = ["--source", str(source), "--tenant", "local", "--sqlite", str(db)]
    assert main(args) == 0

    engine = create_database_engine(sqlite_path=db)
    ensure_schema(engine)
    store = SqlStore(engine, "local")
    upsert_script(script_name="保留剧本", store=store)
    upsert_sessions(
        [
            {
                "store_name": "迷雾剧场",
                "script_name": "额外场次",
                "start_time": "2026-10-23 19:00",
                "total_seats": 6,
                "status": STATUS_PENDING,
            }
        ],
        store=store,
    )
    engine.dispose()

    assert main(args + ["--force"]) == 0

    engine = create_database_engine(sqlite_path=db)
    store = SqlStore(engine, "local")
    assert len(store.load_sessions()) == 2
    assert {row["script_name"] for row in store.load_scripts().to_dict("records")} == {
        "保留剧本"
    }
    engine.dispose()


def test_duplicate_script_names_abort_tenant(tmp_path, capsys):
    source = _make_source(tmp_path)
    frame = load_scripts(source / "scripts.xlsx")
    duplicate = pd.concat([frame, frame], ignore_index=True)
    duplicate["script_id"] = ["one", "two"]
    save_scripts(duplicate, source / "scripts.xlsx")

    code = main(
        [
            "--source",
            str(source),
            "--tenant",
            "dup",
            "--sqlite",
            str(tmp_path / "migrate.sqlite3"),
        ]
    )

    assert code == 1
    assert "同名不同档案" in capsys.readouterr().out


def test_state_import_preserves_approval_and_usage(tmp_path):
    old_db = tmp_path / "old_state.sqlite3"
    clock = lambda: datetime(2026, 10, 20, 4, 0, tzinfo=UTC)
    access = AccessStore(old_db, now_fn=clock, ip_hash_secret="test-ip-secret")
    created = access.create_request(
        "user@example.com", "阿青", browser_token="browser-" + "x" * 20
    )
    access.approve(created.approval_token)
    access.close()
    usage = UsageStore(old_db, daily_limit=10, now_fn=clock)
    usage.reserve()
    usage.close()

    db = tmp_path / "migrate.sqlite3"
    code = main(["--state-only", "--state", str(old_db), "--sqlite", str(db)])

    assert code == 0
    engine = create_database_engine(sqlite_path=db)
    ensure_schema(engine)
    from app.web.access_sql import DatabaseAccessStore
    from app.web.usage_sql import DatabaseUsageStore

    imported = DatabaseAccessStore(
        engine, now_fn=clock, ip_hash_secret="test-ip-secret"
    )
    record = imported.get_by_approval_token(created.approval_token)
    assert record is not None
    assert record.status == "approved"
    assert DatabaseUsageStore(engine, daily_limit=10, now_fn=clock).current() == (
        "2026-10-20",
        1,
    )
    engine.dispose()


def test_state_import_drops_legacy_ip_hashes(tmp_path):
    """旧状态库里的 IP 哈希是裸 SHA-256，导入时必须丢弃。

    这个字段只服务「同一 IP 每小时的申请上限」，窗口 1 小时；历史值对新库
    没有任何用处，留着却能被枚举 IPv4 反推地址。
    """
    old_db = tmp_path / "old_state.sqlite3"
    clock = lambda: datetime(2026, 10, 20, 4, 0, tzinfo=UTC)
    access = AccessStore(old_db, now_fn=clock, ip_hash_secret="test-ip-secret")
    created = access.create_request(
        "user@example.com",
        "阿青",
        browser_token="browser-" + "x" * 20,
        requester_ip="203.0.113.7",
    )
    # 模拟这次更新之前写下的行：不带密钥的裸 SHA-256。
    with access._conn:
        access._conn.execute(
            "UPDATE access_requests SET requester_ip_hash = ?",
            (hashlib.sha256(b"203.0.113.7").hexdigest(),),
        )
    access.close()

    db = tmp_path / "migrate.sqlite3"
    assert main(["--state-only", "--state", str(old_db), "--sqlite", str(db)]) == 0

    engine = create_database_engine(sqlite_path=db)
    ensure_schema(engine)
    from app.web.access_sql import DatabaseAccessStore

    imported = DatabaseAccessStore(
        engine, now_fn=clock, ip_hash_secret="test-ip-secret"
    )
    record = imported.get_by_approval_token(created.approval_token)
    assert record is not None
    assert record.requester_ip_hash is None
    engine.dispose()


def test_export_roundtrip(tmp_path):
    source = _make_source(tmp_path)
    db = tmp_path / "migrate.sqlite3"
    assert main(["--source", str(source), "--sqlite", str(db)]) == 0

    out = tmp_path / "exported"
    counts = export_tenant(
        database_url=None,
        sqlite_path=db,
        tenant_id="local",
        out_dir=out,
    )

    expected = {"sessions": 2, "bookings": 1, "scripts": 1}
    assert {name: counts[name] for name in expected} == expected
    assert len(counts) == 12
    for filename in (
        "rooms.xlsx",
        "dms.xlsx",
        "dm_shifts.xlsx",
        "dm_leaves.xlsx",
        "assignments.xlsx",
        "booking_intake_refs.xlsx",
        "script_stages.xlsx",
        "dm_runs.xlsx",
        "dm_run_events.xlsx",
    ):
        assert (out / filename).exists()
    assert len(load_sessions(out / "sessions.xlsx")) == 2
    assert len(load_bookings(out / "bookings.xlsx")) == 1
    assert len(load_scripts(out / "scripts.xlsx")) == 1


def test_sidecar_roundtrip(tmp_path):
    from app.store.migrate import _import_one

    source = _make_source(tmp_path)
    db = tmp_path / "migrate.sqlite3"
    assert main(["--source", str(source), "--sqlite", str(db)]) == 0

    engine = create_database_engine(sqlite_path=db)
    ensure_schema(engine)
    store = SqlStore(engine, "local")
    upsert_rooms(
        [{"name": "A 房", "capacity": 6, "buffer_minutes": 15}],
        store=store,
        now=datetime(2026, 10, 21, 12, 0),
    )
    with store.write_lock():
        stages = coerce_frame(
            pd.DataFrame(
                [
                    {
                        "stage_id": "stage-abc",
                        "script_id": "script-abc",
                        "script_name": "雾隐山庄",
                        "seq": 1,
                        "name": "开场",
                        "duration_minutes": 30,
                        "warn_before_seconds": 60,
                        "active": True,
                        "version": 1,
                        "created_at": "2026-10-21 12:00",
                        "updated_at": "2026-10-21 12:00",
                    }
                ]
            ),
            SCRIPT_STAGES_SPEC,
        )
        store.save_script_stages(stages)
    engine.dispose()

    out = tmp_path / "exported"
    counts = export_tenant(
        database_url=None,
        sqlite_path=db,
        tenant_id="local",
        out_dir=out,
    )
    assert counts["rooms"] == 1
    assert counts["script_stages"] == 1

    target_db = tmp_path / "target.sqlite3"
    target_engine = create_database_engine(sqlite_path=target_db)
    ensure_schema(target_engine)
    result = _import_one(
        target_engine,
        out,
        "local",
        email_hash=None,
        force=True,
        dry_run=False,
    )
    assert result["rows"]["rooms"] == 1
    target = SqlStore(target_engine, "local")
    assert target.load_rooms().iloc[0]["name"] == "A 房"
    assert target.load_script_stages().iloc[0]["stage_id"] == "stage-abc"
    target_engine.dispose()


def test_export_import_keeps_json_text_fields(tmp_path):
    from app.store.migrate import _import_one

    db = tmp_path / "json.sqlite3"
    engine = create_database_engine(sqlite_path=db)
    ensure_schema(engine)
    store = SqlStore(engine, "local")
    store.ensure_tenant()
    moment = pd.Timestamp("2026-10-21 12:00")
    with store.write_lock():
        store.save_script_stages(
            coerce_frame(
                pd.DataFrame(
                    [
                        {
                            "stage_id": "stage-json",
                            "script_id": "script-json",
                            "script_name": "雾隐山庄",
                            "seq": 1,
                            "name": "开场",
                            "duration_minutes": 30,
                            "warn_before_seconds": 60,
                            "hardware_scene": '{"light":"warm_low","sound":"intro_01"}',
                            "active": True,
                            "version": 1,
                            "created_at": moment,
                            "updated_at": moment,
                        }
                    ]
                ),
                SCRIPT_STAGES_SPEC,
            )
        )
        store.save_dm_runs(
            coerce_frame(
                pd.DataFrame(
                    [
                        {
                            "run_id": "run-json",
                            "session_id": "s-1",
                            "status": "pending",
                            "mode": "hardware",
                            "template_snapshot": '{"stages":[{"seq":1,"name":"开场"}]}',
                            "state_version": 0,
                            "token_version": 0,
                            "hardware_degraded": False,
                            "access_code_hash": "hash-json",
                            "created_by": "dev",
                            "created_at": moment,
                            "updated_at": moment,
                        }
                    ]
                ),
                DM_RUNS_SPEC,
            )
        )
        store.save_dm_run_events(
            coerce_frame(
                pd.DataFrame(
                    [
                        {
                            "event_id": "evt-json",
                            "run_id": "run-json",
                            "event_type": "hardware_failed",
                            "actor": "system",
                            "at": moment,
                            "payload": '{"status":"failed","latency_ms":2000}',
                            "created_at": moment,
                        }
                    ]
                ),
                DM_RUN_EVENTS_SPEC,
            )
        )
    engine.dispose()

    out = tmp_path / "exported_json"
    counts = export_tenant(
        database_url=None,
        sqlite_path=db,
        tenant_id="local",
        out_dir=out,
    )
    assert counts["script_stages"] == 1
    assert counts["dm_runs"] == 1
    assert counts["dm_run_events"] == 1

    target_db = tmp_path / "json_target.sqlite3"
    target_engine = create_database_engine(sqlite_path=target_db)
    ensure_schema(target_engine)
    result = _import_one(
        target_engine,
        out,
        "local",
        email_hash=None,
        force=True,
        dry_run=False,
    )
    assert result["rows"]["script_stages"] == 1
    target = SqlStore(target_engine, "local")
    stage = target.load_script_stages().iloc[0]
    run = target.load_dm_runs().iloc[0]
    event = target.load_dm_run_events().iloc[0]
    assert stage["hardware_scene"] == '{"light":"warm_low","sound":"intro_01"}'
    assert run["template_snapshot"] == '{"stages":[{"seq":1,"name":"开场"}]}'
    assert event["payload"] == '{"status":"failed","latency_ms":2000}'
    target_engine.dispose()


def test_old_three_file_backup_treats_missing_sidecars_as_empty(tmp_path):
    from app.store.migrate import _import_one

    source = _make_source(tmp_path)
    target_db = tmp_path / "target.sqlite3"
    target_engine = create_database_engine(sqlite_path=target_db)
    ensure_schema(target_engine)
    existing = SqlStore(target_engine, "local")
    existing.ensure_tenant()
    upsert_rooms(
        [{"name": "保留房", "capacity": 6}],
        store=existing,
        now=datetime(2026, 10, 21, 12, 0),
    )

    result = _import_one(
        target_engine,
        source,
        "local",
        email_hash=None,
        force=True,
        dry_run=False,
    )
    # 旧备份只含三张原表：新表既不被清空，也不报错。
    assert result["rows"]["sessions"] == 2
    for name in (
        "rooms",
        "dms",
        "dm_shifts",
        "assignments",
        "booking_refs",
        "script_stages",
        "dm_runs",
        "dm_run_events",
    ):
        assert name not in result["rows"]
    rooms = existing.load_rooms()
    assert len(rooms) == 1
    assert rooms.iloc[0]["name"] == "保留房"

    # 全新的租户导入旧备份：新表按空表处理。
    fresh = _import_one(
        target_engine,
        source,
        "fresh-tenant",
        email_hash=None,
        force=True,
        dry_run=False,
    )
    assert fresh["rows"]["sessions"] == 2
    fresh_store = SqlStore(target_engine, "fresh-tenant")
    assert fresh_store.load_rooms().empty
    assert fresh_store.load_script_stages().empty
    assert fresh_store.load_dm_runs().empty
    target_engine.dispose()

"""SQLite Store 的隔离、空值、整表替换与租户碰撞测试。"""

from __future__ import annotations

from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.store.engine import create_database_engine, ensure_schema
from app.store.schema import sessions, tenants
from app.store.sql import SqlStore, TenantConflictError
from app.tools.frame import coerce_frame
from app.tools.frame_specs import (
    DM_RUNS_SPEC,
    DM_RUN_EVENTS_SPEC,
    SCRIPT_STAGES_SPEC,
)
from app.tools.resources import ROOMS_SPEC, save_rooms
from app.tools.scripts import (
    load_scripts,
    save_scripts,
    upsert_script,
)
from app.tools.storage import (
    load_bookings,
    load_sessions,
    load_sessions_with_counts,
    save_bookings,
    save_sessions,
    upsert_booking,
    upsert_sessions,
)


@pytest.fixture()
def engine(tmp_path):
    value = create_database_engine(sqlite_path=tmp_path / "store.sqlite3")
    ensure_schema(value)
    yield value
    value.dispose()


def _store(engine, tenant_id="test-tenant") -> SqlStore:
    store = SqlStore(engine, tenant_id)
    store.ensure_tenant()
    return store


def _session_row(**overrides):
    row = {
        "session_id": "s-1",
        "store_name": "迷雾剧场",
        "script_name": "雾隐山庄",
        "script_type": "推理",
        "start_time": "2026-10-21 19:00",
        "total_seats": 6,
        "list_price": 168,
        "status": "待开场",
        "source_text": "测试",
    }
    row.update(overrides)
    return row


def test_sessions_bookings_roundtrip(engine):
    store = _store(engine)

    upsert_sessions([_session_row()], store=store)
    upsert_booking("s-1", "阿青", seats=2, now=pd.Timestamp("2026-10-20 12:00"), store=store)

    sessions_frame = load_sessions_with_counts(store=store)
    assert len(sessions_frame) == 1
    assert sessions_frame.iloc[0]["booked_seats"] == 2
    assert sessions_frame.iloc[0]["remaining_seats"] == 4
    assert sessions_frame.iloc[0]["list_price"] == 168

    bookings = load_bookings(store=store)
    assert len(bookings) == 1
    assert bookings.iloc[0]["player_name"] == "阿青"


def test_script_profile_roundtrip(engine):
    store = _store(engine)

    result = upsert_script(
        script_name="雾隐山庄",
        aliases="雾隐 / WYSZ",
        script_type="推理",
        total_seats=6,
        duration_minutes=240,
        list_price=168,
        store=store,
    )

    assert result["profile_status"] == "已完整"
    scripts = load_scripts(store=store)
    assert len(scripts) == 1
    assert scripts.iloc[0]["script_name"] == "雾隐山庄"
    assert "WYSZ" in scripts.iloc[0]["aliases"]


def test_partial_script_can_be_completed_later(engine):
    store = _store(engine)

    first = upsert_script(script_name="雾隐", store=store)
    assert first["profile_status"] == "待补全"

    second = upsert_script(
        script_id=first["script_id"],
        script_name="雾隐山庄",
        total_seats=6,
        duration_minutes=240,
        list_price=168,
        script_type="推理",
        store=store,
    )
    assert second["profile_status"] == "已完整"
    assert len(load_scripts(store=store)) == 1


def test_tenants_are_isolated(engine):
    first = _store(engine, "test-tenant-a")
    second = _store(engine, "test-tenant-b")

    upsert_sessions([_session_row()], store=first)

    assert len(load_sessions(store=first)) == 1
    assert load_sessions(store=second).empty
    assert load_bookings(store=second).empty
    assert load_scripts(store=second).empty


def test_missing_values_become_null_and_zero_is_kept(engine):
    store = _store(engine)
    upsert_sessions(
        [
            _session_row(
                session_id="s-null",
                script_type=pd.NA,
                total_seats=np.nan,
                list_price=None,
                source_text=pd.NaT,
            ),
            _session_row(session_id="s-zero", list_price=0, total_seats=0),
        ],
        store=store,
    )

    rows = load_sessions(store=store).set_index("session_id")
    assert pd.isna(rows.loc["s-null", "script_type"])
    assert pd.isna(rows.loc["s-null", "total_seats"])
    assert pd.isna(rows.loc["s-null", "list_price"])
    assert rows.loc["s-zero", "list_price"] == 0
    assert rows.loc["s-zero", "total_seats"] == 0


def test_save_replaces_whole_tenant_table(engine):
    store = _store(engine)
    upsert_sessions([_session_row(session_id="s-1"), _session_row(session_id="s-2")], store=store)

    save_sessions(load_sessions(store=store).head(1), store=store)

    remaining = load_sessions(store=store)
    assert len(remaining) == 1
    assert remaining.iloc[0]["session_id"] == "s-1"


def test_save_rejects_duplicate_primary_keys(engine):
    store = _store(engine)
    upsert_sessions([_session_row()], store=store)
    frame = pd.concat([load_sessions(store=store), load_sessions(store=store)])

    with pytest.raises(ValueError, match="重复主键"):
        save_sessions(frame, store=store)


def test_duplicate_script_name_is_rejected_by_unique_index(engine):
    store = _store(engine)
    upsert_script(script_name="雾隐山庄", store=store)
    frame = load_scripts(store=store)
    duplicate = pd.concat([frame, frame], ignore_index=True)
    duplicate["script_id"] = ["one", "two"]

    with pytest.raises(IntegrityError):
        save_scripts(duplicate, store=store)


def test_booking_cancel_and_counts(engine):
    store = _store(engine)
    upsert_sessions([_session_row()], store=store)
    upsert_booking("s-1", "阿青", seats=2, store=store)

    from app.tools.storage import cancel_booking

    assert cancel_booking("s-1", "阿青", store=store) is True
    assert load_sessions_with_counts(store=store).iloc[0]["booked_seats"] == 0


def test_hash_prefix_collision_is_rejected(engine):
    prefix = "a" * 16
    first_hash = prefix + "0" * 48
    second_hash = prefix + "1" * 48
    store = SqlStore(engine, prefix)

    store.ensure_tenant(email_hash=first_hash)
    with pytest.raises(TenantConflictError):
        store.ensure_tenant(email_hash=second_hash)

    with engine.connect() as conn:
        row = conn.execute(
            select(tenants.c.email_hash).where(tenants.c.tenant_id == prefix)
        ).first()
    assert row.email_hash == first_hash


def test_schema_tables_exist(engine):
    with engine.connect() as conn:
        row = conn.execute(select(sessions.c.session_id)).first()
    assert row is None


def test_seeded_marker(engine):
    store = _store(engine)
    assert store.is_seeded() is False
    store.mark_seeded(datetime(2026, 10, 20, tzinfo=timezone.utc))
    assert store.is_seeded() is True


def test_save_bookings_ignores_derived_columns(engine):
    store = _store(engine)
    upsert_sessions([_session_row()], store=store)
    upsert_booking("s-1", "阿青", store=store)
    frame = load_bookings(store=store)
    frame["derived_status"] = "任意"

    save_bookings(frame, store=store)
    assert len(load_bookings(store=store)) == 1


NOW = pd.Timestamp("2026-10-20 12:00")


def _seed_new_tables(store, tag: str = "a") -> None:
    save_rooms(
        coerce_frame(
            pd.DataFrame(
                [
                    {
                        "room_id": f"room-{tag}",
                        "name": f"{tag} 房",
                        "capacity": 6,
                        "buffer_minutes": 15,
                        "active": True,
                        "created_at": NOW,
                        "updated_at": NOW,
                    }
                ]
            ),
            ROOMS_SPEC,
        ),
        store=store,
    )
    store.save_script_stages(
        coerce_frame(
            pd.DataFrame(
                [
                    {
                        "stage_id": f"stage-{tag}",
                        "script_id": "script-a",
                        "script_name": "雾隐山庄",
                        "seq": 1,
                        "name": "开场",
                        "duration_minutes": 30,
                        "warn_before_seconds": 60,
                        "active": True,
                        "version": 1,
                        "created_at": NOW,
                        "updated_at": NOW,
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
                        "run_id": f"run-{tag}",
                        "session_id": "s-1",
                        "status": "pending",
                        "mode": "software",
                        "access_code_hash": f"hash-{tag}",
                        "created_by": "dev",
                        "created_at": NOW,
                        "updated_at": NOW,
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
                        "event_id": f"evt-{tag}",
                        "run_id": f"run-{tag}",
                        "event_type": "run_created",
                        "actor": "merchant",
                        "at": NOW,
                        "created_at": NOW,
                    }
                ]
            ),
            DM_RUN_EVENTS_SPEC,
        )
    )


def test_new_tables_are_tenant_isolated_and_replaceable(engine):
    first = _store(engine, "tenant-a")
    second = _store(engine, "tenant-b")
    _seed_new_tables(first, "a")
    _seed_new_tables(second, "b")

    assert first.load_rooms().iloc[0]["room_id"] == "room-a"
    assert second.load_rooms().iloc[0]["room_id"] == "room-b"
    assert {row["run_id"] for row in first.load_dm_runs().to_dict("records")} == {
        "run-a"
    }
    assert {row["run_id"] for row in second.load_dm_runs().to_dict("records")} == {
        "run-b"
    }
    assert len(first.load_script_stages()) == 1
    assert len(second.load_dm_run_events()) == 1

    # 整表替换只影响当前租户。
    save_rooms(first.load_rooms().head(0), store=first)
    assert first.load_rooms().empty
    assert len(second.load_rooms()) == 1


def test_stages_and_runs_survive_reopen(tmp_path):
    path = tmp_path / "persist.sqlite3"
    engine = create_database_engine(sqlite_path=path)
    ensure_schema(engine)
    store = SqlStore(engine, "tenant-persist")
    store.ensure_tenant()
    _seed_new_tables(store, "p")
    engine.dispose()

    reopened = create_database_engine(sqlite_path=path)
    ensure_schema(reopened)
    again = SqlStore(reopened, "tenant-persist")
    assert again.load_script_stages().iloc[0]["stage_id"] == "stage-p"
    assert again.load_dm_runs().iloc[0]["run_id"] == "run-p"
    assert again.load_dm_run_events().iloc[0]["event_id"] == "evt-p"
    reopened.dispose()


class TestSqlPublicLibrary:
    """公共库 SQL 后端：load/merge 语义与 Excel 后端一致。"""

    @pytest.fixture(autouse=True)
    def _reset_guard(self):
        from app.store.public_sql import reset_sql_seed_guard
        from app.tools.public_library import reset_seed_guard

        reset_sql_seed_guard()
        reset_seed_guard()
        yield
        reset_sql_seed_guard()
        reset_seed_guard()

    @staticmethod
    def _row(**overrides):
        from app.tools.public_library import make_public_script_id

        row = {
            "script_id": make_public_script_id("九霄"),
            "script_name": "九霄",
            "script_type": "情感",
            "total_seats": 7,
            "duration_minutes": 270,
            "origin": "策展",
        }
        row.update(overrides)
        return row

    def test_merge_add_skip_update(self, engine):
        from app.tools.public_library import coerce_public_scripts

        from app.store.public_sql import SqlPublicLibrary

        library = SqlPublicLibrary(engine)
        assert library.load().empty

        incoming = coerce_public_scripts(pd.DataFrame([self._row()]))
        stats, errors = library.merge(incoming)
        assert stats["added"] == 1 and not errors
        loaded = library.load()
        assert len(loaded) == 1
        assert str(loaded.iloc[0]["script_name"]) == "九霄"
        assert str(loaded.iloc[0]["script_id"]).startswith("pub-")

        stats2, _ = library.merge(incoming)
        assert stats2["skipped"] == 1

        # 补空 list_price → updated；数据库往返后值仍在
        fill = coerce_public_scripts(pd.DataFrame([self._row(list_price=328)]))
        stats3, _ = library.merge(fill)
        assert stats3["updated"] == 1
        reloaded = library.load()
        assert int(reloaded.iloc[0]["list_price"]) == 328
        assert "策展" in str(reloaded.iloc[0]["origin"])

    def test_merge_missing_origin_to_errors(self, engine):
        from app.tools.public_library import coerce_public_scripts

        from app.store.public_sql import SqlPublicLibrary

        library = SqlPublicLibrary(engine)
        incoming = coerce_public_scripts(pd.DataFrame([self._row(origin="")]))
        stats, errors = library.merge(incoming)
        assert stats["added"] == 0
        assert len(errors) == 1
        assert library.load().empty

    def test_ensure_seed_once(self, engine):
        from app.store.public_sql import SqlPublicLibrary

        library = SqlPublicLibrary(engine)
        result = library.ensure_seed()
        assert result["seeded"] is True
        seeded = library.load()
        assert len(seeded) >= 8

        result2 = library.ensure_seed()
        assert result2["seeded"] is False
        assert len(library.load()) == len(seeded)

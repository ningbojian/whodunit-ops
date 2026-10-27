"""数据库工作区：播种一次、不覆盖旧数据、并发与租户碰撞。"""

from __future__ import annotations

import threading
from datetime import datetime, timezone

import pandas as pd
import pytest

from app.store.engine import create_database_engine, ensure_schema
from app.store.public_sql import reset_sql_seed_guard
from app.store.sql import SqlStore
from app.store.workspace import (
    DatabaseWorkspaceManager,
    TenantCollisionError,
    TenantWorkspaceError,
)
from app.tools.scripts import load_scripts, script_to_dict
from app.tools.storage import (
    STATUS_PENDING,
    load_bookings,
    load_sessions,
    load_sessions_with_counts,
    upsert_sessions,
)

UTC = timezone.utc


@pytest.fixture()
def engine(tmp_path):
    value = create_database_engine(sqlite_path=tmp_path / "workspace.sqlite3")
    ensure_schema(value)
    reset_sql_seed_guard()
    yield value
    reset_sql_seed_guard()
    value.dispose()


def _manager(engine, *, now=None):
    return DatabaseWorkspaceManager(
        engine=engine,
        default_store="迷雾剧场",
        now_fn=now or (lambda: datetime(2026, 10, 20, 4, 0, tzinfo=UTC)),
    )


def test_seeds_four_sessions_two_bookings_and_four_scripts(engine):
    workspace = _manager(engine).ensure("a" * 64)

    sessions = load_sessions_with_counts(store=workspace.store)
    assert len(sessions) == 4
    assert set(sessions["script_name"]) == {"雾隐山庄", "长夜将明", "第七码头", "山鬼"}
    assert set(sessions["status"]) == {STATUS_PENDING}
    assert int(sessions["booked_seats"].sum()) == 3

    bookings = load_bookings(store=workspace.store)
    assert len(bookings) == 2

    scripts = load_scripts(store=workspace.store)
    assert len(scripts) == 4
    assert all(
        script_to_dict(row)["profile_status"] == "已完整"
        for _, row in scripts.iterrows()
    )
    assert workspace.store.is_seeded() is True


def test_seeded_dates_are_future_store_local_dates(engine):
    workspace = _manager(engine).ensure("b" * 64)
    sessions = load_sessions(store=workspace.store)
    starts = pd.to_datetime(sessions["start_time"])

    assert starts.min() >= pd.Timestamp("2026-10-20 12:00")
    assert starts.max() <= pd.Timestamp("2026-10-23 23:59")


def test_ensure_is_idempotent(engine):
    manager = _manager(engine)
    first = manager.ensure("c" * 64)
    manager.ensure("c" * 64)

    assert len(load_sessions(store=first.store)) == 4
    assert len(load_bookings(store=first.store)) == 2


def test_existing_data_is_not_overwritten(engine):
    manager = _manager(engine)
    digest = "d" * 64
    # 模拟旧数据已导入、但 seeded_at 尚未标记。
    existing = SqlStore(engine, digest[:16])
    existing.ensure_tenant(email_hash=digest)
    upsert_sessions(
        [
            {
                "store_name": "迷雾剧场",
                "script_name": "老数据",
                "script_type": "推理",
                "start_time": "2026-10-21 19:00",
                "total_seats": 6,
                "list_price": 168,
                "status": STATUS_PENDING,
            }
        ],
        store=existing,
    )

    returned = manager.ensure(digest)

    sessions = load_sessions(store=returned.store)
    assert len(sessions) == 1
    assert sessions.iloc[0]["script_name"] == "老数据"
    assert returned.store.is_seeded() is True


def test_two_emails_get_separate_tenants(engine):
    manager = _manager(engine)

    first = manager.ensure("1" * 64)
    second = manager.ensure("2" * 64)

    assert first.tenant_id != second.tenant_id
    assert len(load_sessions(store=first.store)) == 4
    assert len(load_sessions(store=second.store)) == 4
    upsert_sessions(
        [
            {
                "store_name": "迷雾剧场",
                "script_name": "只属于第一家",
                "start_time": "2026-10-21 19:00",
                "total_seats": 6,
                "status": STATUS_PENDING,
            }
        ],
        store=first.store,
    )
    assert "只属于第一家" not in set(load_sessions(store=second.store)["script_name"])


def test_invalid_hash_rejected(engine):
    manager = _manager(engine)

    with pytest.raises(TenantWorkspaceError):
        manager.ensure("short")


def test_hash_prefix_collision_rejected(engine):
    manager = _manager(engine)
    prefix = "f" * 16

    manager.ensure(prefix + "0" * 48)
    with pytest.raises(TenantCollisionError):
        manager.ensure(prefix + "1" * 48)


def test_concurrent_ensure_seeds_only_once(engine):
    manager = _manager(engine)
    digest = "e" * 64
    errors: list[Exception] = []

    def worker():
        try:
            manager.ensure(digest)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(10)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    workspace = manager.ensure(digest)
    assert len(load_sessions(store=workspace.store)) == 4
    assert len(load_bookings(store=workspace.store)) == 2
    assert len(load_scripts(store=workspace.store)) == 4


def test_ensure_seeds_public_library_once(engine):
    """首次 ensure 同时灌公共库种子（全局表，无 tenant_id）；二次为 no-op。"""
    from sqlalchemy import func, select

    from app.store.public_sql import SqlPublicLibrary
    from app.store.schema import public_scripts

    manager = _manager(engine)
    workspace = manager.ensure("a" * 64)

    library = SqlPublicLibrary(engine)
    first = library.load()
    assert len(first) >= 8
    assert first["script_name"].astype(str).str.startswith("雾隐").sum() == 0

    with engine.connect() as conn:
        count = conn.execute(select(func.count()).select_from(public_scripts)).scalar()
    assert count == len(first)

    # 二次 ensure：种子守卫 + 非空检查，不重复灌入
    manager.ensure("b" * 64)
    assert len(SqlPublicLibrary(engine).load()) == len(first)
    assert workspace.tenant_id == "a" * 16

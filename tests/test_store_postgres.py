"""可选 Postgres 契约测试。

只有设置 TEST_DATABASE_URL 时才运行；必须使用专用测试库或 Neon 分支，
不要指向生产 DATABASE_URL：

  TEST_DATABASE_URL='postgresql+psycopg://...' \
    python -m pytest -q tests/test_store_postgres.py
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone

import pandas as pd
import pytest
from sqlalchemy import delete

from app.store.engine import create_database_engine, ensure_schema
from app.store.schema import access_requests, bookings, scripts, sessions, tenants
from app.store.sql import SqlStore
from app.tools.scripts import load_scripts, upsert_script
from app.tools.storage import (
    STATUS_PENDING,
    load_sessions_with_counts,
    upsert_booking,
    upsert_sessions,
)
from app.web.access_sql import DatabaseAccessStore
from app.web.usage_sql import DatabaseUsageStore

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(
        not TEST_DATABASE_URL, reason="未设置 TEST_DATABASE_URL，跳过 Postgres 测试"
    ),
]

UTC = timezone.utc


@pytest.fixture()
def engine():
    value = create_database_engine(database_url=TEST_DATABASE_URL)
    ensure_schema(value)
    yield value
    value.dispose()


@pytest.fixture()
def tenant_id():
    value = "test-" + uuid.uuid4().hex[:12]
    yield value
    # 引擎在每个测试里独立 dispose，这里用独立引擎清理。
    cleanup = create_database_engine(database_url=TEST_DATABASE_URL)
    try:
        with cleanup.begin() as conn:
            for table in (bookings, scripts, sessions):
                conn.execute(delete(table).where(table.c.tenant_id == value))
            conn.execute(delete(tenants).where(tenants.c.tenant_id == value))
    finally:
        cleanup.dispose()


def test_postgres_roundtrip(engine, tenant_id):
    store = SqlStore(engine, tenant_id)
    store.ensure_tenant()
    upsert_sessions(
        [
            {
                "store_name": "迷雾剧场",
                "script_name": "雾隐山庄",
                "start_time": "2026-10-21 19:00",
                "total_seats": 6,
                "list_price": 168,
                "status": STATUS_PENDING,
            }
        ],
        store=store,
    )
    session_id = str(load_sessions_with_counts(store=store).iloc[0]["session_id"])
    upsert_booking(session_id, "阿青", seats=2, store=store)

    frame = load_sessions_with_counts(store=store)
    assert len(frame) == 1
    assert frame.iloc[0]["booked_seats"] == 2
    assert frame.iloc[0]["remaining_seats"] == 4
    assert frame.iloc[0]["list_price"] == 168

    upsert_script(script_name="雾隐山庄", total_seats=6, store=store)
    assert len(load_scripts(store=store)) == 1


def test_postgres_state_stores(engine):
    clock = lambda: datetime(2026, 10, 20, 4, 0, tzinfo=UTC)
    email = f"user-{uuid.uuid4().hex}@example.com"
    access = DatabaseAccessStore(
        engine, now_fn=clock, ip_hash_secret="test-ip-secret"
    )
    try:
        created = access.create_request(
            email,
            None,
            browser_token="browser-" + "x" * 20,
        )
        approved = access.approve(created.approval_token)
        assert (
            access.validate_access_token(approved.access_token).id
            == created.request.id
        )

        usage = DatabaseUsageStore(engine, daily_limit=1000, now_fn=clock)
        before = usage.current()[1]
        assert usage.reserve().used == before + 1
    finally:
        with engine.begin() as conn:
            conn.execute(delete(access_requests).where(access_requests.c.email == email))

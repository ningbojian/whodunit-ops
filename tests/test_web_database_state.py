"""数据库版审批/用量存储：生命周期、并发批准、每日上限与持久化。"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

import pytest

from app.store.engine import create_database_engine, ensure_schema
from app.web.access import (
    AccessExpired,
    AccessNotReady,
    AlreadyProcessed,
    ApprovalTokenExpired,
)
from app.web.access_sql import DatabaseAccessStore
from app.web.usage_sql import DatabaseUsageStore

UTC = timezone.utc
BROWSER = "browser-token-" + "x" * 24


class Clock:
    def __init__(self, start=None):
        self.current = start or datetime(2026, 10, 20, 4, 0, tzinfo=UTC)

    def now(self):
        return self.current

    def advance(self, **kwargs):
        self.current += timedelta(**kwargs)
        return self.current


@pytest.fixture()
def engine(tmp_path):
    value = create_database_engine(sqlite_path=tmp_path / "state.sqlite3")
    ensure_schema(value)
    yield value
    value.dispose()


def _store(engine, clock):
    return DatabaseAccessStore(engine, now_fn=clock.now)


def test_request_approve_claim_lifecycle(engine):
    clock = Clock()
    store = _store(engine, clock)

    created = store.create_request(
        "User@Example.com",
        "阿青",
        browser_token=BROWSER,
    )
    assert created.request.status == "pending"
    assert store.get_by_approval_token(created.approval_token).id == created.request.id
    assert store.status_for_request(created.request.id) == "pending"

    with pytest.raises(AccessNotReady):
        store.claim(created.request.id, BROWSER)

    approved = store.approve(created.approval_token, access_hours=8)
    assert approved.request.status == "approved"
    assert approved.request.access_expires_at == clock.now() + timedelta(hours=8)
    assert store.get_by_access_token(approved.access_token).id == created.request.id

    claimed = store.claim(created.request.id, BROWSER)
    assert claimed.id == created.request.id
    assert store.validate_access_token(approved.access_token).id == created.request.id


def test_duplicate_approval_is_rejected(engine):
    clock = Clock()
    store = _store(engine, clock)
    created = store.create_request("a@example.com", None, browser_token=BROWSER)

    store.approve(created.approval_token)
    with pytest.raises(AlreadyProcessed):
        store.approve(created.approval_token)


def test_expired_approval_is_marked_expired(engine):
    clock = Clock()
    store = _store(engine, clock)
    created = store.create_request(
        "b@example.com", None, browser_token=BROWSER, approval_ttl_hours=1
    )
    clock.advance(hours=2)

    with pytest.raises(ApprovalTokenExpired):
        store.approve(created.approval_token)

    assert store.status_for_request(created.request.id) == "expired"


def test_access_expires_after_eight_hours(engine):
    clock = Clock()
    store = _store(engine, clock)
    created = store.create_request("c@example.com", None, browser_token=BROWSER)
    approved = store.approve(created.approval_token, access_hours=8)
    clock.advance(hours=8, seconds=1)

    with pytest.raises(AccessExpired):
        store.validate_access_token(approved.access_token)
    with pytest.raises(AccessExpired):
        store.claim(created.request.id, BROWSER)
    assert store.status_for_request(created.request.id) == "expired"


def test_reject(engine):
    clock = Clock()
    store = _store(engine, clock)
    created = store.create_request("d@example.com", None, browser_token=BROWSER)

    rejected = store.reject(created.approval_token)

    assert rejected.status == "rejected"
    assert store.status_for_request(created.request.id) == "rejected"
    with pytest.raises(AlreadyProcessed):
        store.approve(created.approval_token)


def test_duplicate_pending_request_returns_cooldown(engine):
    clock = Clock()
    store = _store(engine, clock)
    created = store.create_request("e@example.com", None, browser_token=BROWSER)

    with pytest.raises(Exception) as caught:
        store.create_request("e@example.com", None, browser_token=BROWSER)
    assert caught.value.__class__.__name__ == "RequestCooldown"


def test_state_persists_across_store_instances(engine):
    clock = Clock()
    first = _store(engine, clock)
    created = first.create_request("f@example.com", None, browser_token=BROWSER)
    first.approve(created.approval_token)

    second = _store(engine, clock)
    assert second.get_request(created.request.id).status == "approved"


def test_concurrent_approval_only_succeeds_once(engine):
    clock = Clock()
    store = _store(engine, clock)
    created = store.create_request("g@example.com", None, browser_token=BROWSER)
    results = []
    errors = []

    def worker():
        try:
            results.append(store.approve(created.approval_token))
        except AlreadyProcessed as exc:
            errors.append(exc)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(results) == 1
    assert len(errors) == 7
    assert all(isinstance(item, AlreadyProcessed) for item in errors)


def test_cleanup_removes_old_rows(engine):
    clock = Clock()
    store = _store(engine, clock)
    store.create_request("h@example.com", None, browser_token=BROWSER)
    clock.advance(days=31)

    assert store.cleanup(retention_days=30) == 1


def test_usage_reserve_and_limit(engine):
    clock = Clock()
    usage = DatabaseUsageStore(engine, daily_limit=3, now_fn=clock.now)

    assert usage.reserve().used == 1
    assert usage.reserve().used == 2
    assert usage.reserve().used == 3
    with pytest.raises(Exception) as caught:
        usage.reserve()
    assert caught.value.__class__.__name__ == "UsageLimitReached"
    assert usage.current() == ("2026-10-20", 3)


def test_usage_persists_and_resets_next_day(engine):
    clock = Clock()
    usage = DatabaseUsageStore(engine, daily_limit=2, now_fn=clock.now)
    usage.reserve()

    second = DatabaseUsageStore(engine, daily_limit=2, now_fn=clock.now)
    assert second.current()[1] == 1
    clock.advance(days=1)

    assert second.reserve().used == 1
    assert second.current() == ("2026-10-21", 1)


def test_usage_concurrent_reservations_do_not_exceed_limit(engine):
    clock = Clock()
    usage = DatabaseUsageStore(engine, daily_limit=5, now_fn=clock.now)
    successes = []
    failures = []

    def worker():
        try:
            successes.append(usage.reserve())
        except Exception as exc:  # noqa: BLE001
            failures.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(20)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(successes) == 5
    assert len(failures) == 15
    assert usage.current()[1] == 5

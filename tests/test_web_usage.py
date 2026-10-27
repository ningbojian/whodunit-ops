"""全站每日模型轮数计数器的离线测试。"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

import pytest

from app.web.usage import (
    UsageError,
    UsageLimitReached,
    UsageStore,
    cst_day_key,
)
from tests.web_helpers import MutableClock

UTC = timezone.utc
CST = timezone(timedelta(hours=8))


def test_reserve_increments_and_returns_remaining(tmp_path):
    clock = MutableClock(datetime(2026, 10, 20, 4, 0, tzinfo=UTC))
    store = UsageStore(tmp_path / "usage.db", daily_limit=3, now_fn=clock.now)

    first = store.reserve()
    second = store.reserve()

    assert first.day == "2026-10-20"
    assert first.used == 1
    assert first.remaining == 2
    assert second.used == 2
    assert second.remaining == 1
    assert store.current() == ("2026-10-20", 2)
    store.close()


def test_limit_reached_does_not_increment(tmp_path):
    clock = MutableClock()
    store = UsageStore(tmp_path / "usage.db", daily_limit=2, now_fn=clock.now)
    store.reserve()
    store.reserve()

    with pytest.raises(UsageLimitReached) as caught:
        store.reserve()

    assert caught.value.used == 2
    assert caught.value.limit == 2
    assert caught.value.retry_after_seconds > 0
    assert store.current()[1] == 2
    store.close()


def test_counter_persists_across_store_instances(tmp_path):
    clock = MutableClock()
    path = tmp_path / "usage.db"
    first = UsageStore(path, daily_limit=10, now_fn=clock.now)
    first.reserve()
    first.reserve()
    first.close()

    second = UsageStore(path, daily_limit=10, now_fn=clock.now)
    assert second.current()[1] == 2
    assert second.reserve().used == 3
    second.close()


def test_cst_midnight_resets_daily_counter(tmp_path):
    clock = MutableClock(datetime(2026, 10, 20, 15, 59, tzinfo=UTC))  # 23:59 CST
    store = UsageStore(tmp_path / "usage.db", daily_limit=1, now_fn=clock.now)
    store.reserve()

    with pytest.raises(UsageLimitReached):
        store.reserve()

    clock.advance(minutes=2)  # 00:01 CST，新的一天
    assert store.current()[0] == "2026-10-21"
    assert store.reserve().used == 1
    store.close()


def test_day_key_uses_utc_plus_8():
    assert cst_day_key(datetime(2026, 10, 20, 15, 59, tzinfo=UTC)) == "2026-10-20"
    assert cst_day_key(datetime(2026, 10, 20, 16, 0, tzinfo=UTC)) == "2026-10-21"
    # naive 时间按 UTC 处理，也不会抛异常
    assert cst_day_key(datetime(2026, 10, 20, 4, 0)) == "2026-10-20"


def test_concurrent_reservations_never_exceed_limit(tmp_path):
    clock = MutableClock()
    store = UsageStore(tmp_path / "usage.db", daily_limit=5, now_fn=clock.now)
    successes: list[int] = []
    failures: list[int] = []
    lock = threading.Lock()

    def worker():
        try:
            reservation = store.reserve()
            with lock:
                successes.append(reservation.used)
        except UsageLimitReached:
            with lock:
                failures.append(1)

    threads = [threading.Thread(target=worker) for _ in range(20)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(successes) == 5
    assert len(failures) == 15
    assert sorted(successes) == [1, 2, 3, 4, 5]
    assert store.current()[1] == 5
    store.close()


def test_closed_database_fails_closed(tmp_path):
    store = UsageStore(tmp_path / "usage.db", daily_limit=5)
    store.close()

    assert store.ping() is False
    with pytest.raises(UsageError):
        store.reserve()


def test_invalid_limit_rejected(tmp_path):
    with pytest.raises(ValueError):
        UsageStore(tmp_path / "usage.db", daily_limit=0)

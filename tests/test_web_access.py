"""访问申请 SQLite 存储测试。"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest

from app.web.access import (
    AccessExpired,
    AccessNotReady,
    AccessStore,
    AccessTokenInvalid,
    ActiveAccessExists,
    AlreadyProcessed,
    ApprovalTokenExpired,
    ApprovalTokenInvalid,
    BrowserTokenInvalid,
    RequestCooldown,
    RequestLimitExceeded,
    RequestNotFound,
)
from app.web.security import InvalidEmail, hash_token, new_token
from tests.web_helpers import MutableClock, make_store


def _create(store: AccessStore, email: str = "user@example.com", **kwargs):
    return store.create_request(
        email,
        "小明",
        browser_token=new_token(),
        requester_ip=kwargs.pop("requester_ip", "203.0.113.10"),
        **kwargs,
    )


class TestCreateRequest:
    def test_pending_request_and_hashes_only(self, tmp_path):
        clock = MutableClock()
        store = make_store(tmp_path, clock)
        created = _create(store, "User@Example.COM", requester_ip="203.0.113.1")
        request = created.request

        assert request.status == "pending"
        assert request.email == "user@example.com"
        assert request.display_name == "小明"
        assert request.created_at == clock.now()
        assert request.approval_expires_at == clock.now() + timedelta(hours=24)
        assert request.approved_at is None
        assert request.access_expires_at is None
        assert request.approval_token_hash == hash_token(created.approval_token)
        assert request.browser_token_hash == hash_token(created.browser_token)
        assert request.access_token_hash is None
        assert request.requester_ip_hash is not None

        row = store._conn.execute("SELECT * FROM access_requests").fetchone()
        row_text = "|".join(str(value) for value in tuple(row))
        assert created.approval_token not in row_text
        assert created.browser_token not in row_text

    def test_display_name_falls_back_to_email(self, tmp_path):
        store = make_store(tmp_path, MutableClock())
        created = store.create_request(
            "alice@example.com",
            "",
            browser_token=new_token(),
        )
        assert created.request.display_name == "alice"

    def test_same_email_pending_is_rejected(self, tmp_path):
        store = make_store(tmp_path, MutableClock())
        first = _create(store)
        with pytest.raises(RequestCooldown) as caught:
            _create(store, "USER@example.com")
        assert caught.value.request.id == first.request.id
        assert store._conn.execute(
            "SELECT COUNT(*) AS n FROM access_requests"
        ).fetchone()["n"] == 1

    def test_active_access_takes_precedence(self, tmp_path):
        store = make_store(tmp_path, MutableClock())
        created = _create(store)
        store.approve(created.approval_token)
        with pytest.raises(ActiveAccessExists) as caught:
            _create(store, "user@example.com")
        assert caught.value.request.status == "approved"

    def test_email_cooldown_after_rejection(self, tmp_path):
        clock = MutableClock()
        store = make_store(tmp_path, clock)
        first = _create(store)
        store.reject(first.approval_token)

        clock.advance(minutes=9)
        with pytest.raises(RequestCooldown) as caught:
            _create(store)
        assert caught.value.retry_after_seconds > 0

        clock.advance(minutes=1, seconds=1)
        second = _create(store)
        assert second.request.id != first.request.id

    def test_email_failed_does_not_arm_cooldown(self, tmp_path):
        # 发送失败等于申请没送达，不应让用户在 10 分钟内无法重试。
        store = make_store(tmp_path, MutableClock())
        first = _create(store)
        assert store.mark_email_failed(first.request.id, "network_timeout") is True

        second = _create(store)
        assert second.request.id != first.request.id

    def test_ip_limit(self, tmp_path):
        clock = MutableClock()
        store = make_store(tmp_path, clock)
        _create(
            store,
            "a@example.com",
            requester_ip="198.51.100.1",
            cooldown_minutes=0,
            ip_limit_per_hour=2,
            global_limit_per_day=0,
        )
        _create(
            store,
            "b@example.com",
            requester_ip="198.51.100.1",
            cooldown_minutes=0,
            ip_limit_per_hour=2,
            global_limit_per_day=0,
        )
        with pytest.raises(RequestLimitExceeded) as caught:
            _create(
                store,
                "c@example.com",
                requester_ip="198.51.100.1",
                cooldown_minutes=0,
                ip_limit_per_hour=2,
                global_limit_per_day=0,
            )
        assert caught.value.scope == "ip"
        assert caught.value.retry_after_seconds > 0

        # 换一个 IP 可以继续申请。
        _create(
            store,
            "c@example.com",
            requester_ip="198.51.100.2",
            cooldown_minutes=0,
            ip_limit_per_hour=2,
            global_limit_per_day=0,
        )

    def test_global_daily_limit_and_cst_reset(self, tmp_path):
        clock = MutableClock()  # 2026-10-20 04:00 UTC = 12:00 CST
        store = make_store(tmp_path, clock)
        _create(
            store,
            "a@example.com",
            requester_ip="198.51.100.1",
            cooldown_minutes=0,
            ip_limit_per_hour=0,
            global_limit_per_day=1,
        )
        with pytest.raises(RequestLimitExceeded) as caught:
            _create(
                store,
                "b@example.com",
                requester_ip="198.51.100.2",
                cooldown_minutes=0,
                ip_limit_per_hour=0,
                global_limit_per_day=1,
            )
        assert caught.value.scope == "global"
        assert caught.value.retry_after_seconds > 0

        # 跨到 UTC+8 的第二天 00:00（UTC 16:00）后重置。
        clock.set(clock.now().replace(hour=15, minute=59, second=0))
        # 上面的记录仍在同一天，仍被拦截。
        with pytest.raises(RequestLimitExceeded):
            _create(
                store,
                "b@example.com",
                requester_ip="198.51.100.2",
                cooldown_minutes=0,
                ip_limit_per_hour=0,
                global_limit_per_day=1,
            )
        clock.advance(minutes=1)
        _create(
            store,
            "b@example.com",
            requester_ip="198.51.100.2",
            cooldown_minutes=0,
            ip_limit_per_hour=0,
            global_limit_per_day=1,
        )

    def test_limits_zero_disable(self, tmp_path):
        store = make_store(tmp_path, MutableClock())
        for index in range(6):
            _create(
                store,
                f"u{index}@example.com",
                requester_ip="203.0.113.5",
                cooldown_minutes=0,
                ip_limit_per_hour=0,
                global_limit_per_day=0,
            )

    def test_no_ip_is_not_rate_limited_by_ip(self, tmp_path):
        store = make_store(tmp_path, MutableClock())
        for index in range(3):
            _create(
                store,
                f"n{index}@example.com",
                requester_ip=None,
                cooldown_minutes=0,
                ip_limit_per_hour=1,
                global_limit_per_day=0,
            )

    def test_invalid_inputs(self, tmp_path):
        store = make_store(tmp_path, MutableClock())
        with pytest.raises(InvalidEmail):
            store.create_request("bad", "x", browser_token=new_token())
        with pytest.raises(ValueError, match="browser_token"):
            store.create_request("a@example.com", "x", browser_token="short")
        with pytest.raises(ValueError, match="approval_ttl_hours"):
            store.create_request(
                "a@example.com",
                "x",
                browser_token=new_token(),
                approval_ttl_hours=0,
            )

    def test_persists_across_store_reopen(self, tmp_path):
        clock = MutableClock()
        store = make_store(tmp_path, clock)
        created = _create(store)
        store.close()

        reopened = make_store(tmp_path, clock)
        loaded = reopened.get_request(created.request.id)
        assert loaded is not None
        assert loaded.status == "pending"
        assert reopened.get_by_approval_token(created.approval_token).id == loaded.id

    def test_concurrent_same_email_only_one_wins(self, tmp_path):
        store = make_store(tmp_path, MutableClock())

        def attempt(index):
            try:
                _create(
                    store,
                    "same@example.com",
                    requester_ip=f"10.0.0.{index}",
                    cooldown_minutes=0,
                    ip_limit_per_hour=0,
                    global_limit_per_day=0,
                )
                return "ok"
            except RequestCooldown:
                return "cooldown"

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(attempt, range(8)))

        assert results.count("ok") == 1
        assert results.count("cooldown") == 7


class TestApproveReject:
    def test_approve_returns_access_token_and_expiry(self, tmp_path):
        clock = MutableClock()
        store = make_store(tmp_path, clock)
        created = _create(store)
        approved = store.approve(created.approval_token)

        assert approved.request.status == "approved"
        assert approved.request.approved_at == clock.now()
        assert approved.request.access_expires_at == clock.now() + timedelta(hours=8)
        assert approved.request.access_token_hash == hash_token(approved.access_token)
        assert store.validate_access_token(approved.access_token).id == created.request.id
        assert store.get_by_access_token(approved.access_token).id == created.request.id

    def test_approval_token_is_single_use(self, tmp_path):
        store = make_store(tmp_path, MutableClock())
        created = _create(store)
        store.approve(created.approval_token)
        with pytest.raises(AlreadyProcessed):
            store.approve(created.approval_token)
        with pytest.raises(AlreadyProcessed):
            store.reject(created.approval_token)

    def test_reject(self, tmp_path):
        store = make_store(tmp_path, MutableClock())
        created = _create(store)
        rejected = store.reject(created.approval_token)
        assert rejected.status == "rejected"
        assert store.status_for_request(created.request.id) == "rejected"
        with pytest.raises(AlreadyProcessed):
            store.reject(created.approval_token)

    def test_invalid_approval_tokens(self, tmp_path):
        store = make_store(tmp_path, MutableClock())
        for token in (None, "", "not-a-token"):
            with pytest.raises(ApprovalTokenInvalid):
                store.approve(token)
            with pytest.raises(ApprovalTokenInvalid):
                store.reject(token)

    def test_approval_expires_after_24h(self, tmp_path):
        clock = MutableClock()
        store = make_store(tmp_path, clock)
        created = _create(store)
        clock.advance(hours=24)

        with pytest.raises(ApprovalTokenExpired):
            store.approve(created.approval_token)
        assert store.get_request(created.request.id).status == "expired"
        assert store.status_for_request(created.request.id) == "expired"

    def test_approval_just_before_expiry(self, tmp_path):
        clock = MutableClock()
        store = make_store(tmp_path, clock)
        created = _create(store)
        clock.advance(hours=23, minutes=59, seconds=59)
        approved = store.approve(created.approval_token)
        assert approved.request.status == "approved"

    def test_access_expires_after_8h(self, tmp_path):
        clock = MutableClock()
        store = make_store(tmp_path, clock)
        created = _create(store)
        approved = store.approve(created.approval_token)

        clock.advance(hours=7, minutes=59, seconds=59)
        assert store.validate_access_token(approved.access_token).id == created.request.id

        clock.advance(seconds=1)
        with pytest.raises(AccessExpired):
            store.validate_access_token(approved.access_token)
        expired = store.get_by_access_token(approved.access_token)
        assert expired is not None
        assert expired.effective_status(clock.now()) == "expired"
        assert store.status_for_request(created.request.id) == "expired"

    def test_custom_access_duration(self, tmp_path):
        clock = MutableClock()
        store = make_store(tmp_path, clock)
        created = _create(store)
        with pytest.raises(ValueError):
            store.approve(created.approval_token, access_hours=0)
        approved = store.approve(created.approval_token, access_hours=1)
        assert approved.request.access_expires_at == clock.now() + timedelta(hours=1)

    def test_new_request_allowed_after_access_expiry(self, tmp_path):
        clock = MutableClock()
        store = make_store(tmp_path, clock)
        created = _create(
            store,
            cooldown_minutes=0,
            ip_limit_per_hour=0,
            global_limit_per_day=0,
        )
        store.approve(created.approval_token)
        clock.advance(hours=8, seconds=1)

        second = _create(
            store,
            cooldown_minutes=0,
            ip_limit_per_hour=0,
            global_limit_per_day=0,
        )
        assert second.request.id != created.request.id

    def test_mark_email_failed(self, tmp_path):
        store = make_store(tmp_path, MutableClock())
        created = _create(store)
        assert store.mark_email_failed(created.request.id, "http_error:422") is True
        failed = store.get_request(created.request.id)
        assert failed.status == "email_failed"
        assert failed.email_error == "http_error:422"
        assert store.mark_email_failed(created.request.id, "again") is False
        with pytest.raises(AlreadyProcessed):
            store.approve(created.approval_token)


class TestClaim:
    def test_pending_cannot_claim(self, tmp_path):
        store = make_store(tmp_path, MutableClock())
        created = _create(store)
        with pytest.raises(AccessNotReady) as caught:
            store.claim(created.request.id, created.browser_token)
        assert caught.value.status == "pending"

    def test_claim_after_approval(self, tmp_path):
        store = make_store(tmp_path, MutableClock())
        created = _create(store)
        store.approve(created.approval_token)
        claimed = store.claim(created.request.id, created.browser_token)
        assert claimed.id == created.request.id
        assert claimed.status == "approved"

    def test_claim_rejects_wrong_and_empty_browser_token(self, tmp_path):
        store = make_store(tmp_path, MutableClock())
        created = _create(store)
        store.approve(created.approval_token)
        with pytest.raises(BrowserTokenInvalid):
            store.claim(created.request.id, "wrong-token")
        with pytest.raises(BrowserTokenInvalid):
            store.claim(created.request.id, None)

    def test_claim_unknown_request(self, tmp_path):
        store = make_store(tmp_path, MutableClock())
        with pytest.raises(RequestNotFound):
            store.claim("missing", new_token())

    def test_claim_after_rejection(self, tmp_path):
        store = make_store(tmp_path, MutableClock())
        created = _create(store)
        store.reject(created.approval_token)
        with pytest.raises(AccessNotReady) as caught:
            store.claim(created.request.id, created.browser_token)
        assert caught.value.status == "rejected"

    def test_claim_after_access_expiry(self, tmp_path):
        clock = MutableClock()
        store = make_store(tmp_path, clock)
        created = _create(store)
        store.approve(created.approval_token)
        clock.advance(hours=8)
        with pytest.raises(AccessExpired):
            store.claim(created.request.id, created.browser_token)


class TestPublicViewAndCleanup:
    def test_public_dict_has_no_secrets(self, tmp_path):
        clock = MutableClock()
        store = make_store(tmp_path, clock)
        created = _create(store)
        data = created.request.public_dict(clock.now())
        assert data["email"] == "u***@example.com"
        assert data["status"] == "pending"
        assert data["access_expires_at"] is None
        text = str(data)
        assert created.approval_token not in text
        assert created.browser_token not in text
        assert "token_hash" not in text

    def test_public_dict_after_approval(self, tmp_path):
        clock = MutableClock()
        store = make_store(tmp_path, clock)
        created = _create(store)
        approved = store.approve(created.approval_token)
        data = approved.request.public_dict(clock.now())
        assert data["status"] == "approved"
        assert data["access_remaining_seconds"] == 8 * 3600

    def test_effective_status_and_remaining(self, tmp_path):
        clock = MutableClock()
        store = make_store(tmp_path, clock)
        created = _create(store)
        assert created.request.effective_status(clock.now()) == "pending"
        assert created.request.approval_remaining_seconds(clock.now()) == 24 * 3600
        assert created.request.access_remaining_seconds(clock.now()) == 0

        approved = store.approve(created.approval_token)
        assert approved.request.access_remaining_seconds(clock.now()) == 8 * 3600
        clock.advance(hours=8)
        assert approved.request.effective_status(clock.now()) == "expired"
        assert approved.request.access_remaining_seconds(clock.now()) == 0

    def test_cleanup(self, tmp_path):
        clock = MutableClock()
        store = make_store(tmp_path, clock)
        created = _create(store)
        clock.advance(days=29)
        assert store.cleanup(retention_days=30) == 0
        assert store.get_request(created.request.id) is not None
        clock.advance(days=1, seconds=1)
        assert store.cleanup(retention_days=30) == 1
        assert store.get_request(created.request.id) is None

    def test_cleanup_rejects_bad_retention(self, tmp_path):
        store = make_store(tmp_path, MutableClock())
        with pytest.raises(ValueError):
            store.cleanup(retention_days=0)

    def test_access_token_invalid(self, tmp_path):
        store = make_store(tmp_path, MutableClock())
        with pytest.raises(AccessTokenInvalid):
            store.validate_access_token(None)
        with pytest.raises(AccessTokenInvalid):
            store.validate_access_token("nope")

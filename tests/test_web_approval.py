"""申请、审批、轮询、解锁、退出的 HTTP 层测试（全部离线）。"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient

from app.web.main import create_app
from app.web.mailer import EmailSendError, RecordingEmailSender
from app.web.models import StaticModelProvider
from app.web.security import email_hash
from tests.web_helpers import (
    ORIGIN,
    MutableClock,
    approval_token_from_mail,
    approve_access,
    claim_access,
    csrf_from,
    fully_authorize,
    make_settings,
    request_access,
)


def build_app(tmp_path, clock, mailer, **overrides):
    settings = make_settings(tmp_path, **overrides)
    return create_app(
        settings=settings,
        mailer=mailer,
        model_provider=StaticModelProvider(object()),
        now_fn=clock.now,
    )


def test_request_sends_approval_only_to_owner_and_get_does_not_approve(tmp_path):
    clock = MutableClock()
    mailer = RecordingEmailSender()
    app = build_app(tmp_path, clock, mailer)

    with TestClient(app) as client:
        response = request_access(client, email="user@example.com", name="阿青")
        assert response.status_code == 200
        assert response.url.path == "/pending"
        assert mailer.last is not None
        assert mailer.last.to == "owner@example.com"
        assert "新的网页版访问申请" in mailer.last.subject

        token = approval_token_from_mail(mailer)
        page = client.get(f"/approve/{token}")
        assert page.status_code == 200
        assert "阿青" in page.text
        assert "user@example.com" in page.text

        record = app.state.access_store.get_by_approval_token(token)
        assert record is not None
        assert record.status == "pending"


def test_approval_grants_eight_hours_and_pending_claim_works(tmp_path):
    clock = MutableClock()
    mailer = RecordingEmailSender()
    app = build_app(tmp_path, clock, mailer)

    with TestClient(app) as client:
        request_access(client)
        approved = approve_access(client, mailer)
        assert approved.status_code == 200
        assert "已批准" in approved.text
        assert "magic-link" in approved.text

        status = client.get("/api/access/status").json()
        assert status["status"] == "approved"
        assert status["claimable"] is True
        assert status["access_remaining_seconds"] == 8 * 3600

        assert claim_access(client) == 200
        assert client.get("/").status_code == 200
        me = client.get("/api/me").json()
        assert me["ok"] is True
        assert len(me["sessions"]) == 4

        cookie = client.cookies.get("jbs_web_session")
        assert cookie
        assert "user@example.com" not in cookie


def test_claim_requires_csrf(tmp_path):
    clock = MutableClock()
    mailer = RecordingEmailSender()
    app = build_app(tmp_path, clock, mailer)

    with TestClient(app) as client:
        request_access(client)
        approve_access(client, mailer)
        response = client.post("/api/access/claim", headers={"Accept": "application/json"})
        assert response.status_code == 403
        assert response.json()["code"] == "csrf_failed"


def test_magic_link_unlocks_a_fresh_browser(tmp_path):
    clock = MutableClock()
    mailer = RecordingEmailSender()
    app = build_app(tmp_path, clock, mailer)

    with TestClient(app) as owner_client:
        request_access(owner_client)
        approved = approve_access(owner_client, mailer)
        match = re.search(r'http://testserver/unlock\?token=[^"<]+', approved.text)
        assert match, "审批页没有备用链接"
        magic_url = match.group(0)

    fresh_app = build_app(tmp_path, clock, mailer)
    with TestClient(fresh_app) as fresh_client:
        parsed = urlsplit(magic_url)
        response = fresh_client.get(f"{parsed.path}?{parsed.query}")
        assert response.status_code == 200
        assert response.url.path == "/"
        assert fresh_client.get("/api/me").status_code == 200


def test_reject_prevents_claim_and_marks_status(tmp_path):
    clock = MutableClock()
    mailer = RecordingEmailSender()
    app = build_app(tmp_path, clock, mailer)

    with TestClient(app) as client:
        request_access(client)
        rejected = approve_access(client, mailer, action="reject")
        assert rejected.status_code == 200
        assert "已拒绝" in rejected.text

        status = client.get("/api/access/status").json()
        assert status["status"] == "rejected"
        response = client.post(
            "/api/access/claim",
            headers={
                "X-CSRF-Token": csrf_from(client.get("/pending").text),
                "Accept": "application/json",
            },
        )
        assert response.status_code == 409
        assert response.json()["code"] == "not_ready"
        assert client.get("/", follow_redirects=False).status_code == 303

        # 已拒绝的申请不能把“重新申请”入口重定向回等待页。
        login = client.get("/login", follow_redirects=False)
        assert login.status_code == 200
        assert 'data-page="login"' in login.text


def test_approval_token_is_single_use(tmp_path):
    clock = MutableClock()
    mailer = RecordingEmailSender()
    app = build_app(tmp_path, clock, mailer)

    with TestClient(app) as client:
        request_access(client)
        token = approval_token_from_mail(mailer)
        first = client.post(
            f"/approve/{token}",
            data={"action": "approve"},
            headers=ORIGIN,
        )
        assert first.status_code == 200
        second = client.post(
            f"/approve/{token}",
            data={"action": "approve"},
            headers=ORIGIN,
        )
        assert second.status_code == 409
        assert "已经处理过" in second.text


def test_approval_link_expires_after_24_hours(tmp_path):
    clock = MutableClock()
    mailer = RecordingEmailSender()
    app = build_app(tmp_path, clock, mailer)

    with TestClient(app) as client:
        request_access(client)
        token = approval_token_from_mail(mailer)
        clock.advance(hours=24, seconds=1)

        page = client.get(f"/approve/{token}")
        assert page.status_code == 200
        assert "已过期" in page.text

        response = client.post(
            f"/approve/{token}",
            data={"action": "approve"},
            headers=ORIGIN,
        )
        assert response.status_code == 410

        record = app.state.access_store.get_by_approval_token(token)
        assert record is not None
        assert record.status == "expired"


def test_access_expiry_invalidates_server_side_session(tmp_path):
    clock = MutableClock()
    mailer = RecordingEmailSender()
    app = build_app(tmp_path, clock, mailer)

    with TestClient(app) as client:
        fully_authorize(client, mailer)
        assert client.get("/api/me").status_code == 200

        clock.advance(hours=8, seconds=1)

        response = client.get("/api/me")
        assert response.status_code == 401
        assert response.json()["code"] == "access_expired"
        page = client.get("/", follow_redirects=False)
        assert page.status_code == 303
        assert page.headers["location"].startswith("/login")

        # 重新打开申请页时显示“已到期”
        page = client.get("/login?expired=1")
        assert page.status_code == 200
        assert "访问已到期" in page.text


def test_email_failure_marks_request_and_returns_503(tmp_path):
    clock = MutableClock()
    mailer = RecordingEmailSender()
    mailer.fail_with = EmailSendError("http_error", status_code=500)
    app = build_app(tmp_path, clock, mailer)

    with TestClient(app) as client:
        response = request_access(client)
        assert response.status_code == 503
        assert "审批邮件暂时发送失败" in response.text

        record = app.state.access_store._latest_for_email(email_hash("user@example.com"))
        assert record is not None
        assert record.status == "email_failed"
        assert record.email_error == "http_error:500"


def test_origin_header_required_for_request_and_approval(tmp_path):
    clock = MutableClock()
    mailer = RecordingEmailSender()
    app = build_app(tmp_path, clock, mailer)

    with TestClient(app) as client:
        response = client.post(
            "/request",
            data={"email": "user@example.com", "display_name": "阿青"},
        )
        assert response.status_code == 403
        assert mailer.messages == []

        request_access(client)
        token = approval_token_from_mail(mailer)
        response = client.post(f"/approve/{token}", data={"action": "approve"})
        assert response.status_code == 403


def test_csrf_token_works_when_proxy_rewrites_origin(tmp_path):
    """端口转发 / IDE 预览可能把 Origin 改掉；页面 token 仍应放行。"""
    clock = MutableClock()
    mailer = RecordingEmailSender()
    app = build_app(tmp_path, clock, mailer)
    proxy_origin = {"Origin": "https://preview.example.test"}

    with TestClient(app) as client:
        login_page = client.get("/login")
        login_csrf = csrf_from(login_page.text)
        response = client.post(
            "/request",
            data={
                "email": "user@example.com",
                "display_name": "阿青",
                "csrf": login_csrf,
            },
            headers=proxy_origin,
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert response.headers["location"] == "/pending"

        token = approval_token_from_mail(mailer)
        approve_page = client.get(f"/approve/{token}")
        approve_csrf = csrf_from(approve_page.text)
        approved = client.post(
            f"/approve/{token}",
            data={"action": "approve", "csrf": approve_csrf},
            headers=proxy_origin,
        )
        assert approved.status_code == 200
        assert "已批准" in approved.text


def test_duplicate_pending_request_is_rate_limited(tmp_path):
    clock = MutableClock()
    mailer = RecordingEmailSender()
    app = build_app(tmp_path, clock, mailer)

    with TestClient(app) as first:
        request_access(first, email="user@example.com")
    second_app = build_app(tmp_path, clock, mailer)
    with TestClient(second_app) as second:
        response = request_access(second, email="user@example.com")
        assert response.status_code == 429
        assert "等待开发者处理" in response.text


def test_active_access_blocks_duplicate_request(tmp_path):
    clock = MutableClock()
    mailer = RecordingEmailSender()
    app = build_app(tmp_path, clock, mailer)

    with TestClient(app) as client:
        fully_authorize(client, mailer)

    second_app = build_app(tmp_path, clock, mailer)
    with TestClient(second_app) as second:
        response = request_access(second, email="user@example.com")
        assert response.status_code == 409
        assert "已有有效访问" in response.text


def test_ip_request_limit(tmp_path):
    clock = MutableClock()
    mailer = RecordingEmailSender()
    app = build_app(tmp_path, clock, mailer, requests_per_ip_per_hour=1)

    with TestClient(app) as client:
        assert request_access(client, email="one@example.com").status_code == 200
        response = request_access(client, email="two@example.com")
        assert response.status_code == 429
        assert "该网络" in response.text


def test_invalid_email_and_missing_origin_do_not_create_request(tmp_path):
    clock = MutableClock()
    mailer = RecordingEmailSender()
    app = build_app(tmp_path, clock, mailer)

    with TestClient(app) as client:
        response = request_access(client, email="not-an-email")
        assert response.status_code == 400
        assert "邮箱格式不正确" in response.text

    assert mailer.messages == []


def test_status_api_without_pending_session_returns_none(tmp_path):
    clock = MutableClock()
    mailer = RecordingEmailSender()
    app = build_app(tmp_path, clock, mailer)

    with TestClient(app) as client:
        assert client.get("/api/access/status").json() == {
            "ok": True,
            "status": "none",
        }
        response = client.post(
            "/api/access/claim",
            headers={"Accept": "application/json"},
        )
        assert response.status_code == 401


def test_invalid_and_expired_magic_links(tmp_path):
    clock = MutableClock()
    mailer = RecordingEmailSender()
    app = build_app(tmp_path, clock, mailer)

    with TestClient(app) as client:
        invalid = client.get("/unlock?token=not-a-real-token")
        assert invalid.status_code == 404

        request_access(client)
        approved = approve_access(client, mailer)
        match = re.search(r'http://testserver/unlock\?token=[^"<]+', approved.text)
        assert match
        parsed = urlsplit(match.group(0))

    fresh_app = build_app(tmp_path, clock, mailer)
    with TestClient(fresh_app) as fresh:
        clock.advance(hours=8, seconds=1)
        expired = fresh.get(f"{parsed.path}?{parsed.query}")
        assert expired.status_code == 401
        assert "访问已到期" in expired.text


def test_logout_clears_session(tmp_path):
    clock = MutableClock()
    mailer = RecordingEmailSender()
    app = build_app(tmp_path, clock, mailer)

    with TestClient(app) as client:
        fully_authorize(client, mailer)
        page = client.get("/")
        response = client.post(
            "/logout",
            headers={
                "X-CSRF-Token": csrf_from(page.text),
                "Accept": "application/json",
            },
        )
        assert response.status_code == 200
        assert client.get("/", follow_redirects=False).status_code == 303


def test_login_redirects_existing_session(tmp_path):
    clock = MutableClock()
    mailer = RecordingEmailSender()
    app = build_app(tmp_path, clock, mailer)

    with TestClient(app) as client:
        fully_authorize(client, mailer)
        response = client.get("/login", follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/"


def test_console_mode_pages_explain_that_no_real_email_is_sent(tmp_path):
    """本地 console 模式不发真实邮件，页面必须明确指向服务端终端。"""
    clock = MutableClock()
    mailer = RecordingEmailSender()
    app = build_app(tmp_path, clock, mailer)

    with TestClient(app) as client:
        login = client.get("/login")
        assert "不会真的发送邮件" in login.text
        assert "本地邮件" in login.text

        pending = request_access(client)
        assert pending.status_code == 200
        assert "[本地邮件]" in pending.text
        assert "OWNER_EMAIL" in pending.text
        assert "不会发到申请者邮箱" in pending.text

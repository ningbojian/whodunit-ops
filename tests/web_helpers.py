"""网页版测试的共享工具：固定时钟、临时配置、临时存储、登录流程。"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.web.access import AccessStore
from app.web.config import WebSettings

ORIGIN = {"Origin": "http://testserver"}

UTC = timezone.utc


class MutableClock:
    """可手动推进的时钟，避免测试 sleep。"""

    def __init__(self, start: datetime | None = None) -> None:
        self.current = start or datetime(2026, 10, 20, 4, 0, tzinfo=UTC)

    def now(self) -> datetime:
        return self.current

    def advance(self, **kwargs) -> datetime:
        self.current = self.current + timedelta(**kwargs)
        return self.current

    def set(self, value: datetime) -> datetime:
        self.current = value
        return self.current


def make_settings(tmp_path: Path, **overrides) -> WebSettings:
    base = dict(
        env="test",
        session_secret="test-session-secret-" + "x" * 40,
        owner_email="owner@example.com",
        email_backend="console",
        resend_api_key=None,
        email_from="onboarding@resend.dev",
        public_base_url="http://testserver",
        data_root=tmp_path / "web_users",
        state_db=tmp_path / "web_state.sqlite3",
        default_store="迷雾剧场",
        access_hours=8.0,
        approval_ttl_hours=24.0,
        session_hours=24.0,
        max_input_chars=2000,
        max_body_bytes=32768,
        max_history_messages=100,
        max_history_chars=50000,
        max_concurrent_turns=3,
        max_turns_global_per_day=300,
        model_max_tokens=1500,
        model_timeout_seconds=60.0,
        request_cooldown_minutes=10,
        requests_per_ip_per_hour=5,
        requests_global_per_day=50,
    )
    base.update(overrides)
    return WebSettings(**base)


def make_store(
    tmp_path: Path,
    clock: MutableClock,
    *,
    name: str = "access.sqlite3",
) -> AccessStore:
    return AccessStore(tmp_path / name, now_fn=clock.now)


def env_base(**overrides) -> dict[str, str]:
    """一份可以启动的最小环境变量，测试按需覆盖。"""
    env = {
        "WEB_ENV": "development",
        "WEB_SESSION_SECRET": "s" * 48,
        "OWNER_EMAIL": "owner@example.com",
        "EMAIL_BACKEND": "console",
    }
    env.update(overrides)
    return env


def csrf_from(html: str) -> str:
    match = re.search(r'<meta name="csrf-token" content="([^"]+)"', html)
    assert match, "页面缺少 CSRF meta"
    return match.group(1)


def approval_token_from_mail(mailer) -> str:
    assert mailer.last is not None, "没有发出审批邮件"
    match = re.search(r"/approve/([^\s<]+)", mailer.last.text)
    assert match, "审批邮件里没有审批链接"
    return match.group(1)


def request_access(
    client,
    email: str = "user@example.com",
    name: str = "阿青",
):
    return client.post(
        "/request",
        data={"email": email, "display_name": name},
        headers=ORIGIN,
    )


def approve_access(client, mailer, *, action: str = "approve"):
    token = approval_token_from_mail(mailer)
    return client.post(
        f"/approve/{token}",
        data={"action": action},
        headers=ORIGIN,
    )


def claim_access(client) -> int:
    page = client.get("/pending")
    assert page.status_code == 200
    response = client.post(
        "/api/access/claim",
        headers={"X-CSRF-Token": csrf_from(page.text), "Accept": "application/json"},
    )
    return response.status_code


def fully_authorize(client, mailer, *, email: str = "user@example.com") -> None:
    """申请 -> 批准 -> claim，得到一个可用的正式会话。"""
    response = request_access(client, email=email)
    assert response.status_code == 200
    response = approve_access(client, mailer)
    assert response.status_code == 200
    response = client.get("/pending")
    assert response.status_code == 200
    status = client.get("/api/access/status").json()
    assert status["status"] == "approved"
    assert claim_access(client) == 200

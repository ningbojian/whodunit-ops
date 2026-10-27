"""页面、安全头、请求体上限、模板转义的离线测试。"""

from __future__ import annotations

import re
from pathlib import Path

from fastapi.testclient import TestClient

from app.web import main as web_main
from app.web.main import create_app
from app.web.mailer import RecordingEmailSender
from app.web.models import StaticModelProvider
from tests.web_helpers import (
    ORIGIN,
    MutableClock,
    approval_token_from_mail,
    approve_access,
    fully_authorize,
    make_settings,
    request_access,
)

WEB_DIR = Path(web_main.__file__).resolve().parent


def build_app(tmp_path, clock, mailer, **overrides):
    settings = make_settings(tmp_path, **overrides)
    return create_app(
        settings=settings,
        mailer=mailer,
        model_provider=StaticModelProvider(object()),
        now_fn=clock.now,
    )


def test_login_page_has_no_inline_script_and_security_headers(tmp_path):
    clock = MutableClock()
    mailer = RecordingEmailSender()
    app = build_app(tmp_path, clock, mailer)

    with TestClient(app) as client:
        response = client.get("/login")

    assert response.status_code == 200
    assert "申请访问" in response.text
    assert "https://" not in response.text
    assert not re.search(r"<script(?![^>]*\bsrc=)", response.text, re.IGNORECASE)
    assert "onclick=" not in response.text.lower()
    assert "javascript:" not in response.text.lower()

    headers = response.headers
    csp = headers["Content-Security-Policy"]
    assert "default-src 'self'" in csp
    assert "script-src 'self'" in csp
    assert "frame-ancestors 'none'" in csp
    assert "unsafe-inline" not in csp
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["X-Frame-Options"] == "DENY"
    assert headers["Referrer-Policy"] == "no-referrer"
    assert headers["Cache-Control"] == "no-store"


def test_healthz_and_disabled_docs(tmp_path):
    clock = MutableClock()
    app = build_app(tmp_path, clock, RecordingEmailSender())

    with TestClient(app) as client:
        health = client.get("/healthz")
        assert health.status_code == 200
        assert health.json()["ok"] is True
        assert client.get("/docs").status_code == 404
        assert client.get("/redoc").status_code == 404
        assert client.get("/openapi.json").status_code == 404
        assert client.get("/api/healthz").status_code == 200


def test_static_assets_are_served_without_store_cache(tmp_path):
    clock = MutableClock()
    app = build_app(tmp_path, clock, RecordingEmailSender())

    with TestClient(app) as client:
        css = client.get("/static/app.css")
        js = client.get("/static/app.js")

    assert css.status_code == 200
    assert "text/css" in css.headers["content-type"]
    assert "no-store" not in css.headers.get("cache-control", "")
    assert js.status_code == 200
    assert "javascript" in js.headers["content-type"]
    assert "no-store" not in js.headers.get("cache-control", "")


def test_production_adds_hsts_and_secure_cookie(tmp_path):
    clock = MutableClock()
    mailer = RecordingEmailSender()
    app = build_app(
        tmp_path,
        clock,
        mailer,
        env="production",
        public_base_url="https://testserver",
    )

    with TestClient(app, base_url="https://testserver") as client:
        response = client.get("/login")
        assert response.headers["Strict-Transport-Security"].startswith("max-age=")

        response = client.post(
            "/request",
            data={"email": "user@example.com", "display_name": "阿青"},
            headers={"Origin": "https://testserver"},
            follow_redirects=False,
        )
        assert response.status_code == 303
        set_cookie = response.headers.get("set-cookie", "")
        assert "httponly" in set_cookie.lower()
        assert "secure" in set_cookie.lower()
        assert "samesite=lax" in set_cookie.lower()


def test_body_size_limit_returns_413(tmp_path):
    clock = MutableClock()
    mailer = RecordingEmailSender()
    app = build_app(tmp_path, clock, mailer, max_body_bytes=1024)

    with TestClient(app) as client:
        response = client.post(
            "/request",
            content=b"x" * 2048,
            headers={
                **ORIGIN,
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )

    assert response.status_code == 413
    assert response.json()["code"] == "payload_too_large"
    assert mailer.messages == []


def test_invalid_approval_token_is_404(tmp_path):
    clock = MutableClock()
    app = build_app(tmp_path, clock, RecordingEmailSender())

    with TestClient(app) as client:
        response = client.get("/approve/not-a-real-token")
        assert response.status_code == 404
        assert "审批链接无效" in response.text
        assert client.get("/unlock?token=nope").status_code == 404


def test_user_text_is_html_escaped_on_approval_page(tmp_path):
    clock = MutableClock()
    mailer = RecordingEmailSender()
    app = build_app(tmp_path, clock, mailer)

    with TestClient(app) as client:
        response = request_access(client, name="<script>alert(1)</script>")
        assert response.status_code == 200
        token = approval_token_from_mail(mailer)
        page = client.get(f"/approve/{token}")

    assert page.status_code == 200
    assert "<script>alert(1)</script>" not in page.text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page.text


def test_invalid_email_reflection_is_escaped(tmp_path):
    clock = MutableClock()
    mailer = RecordingEmailSender()
    app = build_app(tmp_path, clock, mailer)

    with TestClient(app) as client:
        response = request_access(client, email="a<b@example.com")

    assert response.status_code == 400
    assert "a<b@example.com" not in response.text
    assert "a&lt;b@example.com" in response.text


def test_templates_and_js_have_no_dangerous_rendering():
    templates = list((WEB_DIR / "templates").rglob("*.html"))
    assert templates
    for path in templates:
        text = path.read_text(encoding="utf-8")
        assert "|safe" not in text, path
        assert "innerHTML" not in text, path
        assert not re.search(r"<script(?![^>]*\bsrc=)", text, re.IGNORECASE), path

    for name in ("app.js", "app.css"):
        text = (WEB_DIR / "static" / name).read_text(encoding="utf-8")
        assert "innerHTML" not in text
        assert "document.write" not in text
        assert "eval(" not in text


def test_authenticated_pages_do_not_show_raw_email(tmp_path):
    clock = MutableClock()
    mailer = RecordingEmailSender()
    app = build_app(tmp_path, clock, mailer)

    with TestClient(app) as client:
        fully_authorize(client, mailer)
        page = client.get("/")

    assert page.status_code == 200
    assert "user@example.com" not in page.text
    assert "u***@example.com" in page.text


def test_approval_page_does_not_include_financial_fields(tmp_path):
    clock = MutableClock()
    mailer = RecordingEmailSender()
    app = build_app(tmp_path, clock, mailer)

    with TestClient(app) as client:
        request_access(client)
        token = approval_token_from_mail(mailer)
        page = client.get(f"/approve/{token}")

    assert page.status_code == 200
    for word in ("profit", "cost", "margin", "source_text", "raw_snippet"):
        assert word not in page.text.lower()


def test_unknown_page_renders_safe_error(tmp_path):
    clock = MutableClock()
    app = build_app(tmp_path, clock, RecordingEmailSender())

    with TestClient(app) as client:
        response = client.get("/no-such-page")

    assert response.status_code == 404
    assert "页面不存在" in response.text
    assert "no-such-page" not in response.text


def test_healthz_degrades_when_database_closes(tmp_path):
    clock = MutableClock()
    app = build_app(tmp_path, clock, RecordingEmailSender())

    with TestClient(app) as client:
        app.state.access_store.close()
        app.state.usage.close()
        response = client.get("/healthz")

    assert response.status_code == 503
    assert response.json()["ok"] is False


# ---------------------------------------------------------------------------
# 暗色运营台：新页面、移动兜底与本地静态资源
# ---------------------------------------------------------------------------

NEW_PAGES = (
    ("/resources", "resources"),
    ("/schedule", "schedule"),
    ("/imports", "imports"),
    ("/stages", "stages"),
    ("/runs", "runs"),
)


def _authorized_app(tmp_path):
    clock = MutableClock()
    mailer = RecordingEmailSender()
    return build_app(tmp_path, clock, mailer), mailer


def test_new_pages_render_safe_mobile_shell(tmp_path):
    app, mailer = _authorized_app(tmp_path)

    with TestClient(app) as client:
        fully_authorize(client, mailer)
        for path, page in NEW_PAGES:
            response = client.get(path)
            assert response.status_code == 200, path
            html = response.text
            assert f'data-page="{page}"' in html, path
            assert 'name="viewport"' in html, path
            assert 'rel="manifest" href="/static/manifest.webmanifest"' in html, path
            assert 'name="theme-color" content="#05070f"' in html, path
            # 窄屏兜底：服务端渲染的普通链接，不做前端路由。
            assert 'class="mobile-nav"' in html, path
            assert 'aria-label="底部导航"' in html, path
            assert 'aria-current="page"' in html, path
            assert "https://" not in html, path
            assert not re.search(r"<script(?![^>]*\bsrc=)", html, re.IGNORECASE), path
            assert not re.search(r"\sstyle=", html, re.IGNORECASE), path
            assert "onclick=" not in html.lower(), path


def test_schedule_create_dialog_defaults_to_auto_assignment(tmp_path):
    app, mailer = _authorized_app(tmp_path)

    with TestClient(app) as client:
        fully_authorize(client, mailer)
        html = client.get("/schedule").text

    # WP16：建场进 create-dialog，房间/DM 可选且默认自动分配；预览走 preview-dialog。
    assert 'id="create-dialog"' in html
    assert 'id="preview-dialog"' in html
    assert 'id="add-session-button"' in html

    start = html.index('id="session-form"')
    end = html.index("</form>", start)
    form = html[start:end]
    assert 'id="session-script-search"' in form
    assert 'id="session-days"' in form
    assert 'id="session-hours"' in form
    assert 'id="session-minutes"' in form
    assert 'id="session-submit"' in form
    assert 'id="session-room"' in form
    assert 'id="session-dm"' in form
    # 人数/价格可以放在“更多”里，但不是硬必填。
    assert 'name="total_seats"' in form
    assert 'name="list_price"' in form
    # 两个下拉的首选项都是 value="" 的「自动分配」。
    for select_id in ("session-room", "session-dm"):
        sel_start = form.index(f'<select id="{select_id}">')
        sel_end = form.index("</select>", sel_start)
        select_html = form[sel_start:sel_end]
        options = select_html.split("<option")[1:]
        assert any(
            'value=""' in option and "自动分配" in option for option in options
        ), select_id

    assert "<datalist" not in form
    assert not re.search(r"<script(?![^>]*\bsrc=)", html, re.IGNORECASE)
    assert not re.search(r"\sstyle=", html, re.IGNORECASE)


def test_run_detail_template_is_read_only_and_hides_dm_cues():
    text = (WEB_DIR / "templates" / "run_detail.html").read_text(encoding="utf-8")
    assert "cue_text" not in text
    assert "prompt_text" not in text
    # “已暂停”可以作为只读状态显示，但不能有推进/暂停/结束按钮。
    assert "下一幕" not in text
    assert "开始场控" not in text
    assert not re.search(r"<button[^>]*>[^<]*(暂停|继续|结束场控)", text)
    assert 'id="regen-code-button"' in text
    assert 'id="revoke-token-button"' in text


def test_dark_theme_tokens_and_responsive_rules():
    css = (WEB_DIR / "static" / "app.css").read_text(encoding="utf-8")
    for token in (
        "--bg-0: #05070f",
        "--cyan: #22d3ee",
        "--violet: #8b5cf6",
        "--text: #e8f0ff",
        "--danger: #fb7185",
    ):
        assert token in css
    assert "@media (max-width: 860px)" in css
    assert ".mobile-nav" in css
    assert "safe-area-inset-bottom" in css
    assert "prefers-reduced-motion: reduce" in css
    assert ":focus-visible" in css

    js = (WEB_DIR / "static" / "app.js").read_text(encoding="utf-8")
    assert "serviceWorker" not in js
    assert "http://" not in js
    assert "https://" not in js


def test_manifest_and_icons_are_local_and_installable(tmp_path):
    app, mailer = _authorized_app(tmp_path)

    with TestClient(app) as client:
        fully_authorize(client, mailer)
        manifest = client.get("/static/manifest.webmanifest")

        assert manifest.status_code == 200
        assert "application/manifest+json" in manifest.headers["content-type"]
        data = manifest.json()
        assert data["display"] == "standalone"
        assert data["background_color"] == "#05070f"
        assert data["theme_color"] == "#05070f"
        sizes = {item["sizes"] for item in data["icons"]}
        assert {"192x192", "512x512"} <= sizes
        for item in data["icons"]:
            icon = client.get(item["src"])
            assert icon.status_code == 200
            assert icon.headers["content-type"].startswith("image/")

        # 不注册 service worker，避免把已登录租户数据缓存到共享设备。
        assert client.get("/sw.js").status_code == 404

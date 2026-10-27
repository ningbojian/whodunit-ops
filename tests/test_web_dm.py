"""阶段模板、场控直播与商家/DM 双令牌边界。"""

from __future__ import annotations

import json
import re
import uuid
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app.web.main import create_app
from app.web.mailer import RecordingEmailSender
from app.web.models import StaticModelProvider
from tests.web_helpers import (
    ORIGIN,
    MutableClock,
    approval_token_from_mail,
    csrf_from,
    fully_authorize,
    make_settings,
)


def build_app(tmp_path, clock):
    return create_app(
        settings=make_settings(tmp_path),
        mailer=RecordingEmailSender(),
        model_provider=StaticModelProvider(object()),
        now_fn=clock.now,
    )


@pytest.fixture()
def clock():
    return MutableClock()


@pytest.fixture()
def app(tmp_path, clock):
    return build_app(tmp_path, clock)


@pytest.fixture()
def client(app):
    with TestClient(app) as value:
        fully_authorize(value, app.state.mailer)
        yield value


def _csrf(client) -> str:
    return csrf_from(client.get("/schedule").text)


def _headers(client) -> dict[str, str]:
    return {
        "X-CSRF-Token": _csrf(client),
        "Accept": "application/json",
        "Origin": "http://testserver",
    }


def _sessions(client) -> list[dict]:
    return client.get("/api/schedule").json()["sessions"]


def _session(client, script_name: str = "雾隐山庄") -> dict:
    for session in _sessions(client):
        if session["script_name"] == script_name:
            return session
    raise AssertionError(f"找不到演示场次：{script_name}")


def _create_run(client) -> dict:
    headers = _headers(client)
    session = _session(client)
    response = client.post(
        "/api/runs",
        json={"session_id": session["session_id"], "mode": "software"},
        headers=headers,
    )
    assert response.status_code == 201
    return response.json()


def _merchant_token(client, mailer, email: str = "app@example.com") -> str:
    created = client.post(
        "/api/app/auth/request",
        json={"email": email, "display_name": "阿青"},
        headers=ORIGIN,
    )
    assert created.status_code == 201
    data = created.json()
    approved = client.post(
        f"/approve/{approval_token_from_mail(mailer)}",
        data={"action": "approve"},
        headers=ORIGIN,
    )
    assert approved.status_code == 200
    claimed = client.post(
        "/api/app/auth/claim",
        json={
            "request_id": data["request_id"],
            "browser_token": data["browser_token"],
        },
        headers=ORIGIN,
    )
    assert claimed.status_code == 200
    return claimed.json()["token"]


def _redeem(client, code: str) -> dict:
    response = client.post(
        "/api/dm/redeem",
        json={"code": code, "device_id": f"device-{uuid.uuid4().hex[:8]}"},
        headers=ORIGIN,
    )
    assert response.status_code == 200
    return response.json()


# ---------------------------------------------------------------------------
# 页面与阶段模板
# ---------------------------------------------------------------------------


def test_stages_and_runs_pages_require_login(app):
    with TestClient(app) as anon:
        for path in ("/stages", "/runs"):
            page = anon.get(path, follow_redirects=False)
            assert page.status_code == 303
            assert page.headers["location"].startswith("/login")
        assert anon.get("/api/stages").status_code == 401


def test_stages_page_and_template_roundtrip(client):
    page = client.get("/stages")
    assert page.status_code == 200
    html = page.text
    assert 'data-page="stages"' in html
    assert "阶段" in html
    assert not re.search(r"<script(?![^>]*\bsrc=)", html, re.IGNORECASE)
    assert not re.search(r"\sstyle=", html, re.IGNORECASE)

    api = client.get("/api/stages").json()
    assert api["scripts"]
    selected = api["selected"]
    assert selected and selected["stages"]
    assert len(selected["stages"]) >= 3
    assert any(stage.get("cue_text") for stage in selected["stages"])

    headers = _headers(client)
    before = selected["version"]
    script_id = selected["script"]["script_id"]
    stages = [
        {
            "name": stage["name"],
            "stage_type": stage.get("stage_type"),
            "duration_minutes": stage["duration_minutes"] + 1,
            "warn_before_seconds": stage.get("warn_before_seconds", 60),
            "cue_text": stage.get("cue_text"),
            "prompt_text": stage.get("prompt_text"),
            "active": True,
        }
        for stage in selected["stages"]
    ]
    saved = client.post(
        "/api/stages",
        json={"script_id": script_id, "stages": stages},
        headers={**headers, "Origin": "http://testserver"},
    )
    assert saved.status_code == 200
    assert saved.json()["version"] == before + 1
    assert saved.json()["stages"][0]["duration_minutes"] == stages[0]["duration_minutes"]

    without_csrf = client.post(
        "/api/stages",
        json={"script_id": script_id, "stages": stages},
        headers={"Accept": "application/json"},
    )
    assert without_csrf.status_code == 403
    assert without_csrf.json()["code"] == "csrf_failed"


def test_run_creation_shows_code_once_and_live_page_is_read_only(client):
    created = _create_run(client)
    code = created["code"]
    run_id = created["run"]["run_id"]
    assert code and len(code) == 8
    assert created["run"]["status"] == "pending"
    assert created["template_preview"] is True

    runs_page = client.get("/runs")
    assert runs_page.status_code == 200
    assert 'data-page="runs"' in runs_page.text
    assert run_id in runs_page.text

    detail = client.get(f"/runs/{run_id}")
    assert detail.status_code == 200
    html = detail.text
    assert 'data-page="run-detail"' in html
    assert "只读直播" in html
    assert "下一幕" not in html
    assert "结束场控" not in html
    assert code not in html
    # 商家的直播页不能看到 DM 专用提示。
    cue = client.get("/api/stages").json()["selected"]["stages"][0].get("cue_text")
    if cue:
        assert cue not in html

    view = client.get(f"/api/runs/{run_id}")
    assert view.status_code == 200
    body = json.dumps(view.json(), ensure_ascii=False)
    assert code not in body
    assert "cue_text" not in body

    # 商家路由没有推进/暂停/结束入口。
    for action in ("next", "pause", "finish"):
        response = client.post(
            f"/api/runs/{run_id}/{action}",
            json={},
            headers=_headers(client),
        )
        assert response.status_code == 404


def test_dm_token_cannot_be_used_on_merchant_api(client):
    created = _create_run(client)
    redeemed = _redeem(client, created["code"])
    response = client.get(
        "/api/app/me",
        headers={"Authorization": f"Bearer {redeemed['token']}"},
    )
    assert response.status_code == 403
    assert response.json()["code"] == "forbidden"


def test_merchant_token_cannot_be_used_on_dm_api(client, app):
    token = _merchant_token(client, app.state.mailer)
    response = client.get(
        "/api/dm/run", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 403
    assert response.json()["code"] == "forbidden"


def test_dm_flow_does_not_auto_advance_when_time_passes(client, clock):
    created = _create_run(client)
    run_id = created["run"]["run_id"]
    redeemed = _redeem(client, created["code"])
    bearer = {"Authorization": f"Bearer {redeemed['token']}"}

    preview = client.get("/api/dm/run", headers=bearer)
    assert preview.status_code == 200
    assert preview.json()["template_preview"] is True
    assert preview.json()["run"]["current_seq"] is None

    started = client.post(
        "/api/dm/run/action",
        json={
            "action": "start",
            "client_event_id": "evt-start",
            "expected_version": redeemed["run"]["state_version"],
        },
        headers=bearer,
    )
    assert started.status_code == 200
    started_payload = started.json()
    assert started_payload["ok"] is True
    assert started_payload["run"]["current_seq"] == 1
    assert started_payload["hardware"]["status"] == "skipped"

    moved = client.post(
        "/api/dm/run/action",
        json={
            "action": "next",
            "client_event_id": "evt-next",
            "expected_version": started_payload["run"]["state_version"],
        },
        headers=bearer,
    )
    assert moved.status_code == 200
    assert moved.json()["run"]["current_seq"] == 2
    seq = moved.json()["run"]["current_seq"]

    # 时间越过阶段结束点：只允许出现超时，不允许服务端自动推进。
    clock.advance(hours=3)
    later = client.get("/api/dm/run", headers=bearer).json()
    assert later["run"]["current_seq"] == seq
    assert later["run"]["overtime_seconds"] > 0

    events = client.get("/api/dm/run/events", headers=bearer).json()["events"]
    assert [item["event_type"] for item in events].count("stage_next") == 1

    # 商家只读视图同步显示，但没有推进按钮。
    merchant = client.get(f"/api/runs/{run_id}").json()
    assert merchant["run"]["current_seq"] == seq
    assert "cue_text" not in json.dumps(merchant, ensure_ascii=False)

    # 完成与撤销令牌后，旧 DM token 立即失效。
    finished = client.post(
        "/api/dm/run/action",
        json={
            "action": "finish",
            "client_event_id": "evt-finish",
            "expected_version": later["run"]["state_version"],
        },
        headers=bearer,
    )
    assert finished.status_code == 200
    assert finished.json()["run"]["status"] == "completed"
    assert client.get("/api/dm/run", headers=bearer).status_code == 401


def test_merchant_can_reissue_code_and_old_token_is_revoked(client):
    created = _create_run(client)
    run_id = created["run"]["run_id"]
    redeemed = _redeem(client, created["code"])
    bearer = {"Authorization": f"Bearer {redeemed['token']}"}

    reissued = client.post(
        f"/api/runs/{run_id}/code",
        json={},
        headers=_headers(client),
    )
    assert reissued.status_code == 200
    new_code = reissued.json()["code"]
    assert new_code != created["code"]

    old = client.get("/api/dm/run", headers=bearer)
    assert old.status_code == 401
    assert old.json()["code"] == "token_invalid"

    handoff = _redeem(client, new_code)
    assert handoff["run"]["status"] == "pending"


def test_merchant_bearer_api_reuses_operations_service(client, app, clock):
    token = _merchant_token(client, app.state.mailer)
    bearer = {"Authorization": f"Bearer {token}", "Accept": "application/json"}

    me = client.get("/api/app/me", headers=bearer)
    assert me.status_code == 200
    assert me.json()["display_name"] == "阿青"

    schedule = client.get("/api/app/schedule", headers=bearer)
    assert schedule.status_code == 200
    sessions = schedule.json()["sessions"]
    assert sessions

    # 缺硬必填（开场时间）：400 incomplete_session，且不落任何数据。
    before = client.get("/api/app/schedule", headers=bearer).json()["sessions"]
    incomplete = client.post(
        "/api/app/sessions",
        json={"script_name": "雾隐山庄"},
        headers=bearer,
    )
    assert incomplete.status_code == 400
    assert incomplete.json()["code"] == "incomplete_session"
    assert incomplete.json()["error"]
    after = client.get("/api/app/schedule", headers=bearer).json()["sessions"]
    assert len(after) == len(before)

    # 硬必填齐全（剧本全名 + 开场时间）：直接建场，201 promoted。
    start = (clock.current + timedelta(days=3)).strftime("%Y-%m-%d %H:%M")
    created = client.post(
        "/api/app/sessions",
        json={"script_name": "雾隐山庄", "start_time": start},
        headers=bearer,
    )
    assert created.status_code == 201
    body = created.json()
    assert body["status"] == "promoted"
    assert body["missing_required"] == []
    assert body["session"]["script_name"] == "雾隐山庄"
    assert body["session"]["start_time"] == start

    session = sessions[0]
    run = client.post(
        "/api/app/runs",
        json={"session_id": session["session_id"], "mode": "software"},
        headers=bearer,
    )
    assert run.status_code == 201
    assert run.json()["code"]

    view = client.get(f"/api/app/runs/{run.json()['run']['run_id']}", headers=bearer)
    assert view.status_code == 200

    # 商家 Bearer 没有推进能力；DM 接口也拒绝商家令牌。
    dm = client.get("/api/dm/run", headers=bearer)
    assert dm.status_code == 403
    assert dm.json()["code"] == "forbidden"


# ---------------------------------------------------------------------------
# WP16：商家 Bearer 的自动分配 / 应用排期 / 建场可选房间与 DM
# ---------------------------------------------------------------------------


def test_merchant_app_auto_assign_previews_and_requires_confirm(client, app):
    token = _merchant_token(client, app.state.mailer)
    bearer = {"Authorization": f"Bearer {token}", "Accept": "application/json"}

    # 默认与显式 preview:true 都是预览，不落库。
    preview = client.post(
        "/api/app/schedule/auto-assign", json={"preview": True}, headers=bearer
    )
    assert preview.status_code == 200
    assert preview.json()["preview"] is True

    # 显式 preview:false 但没有 confirm：拒绝写入。
    refused = client.post(
        "/api/app/schedule/auto-assign", json={"preview": False}, headers=bearer
    )
    assert refused.status_code == 400
    assert refused.json()["code"] == "confirm_required"

    # preview:false + confirm:true 才真正写入，响应标 preview=False。
    applied = client.post(
        "/api/app/schedule/auto-assign",
        json={"preview": False, "confirm": True},
        headers=bearer,
    )
    assert applied.status_code == 200
    body = applied.json()
    assert body["preview"] is False
    assert body["ok"] is True


def test_merchant_app_apply_endpoint(client, app):
    token = _merchant_token(client, app.state.mailer)
    bearer = {"Authorization": f"Bearer {token}", "Accept": "application/json"}

    schedule = client.get("/api/app/schedule", headers=bearer)
    assert schedule.status_code == 200
    payload = schedule.json()
    session = next(
        (
            item
            for item in payload["sessions"]
            if item.get("room_id") and item.get("dm_id")
        ),
        None,
    )
    assert session is not None, "演示种子应有已分配房间与 DM 的场次"
    room_id = session["room_id"]
    dm_id = session["dm_id"]

    applied = client.post(
        "/api/app/schedule/apply",
        json={
            "assignments": [
                {
                    "session_id": session["session_id"],
                    "room_id": room_id,
                    "dm_id": dm_id,
                    "expect_room_id": room_id,
                    "expect_dm_id": dm_id,
                }
            ],
            "confirm": True,
        },
        headers=bearer,
    )
    assert applied.status_code == 200
    assert applied.json()["status"] == "applied"

    # 预览过期保护：expect_room_id 与当前不一致时 409，一条都不写。
    stale = client.post(
        "/api/app/schedule/apply",
        json={
            "assignments": [
                {
                    "session_id": session["session_id"],
                    "room_id": room_id,
                    "dm_id": dm_id,
                    "expect_room_id": "stale",
                    "expect_dm_id": dm_id,
                }
            ],
            "confirm": True,
        },
        headers=bearer,
    )
    assert stale.status_code == 409
    assert stale.json()["code"] == "preview_stale"

    # 409 之后分配保持上一次成功写入的结果。
    after = client.get("/api/app/schedule", headers=bearer).json()["sessions"]
    refreshed = next(
        item for item in after if item["session_id"] == session["session_id"]
    )
    assert refreshed["room_id"] == room_id
    assert refreshed["dm_id"] == dm_id


def test_merchant_app_create_accepts_optional_room_and_dm(client, app):
    token = _merchant_token(client, app.state.mailer)
    bearer = {"Authorization": f"Bearer {token}", "Accept": "application/json"}

    payload = client.get("/api/app/schedule", headers=bearer).json()
    room = next(
        item
        for item in payload["rooms"]
        if item.get("active") and (item.get("capacity") or 0) >= 4
    )
    dm = next(item for item in payload["dms"] if item.get("active"))

    start = "2026-10-23 13:00"
    created = client.post(
        "/api/app/sessions",
        json={
            "script_name": "雾隐山庄",
            "start_time": start,
            "total_seats": 4,
            "room_id": room["room_id"],
            "dm_id": dm["dm_id"],
        },
        headers=bearer,
    )
    assert created.status_code == 201
    body = created.json()
    assert body["status"] == "promoted"
    # 建场先落库再用手动指定覆盖；失败只会进 manual_assign.failed，
    # 所以 status=promoted 且 manual_assign=applied 才说明指定生效。
    assert body["manual_assign"]["status"] == "applied"
    assert body["manual_assign"]["room_id"] == room["room_id"]
    assert body["manual_assign"]["dm_id"] == dm["dm_id"]
    # 开场时间不因分配而改变。
    assert body["session"]["start_time"] == start

    # 分配结果落在排期视图：新场次的房间/DM 为手动指定值。
    # （响应里的 session 子对象只有场次表字段，房间/DM 在 assignments 表。）
    after = client.get("/api/app/schedule", headers=bearer).json()["sessions"]
    promoted = next(
        item
        for item in after
        if item["script_name"] == "雾隐山庄" and item["start_time"] == start
    )
    assert promoted["room_id"] == room["room_id"]
    assert promoted["dm_id"] == dm["dm_id"]

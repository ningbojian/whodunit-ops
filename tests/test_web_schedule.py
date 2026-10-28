"""排期页与 API：直接建场（硬必填齐全即建场，无草稿）、自动分配、冲突处理。"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.web.main import create_app
from app.web.mailer import RecordingEmailSender
from app.web.models import StaticModelProvider
from tests.web_helpers import (
    MutableClock,
    csrf_from,
    fully_authorize,
    make_settings,
)

START = "2026-10-20 13:00"


def build_app(tmp_path):
    clock = MutableClock()
    settings = make_settings(tmp_path)
    app = create_app(
        settings=settings,
        mailer=RecordingEmailSender(),
        model_provider=StaticModelProvider(object()),
        now_fn=clock.now,
    )
    return app


@pytest.fixture()
def app(tmp_path):
    return build_app(tmp_path)


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


def _find_session(client, session_id: str) -> dict:
    for session in _sessions(client):
        if session["session_id"] == session_id:
            return session
    raise AssertionError(f"场次不存在：{session_id}")


def _promote(client, headers, **fields):
    """直接建场：硬必填齐全 → 200 + status=promoted（没有草稿环节）。"""
    payload = {"script_name": "雾隐山庄", "start_time": START, **fields}
    response = client.post("/api/sessions", json=payload, headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "promoted"
    return body


def test_schedule_page_requires_login(app):
    with TestClient(app) as anon:
        page = anon.get("/schedule", follow_redirects=False)
        assert page.status_code == 303
        assert page.headers["location"].startswith("/login")
        api = anon.get("/api/schedule")
        assert api.status_code == 401
        assert api.json()["code"] == "login_required"


def _slice(html: str, opening: str, closing: str) -> str:
    start = html.index(opening)
    end = html.index(closing, start) + len(closing)
    return html[start:end]


def _option_with_no_value_and_label(select_html: str, label: str) -> bool:
    return any(
        ('value=""' in part) and (label in part)
        for part in select_html.split("<option")[1:]
    )


def test_schedule_page_has_create_and_preview_dialogs(client):
    response = client.get("/schedule")
    assert response.status_code == 200
    html = response.text
    assert 'data-page="schedule"' in html
    assert "雾隐山庄" in html

    # WP16：建场进 dialog，房间/DM 可选；另加审核改动的 preview dialog。
    assert 'id="create-dialog"' in html
    assert 'id="preview-dialog"' in html
    assert 'id="add-session-button"' in html
    assert "草稿" not in html

    form = _slice(html, '<form id="session-form"', "</form>")
    assert 'id="session-script-search"' in form
    assert 'id="session-script-type"' in form
    assert 'id="session-script-players"' in form
    assert 'id="session-room"' in form
    assert 'id="session-dm"' in form
    assert 'id="session-submit"' in form
    # 房间/DM 首选项是「自动分配（推荐）」（value=""）。
    room_select = _slice(form, '<select id="session-room">', "</select>")
    dm_select = _slice(form, '<select id="session-dm">', "</select>")
    assert _option_with_no_value_and_label(room_select, "自动分配")
    assert _option_with_no_value_and_label(dm_select, "自动分配")
    assert "<datalist" not in form

    # 排期页整体无内联脚本/样式。
    assert not re.search(r"<script(?![^>]*\bsrc=)", html, re.IGNORECASE)
    assert not re.search(r"\sstyle=", html, re.IGNORECASE)

    # app.js：initSchedulePage 不再用 location.reload 刷新看板。
    # 边界核对：initResourcesPage 在 initSchedulePage 之前声明（712 < 1047），
    # initSchedulePage 之后的下一个顶层函数是 initImportsPage（3256）。
    js = (Path(__file__).resolve().parents[1] / "app" / "web" / "static" / "app.js").read_text(
        encoding="utf-8"
    )
    start = js.index("function initSchedulePage")
    assert js.index("function initResourcesPage") < start
    end = js.index("function initImportsPage")
    schedule_js = js[start:end]
    assert "function initImportsPage" in js
    assert "location.reload" not in schedule_js


def test_schedule_page_inlines_first_screen_data(client):
    """首屏数据随页面下发，浏览器不必再发一次 GET /api/schedule。

    跨境部署时那条重复请求要重跑 12 条串行查询（约 0.66 秒）。这里只校验
    页面确实带上了与 API 同形的载荷，且属性值被正常转义（JSON 里的双引号
    不能提前闭合属性）。
    """
    import json

    html = client.get("/schedule").text
    match = re.search(r'data-bootstrap="([^"]*)"', html)
    assert match, html[:400]
    raw = match.group(1)
    # Jinja 自动转义把 " 变成 &#34;；浏览器读取属性时会还原成 "。
    assert '"' not in raw
    payload = json.loads(raw.replace("&#34;", '"'))
    assert payload["ok"] is True
    assert payload["sessions"]
    assert "window" in payload and payload["window"]["start"]
    # 首屏不序列化 5800+ 行公共剧本，但形状保持为列表。
    assert payload["scripts"] == []
    # 场次载荷与 API 一致（房间/DM 名称、时长都已补好）。
    api = client.get("/api/schedule?include_scripts=0").json()
    assert payload["sessions"] == api["sessions"]


def test_create_session_promotes_directly_and_auto_assigns(client):
    """硬必填齐全直接建场：无草稿，建场即自动排房排 DM。"""
    headers = _headers(client)
    before = _sessions(client)

    created = client.post(
        "/api/sessions",
        json={"script_name": "雾隐山庄", "start_time": START},
        headers=headers,
    )
    assert created.status_code == 200, created.text
    promoted = created.json()
    assert promoted["status"] == "promoted"
    auto = promoted["auto_assign"]
    assert auto["status"] == "assigned"
    assigned = auto["assigned"]
    assert len(assigned) == 1
    assert assigned[0]["room_id"] and assigned[0]["dm_id"]

    session = _find_session(client, promoted["session"]["session_id"])
    assert session["start_time"] == START
    assert session["room_id"] == assigned[0]["room_id"]
    assert session["dm_id"] == assigned[0]["dm_id"]
    assert len(_sessions(client)) == len(before) + 1


def test_create_session_manual_room_and_dm_are_optional(client):
    """房间/DM 可选：指定成功覆盖自动结果；失败保留场次并显式报告。"""
    headers = _headers(client)
    before = _sessions(client)

    # 硬必填不齐仍然 400，且不落任何数据。
    missing_script = client.post(
        "/api/sessions", json={"start_time": START}, headers=headers
    )
    assert missing_script.status_code == 400
    assert missing_script.json()["code"] == "incomplete_session"
    assert missing_script.json()["error"]

    missing_start = client.post(
        "/api/sessions", json={"script_name": "雾隐山庄"}, headers=headers
    )
    assert missing_start.status_code == 400
    assert missing_start.json()["code"] == "incomplete_session"
    assert "开场时间" in missing_start.json()["error"]
    assert _sessions(client) == before

    # 手动指定有效的房间 + DM：applied，分配落库，开场时间不变。
    resources = client.get("/api/resources").json()
    room = next(
        item
        for item in resources["rooms"]
        if item.get("active") and (item.get("capacity") or 0) >= 4
    )
    dm = next(item for item in resources["dms"] if item.get("active"))
    created = client.post(
        "/api/sessions",
        json={
            "script_name": "雾隐山庄",
            "start_time": START,
            "total_seats": 4,
            "room_id": room["room_id"],
            "dm_id": dm["dm_id"],
        },
        headers=headers,
    )
    assert created.status_code == 200, created.text
    body = created.json()
    assert body["status"] == "promoted"
    assert body["manual_assign"]["status"] == "applied"
    session_id = body["session"]["session_id"]
    session = _find_session(client, session_id)
    assert session["room_id"] == room["room_id"]
    assert session["dm_id"] == dm["dm_id"]
    assert session["start_time"] == START

    # 未知房间：不回滚场次（200 + failed），自动分配结果保留。
    unknown = client.post(
        "/api/sessions",
        json={
            "script_name": "雾隐山庄",
            "start_time": START,
            "total_seats": 4,
            "room_id": "room-does-not-exist",
        },
        headers=headers,
    )
    assert unknown.status_code == 200, unknown.text
    unknown_body = unknown.json()
    assert unknown_body["status"] == "promoted"
    assert unknown_body["manual_assign"]["status"] == "failed"
    warnings = unknown_body.get("warnings") or []
    assert any("已建场" in item and "手动指定未生效" in item for item in warnings)
    unknown_id = unknown_body["session"]["session_id"]
    kept = _find_session(client, unknown_id)
    assert kept["room_id"] != "room-does-not-exist"

    # 容量不足：指定房间装不下场次人数，同样 failed 但场次已建。
    small = next(
        item
        for item in client.get("/api/resources").json()["rooms"]
        if item.get("active") and (item.get("capacity") or 0) >= 1
    )
    capacity = min(int(small["capacity"] or 1), 200)
    over = client.post(
        "/api/sessions",
        json={
            "script_name": "雾隐山庄",
            "start_time": START,
            "total_seats": capacity + 1,
            "room_id": small["room_id"],
        },
        headers=headers,
    )
    assert over.status_code == 200, over.text
    over_body = over.json()
    assert over_body["status"] == "promoted"
    assert over_body["manual_assign"]["status"] == "failed"
    assert any("已建场" in item for item in (over_body.get("warnings") or []))
    assert _find_session(client, over_body["session"]["session_id"])


def test_unique_candidate_script_auto_confirms_with_warning(client):
    """一个名字只对上一个本（含公共库）时走唯一候选快速通道，免一轮确认。"""
    headers = _headers(client)
    promoted = _promote(
        client, headers, script_name="迷雾云使", start_time="2026-10-24 19:00"
    )
    fields = promoted["fields"]
    # 规范名回填 + 场次不指向公共行（script_id 保持空）。
    assert fields["script_name"] == "云使"
    assert not fields["script_id"]
    warnings = promoted.get("warnings") or []
    assert any("唯一匹配" in item for item in warnings)


def test_conflict_keeps_session_and_time_then_resolve(client):
    headers = _headers(client)
    created = client.post(
        "/api/sessions",
        json={
            "script_name": "雾隐山庄",
            "start_time": START,
            "total_seats": 20,
        },
        headers=headers,
    )
    assert created.status_code == 200
    payload = created.json()
    assert payload["status"] == "promoted"
    auto = payload["auto_assign"]
    assert auto["status"] == "conflict"
    assert auto["assigned"] == []
    assert auto["conflicts"]
    conflict = auto["conflicts"][0]
    assert conflict["reason"] in {"room_capacity", "no_active_room"}

    session_id = payload["session"]["session_id"]
    session = _find_session(client, session_id)
    assert session["start_time"] == START
    assert session.get("room_id") is None and session.get("dm_id") is None

    kept = client.post(
        "/api/schedule/resolve",
        json={"session_id": session_id, "choice": "keep_unassigned"},
        headers=headers,
    )
    assert kept.status_code == 200
    assert kept.json()["status"] == "kept"

    # 容量不足的房间不能强行分配。
    rooms = client.get("/api/resources").json()["rooms"]
    small = next(room for room in rooms if room["capacity"] < 20)
    refused = client.post(
        "/api/schedule/resolve",
        json={
            "session_id": session_id,
            "choice": "switch_room",
            "target_id": small["room_id"],
        },
        headers=headers,
    )
    assert refused.status_code == 200
    assert refused.json()["status"] == "conflict"
    assert refused.json()["code"] == "room_capacity"

    # 改期/取消只给确认文案，不动场次。
    shifted = client.post(
        "/api/schedule/resolve",
        json={"session_id": session_id, "choice": "shift_time"},
        headers=headers,
    )
    assert shifted.json()["status"] == "requires_confirmation"
    assert _find_session(client, session_id)["start_time"] == START


def test_auto_assign_is_idempotent_and_keeps_start_time(client):
    headers = _headers(client)
    promoted = _promote(client, headers)
    session_id = promoted["session"]["session_id"]
    original = _find_session(client, session_id)

    # 默认即预览（不写库）：已分配的场次出现在 unchanged 里。
    preview = client.post(
        "/api/schedule/auto-assign",
        json={},
        headers=headers,
    )
    assert preview.status_code == 200
    preview_body = preview.json()
    assert preview_body["preview"] is True
    assert session_id in preview_body["unchanged"]
    assert preview_body["status"] == "unchanged"

    session = _find_session(client, session_id)
    assert session["start_time"] == original["start_time"]
    assert session["room_id"] == original["room_id"]
    assert session["dm_id"] == original["dm_id"]

    # 显式写入模式：仍是幂等 unchanged，时间与分配不动。
    written = client.post(
        "/api/schedule/auto-assign",
        json={"preview": False, "confirm": True},
        headers=headers,
    )
    assert written.status_code == 200
    write_body = written.json()
    assert write_body["preview"] is False
    assert write_body["status"] == "unchanged"
    assert session_id in write_body["unchanged"]

    session = _find_session(client, session_id)
    assert session["start_time"] == original["start_time"]
    assert session["room_id"] == original["room_id"]
    assert session["dm_id"] == original["dm_id"]


def test_plan_apply_and_manual_assign(client):
    headers = _headers(client)
    promoted = _promote(client, headers)
    session_id = promoted["session"]["session_id"]
    assigned = promoted["auto_assign"]["assigned"][0]
    original_start = _find_session(client, session_id)["start_time"]

    plan = client.post(
        "/api/schedule/plan",
        json={"start": "2026-10-20 00:00", "end": "2026-10-27 00:00"},
        headers=headers,
    )
    assert plan.status_code == 200
    planned = plan.json()
    assert isinstance(planned["optimal"], bool)
    assert isinstance(planned["assignments"], list)
    assert isinstance(planned["shortages"], list)
    assert "stats" in planned and "as_of" in planned

    refused = client.post(
        "/api/schedule/apply",
        json={
            "assignments": [
                {
                    "session_id": session_id,
                    "room_id": assigned["room_id"],
                    "dm_id": assigned["dm_id"],
                }
            ]
        },
        headers=headers,
    )
    assert refused.status_code == 400
    assert refused.json()["code"] == "confirm_required"

    applied = client.post(
        "/api/schedule/apply",
        json={
            "assignments": [
                {
                    "session_id": session_id,
                    "room_id": assigned["room_id"],
                    "dm_id": assigned["dm_id"],
                }
            ],
            "confirm": True,
        },
        headers=headers,
    )
    assert applied.status_code == 200
    assert applied.json()["status"] == "applied"
    assert _find_session(client, session_id)["start_time"] == original_start

    rooms = client.get("/api/resources").json()["rooms"]
    bigger = next(
        room
        for room in rooms
        if room["capacity"] >= 6 and room["room_id"] != assigned["room_id"]
    )
    changed = client.post(
        "/api/schedule/assign",
        json={"session_id": session_id, "room_id": bigger["room_id"]},
        headers=headers,
    )
    assert changed.status_code == 200
    session = _find_session(client, session_id)
    assert session["room_id"] == bigger["room_id"]
    assert session["start_time"] == original_start


def test_auto_assign_requires_csrf(client):
    response = client.post(
        "/api/schedule/auto-assign",
        json={},
        headers={"Accept": "application/json"},
    )
    assert response.status_code == 403
    assert response.json()["code"] == "csrf_failed"


# ---------------------------------------------------------------------------
# WP16：预览/审核/应用与 include_scripts=0
# ---------------------------------------------------------------------------


def _promote_unassigned(client, headers, start_time: str, total_seats: int = 20) -> dict:
    """建一场超出演示房间容量的场次，确保建场后仍未分配。"""
    payload = _promote(
        client, headers, start_time=start_time, total_seats=total_seats
    )
    auto = payload["auto_assign"]
    if auto["status"] == "assigned":
        # 演示房间意外变大：用更大人数强行保持未分配。
        payload = _promote(
            client, headers, start_time=start_time, total_seats=50
        )
        assert payload["auto_assign"]["status"] != "assigned"
    session_id = payload["session"]["session_id"]
    session = _find_session(client, session_id)
    assert not session.get("room_id")
    return payload


def _add_room(client, headers, name: str, capacity: int) -> dict:
    response = client.post(
        "/api/resources/rooms",
        json={"name": name, "capacity": capacity},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    room = response.json().get("room")
    assert room and room["room_id"]
    return room


def test_auto_assign_preview_then_apply_selected(client):
    headers = _headers(client)
    created = _promote_unassigned(client, headers, "2026-10-23 13:00")
    session_id = created["session"]["session_id"]
    original_start = _find_session(client, session_id)["start_time"]

    room = _add_room(client, headers, "大房 WP16", 20)

    preview = client.post(
        "/api/schedule/auto-assign", json={"preview": True}, headers=headers
    )
    assert preview.status_code == 200
    body = preview.json()
    assert body["preview"] is True
    assert body["assigned"]
    first = body["assigned"][0]
    assert first["script_name"]
    assert first["start"]
    assert body["proposals"]
    # 预览不写库：场次仍未分配。
    assert not _find_session(client, session_id).get("room_id")

    proposal = next(
        (item for item in body["proposals"] if item["session_id"] == session_id),
        None,
    )
    assert proposal is not None
    applied = client.post(
        "/api/schedule/apply",
        json={
            "assignments": [
                {
                    "session_id": session_id,
                    "room_id": proposal["after"]["room_id"],
                    "dm_id": proposal["after"]["dm_id"],
                    "expect_room_id": "",
                    "expect_dm_id": "",
                }
            ],
            "confirm": True,
        },
        headers=headers,
    )
    assert applied.status_code == 200, applied.text
    assert applied.json()["status"] == "applied"
    session = _find_session(client, session_id)
    assert session["room_id"] == room["room_id"] == proposal["after"]["room_id"]
    assert session["dm_id"] == proposal["after"]["dm_id"]
    assert session["start_time"] == original_start


def test_apply_rejects_stale_preview(client):
    headers = _headers(client)
    promoted = _promote(client, headers)
    session_id = promoted["session"]["session_id"]
    session = _find_session(client, session_id)
    room_id = session["room_id"]
    dm_id = session["dm_id"]
    assert room_id and dm_id

    stale = client.post(
        "/api/schedule/apply",
        json={
            "assignments": [
                {
                    "session_id": session_id,
                    "room_id": room_id,
                    "dm_id": dm_id,
                    "expect_room_id": "stale-room",
                    "expect_dm_id": dm_id,
                }
            ],
            "confirm": True,
        },
        headers=headers,
    )
    assert stale.status_code == 409
    assert stale.json()["code"] == "preview_stale"

    reread = _find_session(client, session_id)
    assert reread["room_id"] == room_id
    assert reread["dm_id"] == dm_id


def test_apply_writes_only_selected_rows(client):
    headers = _headers(client)
    first = _promote_unassigned(client, headers, "2026-10-23 13:00")
    second = _promote_unassigned(client, headers, "2026-10-23 20:00")
    first_id = first["session"]["session_id"]
    second_id = second["session"]["session_id"]
    _add_room(client, headers, "大房 WP16", 20)

    preview = client.post(
        "/api/schedule/auto-assign", json={"preview": True}, headers=headers
    )
    assert preview.status_code == 200
    proposals = preview.json()["proposals"]
    assert 1 <= len(proposals) <= 2
    chosen = proposals[0]
    other_id = second_id if chosen["session_id"] == first_id else first_id

    applied = client.post(
        "/api/schedule/apply",
        json={
            "assignments": [
                {
                    "session_id": chosen["session_id"],
                    "room_id": chosen["after"]["room_id"],
                    "dm_id": chosen["after"]["dm_id"],
                    "expect_room_id": "",
                    "expect_dm_id": "",
                }
            ],
            "confirm": True,
        },
        headers=headers,
    )
    assert applied.status_code == 200, applied.text
    assert applied.json()["status"] == "applied"
    # 只写勾选的那条：另一场保持未分配。
    assert _find_session(client, chosen["session_id"])["room_id"]
    assert not _find_session(client, other_id).get("room_id")


def test_include_scripts_zero_skips_library(client):
    lean = client.get("/api/schedule?include_scripts=0")
    assert lean.status_code == 200
    lean_body = lean.json()
    assert lean_body["scripts"] == []
    assert lean_body["sessions"]
    for session in lean_body["sessions"]:
        assert session["duration_minutes"] > 0

    full = client.get("/api/schedule")
    assert full.status_code == 200
    assert full.json()["scripts"]


def test_plan_proposals_are_reviewable(client):
    headers = _headers(client)
    created = _promote_unassigned(client, headers, "2026-10-23 13:00")
    session_id = created["session"]["session_id"]
    _add_room(client, headers, "大房 WP16", 20)

    plan = client.post(
        "/api/schedule/plan",
        json={"start": "2026-10-23 00:00", "end": "2026-10-24 00:00"},
        headers=headers,
    )
    assert plan.status_code == 200
    proposals = plan.json()["proposals"]
    assert proposals
    for row in proposals:
        assert isinstance(row["before"], dict)
        assert isinstance(row["after"], dict)
        assert row["source"] == "plan"
    changed = [row for row in proposals if row["changed"] is True]
    assert changed

    target = next(
        (row for row in changed if row["session_id"] == session_id), changed[0]
    )
    applied = client.post(
        "/api/schedule/apply",
        json={
            "assignments": [
                {
                    "session_id": target["session_id"],
                    "room_id": target["after"]["room_id"],
                    "dm_id": target["after"]["dm_id"],
                    "expect_room_id": target["before"]["room_id"],
                    "expect_dm_id": target["before"]["dm_id"],
                }
            ],
            "confirm": True,
        },
        headers=headers,
    )
    assert applied.status_code == 200, applied.text
    assert applied.json()["status"] == "applied"
    session = _find_session(client, target["session_id"])
    assert session["room_id"] == target["after"]["room_id"]
    assert session["dm_id"] == target["after"]["dm_id"]


# ---------------------------------------------------------------------------
# WP9：时长透出与改期撞档预检
# ---------------------------------------------------------------------------


def test_schedule_payload_includes_duration_minutes(client):
    headers = _headers(client)
    _promote(client, headers)
    payload = client.get("/api/schedule").json()
    assert payload["sessions"]
    # 工作区会播种自带时长的演示剧本（长夜将明 270、第七码头 300），
    # 期望值从同一响应的 scripts 行推导——与服务端 duration_map 同规则：
    # 同名取首个有效时长，缺剧本行的场次退回默认 240。
    duration_map: dict[str, int] = {}
    for row in payload["scripts"]:
        name = row.get("script_name")
        if not name or name in duration_map:
            continue
        try:
            minutes = int(row.get("duration_minutes"))
        except (TypeError, ValueError):
            continue
        if minutes > 0:
            duration_map[name] = minutes
    for session in payload["sessions"]:
        expected = duration_map.get(session.get("script_name"), 240)
        assert session["duration_minutes"] == expected
        assert session["duration_minutes"] > 0
    for row in payload["scripts"]:
        assert "duration_minutes" in row


def test_patch_session_time_conflict_and_force(client):
    headers = _headers(client)
    # 演示播种占用 10-20~10-22 的房间；构造在 10-23（无种子场次），任意房可共存。
    first = _promote(client, headers, start_time="2026-10-23 13:00")
    second = _promote(
        client, headers, script_name="年轮", start_time="2026-10-23 19:00"
    )
    first_id = first["session"]["session_id"]
    second_id = second["session"]["session_id"]

    # 两场都手工指到同一间房（19:00 在缓冲之外，可共存）。
    rooms = client.get("/api/resources").json()["rooms"]
    room = next(room for room in rooms if room["capacity"] >= 6)
    for session_id in (first_id, second_id):
        assigned = client.post(
            "/api/schedule/assign",
            json={"session_id": session_id, "room_id": room["room_id"]},
            headers=headers,
        )
        assert assigned.status_code == 200, assigned.text

    # 把第二场挪进第一场时段 → 预检拒绝并点名对方场次。
    clashed = client.patch(
        f"/api/sessions/{second_id}",
        json={"start_time": "2026-10-23 14:00"},
        headers=headers,
    )
    assert clashed.status_code == 400
    body = clashed.json()
    assert body["code"] == "assignment_conflict"
    assert first_id in body["error"]
    assert _find_session(client, second_id)["start_time"] == "2026-10-23 19:00"

    # 看板二次确认后的强制改期。
    forced = client.patch(
        f"/api/sessions/{second_id}",
        json={"start_time": "2026-10-23 14:00", "__force__": True},
        headers=headers,
    )
    assert forced.status_code == 200
    payload = forced.json()
    assert payload["session_id_changed"] is True
    assert payload["session"]["start_time"] == "2026-10-23 14:00"


# ---------------------------------------------------------------------------
# WP10 时间看板：拖拽依赖的 PATCH / resolve 契约
# ---------------------------------------------------------------------------


def test_patch_move_returns_recard_payload(client):
    """看板拖拽成功后的重绘数据：新编号、回读时间、chips 数据源。"""
    headers = _headers(client)
    promoted = _promote(client, headers)
    session_id = promoted["session"]["session_id"]
    moved = client.patch(
        f"/api/sessions/{session_id}",
        json={"start_time": "2026-10-23 15:00", "__force__": False},
        headers=headers,
    )
    assert moved.status_code == 200
    body = moved.json()
    assert body["session_id_changed"] is True
    assert body["previous_session_id"] == session_id
    new_id = body["session"]["session_id"]
    assert new_id != session_id
    assert body["session"]["start_time"] == "2026-10-23 15:00"
    # __force__ 是控制键不是业务字段，绝不落进回读的场次。
    assert "__force__" not in body["session"]
    # 冲突 chips 的数据源：响应必须带 auto_assign。
    auto = body["auto_assign"]
    assert isinstance(auto, dict)
    assert auto["status"] in {
        "assigned",
        "conflict",
        "unchanged",
        "resources_not_configured",
    }
    if auto["status"] == "conflict":
        assert auto["conflicts"]
        assert "options" in auto["conflicts"][0]


def test_patch_force_flag_is_not_stored(client):
    headers = _headers(client)
    promoted = _promote(client, headers)
    session_id = promoted["session"]["session_id"]
    forced = client.patch(
        f"/api/sessions/{session_id}",
        json={"start_time": "2026-10-23 16:00", "__force__": True},
        headers=headers,
    )
    assert forced.status_code == 200
    assert forced.json()["session"]["start_time"] == "2026-10-23 16:00"
    reread = _find_session(client, forced.json()["session"]["session_id"])
    assert reread["start_time"] == "2026-10-23 16:00"
    assert "__force__" not in reread


def test_resolve_still_works_after_board_patch(client):
    """看板改期后，冲突建议 chips 走的 resolve 端点仍可用。"""
    headers = _headers(client)
    promoted = _promote(client, headers)
    session_id = promoted["session"]["session_id"]
    moved = client.patch(
        f"/api/sessions/{session_id}",
        json={"start_time": "2026-10-23 17:00"},
        headers=headers,
    )
    assert moved.status_code == 200
    new_id = moved.json()["session"]["session_id"]
    rooms = client.get("/api/resources").json()["rooms"]
    room = next(room for room in rooms if room["capacity"] >= 6)
    resolved = client.post(
        "/api/schedule/resolve",
        json={
            "session_id": new_id,
            "choice": "switch_room",
            "target_id": room["room_id"],
        },
        headers=headers,
    )
    assert resolved.status_code == 200
    assert resolved.json()["status"] == "applied"
    assert _find_session(client, new_id)["room_id"] == room["room_id"]


# ---------------------------------------------------------------------------
# 草稿机制已删除：drafts 路由与载荷都不复存在
# ---------------------------------------------------------------------------


def test_session_drafts_endpoints_are_removed(client):
    headers = _headers(client)

    # 单段路径仍撞上 PATCH /api/sessions/{session_id} 的模式 → 405；
    # 多段路径无任何路由 → 404。两者都说明 drafts 端点已删除。
    listing = client.get("/api/sessions/drafts", headers=headers)
    assert listing.status_code in {404, 405}

    created = client.post(
        "/api/sessions/drafts",
        json={"fields": {"script_name": "雾隐山庄"}},
        headers=headers,
    )
    assert created.status_code in {404, 405}

    dropped = client.request(
        "DELETE", "/api/sessions/drafts/draft-x", headers=headers
    )
    assert dropped.status_code == 404


def test_schedule_payload_has_no_drafts_key(client):
    payload = client.get("/api/schedule").json()
    assert "drafts" not in payload
    assert set(payload) >= {"sessions", "scripts", "rooms", "dms", "as_of", "window"}


# ---------------------------------------------------------------------------
# WP11 状态看板：三态流转 HTTP 契约（待开场 / 已开场 / 已取消）
# ---------------------------------------------------------------------------


def test_status_board_transitions_via_patch(client):
    headers = _headers(client)
    created = _promote(client, headers)
    session_id = created["session"]["session_id"]

    # 待开场 → 已开场（店长纠错：DM 忘点时人工补记）
    started = client.patch(
        f"/api/sessions/{session_id}",
        json={"status": "已开场"},
        headers=headers,
    )
    assert started.status_code == 200, started.text
    assert _find_session(client, session_id)["status"] == "已开场"

    # 已开场 → 已取消
    cancelled = client.patch(
        f"/api/sessions/{session_id}",
        json={"status": "已取消"},
        headers=headers,
    )
    assert cancelled.status_code == 200, cancelled.text
    assert _find_session(client, session_id)["status"] == "已取消"

    # 已取消 → 待开场（纠错恢复）
    restored = client.patch(
        f"/api/sessions/{session_id}",
        json={"status": "待开场"},
        headers=headers,
    )
    assert restored.status_code == 200, restored.text
    assert _find_session(client, session_id)["status"] == "待开场"

    # 非法状态被拒
    bad = client.patch(
        f"/api/sessions/{session_id}",
        json={"status": "进行中"},
        headers=headers,
    )
    assert bad.status_code == 400
    assert bad.json()["code"] == "status_invalid"

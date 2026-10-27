"""资源页与资源 API：鉴权、CSRF、模板、导入预览/确认、演示资源。"""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from app.web.main import create_app
from app.web.mailer import RecordingEmailSender
from app.web.models import StaticModelProvider
from tests.web_helpers import (
    ORIGIN,
    MutableClock,
    csrf_from,
    fully_authorize,
    make_settings,
)


def build_app(tmp_path, clock, **overrides):
    settings = make_settings(tmp_path, **overrides)
    return create_app(
        settings=settings,
        mailer=RecordingEmailSender(),
        model_provider=StaticModelProvider(object()),
        now_fn=clock.now,
    )


@pytest.fixture()
def app(tmp_path):
    return build_app(tmp_path, MutableClock())


@pytest.fixture()
def client(app):
    with TestClient(app) as value:
        fully_authorize(value, app.state.mailer)
        yield value


def _csrf(client) -> str:
    return csrf_from(client.get("/resources").text)


def _json_headers(client) -> dict[str, str]:
    return {
        "X-CSRF-Token": _csrf(client),
        "Accept": "application/json",
        "Origin": "http://testserver",
    }


def test_resources_page_requires_login(app):
    with TestClient(app) as anon:
        page = anon.get("/resources", follow_redirects=False)
        assert page.status_code == 303
        assert page.headers["location"].startswith("/login")

        api = anon.get("/api/resources")
        assert api.status_code == 401
        assert api.json()["code"] == "login_required"


def test_resources_page_is_dark_csp_safe_and_seeded(client):
    response = client.get("/resources")
    assert response.status_code == 200
    html = response.text
    assert 'data-page="resources"' in html
    assert "资源" in html
    assert "雾隐山庄" in html

    assert not re.search(r"<script(?![^>]*\bsrc=)", html, re.IGNORECASE)
    assert not re.search(r"\sstyle=", html, re.IGNORECASE)
    assert "https://" not in html
    assert "unsafe-inline" not in response.headers["Content-Security-Policy"]

    data = client.get("/api/resources").json()
    assert data["ok"] is True
    names = {room["name"] for room in data["rooms"]}
    assert {"A 房", "B 房", "C 房"} <= names
    assert {dm["name"] for dm in data["dms"]} == {"小北", "阿岚", "小满"}
    assert data["counts"]["rooms"] == 3
    assert data["setup"]["rooms"] == 3
    assert data["setup"]["active_rooms"] == 3


def test_saving_room_requires_csrf_and_validates(client):
    without = client.post(
        "/api/resources/rooms",
        json={"name": "D 房", "capacity": 6},
        headers={"Accept": "application/json"},
    )
    assert without.status_code == 403
    assert without.json()["code"] == "csrf_failed"

    headers = _json_headers(client)
    missing_name = client.post("/api/resources/rooms", json={"capacity": 6}, headers=headers)
    assert missing_name.status_code == 400

    bad_capacity = client.post(
        "/api/resources/rooms", json={"name": "D 房", "capacity": 0}, headers=headers
    )
    assert bad_capacity.status_code == 400

    created = client.post(
        "/api/resources/rooms",
        json={
            "name": "D 房",
            "capacity": 6,
            "tags": "推理",
            "buffer_minutes": 10,
            "hourly_cost": 20,
            "notes": "新加",
        },
        headers=headers,
    )
    assert created.status_code == 200
    payload = created.json()
    assert payload["room"]["name"] == "D 房"
    assert payload["room"]["capacity"] == 6
    assert payload["room"]["buffer_minutes"] == 10
    assert any(room["name"] == "D 房" for room in payload["resources"]["rooms"])

    # 同名再次保存走更新而不是新增。
    again = client.post(
        "/api/resources/rooms",
        json={"name": "D 房", "capacity": 8},
        headers=headers,
    )
    assert again.status_code == 200
    names = [room["name"] for room in again.json()["resources"]["rooms"]]
    assert names.count("D 房") == 1
    assert again.json()["room"]["room_id"] == payload["room"]["room_id"]


def test_saving_dm_with_shifts_and_invalid_shift(client):
    headers = _json_headers(client)
    created = client.post(
        "/api/resources/dms",
        json={
            "name": "阿七",
            "skills": "推理/欢乐",
            "max_sessions_per_day": 2,
            "shifts": [
                {"weekday": 5, "start": "12:00", "end": "23:00"},
                {"weekday": "周日", "start": "13:00", "end": "22:00"},
            ],
        },
        headers=headers,
    )
    assert created.status_code == 200
    payload = created.json()
    assert payload["dm"]["name"] == "阿七"
    saved = {dm["name"]: dm for dm in payload["resources"]["dms"]}
    assert len(saved["阿七"]["shifts"]) == 2

    invalid = client.post(
        "/api/resources/dms",
        json={"name": "阿七", "shifts": [{"weekday": 9, "start": "10:00", "end": "11:00"}]},
        headers=headers,
    )
    assert invalid.status_code == 400
    assert invalid.json()["code"] == "invalid_shift"


def test_demo_resources_require_confirm_and_do_not_overwrite(client):
    headers = _json_headers(client)
    refused = client.post("/api/resources/demo", json={}, headers=headers)
    assert refused.status_code == 400
    assert refused.json()["code"] == "confirm_required"

    # 工作区首次进入已经有演示资源；再次载入必须如实返回未覆盖。
    ok = client.post("/api/resources/demo", json={"confirm": True}, headers=headers)
    assert ok.status_code == 200
    payload = ok.json()
    assert payload["resources"]["seeded"] is False
    assert payload["resources"]["reason"] == "resources_exist"
    assert payload["resources_view"]["counts"]["rooms"] == 3


@pytest.mark.parametrize(
    ("kind", "filename", "marker"),
    [
        ("rooms", "rooms.csv", "房间编号,房间名称"),
        ("dms", "dms.csv", "DM编号,DM名称"),
        ("shifts", "shifts.csv", "DM名称,星期"),
    ],
)
def test_resource_template_downloads(kind, filename, marker):
    clock = MutableClock()
    app = build_app(
        __import__("pathlib").Path(__import__("tempfile").mkdtemp()),
        clock,
    )
    with TestClient(app) as client:
        fully_authorize(client, app.state.mailer)
        response = client.get(f"/resources/template/{kind}.csv")
    assert response.status_code == 200
    assert response.text.startswith("﻿")
    assert marker in response.text
    assert filename in response.headers["content-disposition"]
    assert "text/csv" in response.headers["content-type"]


def test_import_preview_does_not_write_then_confirm_writes_valid_rows(client):
    headers = {"X-CSRF-Token": _csrf(client), "Accept": "application/json"}
    csv = (
        "room_id,name,capacity,tags,hourly_cost,buffer_minutes,active,notes\n"
        ",D 房,6,推理,0,15,true,\n"
        ",E 房,0,,0,15,true,\n"
    ).encode("utf-8-sig")

    preview = client.post(
        "/api/resources/import",
        files={"file": ("rooms.csv", csv, "text/csv")},
        data={"kind": "rooms"},
        headers=headers,
    )
    assert preview.status_code == 200
    payload = preview.json()
    assert payload["confirmed"] is False
    assert payload["total_rows"] == 2
    assert payload["valid_rows"] == 1
    assert payload["errors"][0]["row"] == 3
    assert "capacity" in payload["errors"][0]["error"]

    names = {room["name"] for room in client.get("/api/resources").json()["rooms"]}
    assert "D 房" not in names

    confirmed = client.post(
        "/api/resources/import",
        files={"file": ("rooms.csv", csv, "text/csv")},
        data={"kind": "rooms", "confirm": "true"},
        headers=headers,
    )
    assert confirmed.status_code == 200
    result = confirmed.json()
    assert result["confirmed"] is True
    assert result["counts"] == {"imported": 1, "errors": 1}

    rooms = {room["name"]: room for room in client.get("/api/resources").json()["rooms"]}
    assert rooms["D 房"]["capacity"] == 6
    assert "E 房" not in rooms


def test_chinese_header_import_previews_then_confirms(client):
    headers = {"X-CSRF-Token": _csrf(client), "Accept": "application/json"}
    csv = (
        "房间编号,房间名称,可容纳人数,标签,每小时成本,缓冲分钟,是否启用,备注\n"
        ",D 房,6,推理,0,15,是,\n"
    ).encode("utf-8-sig")

    preview = client.post(
        "/api/resources/import",
        files={"file": ("rooms.csv", csv, "text/csv")},
        data={"kind": "rooms"},
        headers=headers,
    )
    assert preview.status_code == 200
    payload = preview.json()
    assert payload["confirmed"] is False
    assert payload["valid_rows"] == 1
    names = {room["name"] for room in client.get("/api/resources").json()["rooms"]}
    assert "D 房" not in names

    confirmed = client.post(
        "/api/resources/import",
        files={"file": ("rooms.csv", csv, "text/csv")},
        data={"kind": "rooms", "confirm": "true"},
        headers=headers,
    )
    assert confirmed.status_code == 200
    result = confirmed.json()
    assert result["confirmed"] is True
    assert result["counts"] == {"imported": 1, "errors": 0}

    rooms = {room["name"]: room for room in client.get("/api/resources").json()["rooms"]}
    assert "D 房" in rooms
    assert rooms["D 房"]["capacity"] == 6


def test_mixed_chinese_english_headers_import(client):
    # 逐列别名：中英表头混排也能映射，不是全有或全无。
    headers = {"X-CSRF-Token": _csrf(client), "Accept": "application/json"}
    csv = "name,可容纳人数,标签,active\nG 房,7,推理,true\n".encode("utf-8-sig")

    preview = client.post(
        "/api/resources/import",
        files={"file": ("rooms.csv", csv, "text/csv")},
        data={"kind": "rooms"},
        headers=headers,
    )
    assert preview.status_code == 200
    payload = preview.json()
    assert payload["confirmed"] is False
    assert payload["valid_rows"] == 1


def test_missing_required_column_is_rejected_with_chinese_guidance(client):
    headers = {"X-CSRF-Token": _csrf(client), "Accept": "application/json"}
    csv = "房间编号,可容纳人数\n,6\n".encode("utf-8-sig")

    response = client.post(
        "/api/resources/import",
        files={"file": ("rooms.csv", csv, "text/csv")},
        data={"kind": "rooms"},
        headers=headers,
    )
    assert response.status_code == 400
    body = response.json()
    assert body["code"] == "missing_columns"
    assert "房间名称" in body["error"]
    assert "模板表头" in body["error"]


def test_import_rejects_bad_kind_and_empty_confirm(client):
    headers = {"X-CSRF-Token": _csrf(client), "Accept": "application/json"}
    bad = client.post(
        "/api/resources/import",
        files={"file": ("x.csv", b"name\nA\n", "text/csv")},
        data={"kind": "players"},
        headers=headers,
    )
    assert bad.status_code == 400
    assert bad.json()["code"] == "bad_request"

    empty = client.post(
        "/api/resources/import",
        files={"file": ("rooms.csv", b"room_id,name\n", "text/csv")},
        data={"kind": "rooms", "confirm": "true"},
        headers=headers,
    )
    assert empty.status_code == 400
    assert empty.json()["code"] == "invalid_import"


def test_import_size_limit(tmp_path):
    clock = MutableClock()
    app = build_app(tmp_path, clock, max_import_bytes=200)
    with TestClient(app) as client:
        fully_authorize(client, app.state.mailer)
        csrf = csrf_from(client.get("/resources").text)
        oversized = b"room_id,name\n" + b"x" * 400
        response = client.post(
            "/api/resources/import",
            files={"file": ("rooms.csv", oversized, "text/csv")},
            data={"kind": "rooms"},
            headers={"X-CSRF-Token": csrf, "Accept": "application/json"},
        )
    assert response.status_code == 413
    assert response.json()["code"] == "file_too_large"


def test_tenant_isolation_of_resources(app):
    # 两个客户端的生命周期重叠，避免先退出的 lifespan 关闭共享的访问库。
    with TestClient(app) as first, TestClient(app) as second:
        fully_authorize(first, app.state.mailer, email="first@example.com")
        headers = _json_headers(first)
        created = first.post(
            "/api/resources/rooms",
            json={"name": "只属于第一家", "capacity": 6},
            headers=headers,
        )
        assert created.status_code == 200

        fully_authorize(second, app.state.mailer, email="second@example.com")
        rooms = second.get("/api/resources").json()["rooms"]
        assert "只属于第一家" not in {room["name"] for room in rooms}


# ---------------------------------------------------------------------------
# WP13A /api/scripts 筛选 + 分页
# ---------------------------------------------------------------------------


def _seeded_script_count(client) -> int:
    payload = client.get("/api/scripts").json()
    assert payload["ok"] is True
    return payload["total"]


def test_scripts_api_filters_by_type_and_seats(client):
    total = _seeded_script_count(client)
    assert total >= 4  # 演示种子 4 部私有 + 可能的公共库种子

    only_inference = client.get("/api/scripts?type=推理").json()
    assert only_inference["ok"] is True
    assert only_inference["total"] >= 1
    assert {item["script_type"] for item in only_inference["items"]} == {"推理"}

    seats = client.get("/api/scripts?seats_min=7&seats_max=7").json()
    assert seats["ok"] is True
    # 演示种子：长夜将明 7 人。
    assert any(item["script_name"] == "长夜将明" for item in seats["items"])
    assert all(item["total_seats"] == 7 for item in seats["items"])

    duration = client.get("/api/scripts?duration_min=280").json()
    assert duration["ok"] is True
    assert all(item["duration_minutes"] >= 280 for item in duration["items"])
    assert any(item["script_name"] == "第七码头" for item in duration["items"])

    search = client.get("/api/scripts?q=雾隐").json()
    assert search["ok"] is True
    assert any(item["script_name"] == "雾隐山庄" for item in search["items"])

    combined = client.get("/api/scripts?type=情感&seats_max=8").json()
    assert combined["ok"] is True
    assert all(item["script_type"] == "情感" for item in combined["items"])


def test_scripts_api_paginates_and_clamps_page_size(client):
    page_size = client.get("/api/scripts?page_size=10").json()
    assert page_size["page"] == 1
    assert page_size["page_size"] == 10

    # page_size 夹取 10..200。
    too_small = client.get("/api/scripts?page_size=2").json()
    assert too_small["page_size"] == 10
    too_big = client.get("/api/scripts?page_size=9999").json()
    assert too_big["page_size"] == 200

    # 越后页夹到最后一页，不报错。
    beyond = client.get("/api/scripts?page=999").json()
    assert beyond["ok"] is True
    assert beyond["page"] == max(beyond["pages"], 1)

    # 第二页条目与第一页不重叠（总量足够时）。
    total = page_size["total"]
    if total > 10:
        first = client.get("/api/scripts?page_size=10&page=1").json()
        second = client.get("/api/scripts?page_size=10&page=2").json()
        first_ids = {item["script_id"] for item in first["items"]}
        second_ids = {item["script_id"] for item in second["items"]}
        assert not first_ids & second_ids


def test_scripts_api_reports_source_and_public_is_read_only(client):
    payload = client.get("/api/scripts").json()
    sources = {item["source"] for item in payload["items"]}
    assert "私有" in sources
    for item in payload["items"]:
        assert item["read_only"] is (item["source"] == "公共库")

    public_only = client.get("/api/scripts?source=公共库").json()
    assert public_only["ok"] is True
    assert all(item["source"] == "公共库" for item in public_only["items"])

    private_only = client.get("/api/scripts?source=私有").json()
    assert all(item["source"] == "私有" for item in private_only["items"])


def test_resources_page_renders_scripts_first_page_without_js(client):
    html = client.get("/resources").text
    assert 'data-scripts-panel' in html
    assert 'data-filter="q"' in html
    assert 'data-filter="source"' in html
    assert "剧本库" in html
    # 首屏服务端兜底：种子剧本出现在 tbody 中。
    tbody = html.split('id="scripts-tbody"', 1)[1].split("</tbody>", 1)[0]
    assert "雾隐山庄" in tbody
    # 来源列。
    assert "私有" in tbody
    # CSP 依然安全：无内联脚本/样式。
    assert not re.search(r"<script(?![^>]*\bsrc=)", html, re.IGNORECASE)
    assert not re.search(r"\sstyle=", html, re.IGNORECASE)


# ---------------------------------------------------------------------------
# WP13B：DM 请假 HTTP 端点
# ---------------------------------------------------------------------------


def _first_dm_id(client) -> str:
    data = client.get("/api/resources").json()
    return data["dms"][0]["dm_id"]


def test_dm_leave_http_roundtrip_idempotent(client):
    dm_id = _first_dm_id(client)
    headers = _json_headers(client)

    created = client.post(
        "/api/resources/dm-leaves",
        json={"dm_id": dm_id, "day": "2026-10-21", "note": "事假"},
        headers=headers,
    )
    assert created.status_code == 200
    body = created.json()
    assert body["ok"] is True
    assert body["created"] is True
    leave_id = body["leave_id"]
    matched = [
        leave
        for dm in body["resources"]["dms"]
        if dm["dm_id"] == dm_id
        for leave in dm["leaves"]
        if leave["leave_id"] == leave_id
    ]
    assert len(matched) == 1
    assert matched[0]["day"] == "2026-10-21"

    again = client.post(
        "/api/resources/dm-leaves",
        json={"dm_id": dm_id, "day": "2026-10-21", "note": "改病假"},
        headers=headers,
    )
    assert again.json()["created"] is False
    assert again.json()["leave_id"] == leave_id

    removed = client.delete(
        f"/api/resources/dm-leaves/{leave_id}", headers=headers
    )
    assert removed.status_code == 200
    assert removed.json()["removed"] is True

    missing = client.delete(
        f"/api/resources/dm-leaves/{leave_id}", headers=headers
    )
    # 与 session_not_found 等既有模式一致：业务码 not_found + HTTP 400。
    assert missing.status_code == 400
    assert missing.json()["code"] == "not_found"


def test_dm_leave_requires_csrf(client):
    dm_id = _first_dm_id(client)
    response = client.post(
        "/api/resources/dm-leaves",
        json={"dm_id": dm_id, "day": "2026-10-21"},
        headers={"Accept": "application/json", "Origin": "http://testserver"},
    )
    assert response.status_code == 403
    assert response.json()["code"] == "csrf_failed"


def test_dm_leave_validation_errors(client):
    dm_id = _first_dm_id(client)
    headers = _json_headers(client)
    no_dm = client.post(
        "/api/resources/dm-leaves", json={"day": "2026-10-21"}, headers=headers
    )
    assert no_dm.status_code == 400
    assert no_dm.json()["code"] == "bad_request"

    no_day = client.post(
        "/api/resources/dm-leaves", json={"dm_id": dm_id}, headers=headers
    )
    assert no_day.status_code == 400


def test_resources_payload_includes_leaves_and_history_keys(client):
    data = client.get("/api/resources").json()
    for dm in data["dms"]:
        assert isinstance(dm["leaves"], list)
        assert "past_runs" in dm
        assert "distinct_scripts" in dm
        assert "recent_scripts" in dm
        # script_counts 不进 API 载荷（体积控制）。
        assert "script_counts" not in dm

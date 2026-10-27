"""快速记单与大众点评导入：容量、幂等、预览、确认与数据截止。"""

from __future__ import annotations

import csv
import io

import pytest
from fastapi.testclient import TestClient

from app.web.main import create_app
from app.web.mailer import RecordingEmailSender
from app.web.models import StaticModelProvider
from tests.web_helpers import MutableClock, csrf_from, fully_authorize, make_settings


def build_app(tmp_path, **overrides):
    clock = MutableClock()
    app = create_app(
        settings=make_settings(tmp_path, **overrides),
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


def _headers(client) -> dict[str, str]:
    return {
        "X-CSRF-Token": csrf_from(client.get("/imports").text),
        "Accept": "application/json",
        "Origin": "http://testserver",
    }


def _sessions(client) -> list[dict]:
    return client.get("/api/schedule").json()["sessions"]


def _session(client, script_name: str) -> dict:
    for session in _sessions(client):
        if session["script_name"] == script_name:
            return session
    raise AssertionError(f"找不到演示场次：{script_name}")


def _booked(client, session_id: str) -> int:
    for session in _sessions(client):
        if session["session_id"] == session_id:
            return int(session.get("booked_seats") or 0)
    raise AssertionError("场次不存在")


def _dianping_csv(rows: list[list[str]]) -> bytes:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["订单号", "场次ID", "人数", "联系人", "订单状态", "手机号"])
    writer.writerows(rows)
    return buffer.getvalue().encode("utf-8-sig")


def _upload(client, headers, content: bytes, *, confirm: bool = False, data_as_of=None):
    data = {"confirm": "true"} if confirm else {}
    if data_as_of:
        data["data_as_of"] = data_as_of
    return client.post(
        "/api/imports/dianping/commit" if confirm else "/api/imports/dianping/preview",
        files={"file": ("dianping.csv", content, "text/csv")},
        data=data,
        headers=headers,
    )


def test_imports_page_requires_login_and_renders(app):
    # 两个客户端生命周期重叠，避免先退出的 lifespan 关闭共享的访问库。
    with TestClient(app) as anon, TestClient(app) as client:
        page = anon.get("/imports", follow_redirects=False)
        assert page.status_code == 303
        assert page.headers["location"].startswith("/login")

        fully_authorize(client, app.state.mailer)
        response = client.get("/imports")
    assert response.status_code == 200
    html = response.text
    assert 'data-page="imports"' in html
    assert "大众点评" in html
    assert "数据截止" in html
    assert "不抓取" in html
    assert "https://" not in html


def test_quick_booking_is_idempotent_and_respects_capacity(client):
    headers = _headers(client)
    session = _session(client, "长夜将明")
    session_id = session["session_id"]
    start = _booked(client, session_id)
    remaining = int(session["total_seats"]) - start
    assert remaining >= 3

    first = client.post(
        "/api/bookings/quick",
        json={"session_id": session_id, "seats": 2, "client_ref": "clientref-0001"},
        headers=headers,
    )
    assert first.status_code == 200
    payload = first.json()
    assert payload["unchanged"] is False
    assert payload["booking_id"]
    assert _booked(client, session_id) == start + 2

    repeat = client.post(
        "/api/bookings/quick",
        json={"session_id": session_id, "seats": 2, "client_ref": "clientref-0001"},
        headers=headers,
    )
    assert repeat.status_code == 200
    assert repeat.json()["unchanged"] is True
    assert _booked(client, session_id) == start + 2

    bad_ref = client.post(
        "/api/bookings/quick",
        json={"session_id": session_id, "seats": 1, "client_ref": "短"},
        headers=headers,
    )
    assert bad_ref.status_code == 400
    assert bad_ref.json()["code"] == "bad_request"

    fill = client.post(
        "/api/bookings/quick",
        json={
            "session_id": session_id,
            "seats": remaining - 2,
            "client_ref": "clientref-0002",
        },
        headers=headers,
    )
    assert fill.status_code == 200
    assert _booked(client, session_id) == int(session["total_seats"])

    overflow = client.post(
        "/api/bookings/quick",
        json={"session_id": session_id, "seats": 1, "client_ref": "clientref-0003"},
        headers=headers,
    )
    assert overflow.status_code == 409
    assert overflow.json()["code"] == "capacity_exceeded"
    assert overflow.json()["remaining_seats"] == 0


def test_preview_then_confirm_imports_only_valid_rows(client):
    headers = _headers(client)
    session = _session(client, "长夜将明")
    session_id = session["session_id"]
    start = _booked(client, session_id)

    content = _dianping_csv(
        [
            ["DP-1", session_id, "2", "小李", "已支付", "13800138000"],
            ["DP-2", session_id, "0", "小王", "已支付", ""],
            ["", session_id, "1", "小张", "已支付", ""],
            ["DP-4", "missing-session", "1", "小赵", "已支付", ""],
        ]
    )

    preview = _upload(client, headers, content)
    assert preview.status_code == 200
    data = preview.json()
    assert data["mapping"]["external_order_id"] == "订单号"
    assert data["mapping"]["session_id"] == "场次ID"
    assert "phone" not in data["mapping"]
    assert data["total_rows"] == 4
    assert data["valid_rows"] == 1
    assert len(data["errors"]) == 3
    assert data["unmatched"][0]["code"] == "session_not_found"
    assert any("手机号列只识别" in item for item in data["warnings"])
    assert len(data["preview"]) <= 10

    # 预览不写预约。
    assert _booked(client, session_id) == start

    commit_without_confirm = client.post(
        "/api/imports/dianping/commit",
        files={"file": ("dianping.csv", content, "text/csv")},
        headers=headers,
    )
    assert commit_without_confirm.status_code == 400
    assert commit_without_confirm.json()["code"] == "confirm_required"

    committed = _upload(client, headers, content, confirm=True)
    assert committed.status_code == 200
    result = committed.json()
    assert result["imported"] == 1
    assert result["rejected"] == 3
    assert _booked(client, session_id) == start + 2

    # 重复导入同一文件：不重复加座。
    again = _upload(client, headers, content, confirm=True)
    assert again.status_code == 200
    assert again.json()["imported"] == 0
    assert again.json()["unchanged"] == 1
    assert _booked(client, session_id) == start + 2


def test_cancel_order_releases_seats(client):
    headers = _headers(client)
    session = _session(client, "第七码头")
    session_id = session["session_id"]
    start = _booked(client, session_id)

    created = _upload(
        client,
        headers,
        _dianping_csv([["DP-CANCEL", session_id, "2", "小周", "已支付", ""]]),
        confirm=True,
    )
    assert created.status_code == 200
    assert created.json()["imported"] == 1
    assert _booked(client, session_id) == start + 2

    cancelled = _upload(
        client,
        headers,
        _dianping_csv([["DP-CANCEL", session_id, "2", "小周", "已退款", ""]]),
        confirm=True,
    )
    assert cancelled.status_code == 200
    assert cancelled.json()["cancelled"] == 1
    assert _booked(client, session_id) == start


def test_over_capacity_rejects_whole_session_group(client):
    headers = _headers(client)
    session = _session(client, "山鬼")
    session_id = session["session_id"]
    start = _booked(client, session_id)
    remaining = int(session["total_seats"]) - start

    content = _dianping_csv(
        [
            ["DP-OK", session_id, "1", "小甲", "已支付", ""],
            ["DP-BIG", session_id, str(remaining + 1), "小乙", "已支付", ""],
        ]
    )
    preview = _upload(client, headers, content)
    assert preview.status_code == 200
    data = preview.json()
    assert data["over_capacity"]
    assert data["over_capacity"][0]["session_id"] == session_id
    assert data["valid_rows"] == 0

    committed = _upload(client, headers, content, confirm=True)
    assert committed.status_code == 200
    assert committed.json()["imported"] == 0
    assert committed.json()["rejected"] == 2
    assert _booked(client, session_id) == start


def test_stale_data_as_of_is_flagged(client):
    headers = _headers(client)
    session = _session(client, "雾隐山庄")
    content = _dianping_csv([["DP-STALE", session["session_id"], "1", "小陈", "已支付", ""]])
    preview = _upload(client, headers, content, data_as_of="2026-10-20 06:00")
    assert preview.status_code == 200
    data = preview.json()
    assert data["stale"] is True
    assert any("数据已过期" in item for item in data["warnings"])


def test_import_rejects_bad_files_and_oversize(tmp_path):
    clock = MutableClock()
    app = build_app(tmp_path, max_import_bytes=300)
    with TestClient(app) as client:
        fully_authorize(client, app.state.mailer)
        headers = {
            "X-CSRF-Token": csrf_from(client.get("/imports").text),
            "Accept": "application/json",
        }
        bad_name = client.post(
            "/api/imports/dianping/preview",
            files={"file": ("orders.txt", b"order_id,seats\nA,1\n", "text/plain")},
            headers=headers,
        )
        assert bad_name.status_code == 400
        assert bad_name.json()["code"] == "bad_file"

        oversized = client.post(
            "/api/imports/dianping/preview",
            files={"file": ("orders.csv", b"order_id,seats\n" + b"x" * 500, "text/csv")},
            headers=headers,
        )
        assert oversized.status_code == 413
        assert oversized.json()["code"] == "file_too_large"


def test_database_mode_records_import_run(tmp_path):
    clock = MutableClock()
    app = build_app(
        tmp_path,
        storage_backend="database",
        database_sqlite_path=tmp_path / "web.sqlite3",
    )
    with TestClient(app) as client:
        fully_authorize(client, app.state.mailer)
        headers = {
            "X-CSRF-Token": csrf_from(client.get("/imports").text),
            "Accept": "application/json",
        }
        session = _session(client, "长夜将明")
        content = _dianping_csv(
            [["DP-DB-1", session["session_id"], "1", "小何", "已支付", ""]]
        )
        committed = _upload(client, headers, content, confirm=True)
        assert committed.status_code == 200
        assert committed.json()["imported"] == 1

    from sqlalchemy import select

    from app.store.schema import import_runs

    with app.state.engine.connect() as conn:
        rows = conn.execute(
            select(import_runs).where(import_runs.c.source == "dianping_csv")
        ).all()
    assert len(rows) == 1
    assert rows[0].status == "success"
    assert rows[0].booking_rows == 1
    assert rows[0].tenant_id

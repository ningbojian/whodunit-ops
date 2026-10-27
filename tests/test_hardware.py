"""硬件 Skill 适配器契约，以及硬件失败不阻断阶段流转的集成行为。"""

from __future__ import annotations

import contextlib
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pandas as pd
import pytest

from app.integrations.hardware import (
    HardwareConfigError,
    HardwareResult,
    HttpHardwareAdapter,
    NullHardwareAdapter,
    build_hardware_adapter,
    hardware_configured,
)
from app.store.engine import create_database_engine, ensure_schema
from app.store.excel import ExcelStore
from app.store.sql import SqlStore
from app.tools.dm_control import (
    apply_action,
    create_run,
    redeem_code,
)
from app.tools.scripts import upsert_script
from app.tools.stages import save_stages
from app.tools.storage import upsert_sessions

SECRET = "hardware-test-secret"
TOKEN = "hardware-skill-token-9f3a"
NOW = pd.Timestamp("2026-10-20 12:00")
SCENE = {"light": "warm_low", "sound": "intro_01", "volume": 40}


# ---------------------------------------------------------------------------
# 测试替身与本地 HTTP 服务
# ---------------------------------------------------------------------------


class RecordingHardware:
    name = "recording"
    configured = True

    def __init__(self, results=None):
        self.calls = []
        self.results = list(results or [])

    def send_scene(self, **kwargs):
        self.calls.append(kwargs)
        if self.results:
            return self.results.pop(0)
        return HardwareResult(ok=True, status="ok", status_code=200, latency_ms=1)


@contextlib.contextmanager
def local_server(
    *,
    status=200,
    body=b'{"ok": true}',
    delay=0.0,
    redirect=None,
    records=None,
):
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length)
            if records is not None:
                records.append(
                    {
                        "path": self.path,
                        "headers": dict(self.headers),
                        "body": json.loads(raw.decode("utf-8") or "null"),
                    }
                )
            if delay:
                time.sleep(delay)
            if redirect:
                self.send_response(302)
                self.send_header("Location", redirect)
                self.end_headers()
                return
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            if body:
                self.wfile.write(body)

        def log_message(self, *args):  # noqa: ANN002
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/scene"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


# ---------------------------------------------------------------------------
# 适配器
# ---------------------------------------------------------------------------


def test_null_adapter_is_skipped():
    adapter = NullHardwareAdapter()
    assert adapter.configured is False
    result = adapter.send_scene(
        run_id="r", session_id="s", room_id=None, stage={"hardware_scene": SCENE}
    )
    assert result.ok is False
    assert result.status == "skipped"
    assert "软件模式" in result.message


def test_build_adapter_without_url_returns_null():
    adapter = build_hardware_adapter(url="", token=TOKEN)
    assert isinstance(adapter, NullHardwareAdapter)


def test_build_adapter_from_env(monkeypatch):
    monkeypatch.setenv("HARDWARE_SKILL_URL", "https://hardware.example.com/hook")
    monkeypatch.setenv("HARDWARE_SKILL_TOKEN", TOKEN)
    monkeypatch.setenv("HARDWARE_TIMEOUT_SECONDS", "3")
    adapter = build_hardware_adapter()
    assert isinstance(adapter, HttpHardwareAdapter)
    assert adapter.url == "https://hardware.example.com/hook"
    assert adapter.token == TOKEN
    assert adapter.timeout_seconds == 3.0
    assert adapter.configured is True
    assert hardware_configured() is True

    monkeypatch.delenv("HARDWARE_SKILL_URL")
    monkeypatch.delenv("HARDWARE_SKILL_TOKEN")
    monkeypatch.delenv("HARDWARE_TIMEOUT_SECONDS")
    assert hardware_configured() is False
    assert isinstance(build_hardware_adapter(), NullHardwareAdapter)


@pytest.mark.parametrize(
    "url",
    [
        "http://hardware.example.com/hook",
        "ftp://127.0.0.1/hook",
        "not-a-url",
    ],
)
def test_adapter_rejects_unsafe_urls(url):
    with pytest.raises(HardwareConfigError):
        HttpHardwareAdapter(url=url)


@pytest.mark.parametrize("timeout", [0.1, 0.49, 5.1, 10])
def test_adapter_rejects_timeout_out_of_range(timeout):
    with pytest.raises(HardwareConfigError):
        HttpHardwareAdapter(url="http://127.0.0.1:9/hook", timeout_seconds=timeout)


def test_adapter_skips_stage_without_scene_without_network():
    adapter = HttpHardwareAdapter(url="http://127.0.0.1:9/hook", timeout_seconds=0.5)
    result = adapter.send_scene(
        run_id="r", session_id="s", room_id=None, stage={"stage_id": "st-1"}
    )
    assert result.ok is False
    assert result.status == "skipped"
    assert "没有配置硬件场景" in result.message


def test_http_success_sends_expected_payload_and_auth_header():
    records = []
    with local_server(body=b'{"ok": true, "message": "scene applied"}', records=records) as url:
        adapter = HttpHardwareAdapter(url=url, token=TOKEN, timeout_seconds=1)
        stage = {
            "stage_id": "stage-1",
            "seq": 3,
            "hardware_scene": SCENE,
        }
        result = adapter.send_scene(
            run_id="run-1",
            session_id="session-1",
            room_id="room-a",
            stage=stage,
            event_id="evt-1",
        )

    assert result.ok is True
    assert result.status == "ok"
    assert result.status_code == 200
    assert result.message == "scene applied"
    assert result.latency_ms is not None

    assert len(records) == 1
    request = records[0]
    assert request["path"] == "/scene"
    assert request["headers"]["Authorization"] == f"Bearer {TOKEN}"
    assert request["headers"]["Content-Type"].startswith("application/json")
    assert request["body"] == {
        "request_id": "run-1:stage-1:evt-1",
        "run_id": "run-1",
        "session_id": "session-1",
        "room_id": "room-a",
        "stage_id": "stage-1",
        "stage_seq": 3,
        "scene": SCENE,
    }


def test_http_empty_success_body_is_ok():
    with local_server(status=200, body=b"") as url:
        adapter = HttpHardwareAdapter(url=url, timeout_seconds=1)
        result = adapter.send_scene(
            run_id="r", session_id="s", room_id=None, stage={"hardware_scene": SCENE}
        )
    assert result.ok is True
    assert result.status == "ok"


def test_http_non_2xx_fails():
    with local_server(status=500, body=b'{"ok": true}') as url:
        adapter = HttpHardwareAdapter(url=url, timeout_seconds=1)
        result = adapter.send_scene(
            run_id="r", session_id="s", room_id=None, stage={"hardware_scene": SCENE}
        )
    assert result.ok is False
    assert result.status == "failed"
    assert result.status_code == 500
    assert "HTTP 500" in result.message


def test_http_invalid_json_fails():
    with local_server(status=200, body=b"not json at all") as url:
        adapter = HttpHardwareAdapter(url=url, timeout_seconds=1)
        result = adapter.send_scene(
            run_id="r", session_id="s", room_id=None, stage={"hardware_scene": SCENE}
        )
    assert result.ok is False
    assert result.status == "failed"
    assert "无法解析" in result.message


def test_http_explicit_ok_false_fails():
    body = json.dumps({"ok": False, "message": "lamp offline"}).encode("utf-8")
    with local_server(status=200, body=body) as url:
        adapter = HttpHardwareAdapter(url=url, timeout_seconds=1)
        result = adapter.send_scene(
            run_id="r", session_id="s", room_id=None, stage={"hardware_scene": SCENE}
        )
    assert result.ok is False
    assert result.status == "failed"
    assert result.message == "lamp offline"


def test_http_timeout_fails():
    with local_server(status=200, delay=1.0) as url:
        adapter = HttpHardwareAdapter(url=url, timeout_seconds=0.5)
        started = time.perf_counter()
        result = adapter.send_scene(
            run_id="r", session_id="s", room_id=None, stage={"hardware_scene": SCENE}
        )
    assert result.ok is False
    assert result.status == "failed"
    assert result.status_code is None
    assert "硬件调用失败" in result.message
    assert time.perf_counter() - started < 3


def test_http_does_not_follow_redirect():
    with local_server(redirect="/elsewhere") as url:
        adapter = HttpHardwareAdapter(url=url, timeout_seconds=1)
        result = adapter.send_scene(
            run_id="r", session_id="s", room_id=None, stage={"hardware_scene": SCENE}
        )
    assert result.ok is False
    assert result.status == "failed"
    assert result.status_code == 302


def test_token_is_sanitized_from_failure_message():
    body = json.dumps({"ok": False, "message": f"bad token {TOKEN} rejected"}).encode(
        "utf-8"
    )
    with local_server(status=200, body=body) as url:
        adapter = HttpHardwareAdapter(url=url, token=TOKEN, timeout_seconds=1)
        result = adapter.send_scene(
            run_id="r", session_id="s", room_id=None, stage={"hardware_scene": SCENE}
        )
    assert result.ok is False
    assert TOKEN not in json.dumps(result.to_dict(), ensure_ascii=False)
    assert "[已隐藏]" in result.message


# ---------------------------------------------------------------------------
# 场控集成：失败不阻断、降级、重试
# ---------------------------------------------------------------------------


@pytest.fixture(params=["excel", "sql"])
def store(request, tmp_path):
    if request.param == "excel":
        yield ExcelStore(
            sessions_path=tmp_path / "sessions.xlsx",
            bookings_path=tmp_path / "bookings.xlsx",
            scripts_path=tmp_path / "scripts.xlsx",
        )
        return
    engine = create_database_engine(sqlite_path=tmp_path / "store.sqlite3")
    ensure_schema(engine)
    value = SqlStore(engine, "test-hardware")
    value.ensure_tenant()
    yield value
    engine.dispose()


def build_pending(store, adapter):
    upsert_script(
        script_name="雾隐山庄",
        aliases="WYSZ",
        duration_minutes=35,
        store=store,
        now=NOW,
    )
    save_stages(
        store,
        script_name="雾隐山庄",
        stages=[
            {
                "name": "开场",
                "duration_minutes": 10,
                "hardware_scene": json.dumps(SCENE),
            },
            {
                "name": "搜证",
                "duration_minutes": 15,
                "hardware_scene": json.dumps({"light": "cold", "sound": "search_01"}),
            },
            {"name": "复盘", "duration_minutes": 10},
        ],
        now=NOW,
    )
    upsert_sessions(
        [
            {
                "session_id": "s-1",
                "store_name": "迷雾剧场",
                "script_name": "雾隐山庄",
                "start_time": "2026-10-20 20:00",
                "total_seats": 6,
                "status": "待开场",
            }
        ],
        store=store,
    )
    created = create_run(
        store,
        session_id="s-1",
        created_by="tenant-hardware",
        mode="hardware",
        hardware=adapter,
        now=NOW,
        secret=SECRET,
    )
    redeemed = redeem_code(store, code=created["code"], now=NOW, secret=SECRET)
    return created, redeemed


def act(store, run_id, action, adapter, *, version, event, **extra):
    return apply_action(
        store,
        run_id=run_id,
        action=action,
        client_event_id=event,
        expected_version=version,
        hardware=adapter,
        now=NOW,
        **extra,
    )


def event_types(store, run_id):
    frame = store.load_dm_run_events()
    if frame.empty:
        return []
    return list(frame[frame["run_id"] == run_id]["event_type"])


def test_hardware_mode_requires_configured_adapter(store):
    upsert_script(
        script_name="雾隐山庄",
        aliases="WYSZ",
        duration_minutes=35,
        store=store,
        now=NOW,
    )
    save_stages(
        store,
        script_name="雾隐山庄",
        stages=[{"name": "开场", "duration_minutes": 10, "hardware_scene": json.dumps(SCENE)}],
        now=NOW,
    )
    upsert_sessions(
        [
            {
                "session_id": "s-1",
                "store_name": "迷雾剧场",
                "script_name": "雾隐山庄",
                "start_time": "2026-10-20 20:00",
                "total_seats": 6,
                "status": "待开场",
            }
        ],
        store=store,
    )
    from app.tools.dm_control import DMControlError

    with pytest.raises(DMControlError) as excinfo:
        create_run(
            store,
            session_id="s-1",
            created_by="tenant-hardware",
            mode="hardware",
            hardware=NullHardwareAdapter(),
            now=NOW,
            secret=SECRET,
        )
    assert excinfo.value.code == "hardware_not_configured"
    assert store.load_dm_runs().empty


def test_start_sends_scene_once(store):
    adapter = RecordingHardware()
    created, redeemed = build_pending(store, adapter)
    result = act(
        store,
        created["run"]["run_id"],
        "start",
        adapter,
        version=redeemed["run"]["state_version"],
        event="evt-start",
    )

    assert result["ok"] is True
    assert result["run"]["status"] == "active"
    assert result["hardware"]["status"] == "ok"
    assert len(adapter.calls) == 1
    call = adapter.calls[0]
    assert call["run_id"] == created["run"]["run_id"]
    assert call["session_id"] == "s-1"
    assert call["stage"]["seq"] == 1
    assert call["stage"]["hardware_scene"] == SCENE
    assert result["run"]["hardware_degraded"] is False
    assert "hardware_ok" in event_types(store, created["run"]["run_id"])


def test_software_start_skips_without_calling(store):
    adapter = NullHardwareAdapter()
    upsert_script(
        script_name="雾隐山庄",
        aliases="WYSZ",
        duration_minutes=35,
        store=store,
        now=NOW,
    )
    save_stages(
        store,
        script_name="雾隐山庄",
        stages=[{"name": "开场", "duration_minutes": 10}],
        now=NOW,
    )
    upsert_sessions(
        [
            {
                "session_id": "s-1",
                "store_name": "迷雾剧场",
                "script_name": "雾隐山庄",
                "start_time": "2026-10-20 20:00",
                "total_seats": 6,
                "status": "待开场",
            }
        ],
        store=store,
    )
    created = create_run(
        store,
        session_id="s-1",
        created_by="tenant-hardware",
        mode="software",
        hardware=adapter,
        now=NOW,
        secret=SECRET,
    )
    redeemed = redeem_code(store, code=created["code"], now=NOW, secret=SECRET)
    result = act(
        store,
        created["run"]["run_id"],
        "start",
        adapter,
        version=redeemed["run"]["state_version"],
        event="evt-start",
    )
    assert result["ok"] is True
    assert result["hardware"]["status"] == "skipped"
    assert "软件模式" in result["hardware"]["message"]
    assert "hardware_ok" not in event_types(store, created["run"]["run_id"])


def test_failure_advances_stage_and_degrades(store):
    failure = HardwareResult(ok=False, status="failed", message="lamp offline")
    adapter = RecordingHardware(results=[failure])
    created, redeemed = build_pending(store, adapter)
    run_id = created["run"]["run_id"]

    result = act(
        store,
        run_id,
        "start",
        adapter,
        version=redeemed["run"]["state_version"],
        event="evt-start",
    )
    assert result["ok"] is True
    assert result["run"]["status"] == "active"
    assert result["run"]["current_seq"] == 1
    assert result["hardware"]["status"] == "failed"
    assert result["run"]["hardware_degraded"] is True
    assert "hardware_failed" in event_types(store, run_id)

    # 降级后切幕不再调用硬件，但阶段照常前进。
    moved = act(
        store,
        run_id,
        "next",
        adapter,
        version=result["run"]["state_version"],
        event="evt-next",
    )
    assert moved["ok"] is True
    assert moved["run"]["current_seq"] == 2
    assert moved["hardware"]["status"] == "skipped"
    assert "已降级" in moved["hardware"]["message"]
    assert len(adapter.calls) == 1
    assert event_types(store, run_id).count("hardware_failed") == 1


def test_retry_success_clears_degraded(store):
    failure = HardwareResult(ok=False, status="failed", message="lamp offline")
    adapter = RecordingHardware(results=[failure])
    created, redeemed = build_pending(store, adapter)
    run_id = created["run"]["run_id"]

    started = act(
        store,
        run_id,
        "start",
        adapter,
        version=redeemed["run"]["state_version"],
        event="evt-start",
    )
    assert started["run"]["hardware_degraded"] is True

    retried = act(
        store,
        run_id,
        "hardware_retry",
        adapter,
        version=started["run"]["state_version"],
        event="evt-retry",
    )
    assert retried["ok"] is True
    assert retried["hardware"]["status"] == "ok"
    assert retried["run"]["hardware_degraded"] is False
    assert event_types(store, run_id).count("hardware_ok") == 1

    # 恢复后下一幕重新尝试硬件。
    moved = act(
        store,
        run_id,
        "next",
        adapter,
        version=retried["run"]["state_version"],
        event="evt-next",
    )
    assert moved["hardware"]["status"] == "ok"
    assert len(adapter.calls) == 3


def test_retry_requires_hardware_mode_and_scene(store):
    adapter = RecordingHardware()
    created, redeemed = build_pending(store, adapter)
    run_id = created["run"]["run_id"]
    started = act(
        store,
        run_id,
        "start",
        adapter,
        version=redeemed["run"]["state_version"],
        event="evt-start",
    )

    software = act(
        store,
        run_id,
        "mode",
        adapter,
        version=started["run"]["state_version"],
        event="evt-mode",
        mode="software",
    )
    assert software["ok"] is True
    assert software["run"]["mode"] == "software"

    refused = act(
        store,
        run_id,
        "hardware_retry",
        adapter,
        version=software["run"]["state_version"],
        event="evt-retry",
    )
    assert refused["ok"] is False
    assert refused["code"] == "invalid_action"

    # 切回硬件模式后，再到没有场景的第三幕重试。
    back = act(
        store,
        run_id,
        "mode",
        adapter,
        version=software["run"]["state_version"],
        event="evt-mode-back",
        mode="hardware",
    )
    moved1 = act(
        store,
        run_id,
        "next",
        adapter,
        version=back["run"]["state_version"],
        event="evt-next-1",
    )
    moved2 = act(
        store,
        run_id,
        "next",
        adapter,
        version=moved1["run"]["state_version"],
        event="evt-next-2",
    )
    assert moved2["run"]["current_seq"] == 3
    assert moved2["hardware"]["status"] == "skipped"

    no_scene = act(
        store,
        run_id,
        "hardware_retry",
        adapter,
        version=moved2["run"]["state_version"],
        event="evt-retry-no-scene",
    )
    assert no_scene["ok"] is False
    assert no_scene["code"] == "invalid_action"


def test_switching_to_software_clears_degraded(store):
    failure = HardwareResult(ok=False, status="failed", message="boom")
    adapter = RecordingHardware(results=[failure])
    created, redeemed = build_pending(store, adapter)
    run_id = created["run"]["run_id"]
    started = act(
        store,
        run_id,
        "start",
        adapter,
        version=redeemed["run"]["state_version"],
        event="evt-start",
    )
    assert started["run"]["hardware_degraded"] is True

    switched = apply_action(
        store,
        run_id=run_id,
        action="mode",
        client_event_id="evt-software",
        expected_version=started["run"]["state_version"],
        mode="software",
        hardware=NullHardwareAdapter(),
        now=NOW,
    )
    assert switched["ok"] is True
    assert switched["run"]["mode"] == "software"
    assert switched["run"]["hardware_degraded"] is False

    refused = apply_action(
        store,
        run_id=run_id,
        action="mode",
        client_event_id="evt-hardware-again",
        expected_version=switched["run"]["state_version"],
        mode="hardware",
        hardware=NullHardwareAdapter(),
        now=NOW,
    )
    assert refused["ok"] is False
    assert refused["code"] == "hardware_not_configured"
    assert refused["run"]["mode"] == "software"


def test_http_failure_is_degraded_and_token_never_leaks(store):
    body = json.dumps({"ok": False, "message": f"auth {TOKEN} rejected"}).encode("utf-8")
    with local_server(status=200, body=body) as url:
        adapter = HttpHardwareAdapter(url=url, token=TOKEN, timeout_seconds=1)
        created, redeemed = build_pending(store, adapter)
        run_id = created["run"]["run_id"]
        result = act(
            store,
            run_id,
            "start",
            adapter,
            version=redeemed["run"]["state_version"],
            event="evt-start",
        )

    assert result["ok"] is True
    assert result["run"]["status"] == "active"
    assert result["run"]["current_seq"] == 1
    assert result["hardware"]["status"] == "failed"
    assert result["run"]["hardware_degraded"] is True
    serialized = json.dumps(result, ensure_ascii=False, default=str)
    assert TOKEN not in serialized
    assert "[已隐藏]" in result["hardware"]["message"]

    events = store.load_dm_run_events()
    assert TOKEN not in events.to_string()
    assert "hardware_failed" in event_types(store, run_id)
    failed = events[
        (events["run_id"] == run_id) & (events["event_type"] == "hardware_failed")
    ]
    messages = [
        json.loads(raw).get("message")
        for raw in failed["payload"].dropna().tolist()
        if raw
    ]
    assert any(message and "已隐藏" in message for message in messages)


def test_hardware_call_not_logged_before_commit(store):
    """适配器收到请求时，阶段状态已经落库。"""
    observed = {}

    class PeekHardware:
        name = "peek"
        configured = True

        def send_scene(self, **kwargs):
            frame = store.load_dm_runs()
            row = frame[frame["run_id"] == kwargs["run_id"]].iloc[0]
            observed["seq"] = int(row["current_seq"])
            observed["status"] = str(row["status"])
            return HardwareResult(ok=True, status="ok")

    adapter = PeekHardware()
    created, redeemed = build_pending(store, adapter)
    result = act(
        store,
        created["run"]["run_id"],
        "start",
        adapter,
        version=redeemed["run"]["state_version"],
        event="evt-start",
    )
    assert result["ok"] is True
    assert observed == {"seq": 1, "status": "active"}

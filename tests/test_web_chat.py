"""登录后的聊天 API 测试：CSRF、上限、轨迹脱敏、跨用户写入隔离。"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage

from app.tools.scripts import load_scripts
from app.tools.storage import BOOKING_ACTIVE, load_bookings, make_session_id
from app.web.main import create_app
from app.web.mailer import RecordingEmailSender
from app.web.models import StaticModelProvider
from app.web.security import email_hash
from tests.test_agent import ScriptedChatModel, _tool_call
from tests.web_helpers import (
    MutableClock,
    csrf_from,
    fully_authorize,
    make_settings,
)


def build_chat_app(tmp_path, clock, responses, mailer=None, **overrides):
    settings = make_settings(tmp_path, **overrides)
    model = ScriptedChatModel(responses=responses)
    app = create_app(
        settings=settings,
        mailer=mailer or RecordingEmailSender(),
        model_provider=StaticModelProvider(model),
        now_fn=clock.now,
    )
    return app, model


def _csrf(client) -> str:
    return csrf_from(client.get("/").text)


def _post_chat(client, message, **extra):
    payload = {"message": message}
    payload.update(extra)
    return client.post(
        "/api/chat",
        json=payload,
        headers={"X-CSRF-Token": _csrf(client), "Accept": "application/json"},
    )


def test_chat_returns_answer_and_remaining_time(tmp_path):
    clock = MutableClock()
    app, _ = build_chat_app(tmp_path, clock, [AIMessage(content="这是回复")])
    mailer = app.state.mailer

    with TestClient(app) as client:
        fully_authorize(client, mailer)
        response = _post_chat(client, "你好")

        assert response.status_code == 200
        payload = response.json()
        assert payload["ok"] is True
        assert payload["answer"] == "这是回复"
        assert payload["trace"] == []
        assert 0 < payload["remaining_seconds"] <= 8 * 3600


def test_chat_requires_csrf(tmp_path):
    clock = MutableClock()
    app, model = build_chat_app(tmp_path, clock, [AIMessage(content="不应调用")])
    mailer = app.state.mailer

    with TestClient(app) as client:
        fully_authorize(client, mailer)
        response = client.post(
            "/api/chat",
            json={"message": "你好"},
            headers={"Accept": "application/json"},
        )

    assert response.status_code == 403
    assert response.json()["code"] == "csrf_failed"
    assert model.index == 0


def test_chat_rejects_empty_long_and_malformed_messages(tmp_path):
    clock = MutableClock()
    app, _ = build_chat_app(
        tmp_path,
        clock,
        [AIMessage(content="不应调用")],
        max_input_chars=5,
    )
    mailer = app.state.mailer

    with TestClient(app) as client:
        fully_authorize(client, mailer)
        csrf = _csrf(client)
        headers = {"X-CSRF-Token": csrf, "Accept": "application/json"}

        empty = client.post("/api/chat", json={"message": "   "}, headers=headers)
        assert empty.status_code == 400
        assert empty.json()["code"] == "bad_message"

        too_long = client.post(
            "/api/chat", json={"message": "123456"}, headers=headers
        )
        assert too_long.status_code == 400

        missing = client.post("/api/chat", json={"other": "x"}, headers=headers)
        assert missing.status_code == 400

        broken = client.post(
            "/api/chat",
            content=b"not-json",
            headers={**headers, "Content-Type": "application/json"},
        )
        assert broken.status_code == 400


def test_history_limit_returns_409_without_reserving_another_turn(tmp_path):
    clock = MutableClock()
    app, model = build_chat_app(
        tmp_path,
        clock,
        [AIMessage(content="第一轮")],
        max_history_messages=2,
    )
    mailer = app.state.mailer

    with TestClient(app) as client:
        fully_authorize(client, mailer)
        assert _post_chat(client, "第一轮").status_code == 200
        response = _post_chat(client, "第二轮")

        assert response.status_code == 409
        assert response.json()["code"] == "history_too_long"
        assert app.state.usage.current()[1] == 1

    assert model.index == 1


def test_daily_global_limit_returns_429(tmp_path):
    clock = MutableClock()
    app, model = build_chat_app(
        tmp_path,
        clock,
        [AIMessage(content="第一轮"), AIMessage(content="不应调用")],
        max_turns_global_per_day=1,
    )
    mailer = app.state.mailer

    with TestClient(app) as client:
        fully_authorize(client, mailer)
        assert _post_chat(client, "第一轮").status_code == 200
        response = _post_chat(client, "第二轮")

        assert response.status_code == 429
        assert response.json()["code"] == "daily_limit"
        assert int(response.headers["Retry-After"]) > 0

    assert model.index == 1


def test_trace_contains_tool_summary_but_not_tool_data(tmp_path):
    clock = MutableClock()
    app, _ = build_chat_app(
        tmp_path,
        clock,
        [
            _tool_call("store_data", {"action": "query_sessions"}, "q1"),
            AIMessage(content="查询完成"),
        ],
    )
    mailer = app.state.mailer

    with TestClient(app) as client:
        fully_authorize(client, mailer)
        response = _post_chat(client, "查一下未来场次")

    assert response.status_code == 200
    payload = response.json()
    assert payload["answer"] == "查询完成"
    assert len(payload["trace"]) == 1
    trace = payload["trace"][0]
    assert set(trace) == {"tool", "args", "ok", "error_code"}
    assert trace["tool"] == "store_data"
    assert trace["ok"] is True
    serialized = json.dumps(payload, ensure_ascii=False)
    assert "raw_snippet" not in serialized
    assert "session_id" not in serialized
    assert "profit" not in serialized


def test_two_emails_write_only_their_own_workspace(tmp_path):
    clock = MutableClock()
    store_name = "迷雾剧场"
    session_id = make_session_id(store_name, "雾隐山庄", "2026-10-20 19:00")

    first_app, _ = build_chat_app(
        tmp_path,
        clock,
        [
            _tool_call(
                "store_data",
                {
                    "action": "record_booking",
                    "session_id": session_id,
                    "player_name": "甲的新客",
                    "seats": 2,
                },
                "b1",
            ),
            AIMessage(content="已记录到甲的工作区"),
        ],
    )
    first_mailer = first_app.state.mailer
    with TestClient(first_app) as first_client:
        fully_authorize(first_client, first_mailer, email="a@example.com")
        assert _post_chat(first_client, "给甲记一个预约").status_code == 200

    second_app, _ = build_chat_app(
        tmp_path,
        clock,
        [AIMessage(content="乙的回复")],
    )
    second_mailer = second_app.state.mailer
    with TestClient(second_app) as second_client:
        fully_authorize(second_client, second_mailer, email="b@example.com")
        assert _post_chat(second_client, "你好").status_code == 200

    first_paths = first_app.state.workspace.paths_for(email_hash("a@example.com"))
    second_paths = second_app.state.workspace.paths_for(email_hash("b@example.com"))
    first_bookings = load_bookings(first_paths.bookings_path)
    second_bookings = load_bookings(second_paths.bookings_path)

    first_active = first_bookings[first_bookings["status"] == BOOKING_ACTIVE]
    second_active = second_bookings[second_bookings["status"] == BOOKING_ACTIVE]
    assert len(first_active) == 3
    assert len(second_active) == 2
    assert "甲的新客" in set(first_active["player_name"])
    assert "甲的新客" not in set(second_active["player_name"])


def test_script_library_writes_only_to_own_workspace(tmp_path):
    clock = MutableClock()
    first_app, _ = build_chat_app(
        tmp_path,
        clock,
        [
            _tool_call(
                "script_library",
                {"action": "upsert", "aliases": "WY"},
                "s1",
            ),
            AIMessage(content="已保存缩写草稿"),
        ],
    )
    with TestClient(first_app) as first_client:
        fully_authorize(first_client, first_app.state.mailer, email="a@example.com")
        assert _post_chat(first_client, "WY 这个本先记一下").status_code == 200

    second_app, _ = build_chat_app(
        tmp_path,
        clock,
        [AIMessage(content="乙的回复")],
    )
    with TestClient(second_app) as second_client:
        fully_authorize(second_client, second_app.state.mailer, email="b@example.com")
        assert _post_chat(second_client, "你好").status_code == 200

    first_paths = first_app.state.workspace.paths_for(email_hash("a@example.com"))
    second_paths = second_app.state.workspace.paths_for(email_hash("b@example.com"))
    first_scripts = load_scripts(first_paths.scripts_path)
    second_scripts = load_scripts(second_paths.scripts_path)

    assert "WY" in first_scripts["aliases"].astype("string").str.cat(sep=" / ")
    assert "WY" not in second_scripts["aliases"].astype("string").str.cat(sep=" / ")
    assert len(first_scripts) == 5
    assert len(second_scripts) == 4


def test_reset_endpoint_starts_new_thread(tmp_path):
    clock = MutableClock()
    app, _ = build_chat_app(
        tmp_path,
        clock,
        [AIMessage(content="第一轮"), AIMessage(content="第二轮")],
    )
    mailer = app.state.mailer

    with TestClient(app) as client:
        fully_authorize(client, mailer)
        assert _post_chat(client, "第一轮").status_code == 200
        csrf = _csrf(client)
        response = client.post(
            "/api/reset",
            headers={"X-CSRF-Token": csrf, "Accept": "application/json"},
        )
        assert response.status_code == 200
        assert response.json()["ok"] is True

        runtime = app.state.runtime
        user = runtime._users[email_hash("user@example.com")]
        state = user.agent.get_state(
            {"configurable": {"thread_id": user.thread_id}}
        )
        assert state.values.get("messages", []) == []


def test_sessions_endpoint_returns_safe_snapshot(tmp_path):
    clock = MutableClock()
    app, _ = build_chat_app(tmp_path, clock, [AIMessage(content="回复")])
    mailer = app.state.mailer

    with TestClient(app) as client:
        fully_authorize(client, mailer)
        response = client.get("/api/sessions")

    assert response.status_code == 200
    sessions = response.json()["sessions"]
    assert len(sessions) == 4
    assert "list_price" in sessions[0]
    assert "source_text" not in json.dumps(sessions, ensure_ascii=False)


def test_model_failure_returns_sanitized_502(tmp_path, monkeypatch):
    clock = MutableClock()
    app, _ = build_chat_app(tmp_path, clock, [AIMessage(content="不应调用")])
    mailer = app.state.mailer

    def boom(*args, **kwargs):
        raise RuntimeError("sk-secret-value")

    monkeypatch.setattr("app.web.runtime.run_turn", boom)

    with TestClient(app) as client:
        fully_authorize(client, mailer)
        response = _post_chat(client, "你好")

    assert response.status_code == 502
    assert response.json()["code"] == "model_error"
    assert "sk-secret-value" not in response.text


def test_chat_without_session_returns_401(tmp_path):
    clock = MutableClock()
    app, model = build_chat_app(tmp_path, clock, [AIMessage(content="不应调用")])

    with TestClient(app) as client:
        response = client.post(
            "/api/chat",
            json={"message": "你好"},
            headers={"Accept": "application/json"},
        )

    assert response.status_code == 401
    assert response.json()["code"] == "login_required"
    assert model.index == 0


def test_chat_includes_choices_key(tmp_path):
    clock = MutableClock()
    app, _ = build_chat_app(tmp_path, clock, [AIMessage(content="这是回复")])
    mailer = app.state.mailer

    with TestClient(app) as client:
        fully_authorize(client, mailer)
        payload = _post_chat(client, "你好").json()

    assert "choices" in payload
    # 纯文本回复没有可结构化的动作 → 空列表，而不是缺字段
    assert payload["choices"] == []


def test_chat_choices_from_incomplete_create_session(tmp_path):
    """create_session 缺硬必填时，工具结果的追问问题变成 quick_reply 按钮。

    场次草稿机制已删除：不齐只追问，不落任何数据，也没有草稿信封。
    """
    clock = MutableClock()
    app, _ = build_chat_app(
        tmp_path,
        clock,
        [
            _tool_call(
                "store_data",
                {
                    "action": "create_session",
                    "fields": {"script_name": "雾隐山庄"},
                },
                "d1",
            ),
            AIMessage(content="还差开场时间。"),
        ],
    )
    mailer = app.state.mailer

    with TestClient(app) as client:
        fully_authorize(client, mailer)
        payload = _post_chat(client, "帮我记一个雾隐山庄的场").json()

    assert payload["answer"] == "还差开场时间。"
    choices = payload["choices"]
    assert choices, "缺开场时间的追问轮应产出可点击按钮"
    assert all(set(group) == {"kind", "buttons", "context"} for group in choices)
    group = choices[0]
    assert group["kind"] == "quick_reply"
    button = group["buttons"][0]
    assert button["action"] == "quick_reply"
    assert "开场时间" in button["payload"]["text"]
    # choices 不得携带原始信封键，也不得出现草稿信封。
    serialized = json.dumps(choices, ensure_ascii=False)
    assert "raw_snippet" not in serialized
    assert "source_text" not in serialized
    assert "draft" not in serialized


def test_chat_choices_needs_confirmation_candidates_become_quick_replies():
    """store_data 返回 needs_confirmation 时，候选剧本变成纯文本追问按钮。

    伪造配对好的 AgentTurn（不跑模型）：一候选一个按钮，点击只是重发
    「就开《X》」文本，不提交任何写。
    """
    from app.agent import AgentTurn
    from app.web.chat_choices import build_choices

    envelope = {
        "ok": True,
        "data": {
            "status": "needs_confirmation",
            "suggested_question": "本地库里找到了几个可能的本：《年轮》、《年轮（重制版）》，是哪一个？",
            "ambiguities": [
                {
                    "field": "script_name",
                    "candidates": [
                        {"script_name": "年轮", "script_id": "a"},
                        {"script_name": "年轮（重制版）", "script_id": "b"},
                    ],
                }
            ],
        },
    }
    turn = AgentTurn(
        answer="找到两个可能的本。",
        tool_calls=({"name": "store_data", "args": {}},),
        tool_results=(
            {"name": "store_data", "content": json.dumps(envelope, ensure_ascii=False)},
        ),
    )

    choices = build_choices(turn)
    assert len(choices) == 1
    group = choices[0]
    assert set(group) == {"kind", "buttons", "context"}
    assert group["kind"] == "quick_reply"
    assert [b["label"] for b in group["buttons"]] == ["《年轮》", "《年轮（重制版）》"]
    assert group["buttons"][0]["payload"] == {"text": "就开《年轮》"}
    assert all(b["action"] == "quick_reply" for b in group["buttons"])
    # 按钮不携带候选元数据，也没有任何草稿键。
    serialized = json.dumps(choices, ensure_ascii=False)
    assert "script_id" not in serialized
    assert "draft" not in serialized


def test_chat_choices_promoted_conflicts_still_produce_resolve_buttons():
    """promoted 结果嵌套的 auto_assign 冲突仍产出 resolve 按钮，无草稿信封。"""
    from app.agent import AgentTurn
    from app.web.chat_choices import build_choices

    envelope = {
        "ok": True,
        "data": {
            "status": "promoted",
            "suggested_question": "",
            "auto_assign": {
                "status": "conflict",
                "conflicts": [
                    {
                        "session_id": "sess-1",
                        "options": [
                            {
                                "type": "switch_room",
                                "label": "换到 2 号房",
                                "target_id": "room-2",
                            },
                            {"type": "shift_time", "label": "改期"},
                        ],
                    }
                ],
            },
        },
    }
    turn = AgentTurn(
        answer="建场了，但排期有冲突。",
        tool_calls=({"name": "store_data", "args": {}},),
        tool_results=(
            {"name": "store_data", "content": json.dumps(envelope, ensure_ascii=False)},
        ),
    )

    choices = build_choices(turn)
    assert len(choices) == 1
    group = choices[0]
    assert group["kind"] == "resolve"
    # shift_time 只有叙述无按钮，只剩可执行的 switch_room。
    assert [b["payload"]["choice"] for b in group["buttons"]] == ["switch_room"]
    assert group["buttons"][0]["payload"] == {
        "session_id": "sess-1",
        "choice": "switch_room",
        "target_id": "room-2",
    }
    assert "draft" not in json.dumps(choices, ensure_ascii=False)


def test_app_chat_includes_choices(tmp_path):
    """移动端 /api/app/chat 响应同样携带 choices（bearer 鉴权路径）。"""
    from tests.test_web_dm import _merchant_token
    from tests.web_helpers import ORIGIN

    clock = MutableClock()
    app, _ = build_chat_app(tmp_path, clock, [AIMessage(content="你好呀")])
    mailer = app.state.mailer

    with TestClient(app) as client:
        fully_authorize(client, mailer)
        token = _merchant_token(client, mailer)
        response = client.post(
            "/api/app/chat",
            json={"message": "你好"},
            headers={"Authorization": f"Bearer {token}", **ORIGIN},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["answer"] == "你好呀"
    assert payload["choices"] == []

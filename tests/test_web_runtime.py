"""模型提供器、会话运行时、轨迹与快照的离线测试。"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pandas as pd
import pytest
from langchain_core.messages import AIMessage

from app.agent import AgentTurn
from app.web.models import (
    DeepSeekModelProvider,
    ModelProviderError,
    StaticModelProvider,
)
from app.web.runtime import (
    BadMessage,
    ChatRuntime,
    ConcurrencyFull,
    DailyLimitReached,
    HistoryTooLong,
    ModelFailure,
    ModelUnavailable,
    TurnBusy,
)
from app.web.security import email_hash
from app.web.trace import safe_trace
from app.web.usage import UsageStore
from app.web.workspace import WorkspaceManager
from tests.test_agent import ScriptedChatModel
from tests.web_helpers import MutableClock, make_settings

UTC = timezone.utc


def _runtime(tmp_path, clock, model, **overrides):
    settings = make_settings(tmp_path, **overrides)
    usage = UsageStore(
        tmp_path / "usage.sqlite3",
        daily_limit=settings.max_turns_global_per_day,
        now_fn=clock.now,
    )
    workspace = WorkspaceManager(
        data_root=settings.data_root,
        default_store=settings.default_store,
        now_fn=clock.now,
    )
    return ChatRuntime(
        settings=settings,
        model_provider=StaticModelProvider(model),
        workspace=workspace,
        usage=usage,
        now_fn=clock.now,
    )


class BrokenProvider:
    def get(self):
        raise ModelProviderError("模型初始化失败")


def test_chat_returns_answer_and_seeds_workspace(tmp_path):
    clock = MutableClock()
    model = ScriptedChatModel(responses=[AIMessage(content="第一轮回复")])
    runtime = _runtime(tmp_path, clock, model)
    digest = email_hash("user@example.com")

    result = runtime.chat(digest, "你好")

    assert result.answer == "第一轮回复"
    assert result.thread_id.startswith("web-")
    assert runtime.snapshot(digest)[0]["script_name"] == "雾隐山庄"


def test_two_emails_have_separate_agents_and_memory(tmp_path):
    clock = MutableClock()
    model = ScriptedChatModel(
        responses=[
            AIMessage(content="给甲的回复"),
            AIMessage(content="给乙的回复"),
        ]
    )
    runtime = _runtime(tmp_path, clock, model)
    first = email_hash("a@example.com")
    second = email_hash("b@example.com")

    one = runtime.chat(first, "我是甲")
    two = runtime.chat(second, "我是乙")

    assert one.thread_id != two.thread_id
    assert runtime._users[first].agent is not runtime._users[second].agent
    assert runtime._users[first].paths.directory != runtime._users[second].paths.directory
    config_one = {"configurable": {"thread_id": one.thread_id}}
    config_two = {"configurable": {"thread_id": two.thread_id}}
    assert len(runtime._users[first].agent.get_state(config_one).values["messages"]) == 2
    assert len(runtime._users[second].agent.get_state(config_two).values["messages"]) == 2


def test_same_user_keeps_memory_across_turns(tmp_path):
    clock = MutableClock()
    model = ScriptedChatModel(
        responses=[AIMessage(content="第一轮"), AIMessage(content="第二轮")]
    )
    runtime = _runtime(tmp_path, clock, model)
    digest = email_hash("user@example.com")

    first = runtime.chat(digest, "你好")
    second = runtime.chat(digest, "继续")

    assert first.thread_id == second.thread_id
    state = runtime._users[digest].agent.get_state(
        {"configurable": {"thread_id": first.thread_id}}
    )
    assert len(state.values["messages"]) == 4


def test_reset_starts_new_thread_and_clears_history(tmp_path):
    clock = MutableClock()
    model = ScriptedChatModel(responses=[AIMessage(content="回复")])
    runtime = _runtime(tmp_path, clock, model)
    digest = email_hash("user@example.com")
    before = runtime.chat(digest, "你好")

    runtime.reset(digest)
    after_user = runtime._users[digest]

    assert after_user.generation == 1
    assert after_user.thread_id != before.thread_id
    state = after_user.agent.get_state(
        {"configurable": {"thread_id": after_user.thread_id}}
    )
    assert state.values.get("messages", []) == []


def test_empty_and_too_long_messages_rejected(tmp_path):
    clock = MutableClock()
    runtime = _runtime(
        tmp_path,
        clock,
        ScriptedChatModel(responses=[AIMessage(content="不应调用")]),
        max_input_chars=5,
    )
    digest = email_hash("user@example.com")

    with pytest.raises(BadMessage):
        runtime.chat(digest, "")
    with pytest.raises(BadMessage):
        runtime.chat(digest, "123456")


def test_history_limit_requires_new_session(tmp_path):
    clock = MutableClock()
    model = ScriptedChatModel(responses=[AIMessage(content="回复")])
    runtime = _runtime(tmp_path, clock, model, max_history_messages=2)
    digest = email_hash("user@example.com")

    runtime.chat(digest, "第一轮")
    with pytest.raises(HistoryTooLong):
        runtime.chat(digest, "第二轮")


def test_turn_busy_when_user_lock_held(tmp_path):
    clock = MutableClock()
    runtime = _runtime(
        tmp_path,
        clock,
        ScriptedChatModel(responses=[AIMessage(content="回复")]),
    )
    digest = email_hash("user@example.com")
    user = runtime._get_user(digest)
    assert user.lock.acquire(blocking=False)

    try:
        with pytest.raises(TurnBusy):
            runtime.chat(digest, "你好")
    finally:
        user.lock.release()


def test_concurrency_full_when_global_semaphore_held(tmp_path):
    clock = MutableClock()
    runtime = _runtime(
        tmp_path,
        clock,
        ScriptedChatModel(responses=[AIMessage(content="回复")]),
        max_concurrent_turns=1,
    )
    digest = email_hash("user@example.com")
    runtime._get_user(digest)
    assert runtime._semaphore.acquire(blocking=False)

    try:
        with pytest.raises(ConcurrencyFull):
            runtime.chat(digest, "你好")
    finally:
        runtime._semaphore.release()


def test_daily_limit_blocks_before_calling_model(tmp_path):
    clock = MutableClock()
    model = ScriptedChatModel(responses=[AIMessage(content="回复")])
    runtime = _runtime(
        tmp_path,
        clock,
        model,
        max_turns_global_per_day=1,
    )
    digest = email_hash("user@example.com")

    runtime.chat(digest, "第一轮")
    with pytest.raises(DailyLimitReached) as caught:
        runtime.chat(digest, "第二轮")

    assert caught.value.retry_after_seconds > 0
    assert model.index == 1


def test_model_provider_failure_maps_to_503(tmp_path):
    clock = MutableClock()
    settings = make_settings(tmp_path)
    runtime = ChatRuntime(
        settings=settings,
        model_provider=BrokenProvider(),
        workspace=WorkspaceManager(
            data_root=settings.data_root,
            default_store=settings.default_store,
            now_fn=clock.now,
        ),
        usage=UsageStore(
            tmp_path / "usage.sqlite3",
            daily_limit=10,
            now_fn=clock.now,
        ),
        now_fn=clock.now,
    )

    with pytest.raises(ModelUnavailable):
        runtime.chat(email_hash("user@example.com"), "你好")


def test_turn_failure_is_sanitized(tmp_path, monkeypatch):
    clock = MutableClock()
    runtime = _runtime(
        tmp_path,
        clock,
        ScriptedChatModel(responses=[AIMessage(content="回复")]),
    )

    def boom(*args, **kwargs):
        raise RuntimeError("secret sk-test-value")

    monkeypatch.setattr("app.web.runtime.run_turn", boom)
    with pytest.raises(ModelFailure) as caught:
        runtime.chat(email_hash("user@example.com"), "你好")

    assert "sk-test-value" not in str(caught.value)
    assert "secret" not in str(caught.value)


def test_invalid_email_hash_rejected(tmp_path):
    clock = MutableClock()
    runtime = _runtime(
        tmp_path,
        clock,
        ScriptedChatModel(responses=[AIMessage(content="回复")]),
    )

    with pytest.raises(ValueError):
        runtime.chat("not-a-hash", "你好")


def test_pandas_clock_is_naive_store_local_time(tmp_path):
    clock = MutableClock(datetime(2026, 10, 20, 4, 0, tzinfo=UTC))
    runtime = _runtime(
        tmp_path,
        clock,
        ScriptedChatModel(responses=[AIMessage(content="回复")]),
    )

    assert runtime._pandas_now() == pd.Timestamp("2026-10-20 12:00")


def test_static_model_provider_bundle():
    main = object()
    extraction = object()
    copy = object()
    bundle = StaticModelProvider(
        main,
        extraction_model=extraction,
        copy_model=copy,
    ).get()

    assert bundle.main is main
    assert bundle.extraction is extraction
    assert bundle.copy is copy


def test_deepseek_provider_lazy_and_passes_limits(monkeypatch):
    calls: list[dict] = []

    def fake_model(**kwargs):
        calls.append(kwargs)
        return object()

    monkeypatch.setattr("app.web.models.llm.get_default_model", fake_model)
    provider = DeepSeekModelProvider(max_tokens=1234, timeout=42.0, max_retries=2)

    assert calls == []
    bundle = provider.get()
    assert provider.get() is bundle
    assert len(calls) == 3
    assert [call["temperature"] for call in calls] == [0.0, 0.0, 0.7]
    assert all(call["max_tokens"] == 1234 for call in calls)
    assert all(call["timeout"] == 42.0 for call in calls)
    assert all(call["max_retries"] == 2 for call in calls)


def test_deepseek_provider_error_is_sanitized(monkeypatch):
    def boom(**kwargs):
        raise RuntimeError("sk-secret")

    monkeypatch.setattr("app.web.models.llm.get_default_model", boom)
    provider = DeepSeekModelProvider(max_tokens=100, timeout=10.0)

    with pytest.raises(ModelProviderError) as caught:
        provider.get()

    assert "sk-secret" not in str(caught.value)


def test_safe_trace_only_exposes_safe_summary():
    turn = AgentTurn(
        answer="answer",
        tool_calls=(
            {"name": "diagnose_risk", "args": {"store": "迷雾剧场", "secret": "x" * 300}},
        ),
        tool_results=(
            {
                "name": "diagnose_risk",
                "content": json.dumps(
                    {
                        "ok": False,
                        "error_code": "bad_input",
                        "data": {"profit": 9999},
                        "raw_snippet": "绝密内容",
                    },
                    ensure_ascii=False,
                ),
            },
        ),
    )

    trace = safe_trace(turn, arg_chars=60)

    assert trace == [
        {
            "tool": "diagnose_risk",
            "args": trace[0]["args"],
            "ok": False,
            "error_code": "bad_input",
        }
    ]
    assert len(trace[0]["args"]) <= 60
    assert "9999" not in json.dumps(trace, ensure_ascii=False)
    assert "绝密内容" not in json.dumps(trace, ensure_ascii=False)


def test_safe_trace_handles_malformed_and_extra_calls():
    turn = AgentTurn(
        answer="answer",
        tool_calls=(
            {"name": "a", "args": {}},
            {"name": "b", "args": {}},
            {"name": "c", "args": {}},
        ),
        tool_results=(
            {"name": "a", "content": "not json"},
            {"name": "b", "content": None},
        ),
    )

    trace = safe_trace(turn, max_tools=2)

    assert len(trace) == 2
    assert trace[0]["ok"] is None
    assert trace[1]["ok"] is None
    assert trace[0]["tool"] == "a"
    assert trace[1]["tool"] == "b"

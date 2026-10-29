"""店长交互控制台 —— 离线测试。

全部离线：
  - 不联网、不需要 API key；
  - 数据文件在 tmp_path 下，不碰真实 data/；
  - Agent 图用脚本模型驱动，不调用真实 DeepSeek；
  - autouse fixture 阻止任何测试意外初始化真实模型。
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Callable, Iterable

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field

from app.agent import AgentDeps, AgentTurn, build_agent
from app.cli import (
    ConsoleContext,
    format_turn_trace,
    main,
    print_banner,
    run_repl,
)


# ===========================================================================
# 测试基础设施
# ===========================================================================

@pytest.fixture(autouse=True)
def _block_real_model(monkeypatch):
    """任何测试都不应初始化真实模型，哪怕本机 .env 配了 key。"""

    def _boom(*args, **kwargs):
        raise AssertionError("CLI 测试不应初始化真实模型")

    monkeypatch.setattr("app.config.get_model", _boom)


class ScriptedChatModel(BaseChatModel):
    """按脚本依次返回消息；支持 bind_tools，可抛异常。"""

    responses: list[Any]
    index: int = 0
    seen_lengths: list[int] = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "scripted-cli-model"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.seen_lengths.append(len(messages))
        response = self.responses[min(self.index, len(self.responses) - 1)]
        self.index += 1
        if isinstance(response, BaseException):
            raise response
        return ChatResult(generations=[ChatGeneration(message=response)])


def feed(lines: Iterable[str]) -> Callable[[str], str]:
    """有限输入；耗尽后抛 EOFError，模拟 Ctrl+D。"""
    iterator = iter(lines)

    def _input(prompt: str = "") -> str:
        try:
            return next(iterator)
        except StopIteration:
            raise EOFError

    return _input


def _ctx(tmp_path, **overrides) -> ConsoleContext:
    base = ConsoleContext(
        sessions_path=tmp_path / "sessions.xlsx",
        bookings_path=tmp_path / "bookings.xlsx",
        scripts_path=tmp_path / "scripts.xlsx",
    )
    return replace(base, **overrides)


def _deps(tmp_path) -> AgentDeps:
    return AgentDeps(
        sessions_path=tmp_path / "sessions.xlsx",
        bookings_path=tmp_path / "bookings.xlsx",
        scripts_path=tmp_path / "scripts.xlsx",
    )


def _run_main(tmp_path, lines, model, *argv_extra):
    output: list[str] = []
    code = main(
        ["--data-dir", str(tmp_path), *argv_extra],
        input_fn=feed(lines),
        output_fn=output.append,
        model=model,
    )
    return code, "\n".join(output)


def _tool_call(name: str, args: dict[str, Any], call_id: str) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{"name": name, "args": args, "id": call_id}],
    )


# ===========================================================================
# 命令与基础循环
# ===========================================================================

class TestCommands:
    def test_help_lists_commands_and_does_not_call_model(self, tmp_path):
        model = ScriptedChatModel(responses=[AIMessage(content="不应调用")])
        code, output = _run_main(tmp_path, ["/help", "/quit"], model)
        assert code == 0
        for command in ("/help", "/reset", "/status", "/quit"):
            assert command in output
        assert model.index == 0

    def test_blank_input_is_ignored(self, tmp_path):
        model = ScriptedChatModel(responses=[AIMessage(content="不应调用")])
        code, output = _run_main(tmp_path, ["", "   ", "/quit"], model)
        assert code == 0
        assert model.index == 0
        assert "已退出" in output

    @pytest.mark.parametrize("command", ["/quit", "/exit"])
    def test_quit_and_exit(self, tmp_path, command):
        model = ScriptedChatModel(responses=[AIMessage(content="不应调用")])
        code, output = _run_main(tmp_path, [command], model)
        assert code == 0
        assert "已退出" in output

    def test_eof_exits_zero(self, tmp_path):
        model = ScriptedChatModel(responses=[AIMessage(content="不应调用")])
        code, output = _run_main(tmp_path, [], model)
        assert code == 0
        assert "已退出" in output

    def test_ctrl_c_at_prompt_exits_zero(self, tmp_path):
        model = ScriptedChatModel(responses=[AIMessage(content="不应调用")])

        def _interrupt(prompt: str = "") -> str:
            raise KeyboardInterrupt

        output: list[str] = []
        code = main(
            ["--data-dir", str(tmp_path)],
            input_fn=_interrupt,
            output_fn=output.append,
            model=model,
        )
        assert code == 0
        assert "已退出" in "\n".join(output)

    def test_unknown_command_does_not_call_model(self, tmp_path):
        model = ScriptedChatModel(responses=[AIMessage(content="不应调用")])
        code, output = _run_main(tmp_path, ["/foo", "/quit"], model)
        assert code == 0
        assert "未知命令" in output
        assert model.index == 0

    def test_status_reports_paths_thread_mode(self, tmp_path):
        model = ScriptedChatModel(responses=[AIMessage(content="不应调用")])
        agent = build_agent(model=model, deps=_deps(tmp_path))
        ctx = _ctx(tmp_path, thread_id="t1", read_only=True)
        output: list[str] = []
        code = run_repl(
            agent, ctx,
            input_fn=feed(["/status", "/quit"]),
            output_fn=output.append,
        )
        text = "\n".join(output)
        assert code == 0
        assert str(tmp_path) in text
        assert "会话线程：t1" in text
        assert "模式：只读" in text
        assert "本会话消息数：0" in text
        assert model.index == 0


# ===========================================================================
# 多轮记忆、重置、错误恢复
# ===========================================================================

class TestConversation:
    def test_multi_turn_same_thread_has_memory(self, tmp_path):
        model = ScriptedChatModel(responses=[
            AIMessage(content="第一轮答复"),
            AIMessage(content="第二轮答复"),
        ])
        agent = build_agent(model=model, deps=_deps(tmp_path))
        output: list[str] = []
        code = run_repl(
            agent, _ctx(tmp_path),
            input_fn=feed(["你好", "继续", "/quit"]),
            output_fn=output.append,
        )
        text = "\n".join(output)
        assert code == 0
        assert "第一轮答复" in text
        assert "第二轮答复" in text
        state = agent.get_state({"configurable": {"thread_id": "store-manager"}})
        assert len(state.values["messages"]) == 4

    def test_reset_isolates_conversation(self, tmp_path):
        model = ScriptedChatModel(responses=[
            AIMessage(content="旧会话答复"),
            AIMessage(content="新会话答复"),
        ])
        agent = build_agent(model=model, deps=_deps(tmp_path))
        output: list[str] = []
        code = run_repl(
            agent, _ctx(tmp_path),
            input_fn=feed(["旧问题", "/reset", "新问题", "/quit"]),
            output_fn=output.append,
        )
        text = "\n".join(output)
        assert code == 0
        assert "store-manager-1" in text
        assert "新会话答复" in text

        old_state = agent.get_state({"configurable": {"thread_id": "store-manager"}})
        new_state = agent.get_state({"configurable": {"thread_id": "store-manager-1"}})
        assert len(old_state.values["messages"]) == 2
        assert len(new_state.values["messages"]) == 2
        # 新线程的第一轮不应看到旧线程的历史：两次模型调用的上下文长度相同。
        assert model.seen_lengths[0] == model.seen_lengths[1]

    def test_per_turn_error_does_not_kill_loop_and_redacts(self, tmp_path):
        model = ScriptedChatModel(responses=[
            RuntimeError("boom sk-abc123"),
            AIMessage(content="失败后仍可继续"),
        ])
        code, output = _run_main(tmp_path, ["坏请求", "再来", "/quit"], model)
        assert code == 0
        assert "失败后仍可继续" in output
        assert "sk-***" in output
        assert "sk-abc123" not in output
        assert "本轮失败" in output


# ===========================================================================
# 工具轨迹与只读闸门
# ===========================================================================

class TestTraceAndSafety:
    def test_trace_off_by_default(self, tmp_path):
        model = ScriptedChatModel(responses=[
            _tool_call("assess_risk", {}, "c1"),
            AIMessage(content="完成"),
        ])
        agent = build_agent(model=model, deps=_deps(tmp_path))
        output: list[str] = []
        run_repl(
            agent, _ctx(tmp_path),
            input_fn=feed(["看风险", "/quit"]),
            output_fn=output.append,
        )
        text = "\n".join(output)
        assert "完成" in text
        assert "->" not in text

    def test_trace_on_shows_compact_trace(self, tmp_path):
        model = ScriptedChatModel(responses=[
            _tool_call("assess_risk", {}, "c1"),
            AIMessage(content="完成"),
        ])
        agent = build_agent(model=model, deps=_deps(tmp_path))
        output: list[str] = []
        run_repl(
            agent, _ctx(tmp_path, trace=True),
            input_fn=feed(["看风险", "/quit"]),
            output_fn=output.append,
        )
        text = "\n".join(output)
        assert "-> assess_risk({})" in text
        assert "<- assess_risk: ok=True" in text
        assert '"sessions"' not in text

    def test_read_only_blocks_write_through_console(self, tmp_path):
        rows = [{
            "store_name": "迷雾剧场",
            "script_name": "新本",
            "start_time": "2026-10-22 19:00",
            "total_seats": 6,
        }]
        model = ScriptedChatModel(responses=[
            _tool_call("store_data", {"action": "upsert_sessions", "rows": rows}, "c1"),
            AIMessage(content="写入被拒绝"),
        ])
        deps = replace(_deps(tmp_path), read_only=True)
        agent = build_agent(model=model, deps=deps)
        output: list[str] = []
        run_repl(
            agent, _ctx(tmp_path, trace=True, read_only=True),
            input_fn=feed(["写入场次", "/quit"]),
            output_fn=output.append,
        )
        text = "\n".join(output)
        assert "error_code=read_only" in text
        assert "写入被拒绝" in text
        assert not (tmp_path / "sessions.xlsx").exists()
        assert not (tmp_path / "bookings.xlsx").exists()

    def test_format_trace_truncates_and_handles_bad_json(self):
        turn = AgentTurn(
            answer="",
            tool_calls=({"name": "x", "args": {"q": "a" * 200}},),
            tool_results=({"name": "x", "content": "not json"},),
        )
        lines = format_turn_trace(turn, arg_limit=20)
        assert lines[0].endswith("...)")
        assert "ok=None" in lines[1]


# ===========================================================================
# 启动参数、密钥检查与构建失败
# ===========================================================================

class TestStartup:
    def test_default_data_dir_warning(self, tmp_path):
        output: list[str] = []
        print_banner(
            _ctx(tmp_path, explicit_data_dir=False),
            output_fn=output.append,
        )
        text = "\n".join(output)
        assert "未指定 --data-dir" in text
        assert "模式：可写" in text

    def test_missing_key_returns_2_with_guidance(self, tmp_path, monkeypatch):
        def _missing() -> str:
            raise RuntimeError(
                "未找到 DEEPSEEK_API_KEY。\n"
                "  1. 把 .env.example 复制为 .env"
            )

        monkeypatch.setattr("app.cli.get_api_key", _missing)

        def _should_not_read(prompt: str = "") -> str:
            raise AssertionError("缺 key 时不应读取输入")

        output: list[str] = []
        code = main(
            ["--data-dir", str(tmp_path)],
            input_fn=_should_not_read,
            output_fn=output.append,
            model=None,
        )
        text = "\n".join(output)
        assert code == 2
        assert ".env.example" in text
        assert "控制台未启动" in text

    def test_injected_model_skips_key_check(self, tmp_path, monkeypatch):
        def _unexpected() -> str:
            raise AssertionError("注入模型时不应检查 key")

        monkeypatch.setattr("app.cli.get_api_key", _unexpected)
        model = ScriptedChatModel(responses=[AIMessage(content="不应调用")])
        code, _ = _run_main(tmp_path, ["/quit"], model)
        assert code == 0

    def test_data_dir_store_thread_wiring(self, tmp_path):
        model = ScriptedChatModel(responses=[AIMessage(content="不应调用")])
        code, output = _run_main(
            tmp_path,
            ["/quit"],
            model,
            "--store", "迷雾剧场",
            "--thread", "t-42",
        )
        assert code == 0
        assert str(tmp_path) in output
        assert "门店：迷雾剧场" in output
        assert "会话线程：t-42" in output
        assert "已检测到 DEEPSEEK_API_KEY" not in output

    def test_missing_data_dir_returns_2(self, tmp_path):
        model = ScriptedChatModel(responses=[AIMessage(content="不应调用")])
        output: list[str] = []
        code = main(
            ["--data-dir", str(tmp_path / "不存在")],
            input_fn=feed(["/quit"]),
            output_fn=output.append,
            model=model,
        )
        assert code == 2
        assert "数据目录不存在" in "\n".join(output)

    def test_build_failure_returns_2(self, tmp_path, monkeypatch):
        def _boom(*args, **kwargs):
            raise RuntimeError("init failed: sk-secret123")

        monkeypatch.setattr("app.cli.build_agent", _boom)
        model = ScriptedChatModel(responses=[AIMessage(content="不应调用")])
        code, output = _run_main(tmp_path, ["/quit"], model)
        assert code == 2
        assert "Agent 初始化失败" in output
        assert "sk-***" in output
        assert "sk-secret123" not in output

    def test_console_wires_the_alias_sidecar(self, tmp_path, monkeypatch):
        """控制台也要接上剧名记忆，否则 remember 永远返回 alias_memory_unavailable。"""
        from app.store.script_aliases import JsonScriptAliasMemory

        captured: dict[str, Any] = {}
        real = __import__("app.cli", fromlist=["build_agent"]).build_agent

        def spy(*, model, deps, extra_system_prompt="", **_kwargs):
            captured["deps"] = deps
            return real(
                model=model, deps=deps, extra_system_prompt=extra_system_prompt
            )

        monkeypatch.setattr("app.cli.build_agent", spy)
        model = ScriptedChatModel(responses=[AIMessage(content="不应调用")])
        code, _ = _run_main(tmp_path, ["/quit"], model)

        assert code == 0
        memory = captured["deps"].script_aliases
        assert isinstance(memory, JsonScriptAliasMemory)
        assert memory.path == tmp_path / "script_aliases.json"

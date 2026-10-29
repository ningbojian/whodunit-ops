"""店长端交互控制台：在终端里连续使用 ReAct Agent。

与 scripts/01–09 的区别：
  - 脚本是单向演示，跑完即退；控制台是店长真正可以连续对话的入口。
  - Agent 只构建一次，同一 thread_id 保留多轮上下文，/reset 换新线程。
  - 默认直接读写 data/ 下的两张 Excel；--read-only 在工具层硬拦截写操作，
    --data-dir 可指向临时目录，避免试用时改到真实数据。

刻意不提供的能力：玩家直连对话、微信自动抓取 / 自动发送、支付与订单履约。
这些边界由产品定位决定，不由控制台参数决定。
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from app import llm
from app.agent import AgentDeps, AgentTurn, build_agent, run_turn
from app.config import DEEPSEEK_MODEL, SESSIONS_XLSX, get_api_key


@dataclass(frozen=True)
class ConsoleContext:
    """控制台的固定配置；当前线程由 run_repl 自行跟踪。"""

    sessions_path: Path
    bookings_path: Path
    scripts_path: Path
    store_name: str | None = None
    thread_id: str = "store-manager"
    read_only: bool = False
    trace: bool = False
    explicit_data_dir: bool = True
    model_label: str = DEEPSEEK_MODEL


def _configure_stdio() -> None:
    """Windows 终端默认编码可能不是 UTF-8；pytest 捕获流不支持时静默跳过。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if not callable(reconfigure):
            continue
        try:
            reconfigure(encoding="utf-8")
        except (ValueError, OSError):
            pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.cli",
        description="剧本杀门店私域运营 Agent 的店长交互控制台。",
    )
    parser.add_argument(
        "--data-dir",
        metavar="PATH",
        default=None,
        help="包含 sessions.xlsx / bookings.xlsx / scripts.xlsx 的目录；默认使用项目 data/。",
    )
    parser.add_argument(
        "--store",
        metavar="NAME",
        default=None,
        help="默认门店，用于抽取缺门店的文本，并写入系统提示词作为当前门店。",
    )
    parser.add_argument(
        "--thread",
        metavar="ID",
        default="store-manager",
        help="初始会话线程，默认 store-manager；/reset 会切换到新线程。",
    )
    parser.add_argument(
        "--read-only",
        action="store_true",
        help=(
            "只读模式：工具层拒绝场次/预约写入、剧本库 upsert、资源写 action、"
            "排期写 action 与草稿转正；dm_control 仍可只读查询。"
        ),
    )
    parser.add_argument(
        "--trace",
        action="store_true",
        help="每轮打印紧凑工具轨迹（工具名、截断参数、ok 与 error_code）。",
    )
    return parser


def resolve_data_dir(value: str | None) -> Path:
    """把 --data-dir 转成绝对路径；None 表示项目自带的 data/。"""
    if value is None:
        return SESSIONS_XLSX.parent
    return Path(value).expanduser().resolve()


def _exists_label(path: Path) -> str:
    return "存在" if path.exists() else "不存在，首次写入时创建"


def _trace_args(args: Any, limit: int) -> str:
    try:
        text = json.dumps(args or {}, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        text = str(args)
    text = " ".join(text.split())
    if len(text) > limit:
        text = text[:limit] + "..."
    return text


def _trace_result(content: Any) -> str:
    """只暴露 ok / error_code，不打印 data 里的价格与内部财务列。"""
    ok = None
    error_code = None
    if isinstance(content, str):
        try:
            payload = json.loads(content)
        except (TypeError, ValueError):
            payload = None
        if isinstance(payload, dict):
            ok = payload.get("ok")
            error_code = payload.get("error_code")
    label = f"ok={ok}" if ok is not None else "ok=None"
    if error_code:
        label += f", error_code={error_code}"
    return label


def format_turn_trace(turn: AgentTurn, *, arg_limit: int = 120) -> list[str]:
    """把一轮工具调用与结果压成可直接打印的短行。"""
    lines: list[str] = []
    for call in turn.tool_calls:
        name = call.get("name") or "tool"
        lines.append(f"  -> {name}({_trace_args(call.get('args'), arg_limit)})")
    for result in turn.tool_results:
        name = result.get("name") or "tool"
        lines.append(f"  <- {name}: {_trace_result(result.get('content'))}")
    return lines


def print_banner(ctx: ConsoleContext, *, output_fn: Callable[[str], None] = print) -> None:
    output_fn("剧本杀门店运营控制台（ReAct Agent）")
    output_fn(f"模型：{ctx.model_label}")
    output_fn(f"数据目录：{ctx.sessions_path.parent}")
    output_fn(f"  场次表：{ctx.sessions_path}（{_exists_label(ctx.sessions_path)}）")
    output_fn(f"  预约表：{ctx.bookings_path}（{_exists_label(ctx.bookings_path)}）")
    output_fn(f"  剧本库：{ctx.scripts_path}（{_exists_label(ctx.scripts_path)}）")
    output_fn(f"门店：{ctx.store_name or '未指定（从原文读取）'}")
    output_fn(f"会话线程：{ctx.thread_id}")
    if ctx.read_only:
        output_fn(
            "模式：只读（场次/预约/资源/排期写入与剧本库 upsert 都会被工具层拒绝；"
            "场控查询仍可用）"
        )
    else:
        output_fn("模式：可写（Agent 可能按你的指令改写上述 Excel；本控制台没有逐次确认）")
    if not ctx.explicit_data_dir:
        output_fn("注意：未指定 --data-dir，使用项目 data/。")
        output_fn("      安全试用请加 --read-only，或用 --data-dir 指向临时目录。")
    output_fn("输入 /help 查看命令，/quit 退出。")


def print_help(*, output_fn: Callable[[str], None] = print) -> None:
    output_fn("可用命令：")
    output_fn("  /help    显示本帮助")
    output_fn("  /reset   清空对话记忆（不改数据），开始新会话")
    output_fn("  /status  显示数据路径、当前会话、读写模式与消息数")
    output_fn("  /quit    退出（/exit 同义）")
    output_fn("示例：")
    output_fn("  粘贴拼场广告原文")
    output_fn("  哪场最危险？今晚先救哪场？")
    output_fn("  帮我给迷雾剧场写今晚的推广文案")
    output_fn("  NL 是哪个本？我记不清全名了")
    output_fn("  帮我加个剧本，我只记得叫《年轮》")
    output_fn("说明：以 / 开头的行按命令处理。")


def _message_count(agent: Any, thread_id: str) -> int | str:
    try:
        state = agent.get_state({"configurable": {"thread_id": thread_id}})
    except Exception:
        return "未知"
    messages = getattr(state, "values", {}).get("messages", [])
    return len(messages)


def print_status(
    agent: Any,
    ctx: ConsoleContext,
    thread_id: str,
    *,
    output_fn: Callable[[str], None] = print,
) -> None:
    output_fn(f"数据目录：{ctx.sessions_path.parent}")
    output_fn(f"  场次表：{ctx.sessions_path}（{_exists_label(ctx.sessions_path)}）")
    output_fn(f"  预约表：{ctx.bookings_path}（{_exists_label(ctx.bookings_path)}）")
    output_fn(f"  剧本库：{ctx.scripts_path}（{_exists_label(ctx.scripts_path)}）")
    output_fn(f"门店：{ctx.store_name or '未指定'}")
    output_fn(f"会话线程：{thread_id}")
    output_fn(f"模式：{'只读' if ctx.read_only else '可写'}")
    output_fn(f"本会话消息数：{_message_count(agent, thread_id)}")
    output_fn(f"模型：{ctx.model_label}")


def run_repl(
    agent: Any,
    ctx: ConsoleContext,
    *,
    input_fn: Callable[[str], str] = input,
    output_fn: Callable[[str], None] = print,
) -> int:
    """读取—执行—打印循环。返回进程退出码。"""
    thread_id = ctx.thread_id
    reset_count = 0

    while True:
        try:
            text = input_fn("店长> ")
        except EOFError:
            output_fn("已退出。")
            return 0
        except KeyboardInterrupt:
            output_fn("")
            output_fn("已退出。")
            return 0

        text = (text or "").strip()
        if not text:
            continue

        if text.startswith("/"):
            command = text.split()[0].lower()
            if command in {"/quit", "/exit"}:
                output_fn("已退出。")
                return 0
            if command == "/help":
                print_help(output_fn=output_fn)
                continue
            if command == "/status":
                print_status(agent, ctx, thread_id, output_fn=output_fn)
                continue
            if command == "/reset":
                reset_count += 1
                thread_id = f"{ctx.thread_id}-{reset_count}"
                output_fn("已重置对话记忆，数据文件未改动。")
                output_fn(f"新会话线程：{thread_id}")
                continue
            output_fn(f"未知命令：{command}。输入 /help 查看可用命令。")
            continue

        try:
            turn = run_turn(agent, text, thread_id=thread_id)
        except KeyboardInterrupt:
            output_fn("")
            output_fn("本轮已中断。对话可能停留在半个回合；如状态异常，请输入 /reset。")
            continue
        except Exception as exc:  # 控制台必须从单轮失败中恢复
            output_fn("本轮失败：" + llm.redact_secrets(f"{type(exc).__name__}: {exc}"))
            output_fn("会话仍可继续；如状态异常，请输入 /reset。")
            continue

        if ctx.trace:
            for line in format_turn_trace(turn):
                output_fn(line)
        if turn.answer.strip():
            output_fn(turn.answer.strip())
        else:
            output_fn("（模型未返回文本；可重试或 /reset。）")


def main(
    argv: list[str] | None = None,
    *,
    input_fn: Callable[[str], str] = input,
    output_fn: Callable[[str], None] = print,
    model: Any = None,
) -> int:
    """启动控制台。model 仅用于测试注入；正常运行时为 None，由 build_agent 建真实模型。"""
    _configure_stdio()
    parser = build_parser()
    args = parser.parse_args(argv)

    data_dir = resolve_data_dir(args.data_dir)
    explicit_data_dir = args.data_dir is not None
    if explicit_data_dir:
        if not data_dir.exists():
            output_fn(f"数据目录不存在：{data_dir}")
            output_fn("请先创建目录，或去掉 --data-dir 使用项目 data/。")
            return 2
        if not data_dir.is_dir():
            output_fn(f"--data-dir 不是目录：{data_dir}")
            return 2

    thread_id = (args.thread or "").strip()
    if not thread_id:
        output_fn("--thread 不能为空。")
        return 2

    if model is None:
        try:
            get_api_key()
        except RuntimeError as exc:
            output_fn(str(exc))
            output_fn("控制台未启动。离线测试与默认演示不需要 API key。")
            return 2
        output_fn("已检测到 DEEPSEEK_API_KEY（不显示具体值）。")

    store_name = (args.store or "").strip() or None
    # 公共剧本库（只读）：与 data_dir 同目录的 public_scripts.xlsx；
    # 没有就只用私有库（种子由 Web 登录或导入 CLI 触发）。
    public_frame = None
    try:
        from app.config import PUBLIC_SCRIPTS_XLSX
        from app.tools.public_library import load_public_scripts

        frame = load_public_scripts(data_dir / PUBLIC_SCRIPTS_XLSX.name)
        if not frame.empty:
            public_frame = frame
    except Exception:  # noqa: BLE001 - 公共库不可用不影响 CLI
        public_frame = None

    # 全局剧名记忆：控制台走 Excel 文件后端，所以落 data_dir 下的 JSON
    # sidecar，与网页版 Excel 模式落在同一个 data/script_aliases.json。
    from app.store.script_aliases import build_script_aliases

    script_aliases = build_script_aliases(uses_database=False, data_dir=data_dir)

    deps = AgentDeps(
        sessions_path=data_dir / "sessions.xlsx",
        bookings_path=data_dir / "bookings.xlsx",
        scripts_path=data_dir / "scripts.xlsx",
        default_store=store_name,
        read_only=args.read_only,
        public_scripts=public_frame,
        script_aliases=script_aliases,
    )

    extra_system_prompt = ""
    if store_name:
        extra_system_prompt = (
            f"【当前门店】{store_name}\n"
            "涉及门店的查询、风险评估、定价与文案，默认使用该门店；"
            "店长明确指定其他门店时，以店长指令为准。"
        )

    try:
        agent = build_agent(
            model=model,
            deps=deps,
            extra_system_prompt=extra_system_prompt,
        )
    except Exception as exc:
        output_fn("Agent 初始化失败：" + llm.redact_secrets(f"{type(exc).__name__}: {exc}"))
        return 2

    ctx = ConsoleContext(
        sessions_path=deps.sessions_path,
        bookings_path=deps.bookings_path,
        scripts_path=deps.scripts_path,
        store_name=store_name,
        thread_id=thread_id,
        read_only=args.read_only,
        trace=args.trace,
        explicit_data_dir=explicit_data_dir,
    )
    print_banner(ctx, output_fn=output_fn)
    return run_repl(agent, ctx, input_fn=input_fn, output_fn=output_fn)


if __name__ == "__main__":
    raise SystemExit(main())

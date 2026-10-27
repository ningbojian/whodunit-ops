"""把 AgentTurn 的工具轨迹压缩成前端可安全展示的摘要。

只保留：工具名、截断后的参数、ok、error_code。
绝不返回工具结果里的 data / raw_snippet / 内部财务字段。
"""

from __future__ import annotations

import json
from typing import Any

from app.agent import AgentTurn


def _truncate(text: str, limit: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: max(0, limit - 3)] + "..."


def _result_status(content: Any) -> tuple[bool | None, str | None]:
    if isinstance(content, str):
        try:
            payload = json.loads(content)
        except (TypeError, ValueError, json.JSONDecodeError):
            return None, None
    elif isinstance(content, dict):
        payload = content
    else:
        return None, None
    if not isinstance(payload, dict):
        return None, None
    ok = payload.get("ok")
    code = payload.get("error_code")
    return (ok if isinstance(ok, bool) else None, str(code) if code else None)


def safe_trace(
    turn: AgentTurn,
    *,
    max_tools: int = 20,
    arg_chars: int = 160,
) -> list[dict[str, Any]]:
    results = turn.tool_results
    items: list[dict[str, Any]] = []
    for index, call in enumerate(turn.tool_calls[:max_tools]):
        name = call.get("name") if isinstance(call, dict) else None
        args = call.get("args") if isinstance(call, dict) else None
        try:
            args_text = json.dumps(args or {}, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            args_text = "{}"
        ok: bool | None = None
        code: str | None = None
        if index < len(results):
            result = results[index]
            result_name = result.get("name")
            if result_name in (None, name):
                ok, code = _result_status(result.get("content"))
        items.append(
            {
                "tool": str(name or "unknown"),
                "args": _truncate(args_text, arg_chars),
                "ok": ok,
                "error_code": code,
            }
        )
    return items

"""把 AgentTurn 的工具结果压成前端可直接渲染的按钮组（choices）。

与 trace.safe_trace 相同的配对规则（tool_calls 与 tool_results 按下标配对、
结果名缺省或一致才算同一调用），但这里只做一件事：逆序扫描，取第一个
能产出按钮的结果，停止。

不变量（与 safe_trace 的「无裸信封」一致）：
  - 绝不拷贝 envelope 的任意字段——只从白名单字段拼装按钮；
  - payload 只含 JSON 标量（str/int/float/bool/None）；
  - 总按钮数 ≤ max_buttons，label 截断 max_label 字。

白名单两类按钮：
  1. quick_reply —— 纯文本追问（suggested_question），以及剧本歧义时的
     候选名单（create_session 返回 needs_confirmation 时，一候选一按钮）。
     点击只是重发一条预填文本，不提交任何写。
  2. resolve —— 冲突选项（switch_room / switch_dm / keep_unassigned），
     与 POST /api/app/schedule/resolve 的白名单一致。
     shift_time / cancel_or_waitlist / add_resource 只有叙述无按钮。
"""

from __future__ import annotations

import json
from typing import Any

from app.agent import AgentTurn

# 与 assignment.resolve_conflict 的可执行 choice 白名单一致。
_RESOLVABLE = frozenset({"switch_room", "switch_dm", "keep_unassigned"})

_SCALAR = (str, int, float, bool)


def _scalar(value: Any) -> Any:
    if isinstance(value, _SCALAR):
        return value
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _truncate(text: str, limit: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: max(0, limit - 3)] + "..."


def _envelope(content: Any) -> dict[str, Any] | None:
    """只接受 ok=True 的 JSON 信封；坏 JSON / 非 dict → None。"""
    if isinstance(content, str):
        try:
            payload = json.loads(content)
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
    elif isinstance(content, dict):
        payload = content
    else:
        return None
    if not isinstance(payload, dict) or payload.get("ok") is not True:
        return None
    return payload


def _resolve_buttons(
    conflicts: Any, *, max_label: int
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    buttons: list[dict[str, Any]] = []
    context: dict[str, Any] = {}
    if not isinstance(conflicts, list):
        return buttons, context
    for conflict in conflicts:
        if not isinstance(conflict, dict):
            continue
        session_id = _scalar(conflict.get("session_id"))
        options = conflict.get("options")
        if not isinstance(options, list):
            continue
        for option in options:
            if not isinstance(option, dict):
                continue
            otype = str(option.get("type") or "").strip()
            if otype not in _RESOLVABLE:
                continue
            label = _truncate(str(option.get("label") or otype), max_label)
            payload: dict[str, Any] = {"session_id": session_id, "choice": otype}
            target = option.get("target_id")
            if target not in (None, ""):
                payload["target_id"] = _scalar(target)
            buttons.append(
                {"label": label, "action": "resolve", "payload": payload}
            )
            if session_id and "session_id" not in context:
                context["session_id"] = session_id
    return buttons, context


def _groups_for(
    name: str, content: Any, *, max_buttons: int, max_label: int
) -> list[dict[str, Any]]:
    """单个工具结果 → 按钮组列表；无匹配返回 []。"""
    payload = _envelope(content)
    if payload is None:
        return []
    data = payload.get("data")
    if not isinstance(data, dict):
        return []

    groups: list[dict[str, Any]] = []
    status = str(data.get("status") or "")

    if name == "store_data" and status == "needs_confirmation":
        # 剧本歧义：一候选一个按钮，点击重发文本（纯追问，不写任何数据）。
        candidates: list[Any] = []
        ambiguities = data.get("ambiguities")
        if isinstance(ambiguities, list):
            for item in ambiguities:
                if isinstance(item, dict) and item.get("field") == "script_name":
                    found = item.get("candidates")
                    if isinstance(found, list):
                        candidates = found
                    break
        buttons: list[dict[str, Any]] = []
        for candidate in candidates[:5]:
            if not isinstance(candidate, dict):
                continue
            script_name = _scalar(candidate.get("script_name"))
            if not script_name:
                continue
            buttons.append(
                {
                    "label": _truncate(f"《{script_name}》", max_label),
                    "action": "quick_reply",
                    "payload": {"text": f"就开《{script_name}》"},
                }
            )
        if buttons:
            groups.append({"kind": "quick_reply", "buttons": buttons, "context": {}})

    # 冲突：promoted 结果嵌在 auto_assign 下；排期结果在顶层。
    conflict_sources: list[Any] = []
    if name == "store_data" and status == "promoted":
        auto = data.get("auto_assign")
        if isinstance(auto, dict):
            conflict_sources.append(auto.get("conflicts"))
    if name == "schedule_sessions":
        conflict_sources.append(data.get("conflicts"))
    for source in conflict_sources:
        buttons, context = _resolve_buttons(source, max_label=max_label)
        if buttons:
            groups.append({"kind": "resolve", "buttons": buttons, "context": context})

    # 快捷追问：任何结果的 suggested_question 非空都可作为纯文本追问。
    question = str(data.get("suggested_question") or "").strip()
    if question and not any(group["kind"] == "quick_reply" for group in groups):
        text = _truncate(question, max_label)
        groups.append(
            {
                "kind": "quick_reply",
                "buttons": [
                    {"label": text, "action": "quick_reply", "payload": {"text": question}}
                ],
                "context": {},
            }
        )
    return groups


def _cap(groups: list[dict[str, Any]], max_buttons: int) -> list[dict[str, Any]]:
    """按组顺序截断到总按钮数上限；截空的组丢弃。"""
    budget = max_buttons
    kept: list[dict[str, Any]] = []
    for group in groups:
        if budget <= 0:
            break
        buttons = group.get("buttons") or []
        if not buttons:
            continue
        kept.append({**group, "buttons": buttons[:budget]})
        budget -= len(buttons[:budget])
    return kept


def build_choices(
    turn: AgentTurn, *, max_buttons: int = 8, max_label: int = 40
) -> list[dict[str, Any]]:
    """逆序扫描工具结果，取第一个能产出按钮的结果；无匹配返回 []。"""
    calls = turn.tool_calls or ()
    results = turn.tool_results or ()
    pairs: list[tuple[str, Any]] = []
    for index, call in enumerate(calls):
        name = str(call.get("name") or "") if isinstance(call, dict) else ""
        content: Any = None
        if index < len(results):
            result = results[index]
            if isinstance(result, dict) and result.get("name") in (None, name or None):
                content = result.get("content")
        pairs.append((name, content))

    for name, content in reversed(pairs):
        groups = _groups_for(
            name, content, max_buttons=max_buttons, max_label=max_label
        )
        if groups:
            return _cap(groups, max_buttons)
    return []


__all__ = ["build_choices"]

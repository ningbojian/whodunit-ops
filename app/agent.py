"""第 9 步：把 12 个业务工具组装成 ReAct Agent。

这一层只做三件事：

  1. **适配**：把纯函数工具包装成 LLM 能调用的 `@tool`。
     入参用简单类型（字符串 / 数字 / 对象数组），出参统一 JSON 字符串；
     模型不需要知道 pandas、Path、DataFrame 这些实现细节。

  2. **路由**：用系统提示词告诉模型各业务场景分别该调哪些工具。
     业务规则仍然在工具里，Agent 不重新实现一遍选场、定价、排期或文案校验。

  3. **兜底**：工具的业务失败全部变成 `{"ok": false, ...}`，
     不向 ReAct 循环抛异常；只有 KeyboardInterrupt / SystemExit 原样抛出。

为什么工具用工厂函数创建：

  `AgentDeps` 里带着数据文件路径、固定时间、可注入的模型。
  生产用默认路径；测试和 demo 指向临时目录，绝不碰真实 data/。
  如果写成模块级 `@tool`，路径和时间就只能靠 monkeypatch 全局变量，容易漏。

仍然不做的三件事（与最初约束一致）：
  - 不做玩家直连对话；
  - 不自动抓取 / 发送微信消息；
  - 不实现支付、订单履约。
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import date, datetime
from functools import wraps
from pathlib import Path
from typing import Any, Callable, Sequence

import pandas as pd
from langchain.agents import create_agent
from langchain_core.tools import BaseTool, tool
from langgraph.checkpoint.memory import InMemorySaver

from app import llm
from app.config import BOOKINGS_XLSX, SCRIPTS_XLSX, SESSIONS_XLSX
from app.store.excel import ExcelStore
from app.tools.assignment import (
    auto_assign as auto_assign_sessions,
    list_schedule,
    load_schedule_context,
    resolve_conflict as resolve_schedule_conflict,
)
from app.tools.copywriter import generate_copy
from app.tools.dedupe import DEDUPE_COLUMNS, dedupe_sessions as dedupe_records
from app.tools.demo import seed_demo_resources as seed_demo_resource_rows
from app.tools.dm_control import (
    DMControlError,
    get_run as get_dm_run,
    list_runs_today,
    run_report as build_run_report,
)
from app.tools.expiry import derive_status, with_derived_status
from app.tools.extract import extract_message as extract_text
from app.tools.pricing import PRICING_COLUMNS, price_sessions
from app.tools.resources import (
    dms_to_dict,
    load_dm_shifts,
    load_dms,
    load_rooms,
    replace_dm_shifts,
    rooms_to_dict,
    schedule_dm_inputs,
    setup_checklist,
    shifts_to_dict,
    upsert_dms,
    upsert_rooms,
)
from app.tools.risk import assess_sessions
from app.tools.scheduling import build_schedule
from app.tools.scripts import (
    find_scripts,
    get_script,
    list_scripts,
    load_scripts,
    upsert_script,
)
from app.tools.session_intake import (
    create_session_from_fields,
    update_session,
)
from app.tools.storage import (
    SESSIONS_COLUMNS,
    STATUS_CANCELLED,
    STATUS_PENDING,
    STATUS_STARTED,
    cancel_booking,
    find_orphan_bookings,
    get_session,
    is_missing_value,
    list_bookings,
    load_sessions,
    load_sessions_with_counts,
    query_sessions,
    record_booking_checked,
    upsert_booking,
    upsert_sessions,
)

# ===========================================================================
# 一、依赖、返回类型、JSON 信封
# ===========================================================================

def _system_now() -> pd.Timestamp:
    return pd.Timestamp.now()


@dataclass(frozen=True)
class AgentDeps:
    """Agent 运行时依赖。测试全部换成临时路径与固定时间。"""

    sessions_path: Path = SESSIONS_XLSX
    bookings_path: Path = BOOKINGS_XLSX
    scripts_path: Path = SCRIPTS_XLSX
    store: Any = None               # Store 协议；None -> 从三个 Excel 路径构造
    now: Callable[[], pd.Timestamp] = _system_now
    extraction_model: Any = None   # None -> extract_message 内部取默认模型
    copy_model: Any = None         # None -> generate_copy 内部取默认模型
    default_store: str | None = None
    read_only: bool = False        # True -> store_data 三个写 action + script_library upsert/remember 被拒绝
    public_scripts: Any = None     # 公共库帧（只读）；None -> 仅私有库，行为与旧版一致
    tenant_id: str | None = None   # web_search 日配额的租户键；None -> "default"（CLI 单用户）
    search_usage: Any = None       # 配额计数器（Json/Sql SearchUsage）；None -> 工具内建 Excel sidecar
    web_search_provider: Any = None  # 注入 provider 测试；None -> 按 env 解析
    # 全局剧名记忆（只存「说法 -> 公共库 script_id」）。只在正常匹配为空时点查，
    # 从不参与打分；None -> 搜索行为与没有这个功能时完全一致。
    script_aliases: Any = None


@dataclass(frozen=True)
class AgentTurn:
    """一轮对话的结果：最终答复 + 本轮工具轨迹。"""

    answer: str
    tool_calls: tuple[dict[str, Any], ...] = ()
    tool_results: tuple[dict[str, Any], ...] = ()
    messages: tuple[Any, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "answer": self.answer,
            "tool_calls": [dict(item) for item in self.tool_calls],
            "tool_results": [dict(item) for item in self.tool_results],
        }


def _public_shadow_warnings(deps: AgentDeps, script_name: Any) -> list[str]:
    """私有 upsert 成功后：公共库已有同名时提示「私有覆盖显示」。"""
    public = getattr(deps, "public_scripts", None)
    if public is None or getattr(public, "empty", True) or not script_name:
        return []
    from app.tools.scripts import normalize_script_name

    key = normalize_script_name(script_name).casefold()
    if not key:
        return []
    try:
        keys = {
            normalize_script_name(value).casefold()
            for value in public["script_name"].tolist()
        }
    except (KeyError, TypeError):
        return []
    if key in keys:
        return ["公共库已有同名条目；私有档案在检索与排期时会覆盖公共条目显示。"]
    return []


def _candidate_warnings(items: Sequence[dict[str, Any]]) -> list[str]:
    """给候选加「这不是确认过的记录」的说明。

    ``find_scripts`` 每条结果都带 ``match_quality``，但模型看到的是一串
    字段齐全的档案。不把弱匹配单独点出来，它就会当成已确认的剧本来用。
    """
    qualities = {item.get("match_quality") for item in items}
    warnings: list[str] = []
    if "learned" in qualities:
        # 措辞只说「匹配不是按本店档案来的」，不说「结果不是本地档案」：
        # 本店若有同名私有剧本，返回的那一条恰恰就是私有行。
        warnings.append(
            "有结果是通过其他门店确认过的剧名映射命中的，不是按本店档案"
            "匹配出来的，请核对全名。"
        )
    if "suggested" in qualities:
        warnings.append(
            "有结果只是按字面相近猜出来的候选，不是确认过的匹配；"
            "必须让店长确认全名后才能使用。"
        )
    return warnings


# --- 工具返回里允许出现的字段 ---
_SESSION_TOOL_FIELDS = [
    "session_id", "store_name", "script_name", "script_type",
    "start_time", "total_seats", "booked_seats", "remaining_seats",
    "list_price", "status", "derived_status",
]
_BOOKING_TOOL_FIELDS = [
    "booking_id", "session_id", "player_name", "seats",
    "status", "booked_at", "updated_at",
]
_RISK_TOOL_FIELDS = [
    "session_id", "store_name", "script_name", "start_time",
    "total_seats", "booked_seats", "remaining_seats", "list_price",
    "hours_left", "gap_ratio", "needed_rate", "rate_multiple",
    "time_level", "fail_risk", "rescue_level", "stake_level",
    "priority_score", "priority_level",
]
_PRICING_TOOL_FIELDS = [
    "session_id", "store_name", "script_name", "start_time",
    "total_seats", "booked_seats", "remaining_seats", "list_price",
    "priority_level", "fail_risk",
    *PRICING_COLUMNS,
]

# store_data 中真正会写数据的动作；只读模式全部拦截。
_WRITE_ACTIONS = frozenset(
    {
        "upsert_sessions",
        "record_booking",
        "cancel_booking",
        "create_session",
        "update_session",
    }
)


def _plain(value: Any) -> Any:
    """把 pandas / numpy 的值转成可 JSON 序列化的 Python 原生值。"""
    if value is None:
        return None
    if isinstance(value, (list, tuple, set)):
        return [_plain(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in value.items()}
    # 先判空：NaT 也是 Timestamp，先走时间格式化会得到 "NaT" 而不是 None。
    if is_missing_value(value):
        return None
    if isinstance(value, (pd.Timestamp, datetime, date)):
        return pd.Timestamp(value).strftime("%Y-%m-%d %H:%M")
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return None if not math.isfinite(value) else value
    if isinstance(value, str):
        return value
    # numpy 标量：np.int64(3).item() -> 3，np.float64(1.5).item() -> 1.5
    item = getattr(value, "item", None)
    if callable(item):
        try:
            return _plain(item())
        except (ValueError, AttributeError):
            pass
    return str(value)


def _number(value: Any, digits: int = 2) -> Any:
    """数值转原生类型并保留指定小数位；空值仍是 None。"""
    plain = _plain(value)
    if plain is None or isinstance(plain, bool):
        return plain
    if isinstance(plain, (int, float)):
        return round(float(plain), digits)
    return plain


def _records(df: pd.DataFrame | None, fields: Sequence[str]) -> list[dict[str, Any]]:
    """按字段白名单把 DataFrame 转成记录数组，缺失列补 None。"""
    if df is None or df.empty:
        return []
    return [
        {field: _plain(row.get(field)) for field in fields}
        for row in df.to_dict("records")
    ]


def _envelope(
    ok: bool,
    *,
    data: Any = None,
    warnings: Sequence[str] = (),
    error_code: str | None = None,
    error: str | None = None,
    raw_snippet: str | None = None,
) -> str:
    body: dict[str, Any] = {
        "ok": ok,
        "data": _plain(data),
        "warnings": [_plain(item) for item in warnings if item],
        "error_code": error_code,
        "error": error,
    }
    if raw_snippet is not None:
        body["raw_snippet"] = raw_snippet
    return json.dumps(body, ensure_ascii=False)


def _ok(data: Any = None, warnings: Sequence[str] = ()) -> str:
    return _envelope(True, data=data, warnings=warnings)


def _err(
    code: str,
    message: str,
    *,
    data: Any = None,
    warnings: Sequence[str] = (),
    raw_snippet: str | None = None,
) -> str:
    return _envelope(
        False,
        data=data,
        warnings=warnings,
        error_code=code,
        error=llm.redact_secrets(message),
        raw_snippet=raw_snippet,
    )


def _safe_tool(fn: Callable[..., str]) -> Callable[..., str]:
    """兜住未预期异常，避免一个工具 bug 直接把 ReAct 循环打断。"""

    @wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> str:
        try:
            return fn(*args, **kwargs)
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception as exc:  # noqa: BLE001 - 工具层必须把失败转成结构化结果
            return _err(
                "internal_error",
                f"{type(exc).__name__}: {exc}",
            )

    return wrapper


def _derive_row(row: dict[str, Any], now: pd.Timestamp) -> dict[str, Any]:
    """给单场记录补一个推导状态，不改文件里的 status。"""
    out = dict(row)
    out["derived_status"] = derive_status(
        out.get("start_time"), out.get("status"), now=now
    )
    return out


def _dedupe_notes(df: pd.DataFrame) -> list[str]:
    if df is None or df.empty or "dedupe_note" not in df.columns:
        return []
    notes: list[str] = []
    for row in df.to_dict("records"):
        note = _plain(row.get("dedupe_note"))
        if not note:
            continue
        label = _plain(row.get("script_name")) or _plain(row.get("session_id")) or "未命名场次"
        note = f"{label}：{note}"
        if note not in notes:
            notes.append(note)
    return notes


def _price_warnings(df: pd.DataFrame) -> list[str]:
    if df is None or df.empty or "price_note" not in df.columns:
        return []
    warnings: list[str] = []
    for row in df.to_dict("records"):
        note = _plain(row.get("price_note"))
        if not note:
            continue
        label = _plain(row.get("script_name")) or _plain(row.get("session_id")) or "未命名场次"
        text = f"{label}：{note}"
        if text not in warnings:
            warnings.append(text)
    return warnings


# ===========================================================================
# 二、12 个工具
# ===========================================================================

def build_store_tools(deps: AgentDeps | None = None) -> list[BaseTool]:
    """创建绑定到同一份依赖上的 12 个业务工具。

    返回顺序与 README 的编号一致，便于测试与演示。
    """
    deps = deps or AgentDeps()
    store = deps.store or ExcelStore(
        sessions_path=deps.sessions_path,
        bookings_path=deps.bookings_path,
        scripts_path=deps.scripts_path,
    )

    # ------------------------------------------------------------------
    # 工具 1：文本抽取
    # ------------------------------------------------------------------
    @tool("extract_message")
    @_safe_tool
    def _extract_tool(text: str, default_store: str | None = None) -> str:
        """从一段微信 / 朋友圈原文中抽取场次、预约或跳车信息。

        何时使用：
          - 店长转发拼场广告、预约消息、跳车消息时，先调用本工具。

        参数：
          text          原文，逐字传入，不要自己改写。
          default_store 仅当原文确实没写门店时使用；不确定就不要传。

        返回：
          intent（广告 / 预约 / 跳车 / 混合 / 未知）、sessions、bookings、warnings。
          本工具只解析，不写库；写库请继续调用 store_data。
        """
        store = default_store.strip() if isinstance(default_store, str) else ""
        result = extract_text(
            text,
            model=deps.extraction_model,
            now=deps.now(),
            default_store=store or deps.default_store,
        )
        data = {
            "intent": result.intent,
            "sessions": result.sessions,
            "bookings": result.bookings,
        }
        if result.ok:
            return _ok(data, result.warnings)
        return _err(
            result.error_code or "extract_error",
            result.error or "文本抽取失败",
            data=data,
            warnings=result.warnings,
            raw_snippet=result.raw_snippet,
        )

    # ------------------------------------------------------------------
    # 工具 2：场次去重（只读）
    # ------------------------------------------------------------------
    @tool("dedupe_sessions")
    @_safe_tool
    def _dedupe_tool(
        records: list[dict[str, Any]],
        compare_existing: bool = True,
    ) -> str:
        """对抽取出的场次记录做批内去重，并可与现有表比对。

        何时使用：
          - extract_message 返回 sessions 之后，写入之前。
          - 想知道「这条广告会新增还是更新哪一场」时。

        参数：
          records           场次对象数组，可直接用 extract_message 返回的 sessions。
          compare_existing  true 读取现有表并标注 新增 / 更新 / 无变化；false 只做批内合并。

        返回：
          count 与 records；每条带 dedupe_action、changed_fields、dedupe_note。
          本工具**只读**，不会写 Excel。
        """
        if not isinstance(records, list) or not records:
            return _err("bad_request", "records 必须是非空的对象数组。")
        try:
            existing = (
                load_sessions(deps.sessions_path, store=store)
                if compare_existing
                else None
            )
            deduped = dedupe_records(records, existing=existing)
        except (TypeError, ValueError, KeyError) as exc:
            return _err("bad_request", str(exc))

        data = {
            "count": int(len(deduped)),
            "records": _records(
                deduped, [*SESSIONS_COLUMNS, *DEDUPE_COLUMNS]
            ),
        }
        return _ok(data, _dedupe_notes(deduped))

    # ------------------------------------------------------------------
    # 工具 3：过期过滤（只读）
    # ------------------------------------------------------------------
    @tool("filter_expired")
    @_safe_tool
    def _filter_tool(
        store_name: str | None = None,
        script_name: str | None = None,
    ) -> str:
        """按当前时间把场次分成 待开场 / 已开场 / 已取消 三组。

        何时使用：
          - 店长问「哪些场已经过期了」「有没有取消的场」。
          - 风险与定价工具内部会自动排除过期和取消场次；
            只有需要**展示**过期名单时才单独调用本工具。

        参数：
          store_name   可选，按门店精确过滤。
          script_name  可选，按剧本精确过滤。

        返回：
          kept / expired / cancelled 三组记录与各自数量。只读。
        """
        sessions = query_sessions(
            store_name=store_name,
            script_name=script_name,
            with_counts=True,
            path=deps.sessions_path,
            bookings_path=deps.bookings_path,
            store=store,
        )
        if sessions.empty:
            return _ok(
                {"counts": {"kept": 0, "expired": 0, "cancelled": 0},
                 "kept": [], "expired": [], "cancelled": []},
                ["没有场次。"],
            )

        marked = with_derived_status(sessions, now=deps.now())
        kept = marked[marked["derived_status"] == STATUS_PENDING]
        expired = marked[marked["derived_status"] == STATUS_STARTED]
        cancelled = marked[marked["derived_status"] == STATUS_CANCELLED]
        data = {
            "counts": {
                "kept": int(len(kept)),
                "expired": int(len(expired)),
                "cancelled": int(len(cancelled)),
            },
            "kept": _records(kept, _SESSION_TOOL_FIELDS),
            "expired": _records(expired, _SESSION_TOOL_FIELDS),
            "cancelled": _records(cancelled, _SESSION_TOOL_FIELDS),
        }
        return _ok(data)

    # ------------------------------------------------------------------
    # 工具 4：空场风险
    # ------------------------------------------------------------------
    @tool("assess_risk")
    @_safe_tool
    def _assess_tool(
        store_name: str | None = None,
        script_name: str | None = None,
        start_from: str | None = None,
        start_to: str | None = None,
    ) -> str:
        """评估场次的空场风险，并按「该先救哪场」排序。

        何时使用：
          - 录入 / 更新场次后给店长运营建议。
          - 店长问「哪场最危险」「今晚先救哪场」。

        参数：
          store_name   可选，按门店精确过滤。
          script_name  可选，按剧本精确过滤。
          start_from   可选，开场时间下界，如 2026-10-20。
          start_to     可选，开场时间上界。

        返回：
          按 priority_level（高危 / 关注 / 正常）排序的场次。
          fail_risk 表示会不会黄；priority_level 表示先救哪场。
          已开场与已取消场次自动排除。
        """
        sessions = query_sessions(
            store_name=store_name,
            script_name=script_name,
            start_from=start_from,
            start_to=start_to,
            with_counts=True,
            path=deps.sessions_path,
            bookings_path=deps.bookings_path,
            store=store,
        )
        if sessions.empty:
            return _ok({"count": 0, "sessions": []}, ["没有可评估的场次。"])

        assessed = assess_sessions(sessions, now=deps.now())
        if assessed.empty:
            return _ok(
                {"count": 0, "sessions": []},
                ["没有未开场且未取消的场次；过期或已取消的场次可调 filter_expired 查看。"],
            )

        records = _records(assessed, _RISK_TOOL_FIELDS)
        for record in records:
            for field in ("hours_left", "gap_ratio", "needed_rate", "rate_multiple"):
                record[field] = _number(record.get(field), 2)
        return _ok({"count": len(records), "sessions": records})

    # ------------------------------------------------------------------
    # 工具 5：动态定价
    # ------------------------------------------------------------------
    @tool("suggest_pricing")
    @_safe_tool
    def _pricing_tool(
        store_name: str | None = None,
        script_name: str | None = None,
        start_from: str | None = None,
        start_to: str | None = None,
    ) -> str:
        """计算每场的推荐售价、让价金额与内部经营指标。

        何时使用：
          - 店长问「这场该卖多少钱」「能打几折」「保本线是多少」。
          - 录入 / 更新场次后需要给出折扣建议。

        参数：
          store_name / script_name / start_from / start_to 均为可选过滤条件。

        返回：
          推荐价与内部指标。floor_price / break_even_price / sunk_cost /
          profit_if_sold_out / contribution_per_seat 是**内部参考**，
          只能给店长看，不得写进对外文案。
          营销文案必须调用 write_marketing_copy。
        """
        sessions = query_sessions(
            store_name=store_name,
            script_name=script_name,
            start_from=start_from,
            start_to=start_to,
            with_counts=True,
            path=deps.sessions_path,
            bookings_path=deps.bookings_path,
            store=store,
        )
        if sessions.empty:
            return _ok({"count": 0, "sessions": []}, ["没有可定价的场次。"])

        priced = price_sessions(sessions, now=deps.now())
        if priced.empty:
            return _ok(
                {"count": 0, "sessions": []},
                ["没有未开场且未取消的场次；过期或已取消的场次可调 filter_expired 查看。"],
            )

        records = _records(priced, _PRICING_TOOL_FIELDS)
        for record in records:
            for field in (
                "recommended_price", "seat_saving", "floor_price",
                "break_even_price", "sunk_cost", "profit_if_sold_out",
                "contribution_per_seat",
            ):
                record[field] = _number(record.get(field), 2)
        return _ok(
            {"count": len(records), "sessions": records},
            _price_warnings(priced),
        )

    # ------------------------------------------------------------------
    # 工具 6：表格读写
    # ------------------------------------------------------------------
    @tool("store_data")
    @_safe_tool
    def _store_tool(
        action: str,
        store_name: str | None = None,
        script_name: str | None = None,
        session_id: str | None = None,
        player_name: str | None = None,
        seats: int = 1,
        rows: list[dict[str, Any]] | None = None,
        start_from: str | None = None,
        start_to: str | None = None,
        include_cancelled: bool = False,
        limit: int = 20,
        fields: dict[str, Any] | None = None,
        source_text: str | None = None,
    ) -> str:
        """读写场次表、预约明细与场次录入。action 决定具体操作。

        action 取值：
          query_sessions       查场次（可按门店 / 剧本 / 时间过滤，带真实人数）。
                               参数：store_name, script_name, start_from, start_to, limit
          get_session          按 session_id 查单场。参数：session_id
          upsert_sessions      写入一批完整场次（有则更新、无则新增，自动去重）。
                               参数：rows（对象数组，可直接用 extract_message 的 sessions）
                               不接受 room_id / dm_id；写入后自动尝试排期。
          record_booking       记录一条预约。参数：session_id, player_name, seats
          cancel_booking       取消一条预约（软删除）。参数：session_id, player_name
          list_bookings        列出某场预约人。参数：session_id, include_cancelled
          find_orphan_bookings 找出指向不存在场次的预约。无额外参数。
          create_session       增量建场。参数：fields（对象）、source_text。
                               hard required 只有 script_name 与 start_time；
                               齐全时直接建场并自动排期；缺任意一项时只返回一个
                               suggested_question 追问，不写任何数据（不落草稿）。
          update_session       修正已保存场次。参数：session_id、fields。
                               改剧本 / 开场时间会重算 session_id 并搬移预约与排期；
                               该场有进行中场控时返回 run_active。

        fields 只接受 store_name / script_name / script_type / start_time /
        start_time_text / total_seats / list_price / notes；带 room_id / dm_id
        会返回 bad_request。房间和 DM 永远由系统分配。

        写操作只在店长明确要求录入 / 预约 / 跳车 / 补全时使用。
        只读模式下，所有写 action 返回 error_code=read_only，不修改数据。
        record_booking 会先确认场次存在并复核容量；满员返回 capacity_exceeded。
        """
        try:
            limit_value = int(limit)
        except (TypeError, ValueError):
            return _err("bad_request", "limit 必须是整数。")
        limit_value = max(1, min(limit_value, 50))

        if deps.read_only and action in _WRITE_ACTIONS:
            return _err(
                "read_only",
                f"只读模式：已拒绝 {action}，未修改任何文件。"
                "若需写入，请重启并去掉 --read-only。",
            )

        if action == "query_sessions":
            sessions = query_sessions(
                store_name=store_name,
                script_name=script_name,
                start_from=start_from,
                start_to=start_to,
                with_counts=True,
                path=deps.sessions_path,
                bookings_path=deps.bookings_path,
                store=store,
            )
            if sessions.empty:
                return _ok({"count": 0, "total": 0, "sessions": []}, ["没有符合条件的场次。"])
            marked = with_derived_status(sessions, now=deps.now())
            total = int(len(marked))
            page = marked.head(limit_value)
            return _ok({
                "count": int(len(page)),
                "total": total,
                "sessions": _records(page, _SESSION_TOOL_FIELDS),
            })

        if action == "get_session":
            sid = str(session_id or "").strip()
            if not sid:
                return _err("bad_request", "get_session 需要 session_id。")
            row = get_session(
                sid,
                path=deps.sessions_path,
                bookings_path=deps.bookings_path,
                store=store,
            )
            if row is None:
                suggestions = query_sessions(
                    store_name=store_name,
                    script_name=script_name,
                    with_counts=True,
                    path=deps.sessions_path,
                    bookings_path=deps.bookings_path,
                    store=store,
                )
                if not suggestions.empty:
                    suggestions = with_derived_status(suggestions, now=deps.now()).head(5)
                return _err(
                    "session_not_found",
                    f"没有找到 session_id={sid} 的场次。请先用 query_sessions 确认场次。",
                    data={"suggestions": _records(suggestions, _SESSION_TOOL_FIELDS)},
                )
            row = _derive_row(row, deps.now())
            return _ok({"session": {field: _plain(row.get(field)) for field in _SESSION_TOOL_FIELDS}})

        if action == "upsert_sessions":
            if not isinstance(rows, list) or not rows:
                return _err("bad_request", "upsert_sessions 需要非空的 rows 对象数组。")
            if any(
                isinstance(row, dict)
                and (_plain(row.get("room_id")) or _plain(row.get("dm_id")))
                for row in rows
            ):
                return _err(
                    "room_not_allowed",
                    "场次录入不接受房间或 DM；保存后由系统自动分配。",
                )
            try:
                existing = load_sessions(deps.sessions_path, store=store)
                summary = dedupe_records(rows, existing=existing)
                merged = upsert_sessions(rows, path=deps.sessions_path, store=store)
            except (TypeError, ValueError, KeyError) as exc:
                return _err("bad_request", str(exc))

            target_ids = [
                str(item.get("session_id") or "").strip()
                for item in rows
                if isinstance(item, dict)
            ]
            target_ids = [item for item in target_ids if item]
            auto_assign = auto_assign_sessions(
                store,
                session_ids=target_ids or None,
                now=deps.now(),
                trigger="agent_upsert_sessions",
            )

            counts: dict[str, int] = {}
            if not summary.empty and "dedupe_action" in summary.columns:
                for key, value in summary["dedupe_action"].value_counts().items():
                    counts[str(key)] = int(value)
            data = {
                "input_count": len(rows),
                "deduped_count": int(len(summary)),
                "total_sessions": int(len(merged)),
                "actions": counts,
                "records": _records(
                    summary.head(limit_value),
                    [*SESSIONS_COLUMNS, *DEDUPE_COLUMNS],
                ),
                "auto_assign": auto_assign,
            }
            return _ok(data, _dedupe_notes(summary))

        if action == "create_session":
            if not isinstance(fields, dict) or not fields:
                return _err(
                    "bad_request",
                    "create_session 需要 fields 对象，例如 {script_name, start_time}。",
                )
            if "room_id" in fields or "dm_id" in fields:
                return _err(
                    "room_not_allowed",
                    "场次录入不接受房间或 DM；建场后由系统自动分配。",
                )
            try:
                result = create_session_from_fields(
                    fields=fields,
                    source_text=source_text,
                    default_store=deps.default_store or "迷雾剧场",
                    path=deps.sessions_path,
                    store=store,
                    now=deps.now(),
                    public=deps.public_scripts,
                )
            except ValueError as exc:
                return _err(
                    getattr(exc, "code", None) or "bad_request",
                    str(exc),
                )
            warnings = list(result.pop("warnings", []) or [])
            return _ok(result, warnings)

        if action == "update_session":
            sid = str(session_id or "").strip()
            if not sid:
                return _err("bad_request", "update_session 需要 session_id。")
            if not isinstance(fields, dict) or not fields:
                return _err("bad_request", "update_session 需要 fields 对象。")
            if "room_id" in fields or "dm_id" in fields:
                return _err(
                    "room_not_allowed",
                    "场次修正不接受房间或 DM；请用 schedule_sessions(resolve)。",
                )
            try:
                result = update_session(
                    session_id=sid,
                    fields=fields,
                    path=deps.sessions_path,
                    store=store,
                    now=deps.now(),
                )
            except ValueError as exc:
                return _err(getattr(exc, "code", None) or "bad_request", str(exc))
            warnings = list(result.pop("warnings", []) or [])
            return _ok(result, warnings)

        if action == "record_booking":
            sid = str(session_id or "").strip()
            name = str(player_name or "").strip()
            if not sid or not name:
                return _err("bad_request", "record_booking 需要 session_id 与 player_name。")
            try:
                seats_value = int(seats)
            except (TypeError, ValueError):
                return _err("bad_request", "seats 必须是整数。")
            if seats_value < 1:
                return _err("bad_request", "seats 至少为 1。")

            check = record_booking_checked(
                sid,
                name,
                seats=seats_value,
                path=deps.bookings_path,
                now=deps.now(),
                store=store,
            )
            if not check.ok:
                suggestions: list[dict[str, Any]] = []
                if check.error_code == "session_not_found":
                    candidates = query_sessions(
                        store_name=store_name,
                        script_name=script_name,
                        with_counts=True,
                        path=deps.sessions_path,
                        bookings_path=deps.bookings_path,
                        store=store,
                    )
                    if not candidates.empty:
                        candidates = with_derived_status(candidates, now=deps.now()).head(5)
                        suggestions = _records(candidates, _SESSION_TOOL_FIELDS)
                return _err(
                    check.error_code or "booking_failed",
                    check.error or "预约写入失败。",
                    data={
                        "suggestions": suggestions,
                        "warnings": list(check.warnings),
                    },
                )
            updated = get_session(
                sid,
                path=deps.sessions_path,
                bookings_path=deps.bookings_path,
                store=store,
            )
            updated = _derive_row(updated or {}, deps.now())
            return _ok({
                "session_id": sid,
                "player_name": name,
                "seats": seats_value,
                "session": {field: _plain(updated.get(field)) for field in _SESSION_TOOL_FIELDS},
            })

        if action == "cancel_booking":
            sid = str(session_id or "").strip()
            name = str(player_name or "").strip()
            if not sid or not name:
                return _err("bad_request", "cancel_booking 需要 session_id 与 player_name。")
            changed = cancel_booking(
                sid,
                name,
                path=deps.bookings_path,
                now=deps.now(),
                store=store,
            )
            if not changed:
                active = list_bookings(
                    sid,
                    include_cancelled=False,
                    path=deps.bookings_path,
                    store=store,
                )
                return _err(
                    "booking_not_found",
                    f"没有找到 {name} 在 {sid} 的有效预约（可能不存在或已取消）。",
                    data={"active_bookings": _records(active, _BOOKING_TOOL_FIELDS)},
                )
            updated = get_session(
                sid,
                path=deps.sessions_path,
                bookings_path=deps.bookings_path,
                store=store,
            )
            updated = _derive_row(updated or {}, deps.now())
            return _ok({
                "session_id": sid,
                "player_name": name,
                "cancelled": True,
                "session": {field: _plain(updated.get(field)) for field in _SESSION_TOOL_FIELDS},
            })

        if action == "list_bookings":
            sid = str(session_id or "").strip()
            if not sid:
                return _err("bad_request", "list_bookings 需要 session_id。")
            bookings = list_bookings(
                sid,
                include_cancelled=bool(include_cancelled),
                path=deps.bookings_path,
                store=store,
            )
            return _ok({
                "count": int(len(bookings)),
                "bookings": _records(bookings, _BOOKING_TOOL_FIELDS),
            })

        if action == "find_orphan_bookings":
            orphans = find_orphan_bookings(
                sessions_path=deps.sessions_path,
                bookings_path=deps.bookings_path,
                store=store,
            )
            return _ok({
                "count": int(len(orphans)),
                "bookings": _records(orphans.head(limit_value), _BOOKING_TOOL_FIELDS),
            })

        return _err(
            "bad_request",
            f"未知 action：{action}。可用值见工具说明。",
        )

    # ------------------------------------------------------------------
    # 工具 7：营销文案
    # ------------------------------------------------------------------
    @tool("write_marketing_copy")
    @_safe_tool
    def _copy_tool(
        store_name: str | None = None,
        max_sessions: int = 3,
    ) -> str:
        """生成社群短版 + 朋友圈长版两套营销文案。

        何时使用：
          - 店长要求写推广文案、朋友圈文案、群里发的拼场消息。

        参数：
          store_name   可选；一次只处理一个门店。多门店数据必须指定。
          max_sessions 最多写几场，默认 3，范围 1-5。

        返回：
          community（社群短版）、moments（朋友圈长版）、sessions（入选场次）、warnings。
          工具内部已完成选场、双层脱敏、内部财务词与价格校验。
          失败时（如 internal_leak / price_mismatch）不会返回可复制文案；
          不要自行补写，直接把失败原因告诉店长。
        """
        sessions = load_sessions_with_counts(
            deps.sessions_path,
            deps.bookings_path,
            store=store,
        )
        result = generate_copy(
            sessions,
            model=deps.copy_model,
            now=deps.now(),
            max_sessions=max_sessions,
            store_name=store_name,
        )
        data = {
            "community": result.community,
            "moments": result.moments,
            "sessions": result.sessions,
        }
        if result.ok:
            return _ok(data, result.warnings)
        return _err(
            result.error_code or "copy_error",
            result.error or "营销文案生成失败",
            data=data,
            warnings=result.warnings,
            raw_snippet=result.raw_snippet,
        )

    # ------------------------------------------------------------------
    # 工具 8：剧本库（档案 / 别名 / 缺失字段）
    # ------------------------------------------------------------------
    @tool("script_library")
    @_safe_tool
    def _script_tool(
        action: str,
        query: str | None = None,
        script_id: str | None = None,
        script_name: str | None = None,
        aliases: str | None = None,
        script_type: str | None = None,
        total_seats: int | float | str | None = None,
        duration_minutes: int | float | str | None = None,
        list_price: int | float | str | None = None,
        notes: str | None = None,
        limit: int = 10,
    ) -> str:
        """维护剧本档案，并处理缩写 / 别名和缺失字段。

        action 取值：
          search  按全名、别名或缩写找候选。参数：query, limit
                  返回剧本库 + 场次历史两类来源。**只查本地数据，不联网。**
                  每条结果带 match_quality：exact / prefix / partial / loose
                  是正常匹配；learned 是「其他门店确认过的说法」命中的；
                  suggested 是按字面相近猜出来的**候选**，必须店长确认。
          get     按 script_id 取一条档案。参数：script_id
          upsert  新增或补齐档案。参数：script_id（补已有草稿时必带）、
                  script_name、aliases、script_type、total_seats、
                  duration_minutes、list_price、notes
          remember 记录一条剧名映射（全站共享的公共记忆）。参数：query
                  （店长最初用的那个说法）、script_id 或 script_name。
                  **只在店长确认全名之后调用**，且只对公共库条目有效。
                  记录内容是「说法 -> 公共库 script_id」，不含对话正文。
          list    列出剧本库。参数：limit

        重要规则：
          - 店长说缩写时，先 search；本地没有匹配，可以给出你联想的全名候选，
            但必须明确说是「猜测」，等店长确认后再 upsert。
            绝不能把猜测的名字直接写进档案，也不能假装 search 到了。
          - 店长要加剧本但信息不全时，**先保存已知字段**，再根据返回的
            missing_fields / next_question 问缺什么；不要要求一次说完。
            补全时带上返回的 script_id。
          - total_seats / duration_minutes / list_price 接受「8人」「4小时」
            「168元」这类写法，工具会解析。
          - 只读模式下 upsert / remember 返回 error_code=read_only。
          - remember 不是「模糊匹配设置」：它记的是**精确键 → 公共库条目**，
            只有在店长指认全名之后才有意义。未经确认不得调用。
            被拒（not_similar / sensitive_query / collides_with_other /
            ambiguous_query / alias_quota_exceeded）说明这个说法没法当别名
            存，如实告诉店长没记上即可，**不要改写说法后再试一次**。
        """
        try:
            limit_value = max(1, min(int(limit), 50))
        except (TypeError, ValueError):
            return _err("bad_request", "limit 必须是整数。")

        if action == "search":
            sessions = load_sessions(deps.sessions_path, store=store)
            items = find_scripts(
                query or "",
                path=deps.scripts_path,
                sessions=sessions,
                limit=limit_value,
                store=store,
                public=deps.public_scripts,
                learned_aliases=deps.script_aliases,
            )
            qualities = {item.get("match_quality") for item in items}
            warnings = _candidate_warnings(items)
            if "loose" in qualities and items and any(
                quality in {"learned", "suggested"} for quality in qualities
            ):
                warnings.append(
                    "结果里既有「查询里含某个短剧名」的弱匹配，也有另外的候选；"
                    "弱匹配不等于店长要找的那个本。"
                )
            if not items:
                warnings.append(
                    "本地剧本库和场次历史都没有匹配。可以联想候选，"
                    "但必须先让店长确认全名，确认后再 upsert；不要直接写入猜测。"
                )
            return _ok(
                {"query": str(query or ""), "count": len(items), "candidates": items},
                warnings,
            )

        if action == "get":
            key = str(script_id or query or "").strip()
            if not key:
                return _err("bad_request", "get 需要 script_id（也可用 query 传名称）。")
            item = get_script(
                key, path=deps.scripts_path, store=store, public=deps.public_scripts
            )
            if item is None:
                suggestions = find_scripts(
                    key,
                    path=deps.scripts_path,
                    sessions=load_sessions(deps.sessions_path, store=store),
                    limit=5,
                    store=store,
                    public=deps.public_scripts,
                    learned_aliases=deps.script_aliases,
                )
                # 这些是候选，不是档案。不带标记地塞进 data 里，模型会把
                # script_id / read_only / profile_status 当成已确认记录。
                warnings = _candidate_warnings(suggestions)
                return _err(
                    "script_not_found",
                    f"剧本库没有找到 {key}。",
                    data={"suggestions": suggestions},
                    warnings=warnings,
                )
            return _ok({"script": item})

        if action == "upsert":
            if deps.read_only:
                return _err(
                    "read_only",
                    "只读模式：已拒绝 upsert，未修改剧本库。",
                )
            requested_id = str(script_id or "").strip()
            if requested_id.startswith("pub-"):
                return _err(
                    "public_readonly",
                    "公共库条目只读，不能修改。门店需要定制信息时，"
                    "不带 script_id 以同名创建私有档案即可；私有同名条目会覆盖公共条目的显示。",
                )
            try:
                result = upsert_script(
                    script_id=script_id,
                    script_name=script_name,
                    aliases=aliases,
                    script_type=script_type,
                    total_seats=total_seats,
                    duration_minutes=duration_minutes,
                    list_price=list_price,
                    notes=notes,
                    path=deps.scripts_path,
                    now=deps.now(),
                    sessions=load_sessions(deps.sessions_path, store=store),
                    store=store,
                )
            except (TypeError, ValueError) as exc:
                return _err("bad_request", str(exc))
            warnings = result.pop("warnings", [])
            warnings.extend(_public_shadow_warnings(deps, result.get("script_name")))
            return _ok(result, warnings)

        if action == "list":
            items = list_scripts(
                path=deps.scripts_path,
                limit=limit_value,
                store=store,
                public=deps.public_scripts,
            )
            return _ok({"count": len(items), "scripts": items})

        if action == "remember":
            if deps.read_only:
                return _err(
                    "read_only",
                    "只读模式：已拒绝 remember，未写入剧名记忆。",
                )
            memory = deps.script_aliases
            if memory is None:
                return _err(
                    "alias_memory_unavailable",
                    "剧名记忆未启用，本次未记录（不影响检索，也不会报错）。",
                )
            public = deps.public_scripts
            if public is None or getattr(public, "empty", True):
                return _err(
                    "public_library_unavailable",
                    "公共剧本库不可用，暂不能记录剧名映射。",
                )
            # 按名解析要读这两列；缺列时应当报「库不可用」，而不是让
            # pandas 抛 KeyError 冒到 ReAct 循环里。
            if "script_id" not in public.columns or "script_name" not in public.columns:
                return _err(
                    "public_library_unavailable",
                    "公共剧本库缺少必要列，暂不能记录剧名映射。",
                )

            spoken = str(query or "").strip()
            if not spoken:
                return _err(
                    "bad_request",
                    "remember 需要 query：店长最初说的那个说法。",
                )

            target = str(script_id or "").strip()
            if not target:
                # 模型手里通常是名字不是 pub id。允许按名解析，但必须唯一；
                # 同名多行时让它先 search，避免把映射记到错的那一条。
                from app.tools.scripts import normalize_script_name

                wanted = normalize_script_name(script_name).casefold()
                if not wanted:
                    return _err(
                        "bad_request", "remember 需要 script_id 或 script_name。"
                    )
                ids = public["script_id"].astype("string").fillna("").tolist()
                names = public["script_name"].tolist()
                matches = [
                    str(sid)
                    for sid, name in zip(ids, names)
                    if normalize_script_name(name).casefold() == wanted
                ]
                if len(matches) != 1:
                    return _err(
                        "script_not_public",
                        "公共库里找不到唯一的同名剧本，请先 search 确认 script_id。",
                    )
                target = matches[0]

            from app.store.script_aliases import AliasRejected, AliasUnavailable

            try:
                result = memory.remember(
                    spoken, target, public_frame=deps.public_scripts
                )
            except AliasRejected as exc:
                return _err(exc.code, exc.message)
            except AliasUnavailable as exc:
                return _err("alias_memory_unavailable", str(exc))

            if result.get("created"):
                warnings = [
                    "已记录为全站共享的剧名映射：只含剧名，不含对话、"
                    "报价、排期或客户信息。"
                ]
            else:
                # 先写者胜。绝不透露「别的门店已经映射过」——那本身就是
                # 跨租户推断通道。
                warnings = ["这个说法此前已有记录，本次未改动。"]
            return _ok(result, warnings)

        return _err(
            "bad_request",
            f"未知 action：{action}。可用值：search / get / upsert / remember / list。",
        )

    # ------------------------------------------------------------------
    # 工具 9：门店资源（房间 / DM / 班次）
    # ------------------------------------------------------------------
    def _resource_snapshot() -> dict[str, Any]:
        rooms = load_rooms(store=store)
        dms = load_dms(store=store)
        shifts = load_dm_shifts(store=store)
        scripts = load_scripts(deps.scripts_path, store=store)
        stages = store.load_script_stages()
        sessions = load_sessions_with_counts(store=store)
        checklist = setup_checklist(
            rooms=rooms,
            dms=dms,
            shifts=shifts,
            scripts=scripts,
            stages=stages,
            sessions=sessions,
        )
        by_dm: dict[str, list[dict[str, Any]]] = {}
        for row in shifts.to_dict("records"):
            item = shifts_to_dict(row)
            by_dm.setdefault(item["dm_id"], []).append(
                {
                    "weekday": item["weekday"],
                    "start_minute": item["start_minute"],
                    "end_minute": item["end_minute"],
                    "active": item["active"],
                }
            )
        dm_rows = []
        for row in dms.to_dict("records"):
            item = dms_to_dict(row)
            item["shifts"] = sorted(
                by_dm.get(str(item.get("dm_id") or ""), []),
                key=lambda value: (
                    value["weekday"] if value["weekday"] is not None else 0,
                    value["start_minute"] if value["start_minute"] is not None else 0,
                ),
            )
            dm_rows.append(item)
        return {
            "counts": {
                "rooms": int(len(rooms)),
                "dms": int(len(dms)),
                "sessions": int(len(sessions)),
            },
            "rooms": [rooms_to_dict(row) for row in rooms.to_dict("records")],
            "dms": dm_rows,
            "setup": checklist,
        }

    @tool("manage_resources")
    @_safe_tool
    def _resource_tool(
        action: str,
        room: dict[str, Any] | None = None,
        dm: dict[str, Any] | None = None,
        shifts: list[dict[str, Any]] | None = None,
        confirm: bool = False,
    ) -> str:
        """维护门店的房间、DM 与班次，是自动排期的基础。

        action 取值：
          setup_status  返回房间数、DM 数、容量/技能/班次/模板缺口。无参数。
          list_rooms    列出房间。无参数。
          upsert_room   新增或更新房间。参数：room（对象）：
                        name、room_id（更新时带）、capacity、buffer_minutes、
                        tags、hourly_cost、active、notes。
          list_dms      列出 DM 与班次。无参数。
          upsert_dm     新增或更新 DM。参数：dm（对象）：
                        name、dm_id（更新时带）、skills（如「推理/情感」）、
                        hourly_cost、max_sessions_per_day、active、notes。
          set_shifts    替换某个 DM 的班次。参数：dm_id 放在 dm 对象里，
                        shifts 数组，每项 {weekday: 0-6 或「周一」, start: "12:00",
                        end: "23:30"}。跨夜请拆成两段。
          seed_demo     资源为空时写入演示房间/DM/班次。参数：confirm=true。

        容量缺失时仍可保存，但自动排期不会猜容量；
        技能缺失按「未配置」放行并提示；班次缺失按全天可用并提示。
        只读模式下所有写 action 返回 error_code=read_only。
        """
        normalized = str(action or "").strip()
        if normalized in {"setup_status", "list_rooms", "list_dms"}:
            snapshot = _resource_snapshot()
            if normalized == "setup_status":
                return _ok(
                    {
                        "counts": snapshot["counts"],
                        "setup": snapshot["setup"],
                        "rooms": snapshot["rooms"],
                        "dms": snapshot["dms"],
                    }
                )
            if normalized == "list_rooms":
                return _ok({"count": len(snapshot["rooms"]), "rooms": snapshot["rooms"]})
            return _ok({"count": len(snapshot["dms"]), "dms": snapshot["dms"]})

        if normalized in {"upsert_room", "upsert_dm", "set_shifts", "seed_demo"}:
            if deps.read_only:
                return _err(
                    "read_only",
                    f"只读模式：已拒绝 {normalized}，未修改资源。",
                )

        if normalized == "upsert_room":
            if not isinstance(room, dict) or not str(room.get("name") or "").strip():
                return _err("bad_request", "upsert_room 需要 room.name。")
            try:
                _, warnings = upsert_rooms([room], store=store, now=deps.now())
            except (TypeError, ValueError) as exc:
                return _err("bad_request", str(exc))
            return _ok(_resource_snapshot(), warnings)

        if normalized == "upsert_dm":
            if not isinstance(dm, dict) or not str(dm.get("name") or "").strip():
                return _err("bad_request", "upsert_dm 需要 dm.name。")
            try:
                _, warnings = upsert_dms([dm], store=store, now=deps.now())
            except (TypeError, ValueError) as exc:
                return _err("bad_request", str(exc))
            return _ok(_resource_snapshot(), warnings)

        if normalized == "set_shifts":
            target = str((dm or {}).get("dm_id") or (dm or {}).get("name") or "").strip()
            if not target:
                return _err("bad_request", "set_shifts 需要在 dm 对象里提供 dm_id。")
            if not isinstance(shifts, list):
                return _err("bad_request", "set_shifts 需要 shifts 数组。")
            try:
                replace_dm_shifts(target, shifts, store=store, now=deps.now())
            except (TypeError, ValueError) as exc:
                return _err("invalid_shift", str(exc))
            return _ok(_resource_snapshot())

        if normalized == "seed_demo":
            if not confirm:
                return _err("confirm_required", "载入演示资源需要 confirm=true。")
            result = seed_demo_resource_rows(
                store,
                now=deps.now(),
                only_if_empty=True,
                assign=True,
            )
            return _ok(result, result.get("warnings", []))

        return _err(
            "bad_request",
            f"未知 action：{action}。可用值：setup_status / list_rooms / "
            "upsert_room / list_dms / upsert_dm / set_shifts / seed_demo。",
        )

    # ------------------------------------------------------------------
    # 工具 10：排期
    # ------------------------------------------------------------------
    @tool("schedule_sessions")
    @_safe_tool
    def _schedule_tool(
        action: str,
        session_ids: list[str] | None = None,
        start_from: str | None = None,
        start_to: str | None = None,
        session_id: str | None = None,
        choice: str | None = None,
        target_id: str | None = None,
        confirm: bool = False,
    ) -> str:
        """读取或生成房间 / DM 排期；不改开场时间、不合并场次。
        取消场次只有一个入口 cancel_session，且必须店长明确要求（confirm=true）。

        action 取值：
          list        列出未来场次及当前分配。参数：start_from, start_to。
          plan        生成优化方案，只读。参数：start_from, start_to, session_ids。
                      返回 assignments / shortages / warnings / optimal /
                      search_truncated。optimal=false 时不要说「最优」。
          auto_assign 对未分配场次自动分配。参数：session_ids（可空）。
                      无冲突直接写库；冲突时场次保持已保存，返回 conflicts 和选项。
                      不会覆盖已有手工分配。
          apply       店长明确确认后，对指定场次重新执行自动分配。
                      参数：session_ids（必填）、confirm=true。
          resolve     处理冲突。参数：session_id、choice、target_id。
                      choice: switch_room / switch_dm / shift_time /
                      cancel_or_waitlist / add_resource / keep_unassigned。
                      只有换房/换 DM 会写库；改期/取消只返回需人工确认的下一步。
          cancel_session 店长明确说「撤销 / 取消这场」时取消场次。
                      参数：session_id（必填）、confirm=true（必填）。
                      只把 status 改成「已取消」，不删数据；缺 confirm 拒绝。

        房间和 DM 永远由系统分配，不要向店长索要 room_id / dm_id。
        """
        normalized = str(action or "").strip()
        if normalized == "list":
            return _ok(
                list_schedule(
                    store,
                    start_from=start_from,
                    start_to=start_to,
                    now=deps.now(),
                )
            )

        if normalized == "plan":
            context = load_schedule_context(store)
            leave_rows, familiarities = schedule_dm_inputs(store=store)
            result = build_schedule(
                sessions=context.sessions,
                rooms=context.rooms,
                dms=context.dms,
                shifts=context.shifts,
                assignments=context.assignments,
                scripts=context.scripts,
                public=deps.public_scripts,
                now=deps.now(),
                start_from=start_from,
                start_to=start_to,
                target_session_ids=session_ids or None,
                lock_existing=True,
                dm_leaves=leave_rows,
                familiarities=familiarities,
            )
            return _ok(result.to_dict(), result.warnings)

        if normalized in {"auto_assign", "apply", "resolve", "cancel_session"} and deps.read_only:
            return _err(
                "read_only",
                f"只读模式：已拒绝 {normalized}，未修改排期。",
            )

        if normalized == "auto_assign":
            result = auto_assign_sessions(
                store,
                session_ids=session_ids or None,
                now=deps.now(),
                trigger="agent_auto_assign",
            )
            return _ok(result, result.get("warnings", []))

        if normalized == "apply":
            if not confirm:
                return _err("confirm_required", "应用排期方案需要 confirm=true。")
            ids = [str(item) for item in (session_ids or []) if str(item).strip()]
            if not ids:
                return _err("bad_request", "apply 需要明确的 session_ids。")
            result = auto_assign_sessions(
                store,
                session_ids=ids,
                now=deps.now(),
                trigger="agent_apply",
            )
            return _ok(result, result.get("warnings", []))

        if normalized == "resolve":
            sid = str(session_id or "").strip()
            if not sid or not str(choice or "").strip():
                return _err("bad_request", "resolve 需要 session_id 与 choice。")
            try:
                result = resolve_schedule_conflict(
                    store,
                    session_id=sid,
                    choice=str(choice),
                    target_id=target_id,
                    now=deps.now(),
                )
            except ValueError as exc:
                return _err(getattr(exc, "code", None) or "schedule_conflict", str(exc))
            return _ok(result, result.get("warnings", []))

        if normalized == "cancel_session":
            sid = str(session_id or "").strip()
            if not sid:
                return _err("bad_request", "cancel_session 需要 session_id。")
            if not confirm:
                return _err(
                    "confirm_required",
                    "取消场次需要店长明确确认（confirm=true）。",
                )
            try:
                result = update_session(
                    session_id=sid,
                    fields={"status": "已取消"},
                    path=deps.sessions_path,
                    store=store,
                    now=deps.now(),
                )
            except ValueError as exc:
                return _err(getattr(exc, "code", None) or "bad_request", str(exc))
            warnings = list(result.pop("warnings", []) or [])
            return _ok(result, warnings)

        return _err(
            "bad_request",
            f"未知 action：{action}。可用值：list / plan / auto_assign / apply / "
            "resolve / cancel_session。",
        )

    # ------------------------------------------------------------------
    # 工具 11：DM 场控（只读）
    # ------------------------------------------------------------------
    @tool("dm_control")
    @_safe_tool
    def _dm_tool(
        action: str = "status",
        run_id: str | None = None,
        day: str | None = None,
    ) -> str:
        """只读查询 DM 场控进度。没有推进、暂停、结束或改模式的能力。

        action 取值：
          status     查某场控（run_id）或当前进行中的场控。参数：run_id（可空）。
                     返回当前阶段、序号、剩余/超时、暂停、模式、硬件是否降级。
                     不返回 DM 专用 cue_text，也不返回玩家信息。
          list_today 列出某天（默认今天）的场控。参数：day（YYYY-MM-DD，可空）。
          report     完成后的基础汇总。参数：run_id。进行中返回 run_active。

        店主问「演到哪 / 有没有超时 / 灯光怎么没反应」时使用。
        只播报状态；绝不建议或执行「到点自动下一幕」。阶段只能由 DM 手动推进。
        """
        normalized = str(action or "status").strip()
        if normalized == "status":
            target = str(run_id or "").strip()
            if target:
                try:
                    return _ok(get_dm_run(store, target, for_dm=False, now=deps.now()))
                except DMControlError as exc:
                    return _err(exc.code, str(exc))
            return _ok(
                list_runs_today(store, day=day or deps.now().date(), now=deps.now())
            )

        if normalized == "list_today":
            return _ok(
                list_runs_today(store, day=day or deps.now().date(), now=deps.now())
            )

        if normalized == "report":
            target = str(run_id or "").strip()
            if not target:
                return _err("bad_request", "report 需要 run_id。")
            try:
                return _ok(build_run_report(store, run_id=target, now=deps.now()))
            except DMControlError as exc:
                return _err(exc.code, str(exc))

        return _err(
            "bad_request",
            f"未知 action：{action}。dm_control 只支持 status / list_today / report。",
        )

    # ------------------------------------------------------------------
    # 工具 12：联网搜索（web_search，三层成本闸门）
    # ------------------------------------------------------------------
    @tool("web_search")
    @_safe_tool
    def _web_search_tool(query: str) -> str:
        """查询剧本相关的公开网络信息（发行信息 / 类型 / 测评动态）。

        何时使用：
          - 本地 script_library（含公共库）没有命中，且店长在问新剧本的
            公开信息（类型 / 人数 / 发行 / 评价动态）时。

        硬约束：
          - 仅查公开剧本 / 行业信息；结果必须向店长标注「网络信息非官方」。
          - 绝不根据搜索结果自动写入剧本库；店长确认全名后走 script_library。
          - 禁查玩家隐私、商家后台、微信相关内容。
          - 每次最多 3 条结果。未配置或超出每日配额时如实转述错误，
            不伪造结果、不重试超过一次。

        参数：
          query  查询词，2–120 字符；须与剧本 / 剧本杀行业相关。

        返回：
          results（title / url / snippet，snippet 为节选）、provider、cached。
          网络信息只作参考，引用时必须告知店长来源与「非官方」限定。
        """
        from app.config import DATA_DIR
        from app.integrations.web_search import SearchError, build_search_usage, run_web_search

        usage = deps.search_usage
        if usage is None:
            # CLI / 未接线场景：Excel sidecar 计数器，配额闸门不旁路。
            usage = build_search_usage(uses_database=False, data_dir=DATA_DIR)
        try:
            payload = run_web_search(
                str(query or ""),
                provider=deps.web_search_provider,
                usage=usage,  # 必须传回退后的 usage：传 deps.search_usage 会把闸门旁路回去
                tenant_id=deps.tenant_id or "default",
                day=deps.now().strftime("%Y-%m-%d"),
            )
        except SearchError as exc:
            return _err(exc.code, exc.message)
        warnings = ["网络信息非官方，录入剧本库前请核对全名与关键信息。"]
        return _ok(payload, warnings)

    return [
        _extract_tool,
        _dedupe_tool,
        _filter_tool,
        _assess_tool,
        _pricing_tool,
        _store_tool,
        _copy_tool,
        _script_tool,
        _resource_tool,
        _schedule_tool,
        _dm_tool,
        _web_search_tool,
    ]


# ===========================================================================
# 三、系统提示词
# ===========================================================================

SYSTEM_PROMPT = """你是剧本杀门店的私域运营助手，服务对象是店长，不是玩家。

【总规则】
1. 所有场次、预约、价格、风险事实必须来自工具返回。不要凭记忆或猜测补全。
2. 写操作（upsert_sessions / record_booking / cancel_booking / script_library upsert /
   script_library remember / store_data 的建场与修正 / manage_resources / schedule_sessions 写 action）
   只在店长明确要求录入、预约、跳车、加剧本、补资源或确认排期时执行。
   店长转发拼场广告，视为要求录入；转发预约 / 跳车消息，视为要求更新预约。
   追问时先问最关键的一项，不要一次逼店长补完所有字段。
   remember 例外地只在「店长已经确认过全名」之后才允许调用，见第 9 条。
3. 工具返回 ok=false 时，如实转述 error_code 和 error，不要假装成功；同一个失败不要连续重试超过一次。
4. 不编造场次、玩家、价格、地址、联系方式、赠品、活动或「保证成团」。
5. 金额、时间、人数以工具结果为准。不要自己计算折扣、改价或补人数。
6. 内部财务指标（floor_price / break_even_price / sunk_cost / profit_if_sold_out /
   contribution_per_seat）只能给店长做经营分析，展示时必须标注「内部参考」。
   对外营销文案只能通过 write_marketing_copy 生成，不得把内部指标写进文案。
7. 不自动发送微信、不自动发布、不做玩家直连。文案交给店长人工复核。
8. 回复用中文，简洁；多场用列表或表格；结尾给下一步建议。
9. 剧本全名是事实，不是常识。你可以在店长说缩写时提出候选，但必须标注「我猜是」
   并等店长确认；确认前不得调用 script_library(upsert) 写入猜测的名字。
   本地 search 没有结果，也要明确说「本地没查到」，不要伪造搜索结果。
   场次录入的唯一候选匹配（script_match=unique）来自剧本库，不算猜测，可直接建场，
   但要把规范全名回读给店长核对。
   店长确认的全名若属于公共库（script_id 以 pub- 开头），确认之后再调一次
   script_library(action="remember", query=店长最初用的说法, script_id=…)。
   这一步只是把「他这么拼 → 这个本」记下来供全站检索，不写档案、不写场次；
   未确认前不许调用，也不要拿它当模糊搜索使用。
10. 场次录入允许信息不完整。硬必填只有剧本全名和开场时间；
    这两项齐全就直接建场，不要求人数 / 价格 / 类型 / 时长；
    缺任意一项硬必填时只追问最关键的一项，不落任何数据，也不要把缺失项一次列完。
11. 一次只问一个硬必填；已经问过的不要重复问。
    店长说「先这样 / 回头再说」时就不建场，等店长补齐后再录。
12. 房间和 DM 永远不由店长输入；建场后由系统自动分配。
    已保存场次改剧本 / 时间必须先确认，再走 store_data(action="update_session")，
    不得用 upsert_sessions 直接产生第二条场次。
13. 网络信息只作参考：web_search 结果引用时必须标注「网络信息非官方」；
    绝不根据搜索结果自动写入剧本库——店长确认全名后才走 script_library；
    禁查玩家隐私、商家后台与微信内容。未配置或超配额时如实转述，不伪造结果。

【场景路由】
A. 店长转发拼场广告：
   1) extract_message 抽取；
   2) dedupe_sessions 对照现有表，说明会新增还是更新；
   3) store_data(action="upsert_sessions") 写入；返回自带 auto_assign；
   4) assess_risk + suggest_pricing 给出高危场次与折扣建议。

B. 店长转发玩家预约：
   1) extract_message 抽取；
   2) 用 bookings 里的 candidate_session_id 调 store_data(action="get_session")；
   3) 查不到再调 store_data(action="query_sessions") 按门店 / 剧本 / 时间找；
      仍不唯一或找不到，先问店长，不要猜；
   4) store_data(action="record_booking") 写入，并报告最新剩余空位。
      如果写入后剩余为 0，说明已满员，不再建议折扣。

C. 店长转发玩家跳车：
   1) extract_message 抽取；
   2) 定位 session_id 与玩家昵称；
   3) store_data(action="cancel_booking") 取消；
   4) 重新 assess_risk + suggest_pricing，报告空位与策略变化。

D. 店长要求写推广文案：
   1) 只调 write_marketing_copy；
   2) 原样给出 community 与 moments，不要自己改写或续写；
   3) 如果工具返回 internal_leak / price_mismatch / no_sessions 等失败，
      说明原因，不要输出任何替代文案；
   4) 提醒店长人工复核后再复制发布。

E. 店长说剧本缩写 / 要加剧本：
   1) 先 script_library(action="search", query=店长说的缩写或名称)；
   2) 有本地匹配：列出全名、别名、当前缺失字段，问店长选哪个；
      店长确认后，用返回的 script_id 调 script_library(action="upsert") 补全。
      若这条是公共库条目（script_id 以 pub- 开头），再补一次
      script_library(action="remember", query=店长最初的说法, script_id=…)。
   3) 本地没有匹配：可以说「我猜是《…》」，但必须标明是猜测并等店长确认；
      店长确认全名后，再 upsert；店长否定时，问正确全名，不要硬写。
      确认的是公共库条目时同样补一次 remember。
      若 search 返回的是 match_quality=suggested 的候选，要明确说「这是按字面
      相近猜的」，不得直接当成已确认结果；确认后 remember 才有意义。
   4) 店长要加剧本时，即使只说了全名或缩写，也先把已知信息 upsert 成草稿；
      然后根据返回的 missing_fields 只问一个最关键的问题。
      后续每轮都带上 script_id 继续补，不要把已保存字段再问一遍。
   5) 剧本档案和场次排期是两件事：档案只描述剧本本身；
      要加具体场次，走 G 的增量录入。

F. 资源初始化：
   先 manage_resources(action="setup_status")，按缺口逐项询问；
   不要一次要求店长补完所有字段。
   房间缺容量时不要猜容量；DM 缺技能/班次时可以继续排，但要把 warning 告诉店长。
   店长没有整理资源时，可以提议 manage_resources(action="seed_demo", confirm=true)，
   但必须说明是演示数据、只在资源为空时写入。

G. 场次录入与排班：
   录入场次时不要求店长提供房间或 DM，也不要求一次说全。
   店长只是问行情 / 问空位 / 比价时，不要建场；明确要记录时才写。
   1) 先用 extract_message；缺人数 / 价格 / 类型 / 时长都不算失败。
   2) 调 store_data(action="create_session")；对话上下文里已有的信息直接带上，
      不要重复问。
   3) status=incomplete：只问 suggested_question 里的那一项；不要一次列出
      全部缺失项；信息不全时不写任何数据（不落草稿）。
   4) status=needs_confirmation：把候选 / 歧义说清楚，等店长确认；确认前不建场。
      本地完全没匹配、且店长说的像缩写时，先问全名；店长确认是全新剧本时照常建场，
      并提示之后可到 /resources 或 script_library 补档案。
      extract_message 返回 start_time_source="模型推断"（「明晚 / 周六」等相对时间）时，
      硬必填齐全可以直接建场，但回复必须回读绝对日期、时间和星期
      （例如「已保存：10 月 21 日 周二 20:00 开场」）并请店长核对；
      仅剧本名多候选（status=needs_confirmation）时才等确认。
   5) status=promoted：报告「已保存 + 自动排到 X 房 / Y DM」；
      只有返回 conflicts 时，才列可选动作（换房 / 换 DM / 改期 / 取消 / 先不排），
      等店长选择后再调 schedule_sessions(action="resolve")。
      若原因是 missing_total_seats，只问「这场几个人」，不要同时问价格 / 类型。
   6) 店长补充或纠正后重新调 create_session（带上对话里已有的字段）；
      已保存场次改门店 / 剧本 / 时间，先确认，再 update_session。
      不要用 upsert_sessions 硬造第二条。
   7) 店长明确要求整店重排时，才用 schedule_sessions(action="plan")，
      再用 apply + confirm=true + 明确 session_ids。
   8) 店长说「撤销 / 取消这场」时：session_id 从本轮上下文取；
      没有明确 id 先 query_sessions 问是哪一场，绝不猜。
      确认后调 schedule_sessions(action="cancel_session", session_id, confirm=true)；
      这是店长的显式动作，不违反「不自动取消」边界。
   永远不要自动修改场次开场时间、合并场次或取消场次。

H. 资源不足 / 排不开：
   用 schedule_sessions(action="list" / "plan" / "auto_assign") 返回的
   shortages / conflicts / booked_revenue_at_risk 说明影响。
   换房 / 换 DM 可在店长选择后调 resolve 应用；
   改时间 / 取消 / 候补只给建议，必须店长确认后人工处理。
   optimal=false 时不要说「最优方案」，要说明搜索被截断。

I. 大众点评：
   本系统没有实时 API；导入由 UI 完成。
   必须说明 data_as_of，不得声称实时同步；
   不得建议抓取或登录商家后台。快速记单可用 /imports 的 +N，
   聊天里只处理店长明确给出的预约 / 跳车。

K. DM 场控查询：
   店长问「现在演到哪 / 有没有超时 / 灯光怎么没反应」时，
   用 dm_control(action="status") 或 status(run_id) 读状态；
   只播报，不推进、不暂停、不结束。
   绝不建议「到点自动下一幕」；阶段只能由 DM 在 DM 端手动推进。
   DM 提示词 / cue_text 不在店主聊天里展开；硬件失败只说「已降级为文字提示」。
   店主只能看场控和生成 / 撤销场控码，不能远程控制。

N. 新公开信息 / 网络查询：
   本地 script_library（含公共库）没有命中，且店长在问新剧本的公开信息时，
   用 web_search 查一次；结果标注「网络信息非官方」并给出来源链接。
   绝不根据搜索结果直接 upsert 剧本；店长确认全名后走 script_library。
   返回 not_configured：如实说「未配置网络搜索」，引导用本地剧本库；
   返回 quota_exceeded：如实转述每日次数限制，不要重试。

【工具选择】
- 只想知道哪些场过期 / 取消：filter_expired。
- 想看预约人：store_data(action="list_bookings")。
- 想看数据是否有孤儿预约：store_data(action="find_orphan_bookings")。
- 不确定场次是否存在：先 store_data(action="query_sessions") / get_session，再写。
- 剧本全名 / 缩写 / 类型 / 几人本 / 时长：script_library（含公共库）。
- 新剧本公开信息 / 行业动态（本地库没有时）：web_search。
- 房间 / DM / 班次 / 资源缺口：manage_resources。
- 排期、自动分配、冲突处理：schedule_sessions。
- 场控进度（只读）：dm_control。
"""


# ===========================================================================
# 四、Agent 组装与单轮执行
# ===========================================================================

def build_agent(
    *,
    model: Any = None,
    deps: AgentDeps | None = None,
    checkpointer: Any = None,
    extra_system_prompt: str = "",
):
    """组装 ReAct Agent。

    model=None 时才初始化真实模型（会读 .env）；测试全部注入脚本模型。
    checkpointer 默认 InMemorySaver，thread_id 决定多轮记忆的隔离边界。
    extra_system_prompt 只追加、不替换基础提示词，避免覆盖安全约束。
    """
    deps = deps or AgentDeps()
    tools = build_store_tools(deps)
    if model is None:
        model = llm.get_default_model(temperature=0.0)
    if checkpointer is None:
        checkpointer = InMemorySaver()

    prompt = SYSTEM_PROMPT
    extra = extra_system_prompt.strip()
    if extra:
        prompt = f"{SYSTEM_PROMPT}\n\n{extra}"

    return create_agent(
        model=model,
        tools=tools,
        system_prompt=prompt,
        checkpointer=checkpointer,
    )


def run_turn(
    agent: Any,
    text: str,
    *,
    thread_id: str = "store-manager",
    config: dict[str, Any] | None = None,
) -> AgentTurn:
    """执行一轮对话，返回最终答复与本轮新增的工具轨迹。

    thread_id 相同 = 同一段记忆；换一个 thread_id = 全新会话。
    """
    if not isinstance(text, str) or not text.strip():
        raise ValueError("text 不能为空")

    cfg = config or {"configurable": {"thread_id": thread_id}}
    before = len(agent.get_state(cfg).values.get("messages", []))
    result = agent.invoke(
        {"messages": [{"role": "user", "content": text}]},
        cfg,
    )
    messages = result.get("messages", [])
    new_messages = messages[before:]

    calls: list[dict[str, Any]] = []
    results: list[dict[str, Any]] = []
    for message in new_messages:
        for call in getattr(message, "tool_calls", None) or []:
            calls.append({
                "name": call.get("name"),
                "args": _plain(call.get("args") or {}),
            })
        if message.__class__.__name__ == "ToolMessage":
            results.append({
                "name": getattr(message, "name", None),
                "content": _plain(getattr(message, "content", "")),
            })

    answer = llm.message_text(messages[-1]) if messages else ""
    return AgentTurn(
        answer=answer,
        tool_calls=tuple(calls),
        tool_results=tuple(results),
        messages=tuple(new_messages),
    )

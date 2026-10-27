"""第 9 步 ReAct Agent —— 单元测试。

运行：
  PYTHONUTF8=1 "C:/Users/Lenovo/anaconda3/python.exe" -m pytest tests/test_agent.py -v

全部离线：
  - 不联网、不需要 API key；
  - 文本抽取与文案生成注入 FakeLLM；
  - Agent 图用脚本模型驱动，不调用真实 DeepSeek；
  - 数据文件全部在 pytest 的 tmp_path 下，不碰真实 data/。

核心回归：
  1. 12 个工具都返回可解析 JSON，业务失败不抛异常。
  2. 工具返回里没有 source_text / 内部实现字段。
  3. 脚本模型能跑通「工具调用 -> ToolMessage 回填 -> 最终答复」。
  4. 同一 thread_id 有记忆，不同 thread_id 隔离。
"""

from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pandas as pd
import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from app.agent import (
    SYSTEM_PROMPT,
    AgentDeps,
    build_agent,
    build_store_tools,
    run_turn,
)
from app.tools.extract import INTENT_AD
from app.tools.storage import (
    BOOKING_ACTIVE,
    STATUS_CANCELLED,
    STATUS_PENDING,
    load_bookings,
    load_sessions,
    load_sessions_with_counts,
    make_session_id,
    upsert_booking,
    upsert_sessions,
)

NOW = pd.Timestamp("2026-10-20 18:00")  # 周二
STORE = "迷雾剧场"
OTHER_STORE = "另一家店"

SOURCE_AD = "【迷雾剧场】年轮 20号19:30 8人本 168/位 已约6人 还差2人"
SOURCE_BOOKING = "迷雾剧场 年轮 20号22:00 王五 约2人"


# ===========================================================================
# 测试基础设施
# ===========================================================================

@pytest.fixture(autouse=True)
def _block_real_model(monkeypatch):
    """任何测试都不许初始化真实模型。"""

    def _boom(*args, **kwargs):
        raise AssertionError("测试中不应初始化真实模型")

    monkeypatch.setattr("app.config.get_model", _boom)


class FakeLLM:
    """只实现 .invoke(messages)，模拟工具 1 / 工具 7 的内部模型。"""

    def __init__(self, content: str = "", *, error: Exception | None = None):
        self.content = content
        self.error = error
        self.calls: list[list[dict[str, str]]] = []

    def invoke(self, messages):
        self.calls.append(messages)
        if self.error is not None:
            raise self.error
        return SimpleNamespace(content=self.content)


class ScriptedChatModel(BaseChatModel):
    """按脚本依次返回 AIMessage 的假 chat model。

    只用于 Agent 图测试：不支持真实推理，只负责让 create_agent 走完
    「模型要求调工具 -> 工具执行 -> 模型看到 ToolMessage -> 最终答复」。
    """

    responses: list[Any]
    index: int = 0

    @property
    def _llm_type(self) -> str:
        return "scripted-test-model"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        message = self.responses[min(self.index, len(self.responses) - 1)]
        self.index += 1
        return ChatResult(generations=[ChatGeneration(message=message)])


@pytest.fixture
def make_deps(tmp_path):
    """构造指向临时 Excel 的依赖。"""

    def _make(**kwargs):
        base = AgentDeps(
            sessions_path=tmp_path / "sessions.xlsx",
            bookings_path=tmp_path / "bookings.xlsx",
            scripts_path=tmp_path / "scripts.xlsx",
            now=lambda: NOW,
        )
        return replace(base, **kwargs)

    return _make


def _tools(deps: AgentDeps) -> dict[str, Any]:
    return {item.name: item for item in build_store_tools(deps)}


def _call(tool, args: dict[str, Any]) -> dict[str, Any]:
    payload = tool.invoke(args)
    assert isinstance(payload, str)
    return json.loads(payload)


def _sid(script: str, start: str, store: str = STORE) -> str:
    return make_session_id(store, script, start)


def _seed(deps: AgentDeps, *, extra: list[dict] | None = None) -> pd.DataFrame:
    """三场基准数据：雾隐村高危、年轮关注、漓川普通。"""
    rows = [
        {
            "store_name": STORE,
            "script_name": "雾隐村",
            "start_time": "2026-10-20 20:00",
            "total_seats": 6,
            "list_price": 168,
            "status": STATUS_PENDING,
        },
        {
            "store_name": STORE,
            "script_name": "年轮",
            "start_time": "2026-10-20 22:00",
            "total_seats": 8,
            "list_price": 168,
            "status": STATUS_PENDING,
        },
        {
            "store_name": STORE,
            "script_name": "漓川怪谈簿",
            "start_time": "2026-10-21 19:00",
            "total_seats": 6,
            "list_price": 168,
            "status": STATUS_PENDING,
        },
    ]
    if extra:
        rows.extend(extra)
    upsert_sessions(rows, path=deps.sessions_path)
    _book(deps, _sid("雾隐村", "2026-10-20 20:00"), "张三", 5)
    _book(deps, _sid("年轮", "2026-10-20 22:00"), "李四", 2)
    return load_sessions(deps.sessions_path)


def _book(deps: AgentDeps, session_id: str, name: str, seats: int) -> None:
    upsert_booking(
        session_id,
        name,
        seats=seats,
        path=deps.bookings_path,
        now=NOW,
    )


def _extract_payload(
    *,
    sessions: list[dict] | None = None,
    bookings: list[dict] | None = None,
    intent: str = "预约",
) -> str:
    return json.dumps(
        {
            "intent": intent,
            "sessions": sessions or [],
            "bookings": bookings or [],
            "warnings": [],
        },
        ensure_ascii=False,
    )


def _copy_payload(community: str, moments: str) -> str:
    return json.dumps(
        {"community": community, "moments": moments, "warnings": []},
        ensure_ascii=False,
    )


def _tool_call(name: str, args: dict[str, Any], call_id: str) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{"name": name, "args": args, "id": call_id}],
    )


# ===========================================================================
# 工具注册
# ===========================================================================

class TestToolRegistry:
    def test_正好十二个工具且顺序稳定(self, make_deps):
        names = [tool.name for tool in build_store_tools(make_deps())]
        assert names == [
            "extract_message",
            "dedupe_sessions",
            "filter_expired",
            "assess_risk",
            "suggest_pricing",
            "store_data",
            "write_marketing_copy",
            "script_library",
            "manage_resources",
            "schedule_sessions",
            "dm_control",
            "web_search",
        ]

    def test_每个工具都有说明(self, make_deps):
        for item in build_store_tools(make_deps()):
            assert item.description
            assert len(item.description) > 20

    def test_工具返回_json_字符串(self, make_deps):
        tools = _tools(make_deps())
        payload = _call(tools["filter_expired"], {})
        assert payload["ok"] is True

    def test_工具参数不暴露文件路径(self, make_deps):
        for item in build_store_tools(make_deps()):
            assert "path" not in item.args
            assert "deps" not in item.args


# ===========================================================================
# 工具 1：文本抽取
# ===========================================================================

class TestExtractTool:
    def test_抽取广告成功(self, make_deps):
        payload = _extract_payload(
            sessions=[{
                "store_name": STORE,
                "script_name": "年轮",
                "script_type": "情感",
                "start_time": "2026-10-20 19:30",
                "start_time_text": "20号19:30",
                "total_seats": 8,
                "list_price": 168,
                "price_text": "168元/位",
                "count_text": "8人本",
                "remaining_reported": 2,
                "booked_reported": 6,
                "source_excerpt": "年轮 20号19:30 8人本 168/位 已约6人 还差2人",
            }],
            intent="广告",
        )
        deps = make_deps(extraction_model=FakeLLM(payload))
        result = _call(_tools(deps)["extract_message"], {"text": SOURCE_AD})
        assert result["ok"] is True
        assert result["data"]["intent"] == INTENT_AD
        assert len(result["data"]["sessions"]) == 1
        assert result["data"]["sessions"][0]["script_name"] == "年轮"

    def test_抽取不写库(self, make_deps):
        deps = make_deps(extraction_model=FakeLLM(_extract_payload()))
        _call(_tools(deps)["extract_message"], {"text": SOURCE_AD})
        assert load_sessions(deps.sessions_path).empty

    def test_空文本结构化失败(self, make_deps):
        deps = make_deps(extraction_model=FakeLLM(_extract_payload()))
        result = _call(_tools(deps)["extract_message"], {"text": "   "})
        assert result["ok"] is False
        assert result["error_code"] == "empty_input"

    def test_模型异常脱敏(self, make_deps):
        deps = make_deps(
            extraction_model=FakeLLM(error=RuntimeError("网络错误 sk-demo123456"))
        )
        result = _call(_tools(deps)["extract_message"], {"text": SOURCE_AD})
        assert result["ok"] is False
        assert result["error_code"] == "llm_call_error"
        assert "sk-demo123456" not in (result["error"] or "")

    def test_默认门店补齐(self, make_deps):
        payload = _extract_payload(
            sessions=[{
                "script_name": "年轮",
                "start_time": "2026-10-20 19:30",
                "start_time_text": "20号19:30",
                "total_seats": 8,
                "list_price": 168,
                "source_excerpt": "年轮 20号19:30",
            }],
            intent="广告",
        )
        deps = make_deps(extraction_model=FakeLLM(payload), default_store=STORE)
        result = _call(
            _tools(deps)["extract_message"],
            {"text": "年轮 20号19:30 8人本 168/位"},
        )
        assert result["ok"] is True
        assert result["data"]["sessions"][0]["store_name"] == STORE


# ===========================================================================
# 工具 2：去重
# ===========================================================================

class TestDedupeTool:
    def test_新增(self, make_deps):
        deps = make_deps()
        _seed(deps)
        records = [{
            "store_name": STORE,
            "script_name": "新本",
            "start_time": "2026-10-22 19:00",
            "total_seats": 6,
            "list_price": 168,
        }]
        result = _call(_tools(deps)["dedupe_sessions"], {"records": records})
        assert result["ok"] is True
        assert result["data"]["count"] == 1
        assert result["data"]["records"][0]["dedupe_action"] == "新增"

    def test_更新(self, make_deps):
        deps = make_deps()
        _seed(deps)
        records = [{
            "store_name": STORE,
            "script_name": "年轮",
            "start_time": "2026-10-20 22:00",
            "list_price": 188,
        }]
        result = _call(_tools(deps)["dedupe_sessions"], {"records": records})
        row = result["data"]["records"][0]
        assert row["dedupe_action"] == "更新"
        assert "list_price" in (row["changed_fields"] or [])

    def test_不比对(self, make_deps):
        deps = make_deps()
        _seed(deps)
        records = [{
            "store_name": STORE,
            "script_name": "年轮",
            "start_time": "2026-10-20 22:00",
            "list_price": 168,
        }]
        result = _call(
            _tools(deps)["dedupe_sessions"],
            {"records": records, "compare_existing": False},
        )
        assert result["data"]["records"][0]["dedupe_action"] == "未比对"

    def test_空数组结构化失败(self, make_deps):
        result = _call(_tools(make_deps())["dedupe_sessions"], {"records": []})
        assert result["ok"] is False
        assert result["error_code"] == "bad_request"

    def test_不写库(self, make_deps):
        deps = make_deps()
        _seed(deps)
        before = len(load_sessions(deps.sessions_path))
        _call(
            _tools(deps)["dedupe_sessions"],
            {"records": [{
                "store_name": STORE,
                "script_name": "新本",
                "start_time": "2026-10-22 19:00",
                "total_seats": 6,
                "list_price": 168,
            }]},
        )
        assert len(load_sessions(deps.sessions_path)) == before


# ===========================================================================
# 工具 3：过期过滤
# ===========================================================================

class TestFilterTool:
    def test_按状态分组(self, make_deps):
        deps = make_deps()
        _seed(deps, extra=[
            {
                "store_name": STORE,
                "script_name": "过期本",
                "start_time": "2026-10-19 19:00",
                "total_seats": 6,
                "list_price": 168,
                "status": STATUS_PENDING,
            },
            {
                "store_name": STORE,
                "script_name": "取消本",
                "start_time": "2026-10-21 19:00",
                "total_seats": 6,
                "list_price": 168,
                "status": STATUS_CANCELLED,
            },
        ])
        result = _call(_tools(deps)["filter_expired"], {})
        counts = result["data"]["counts"]
        assert counts == {"kept": 3, "expired": 1, "cancelled": 1}
        assert result["data"]["expired"][0]["script_name"] == "过期本"

    def test_按门店过滤(self, make_deps):
        deps = make_deps()
        _seed(deps, extra=[{
            "store_name": OTHER_STORE,
            "script_name": "别家本",
            "start_time": "2026-10-21 19:00",
            "total_seats": 6,
            "list_price": 168,
            "status": STATUS_PENDING,
        }])
        result = _call(_tools(deps)["filter_expired"], {"store_name": STORE})
        assert result["data"]["counts"]["kept"] == 3

    def test_空表(self, make_deps):
        result = _call(_tools(make_deps())["filter_expired"], {})
        assert result["ok"] is True
        assert result["data"]["counts"] == {"kept": 0, "expired": 0, "cancelled": 0}
        assert result["warnings"]


# ===========================================================================
# 工具 4：风险评估
# ===========================================================================

class TestAssessTool:
    def test_高危排在最前(self, make_deps):
        deps = make_deps()
        _seed(deps)
        result = _call(_tools(deps)["assess_risk"], {})
        assert result["ok"] is True
        sessions = result["data"]["sessions"]
        assert sessions[0]["script_name"] == "雾隐村"
        assert sessions[0]["priority_level"] == "高危"

    def test_满员归正常(self, make_deps):
        deps = make_deps()
        _seed(deps)
        _book(deps, _sid("雾隐村", "2026-10-20 20:00"), "补位", 1)
        result = _call(_tools(deps)["assess_risk"], {})
        row = next(
            item for item in result["data"]["sessions"]
            if item["script_name"] == "雾隐村"
        )
        assert row["priority_level"] == "正常"
        assert row["fail_risk"] == "低"

    def test_空结果带提示(self, make_deps):
        result = _call(_tools(make_deps())["assess_risk"], {})
        assert result["ok"] is True
        assert result["data"]["count"] == 0
        assert result["warnings"]

    def test_不返回_source_text(self, make_deps):
        deps = make_deps()
        _seed(deps)
        result = _call(_tools(deps)["assess_risk"], {})
        for row in result["data"]["sessions"]:
            assert "source_text" not in row
            assert "updated_at" not in row


# ===========================================================================
# 工具 5：定价
# ===========================================================================

class TestPricingTool:
    def test_返回推荐价与内部指标(self, make_deps):
        deps = make_deps()
        _seed(deps)
        result = _call(_tools(deps)["suggest_pricing"], {})
        assert result["ok"] is True
        row = result["data"]["sessions"][0]
        for field in (
            "recommended_price", "seat_saving", "discount_tier",
            "floor_price", "break_even_price", "sunk_cost",
            "profit_if_sold_out", "contribution_per_seat",
        ):
            assert field in row
        assert row["recommended_price"] <= row["list_price"]

    def test_原价缺失给提示(self, make_deps):
        deps = make_deps()
        upsert_sessions([{
            "store_name": STORE,
            "script_name": "无价本",
            "start_time": "2026-10-21 19:00",
            "total_seats": 6,
            "list_price": None,
            "status": STATUS_PENDING,
        }], path=deps.sessions_path)
        result = _call(_tools(deps)["suggest_pricing"], {})
        assert result["ok"] is True
        assert result["warnings"]
        assert any("原价" in item for item in result["warnings"])

    def test_空结果(self, make_deps):
        result = _call(_tools(make_deps())["suggest_pricing"], {})
        assert result["ok"] is True
        assert result["data"]["count"] == 0


# ===========================================================================
# 工具 6：表格读写
# ===========================================================================

class TestStoreTool:
    def test_查询场次带真实人数(self, make_deps):
        deps = make_deps()
        _seed(deps)
        result = _call(_tools(deps)["store_data"], {"action": "query_sessions"})
        assert result["ok"] is True
        assert result["data"]["count"] == 3
        row = next(
            item for item in result["data"]["sessions"]
            if item["script_name"] == "雾隐村"
        )
        assert row["booked_seats"] == 5
        assert row["remaining_seats"] == 1
        assert row["derived_status"] == "待开场"

    def test_查询_limit(self, make_deps):
        deps = make_deps()
        _seed(deps)
        result = _call(
            _tools(deps)["store_data"],
            {"action": "query_sessions", "limit": 1},
        )
        assert result["data"]["count"] == 1
        assert result["data"]["total"] == 3

    def test_单场查询(self, make_deps):
        deps = make_deps()
        _seed(deps)
        sid = _sid("年轮", "2026-10-20 22:00")
        result = _call(
            _tools(deps)["store_data"],
            {"action": "get_session", "session_id": sid},
        )
        assert result["ok"] is True
        assert result["data"]["session"]["script_name"] == "年轮"

    def test_单场不存在(self, make_deps):
        deps = make_deps()
        _seed(deps)
        result = _call(
            _tools(deps)["store_data"],
            {"action": "get_session", "session_id": "不存在"},
        )
        assert result["ok"] is False
        assert result["error_code"] == "session_not_found"
        assert result["data"]["suggestions"]

    def test_写入场次(self, make_deps):
        deps = make_deps()
        _seed(deps)
        rows = [{
            "store_name": STORE,
            "script_name": "新本",
            "start_time": "2026-10-22 19:00",
            "total_seats": 6,
            "list_price": 168,
        }]
        result = _call(
            _tools(deps)["store_data"],
            {"action": "upsert_sessions", "rows": rows},
        )
        assert result["ok"] is True
        assert result["data"]["actions"].get("新增") == 1
        assert len(load_sessions(deps.sessions_path)) == 4

    def test_重复写入不新增行(self, make_deps):
        deps = make_deps()
        _seed(deps)
        rows = [{
            "store_name": STORE,
            "script_name": "年轮",
            "start_time": "2026-10-20 22:00",
            "list_price": 188,
        }]
        _call(_tools(deps)["store_data"], {"action": "upsert_sessions", "rows": rows})
        _call(_tools(deps)["store_data"], {"action": "upsert_sessions", "rows": rows})
        sessions = load_sessions_with_counts(deps.sessions_path, deps.bookings_path)
        assert len(sessions) == 3
        hit = sessions[sessions["script_name"] == "年轮"].iloc[0]
        assert hit["booked_seats"] == 2  # 预约人数不被文案覆盖

    def test_写入主键缺失(self, make_deps):
        deps = make_deps()
        result = _call(
            _tools(deps)["store_data"],
            {"action": "upsert_sessions", "rows": [{"store_name": STORE}]},
        )
        assert result["ok"] is False
        assert result["error_code"] == "bad_request"

    def test_记录预约(self, make_deps):
        deps = make_deps()
        _seed(deps)
        sid = _sid("漓川怪谈簿", "2026-10-21 19:00")
        result = _call(
            _tools(deps)["store_data"],
            {"action": "record_booking", "session_id": sid,
             "player_name": "王五", "seats": 2},
        )
        assert result["ok"] is True
        assert result["data"]["session"]["booked_seats"] == 2
        assert result["data"]["session"]["remaining_seats"] == 4
        bookings = load_bookings(deps.bookings_path)
        assert (bookings["status"] == BOOKING_ACTIVE).sum() == 3

    def test_预约场次不存在(self, make_deps):
        deps = make_deps()
        _seed(deps)
        result = _call(
            _tools(deps)["store_data"],
            {"action": "record_booking", "session_id": "不存在",
             "player_name": "王五", "seats": 1},
        )
        assert result["ok"] is False
        assert result["error_code"] == "session_not_found"
        bookings = load_bookings(deps.bookings_path)
        assert "不存在" not in set(bookings["session_id"])

    def test_预约人数非法(self, make_deps):
        deps = make_deps()
        _seed(deps)
        result = _call(
            _tools(deps)["store_data"],
            {"action": "record_booking",
             "session_id": _sid("年轮", "2026-10-20 22:00"),
             "player_name": "王五", "seats": 0},
        )
        assert result["ok"] is False
        assert result["error_code"] == "bad_request"

    def test_取消预约(self, make_deps):
        deps = make_deps()
        _seed(deps)
        sid = _sid("雾隐村", "2026-10-20 20:00")
        result = _call(
            _tools(deps)["store_data"],
            {"action": "cancel_booking", "session_id": sid,
             "player_name": "张三"},
        )
        assert result["ok"] is True
        assert result["data"]["session"]["booked_seats"] == 0
        assert result["data"]["session"]["remaining_seats"] == 6

    def test_取消不存在的预约(self, make_deps):
        deps = make_deps()
        _seed(deps)
        result = _call(
            _tools(deps)["store_data"],
            {"action": "cancel_booking",
             "session_id": _sid("年轮", "2026-10-20 22:00"),
             "player_name": "查无此人"},
        )
        assert result["ok"] is False
        assert result["error_code"] == "booking_not_found"
        assert result["data"]["active_bookings"]

    def test_列出预约人(self, make_deps):
        deps = make_deps()
        _seed(deps)
        sid = _sid("年轮", "2026-10-20 22:00")
        result = _call(
            _tools(deps)["store_data"],
            {"action": "list_bookings", "session_id": sid},
        )
        assert result["ok"] is True
        assert result["data"]["count"] == 1
        assert result["data"]["bookings"][0]["player_name"] == "李四"

    def test_孤儿预约(self, make_deps):
        deps = make_deps()
        _seed(deps)
        upsert_booking("不存在的场次", "路人", path=deps.bookings_path, now=NOW)
        result = _call(
            _tools(deps)["store_data"],
            {"action": "find_orphan_bookings"},
        )
        assert result["ok"] is True
        assert result["data"]["count"] == 1

    def test_未知_action(self, make_deps):
        result = _call(
            _tools(make_deps())["store_data"],
            {"action": "drop_table"},
        )
        assert result["ok"] is False
        assert result["error_code"] == "bad_request"


class TestStoreCreateSessionActions:
    """store_data 的增量建场 action（create_session）与已保存场次的修正。"""

    def test_硬必填齐全直接建场并自动排期(self, make_deps):
        deps = make_deps()
        result = _call(
            _tools(deps)["store_data"],
            {
                "action": "create_session",
                "fields": {"script_name": "雾隐山庄", "start_time": "2026-10-21 20:00"},
            },
        )
        assert result["ok"] is True
        data = result["data"]
        assert data["status"] == "promoted"
        assert data["missing_required"] == []
        assert data["session"]["script_name"] == "雾隐山庄"
        assert data["session"]["start_time"] == "2026-10-21 20:00"
        assert data["session"]["status"] == STATUS_PENDING
        assert not data["suggested_question"]
        # 没有任何房间/DM 时不报错，只提示先建档。
        assert data["auto_assign"]["status"] == "resources_not_configured"
        assert data["auto_assign"]["trigger"] == "session_promoted"

        sessions = load_sessions(deps.sessions_path)
        assert len(sessions) == 1
        assert sessions.iloc[0]["script_name"] == "雾隐山庄"

    def test_唯一候选快速通道自动确认(self, make_deps):
        from app.tools.scripts import upsert_script

        deps = make_deps()
        upsert_script(
            script_name="雾隐山庄",
            script_type="推理",
            total_seats=6,
            path=deps.scripts_path,
            now=NOW,
        )
        result = _call(
            _tools(deps)["store_data"],
            {
                "action": "create_session",
                "fields": {"script_name": "雾隐", "start_time": "2026-10-22 20:00"},
            },
        )
        assert result["ok"] is True
        data = result["data"]
        assert data["status"] == "promoted"
        assert data["script_match"] == "unique"
        assert any("唯一匹配自动确认" in item for item in result["warnings"])
        assert data["session"]["script_name"] == "雾隐山庄"
        # 元数据从唯一候选借入。
        assert data["session"]["script_type"] == "推理"
        assert data["session"]["total_seats"] == 6
        assert len(load_sessions(deps.sessions_path)) == 1

    def test_公共库匹配只借元数据(self, make_deps):
        public = _public_frame(
            {"script_name": "云使", "script_type": "推理", "total_seats": 6}
        )
        deps = make_deps(public_scripts=public)
        result = _call(
            _tools(deps)["store_data"],
            {
                "action": "create_session",
                "fields": {"script_name": "云使", "start_time": "2026-10-23 20:00"},
            },
        )
        assert result["ok"] is True
        data = result["data"]
        assert data["status"] == "promoted"
        assert data["script_source"] == "公共库"
        assert any("剧本信息来自公共库" in item for item in result["warnings"])
        # 场次永不指向公共行：script_id 保持空，只借元数据。
        assert data["fields"]["script_id"] is None
        assert data["session"]["script_type"] == "推理"
        assert data["session"]["total_seats"] == 6

    def test_缺开场时间只追问一项且不落数据(self, make_deps):
        deps = make_deps()
        _seed(deps)
        result = _call(
            _tools(deps)["store_data"],
            {"action": "create_session", "fields": {"script_name": "雾隐山庄"}},
        )
        assert result["ok"] is True
        data = result["data"]
        assert data["status"] == "incomplete"
        assert data["missing_required"] == ["start_time"]
        assert "开场时间" in data["suggested_question"]
        assert data["session"] is None
        assert data["auto_assign"] is None
        # 不完整不落任何数据：sessions 原封不动。
        assert len(load_sessions(deps.sessions_path)) == 3

    def test_缺剧本名只追问一项且不落数据(self, make_deps):
        deps = make_deps()
        _seed(deps)
        result = _call(
            _tools(deps)["store_data"],
            {"action": "create_session", "fields": {"start_time": "2026-10-25 20:00"}},
        )
        assert result["ok"] is True
        data = result["data"]
        assert data["status"] == "incomplete"
        assert data["missing_required"] == ["script_name"]
        assert "本" in data["suggested_question"]
        assert len(load_sessions(deps.sessions_path)) == 3

    def test_多候选列出候选等确认且不建场(self, make_deps):
        from app.tools.scripts import upsert_script

        deps = make_deps()
        upsert_script(script_name="雾隐山庄", path=deps.scripts_path, now=NOW)
        upsert_script(
            script_name="雾隐山庄前传", aliases="", path=deps.scripts_path, now=NOW
        )
        result = _call(
            _tools(deps)["store_data"],
            {
                "action": "create_session",
                "fields": {"script_name": "雾隐", "start_time": "2026-10-21 20:00"},
            },
        )
        assert result["ok"] is True
        data = result["data"]
        assert data["status"] == "needs_confirmation"
        assert data["script_match"] == "candidate"
        names = [
            item["script_name"] for item in data["ambiguities"][0]["candidates"]
        ]
        assert names == ["雾隐山庄", "雾隐山庄前传"]
        for name in names:
            assert f"《{name}》" in data["suggested_question"]
        # 确认前不建场。
        assert load_sessions(deps.sessions_path).empty

    def test_空_fields_与无识别字段返回_bad_request(self, make_deps):
        tool = _tools(make_deps())["store_data"]
        assert _call(tool, {"action": "create_session"})["error_code"] == "bad_request"
        empty = _call(tool, {"action": "create_session", "fields": {}})
        assert empty["ok"] is False
        assert empty["error_code"] == "bad_request"
        junk = _call(tool, {"action": "create_session", "fields": {"foo": 1}})
        assert junk["ok"] is False
        assert junk["error_code"] == "bad_request"

    def test_录入与修正都拒绝房间和_dm_字段(self, make_deps):
        tool = _tools(make_deps())["store_data"]
        for field in ("room_id", "dm_id"):
            result = _call(
                tool,
                {
                    "action": "create_session",
                    "fields": {
                        "script_name": "雾隐山庄",
                        "start_time": "2026-10-21 20:00",
                        field: "x",
                    },
                },
            )
            assert result["ok"] is False
            assert result["error_code"] == "room_not_allowed"

        update = _call(
            tool,
            {
                "action": "update_session",
                "session_id": "sid",
                "fields": {"dm_id": "x"},
            },
        )
        assert update["ok"] is False
        assert update["error_code"] == "room_not_allowed"

    def test_草稿_action_已下线返回_bad_request(self, make_deps):
        tool = _tools(make_deps())["store_data"]
        for action in (
            "save_session_draft",
            "list_session_drafts",
            "drop_session_draft",
        ):
            result = _call(tool, {"action": action})
            assert result["ok"] is False
            assert result["error_code"] == "bad_request"

    def test_create_session_签名没有_draft_参数(self, make_deps):
        store_tool = _tools(make_deps())["store_data"]
        assert "draft_id" not in store_tool.args
        assert "auto_promote" not in store_tool.args
        description = store_tool.description
        assert "create_session" in description
        assert "save_session_draft" not in description
        assert "drop_session_draft" not in description

    def test_修正人数不换_session_id(self, make_deps):
        deps = make_deps()
        _seed(deps)
        sid = _sid("年轮", "2026-10-20 22:00")
        result = _call(
            _tools(deps)["store_data"],
            {
                "action": "update_session",
                "session_id": sid,
                "fields": {"total_seats": 10, "list_price": 188},
            },
        )
        assert result["ok"] is True
        data = result["data"]
        assert data["session_id_changed"] is False
        assert data["session"]["session_id"] == sid
        assert data["session"]["total_seats"] == 10
        assert data["session"]["list_price"] == 188
        assert data["auto_assign"]["status"] == "resources_not_configured"

    def test_改时间重算_id_并搬移预约(self, make_deps):
        deps = make_deps()
        _seed(deps)
        old = _sid("年轮", "2026-10-20 22:00")
        new = _sid("年轮", "2026-10-21 21:00")
        result = _call(
            _tools(deps)["store_data"],
            {
                "action": "update_session",
                "session_id": old,
                "fields": {"start_time": "2026-10-21 21:00"},
            },
        )
        assert result["ok"] is True
        data = result["data"]
        assert data["session_id_changed"] is True
        assert data["previous_session_id"] == old
        assert data["session"]["session_id"] == new

        sessions = load_sessions(deps.sessions_path)
        assert new in set(sessions["session_id"])
        assert old not in set(sessions["session_id"])

        bookings = load_bookings(deps.bookings_path)
        moved = bookings[bookings["session_id"] == new]
        assert len(moved) == 1
        assert int(moved.iloc[0]["seats"]) == 2
        assert (bookings["session_id"] == old).sum() == 0

    def test_只读模式拒绝建场与修正(self, make_deps):
        deps = make_deps(read_only=True)
        tool = _tools(deps)["store_data"]
        cases = [
            {
                "action": "create_session",
                "fields": {
                    "script_name": "雾隐山庄",
                    "start_time": "2026-10-21 20:00",
                },
            },
            {
                "action": "update_session",
                "session_id": "sid",
                "fields": {"total_seats": 6},
            },
        ]
        for args in cases:
            result = _call(tool, args)
            assert result["ok"] is False
            assert result["error_code"] == "read_only"
        assert load_sessions(deps.sessions_path).empty


class TestReadOnlyMode:
    def test_只读模式拒绝场次与预约写操作(self, make_deps):
        deps = make_deps(read_only=True)
        _seed(deps)
        tools = _tools(deps)
        before = (
            deps.sessions_path.stat().st_mtime_ns,
            deps.bookings_path.stat().st_mtime_ns,
        )
        cases = [
            ("upsert_sessions", {
                "rows": [{
                    "store_name": STORE,
                    "script_name": "新本",
                    "start_time": "2026-10-22 19:00",
                    "total_seats": 6,
                }],
            }),
            ("record_booking", {
                "session_id": _sid("漓川怪谈簿", "2026-10-21 19:00"),
                "player_name": "王五",
                "seats": 2,
            }),
            ("cancel_booking", {
                "session_id": _sid("雾隐村", "2026-10-20 20:00"),
                "player_name": "张三",
            }),
        ]
        for action, extra in cases:
            result = _call(tools["store_data"], {"action": action, **extra})
            assert result["ok"] is False
            assert result["error_code"] == "read_only"

        after = (
            deps.sessions_path.stat().st_mtime_ns,
            deps.bookings_path.stat().st_mtime_ns,
        )
        assert after == before
        assert len(load_sessions(deps.sessions_path)) == 3
        assert (load_bookings(deps.bookings_path)["status"] == BOOKING_ACTIVE).sum() == 2

    def test_只读模式允许查询(self, make_deps):
        deps = make_deps(read_only=True)
        _seed(deps)
        result = _call(
            _tools(deps)["store_data"],
            {"action": "query_sessions"},
        )
        assert result["ok"] is True
        assert result["data"]["count"] == 3

    def test_只读模式默认关闭(self):
        assert AgentDeps().read_only is False


# ===========================================================================
# 工具 8：剧本库
# ===========================================================================

class TestScriptTool:
    def test_先存草稿再逐轮补全(self, make_deps):
        deps = make_deps()
        tool = _tools(deps)["script_library"]

        first = _call(tool, {"action": "upsert", "aliases": "NL"})
        assert first["ok"] is True
        assert first["data"]["profile_status"] == "待确认名称"
        assert "script_name" in first["data"]["missing_fields"]
        assert first["data"]["next_question"]
        draft_id = first["data"]["script_id"]

        second = _call(
            tool,
            {
                "action": "upsert",
                "script_id": draft_id,
                "script_name": "年轮",
                "script_type": "情感",
                "total_seats": "6人",
                "duration_minutes": "4小时",
            },
        )
        assert second["ok"] is True
        assert second["data"]["profile_status"] == "已完整"
        assert second["data"]["missing_fields"] == []
        assert second["data"]["aliases"] == ["NL"]

    def test_缩写搜索命中别名(self, make_deps):
        deps = make_deps()
        tool = _tools(deps)["script_library"]
        created = _call(
            tool,
            {
                "action": "upsert",
                "script_name": "年轮",
                "aliases": "NL",
                "script_type": "情感",
                "total_seats": 6,
                "duration_minutes": 240,
            },
        )["data"]

        found = _call(tool, {"action": "search", "query": "nl"})
        assert found["ok"] is True
        assert found["data"]["count"] == 1
        assert found["data"]["candidates"][0]["script_id"] == created["script_id"]
        assert found["data"]["candidates"][0]["source"] == "剧本库"

    def test_搜索可以回退到场次历史(self, make_deps):
        deps = make_deps()
        _seed(deps)
        result = _call(_tools(deps)["script_library"], {"action": "search", "query": "漓川"})
        assert result["ok"] is True
        assert result["data"]["candidates"][0]["source"] == "场次历史"

    def test_本地无结果时明确提示不要直接写入猜测(self, make_deps):
        result = _call(
            _tools(make_deps())["script_library"],
            {"action": "search", "query": "XX"},
        )
        assert result["ok"] is True
        assert result["data"]["candidates"] == []
        assert any("确认" in item for item in result["warnings"])

    def test_get_不存在时给候选(self, make_deps):
        deps = make_deps()
        _seed(deps)
        result = _call(
            _tools(deps)["script_library"],
            {"action": "get", "script_id": "漓川"},
        )
        assert result["ok"] is False
        assert result["error_code"] == "script_not_found"
        assert result["data"]["suggestions"]

    def test_只读模式拒绝剧本写入(self, make_deps):
        deps = make_deps(read_only=True)
        result = _call(
            _tools(deps)["script_library"],
            {"action": "upsert", "aliases": "NL"},
        )
        assert result["ok"] is False
        assert result["error_code"] == "read_only"
        assert not deps.scripts_path.exists()

    def test_未知_action(self, make_deps):
        result = _call(
            _tools(make_deps())["script_library"],
            {"action": "delete_all"},
        )
        assert result["ok"] is False
        assert result["error_code"] == "bad_request"


# ===========================================================================
# 工具 7：营销文案
# ===========================================================================

class TestCopyTool:
    def test_生成两版文案(self, make_deps):
        deps = make_deps(
            copy_model=FakeLLM(_copy_payload(
                "今晚迷雾剧场还有位置，欢迎来玩。",
                "今晚迷雾剧场还有位置，欢迎来玩～🎭",
            ))
        )
        _seed(deps)
        result = _call(
            _tools(deps)["write_marketing_copy"],
            {"store_name": STORE},
        )
        assert result["ok"] is True
        assert result["data"]["community"]
        assert result["data"]["moments"]
        assert result["data"]["sessions"]
        for row in result["data"]["sessions"]:
            assert "contribution_per_seat" not in row
            assert "floor_price" not in row

    def test_内部财务词整次失败(self, make_deps):
        deps = make_deps(
            copy_model=FakeLLM(_copy_payload(
                "这场保本价 20 元，快来。",
                "年轮 20:00 开场，欢迎来玩。",
            ))
        )
        _seed(deps)
        result = _call(
            _tools(deps)["write_marketing_copy"],
            {"store_name": STORE},
        )
        assert result["ok"] is False
        assert result["error_code"] == "internal_leak"
        assert result["data"]["community"] is None
        assert result["data"]["moments"] is None

    def test_未授权价格整次失败(self, make_deps):
        deps = make_deps(
            copy_model=FakeLLM(_copy_payload(
                "今晚只要 99 元。",
                "年轮 20:00 开场，欢迎来玩。",
            ))
        )
        _seed(deps)
        result = _call(
            _tools(deps)["write_marketing_copy"],
            {"store_name": STORE},
        )
        assert result["ok"] is False
        assert result["error_code"] == "price_mismatch"
        assert result["data"]["community"] is None

    def test_多门店未指定(self, make_deps):
        deps = make_deps(copy_model=FakeLLM(_copy_payload("文案", "文案")))
        _seed(deps, extra=[{
            "store_name": OTHER_STORE,
            "script_name": "别家本",
            "start_time": "2026-10-21 19:00",
            "total_seats": 6,
            "list_price": 168,
            "status": STATUS_PENDING,
        }])
        result = _call(_tools(deps)["write_marketing_copy"], {})
        assert result["ok"] is False
        assert result["error_code"] == "bad_request"

    def test_空场不调用模型(self, make_deps):
        model = FakeLLM(_copy_payload("文案", "文案"))
        deps = make_deps(copy_model=model)
        result = _call(_tools(deps)["write_marketing_copy"], {})
        assert result["ok"] is False
        assert result["error_code"] == "no_sessions"
        assert model.calls == []


# ===========================================================================
# Agent 图
# ===========================================================================

class TestAgentLoop:
    def test_场景D_工具调用与结果回填(self, make_deps):
        deps = make_deps(
            copy_model=FakeLLM(_copy_payload(
                "今晚迷雾剧场还有位置，欢迎来玩。",
                "今晚迷雾剧场还有位置，欢迎来玩～🎭",
            ))
        )
        _seed(deps)
        model = ScriptedChatModel(responses=[
            _tool_call("write_marketing_copy", {"store_name": STORE}, "c1"),
            AIMessage(content="文案已生成，请复核。"),
        ])
        agent = build_agent(model=model, deps=deps)
        turn = run_turn(agent, "帮我写今晚的推广文案", thread_id="d1")

        assert turn.answer == "文案已生成，请复核。"
        assert [call["name"] for call in turn.tool_calls] == ["write_marketing_copy"]
        assert len(turn.tool_results) == 1
        tool_payload = json.loads(turn.tool_results[0]["content"])
        assert tool_payload["ok"] is True
        assert tool_payload["data"]["community"]

    def test_场景B_通过Agent写入预约(self, make_deps):
        deps = make_deps()
        _seed(deps)
        sid = _sid("漓川怪谈簿", "2026-10-21 19:00")
        payload = _extract_payload(
            bookings=[{
                "action": "预约",
                "player_name": "王五",
                "seats": 2,
                "store_name": STORE,
                "script_name": "漓川怪谈簿",
                "start_time": "2026-10-21 19:00",
                "source_excerpt": "王五 约2人",
            }],
        )
        deps = replace(deps, extraction_model=FakeLLM(payload))
        model = ScriptedChatModel(responses=[
            _tool_call("extract_message", {"text": SOURCE_BOOKING}, "b1"),
            _tool_call(
                "store_data",
                {"action": "get_session", "session_id": sid},
                "b2",
            ),
            _tool_call(
                "store_data",
                {"action": "record_booking", "session_id": sid,
                 "player_name": "王五", "seats": 2},
                "b3",
            ),
            AIMessage(content="已记录预约。"),
        ])
        agent = build_agent(model=model, deps=deps)
        turn = run_turn(agent, SOURCE_BOOKING, thread_id="b1")

        assert turn.answer == "已记录预约。"
        assert [call["name"] for call in turn.tool_calls] == [
            "extract_message", "store_data", "store_data",
        ]
        bookings = load_bookings(deps.bookings_path)
        active = bookings[bookings["status"] == BOOKING_ACTIVE]
        assert int(active.loc[active["session_id"] == sid, "seats"].sum()) == 2

    def test_同一thread有记忆(self, make_deps):
        model = ScriptedChatModel(responses=[
            AIMessage(content="第一轮"),
            AIMessage(content="第二轮"),
        ])
        agent = build_agent(model=model, deps=make_deps())
        first = run_turn(agent, "你好", thread_id="m1")
        second = run_turn(agent, "继续", thread_id="m1")
        assert first.answer == "第一轮"
        assert second.answer == "第二轮"
        state = agent.get_state({"configurable": {"thread_id": "m1"}})
        assert len(state.values["messages"]) == 4

    def test_thread隔离(self, make_deps):
        model = ScriptedChatModel(responses=[
            AIMessage(content="A"),
            AIMessage(content="B"),
        ])
        agent = build_agent(model=model, deps=make_deps())
        run_turn(agent, "会话一", thread_id="t1")
        run_turn(agent, "会话二", thread_id="t2")
        s1 = agent.get_state({"configurable": {"thread_id": "t1"}})
        s2 = agent.get_state({"configurable": {"thread_id": "t2"}})
        assert len(s1.values["messages"]) == 2
        assert len(s2.values["messages"]) == 2

    def test_轨迹只含本轮(self, make_deps):
        deps = make_deps(
            copy_model=FakeLLM(_copy_payload(
                "今晚还有位置，欢迎来玩。",
                "今晚还有位置，欢迎来玩～🎭",
            ))
        )
        _seed(deps)
        model = ScriptedChatModel(responses=[
            _tool_call("write_marketing_copy", {"store_name": STORE}, "c1"),
            AIMessage(content="第一轮结束"),
            AIMessage(content="第二轮结束"),
        ])
        agent = build_agent(model=model, deps=deps)
        first = run_turn(agent, "写文案", thread_id="x1")
        second = run_turn(agent, "谢谢", thread_id="x1")
        assert first.tool_calls
        assert second.tool_calls == ()

    def test_空输入(self, make_deps):
        agent = build_agent(
            model=ScriptedChatModel(responses=[AIMessage(content="ok")]),
            deps=make_deps(),
        )
        with pytest.raises(ValueError):
            run_turn(agent, "   ", thread_id="empty")


# ===========================================================================
# 契约与安全
# ===========================================================================

class TestSafety:
    def test_系统提示覆盖四场景与禁止项(self):
        for marker in ("A. 店长转发拼场广告", "B. 店长转发玩家预约",
                       "C. 店长转发玩家跳车", "D. 店长要求写推广文案"):
            assert marker in SYSTEM_PROMPT
        assert "不自动发送微信" in SYSTEM_PROMPT
        assert "人工复核" in SYSTEM_PROMPT
        assert "players" not in SYSTEM_PROMPT

    def test_未预期异常不穿透(self, make_deps, tmp_path):
        deps = make_deps(sessions_path=tmp_path)  # 目录当 Excel 读，必然抛异常
        result = _call(_tools(deps)["filter_expired"], {})
        assert result["ok"] is False
        assert result["error_code"] == "internal_error"

    def test_只读工具不写文件(self, make_deps):
        deps = make_deps()
        _seed(deps)
        before_sessions = deps.sessions_path.stat().st_mtime_ns
        before_bookings = deps.bookings_path.stat().st_mtime_ns
        tools = _tools(deps)
        _call(tools["filter_expired"], {})
        _call(tools["assess_risk"], {})
        _call(tools["suggest_pricing"], {})
        assert deps.sessions_path.stat().st_mtime_ns == before_sessions
        assert deps.bookings_path.stat().st_mtime_ns == before_bookings

    def test_注入模型时不需要key(self, make_deps):
        agent = build_agent(
            model=ScriptedChatModel(responses=[AIMessage(content="ok")]),
            deps=make_deps(),
        )
        assert agent is not None

    def test_附加系统提示词只追加不替换(self, monkeypatch):
        captured: dict[str, Any] = {}

        def fake_create_agent(**kwargs):
            captured.update(kwargs)
            return object()

        monkeypatch.setattr("app.agent.create_agent", fake_create_agent)
        build_agent(
            model=object(),
            extra_system_prompt="【当前门店】迷雾剧场",
        )
        prompt = captured["system_prompt"]
        assert prompt.startswith(SYSTEM_PROMPT)
        assert "【当前门店】迷雾剧场" in prompt
        assert "不自动发送微信" in prompt


# ===========================================================================
# 工具 9-11：资源、排期、DM 场控（只读）
# ===========================================================================

class TestResourceTool:
    def test_初始缺口清单(self, make_deps):
        result = _call(_tools(make_deps())["manage_resources"], {"action": "setup_status"})
        assert result["ok"] is True
        assert result["data"]["counts"]["rooms"] == 0
        codes = [item["code"] for item in result["data"]["setup"]["items"]]
        assert "no_rooms" in codes
        assert "no_dms" in codes

    def test_新增房间与_dm_后可见(self, make_deps):
        deps = make_deps()
        tools = _tools(deps)
        room = _call(
            tools["manage_resources"],
            {"action": "upsert_room", "room": {"name": "A 房", "capacity": 6}},
        )
        assert room["ok"] is True
        assert room["data"]["counts"]["rooms"] == 1

        dm = _call(
            tools["manage_resources"],
            {"action": "upsert_dm", "dm": {"name": "小北", "skills": "推理"}},
        )
        assert dm["ok"] is True
        assert dm["data"]["counts"]["dms"] == 1

        listed = _call(tools["manage_resources"], {"action": "list_rooms"})
        assert listed["data"]["rooms"][0]["name"] == "A 房"

    def test_载入演示资源需要确认(self, make_deps):
        tool = _tools(make_deps())["manage_resources"]
        refused = _call(tool, {"action": "seed_demo"})
        assert refused["ok"] is False
        assert refused["error_code"] == "confirm_required"
        seeded = _call(tool, {"action": "seed_demo", "confirm": True})
        assert seeded["ok"] is True
        assert seeded["data"]["seeded"] is True

    def test_只读模式拒绝写_action(self, make_deps):
        deps = make_deps(read_only=True)
        result = _call(
            _tools(deps)["manage_resources"],
            {"action": "upsert_room", "room": {"name": "A 房", "capacity": 6}},
        )
        assert result["ok"] is False
        assert result["error_code"] == "read_only"

    def test_未知_action_返回_bad_request(self, make_deps):
        result = _call(_tools(make_deps())["manage_resources"], {"action": "delete_all"})
        assert result["ok"] is False
        assert result["error_code"] == "bad_request"


class TestScheduleTool:
    def test_list_返回未来场次(self, make_deps):
        deps = make_deps()
        _seed(deps)
        result = _call(_tools(deps)["schedule_sessions"], {"action": "list"})
        assert result["ok"] is True
        assert result["data"]["sessions"]

    def test_plan_只读且标记最优性(self, make_deps):
        deps = make_deps()
        _seed(deps)
        result = _call(_tools(deps)["schedule_sessions"], {"action": "plan"})
        assert result["ok"] is True
        assert "optimal" in result["data"]
        assert result["data"]["shortages"]

    def test_apply_需要确认与明确场次(self, make_deps):
        deps = make_deps()
        _seed(deps)
        tool = _tools(deps)["schedule_sessions"]
        no_confirm = _call(tool, {"action": "apply", "session_ids": ["x"]})
        assert no_confirm["error_code"] == "confirm_required"
        no_ids = _call(tool, {"action": "apply", "confirm": True})
        assert no_ids["error_code"] == "bad_request"

    def test_只读模式拒绝自动分配(self, make_deps):
        deps = make_deps(read_only=True)
        result = _call(_tools(deps)["schedule_sessions"], {"action": "auto_assign"})
        assert result["ok"] is False
        assert result["error_code"] == "read_only"

    def test_resolve_缺参数返回_bad_request(self, make_deps):
        result = _call(_tools(make_deps())["schedule_sessions"], {"action": "resolve"})
        assert result["ok"] is False
        assert result["error_code"] == "bad_request"

    def test_cancel_session_缺确认拒绝(self, make_deps):
        deps = make_deps()
        _seed(deps)
        sid = _sid("年轮", "2026-10-20 22:00")
        tool = _tools(deps)["schedule_sessions"]

        no_sid = _call(tool, {"action": "cancel_session", "confirm": True})
        assert no_sid["ok"] is False
        assert no_sid["error_code"] == "bad_request"

        no_confirm = _call(tool, {"action": "cancel_session", "session_id": sid})
        assert no_confirm["ok"] is False
        assert no_confirm["error_code"] == "confirm_required"
        # 没有确认就没有写入。
        sessions = load_sessions(path=deps.sessions_path)
        row = sessions[sessions["session_id"] == sid]
        assert row.iloc[0]["status"] == STATUS_PENDING

    def test_cancel_session_确认后标记已取消(self, make_deps):
        deps = make_deps()
        _seed(deps)
        sid = _sid("年轮", "2026-10-20 22:00")
        result = _call(
            _tools(deps)["schedule_sessions"],
            {"action": "cancel_session", "session_id": sid, "confirm": True},
        )
        assert result["ok"] is True
        assert result["data"]["session"]["status"] == STATUS_CANCELLED
        sessions = load_sessions(path=deps.sessions_path)
        row = sessions[sessions["session_id"] == sid]
        # 只改状态，不删数据。
        assert len(sessions) == 3
        assert row.iloc[0]["status"] == STATUS_CANCELLED

    def test_cancel_session_只读模式拒绝(self, make_deps):
        deps = make_deps(read_only=True)
        _seed(deps)
        sid = _sid("年轮", "2026-10-20 22:00")
        result = _call(
            _tools(deps)["schedule_sessions"],
            {"action": "cancel_session", "session_id": sid, "confirm": True},
        )
        assert result["ok"] is False
        assert result["error_code"] == "read_only"
        sessions = load_sessions(path=deps.sessions_path)
        row = sessions[sessions["session_id"] == sid]
        assert row.iloc[0]["status"] == STATUS_PENDING


class TestWp12PromptAndChoices:
    """WP12：单轮化提示词 + 候选确认改为 quick_reply 按钮。"""

    def test_system_prompt_has_no_session_draft_mechanics(self):
        # 场次草稿机制（draft_id / 转正流程）已随 WP15 删除；
        # 剧本档案草稿（PROFILE_DRAFT）有意保留，不在此列。
        for banned in (
            "save_session_draft",
            "list_session_drafts",
            "drop_session_draft",
            "draft_id",
            "auto_promote",
            "草稿续接",
            "转正",
        ):
            assert banned not in SYSTEM_PROMPT
        assert "这两项齐全就直接建场" in SYSTEM_PROMPT
        assert "不落任何数据" in SYSTEM_PROMPT
        assert "cancel_session" in SYSTEM_PROMPT
        assert "唯一候选匹配" in SYSTEM_PROMPT

    def test_unique_match_promoted_has_no_draft_select_script(self, make_deps):
        from app.agent import AgentTurn
        from app.tools.scripts import upsert_script
        from app.web.chat_choices import build_choices

        deps = make_deps()
        upsert_script(
            script_name="雾隐山庄",
            script_type="推理",
            total_seats=6,
            path=deps.scripts_path,
            now=NOW,
        )
        payload = _call(
            _tools(deps)["store_data"],
            {
                "action": "create_session",
                "fields": {"script_name": "雾隐", "start_time": "2026-10-21 20:00"},
            },
        )
        assert payload["ok"] is True
        assert payload["data"]["status"] == "promoted"
        assert payload["data"]["script_match"] == "unique"

        turn = AgentTurn(
            answer="已保存",
            tool_calls=({"name": "store_data", "args": {}},),
            tool_results=({"name": "store_data", "content": json.dumps(payload, ensure_ascii=False)},),
        )
        groups = build_choices(turn)
        kinds = [group["kind"] for group in groups]
        # 唯一匹配没有歧义，不应出现候选取按钮，也没有任何 draft_* 种类。
        assert "draft_select_script" not in kinds
        assert "draft_confirm" not in kinds
        assert set(kinds) <= {"quick_reply", "resolve"}

    def test_two_candidates_become_quick_reply_buttons(self, make_deps):
        from app.agent import AgentTurn
        from app.tools.scripts import upsert_script
        from app.web.chat_choices import build_choices

        deps = make_deps()
        upsert_script(script_name="雾隐山庄", path=deps.scripts_path, now=NOW)
        upsert_script(
            script_name="雾隐山庄前传", aliases="", path=deps.scripts_path, now=NOW
        )
        payload = _call(
            _tools(deps)["store_data"],
            {
                "action": "create_session",
                "fields": {"script_name": "雾隐", "start_time": "2026-10-21 20:00"},
            },
        )
        assert payload["ok"] is True
        assert payload["data"]["status"] == "needs_confirmation"
        assert payload["data"]["script_match"] == "candidate"

        turn = AgentTurn(
            answer="请确认剧本",
            tool_calls=({"name": "store_data", "args": {}},),
            tool_results=({"name": "store_data", "content": json.dumps(payload, ensure_ascii=False)},),
        )
        groups = build_choices(turn)
        assert [group["kind"] for group in groups] == ["quick_reply"]
        buttons = groups[0]["buttons"]
        # 候选变成纯文本 quick_reply 按钮，点击只重发文本，不提交任何写。
        assert [button["payload"]["text"] for button in buttons] == [
            "就开《雾隐山庄》",
            "就开《雾隐山庄前传》",
        ]
        for button in buttons:
            assert button["action"] == "quick_reply"
            assert set(button["payload"]) == {"text"}
            assert len(button["label"]) <= 40
        assert all(group["kind"] in {"quick_reply", "resolve"} for group in groups)


class TestDMControlTool:
    def test_只读且没有推进_action(self, make_deps):
        tool = _tools(make_deps())["dm_control"]
        today = _call(tool, {"action": "list_today"})
        assert today["ok"] is True
        assert today["data"] == []

        status = _call(tool, {"action": "status"})
        assert status["ok"] is True

        for forbidden in ("next", "start", "pause", "finish", "mode"):
            result = _call(tool, {"action": forbidden})
            assert result["ok"] is False
            assert result["error_code"] == "bad_request"

    def test_未知_run_返回_run_not_found(self, make_deps):
        result = _call(
            _tools(make_deps())["dm_control"],
            {"action": "status", "run_id": "run-missing"},
        )
        assert result["ok"] is False
        assert result["error_code"] == "run_not_found"

    def test_只读模式下仍可查询(self, make_deps):
        result = _call(
            _tools(make_deps(read_only=True))["dm_control"],
            {"action": "list_today"},
        )
        assert result["ok"] is True


class TestPromptRules:
    def test_硬必填与不自动推进(self):
        assert "硬必填只有剧本全名和开场时间" in SYSTEM_PROMPT
        assert "这两项齐全就直接建场" in SYSTEM_PROMPT
        assert "不落任何数据" in SYSTEM_PROMPT
        assert "绝不建议「到点自动下一幕」" in SYSTEM_PROMPT
        assert "房间和 DM 永远不由店长输入" in SYSTEM_PROMPT
        assert "manage_resources" in SYSTEM_PROMPT
        assert "dm_control" in SYSTEM_PROMPT

    def test_系统提示词没有场次草稿机制(self):
        for banned in (
            "save_session_draft",
            "drop_session_draft",
            "draft_id",
            "草稿续接",
            "draft_select_script",
            "draft_confirm",
            "转正",
        ):
            assert banned not in SYSTEM_PROMPT
        # 剧本档案草稿（PROFILE_DRAFT，场景 E）是有意保留的，
        # 所以不能一刀切禁掉「草稿」二字。
        assert "草稿" in SYSTEM_PROMPT


# ---------------------------------------------------------------------------
# 公共剧本库接入：pub- 守卫、公共回退、同名覆盖警告
# ---------------------------------------------------------------------------


def _public_frame(*rows):
    import pandas as pd

    from app.tools.public_library import coerce_public_scripts, make_public_script_id

    records = []
    for row in rows:
        records.append(
            {
                "script_id": make_public_script_id(row["script_name"]),
                "script_name": row["script_name"],
                "aliases": row.get("aliases", ""),
                "script_type": row.get("script_type"),
                "total_seats": row.get("total_seats"),
                "duration_minutes": row.get("duration_minutes"),
                "list_price": row.get("list_price"),
                "notes": "",
                "source_text": "",
                "created_at": NOW,
                "updated_at": NOW,
                "origin": "策展",
                "imported_at": NOW,
            }
        )
    return coerce_public_scripts(pd.DataFrame(records))


class TestPublicLibraryIntegration:
    def test_upsert_pub_id_rejected(self, make_deps):
        public = _public_frame({"script_name": "云使", "script_type": "推理"})
        deps = make_deps(public_scripts=public)
        result = _call(
            _tools(deps)["script_library"],
            {"action": "upsert", "script_id": "pub-abc123", "script_type": "情感"},
        )
        assert result["ok"] is False
        assert result["error_code"] == "public_readonly"

    def test_search_falls_back_to_public(self, make_deps):
        public = _public_frame(
            {"script_name": "云使", "script_type": "推理", "total_seats": 6, "duration_minutes": 270}
        )
        deps = make_deps(public_scripts=public)
        result = _call(
            _tools(deps)["script_library"], {"action": "search", "query": "云使"}
        )
        assert result["data"]["count"] >= 1
        hit = result["data"]["candidates"][0]
        assert hit["script_name"] == "云使"
        assert hit["source"] == "公共库"
        assert hit["read_only"] is True

    def test_get_falls_back_to_public(self, make_deps):
        from app.tools.public_library import make_public_script_id

        public = _public_frame({"script_name": "云使", "script_type": "推理"})
        deps = make_deps(public_scripts=public)
        result = _call(
            _tools(deps)["script_library"],
            {"action": "get", "script_id": make_public_script_id("云使")},
        )
        assert result["ok"] is True
        assert result["data"]["script"]["source"] == "公共库"
        assert result["data"]["script"]["read_only"] is True

    def test_private_same_name_upsert_warns_shadow(self, make_deps):
        public = _public_frame({"script_name": "云使", "script_type": "推理"})
        deps = make_deps(public_scripts=public)
        result = _call(
            _tools(deps)["script_library"],
            {"action": "upsert", "script_name": "云使", "script_type": "情感"},
        )
        assert result["ok"] is True
        assert any("公共库已有同名" in w for w in result.get("warnings", []))

    def test_find_scripts_public_param(self, make_deps):
        from app.tools.scripts import find_scripts

        public = _public_frame({"script_name": "云使", "script_type": "推理"})
        deps = make_deps(public_scripts=public)
        items = find_scripts(
            "云使",
            path=deps.scripts_path,
            sessions=pd.DataFrame(),  # 显式空表：避免触发真实 data/ 读取
            store=None,
            public=public,
        )
        assert items and items[0]["source"] == "公共库"

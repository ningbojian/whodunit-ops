"""第 9 步演示：ReAct Agent 组装与五个业务场景。

默认离线：
  - 用脚本模型固定工具调用顺序，验证 Agent 图、参数传递、工具读写、结果回填；
  - 文本抽取与文案生成用脚本内 FakeLLM；
  - 数据写在临时目录，不碰真实 data/；
  - 不发网络请求、不需要 API key。

真实调用（可选）：
  PYTHONUTF8=1 "C:/Users/Lenovo/anaconda3/python.exe" scripts/09_agent_demo.py --live

  只打印「检测到 DEEPSEEK_API_KEY：是/否」，绝不打印 key 的值。
  会真实调用 DeepSeek 并产生 API 费用，由使用者决定是否运行：
  先跑场景 A–D，再跑新增路由四轮（资源缺口 / 场次草稿转正 + 自动分配 /
  排期缺口 / DM 场控只读），最后跑第 9 轮「把没排上的场次补一下」——
  这一轮只观察模型在无可用房间时是否会调用会直接写库的 auto_assign，
  跑完会核对该场是否真的被写入分配行。
  场景 E（剧本缩写 / 残缺档案）只有离线演示，不包含在 --live 中。

注意：
  离线模式的脚本模型**固定**了工具调用顺序，只能证明装配与工具链路正确，
  不能证明真实模型一定会按预期选择工具。真实模型的选择能力需要 --live 观察。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import unicodedata
from pathlib import Path
from typing import Any

# Windows 控制台默认 stdout / stderr 编码是 GBK，中文会全部乱码。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd  # noqa: E402
from langchain_core.language_models.chat_models import BaseChatModel  # noqa: E402
from langchain_core.messages import AIMessage  # noqa: E402
from langchain_core.outputs import ChatGeneration, ChatResult  # noqa: E402

from app.agent import AgentDeps, AgentTurn, build_agent, run_turn  # noqa: E402
from app.config import BOOKINGS_XLSX, SCRIPTS_XLSX, SESSIONS_XLSX  # noqa: E402
from app.store.excel import ExcelStore  # noqa: E402
from app.tools.demo import seed_demo_resources, seed_demo_stages  # noqa: E402
from app.tools.extract import ACTION_CANCEL, extract_message  # noqa: E402
from app.tools.scripts import upsert_script  # noqa: E402
from app.tools.resources import load_assignments  # noqa: E402
from app.tools.storage import (  # noqa: E402
    STATUS_PENDING,
    load_sessions,
    load_sessions_with_counts,
    make_session_id,
    upsert_booking,
    upsert_sessions,
)

NOW = pd.Timestamp("2026-10-20 18:00")  # 固定演练时间，保证输出可复现
STORE = "迷雾剧场"

AD_TEXT = "【迷雾剧场】七日怪谈 10月22日19:00 6人本 188/位 还差4人"
BOOKING_TEXT = "迷雾剧场 漓川怪谈簿 10月21日19:00 王五 约2人"
CANCEL_TEXT = "迷雾剧场 雾隐村 10月20日20:00 张三 跳车"
COPY_TEXT = "帮我给迷雾剧场写今晚的推广文案"
ABBREV_TEXT = "WY 是哪个本？我记不清全名了。"
ADD_SCRIPT_TEXT = "帮我加个剧本，我只记得叫《年轮》，8 人本。"

# --live 的新增路由四轮，对应 SYSTEM_PROMPT 的 F / G / H / K。
RESOURCE_TEXT = "帮我看看现在房间里和 DM 还缺什么"
DRAFT_TEXT = "帮我记一场：10 月 23 日晚上 8 点开《雾隐山庄》"
SHORTAGE_TEXT = "这周有没有排不开的场次？"
DM_STATUS_TEXT = "现在场控演到哪一幕了？"
# 补充轮：房间里最大 10 人，12 人场必然排不上，用来观察 auto_assign 的行为。
FILL_TEXT = "把没排上的场次补一下"


# ---------------------------------------------------------------------------
# 显示工具（与 06-08 演示一致：按终端显示宽度对齐）
# ---------------------------------------------------------------------------

def title(text: str) -> None:
    print()
    print("=" * 96)
    print(text)
    print("=" * 96)


def note(text: str) -> None:
    print(f"  >> {text}")


def _w(text) -> int:
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in str(text))


def _cell(value, width: int, right: bool) -> str:
    text = str(value)
    pad = " " * max(width - _w(text), 0)
    return pad + text if right else text + pad


def _line(cells, widths, rights) -> str:
    return " ".join(_cell(v, w, r) for v, w, r in zip(cells, widths, rights))


def show_table(headers, rows, widths, rights=None) -> None:
    rights = rights or [False] * len(headers)
    print("  " + _line(headers, widths, rights))
    print("  " + "─" * (sum(widths) + len(widths) - 1))
    for row in rows:
        print("  " + _line(row, widths, rights))


def _num(value) -> str:
    return "—" if pd.isna(value) else str(int(value))


# ---------------------------------------------------------------------------
# 假模型：抽取、文案、脚本化工具调用
# ---------------------------------------------------------------------------

class FakeLLM:
    """工具 1 / 工具 7 内部模型的离线替身。"""

    def __init__(self, content: str = "", *, error: Exception | None = None):
        self.content = content
        self.error = error
        self.calls: list[list[dict[str, str]]] = []

    def invoke(self, messages):
        self.calls.append(messages)
        if self.error is not None:
            raise self.error
        return self.content


class RoutedExtractLLM:
    """按原文关键词返回预设抽取结果，供离线演示使用。"""

    def __init__(self) -> None:
        self.calls: list[list[dict[str, str]]] = []

    def invoke(self, messages):
        self.calls.append(messages)
        text = messages[-1]["content"] if messages else ""
        if "跳车" in text or "取消" in text:
            payload = {
                "intent": "跳车",
                "sessions": [],
                "bookings": [{
                    "action": ACTION_CANCEL,
                    "player_name": "张三",
                    "seats": 5,
                    "store_name": STORE,
                    "script_name": "雾隐村",
                    "start_time": "2026-10-20 20:00",
                    "start_time_text": "10月20日20:00",
                    "source_excerpt": "张三 跳车",
                }],
                "warnings": [],
            }
        elif "约" in text:
            payload = {
                "intent": "预约",
                "sessions": [],
                "bookings": [{
                    "action": "预约",
                    "player_name": "王五",
                    "seats": 2,
                    "store_name": STORE,
                    "script_name": "漓川怪谈簿",
                    "start_time": "2026-10-21 19:00",
                    "start_time_text": "10月21日19:00",
                    "source_excerpt": "王五 约2人",
                }],
                "warnings": [],
            }
        else:
            payload = {
                "intent": "拼场广告",
                "sessions": [{
                    "store_name": STORE,
                    "script_name": "七日怪谈",
                    "script_type": "推理",
                    "start_time": "2026-10-22 19:00",
                    "start_time_text": "10月22日19:00",
                    "total_seats": 6,
                    "list_price": 188,
                    "price_text": "188/位",
                    "count_text": "6人本",
                    "remaining_reported": 4,
                    "booked_reported": 2,
                    "source_excerpt": AD_TEXT,
                }],
                "bookings": [],
                "warnings": [],
            }
        return json.dumps(payload, ensure_ascii=False)


class CopyFakeLLM:
    """从文案工具的 Prompt 里读出公开场次，拼出符合校验的两套文案。"""

    def invoke(self, messages):
        user = messages[-1]["content"]
        block = user.split("】\n", 1)[1].split("\n\n请按", 1)[0]
        records = json.loads(block)
        lines = []
        for row in records:
            price = row.get("recommended_price")
            saving = row.get("seat_saving")
            if price is None:
                text = (
                    f"《{row['script_name']}》{row['start_time']} 开场，"
                    f"还剩 {row['remaining_seats']} 位"
                )
            else:
                text = (
                    f"《{row['script_name']}》{row['start_time']} 开场，"
                    f"还剩 {row['remaining_seats']} 位，"
                    f"现价 {int(price)} 元/位"
                )
                if saving and float(saving) > 0:
                    text += f"，立省 {int(saving)} 元"
            lines.append(text)

        community = (
            f"今晚{STORE}还有场次可以上车：\n"
            + "；\n".join(lines)
            + "。\n想玩的朋友直接来约～"
        )
        moments = (
            f"今晚的{STORE}，还有几场等着人来凑齐。\n\n"
            + "\n".join(lines)
            + "。\n\n位置不多，想上车的朋友直接私信我～🎭✨"
        )
        return json.dumps(
            {
                "community": community,
                "moments": moments,
                "warnings": ["地址与联系方式请店长补充"],
            },
            ensure_ascii=False,
        )


class ScriptedChatModel(BaseChatModel):
    """按脚本返回 AIMessage 的假 chat model，用于离线跑 Agent 图。"""

    responses: list[Any]
    index: int = 0

    @property
    def _llm_type(self) -> str:
        return "scripted-demo-model"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        message = self.responses[min(self.index, len(self.responses) - 1)]
        self.index += 1
        return ChatResult(generations=[ChatGeneration(message=message)])


def _call(name: str, args: dict[str, Any], call_id: str) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{"name": name, "args": args, "id": call_id}],
    )


# ---------------------------------------------------------------------------
# 数据与轨迹
# ---------------------------------------------------------------------------

def _seed(deps: AgentDeps) -> None:
    upsert_sessions([
        {
            "store_name": STORE,
            "script_name": "雾隐村",
            "script_type": "恐怖",
            "start_time": "2026-10-20 20:00",
            "total_seats": 6,
            "list_price": 168,
            "status": STATUS_PENDING,
        },
        {
            "store_name": STORE,
            "script_name": "年轮",
            "script_type": "情感",
            "start_time": "2026-10-20 22:00",
            "total_seats": 8,
            "list_price": 168,
            "status": STATUS_PENDING,
        },
        {
            "store_name": STORE,
            "script_name": "漓川怪谈簿",
            "script_type": "推理",
            "start_time": "2026-10-21 19:00",
            "total_seats": 6,
            "list_price": 168,
            "status": STATUS_PENDING,
        },
    ], path=deps.sessions_path)

    sessions = load_sessions(deps.sessions_path)
    for row in sessions.to_dict("records"):
        if row["script_name"] == "雾隐村":
            upsert_booking(
                row["session_id"], "张三", seats=5,
                path=deps.bookings_path, now=NOW,
            )
        elif row["script_name"] == "年轮":
            upsert_booking(
                row["session_id"], "李四", seats=2,
                path=deps.bookings_path, now=NOW,
            )


def _show_turn(turn: AgentTurn) -> None:
    for call in turn.tool_calls:
        args = json.dumps(call["args"], ensure_ascii=False)
        print(f"  -> {call['name']}({args[:160]})")
    for item in turn.tool_results:
        try:
            payload = json.loads(item["content"])
        except (TypeError, json.JSONDecodeError):
            payload = {"ok": None, "raw": str(item["content"])[:80]}
        code = payload.get("error_code") or "—"
        print(f"  <- {item['name']}: ok={payload.get('ok')}，error_code={code}")
        if item["name"] == "write_marketing_copy" and payload.get("ok"):
            data = payload.get("data") or {}
            print("\n  --- 社群短版 ---")
            print(data.get("community") or "（空）")
            print("\n  --- 朋友圈长版 ---")
            print(data.get("moments") or "（空）")
        if item["name"] == "store_data":
            data = payload.get("data") or {}
            if isinstance(data.get("session"), dict):
                row = data["session"]
                print(
                    f"     场次 {row.get('script_name')}："
                    f"已约 {row.get('booked_seats')}，剩余 {row.get('remaining_seats')}"
                )
            elif data.get("count") is not None:
                print(f"     共 {data.get('count')} 条")
        if item["name"] == "script_library" and payload.get("ok"):
            data = payload.get("data") or {}
            if "candidates" in data:
                for row in data.get("candidates") or []:
                    print(
                        f"     候选：《{row.get('script_name')}》"
                        f"（{row.get('profile_status')}，来源 {row.get('source')}）"
                    )
            elif data.get("script_name"):
                print(
                    f"     档案：《{data['script_name']}》"
                    f"（{data.get('profile_status')}）"
                )
                missing = "、".join(data.get("missing_fields") or []) or "无"
                print(f"     缺失：{missing}")
                if data.get("next_question"):
                    print(f"     追问：{data['next_question']}")
    print(f"  Agent：{turn.answer}")


def _session_id(deps: AgentDeps, script: str) -> str:
    sessions = load_sessions(deps.sessions_path)
    hit = sessions[sessions["script_name"] == script]
    if hit.empty:
        raise RuntimeError(f"演示数据缺少剧本 {script}")
    return str(hit.iloc[0]["session_id"])


def _show_data(deps: AgentDeps) -> None:
    sessions = load_sessions_with_counts(deps.sessions_path, deps.bookings_path)
    bookings = pd.read_excel(deps.bookings_path)
    print()
    show_table(
        ["剧本", "开场", "总位", "已约", "剩余", "状态"],
        [
            [
                row["script_name"],
                pd.Timestamp(row["start_time"]).strftime("%m-%d %H:%M"),
                _num(row["total_seats"]),
                _num(row["booked_seats"]),
                _num(row["remaining_seats"]),
                row["status"],
            ]
            for row in sessions.to_dict("records")
        ],
        [14, 12, 5, 5, 5, 8],
        [False, False, True, True, True, False],
    )
    note(f"预约明细共 {len(bookings)} 行")


# ---------------------------------------------------------------------------
# 五个场景（离线脚本模型）
# ---------------------------------------------------------------------------

def offline_scenario_a(deps: AgentDeps) -> AgentTurn:
    title("场景 A｜拼场广告：抽取 → 去重 → 入库 → 风险 → 定价")

    extracted = extract_message(
        AD_TEXT, model=deps.extraction_model, now=NOW, default_store=STORE
    )
    rows = extracted.sessions
    note(f"预设抽取结果：{len(rows)} 场；intent={extracted.intent}")

    model = ScriptedChatModel(responses=[
        _call("extract_message", {"text": AD_TEXT}, "a1"),
        _call("dedupe_sessions", {"records": rows}, "a2"),
        _call("store_data", {"action": "upsert_sessions", "rows": rows}, "a3"),
        _call("assess_risk", {}, "a4"),
        _call("suggest_pricing", {}, "a5"),
        AIMessage(content=(
            "广告已录入。新增场次《七日怪谈》，已给出高危场次与建议折扣；"
            "请核对开场时间与价格后再安排推广。"
        )),
    ])
    agent = build_agent(model=model, deps=deps)
    return run_turn(agent, AD_TEXT, thread_id="demo-a")


def offline_scenario_b(deps: AgentDeps) -> AgentTurn:
    title("场景 B｜玩家预约：抽取 → 定位场次 → 写入预约")

    extracted = extract_message(
        BOOKING_TEXT, model=deps.extraction_model, now=NOW, default_store=STORE
    )
    booking = extracted.bookings[0]
    sid = _session_id(deps, booking["script_name"])
    note(f"候选场次 {sid}（{booking['script_name']}）")

    model = ScriptedChatModel(responses=[
        _call("extract_message", {"text": BOOKING_TEXT}, "b1"),
        _call("store_data", {"action": "get_session", "session_id": sid}, "b2"),
        _call(
            "store_data",
            {"action": "record_booking", "session_id": sid,
             "player_name": booking["player_name"], "seats": booking["seats"]},
            "b3",
        ),
        AIMessage(content="已记录王五的 2 人预约，并更新剩余空位。"),
    ])
    agent = build_agent(model=model, deps=deps)
    return run_turn(agent, BOOKING_TEXT, thread_id="demo-b")


def offline_scenario_c(deps: AgentDeps) -> AgentTurn:
    title("场景 C｜玩家跳车：抽取 → 取消预约 → 重新评估与定价")

    extracted = extract_message(
        CANCEL_TEXT, model=deps.extraction_model, now=NOW, default_store=STORE
    )
    booking = extracted.bookings[0]
    sid = _session_id(deps, booking["script_name"])
    note(f"跳车场次 {sid}（{booking['script_name']}），玩家 {booking['player_name']}")

    model = ScriptedChatModel(responses=[
        _call("extract_message", {"text": CANCEL_TEXT}, "c1"),
        _call(
            "store_data",
            {"action": "cancel_booking", "session_id": sid,
             "player_name": booking["player_name"]},
            "c2",
        ),
        _call("assess_risk", {}, "c3"),
        _call("suggest_pricing", {}, "c4"),
        AIMessage(content="已取消张三的预约，并按最新空位重新评估风险与建议价。"),
    ])
    agent = build_agent(model=model, deps=deps)
    return run_turn(agent, CANCEL_TEXT, thread_id="demo-c")


def offline_scenario_d(deps: AgentDeps) -> AgentTurn:
    title("场景 D｜营销文案：只调文案工具，不在 Agent 层改写")

    model = ScriptedChatModel(responses=[
        _call("write_marketing_copy", {"store_name": STORE}, "d1"),
        AIMessage(content="两套文案已生成，请店长人工复核后复制发布。"),
    ])
    agent = build_agent(model=model, deps=deps)
    return run_turn(agent, COPY_TEXT, thread_id="demo-d")


def offline_scenario_e(deps: AgentDeps) -> tuple[AgentTurn, AgentTurn]:
    title("场景 E｜剧本缩写：先查本地别名；残缺档案先存草稿再追问")

    upsert_script(
        script_name="雾隐村",
        aliases="WY / 雾村",
        script_type="恐怖",
        total_seats=6,
        duration_minutes=240,
        list_price=168,
        path=deps.scripts_path,
        now=NOW,
    )
    note("本地剧本库预置：《雾隐村》，别名 WY / 雾村。")

    search_model = ScriptedChatModel(responses=[
        _call("script_library", {"action": "search", "query": "WY"}, "e1"),
        AIMessage(content=(
            "本地剧本库里 WY 对应《雾隐村》（恐怖，6 人，240 分钟）。"
            "要按这个全名建场次吗？"
        )),
    ])
    first = run_turn(
        build_agent(model=search_model, deps=deps),
        ABBREV_TEXT,
        thread_id="demo-e1",
    )

    note("第二轮只提供全名和人数，工具应先生成草稿，并从场次历史补类型/价格。")
    add_model = ScriptedChatModel(responses=[
        _call(
            "script_library",
            {"action": "upsert", "script_name": "年轮", "total_seats": "8人"},
            "e2",
        ),
        AIMessage(content=(
            "《年轮》已先保存为草稿，类型/人数/参考价已补齐；"
            "还缺时长。这个本大概几小时？"
        )),
    ])
    second = run_turn(
        build_agent(model=add_model, deps=deps),
        ADD_SCRIPT_TEXT,
        thread_id="demo-e2",
    )
    return first, second


# ---------------------------------------------------------------------------
# 真实模型
# ---------------------------------------------------------------------------

def _seed_newtools(deps: AgentDeps) -> None:
    """为新增路由四轮准备演示资源、剧本档案与阶段模板。"""
    store = deps.store or ExcelStore(
        sessions_path=deps.sessions_path,
        bookings_path=deps.bookings_path,
        scripts_path=deps.scripts_path,
    )
    for name, kind, seats, price in (
        ("雾隐山庄", "推理", 6, 168),
        ("年轮", "情感", 6, 158),
    ):
        upsert_script(
            script_name=name,
            script_type=kind,
            total_seats=seats,
            duration_minutes=240,
            list_price=price,
            store=store,
            now=NOW,
        )
    seed_demo_resources(store, now=NOW)
    seed_demo_stages(store, now=NOW)


def _seed_unassigned(deps: AgentDeps) -> tuple[Any, str]:
    """造一场「所有房间都放不下」的场次，返回 (store, session_id)。

    演示房间最大 10 人，所以 12 人场一定排不上；用于观察模型在无资源时
    是否如实说明、是否会调用会直接写库的 auto_assign。
    """
    store = deps.store or ExcelStore(
        sessions_path=deps.sessions_path,
        bookings_path=deps.bookings_path,
        scripts_path=deps.scripts_path,
    )
    upsert_script(
        script_name="千灯愿",
        script_type="情感",
        total_seats=12,
        duration_minutes=240,
        list_price=178,
        store=store,
        now=NOW,
    )
    upsert_sessions(
        [
            {
                "store_name": STORE,
                "script_name": "千灯愿",
                "script_type": "情感",
                "start_time": "2026-10-24 19:00",
                "total_seats": 12,
                "list_price": 178,
                "status": STATUS_PENDING,
            }
        ],
        path=deps.sessions_path,
    )
    return store, make_session_id(STORE, "千灯愿", "2026-10-24 19:00")


def live_scenarios(deps: AgentDeps) -> None:
    title("可选｜--live：真实 DeepSeek 跑四个场景 + 新增路由四轮（场景 E 仅离线）")
    note("真实模型会自行选择工具；下面只展示它实际调用了什么。")
    agent = build_agent(deps=deps)
    for index, text in enumerate(
        (AD_TEXT, BOOKING_TEXT, CANCEL_TEXT, COPY_TEXT), start=1
    ):
        print(f"\n第 {index} 轮｜用户：{text}")
        turn = run_turn(agent, text, thread_id=f"live-{index}")
        _show_turn(turn)

    title("新增路由｜F 资源缺口 / G 场次草稿 / H 排期缺口 / K 场控只读")
    _seed_newtools(deps)
    note("已载入演示资源、剧本档案与阶段模板。")
    for index, text in enumerate(
        (RESOURCE_TEXT, DRAFT_TEXT, SHORTAGE_TEXT, DM_STATUS_TEXT), start=5
    ):
        print(f"\n第 {index} 轮｜用户：{text}")
        turn = run_turn(agent, text, thread_id=f"live-new-{index}")
        _show_turn(turn)

    # 补充轮：只观察，不改产品语义。Web 看板是「预览 → 勾选 → 应用」，
    # Agent 工具的 auto_assign/apply 目前仍是直接写库，这里记录真实表现。
    title("补充轮｜L 未分配场次补齐（观察 auto_assign 是否直接写库）")
    store, fill_id = _seed_unassigned(deps)
    note("已写入一场 12 人场（演示房间最大 10 人）：既没有房间，也没有 DM。")
    print(f"\n第 9 轮｜用户：{FILL_TEXT}")
    turn = run_turn(agent, FILL_TEXT, thread_id="live-fill")
    _show_turn(turn)

    assignments = load_assignments(store=store)
    mine = assignments[
        assignments["session_id"].astype("string").fillna("") == fill_id
    ] if not assignments.empty else assignments
    if mine.empty:
        note("本轮结束后该场仍然没有任何分配行（未被写入）。")
    else:
        kinds = {
            str(row["resource_kind"]): str(row["resource_id"])
            for row in mine.to_dict("records")
        }
        note(f"本轮结束后该场已有分配行：{kinds}（Agent 侧没有预览/审核步骤）。")


def _mtime(path: Path) -> int | None:
    try:
        return path.stat().st_mtime_ns
    except FileNotFoundError:
        return None


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="第 9 步 ReAct Agent 演示")
    parser.add_argument(
        "--live",
        action="store_true",
        help="真实调用 DeepSeek（需要 .env 中配置 DEEPSEEK_API_KEY）",
    )
    args = parser.parse_args()

    key = os.getenv("DEEPSEEK_API_KEY", "").strip()
    print(f"检测到 DEEPSEEK_API_KEY：{'是' if key else '否'}")
    if args.live and not key:
        note("未检测到 key，跳过真实调用。")
        note("配置方式：把 .env.example 复制为 .env，填入 DEEPSEEK_API_KEY=sk-xxxx。")
        note("不要在聊天或终端输出 key 的值。")
        return 0

    real_paths = [SESSIONS_XLSX, BOOKINGS_XLSX, SCRIPTS_XLSX]
    before = {path: _mtime(path) for path in real_paths}

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        deps = AgentDeps(
            sessions_path=root / "sessions.xlsx",
            bookings_path=root / "bookings.xlsx",
            scripts_path=root / "scripts.xlsx",
            now=lambda: NOW,
            extraction_model=None if args.live else RoutedExtractLLM(),
            copy_model=None if args.live else CopyFakeLLM(),
            default_store=STORE,
        )
        _seed(deps)
        note(f"演示数据写在临时目录：{root}")
        _show_data(deps)

        if args.live:
            live_scenarios(deps)
        else:
            title("离线模式说明")
            note("脚本模型固定了工具调用顺序：验证装配、参数传递、工具读写与结果回填。")
            note("它不代表真实模型的工具选择能力；真实选择需加 --live 观察。")

            for scenario in (
                offline_scenario_a,
                offline_scenario_b,
                offline_scenario_c,
                offline_scenario_d,
                offline_scenario_e,
            ):
                result = scenario(deps)
                turns = result if isinstance(result, tuple) else (result,)
                for turn in turns:
                    _show_turn(turn)
                    print()
                _show_data(deps)

    after = {path: _mtime(path) for path in real_paths}
    changed = [path.name for path in real_paths if before[path] != after[path]]
    if changed:
        note("真实 data/ 的 mtime 发生变化：" + "、".join(changed))
    else:
        note("真实 data/ 的 mtime 未变化：演示全程只写临时目录。")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""工具 1 演示：文本抽取。

演示七件事：
  1. 看一眼发给模型的 Prompt：当前时间、默认门店、反注入规则、JSON 结构
  2. 完整广告：原文时间确定性解析、提示人数 `_reported` 不落盘
  3. 缺字段 + 默认门店：模型推断时间，必须留下「时间由模型推断」备注
  4. 预约 / 跳车：动作、昵称、人数、候选场次 ID（只候选，不查库）
  5. 坏 JSON 与字段降级：代码围栏能恢复；坏 JSON 结构化失败；坏字段不整批失败
  6. 端到端：抽取 -> dedupe -> 落盘 -> 预约 -> 跳车 -> 人数重算
  7. 幂等 + 对照真实 data/

运行：
  PYTHONUTF8=1 "C:/Users/Lenovo/anaconda3/python.exe" scripts/07_extract_demo.py

真实调用（可选，需要 .env 已配置 DEEPSEEK_API_KEY）：
  PYTHONUTF8=1 "C:/Users/Lenovo/anaconda3/python.exe" scripts/07_extract_demo.py --live

默认离线：使用脚本里的 FakeLLM，不发网络请求、不需要 API key。
演示数据只写临时目录，结束自动清理；最后只读真实 data/ 做对照。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import unicodedata
from pathlib import Path
from types import SimpleNamespace

# Windows 控制台默认 stdout / stderr 编码是 GBK，中文会全部乱码。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd  # noqa: E402

from app.config import BOOKINGS_XLSX, SESSIONS_XLSX  # noqa: E402
from app.tools.dedupe import dedupe_sessions  # noqa: E402
from app.tools.extract import (  # noqa: E402
    NOTE_STORE_DEFAULTED,
    build_extraction_messages,
    extract_message,
    parse_extraction_response,
    sessions_frame,
)
from app.tools.storage import (  # noqa: E402
    BOOKING_ACTIVE,
    cancel_booking,
    is_missing_value,
    load_bookings,
    load_sessions,
    load_sessions_with_counts,
    upsert_booking,
    upsert_sessions,
)

NOW = pd.Timestamp("2026-10-18 12:00")  # 周日，固定下来保证输出稳定
STORE = "迷雾剧场"


# ---------------------------------------------------------------------------
# 显示工具（与 06_dedupe_demo.py 相同：按终端显示宽度对齐）
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


def _text(value) -> str:
    return "—" if is_missing_value(value) else str(value)


def _num(value) -> str:
    return "—" if is_missing_value(value) else str(int(value))


def _price(value) -> str:
    return "—" if is_missing_value(value) else f"{float(value):.0f}"


def _dt(value) -> str:
    return "—" if is_missing_value(value) else pd.to_datetime(value).strftime("%m-%d %H:%M")


def _tuple(value) -> str:
    return "—" if not value else " / ".join(value)


def _notes(notes) -> str:
    return "—" if not notes else "；".join(notes)


# ---------------------------------------------------------------------------
# FakeLLM
# ---------------------------------------------------------------------------

class FakeLLM:
    """只实现 .invoke(messages) —— 工具 1 只依赖这一个方法。"""

    def __init__(self, content: str):
        self.content = content
        self.calls: list[list[dict[str, str]]] = []

    def invoke(self, messages):
        self.calls.append(messages)
        return SimpleNamespace(content=self.content)


def _json(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False)


# ---------------------------------------------------------------------------
# 演示数据
# ---------------------------------------------------------------------------

SOURCE_AD = "【迷雾剧场】年轮 20号19:30 8人本 情感/还原 168/位 已约6人 还差2人"

SOURCE_SHORT = "蛊魂铃 明晚8点 6人本 还差2人"

SOURCE_BOOKINGS = (
    "迷雾剧场 年轮 20号19:30 张三 约2人\n"
    "迷雾剧场 年轮 20号19:30 李四 跳车"
)


def ad_payload() -> dict:
    return {
        "intent": "拼场广告",
        "sessions": [{
            "store_name": STORE,
            "script_name": "年轮",
            "script_type": "情感/还原",
            "start_time": "2026-10-20 19:30",
            "start_time_text": "20号19:30",
            "total_seats": 8,
            "list_price": 168,
            "price_text": "168/位",
            "count_text": "8人本",
            "remaining_reported": 2,
            "booked_reported": 6,
            "status": None,
            "source_excerpt": SOURCE_AD,
        }],
        "bookings": [],
        "warnings": [],
    }


def short_payload() -> dict:
    return {
        "intent": "拼场广告",
        "sessions": [{
            "store_name": None,
            "script_name": "蛊魂铃",
            "script_type": None,
            "start_time": "2026-10-19 20:00",
            "start_time_text": "明晚8点",
            "total_seats": 6,
            "list_price": None,
            "price_text": None,
            "count_text": "6人本",
            "remaining_reported": 2,
            "booked_reported": None,
            "status": None,
            "source_excerpt": SOURCE_SHORT,
        }],
        "bookings": [],
        "warnings": ["原文没有写价格"],
    }


def booking_payload() -> dict:
    common = {
        "store_name": STORE,
        "script_name": "年轮",
        "start_time": "2026-10-20 19:30",
        "start_time_text": "20号19:30",
    }
    return {
        "intent": "混合",
        "sessions": [],
        "bookings": [
            {
                "action": "预约",
                "player_name": "张三",
                "seats": 2,
                **common,
                "source_excerpt": "迷雾剧场 年轮 20号19:30 张三 约2人",
            },
            {
                "action": "跳车",
                "player_name": "李四",
                "seats": 1,
                **common,
                "source_excerpt": "迷雾剧场 年轮 20号19:30 李四 跳车",
            },
        ],
        "warnings": [],
    }


# ---------------------------------------------------------------------------
# 视图
# ---------------------------------------------------------------------------

def show_sessions(result) -> None:
    rows = []
    for row in result.sessions:
        rows.append([
            _text(row["store_name"]), _text(row["script_name"]),
            _text(row["script_type"]), _dt(row["start_time"]),
            _text(row["start_time_source"]), _num(row["total_seats"]),
            _price(row["list_price"]), _num(row["booked_reported"]),
            _num(row["remaining_reported"]), _text(row["status"]),
        ])
    show_table(
        ["门店", "剧本", "类型", "开场", "时间来源", "总位", "原价",
         "已约*", "还差*", "状态"],
        rows,
        widths=[9, 8, 9, 12, 8, 4, 5, 5, 5, 7],
        rights=[False, False, False, False, False, True, True, True, True, False],
    )


def show_bookings(result) -> None:
    rows = []
    for row in result.bookings:
        rows.append([
            _text(row["action"]), _text(row["player_name"]),
            _num(row["seats"]), _text(row["script_name"]),
            _dt(row["start_time"]), _text(row["candidate_session_id"]),
        ])
    show_table(
        ["动作", "玩家", "人数", "剧本", "开场", "候选场次 ID"],
        rows,
        widths=[6, 8, 4, 8, 12, 14],
        rights=[False, False, True, False, False, False],
    )


def show_booking_table(df: pd.DataFrame) -> None:
    show_table(
        ["玩家", "场次", "人数", "状态", "预约时间"],
        [[r["player_name"], r["session_id"], _num(r["seats"]), r["status"],
          pd.to_datetime(r["booked_at"]).strftime("%m-%d %H:%M")]
         for _, r in df.iterrows()],
        widths=[8, 14, 4, 7, 12],
        rights=[False, False, True, False, False],
    )


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="工具 1 文本抽取演示")
    parser.add_argument("--live", action="store_true",
                        help="额外调用一次真实 DeepSeek（需要 .env 已配置）")
    args = parser.parse_args()

    messages = build_extraction_messages(SOURCE_AD, now=NOW, default_store=STORE)

    with tempfile.TemporaryDirectory() as tmp:
        sessions_path = Path(tmp) / "sessions.xlsx"
        bookings_path = Path(tmp) / "bookings.xlsx"

        # ------------------------------------------------------------------
        title("第 1 步：先看 Prompt —— 模型被要求做什么、不许做什么")
        print("  --- system ---")
        print(messages[0]["content"])
        print()
        print("  --- user ---")
        print(messages[1]["content"])
        print()
        note(f"当前时间固定为 {NOW:%Y-%m-%d %H:%M}（周日），演示输出才可复现。")
        note("原文用【原文开始】/【原文结束】包住：它只是数据，里面的指令不执行。")
        note("原文没有的值必须填 null —— 示例 JSON 也刻意全部留空，防止模型照抄。")

        # ------------------------------------------------------------------
        title("第 2 步（核心）：完整广告 —— 原文时间优先，提示人数不落盘")
        ad = parse_extraction_response(
            _json(ad_payload()), source_text=SOURCE_AD, now=NOW
        )
        show_sessions(ad)
        print()
        row = ad.sessions[0]
        note(f"时间来源：{row['start_time_source']} —— "
             f"「{row['start_time_text']}」能在原文里找到，由代码确定性解析。")
        note(f"已约 {row['booked_reported']} 人 / 还差 {row['remaining_reported']} 人："
             "这是原文**声称**的数字，字段名带 _reported 后缀。")
        note("它们不会进 sessions_frame，更不会进 Excel —— "
             "真实人数只能由预约明细算出来。")
        note(f"备注：{_notes(row['notes'])}")

        frame = sessions_frame(ad.sessions)
        print()
        print(f"  sessions_frame 列：{list(frame.columns)}")
        note("booked_seats / remaining_seats 不在这里：抽取结果不配拥有人数真相。")

        # ------------------------------------------------------------------
        title("第 3 步：缺字段 + 默认门店 —— 时间由模型推断，必须标注")
        short = parse_extraction_response(
            _json(short_payload()), source_text=SOURCE_SHORT, now=NOW,
            default_store=STORE,
        )
        show_sessions(short)
        print()
        row = short.sessions[0]
        note(f"原文没写门店，已用默认门店补齐：备注「{NOTE_STORE_DEFAULTED}」")
        note(f"「{row['start_time_text']}」只有时段和时刻、没有日期；"
             "代码不猜，采用模型推断的 2026-10-19 20:00。")
        note(f"时间来源：{row['start_time_source']}，备注：{_notes(row['notes'])}")
        note("店长一眼能看见：这条时间不是原文写死的，需要确认。")
        note(f"模型自己的提示也保留：{_notes(short.warnings)}")

        # ------------------------------------------------------------------
        title("第 4 步：预约 / 跳车 —— 只给候选场次 ID，不查库不写库")
        bookings = parse_extraction_response(
            _json(booking_payload()), source_text=SOURCE_BOOKINGS, now=NOW
        )
        show_bookings(bookings)
        print()
        note("两条记录指向同一个 candidate_session_id：门店 + 剧本 + 时间算出的哈希。")
        note("它只是**候选** —— 本工具不查场次表，也不调用 upsert_booking / cancel_booking。")
        note("Agent 必须先查库确认场次存在，再决定写预约还是写跳车。")
        note("李四的「跳车」是玩家动作，不是场次状态；场次本身没有被取消。")

        # ------------------------------------------------------------------
        title("第 5 步：坏 JSON 显式失败，坏字段只降级不整批失败")
        fenced = "```json\n" + _json(ad_payload()) + "\n```"
        recovered = parse_extraction_response(fenced, source_text=SOURCE_AD, now=NOW)
        print(f"  代码围栏：ok={recovered.ok}，抽到 {len(recovered.sessions)} 条场次")
        note("模型偶尔会套 Markdown 围栏；这不是坏 JSON，允许恢复。")

        broken_raw = '{"sessions": [{"store_name": "迷雾剧场",}]}'
        broken = parse_extraction_response(broken_raw, source_text=SOURCE_AD, now=NOW)
        print(f"\n  尾逗号：ok={broken.ok}，error_code={broken.error_code}")
        print(f"  error：{broken.error}")
        print(f"  raw_snippet：{broken.raw_snippet}")
        note("不自动删尾逗号、不自动补括号 —— 坏 JSON 必须让人看见模型输出质量。")

        degraded_payload = ad_payload()
        degraded_payload["sessions"][0].update({
            "total_seats": "待定",
            "list_price": "总价800",
            "status": "满员",
        })
        degraded = parse_extraction_response(
            _json(degraded_payload), source_text=SOURCE_AD, now=NOW
        )
        row = degraded.sessions[0]
        print(f"\n  坏字段：ok={degraded.ok}，"
              f"总位={row['total_seats']}，原价={row['list_price']}，"
              f"状态={row['status']}")
        note(f"备注：{_notes(row['notes'])}")
        note("整条记录、整批消息都还在；只有坏字段降级为 null 并留下原因。")

        # ------------------------------------------------------------------
        title("第 6 步：端到端 —— 抽取 -> dedupe -> 落盘 -> 预约 -> 人数重算")
        first = dedupe_sessions(
            sessions_frame(ad.sessions), existing=load_sessions(sessions_path)
        )
        upsert_sessions(first, sessions_path)

        second = dedupe_sessions(
            sessions_frame(short.sessions), existing=load_sessions(sessions_path)
        )
        upsert_sessions(second, sessions_path)

        stored = load_sessions(sessions_path)
        show_table(
            ["session_id", "剧本", "开场", "总位", "原价", "状态"],
            [[r["session_id"], r["script_name"], _dt(r["start_time"]),
              _num(r["total_seats"]), _price(r["list_price"]), r["status"]]
             for _, r in stored.iterrows()],
            widths=[14, 8, 12, 4, 5, 7],
            rights=[False, False, False, True, True, False],
        )
        note("两条场次都落盘了；session_id 由 dedupe 按联合键补出来。")

        player_book = bookings.bookings[0]
        player_cancel = bookings.bookings[1]
        sid = player_book["candidate_session_id"]

        upsert_booking(
            sid, player_book["player_name"], seats=player_book["seats"],
            source_text=SOURCE_BOOKINGS, path=bookings_path, now=NOW,
        )
        # 李四先有预约、再跳车：cancel_booking 是软删除，行会保留。
        upsert_booking(
            sid, player_cancel["player_name"], seats=1,
            source_text=SOURCE_BOOKINGS, path=bookings_path, now=NOW,
        )
        cancelled = cancel_booking(
            sid, player_cancel["player_name"],
            source_text=SOURCE_BOOKINGS, path=bookings_path, now=NOW,
        )

        print()
        show_booking_table(load_bookings(bookings_path))
        note(f"cancel_booking 返回：{cancelled}（软删除：行还在，状态变已取消）。")

        with_counts = load_sessions_with_counts(sessions_path, bookings_path)
        print()
        show_table(
            ["剧本", "总位", "已约", "剩余"],
            [[r["script_name"], _num(r["total_seats"]),
              _num(r["booked_seats"]), _num(r["remaining_seats"])]
             for _, r in with_counts.iterrows()],
            widths=[8, 4, 4, 4],
            rights=[False, True, True, True],
        )
        note("人数不是从文案里抄的，而是从预约明细重算：张三 2 人有效，李四已取消。")

        excel_columns = pd.read_excel(sessions_path).columns.tolist()
        print()
        print(f"  Excel 列：{excel_columns}")
        for hidden in ("remaining_reported", "booked_reported", "notes",
                       "source_excerpt", "candidate_session_id"):
            print(f"    包含 {hidden}：{hidden in excel_columns}")

        # ------------------------------------------------------------------
        title("第 7 步：幂等 —— 同一批再抽一次、同一条预约再写一次")
        again = dedupe_sessions(
            sessions_frame(ad.sessions), existing=load_sessions(sessions_path)
        )
        print(f"  dedupe 动作：{again['dedupe_action'].value_counts().to_dict()}")
        note("同一批文案再处理一次，全部「无变化」：主键稳定，不会产生重复场次。")

        upsert_booking(
            sid, player_book["player_name"], seats=player_book["seats"],
            source_text=SOURCE_BOOKINGS, path=bookings_path, now=NOW,
        )
        active = load_bookings(bookings_path)
        active = active[active["status"] == BOOKING_ACTIVE]
        count = int(active["seats"].sum())
        print(f"  张三再写一次后，有效预约总人数：{count}")
        note("booking_id = 场次 + 昵称的哈希，同一个人反复预约只算一次。")

    # ----------------------------------------------------------------------
    title("对照：真实 data/（只读）")
    for label, path in (("场次表", SESSIONS_XLSX), ("预约表", BOOKINGS_XLSX)):
        if not path.exists():
            print(f"  {label} {path.name} 不存在，跳过。")
            continue
        before = path.stat().st_mtime_ns
        if label == "场次表":
            data = load_sessions(path)
        else:
            data = load_bookings(path)
        after = path.stat().st_mtime_ns
        print(f"  {label}：{len(data)} 行，文件修改时间未变：{before == after}")
    note("演示只写临时目录；真实 data/ 全程只读。")

    if args.live:
        run_live()

    return 0


def run_live() -> None:
    """可选的真实调用。只判断 key 是否存在，绝不打印它的值。"""
    title("可选：真实调用 DeepSeek")
    has_key = bool(os.getenv("DEEPSEEK_API_KEY", "").strip())
    print(f"  检测到 DEEPSEEK_API_KEY：{'是' if has_key else '否'}")
    if not has_key:
        note("跳过真实调用。配置方式：")
        note("  1. 把项目根目录的 .env.example 复制为 .env")
        note("  2. 在 .env 中填入 DEEPSEEK_API_KEY=sk-xxxx")
        note("  3. 重新运行：scripts/07_extract_demo.py --live")
        return

    for label, text in (("拼场广告", SOURCE_AD), ("预约 / 跳车", SOURCE_BOOKINGS)):
        print()
        print(f"  --- 真实抽取：{label} ---")
        result = extract_message(text, now=NOW, default_store=STORE)
        if not result.ok:
            print(f"  ok=False，error_code={result.error_code}")
            print(f"  error={result.error}")
            print(f"  raw_snippet={result.raw_snippet}")
            continue
        print(f"  ok=True，intent={result.intent}，"
              f"sessions={len(result.sessions)}，bookings={len(result.bookings)}")
        if result.sessions:
            show_sessions(result)
        if result.bookings:
            show_bookings(result)
        if result.warnings:
            print(f"  warnings：{_notes(result.warnings)}")
        note("真实模型的输出不保证和离线演示一致；这里只展示、不做断言。")


if __name__ == "__main__":
    raise SystemExit(main())

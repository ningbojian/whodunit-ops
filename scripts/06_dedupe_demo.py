"""工具 2 演示：场次去重。

演示七件事：
  1. 建一张「已经落过盘」的现有场次表
  2. **核心** —— 同一场转发三次（完整 / 残缺 / 改价），合并后字段一个不丢
  3. 与现有表比对的四种动作：更新 / 无变化 / 新增 / 未比对
  4. 身份缺失的四类坏行：放行、标注、互不合并、整批不崩
  5. 显式 session_id 的边界，以及 storage 为什么拒绝同样的坏行
  6. storage 复用同一套合并规则：人数和取消状态不被复转文案覆盖
  7. 幂等 + 对照真实 data/

运行：
  PYTHONUTF8=1 "C:/Users/Lenovo/anaconda3/python.exe" scripts/06_dedupe_demo.py

本脚本**不写 data/ 目录**：演示数据建在临时目录里，结束自动清理。
只在最后一步读一下真实的 data/ 做对照，只读不写。
"""

from __future__ import annotations

import sys
import tempfile
import unicodedata
from pathlib import Path

# Windows 控制台默认 stdout / stderr 编码是 GBK，中文会全部乱码。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd  # noqa: E402

from app.config import SESSIONS_XLSX  # noqa: E402
from app.tools.dedupe import dedupe_sessions  # noqa: E402
from app.tools.storage import (  # noqa: E402
    STATUS_CANCELLED,
    is_missing_value,
    load_sessions,
    save_sessions,
    upsert_sessions,
)

STORE = "迷雾剧场"


# ---------------------------------------------------------------------------
# 显示工具（与 05_pricing_demo.py 相同：按终端显示宽度对齐）
# ---------------------------------------------------------------------------

def title(text: str) -> None:
    print()
    print("=" * 92)
    print(text)
    print("=" * 92)


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
    return "—" if not value else ", ".join(value)


# ---------------------------------------------------------------------------
# 演示数据
# ---------------------------------------------------------------------------

def _row(script: str, **extra) -> dict:
    row = {"store_name": STORE, "script_name": script, "start_time": "2026-10-20 19:30"}
    row.update(extra)
    return row


def build_existing(path: Path) -> pd.DataFrame:
    """第 1 步：先落一张现有表，后面的比对才有对象。"""
    upsert_sessions([
        _row("年轮", script_type="情感", total_seats=8, list_price=168,
             source_text="年轮 20号19:30 8人本 168/位"),
        _row("蛊魂铃", script_type="恐怖", start_time="2026-10-20 21:00",
             total_seats=6, list_price=198,
             source_text="蛊魂铃 21:00 6人本 198/位"),
    ], path)
    return load_sessions(path)


def three_forwards() -> list[dict]:
    """同一场的三次转发：完整 → 残缺 → 残缺但改价。"""
    return [
        _row("年轮", script_type="情感", total_seats=8, list_price=168,
             source_text="年轮 20号19:30 8人本 168/位"),
        _row("年轮", source_text="年轮 还差2人"),
        _row("年轮", list_price=158, source_text="年轮 今晚特价 158/位"),
    ]


# ---------------------------------------------------------------------------
# 两种视图
# ---------------------------------------------------------------------------

def show_merge_view(df: pd.DataFrame) -> None:
    show_table(
        ["剧本", "开场", "总位", "原价", "类型", "合并条数", "冲突字段"],
        [[_text(r["script_name"]), _dt(r["start_time"]), _num(r["total_seats"]),
          _price(r["list_price"]), _text(r["script_type"]),
          _num(r["merged_count"]), _tuple(r["conflict_fields"])]
         for _, r in df.iterrows()],
        widths=[10, 11, 4, 5, 6, 8, 14],
        rights=[False, False, True, True, False, True, False],
    )


def show_compare_view(df: pd.DataFrame) -> None:
    show_table(
        ["剧本", "动作", "会改写的字段", "合并条数", "冲突字段"],
        [[_text(r["script_name"]), _text(r["dedupe_action"]),
          _tuple(r["changed_fields"]), _num(r["merged_count"]),
          _tuple(r["conflict_fields"])]
         for _, r in df.iterrows()],
        widths=[10, 8, 28, 8, 14],
        rights=[False, False, False, True, False],
    )


def show_missing_view(df: pd.DataFrame) -> None:
    show_table(
        ["行", "门店", "剧本", "开场", "放行原因"],
        [[i, _text(r["store_name"]), _text(r["script_name"]),
          _dt(r["start_time"]), _text(r["dedupe_note"])]
         for i, (_, r) in enumerate(df.iterrows(), start=1)],
        widths=[3, 9, 8, 12, 24],
        rights=[True, False, False, False, False],
    )


def action_counts(df: pd.DataFrame) -> dict[str, int]:
    return df["dedupe_action"].value_counts().to_dict()


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        sessions_path = Path(tmp) / "sessions.xlsx"

        # ------------------------------------------------------------------
        title("第 1 步：先建一张现有场次表")
        existing = build_existing(sessions_path)
        show_table(
            ["session_id", "剧本", "开场", "总位", "原价", "状态"],
            [[r["session_id"], r["script_name"], _dt(r["start_time"]),
              _num(r["total_seats"]), _price(r["list_price"]), r["status"]]
             for _, r in existing.iterrows()],
            widths=[13, 10, 11, 4, 5, 7],
            rights=[False, False, False, True, True, False],
        )
        note("session_id 不是手填的：门店 + 剧本 + 归一化开场时间的哈希。")
        note("后面的合并、比对、落盘，全都以它为主键。")

        # ------------------------------------------------------------------
        title("第 2 步（核心）：同一场转发三次，合并后一个字段都不丢")
        forwards = three_forwards()
        show_table(
            ["转发", "剧本", "类型", "总位", "原价", "原文"],
            [[i, r["script_name"], _text(r.get("script_type")),
              _num(r.get("total_seats")), _price(r.get("list_price")),
              r["source_text"]]
             for i, r in enumerate(forwards, start=1)],
            widths=[4, 6, 5, 4, 5, 36],
            rights=[True, False, False, True, True, False],
        )
        print()

        merged = dedupe_sessions(forwards, existing=existing)
        show_merge_view(merged)
        print()
        row = merged.iloc[0]
        note(f"合并条数 {int(row['merged_count'])}：三条转发被认成同一场。")
        note("total_seats / script_type 只有第一条提供 —— 后两条残缺，但**没有把它们抹掉**。")
        note("list_price 从 168 变成 158：最后一个非空值胜出。")
        note(f"冲突字段 {row['conflict_fields']}：两个非空值打架，工具不替你判断，但必须报出来。")
        note("旧实现按整行去重，后一条残缺会把 8 人本、情感本、168 全部丢掉。")
        note("source_text 三次都不同，所以它被刻意排除在冲突报告之外。")

        # ------------------------------------------------------------------
        title("第 3 步：和现有表比，结果是四种动作之一")
        batch = [
            _row("年轮", script_type="情感", total_seats=8, list_price=158,
                 source_text="年轮 今晚特价 158/位"),
            _row("蛊魂铃", script_type="恐怖", start_time="2026-10-20 21:00",
                 total_seats=6, list_price=198,
                 source_text="蛊魂铃 21:00 6人本 198/位"),
            _row("青楼", script_type="阵营", start_time="2026-10-20 20:30",
                 total_seats=7, list_price=188,
                 source_text="青楼 20:30 7人本 188/位"),
        ]
        compared = dedupe_sessions(batch, existing=existing)
        show_compare_view(compared)
        print()
        note("年轮：价格 168 -> 158，动作「更新」。")
        note("蛊魂铃：与现有表逐字段一致，动作「无变化」—— 重复转发不会产生无谓写入。")
        note("青楼：现有表里没有，动作「新增」。")
        note("status / booked_seats / updated_at 不参与比对：")
        note("  前两个 storage 会无条件保留旧值，最后一个每次写入都变。")

        print()
        uncompared = dedupe_sessions(batch)
        show_compare_view(uncompared)
        note("不传 existing：全部「未比对」。这不等于「新增」—— 调用方没给现有表，")
        note("工具不假装知道它是不是已经存在。两种语义必须分开。")

        # ------------------------------------------------------------------
        title("第 4 步：身份缺失 —— 放行、标注、不撞键、不崩")
        bad = [
            {"script_name": "年轮", "start_time": "2026-10-20 19:30",
             "source_text": "没写门店的转发 A"},
            {"script_name": "年轮", "start_time": "2026-10-20 19:30",
             "source_text": "没写门店的转发 B"},
            {"store_name": STORE, "start_time": "2026-10-20 19:30",
             "source_text": "没写剧本的转发"},
            {"store_name": STORE, "script_name": "年轮",
             "source_text": "没写时间的转发"},
            {"store_name": STORE, "script_name": "年轮",
             "start_time": "下周三晚上", "source_text": "时间写成口语的转发"},
        ]
        released = dedupe_sessions(bad)
        show_missing_view(released)
        print()
        note("前两行门店缺失、剧本和时间都一样 —— 仍然各自独立，没有互相合并。")
        note("旧实现会把 str(pd.NA) == '<NA>' 当成身份参与哈希，所有缺门店的行撞成一场。")
        note("「没写时间」和「写了但解析不了」分开报：整形会把后者静默变成 NaT，")
        note("所以身份判定必须在类型整形之前完成。")
        note("整批一条都不抛异常：分析层要能容忍坏数据，让 LLM 拿着备注去追问店长。")

        # ------------------------------------------------------------------
        title("第 5 步：显式 session_id —— 逃生舱与落盘闸")
        same_room = dedupe_sessions([
            _row("年轮", session_id="room-101", source_text="101 房 19:30"),
            _row("年轮", session_id="room-102", source_text="102 房 19:30"),
        ], existing=existing)
        show_table(
            ["显式ID", "剧本", "合并条数", "放行备注"],
            [[r["session_id"], r["script_name"], _num(r["merged_count"]),
              _text(r["dedupe_note"])]
             for _, r in same_room.iterrows()],
            widths=[9, 8, 8, 28],
            rights=[False, False, True, False],
        )
        print()
        note("同店、同剧本、同时间，但调用方给了两个不同的显式 ID。")
        note("场次表没有房间字段 —— 这完全可能是两个房间同时开，所以**不合并**。")
        note("工具不替调用方改判，但联合键撞了必须报出来。")

        print()
        unsafe = dedupe_sessions([{
            "script_name": "年轮", "source_text": "缺门店的转发",
        }])
        print("  把同一批的坏行直接喂给 storage.upsert_sessions：\n")
        try:
            upsert_sessions(unsafe, Path(tmp) / "should_not_be_written.xlsx")
        except ValueError as exc:
            for line in str(exc).split("。"):
                if line.strip():
                    print(f"    {line.strip()}。")
        print()
        note("分析层放行，落盘层拒绝 —— 这是刻意的不对称：")
        note("  dedupe 是给 LLM 看的，坏数据要能流过去并被标注；")
        note("  upsert 是进 Excel 前的最后一道闸，主键为空的行一旦落盘，身份就永久丢了。")
        note("文件没有被创建：" + str(not (Path(tmp) / "should_not_be_written.xlsx").exists()))

        # ------------------------------------------------------------------
        title("第 6 步：storage 复用同一套合并规则")
        frame = load_sessions(sessions_path)
        lib = frame["script_name"] == "年轮"
        frame.loc[lib, "booked_seats"] = 3        # 模拟预约明细已经算出的结果
        frame.loc[lib, "status"] = STATUS_CANCELLED  # 模拟店长手工取消
        save_sessions(frame, sessions_path)
        before = load_sessions(sessions_path).set_index("script_name").loc["年轮"]

        upsert_sessions(merged, sessions_path)   # 第 2 步的合并结果直接落盘
        after = load_sessions(sessions_path).set_index("script_name").loc["年轮"]

        fields = [
            ("total_seats", _num), ("list_price", _price),
            ("script_type", _text), ("source_text", _text),
            ("booked_seats", _num), ("status", _text),
        ]
        show_table(
            ["字段", "写入前", "写入后", "结论"],
            [[name, fmt(before[name]), fmt(after[name]),
              "未改变" if fmt(before[name]) == fmt(after[name]) else "被新值更新"]
             for name, fmt in fields],
            widths=[14, 34, 34, 12],
            rights=[False, False, False, False],
        )
        print()
        note("价格、类型、原文被复转文案更新；人数和取消状态原样保留。")
        note("这两条是硬不变量：booked_seats 只能来自预约明细，")
        note("status 必须保留店长的人工决定 —— 已取消的场次不能被一条复转文案复活。")
        note("合并规则只有一份实现（storage 第四节），工具 2 和工具 6 不会各算各的。")

        # ------------------------------------------------------------------
        title("第 7 步：幂等 —— 同一批再跑一次")
        again = dedupe_sessions(forwards, existing=load_sessions(sessions_path))
        counts = action_counts(again)
        print(f"  动作统计：{counts}")
        print()
        note("全部「无变化」：第二次处理同一批转发不会产生任何写入。")
        note("去重的幂等性来自主键的稳定性 —— 同样的门店、剧本、时间永远算出同样的哈希。")

    # ----------------------------------------------------------------------
    title("对照：真实 data/sessions.xlsx（只读）")
    if not SESSIONS_XLSX.exists():
        print("  文件不存在，跳过。先运行 scripts/02_storage_demo.py 生成它。")
        return 0

    before_mtime = SESSIONS_XLSX.stat().st_mtime_ns
    real = load_sessions(SESSIONS_XLSX)
    if real.empty:
        print("  真实表是空的，跳过。")
        return 0

    result = dedupe_sessions(real, existing=real)
    after_mtime = SESSIONS_XLSX.stat().st_mtime_ns
    print(f"  文件行数：{len(real)} -> 去重后 {len(result)}")
    print(f"  动作统计：{action_counts(result)}")
    print(f"  文件修改时间未变：{before_mtime == after_mtime}")
    note("自比对的结果全部是「无变化」—— 表里现有的行各自认出了自己。")
    note("如果某天文件里真的出现重复行，这里会显示合并后的行数变少。")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

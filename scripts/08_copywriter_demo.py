"""工具 7 演示：营销文案生成。

演示七件事：
  1. Prompt 与白名单：public_view() 后还剩 contribution_per_seat，所以必须再走 COPY_FIELDS
  2. 选场：高危 / 关注 / 普通 / 满员 / 过期，谁入选、谁被跳过
  3. 逐级回退：拿掉高危后，关注与普通仍然能把名额补满
  4. 生成两版：FakeLLM 返回合法 JSON，打印社群短版与朋友圈长版
  5. 生成后校验：内部财务词 -> internal_leak；未授权价格 / 折扣 -> price_mismatch
  6. 坏回复：坏 JSON、缺 moments、模型异常，全部结构化失败并脱敏
  7. 真实 data/ 只读对照：读取真实表尝试选场，前后 mtime 不变

运行：
  PYTHONUTF8=1 "C:/Users/Lenovo/anaconda3/python.exe" scripts/08_copywriter_demo.py

真实调用（可选，需要 .env 已配置 DEEPSEEK_API_KEY）：
  PYTHONUTF8=1 "C:/Users/Lenovo/anaconda3/python.exe" scripts/08_copywriter_demo.py --live

默认离线：使用脚本里的 FakeLLM，不发网络请求、不需要 API key。
--live 只打印「检测到 DEEPSEEK_API_KEY：是/否」，绝不打印 key 的值。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import unicodedata
from pathlib import Path

# Windows 控制台默认 stdout / stderr 编码是 GBK，中文会全部乱码。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd  # noqa: E402

from app.config import BOOKINGS_XLSX, SESSIONS_XLSX  # noqa: E402
from app.tools.copywriter import (  # noqa: E402
    COPY_FIELDS,
    COPY_PROMPT_FIELDS,
    build_copy_messages,
    generate_copy,
    select_copy_sessions,
)
from app.tools.pricing import price_sessions, public_view  # noqa: E402
from app.tools.storage import (  # noqa: E402
    STATUS_CANCELLED,
    STATUS_PENDING,
    _coerce_types,
    load_sessions_with_counts,
)

NOW = pd.Timestamp("2026-10-20 18:00")  # 固定下来保证输出稳定
STORE = "迷雾剧场"


# ---------------------------------------------------------------------------
# 显示工具（与 06 / 07 演示相同：按终端显示宽度对齐）
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


def _dt(value) -> str:
    return "—" if pd.isna(value) else pd.to_datetime(value).strftime("%m-%d %H:%M")


def _num(value) -> str:
    return "—" if pd.isna(value) else str(int(value))


# ---------------------------------------------------------------------------
# FakeLLM 与合成场次
# ---------------------------------------------------------------------------

class FakeLLM:
    """只实现 .invoke(messages) —— 工具 7 只依赖这一个方法。"""

    def __init__(self, content: str = "", *, error: Exception | None = None):
        self.content = content
        self.error = error
        self.calls: list[list[dict[str, str]]] = []

    def invoke(self, messages):
        self.calls.append(messages)
        if self.error is not None:
            raise self.error
        return self.content


def _表(rows) -> pd.DataFrame:
    defaults = {
        "store_name": STORE,
        "total_seats": 8,
        "booked_seats": 0,
        "list_price": 168,
        "status": STATUS_PENDING,
    }
    return _coerce_types(pd.DataFrame([{**defaults, **row} for row in rows]))


def _场(*, 剩几小时=24, 总位=8, 已约=0, 原价=168, 名="某场", 状态=STATUS_PENDING) -> dict:
    return {
        "store_name": STORE,
        "script_name": 名,
        "start_time": NOW + pd.Timedelta(hours=剩几小时),
        "total_seats": 总位,
        "booked_seats": 已约,
        "list_price": 原价,
        "status": 状态,
    }


def _合成表() -> pd.DataFrame:
    return _表([
        _场(名="雾隐村", 剩几小时=2, 已约=7),       # 高危：2 小时后、还剩 1 位
        _场(名="年轮", 剩几小时=4, 已约=4),         # 关注：4 小时后、还剩 4 位、中让 88%
        _场(名="漓川怪谈簿", 剩几小时=24, 已约=0),  # 普通：次日、还剩 8 位
    ])


def _回复(community: str, moments: str, warnings=None) -> str:
    return json.dumps(
        {"community": community, "moments": moments, "warnings": warnings or []},
        ensure_ascii=False,
    )


def _选场名(selection) -> list[str]:
    return [record["script_name"] for record in selection.sessions]


def _公开行(record) -> list:
    return [
        record["script_name"],
        record["start_time"] or "—",
        _num(record["remaining_seats"]),
        _num(record["list_price"]),
        _num(record["recommended_price"]),
        _num(record["seat_saving"]),
    ]


# ---------------------------------------------------------------------------
# 七步演示
# ---------------------------------------------------------------------------

def step1_prompt() -> None:
    title("第 1 步｜Prompt 与白名单：public_view() 之后为什么还要再挡一层")

    table = _合成表()
    selection = select_copy_sessions(table, now=NOW)
    messages = build_copy_messages(selection.sessions, now=NOW)

    scored = price_sessions(table, now=NOW)
    public = public_view(scored)
    note("public_view() 已经剔除 floor_price / break_even_price / sunk_cost / profit_if_sold_out")
    note("但 public_view() 仍保留 contribution_per_seat："
         + ("在" if "contribution_per_seat" in public.columns else "不在"))
    note("所以工具 7 只把 COPY_FIELDS 白名单写进 Prompt：")
    print("     " + "、".join(COPY_FIELDS))
    note("发给模型的字段去掉 session_id：")
    print("     " + "、".join(COPY_PROMPT_FIELDS))

    print("\n  --- system prompt ---")
    print(messages[0]["content"])
    print("\n  --- user prompt ---")
    print(messages[1]["content"])


def step2_select() -> tuple[pd.DataFrame, object]:
    title("第 2 步｜选场：高危优先，满员与过期不进入文案")

    table = _表([
        _场(名="雾隐村", 剩几小时=2, 已约=7),
        _场(名="年轮", 剩几小时=4, 已约=4),
        _场(名="漓川怪谈簿", 剩几小时=24, 已约=0),
        _场(名="满员本", 剩几小时=24, 已约=8),
        _场(名="已过期本", 剩几小时=-1, 已约=0),
        _场(名="已取消本", 剩几小时=24, 已约=0, 状态=STATUS_CANCELLED),
    ])
    selection = select_copy_sessions(table, now=NOW)
    selected = set(_选场名(selection))

    rows = []
    for row in table.to_dict("records"):
        name = row["script_name"]
        if name in selected:
            action = "入选"
        elif pd.to_datetime(row["start_time"]) <= NOW:
            action = "已过期"
        elif not pd.isna(row["remaining_seats"]) and int(row["remaining_seats"]) <= 0:
            action = "满员跳过"
        elif row["status"] == STATUS_CANCELLED:
            action = "已取消"
        else:
            action = "未入选"
        rows.append([
            name,
            _dt(row["start_time"]),
            _num(row["remaining_seats"]),
            _num(row["list_price"]),
            action,
        ])

    show_table(
        ["剧本", "开场", "剩余", "原价", "结果"],
        rows,
        [14, 12, 6, 6, 10],
        [False, False, True, True, False],
    )
    note(f"本次选中 {len(selection.sessions)} 场：" + "、".join(_选场名(selection)))
    for warning in selection.warnings:
        note(warning)
    note("已过期 / 已取消由 price_sessions 内部调用工具 3 剔除；满员在这里被排除。")
    return table, selection


def step3_fallback() -> None:
    title("第 3 步｜逐级回退：没有高危时，关注 + 普通仍然能生成")

    table = _表([
        _场(名="年轮", 剩几小时=4, 已约=4),
        _场(名="漓川怪谈簿", 剩几小时=24, 已约=0),
    ])
    selection = select_copy_sessions(table, now=NOW)

    show_table(
        ["剧本", "开场", "剩余", "原价", "推荐价", "省下"],
        [_公开行(record) for record in selection.sessions],
        [14, 18, 6, 6, 7, 6],
        [False, False, True, True, True, True],
    )
    note("选中顺序：" + " → ".join(_选场名(selection)))
    for warning in selection.warnings:
        note(warning)
    note("低级别只用于补足名额，不会排到高危前面。")


def step4_generate() -> None:
    title("第 4 步｜生成两版：社群短版 + 朋友圈长版")

    table = _表([
        _场(名="年轮", 剩几小时=4, 已约=4),
        _场(名="漓川怪谈簿", 剩几小时=24, 已约=0),
    ])
    selection = select_copy_sessions(table, now=NOW)
    first, second = selection.sessions

    community = (
        "今晚迷雾剧场还有两车可以上车：\n"
        f"《{first['script_name']}》{first['start_time']} 开场，还剩 {first['remaining_seats']} 位，"
        f"原价 {int(first['list_price'])}，现价 {int(first['recommended_price'])} 元/位，"
        f"立省 {int(first['seat_saving'])} 元；\n"
        f"《{second['script_name']}》{second['start_time']} 开场，还剩 {second['remaining_seats']} 位，"
        f"原价 {int(second['list_price'])}。\n"
        "想玩的朋友直接来约～"
    )
    moments = (
        "今晚的年轮和漓川怪谈簿都还差人，来一把？\n\n"
        f"《{first['script_name']}》{first['start_time']} 开场，还剩 {first['remaining_seats']} 位，"
        f"原价 {int(first['list_price'])}，现价 {int(first['recommended_price'])} 元/位，"
        f"立省 {int(first['seat_saving'])} 元。\n"
        f"《{second['script_name']}》{second['start_time']} 开场，还剩 {second['remaining_seats']} 位，"
        f"原价 {int(second['list_price'])}。\n\n"
        "位置不多，想上车的朋友直接私信我～🎭✨"
    )

    model = FakeLLM(_回复(community, moments, warnings=["地址请店长补一句"]))
    result = generate_copy(table, model=model, now=NOW)

    note(f"ok = {result.ok}，error_code = {result.error_code}")
    print("\n  --- 社群短版 ---")
    print(result.community)
    print("\n  --- 朋友圈长版 ---")
    print(result.moments)
    print("\n  --- 结果里的公开场次 ---")
    show_table(
        ["剧本", "开场", "剩余", "原价", "推荐价", "省下"],
        [_公开行(record) for record in result.sessions],
        [14, 18, 6, 6, 7, 6],
        [False, False, True, True, True, True],
    )
    for warning in result.warnings:
        note(warning)
    note("店长必须人工复核后再复制发布；本工具不自动发送微信、不自动发布。")


def step5_guardrails() -> None:
    title("第 5 步｜生成后校验：内部财务词与未授权价格都整次失败")

    table = _表([
        _场(名="年轮", 剩几小时=4, 已约=4),
    ])

    cases = [
        ("内部财务词", _回复("这场保本价 20 元，快来。", "年轮 20:00 开场，欢迎来玩。")),
        ("未授权价格", _回复("今晚只要 99 元。", "年轮 20:00 开场，欢迎来玩。")),
        ("未授权折扣", _回复("今晚打 8 折。", "年轮 20:00 开场，欢迎来玩。")),
    ]

    for label, content in cases:
        result = generate_copy(table, model=FakeLLM(content), now=NOW)
        note(f"{label} -> ok={result.ok}，error_code={result.error_code}")
        note(f"error：{result.error}")
        note(f"community is None：{result.community is None}；"
             f"moments is None：{result.moments is None}")
        print()

    note("两类失败都不返回半套文案；店长看不到可复制内容，就不会误发。")


def step6_bad_response() -> None:
    title("第 6 步｜坏回复：坏 JSON / 缺字段 / 模型异常全部结构化失败")

    table = _表([_场(名="年轮", 剩几小时=4, 已约=4)])

    cases = [
        ("坏 JSON", FakeLLM("{坏 JSON")),
        ("缺 moments", FakeLLM(json.dumps({"community": "只有短版"}, ensure_ascii=False))),
        ("模型异常", FakeLLM(error=RuntimeError("模拟网络错误 sk-demo123456789"))),
    ]

    for label, model in cases:
        result = generate_copy(table, model=model, now=NOW)
        note(f"{label} -> error_code={result.error_code}")
        note(f"error：{result.error}")
        if result.raw_snippet:
            note(f"raw_snippet：{result.raw_snippet[:80]}")
        if label == "模型异常":
            note("错误信息已脱敏："
                 + ("是" if "sk-demo123456789" not in (result.error or "") else "否"))
        print()

    note("业务失败不向调用方抛异常；KeyboardInterrupt / SystemExit 例外，会原样抛出。")


def _mtime(path: Path) -> int | None:
    try:
        return path.stat().st_mtime_ns
    except FileNotFoundError:
        return None


def step7_real_data() -> None:
    title("第 7 步｜真实 data/ 只读对照：选场但不写文件")

    paths = [SESSIONS_XLSX, BOOKINGS_XLSX]
    before = {path: _mtime(path) for path in paths}

    try:
        real = load_sessions_with_counts()
    except Exception as exc:
        note(f"读取真实场次表失败：{type(exc).__name__}: {exc}")
        return

    note(f"真实场次表：{len(real)} 行")
    if real.empty:
        note("表为空，跳过选场。")
    else:
        show_table(
            ["门店", "剧本", "开场", "总位", "已约", "剩余", "原价", "状态"],
            [
                [
                    row["store_name"], row["script_name"], _dt(row["start_time"]),
                    _num(row["total_seats"]), _num(row["booked_seats"]),
                    _num(row["remaining_seats"]), _num(row["list_price"]), row["status"],
                ]
                for row in real.to_dict("records")
            ],
            [12, 14, 12, 5, 5, 5, 6, 8],
            [False, False, False, True, True, True, True, False],
        )

        try:
            selection = select_copy_sessions(real, now=NOW)
        except ValueError as exc:
            note(f"选场被拒绝（符合单门店约束）：{exc}")
        else:
            if selection.empty:
                note("按固定演示时间，真实表里没有可推广场次。")
            else:
                note("选中的场次：" + "、".join(_选场名(selection)))
            for warning in selection.warnings:
                note(warning)

    after = {path: _mtime(path) for path in paths}
    changed = [path.name for path in paths if before[path] != after[path]]
    if changed:
        note("⚠️ 文件 mtime 发生变化：" + "、".join(changed))
    else:
        note("真实 data/ 的 mtime 未变化：本步骤只读，没有落盘。")


def run_live() -> None:
    title("可选｜--live：真实调用一次 DeepSeek")

    key = os.getenv("DEEPSEEK_API_KEY", "").strip()
    print(f"  检测到 DEEPSEEK_API_KEY：{'是' if key else '否'}")
    if not key:
        note("未检测到 key，跳过真实调用。")
        note("配置方式：把 .env.example 复制为 .env，填入 DEEPSEEK_API_KEY=sk-xxxx。")
        note("不要在聊天或终端输出 key 的值。")
        return

    note("将对合成场次调用一次真实模型；失败也只打印结构化错误。")
    result = generate_copy(_合成表(), now=NOW)
    note(f"ok = {result.ok}，error_code = {result.error_code}")
    if result.ok:
        print("\n  --- 社群短版 ---")
        print(result.community)
        print("\n  --- 朋友圈长版 ---")
        print(result.moments)
    else:
        note(f"error：{result.error}")
    for warning in result.warnings:
        note(warning)


def main() -> int:
    parser = argparse.ArgumentParser(description="工具 7 营销文案生成演示")
    parser.add_argument(
        "--live",
        action="store_true",
        help="真实调用 DeepSeek（需要 .env 中配置 DEEPSEEK_API_KEY）",
    )
    args = parser.parse_args()

    step1_prompt()
    step2_select()
    step3_fallback()
    step4_generate()
    step5_guardrails()
    step6_bad_response()
    step7_real_data()

    if args.live:
        run_live()
    else:
        print()
        note("以上全部离线运行，使用脚本内 FakeLLM；加 --live 才会真实调用模型。")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

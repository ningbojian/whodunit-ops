"""工具 5 演示：动态折扣定价。

演示七件事：
  1. 定价结果 —— 每场该让多少
  2. **手算对账** —— 挑一场把每个数字算一遍，验证模型没有黑箱
  3. **核心** —— 同一场在工具 4 和工具 5 里结论相反，且必须相反
  4. 底价外泄防护 —— public_view() 前后列名对比
  5. 标定视图 —— 所有参数与阶梯表，现算
  6. 原价缺失 / 空位未知的兜底
  7. 对照真实 data/

运行：
  PYTHONUTF8=1 "C:/Users/Lenovo/anaconda3/python.exe" scripts/05_pricing_demo.py

本脚本**不写 data/ 目录**：演示数据建在临时目录里，结束自动清理。
只在最后一步读一下真实的 data/ 做对照，文件不存在就跳过。
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

from app.config import BOOKINGS_XLSX, SESSIONS_XLSX  # noqa: E402
from app.tools.pricing import (  # noqa: E402
    DEFAULT_PRICING_PARAMS,
    INTERNAL_COLUMNS,
    PricingParams,
    break_even_price,
    contribution_per_seat,
    discount_tier,
    fixed_cost,
    floor_price,
    price_sessions,
    public_view,
)
from app.tools.risk import NATURAL_MULTIPLE  # noqa: E402
from app.tools.storage import (  # noqa: E402
    STATUS_CANCELLED,
    load_sessions_with_counts,
    upsert_booking,
    upsert_sessions,
)

NOW = pd.Timestamp("2026-10-20 19:30:00")

HEADERS = ["剧本", "开场", "总位", "已约", "空位", "剩(时)", "倍数", "原价",
           "让利档", "推荐价", "每座贡献"]


def title(text: str) -> None:
    print()
    print("=" * 92)
    print(text)
    print("=" * 92)


def note(text: str) -> None:
    print(f"  >> {text}")


def _fmt(v, pattern: str) -> str:
    return "—" if v is None or pd.isna(v) else pattern.format(v)


def _w(text) -> int:
    """终端显示宽度：中日韩全角字符占 2 列，其余占 1 列。

    直接靠 f-string 的宽度补空格会错位 —— Python 按字符数补，不按显示宽度补。
    """
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in str(text))


def _cell(value, width: int, right: bool) -> str:
    text = str(value)
    pad = " " * max(width - _w(text), 0)
    return pad + text if right else text + pad


def _line(cells, widths, rights) -> str:
    return " ".join(_cell(v, w, r) for v, w, r in zip(cells, widths, rights))


def show_table(headers, rows, widths, rights=None) -> None:
    """按显示宽度对齐的中文表格。"""
    rights = rights or [False] * len(headers)
    print("  " + _line(headers, widths, rights))
    print("  " + "─" * (sum(widths) + len(widths) - 1))
    for row in rows:
        print("  " + _line(row, widths, rights))


def show_pricing(df: pd.DataFrame) -> None:
    if df.empty:
        print("  （空）")
        return

    rows = []
    for _, r in df.iterrows():
        rows.append([
            r["script_name"],
            pd.to_datetime(r["start_time"]).strftime("%m-%d %H:%M"),
            "—" if pd.isna(r["total_seats"]) else int(r["total_seats"]),
            "—" if pd.isna(r["booked_seats"]) else int(r["booked_seats"]),
            "—" if pd.isna(r["remaining_seats"]) else int(r["remaining_seats"]),
            _fmt(r["hours_left"], "{:.1f}"),
            _fmt(r["rate_multiple"], "{:.2f}"),
            _fmt(r["list_price"], "{:.0f}"),
            r["discount_tier"],
            _fmt(r["recommended_price"], "{:.0f}"),
            _fmt(r["contribution_per_seat"], "{:.0f}"),
        ])
    show_table(
        HEADERS, rows,
        widths=[14, 12, 5, 5, 5, 7, 6, 6, 10, 8, 9],
        rights=[False, False, True, True, True, True, True, True, False, True, True],
    )


# ===========================================================================
# 造演示数据
# ===========================================================================

# (剧本名, 开场时间, 总位, 约了几人, 原价)
#
# 数据是按倍数列成阶梯的，好让「倍数 -> 档位」的变化一眼看出来。
示例场次 = [
    ("满员的场",     "2026-10-20 20:00", 6,  6,  168),   # 倍数 0    满员，无位可让
    ("差1人剩2小时", "2026-10-20 21:30", 8,  7,  168),   # 倍数 1.0  原价（工具4判"高危"）
    ("差2人剩2小时", "2026-10-20 21:30", 8,  6,  168),   # 倍数 2.0  中让 88 折
    ("差4人剩2小时", "2026-10-20 21:30", 6,  2,  168),   # 倍数 4.0  重让 80 折（工具4判"正常"）
    ("差3人剩5小时", "2026-10-21 00:30", 12, 9,  268),   # 倍数 1.2  轻让 95 折
    ("差1人剩5小时", "2026-10-21 00:30", 8,  7,  198),   # 倍数 0.4  原价
    ("明天的空场",   "2026-10-21 19:00", 8,  0,  168),   # 倍数 0.68 原价
    ("没写原价的场", "2026-10-20 22:00", 10, 4,  None),  # 倍数 4.8  无法定价
    ("昨天那场",     "2026-10-19 19:00", 8,  3,  188),   # 已开场，不出现
    ("取消了的场",   "2026-10-20 21:00", 6,  1,  198),   # 已取消，不出现
]


def build_demo_data(sessions_path: Path, bookings_path: Path) -> pd.DataFrame:
    rows = [
        {"store_name": "迷雾剧场", "script_name": 名, "script_type": "推理",
         "start_time": 时间, "total_seats": 总位, "list_price": 价格,
         **({"status": STATUS_CANCELLED} if 名 == "取消了的场" else {})}
        for 名, 时间, 总位, _, 价格 in 示例场次
    ]
    merged = upsert_sessions(rows, sessions_path)
    id_of = dict(zip(merged["script_name"], merged["session_id"]))

    for 名, _, _, 人数, _ in 示例场次:
        for i in range(人数):
            upsert_booking(id_of[名], f"玩家{i + 1:02d}", path=bookings_path)

    return load_sessions_with_counts(sessions_path, bookings_path)


# ===========================================================================
# 标定视图
# ===========================================================================

def print_calibration(params: PricingParams) -> None:
    """把当前参数摊开成表 —— 现算，不手抄文档。"""
    print("  ① 固定成本 —— 这场戏开起来就花出去的钱\n")
    show_table(
        ["总位", "固定成本", "说明"],
        [
            [f"<= {params.medium_room_seats - 1}", f"{params.small_fixed_cost:.0f} 元", "短本"],
            [f"{params.medium_room_seats} ~ {params.large_room_seats - 1}",
             f"{params.medium_fixed_cost:.0f} 元", "中本"],
            [f">= {params.large_room_seats}", f"{params.large_fixed_cost:.0f} 元", "大本"],
        ],
        widths=[10, 12, 8],
        rights=[True, True, False],
    )
    print(f"\n       验算：6 位 -> {fixed_cost(6, params):.0f}    8 位 -> {fixed_cost(8, params):.0f}"
          f"    12 位 -> {fixed_cost(12, params):.0f}")
    note("分档线沿用工具 4 的 medium_room_seats / large_room_seats —— 两个工具对")
    note("「小/中/大本」必须是同一套线，否则同一场在两边的认知会打架。")
    note("总位是时长的代理：12 人机制本和 12 人欢乐本时长可能差一倍，成本却算成一样。")

    print()
    print("  ② 每人变动成本 = 边际底价 —— 多来一个人才多花的钱")
    print(f"       floor_price = {floor_price(params):.0f} 元/人")
    note("低于它，多卖一个位子多亏一笔。这条线**与人数和原价都无关**，")
    note("是推出来的硬约束 —— 和工具 4 的「倍数 ≤ 1」同一性质。")

    print()
    print("  ③ 折扣阶梯（以原价 168 元为例，现算）\n")
    stair_rows = []
    for label, m in [
        (f"<= {NATURAL_MULTIPLE:.1f}", 0.8),
        (f"{NATURAL_MULTIPLE:.1f} ~ {params.light_multiple:.1f}", 1.3),
        (f"{params.light_multiple:.1f} ~ {params.medium_multiple:.1f}", 2.0),
        (f"> {params.medium_multiple:.1f}", 6.0),
    ]:
        tier, rate = discount_tier(m, params)
        price = float(round(168 * rate))
        stair_rows.append([label, tier, f"{rate:.0%}", f"{price:.0f}",
                           f"{168 - price:.0f}", f"{contribution_per_seat(price, params):.0f}"])
    show_table(["倍数", "档位", "折扣", "推荐价", "每座让", "每座贡献"], stair_rows,
               widths=[12, 8, 7, 9, 8, 10],
               rights=[True, False, True, True, True, True])
    print()
    note("「每座贡献」= 推荐价 − 变动成本，是整张表最有行动含义的数：")
    note("它直接告诉店长「每拉来一个人，这场少亏多少钱」。")
    note("折扣档位本身是**经验值**，不是算出来的 —— 模型只证明「不破底价就不亏」，")
    note("它没有算出「打几折能多来几个人」，那需要价格弹性数据，门店没有。")

    print()
    print(f"  ④ 两条保本线（10 人本，固定 {fixed_cost(10, params):.0f}）—— 别混用\n")
    show_table(
        ["到客人数", "均摊保本价", "边际底价"],
        [[f"{n} 人", f"{break_even_price(n, 10, params):.1f}", f"{floor_price(params):.1f}"]
         for n in [10, 8, 5, 3, 2]],
        widths=[10, 12, 10],
        rights=[True, True, True],
    )
    print()
    note("均摊保本价随人数剧烈变化：人越少，固定成本摊得越薄，它越高。")
    note("到客 2 人时它 260 元，已经高于原价 168 —— 这时它**不能**当售卖下限用。")
    note("所以必须有 floor_price 这条与人数无关的硬线，两条线回答的是不同问题。")


# ===========================================================================
# 主流程
# ===========================================================================

def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        sessions_path = tmp / "sessions.xlsx"
        bookings_path = tmp / "bookings.xlsx"

        raw = build_demo_data(sessions_path, bookings_path)
        priced = price_sessions(raw, now=NOW)

        # ------------------------------------------------------------------
        title(f"演示数据（{len(raw)} 场，按 now = {NOW} 评估）")
        show_table(
            ["剧本", "开场", "总位", "已约", "空位", "原价", "状态"],
            [[r["script_name"], pd.to_datetime(r["start_time"]).strftime("%m-%d %H:%M"),
              int(r["total_seats"]), int(r["booked_seats"]), int(r["remaining_seats"]),
              _fmt(r["list_price"], "{:.0f}"), r["status"]]
             for _, r in raw.iterrows()],
            widths=[14, 12, 5, 5, 5, 6, 8],
            rights=[False, False, True, True, True, True, False],
        )
        note("原价来自场次表的 list_price 列；成本不在表里，全部来自 PricingParams。")

        # ------------------------------------------------------------------
        title("第 1 步：定价结果")
        show_pricing(priced)
        print()
        note("顺序**沿用工具 4 的优先级排序**，不按价格重排 —— 最该动手的场次排在最前。")
        note("「没写原价的场」倍数 4.8，本该是重让，但没有原价就算不出折扣率 ——")
        note("给的是「无法定价」，不猜、不默认。第 6 步单独讲。")

        # ------------------------------------------------------------------
        title("第 2 步：手算对账 —— 挑一场把每个数字算一遍")
        目标 = "差2人剩2小时"
        r = priced.set_index("script_name").loc[目标]
        print(f"  「{目标}」：8 人本、已约 6、原价 168、距开场 2 小时\n")
        print("    ① 还差几个人        = 8 − 6            = 2 个空位")
        print("    ② 需要的到客速率    = 2 ÷ 2            = 1.00 人/小时")
        print(f"    ③ 倍数              = 1.00 ÷ 0.5      = {r['rate_multiple']:.2f}")
        print(f"    ④ 让利档            = 倍数 2.0 落在 1.5~2.5 -> {r['discount_tier']}")
        print(f"    ⑤ 推荐价            = 168 × 0.88 = 147.84 -> 四舍五入 {r['recommended_price']:.0f} 元")
        print(f"    ⑥ 每座让掉          = 168 − 148       = {r['seat_saving']:.0f} 元")
        print(f"    ⑦ 每座贡献          = 148 − 20        = {r['contribution_per_seat']:.0f} 元")
        print(f"    ⑧ 固定成本          = 8 位 -> 中本    = {r['sunk_cost']:.0f} 元")
        print(f"    ⑨ 边际底价          = 每人变动成本    = {r['floor_price']:.0f} 元")
        print(f"    ⑩ 满员保本价        = 340 ÷ 8 + 20    = {r['break_even_price']:.1f} 元")
        print(f"    ⑪ 满座收入          = 6×168 + 2×148   = {6 * 168 + 2 * 148:.0f} 元")
        print(f"    ⑫ 满座成本          = 340 + 20×8      = {340 + 20 * 8:.0f} 元")
        print(f"    ⑬ 满座利润          = 1304 − 500      = {r['profit_if_sold_out']:.0f} 元")
        print()
        note("模型里没有黑箱：每个数都能手算出来，测试也是按这些式子断言的。")
        note("⑪ 里的 6 个已约客人按**原价**算 —— 折扣只作用于剩下 2 个空位。")
        note("这正是「只对剩余空位限时特价」的意思：已经按 168 订了的客人不受影响，")
        note("门店不必退差价，也不会得罪老客户。")

        # ------------------------------------------------------------------
        title("第 3 步：同一场，工具 4 说「别动手」，工具 5 说「打 8 折」")
        show_table(
            ["剧本", "倍数", "工具4·该先救", "工具4·可救性", "工具5·让利", "推荐价"],
            [[r["script_name"], _fmt(r["rate_multiple"], "{:.2f}"), r["priority_level"],
              int(r["rescue_level"]), r["discount_tier"], _fmt(r["recommended_price"], "{:.0f}")]
             for _, r in priced.iterrows()],
            widths=[14, 7, 15, 14, 12, 9],
            rights=[False, True, True, True, True, True],
        )
        print()
        note("看两行就能看出矛盾，也就能看出为什么不矛盾：")
        print()
        note("「差4人剩2小时」：工具 4 判「正常，别浪费推广力气」（可救性 0），")
        note("                 工具 5 判「重让 80 折」。")
        note("  -> 工具 4 问的是**填不填得满**：倍数 4.0，填不满。")
        note("  -> 工具 5 问的是**能不能少亏**：填不满不等于不该少亏。")
        note(f"     每拉来一个人少亏 {priced.set_index('script_name').loc['差4人剩2小时']['contribution_per_seat']:.0f} 元，"
             "多来两个就少亏两百多。")
        print()
        note("「差1人剩2小时」：工具 4 判「高危，现在就动手」，")
        note("                 工具 5 判「原价，不动价格」。")
        note("  -> 工具 4 说的动手是**推广**（发朋友圈），不是让利。")
        note("  -> 倍数恰好 1.0，自然流量就够 —— 让利是白让。")
        print()
        note("一句话：**要推广 ≠ 要让利**。两个工具目标函数不同，结论本来就该不同。")

        # ------------------------------------------------------------------
        title("第 4 步：底价外泄防护 —— public_view()")
        print(f"  完整结果共 {len(priced.columns)} 列：")
        print(f"    {', '.join(priced.columns)}")
        print()
        pub = public_view(priced)
        print(f"  对外视图共 {len(pub.columns)} 列：")
        print(f"    {', '.join(pub.columns)}")
        print()
        print(f"  被剔除的内部列（{len(INTERNAL_COLUMNS)} 个）：")
        print(f"    {', '.join(INTERNAL_COLUMNS)}")
        note("公共不变量：门店保本底价仅内部可见，不进入任何对外文案。")
        note("靠注释提醒工具 7「别写底价」迟早会漏，漏一次就是把门店的底牌发到朋友圈。")
        note("public_view() 之后，下游手里的 DataFrame **根本没有**这一列，想写也写不出来。")

        # ------------------------------------------------------------------
        title("第 5 步：标定视图 —— 当前所有参数")
        note("下面的默认值是**占位值**，没有经过任何真实门店数据标定。")
        note("结构（固定成本 + 变动成本、让利不破底价、人越少保本价越高）可论证；")
        note("具体是 340 还是 400、88 折还是 9 折，只能靠门店自己的账本标出来。")
        print()
        print_calibration(DEFAULT_PRICING_PARAMS)

        # ------------------------------------------------------------------
        title("第 6 步：没有原价 / 不知道空位 —— 不猜")
        row = priced.set_index("script_name").loc["没写原价的场"]
        show_table(
            ["剧本", "倍数", "档位", "推荐价", "说明"],
            [[row.name, f"{row['rate_multiple']:.2f}", row["discount_tier"], "—", row["price_note"]]],
            widths=[14, 7, 10, 9, 10],
            rights=[False, True, False, True, False],
        )
        print()
        note("倍数 4.8，本来该判重让 —— 但没有原价，折扣率无从谈起。")
        note("给的是「无法定价」而不是「按 100 元估一个」。")
        note("成本侧照样算得出来（sunk_cost / floor_price 不依赖原价），")
        note("所以店长至少知道「这场黄了亏多少」，只是不知道「该卖多少」。")

        # ------------------------------------------------------------------
        title("第 7 步：改一个参数，推荐价立刻跟着变")
        观察 = "差2人剩2小时"
        print(f"  看「{观察}」（倍数 2.0，落在中让档）在中让折扣取三个不同值时的结果：\n")
        rows = []
        for d in [0.95, 0.88, 0.80]:
            row = (price_sessions(raw, now=NOW, params=PricingParams(medium_discount=d))
                   .set_index("script_name").loc[观察])
            rows.append([f"{d:.0%}", row["discount_tier"], f"{row['recommended_price']:.0f}",
                         f"{row['seat_saving']:.0f}", f"{row['contribution_per_seat']:.0f}"])
        show_table(["中让折扣", "档位", "推荐价", "每座让", "每座贡献"], rows,
                   widths=[10, 10, 9, 8, 10],
                   rights=[True, False, True, True, True])
        print()
        note("整条链路是现算的：改 PricingParams 立刻改变推荐价，文档里的表不会和代码脱节。")
        note("注意让到 8 折每座仍贡献 114 元 —— 离边际底价 20 元还很远。")
        note("数学上还可以让得更狠，是「价格保护线」让它停在 80 折，这是经营策略不是算术。")

    # ----------------------------------------------------------------------
    title("对照：data/ 下的真实数据用真实的「现在」定价")
    if not SESSIONS_XLSX.exists():
        print("  文件不存在，跳过。先运行 scripts/02_storage_demo.py 生成它。")
    else:
        real = load_sessions_with_counts()
        print(f"  真实现在时间：{pd.Timestamp.now().floor('s')}\n")
        if not BOOKINGS_XLSX.exists():
            print(f"  （{BOOKINGS_XLSX.name} 不存在，已约人数全部按 0 算）\n")
        show_pricing(price_sessions(real))
        note("02_storage_demo.py 造的场次都在 10-20 之后，所以现在还没开场。")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

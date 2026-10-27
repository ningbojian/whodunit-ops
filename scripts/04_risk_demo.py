"""工具 4 演示：空场风险识别。

演示八件事：
  1. 评估结果 —— 两个输出（会不会黄 / 该先救哪场）并排看
  2. **核心** —— 为什么排序不按危险度来（上一版就是排反的）
  3. 硬规则 —— 满员的场次，剩得再少也正常
  4. 标定视图 —— 把当前所有阈值摊开成一张表，给店长看
  5. 推进 now —— 同一批数据不落盘，等级随时间自己往上走
  6. 空位填满 —— 高危自动消失，没人需要去"取消标记"
  7. **λ 敏感性** —— 占位值到底有多大影响，不靠嘴说，跑给你看
  8. 给下游的接口

运行：
  PYTHONUTF8=1 "C:/Users/Lenovo/anaconda3/python.exe" scripts/04_risk_demo.py

本脚本**不写 data/ 目录**：演示数据建在临时目录里，结束自动清理。
只在最后一步读一下真实的 data/ 做对照，文件不存在就跳过。

关于演示数据：已约人数是通过 upsert_booking 一条条真实写入预约明细表的，
不是直接塞进场次表的 booked_seats 列 —— 这样走的才是真实链路
（load_sessions_with_counts 会从预约明细重算人数）。
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

# Windows 控制台默认 stdout / stderr 编码是 GBK，中文会全部乱码。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

# 让脚本能 import 到项目根目录下的 app 包（scripts/ 与 app/ 平级）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd  # noqa: E402

from app.config import BOOKINGS_XLSX, SESSIONS_XLSX  # noqa: E402
from app.tools.risk import (  # noqa: E402
    DEFAULT_RISK_PARAMS,
    PRIORITY_HIGH,
    PRIORITY_WATCH,
    RiskParams,
    assess_sessions,
    fail_risk,
    high_risk_only,
    priority_level,
    rescue_level,
    time_level,
)  # noqa: E402
from app.tools.storage import (  # noqa: E402
    STATUS_CANCELLED,
    load_sessions_with_counts,
    upsert_booking,
    upsert_sessions,
)

# 演示用的"现在"：写死，这样每次运行结果完全一样。
NOW = pd.Timestamp("2026-10-20 19:30:00")

COLS = [
    "script_name", "start_time", "total_seats", "booked_seats", "remaining_seats",
    "hours_left", "needed_rate", "rate_multiple", "fail_risk", "priority_score",
    "priority_level",
]

HEADERS = ["剧本", "开场", "总位", "已约", "空位", "剩(时)", "需(人/时)", "倍数",
           "会不会黄", "分", "该先救哪场"]


def title(text: str) -> None:
    print()
    print("=" * 86)
    print(text)
    print("=" * 86)


def note(text: str) -> None:
    print(f"  >> {text}")


def show_risk(df: pd.DataFrame) -> None:
    """打印评估结果。浮点列格式化成好读的样子。"""
    if df.empty:
        print("  （空）")
        return

    view = df[COLS].copy()
    view["start_time"] = pd.to_datetime(view["start_time"], errors="coerce").dt.strftime("%m-%d %H:%M")
    for col, fmt in [("hours_left", "{:.1f}"), ("needed_rate", "{:.2f}"),
                     ("rate_multiple", "{:.2f}")]:
        view[col] = view[col].map(lambda v, f=fmt: "—" if pd.isna(v) else f.format(v))
    view["remaining_seats"] = view["remaining_seats"].map(
        lambda v: "—" if pd.isna(v) else str(int(v)))
    view.columns = HEADERS

    print(view.to_string(index=False, col_space=5))


# ===========================================================================
# 造演示数据
# ===========================================================================

# (剧本名, 开场时间, 总位, 约了几人)
#
# 数据是**成对**造的，好让两两对比能直接看出来：
#   差1人剩2小时 / 差4人剩2小时   -> 同样紧迫，一个救得回来一个救不回来
#   差1人剩5小时 / 差4人剩5小时   -> 时间宽裕，同样的两种缺口
#   差1人剩2小时 / 差1人剩5小时   -> 同样只差一个人，时间不同
示例场次 = [
    ("满员的场",        "2026-10-20 20:00", 6,  6),   # 30 分钟后开场，但已经满了
    ("差1人剩2小时",    "2026-10-20 21:30", 8,  7),   # 紧迫2 + 可救2 -> 高危
    ("差4人剩2小时",    "2026-10-20 21:30", 6,  2),   # 紧迫2 + 可救0 -> 正常（救不回来）
    ("差1人剩5小时",    "2026-10-21 00:30", 8,  7),   # 紧迫1 + 可救2 -> 关注
    ("差4人剩5小时",    "2026-10-21 00:30", 12, 8),   # 紧迫1 + 可救1 -> 关注
    ("明天的空场",      "2026-10-21 19:00", 8,  0),   # 紧迫0 -> 正常（自己会满）
    ("明天快满了",      "2026-10-21 19:30", 10, 9),   # 紧迫0 -> 正常
    ("昨天那场",        "2026-10-19 19:00", 8,  3),   # 已开场 -> 不出现在结果里
    ("取消了的场",      "2026-10-20 21:00", 6,  1),   # 已取消 -> 不出现在结果里
]


def build_demo_data(sessions_path: Path, bookings_path: Path) -> pd.DataFrame:
    """建场次 + 逐条写预约明细，最后按真实路径读回来。"""
    rows = [
        {"store_name": "迷雾剧场", "script_name": 名, "script_type": "推理",
         "start_time": 时间, "total_seats": 总位,
         **({"status": STATUS_CANCELLED} if 名 == "取消了的场" else {})}
        for 名, 时间, 总位, _ in 示例场次
    ]
    merged = upsert_sessions(rows, sessions_path)
    id_of = dict(zip(merged["script_name"], merged["session_id"]))

    for 名, _, _, 人数 in 示例场次:
        for i in range(人数):
            upsert_booking(id_of[名], f"玩家{i + 1:02d}", path=bookings_path)

    return load_sessions_with_counts(sessions_path, bookings_path)


# ===========================================================================
# 标定视图
# ===========================================================================

def print_calibration(params: RiskParams) -> None:
    """把当前阈值摊开成一张表 —— 给店长看的「标定视图」。

    这张表是**现算的**（直接调用 time_level / rescue_level / priority_level /
    fail_risk / stake_level），不是手抄的文档。改了 params，这里立刻跟着变，
    不会出现"代码改了文档没改"。

    将来真有门店用，店长看着这张表就能说「这格不对」，
    改 RiskParams 里的数字即可，不需要动任何逻辑。
    """
    print("  ① 紧迫度 —— 距离开场还剩多久")
    u, t = params.urgent_hours, params.tight_hours
    print(f"       {f'<= {u:.1f}':<12}小时     档位 2   快开场了")
    print(f"       {f'{u:.1f} ~ {t:.1f}':<12}小时     档位 1   偏紧")
    print(f"       {f'> {t:.1f}':<12}小时     档位 0   时间还多")
    print(f"       验算：剩 {t:.1f} 时 -> 档位 {time_level(t, params)}"
          f"      剩 {u:.1f} 时 -> 档位 {time_level(u, params)}"
          f"      （边界值落在更严重的一侧）")
    note("开场时间缺失的场次按档位 0 —— 宁可不报警，也不因数据缺失造假警报。")

    print()
    print("  ② 门店常态到客速率 λ（占位值，必须标定的就是它）")
    print(f"       需要的到客速率 = 剩余空位 ÷ 剩余小时              （人/小时）")
    print(f"       λ = {params.normal_arrival_rate:g} 人/小时"
          f"  ->  4 小时里自然能来 {params.normal_arrival_rate * 4:g} 个人")
    print("       倍数 = 需要的到客速率 ÷ λ")
    print()
    print(f"       倍数 <= 1.0            会不会黄 = 低     可救性 = 2（顺手）")
    print(f"       1.0 < 倍数 <= {params.hard_multiple:<4.1f}    会不会黄 = 中     可救性 = 1（要赶）")
    print(f"       倍数 > {params.hard_multiple:<4.1f}            会不会黄 = 高     可救性 = 0（没戏）")
    note("「倍数 = 1」这条线不是拍的：需要的速率不超过门店常态，"
         "意味着自然流量刚好够。")
    note("倍数算不出来（时间缺失）时：会不会黄判「低」（不报警），"
         "可救性判 0（不推荐动手）—— 缺省动作都是「别折腾店长」。")

    print()
    print("  ③ 规模 —— 这场坐满能坐几人")
    print(f"       >= {params.large_room_seats:>2} 位     档位 2   大本")
    print(f"       {params.medium_room_seats:>2} ~ {params.large_room_seats - 1:>2} 位     档位 1   中本")
    print(f"       <= {params.medium_room_seats - 1:>2} 位     档位 0   小本")
    note("规模**不参与打分**，只做排序的第二个关键字（同样该救的，先救大的）。")
    note("反例：12 人本差 1 人剩 2 小时，若把规模计入打分会被判「高危」——"
         "可它 92% 满。")

    print()
    print("  ④ 优先级 = 紧迫档 × 可救性档（0~4 分）")
    print(f"       分数 >= {params.high_score}  ->  {PRIORITY_HIGH}"
          f"          两档都拉满：快开场了 + 还救得回来")
    print(f"       分数 >= {params.watch_score}  ->  {PRIORITY_WATCH}"
          f"            还来得及，但已经开始悬了")
    print(f"       其余      ->  正常")
    print()
    print("                     可救2(顺手)   可救1(要赶)   可救0(没戏)")
    # 行列都按"由重到轻"排：行 紧迫 2 -> 0，列 可救 2 -> 0。
    # 这样左上角就是高危，越往右下越不用管。
    for t in range(2, -1, -1):
        标签 = {0: ">6h", 1: "≤6h", 2: "≤2h"}[t]
        cells = "".join(f"{priority_level(t, r, params)[0]:^13}" for r in range(2, -1, -1))
        print(f"     紧迫{t}({标签:>3}) {cells}")
    print()
    note("满格只有左上角那一格。但真正要留意的是**另一条对角线上的两格**：")
    note("  右上角 紧迫2(快开场) + 可救0(没戏)  -> 正常。别浪费力气，改成提前通知客人。")
    note("  左下角 紧迫0(时间还多) + 可救2(顺手) -> 正常。它自己会满，不用现在推。")
    note("若把两档相加或取 max，这两格都会变成「该动手」，店长的待办列表就被灌水了。")

    print()
    print("  ⑤ 两个输出方向相反 —— 同一个倍数，两边读法不同")
    print()
    print("       倍数        " + "".join(f"{m:>7}" for m in [0.5, 1.0, 1.5, 2.0, 3.0, 8.0]))
    print("       会不会黄    " + "".join(f"{fail_risk(m, params):>8}" for m in [0.5, 1.0, 1.5, 2.0, 3.0, 8.0]))
    print("       可救性      " + "".join(f"{rescue_level(m, 1, params):>8}" for m in [0.5, 1.0, 1.5, 2.0, 3.0, 8.0]))
    print()
    note("倍数往上走：会不会黄 低->中->高（预警越来越响），")
    note("可救性 2->1->0（越来越不值得动手）。同一个数，回答两个相反的问题 ——")
    note("所以不能只算一个「危险度」，更不能拿它当排序依据。这就是第 2 步要演的。")


# ===========================================================================
# 主流程
# ===========================================================================

def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        sessions_path = tmp / "sessions.xlsx"
        bookings_path = tmp / "bookings.xlsx"

        raw = build_demo_data(sessions_path, bookings_path)

        # ------------------------------------------------------------------
        title(f"演示数据（{len(raw)} 场，按 now = {NOW} 评估）")
        view = raw[["script_name", "start_time", "total_seats", "booked_seats",
                    "remaining_seats", "status"]].copy()
        view["start_time"] = pd.to_datetime(view["start_time"]).dt.strftime("%m-%d %H:%M")
        print(view.to_string(index=False, col_space=6))
        note("已约人数是从预约明细表汇总出来的，不是场次表里存的数字。")

        assessed = assess_sessions(raw, now=NOW)

        # ------------------------------------------------------------------
        title("第 1 步：评估结果 —— 两个输出并排看")
        show_risk(assessed)
        print()
        note("「会不会黄」是预警：这场不做任何事，会不会开不起来。")
        note("「该先救哪场」是派活：现在该把力气花在哪一场。")
        print()
        note("看第一行和最后一行 —— 两场都是 21:30 开场、都剩 2 小时：")
        note("  · 差1人：倍数 1.00 -> 会不会黄「低」，但该先救「高危」")
        note("  · 差4人：倍数 4.00 -> 会不会黄「高」，但该先救「正常」")
        note("危险度更高的那场，优先级反而更低。这不是 bug，是第 2 步要讲的全部内容。")
        print()
        note("「昨天那场」和「取消了的场」不在结果里 —— assess_sessions 内部")
        note("自动调了工具 3 的 filter_active。不需要调用方记得先过滤，")
        note("因为调用方是 LLM，要求它记住步骤顺序，早晚会漏。")

        # ------------------------------------------------------------------
        title("第 2 步：为什么顺序是这样 —— 缺口大 = 危险，同时 = 难救")
        row = assessed.set_index("script_name")
        没戏的 = row.loc["差4人剩2小时"]
        该救的 = row.loc["差1人剩2小时"]
        print(f"  {'':<14}{'空位':>5}{'剩(时)':>8}{'需(人/时)':>11}{'倍数':>7}"
              f"{'会不会黄':>10}{'可救性':>8}{'优先级分':>10}{'该先救哪场':>12}")
        for 名, r in [("差4人剩2小时", 没戏的), ("差1人剩2小时", 该救的)]:
            print(f"  {名:<16}{int(r['remaining_seats']):>4}{r['hours_left']:>8.1f}"
                  f"{r['needed_rate']:>11.2f}{r['rate_multiple']:>7.2f}"
                  f"{r['fail_risk']:>11}{int(r['rescue_level']):>8}"
                  f"{int(r['priority_score']):>10}{r['priority_level']:>12}")
        print()
        note("「差4人剩2小时」需要每小时来 2 个人，是门店常态（0.5）的 4 倍 ——")
        note("大概率黄（预警必须响），但推也推不动（可救性 0）。该做的是提前通知客人")
        note("改期或准备流局，不是发朋友圈。")
        note("「差1人剩2小时」需要的速率刚好等于常态 —— 顺手推一把就锁死了。")
        print()
        note("上一版只有一个分数叫「危险度」，还拿它当排序依据 —— 结果就是")
        note("把「差4人剩2小时」这种救不回来的排在最前面，把「差1人剩2小时」")
        note("这种推一把就成的排在末尾。正好排反。根因是结构性的：")
        print()
        print("        缺口大  ->  情况糟，越缺越可能黄       （危险度高）")
        print("        缺口大  ->  难救，越缺越救不回来       （危险度低）")
        print()
        note("同一个变量回答两个方向相反的问题，所以现在拆成两个输出，")
        note("排序只按后者。救不回来的场次不会消失 —— 它带着「会不会黄 = 高」")
        note("留在表里，看板可以按这一列单独筛出「该考虑流局的场次」。")

        # ------------------------------------------------------------------
        title("第 3 步：硬规则 —— 满员没有空场风险")
        满员 = assessed[assessed["script_name"] == "满员的场"]
        show_risk(满员)
        note("这场 30 分钟后就要开场，但所有档位都是 0 —— 满员就是满员，")
        note("时间再紧也改变不了「这场已经开得起来」这件事。")
        print()
        note("注意 剩(时) 那列如实写着 0.5，但各个档位都被压成了 0。这是刻意的分工：")
        note("hours_left 是**事实**，档位是「这项给风险贡献了几分」。档位必须一起归零，")
        note("否则分数和等级会对不上 —— 一场满员的场会带着 2 分排到真有风险的场前面去。")
        note("（约超了、空位为负也一样按正常处理：那是超售问题，不是空场问题。）")

        # ------------------------------------------------------------------
        title("第 4 步：标定视图 —— 当前所有阈值")
        note("下面的默认值是**占位值**，没有经过任何真实门店数据标定。")
        note("结构（时间越少越紧迫、需要的人比平时来的多就越难救、满员无风险）")
        note("是可以论证的；具体是 6 小时还是 5 小时、常态到客速率是多少，")
        note("只能靠门店自己的数据标出来。")
        print()
        print_calibration(DEFAULT_RISK_PARAMS)

        # ------------------------------------------------------------------
        title("第 5 步：推进 now —— 等级自己变，没人去改过它")
        观察的场 = "明天快满了"     # 10-21 19:30 开场，10 个位子约了 9 个
        print(f"  看「{观察的场}」这一场（10-21 19:30 开场，10 个位子只剩 1 个）：")
        print()
        print(f"    {'now':<18}{'剩(时)':>8}{'需(人/时)':>11}{'倍数':>7}{'会不会黄':>10}{'该先救':>10}")
        for moment in ["2026-10-20 19:30", "2026-10-21 14:00", "2026-10-21 17:30",
                       "2026-10-21 18:30", "2026-10-21 19:00"]:
            ts = pd.Timestamp(moment)
            r = assess_sessions(raw, now=ts)
            r = r[r["script_name"] == 观察的场]
            if r.empty:
                print(f"    {moment:<18}（已开场，不再评估）")
                continue
            r = r.iloc[0]
            print(f"    {moment:<20}{r['hours_left']:>6.1f}{r['needed_rate']:>11.2f}"
                  f"{r['rate_multiple']:>7.2f}{r['fail_risk']:>11}{r['priority_level']:>12}")
        print()
        note("同一行数据，一个字都没改过，等级是「正常 -> 关注 -> 高危 -> 关注 -> 正常」。")
        print()
        note("先往上走好理解（时间越来越紧）。往下走这段才是关键：")
        note("剩 1 小时的时候，需要的速率是门店常态的 2 倍 —— 已经来不及了，")
        note("可救性掉到 1，等级跟着掉回「关注」；剩半小时时掉到 0，等级回「正常」。")
        print()
        note("也就是说：**优先级不是随时间单调上升的**。「越接近开场越该动手」是错的 ——")
        note("接近到一定程度之后，动手已经没有意义了。真正该动手的窗口很窄。")
        note("（窄到什么程度，第 7 步会给出具体数字 —— 它和占位参数 λ 直接相关。）")
        print()
        note("另外，等级**不落盘**，每次读取时重算 —— 和 booked_seats 同一个原则：")
        note("存下来就会过期，存的时候是「剩 3 小时高危」，一小时后还是这三个字。")

        # ------------------------------------------------------------------
        title("第 6 步：空位填满 —— 高危自动消失")
        目标 = "差1人剩2小时"     # 这一场在 NOW 时点上是唯一的「高危」
        id_of = dict(zip(raw["script_name"], raw["session_id"]))
        before = assess_sessions(raw, now=NOW).set_index("script_name").loc[目标]
        print(f"  填之前：空位 {int(before['remaining_seats'])}，"
              f"倍数 {before['rate_multiple']:.2f}，"
              f"该先救「{before['priority_level']}」")

        for i in range(int(before["remaining_seats"])):
            upsert_booking(id_of[目标], f"补位{i + 1}", path=bookings_path)

        raw2 = load_sessions_with_counts(sessions_path, bookings_path)
        after = assess_sessions(raw2, now=NOW).set_index("script_name").loc[目标]
        print(f"  填之后：空位 {int(after['remaining_seats'])}，"
              f"倍数 {after['rate_multiple']:.2f}，"
              f"该先救「{after['priority_level']}」")
        note("场景 B 要求的「空位填满时自动取消高危标记」不需要谁去实现 ——")
        note("等级本来就是算出来的，没人写过它，也就没人需要去删掉它。")

        # ------------------------------------------------------------------
        title("第 7 步：λ 换了，谁该先救就换了（占位值到底有多大影响）")
        note(f"现在演示里 λ = {DEFAULT_RISK_PARAMS.normal_arrival_rate} 人/小时。"
             f"下面把同一批数据换个 λ 再跑一遍：")
        print()
        print(f"   {'λ':>6}   {'高危':>4}  {'关注':>4}   高危的是哪几场")
        print(f"   {'-' * 60}")
        for lam in [0.5, 1.0, 2.0, 4.0]:
            p = RiskParams(normal_arrival_rate=lam)
            df = assess_sessions(raw, now=NOW, params=p)
            高 = df[df["priority_level"] == PRIORITY_HIGH]
            关 = df[df["priority_level"] == PRIORITY_WATCH]
            名单 = "、".join(高["script_name"]) or "（无）"
            print(f"   {lam:>6.1f}   {len(高):>4}  {len(关):>4}   {名单}")
        print()
        note("同一个模型、同一批数据，只是换了个 λ，该先救的场次就完全不一样了。")
        note(f"λ = 0.5 时高危只剩一场 —— 这不是巧合：倍数 = 2 × 空位 ÷ 剩余小时，")
        note("要同时满足「倍数 ≤ 1」和「剩余 ≤ 2 小时」，整数空位下只剩"
             "「差 1 人、剩整 2 小时」这一个格子。")
        note("所以 λ 是**必须先标定的第一个参数**，它决定的不只是等级，是排序本身。")
        note("（真实门店的 λ 远不止 0.5 —— 周末晚上一小时来几个人，店长心里有数。）")

        # ------------------------------------------------------------------
        title("第 8 步：给下游用的接口")
        高危 = high_risk_only(assessed)
        print(f"  high_risk_only() 挑出 {len(高危)} 场高危："
              f"{'、'.join(高危['script_name']) or '无'}")
        note("工具 7（文案生成）从这里拿数据，不必自己写筛选条件。")
        note("注意它挑的是**优先级**高危（该现在动手的），不是「会不会黄」高危。")
        note("要预警那些大概要黄的场次，直接按 fail_risk 列筛。")

    # ----------------------------------------------------------------------
    # 对照：真实 data/ 目录
    # ----------------------------------------------------------------------
    title("对照：data/ 下的真实数据用真实的「现在」评估")
    if not SESSIONS_XLSX.exists():
        print("  文件不存在，跳过。先运行 scripts/02_storage_demo.py 生成它。")
    else:
        real = load_sessions_with_counts()
        print(f"  真实现在时间：{pd.Timestamp.now().floor('s')}\n")
        if not BOOKINGS_XLSX.exists():
            print(f"  （{BOOKINGS_XLSX.name} 不存在，已约人数全部按 0 算）\n")
        show_risk(assess_sessions(real))
        note("02_storage_demo.py 造的场次都在 10-20 之后，所以现在还没开场。")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

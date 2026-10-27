"""工具 4：空场风险识别（会不会黄 / 该先救哪场）。

========================= 两个输出，回答两个问题 =========================

  fail_risk       会不会黄        这场不做任何事，会不会开不起来
  priority_level  该先救哪场      现在该把力气花在哪一场

这两个问题的答案**方向经常是相反的**，这是本模块最核心的一点：

    缺口大  ->  情况糟，越缺越可能黄         (fail_risk 变高)
    缺口大  ->  难救，越缺越救不回来         (priority 变低)

同一个变量，回答两个方向相反的问题。所以**不能用同一个分数排序** ——
早期版本按"危险度"排序，结果是把「已经救不回来的场次」排在最前面，
把「推一把就锁死的场次」排在末尾，正好排反。

  fail_risk 是给店长看的预警：这场要黄，考虑提前通知客人、准备流局。
  priority  是给店长派的活：先推这一场。

救不回来的场次不会从结果里消失（它带着 fail_risk = 高 留在列表里），
只是不排在前面。

========================= 核心量：需要的到客速率 =========================

    需要的到客速率 = 剩余空位 ÷ 剩余小时     （人/小时）
    倍数          = 需要的到客速率 ÷ 门店常态到客速率

这个量把**人数**和**时间**放进了同一个尺度上比：还差 4 个人、还剩 2 小时，
就是要每小时来 2 个人才填得满。门店知道自己平时一小时大概来几个客人，
所以倍数是**可标定**的 —— 比「缺口 25% 算偏缺」这种拍出来的线好定得多。

它也是**绝对人数口径**：剩余空位÷小时天然看的是还要来几个人，
不需要再纠结"该用比例还是绝对人数"。

========================= 关于阈值，说清楚 =========================

结构（写在代码里，可论证）：
  时间越少越紧迫；需要的人比平时来的多就危险；满员没有空场风险；
  「倍数 <= 1」这条线不是拍的 —— 需要的速率不超过常态，意味着自然流量就够。

数字（抽成 RiskParams，是经验值，只能标定）：
  6 小时还是 5 小时算紧迫、门店常态到客速率是多少、多少人算大本。

⚠️ 其中 **normal_arrival_rate 影响最大**：谁比谁优先，直接取决于它。
   默认值是占位值，未经任何真实门店数据标定。

它还有一个已知的粗糙之处：**到客速率不是恒定的**，周末晚上和周二下午
完全不是一回事。真实的做法要按时段分别标定，本轮不做。

========================= 两条刻意的设计 =========================

一、结果不落盘
   每次读取时重算，不写进 Excel。理由和 storage 里 booked_seats 一样：
   存下来就会过期。重算的副产品是「空位填满后高危标记自动消失」自动成立。

二、只给结构化字段，不给中文句子
   返回 fail_risk / priority_level 这些列，但**不返回**任何现成的句子。
   判断由代码给，表达由 LLM 给（分层原则，见 README 二）。

本模块**只放纯函数，不出现 @tool 装饰器**（理由见 storage.py 顶部说明）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pandas as pd

from app.tools.expiry import filter_active, resolve_now, to_timestamp

# ===========================================================================
# 一、等级常量
# ===========================================================================

# 会不会黄
FAIL_HIGH = "高"
FAIL_MEDIUM = "中"
FAIL_LOW = "低"
FAIL_LEVELS = [FAIL_HIGH, FAIL_MEDIUM, FAIL_LOW]      # 由重到轻

# 该先救哪场
PRIORITY_HIGH = "高危"
PRIORITY_WATCH = "关注"
PRIORITY_NORMAL = "正常"
PRIORITY_LEVELS = [PRIORITY_HIGH, PRIORITY_WATCH, PRIORITY_NORMAL]   # 由重到轻

# 「需要的速率不超过常态」这条线不是拍的：等于常态就意味着自然流量刚好够。
# 所以它写成常量而不是参数。
NATURAL_MULTIPLE = 1.0


# ===========================================================================
# 二、可标定的参数
# ===========================================================================

@dataclass(frozen=True)
class RiskParams:
    """空场识别的全部阈值。

    ⚠️ 默认值是**占位值**，未经真实门店数据标定。结构对，数字需要按门店调整。
    改这些数字只改变分级结果，不影响任何逻辑。
    """

    # --- 紧迫度：距离开场还剩多久 ---
    tight_hours: float = 6.0        # 剩余 <= 6 小时 -> 档位 1（偏紧）
    urgent_hours: float = 2.0       # 剩余 <= 2 小时 -> 档位 2（紧急）

    # --- 门店常态到客速率：平时每小时自然来几个客人 ---
    # 这个数影响最大 —— 谁比谁优先，直接取决于它。门店接入时应最先标定它。
    normal_arrival_rate: float = 0.5

    # 需要的速率超过常态的这么多倍，就算救不回来了（同时也是"大概率黄"的线）
    hard_multiple: float = 2.0

    # --- 规模：这场坐满能坐几人（决定"空掉多疼"，不参与优先级打分）---
    medium_room_seats: int = 7      # >= 7 位  -> 档位 1（中本）
    large_room_seats: int = 10      # >= 10 位 -> 档位 2（大本）

    # --- 优先级分级线：priority_score = 紧迫档 × 可救性档，范围 0~4 ---
    watch_score: int = 1            # >= 1 -> 关注
    high_score: int = 3             # >= 3 -> 高危


DEFAULT_RISK_PARAMS = RiskParams()


# ===========================================================================
# 三、时间与紧迫度
# ===========================================================================

def hours_left(start_time: Any, *, now: Any = None) -> float | None:
    """距离开场还剩多少小时。开场时间缺失或看不懂时返回 None。"""
    ts = to_timestamp(start_time)
    if ts is None:
        return None
    return (ts - resolve_now(now)).total_seconds() / 3600.0


def time_level(left_hours: float | None, params: RiskParams = DEFAULT_RISK_PARAMS) -> int:
    """紧迫度分档：0 不急 / 1 偏紧 / 2 紧急。

    left_hours 为 None（开场时间缺失）-> 档位 0。
    宁可不报警，也不要因为一条数据缺失就把它标成高危 ——
    假警报多了，真警报就没人看了。
    """
    if left_hours is None or pd.isna(left_hours):
        return 0
    if left_hours <= params.urgent_hours:
        return 2
    if left_hours <= params.tight_hours:
        return 1
    return 0


# ===========================================================================
# 四、核心量：需要的到客速率
# ===========================================================================

def needed_rate(remaining_seats: Any, left_hours: float | None) -> float | None:
    """还差几个人 ÷ 还剩几小时 = 每小时得再来几个人才填得满。单位：人/小时。

    返回 None 表示算不出来（时间缺失或已过），调用方据此不施加压力。
    满员返回 0.0 —— 一个人都不用再来，和"算不出来"是两回事。
    """
    if remaining_seats is None or pd.isna(remaining_seats):
        return None

    remaining = float(remaining_seats)
    if remaining <= 0:
        return 0.0
    if left_hours is None or pd.isna(left_hours):
        return None
    if float(left_hours) <= 0:
        # 已经开场了，再来人也来不及。
        return None

    return remaining / float(left_hours)


def gap_ratio(remaining_seats: Any, total_seats: Any) -> float | None:
    """缺口比例 = 剩余空位 / 总位。**只作展示用**，不参与任何判断。

    模型的核心量是 needed_rate（绝对人数 ÷ 时间），不是比例。
    留这一列是因为店长看「还差几成」比看「每小时 0.33 人」直观，
    但别拿它做判断 —— 6 人本差 3 人和 8 人本差 3 人的难度是一样的，
    比例会把它俩算成不一样，绝对人数不会。
    """
    if remaining_seats is None or pd.isna(remaining_seats):
        return None
    if total_seats is None or pd.isna(total_seats) or float(total_seats) <= 0:
        return None
    return float(remaining_seats) / float(total_seats)


def rate_multiple(rate: float | None, params: RiskParams = DEFAULT_RISK_PARAMS) -> float | None:
    """需要的到客速率是门店常态的几倍。算不出来返回 None。"""
    if rate is None or pd.isna(rate):
        return None
    if params.normal_arrival_rate <= 0:
        return None
    return float(rate) / params.normal_arrival_rate


# ===========================================================================
# 五、两个输出
# ===========================================================================

def fail_risk(multiple: float | None, params: RiskParams = DEFAULT_RISK_PARAMS) -> str:
    """会不会黄：由倍数单调决定。

    倍数 <= 1.0        自然流量就够，不干预也开得起来
    1.0 < 倍数 <= 2.0  悬，不推一把可能就黄了
    倍数 > 2.0         需要的人远超平时来的，大概率黄

    倍数算不出来（时间缺失）-> 低。同 time_level 的取舍：不因数据缺失造假警报。
    """
    if multiple is None or pd.isna(multiple):
        return FAIL_LOW
    if multiple <= NATURAL_MULTIPLE:
        return FAIL_LOW
    if multiple <= params.hard_multiple:
        return FAIL_MEDIUM
    return FAIL_HIGH


def rescue_level(
    multiple: float | None,
    remaining_seats: Any,
    params: RiskParams = DEFAULT_RISK_PARAMS,
) -> int:
    """可救性分档：0 没戏 / 1 要赶一赶 / 2 顺手就成。

    和 fail_risk 用的是同一个倍数，但读的方向相反 ——
    倍数大的时候 fail_risk 说"要黄了"，rescue_level 说"别费劲了"。
    两者不矛盾，它们回答的是不同的问题。

    满员 -> 0：没有空位要填，不存在"救"这回事。
    """
    if remaining_seats is None or pd.isna(remaining_seats) or float(remaining_seats) <= 0:
        return 0
    if multiple is None or pd.isna(multiple):
        return 0
    if multiple <= NATURAL_MULTIPLE:
        return 2
    if multiple <= params.hard_multiple:
        return 1
    return 0


def stake_level(total_seats: Any, params: RiskParams = DEFAULT_RISK_PARAMS) -> int:
    """规模分档：0 小本 / 1 中本 / 2 大本。由**总位**决定。

    在结果里单独一列，**不参与 priority_score**。反例说明了为什么不能加进去：

      12 人本、11 人已约、剩 2 小时 -> 紧迫档 2 + 规模档 2 会把它判成"高危"，
      可它 92% 满、只差一个人。

    规模回答的是"该不该救这场"（空掉多疼），不是"先不先救"。它只做排序的
    第二个关键字：同样该救的场次里，先救大的。

    总位缺失 -> 0（不知道多大，不施加规模权重）。
    """
    if total_seats is None or pd.isna(total_seats):
        return 0
    total = float(total_seats)
    if total >= params.large_room_seats:
        return 2
    if total >= params.medium_room_seats:
        return 1
    return 0


def priority_level(
    t_level: int,
    r_level: int,
    params: RiskParams = DEFAULT_RISK_PARAMS,
) -> tuple[str, int]:
    """优先级 = 紧迫度 × 可救性。返回 (等级, 分数)。

    为什么是**相乘**而不是相加或取 max：
      相乘才有那个"甜点区"。时间还多 -> 紧迫低 -> 不急着动；
      时间少到救不回来 -> 可救性低 -> 动了也白动；
      只有**快开场了、而且还救得回来**才是现在真正该动手的。
      相加或取 max 都造不出这个形状（取 max 会把"差 4 人剩 2 小时"判成高危）。

    紧迫档 0~2 × 可救性档 0~2 = 分数 0~4（默认参数下的对照）：

                 可救2(顺手)  可救1(要赶)  可救0(没戏)
      紧迫2(≤2h)     高危         关注         正常
      紧迫1(≤6h)     关注         关注         正常
      紧迫0(>6h)     正常         正常         正常
    """
    score = int(t_level) * int(r_level)
    if score >= params.high_score:
        return PRIORITY_HIGH, score
    if score >= params.watch_score:
        return PRIORITY_WATCH, score
    return PRIORITY_NORMAL, score


# ===========================================================================
# 六、主入口
# ===========================================================================

ASSESS_COLUMNS = [
    "hours_left",       # 距离开场还剩几小时（事实）
    "gap_ratio",        # 还差几成（展示用，不参与判断）
    "needed_rate",      # 每小时得再来几个人（核心量）
    "rate_multiple",    # 需要的速率是常态的几倍
    "time_level",       # 紧迫档 0/1/2
    "fail_risk",        # 会不会黄：高 / 中 / 低
    "rescue_level",     # 可救性 0/1/2
    "stake_level",      # 规模 0/1/2（只影响排序）
    "priority_score",   # 优先级分 = 紧迫 × 可救性，0~4
    "priority_level",   # 该先救哪场：高危 / 关注 / 正常
]


def _assess_one(row: pd.Series, now_ts: pd.Timestamp, params: RiskParams) -> dict:
    """评估一场。"""
    left = hours_left(row.get("start_time"), now=now_ts)
    remaining = row.get("remaining_seats")
    total = row.get("total_seats")

    # --- 硬规则：满员（含超额预约）没有空场风险，不看时间 ---
    # 一条 8 人本塞了 9 个人，问题不是"会空场"，是超售，那是另一个问题。
    # 所有档位一起归零，保证"满员行的档位全是 0"这条不变式成立 ——
    # 看的人不必分辨"哪些行归零了、哪些没有"。规模这个事实没丢，total_seats 列还在。
    if remaining is not None and not pd.isna(remaining) and float(remaining) <= 0:
        return {
            "hours_left": left,
            "gap_ratio": 0.0,
            "needed_rate": 0.0,
            "rate_multiple": 0.0,
            "time_level": 0,
            "fail_risk": FAIL_LOW,
            "rescue_level": 0,
            "stake_level": 0,
            "priority_score": 0,
            "priority_level": PRIORITY_NORMAL,
        }

    t_lv = time_level(left, params)
    rate = needed_rate(remaining, left)
    multiple = rate_multiple(rate, params)
    r_lv = rescue_level(multiple, remaining, params)
    level, score = priority_level(t_lv, r_lv, params)

    return {
        "hours_left": left,
        "gap_ratio": gap_ratio(remaining, total),
        "needed_rate": rate,
        "rate_multiple": multiple,
        "time_level": t_lv,
        "fail_risk": fail_risk(multiple, params),
        "rescue_level": r_lv,
        "stake_level": stake_level(total, params),
        "priority_score": score,
        "priority_level": level,
    }


def assess_sessions(
    sessions: pd.DataFrame,
    *,
    now: Any = None,
    params: RiskParams | None = None,
    include_expired: bool = False,
) -> pd.DataFrame:
    """评估一批场次，**按优先级从高到低**排序返回。

    返回两个独立输出，别只看一个：
      fail_risk       会不会黄    —— 预警
      priority_level  该先救哪场  —— 派活

    排序按 priority_score 降序 → stake_level 降序 → start_time 升序。
    **不是**按危险度排 —— 救不回来的场次会沉到后面去（它们带着
    fail_risk = 高 留在结果里，需要时按那一列筛）。同样该救的场次里先救大的。

    **默认自动剔除已开场与已取消的场次**（内部调用工具 3 的 filter_active）。
    为什么不让调用方自己先过滤：
      这个工具的消费者是 LLM。要求模型记住「必须先调工具 3 再调工具 4」
      是把它不擅长的事强加给它 —— 漏调一次，就会给已经开场的场次评估。
    确实需要评估已开场场次时，传 include_expired=True。
    """
    p = params if params is not None else DEFAULT_RISK_PARAMS
    moment = resolve_now(now)

    usable = sessions if include_expired else filter_active(sessions, now=moment)

    if usable.empty:
        out = usable.copy()
        for col in ASSESS_COLUMNS:
            out[col] = pd.Series(dtype="object")
        return out.reset_index(drop=True)

    # 逐行 apply 而不是向量化：这里的逻辑全是分支（硬规则、多重兜底），
    # 硬写成 numpy 条件表达式会难读得多。当前数据量下性能不是瓶颈。
    assessed = usable.apply(lambda row: _assess_one(row, moment, p), axis=1, result_type="expand")
    out = pd.concat([usable.reset_index(drop=True), assessed], axis=1)

    return out.sort_values(
        ["priority_score", "stake_level", "start_time"],
        ascending=[False, False, True],
        ignore_index=True,
    )


def high_risk_only(assessed: pd.DataFrame) -> pd.DataFrame:
    """从评估结果里挑出优先级为「高危」的场次。工具 7（文案生成）会用到。

    保留排序 —— 结果内部已按规模、开场时间排好，下游不必再排一遍。
    """
    return assessed[assessed["priority_level"] == PRIORITY_HIGH].reset_index(drop=True)

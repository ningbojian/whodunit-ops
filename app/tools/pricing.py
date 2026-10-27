"""工具 5：动态折扣定价（该让多少 / 让到哪条线为止）。

========================= 一句话 =========================

工具 4 回答「这场会不会黄、该先救哪场」；本工具回答「**让利的经济账怎么算、
底线在哪**」。它不声称算出了最优折扣 —— 那需要价格弹性数据，门店没有，模型也没有。

========================= 推导一：只对剩余空位打折，让利是免费的 =========================

已约的 b 人按原价 p₀ 不变，降价只作用于剩下的 r 个位子。设降价后多来 k 人：

    不降价：  收入 = b·p₀              成本 = F + v·b
    降价到 p： 收入 = b·p₀ + k·p        成本 = F + v·(b+k)

    差额 = k·p − k·v = k·(p − v)

**只要 p > v，每多来一个人都是净赚的，与降了多少无关。**

这条推导同时给出两件事：

  1. 边际底价 floor_price = v（每人变动成本）。低于它，多来一个多亏一个。
     和工具 4 的「倍数 ≤ 1」一样，这条线是**推出来的**，不是拍的。
  2. 折扣力度不影响「划不划算」。力度只影响「能多拉几个人」——
     而这是我们不知道的。所以本工具的折扣档位是**经验值**，模型只保证
     「不破底价就不亏」。诚实地说：我们算的是账，不是最优解。

========================= 推导二：两条保本线，别混用 =========================

    floor_price        = v                       边际底价（硬约束）
    break_even_price   = F ÷ 到客人数 + v         均摊保本价（参考线）

v 是常数；F/n 随人数变化 —— 人越少，固定成本摊得越薄，均摊保本价越高：

    10 人本、固定 480、变动 20
      满员 10 人 -> 68 元      只来 5 人 -> 116 元      只来 2 人 -> 260 元

所以：

  · **不能拿 break_even_price 当售卖下限**。差 4 个人时它高得没法卖，
    而实际上只要能覆盖 v 就该卖 —— 黄掉的话 F 照样花出去。
  · break_even_price 回答的是另一个问题：「这个价格卖满，这场戏赚不赚」。
    主表里按**满员**摊薄，因为那是排场定价时的锚。

========================= ⚠️ 与工具 4 的定位差异（本模块最容易读错的地方） =========================

工具 4 的 rescue_level = 0（倍数 > 2）意思是「**填不满**」，判「正常，别浪费力气」。
那是指**推广**：再发朋友圈也拉不齐人。

但本工具问的是另一个问题：**能不能少亏**。填不满不等于不该少亏。

    原价 168、变动成本 20、只剩 2 人、打 7 折
    多来 2 个人 -> 收入 +2×117.6=235.2，成本 +40  ->  亏损减少 195 元

所以倍数 > 2 的场次**更应该打折**，只是目标从「填满」降级为「少亏」。

结论：折扣的触发条件是 **倍数 > 1**（fail_risk ≠ 低），**不是**工具 4 的
rescue_level == 1。两个工具的目标函数不同，看起来矛盾，实际上各说各的。
另外注意反过来的一面：工具 4 判「高危」的场次（倍数恰好 ≤ 1）也不需要打折 ——
**要推广 ≠ 要让利**。自然流量就够的场次，发条朋友圈即可，不必动价格。

========================= 三、底价外泄防护 =========================

公共不变量：门店保本底价仅内部可见，不进入任何对外文案（见 README 三）。

靠注释提醒下游工具「别写底价」迟早会漏。所以把内部列登记在 INTERNAL_COLUMNS，
下游调 public_view() 拿可见列 —— **结构上就拿不到底价**。
新增任何财务列时必须同时登记进 INTERNAL_COLUMNS。

========================= 四、刻意的设计 =========================

一、结果不落盘，每次读取时重算（同 booked_seats、同工具 4）。
二、只给结构化字段，不给中文句子 —— 判断由代码给，表达由 LLM 给。
三、内部自动调用工具 4（工具 4 内部又调工具 3）。消费者是 LLM，
   不能要求模型记得「先调 3、再调 4、最后调 5」。

本模块**只放纯函数，不出现 @tool 装饰器**（理由见 storage.py 顶部说明）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pandas as pd

from app.tools.risk import (
    DEFAULT_RISK_PARAMS,
    NATURAL_MULTIPLE,
    RiskParams,
    assess_sessions,
)

# ===========================================================================
# 一、等级常量
# ===========================================================================

TIER_LIST = "原价"
TIER_LIGHT = "轻让"
TIER_MEDIUM = "中让"
TIER_HEAVY = "重让"
TIER_UNKNOWN = "无法定价"

# 由轻到重。TIER_UNKNOWN 不在其中 —— 它不是"让得多"或"让得少"，是"算不出来"。
DISCOUNT_TIERS = [TIER_LIST, TIER_LIGHT, TIER_MEDIUM, TIER_HEAVY]

NOTE_PRICE_MISSING = "原价缺失"


# ===========================================================================
# 二、可标定的参数
# ===========================================================================

@dataclass(frozen=True)
class PricingParams:
    """定价的全部参数。

    ⚠️ 默认值是**占位值**，未经任何真实门店数据标定。
    结构（固定成本 + 变动成本、让利不破底价）可论证，数字只能由门店给。
    """

    # --- 固定成本：这场戏开起来就花出去的钱（DM 工时 + 房间时段 + 剧本摊销）---
    # 按总位分档，理由：大本时长更长、可能要多名 DM。分档线沿用工具 4 的
    # medium_room_seats / large_room_seats，保证两个工具对"小/中/大本"的认知一致。
    small_fixed_cost: float = 260.0     # <=6 位：短本
    medium_fixed_cost: float = 340.0    # 7~9 位
    large_fixed_cost: float = 480.0     # >=10 位：大本，DM 工时更长
    medium_room_seats: int = 7
    large_room_seats: int = 10

    # --- 变动成本：每多来一个玩家才多花的钱（耗材、饮料）---
    # 这就是边际底价 floor_price。
    marginal_cost_per_seat: float = 20.0

    # --- 折扣阶梯：倍数越大让得越多 ---
    light_multiple: float = 1.5
    medium_multiple: float = 2.5
    light_discount: float = 0.95
    medium_discount: float = 0.88
    # 同时是门店的**价格保护线**：宁可流局也不砸价格体系。
    # 这条下限是经营策略，不是数学推导 —— 数学上只要 > floor_price 就该卖。
    heavy_discount: float = 0.80


DEFAULT_PRICING_PARAMS = PricingParams()


# ===========================================================================
# 三、两条保本线
# ===========================================================================

def fixed_cost(
    total_seats: Any,
    params: PricingParams = DEFAULT_PRICING_PARAMS,
) -> float:
    """这场排班的固定成本，按总位分档。

    总位缺失时按中本估：不知道多大，取中间值，不猜极端。
    """
    if total_seats is None or pd.isna(total_seats):
        return params.medium_fixed_cost

    total = float(total_seats)
    if total >= params.large_room_seats:
        return params.large_fixed_cost
    if total >= params.medium_room_seats:
        return params.medium_fixed_cost
    return params.small_fixed_cost


def floor_price(params: PricingParams = DEFAULT_PRICING_PARAMS) -> float:
    """边际底价 = 每人变动成本。低于它，多卖一个位子多亏一笔。

    这条线跟人数无关，也跟原价无关 —— 它只取决于"多来一个人要多花多少"。
    """
    return params.marginal_cost_per_seat


def break_even_price(
    headcount: Any,
    total_seats: Any,
    params: PricingParams = DEFAULT_PRICING_PARAMS,
) -> float | None:
    """均摊保本价 = 固定成本 ÷ 到客人数 + 每人变动成本。

    headcount   这场最终来了几个人（决定固定成本摊到几个人头上）
    total_seats 总位（决定固定成本按哪一档算）

    人数为 0 或缺失时无意义（除零）-> None。

    ⚠️ 它**不是**售卖下限。人越少它越高，差 4 个人时高到没法卖。
    它回答的是「这个价卖满，这场戏赚不赚」，不是「最低能卖多少」。
    """
    if headcount is None or pd.isna(headcount) or float(headcount) <= 0:
        return None
    return fixed_cost(total_seats, params) / float(headcount) + params.marginal_cost_per_seat


# ===========================================================================
# 四、折扣阶梯
# ===========================================================================

def discount_tier(
    multiple: Any,
    params: PricingParams = DEFAULT_PRICING_PARAMS,
) -> tuple[str, float]:
    """按「需要的到客速率是常态的几倍」给出让利档位，返回 (档位标签, 折扣率)。

    倍数由工具 4 算出，这里不重算 —— 两个工具对"难度"用同一个定义。

    倍数 <= 1    不打折。自然流量就够，**要推广 ≠ 要让利**：
                 发条朋友圈即可，不必动价格。
    倍数 > 2.5   重让。此时工具 4 已经判「救不回来」，但救不回来只意味着
                 填不满，不意味着不该少亏（见模块顶部推导）。

    倍数算不出来（时间缺失）-> 原价。同工具 4 的取舍：数据缺失不施加压力。
    """
    if multiple is None or pd.isna(multiple):
        return TIER_LIST, 1.0

    m = float(multiple)
    if m <= NATURAL_MULTIPLE:
        return TIER_LIST, 1.0
    if m <= params.light_multiple:
        return TIER_LIGHT, params.light_discount
    if m <= params.medium_multiple:
        return TIER_MEDIUM, params.medium_discount
    return TIER_HEAVY, params.heavy_discount


def contribution_per_seat(
    price: Any,
    params: PricingParams = DEFAULT_PRICING_PARAMS,
) -> float | None:
    """每多卖一个位子，这场少亏多少钱 = 售价 − 每人变动成本。

    整张表里最有行动含义的一列：它告诉店长「每拉来一个人值多少」。
    例：168 打 88 折 = 148，减去 20 = **128 元/人**。

    ⚠️ 这是**上界**。推导假设"降价拉来的人都是原本不会来的"；
    实际有一部分本来就打算按原价来，降价只是让他们少付了钱。
    这部分损失没有计入。
    """
    if price is None or pd.isna(price):
        return None
    return float(price) - params.marginal_cost_per_seat


def _round_price(value: float) -> float:
    """四舍五入到整数元。凑 8/9 尾数属于营销技巧，本模块不做。"""
    return float(round(value))


# ===========================================================================
# 五、主入口
# ===========================================================================

# 本模块**新增**的列。原价 list_price 来自场次表，已经在表里，不重复返回。
PRICING_COLUMNS = [
    "discount_tier",        # 原价 / 轻让 / 中让 / 重让 / 无法定价
    "discount_rate",        # 折扣率，0.88 表示 88 折
    "recommended_price",    # 对外推荐售价（只作用于剩余空位）
    "seat_saving",          # 每个空位让掉多少钱 = 原价 − 推荐价
    "contribution_per_seat",  # 每多来一个人，这场少亏多少
    "floor_price",          # [内部] 边际底价
    "break_even_price",     # [内部] 满员保本价
    "sunk_cost",            # [内部] 黄掉亏多少 = 固定成本
    "profit_if_sold_out",   # [内部] 已约按原价 + 空位按推荐价，这场赚多少
    "price_note",           # 定不了价时的标签，正常为空
]

# 公共不变量：这些列**不得进入任何对外文案**。
# 新增财务列时必须登记到这里，否则 public_view 会漏掉它。
INTERNAL_COLUMNS = [
    "floor_price",
    "break_even_price",
    "sunk_cost",
    "profit_if_sold_out",
]


def _blank_row(floor: float, be: float | None, sunk: float, note: str) -> dict:
    """定不了价时的整行（原价缺失 / 空位未知）。不猜、不默认。"""
    return {
        "discount_tier": TIER_UNKNOWN,
        "discount_rate": None,
        "recommended_price": None,
        "seat_saving": None,
        "contribution_per_seat": None,
        "floor_price": floor,
        "break_even_price": be,
        "sunk_cost": sunk,
        "profit_if_sold_out": None,
        "price_note": note,
    }


def _price_one(row: pd.Series, params: PricingParams) -> dict:
    """给一场定价。"""
    total = row.get("total_seats")
    booked = row.get("booked_seats")
    remaining = row.get("remaining_seats")
    list_price = row.get("list_price")
    multiple = row.get("rate_multiple")

    sunk = fixed_cost(total, params)                      # 黄掉就是亏这么多
    floor = floor_price(params)
    # 主表按**满员**摊薄：那是这场戏排场定价时的锚。
    be = break_even_price(total, total, params)

    # --- 原价缺失：不猜、不默认。没有原价，折扣率无从谈起 ---
    if list_price is None or pd.isna(list_price):
        return _blank_row(floor, be, sunk, NOTE_PRICE_MISSING)

    # --- 空位未知：连"要不要让"都判断不了 ---
    if remaining is None or pd.isna(remaining):
        return _blank_row(floor, be, sunk, "空位未知")

    n_remaining = max(float(remaining), 0.0)
    n_booked = 0.0 if booked is None or pd.isna(booked) else float(booked)

    tier, rate = discount_tier(multiple, params)

    # 满员（含超售）：没有空位可让，折扣无从作用。
    if n_remaining <= 0:
        tier, rate = TIER_LIST, 1.0

    base = float(list_price)
    recommended = _round_price(base * rate)
    saving = round(base - recommended, 2)

    # 已约的按原价、剩余空位按推荐价；成本按满座算（固定 + 每人变动）。
    revenue = n_booked * base + n_remaining * recommended
    cost = sunk + params.marginal_cost_per_seat * (n_booked + n_remaining)
    profit = round(revenue - cost, 2)

    return {
        "discount_tier": tier,
        "discount_rate": rate,
        "recommended_price": recommended,
        "seat_saving": saving,
        "contribution_per_seat": contribution_per_seat(recommended, params),
        "floor_price": floor,
        "break_even_price": be,
        "sunk_cost": sunk,
        "profit_if_sold_out": profit,
        "price_note": "",
    }


def price_sessions(
    sessions: pd.DataFrame,
    *,
    now: Any = None,
    params: PricingParams | None = None,
    risk_params: RiskParams | None = None,
    include_expired: bool = False,
) -> pd.DataFrame:
    """给一批场次定价。

    内部调用工具 4 的 assess_sessions（它内部再调工具 3 的 filter_active），
    所以结果里同时带着评估列（rate_multiple / fail_risk / priority_level …）
    和定价列 —— Agent 调一次就够，不必记住「先调 4 再调 5」。
    理由同工具 4：消费者是 LLM，不能把步骤顺序的记性压在它身上。

    **返回顺序沿用工具 4 的优先级排序**（priority_score 降序 → 规模降序 →
    开场时间升序），所以最该动手的场次排在前面，不重新按价格排。

    已开场与已取消的场次默认剔除；确实要看全部时传 include_expired=True。
    """
    p = params if params is not None else DEFAULT_PRICING_PARAMS
    rp = risk_params if risk_params is not None else DEFAULT_RISK_PARAMS

    assessed = assess_sessions(
        sessions, now=now, params=rp, include_expired=include_expired,
    )

    if assessed.empty:
        out = assessed.copy()
        for col in PRICING_COLUMNS:
            if col not in out.columns:
                out[col] = pd.Series(dtype="object")
        return out.reset_index(drop=True)

    priced = assessed.apply(
        lambda row: _price_one(row, p), axis=1, result_type="expand",
    )
    return pd.concat([assessed.reset_index(drop=True), priced], axis=1)


def public_view(priced: pd.DataFrame) -> pd.DataFrame:
    """**对外可见视图**：剔除所有内部财务列。工具 7（文案生成）应该调这个。

    公共不变量见 README 三：门店保本底价仅内部可见，不进入任何对外文案。

    为什么要有这个函数：靠注释提醒下游「别把底价写进文案」迟早会漏，
    而漏一次就是把门店的底牌发到朋友圈。这里按 INTERNAL_COLUMNS 剔除 ——
    下游拿到的 DataFrame 里**根本没有**底价这一列，想写也写不出来。

    注意这是"按名单剔除"，不是"按白名单保留"：将来新增一列，默认会被保留。
    所以新增任何财务列时，必须同时登记进 INTERNAL_COLUMNS。
    """
    return priced.drop(columns=[c for c in INTERNAL_COLUMNS if c in priced.columns])

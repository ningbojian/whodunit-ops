"""工具 5 动态折扣定价 —— 单元测试。

运行：
  PYTHONUTF8=1 "C:/Users/Lenovo/anaconda3/python.exe" -m pytest tests/ -v

同工具 3/4：**不联网、不需要 API key、不依赖系统时钟**（now 一律显式注入）。

这些测试**不校验具体折扣率是否合理** —— 「88 折」这件事没有对错，只能由门店标定。
测试校验的是**结构**：

  只要不破边际底价，让利就是划算的（contribution_per_seat > 0）；
  人越少均摊保本价越高；满员不打折；原价缺失不猜……

本轮的重点是 TestToolAlignment 那一节：同一场在工具 4 和工具 5 里结论不同，
而且**必须**不同 —— 两个工具的目标函数本来就不一样。
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import timedelta

import pandas as pd
import pytest

from app.tools.pricing import (
    DEFAULT_PRICING_PARAMS,
    DISCOUNT_TIERS,
    INTERNAL_COLUMNS,
    PRICING_COLUMNS,
    TIER_HEAVY,
    TIER_LIGHT,
    TIER_LIST,
    TIER_MEDIUM,
    TIER_UNKNOWN,
    PricingParams,
    break_even_price,
    contribution_per_seat,
    discount_tier,
    fixed_cost,
    floor_price,
    price_sessions,
    public_view,
)
from app.tools.risk import (
    DEFAULT_RISK_PARAMS,
    PRIORITY_HIGH,
    PRIORITY_NORMAL,
    stake_level,
)
from app.tools.storage import STATUS_CANCELLED, STATUS_PENDING, _coerce_types

NOW = pd.Timestamp("2026-10-20 19:30:00")
P = DEFAULT_PRICING_PARAMS

# 默认原价：演示与测试统一用 168。折扣率 0.88 时 168×0.88=147.84 -> 148。
原价 = 168


def _表(rows) -> pd.DataFrame:
    """把一批场次记录整形成规范表（借用 storage 自己的整形逻辑）。"""
    defaults = {
        "store_name": "迷雾剧场",
        "total_seats": 8,
        "booked_seats": 0,
        "list_price": 原价,
        "status": STATUS_PENDING,
    }
    return _coerce_types(pd.DataFrame([{**defaults, **row} for row in rows]))


def _场(*, 剩几小时=12, 总位=10, 已约=0, 原价=原价, 名="某场", **extra) -> dict:
    start = None if 剩几小时 is None else NOW + timedelta(hours=剩几小时)
    return {
        "script_name": 名,
        "start_time": start,
        "total_seats": 总位,
        "booked_seats": 已约,
        "list_price": 原价,
        **extra,
    }


def _定价一场(**kwargs) -> pd.Series:
    return price_sessions(_表([_场(**kwargs)]), now=NOW).iloc[0]


# ===========================================================================
# 一、固定成本分档
# ===========================================================================

class TestFixedCost:

    def test_三档划分(self):
        assert fixed_cost(6, P) == 260      # 小本
        assert fixed_cost(8, P) == 340      # 中本
        assert fixed_cost(12, P) == 480     # 大本

    def test_边界值落在更大的一档(self):
        assert fixed_cost(6, P) == P.small_fixed_cost
        assert fixed_cost(P.medium_room_seats, P) == P.medium_fixed_cost
        assert fixed_cost(P.large_room_seats - 1, P) == P.medium_fixed_cost
        assert fixed_cost(P.large_room_seats, P) == P.large_fixed_cost

    def test_缺失按中本估(self):
        # 不知道多大，取中间值，不猜极端。
        assert fixed_cost(None, P) == P.medium_fixed_cost
        assert fixed_cost(pd.NA, P) == P.medium_fixed_cost

    def test_与工具4的规模分档线一致(self):
        # 两个工具对"小/中/大本"必须是同一套线，否则同一场在两边的规模认知会打架。
        # 同一个总位数，工具 4 的 stake_level 与工具 5 的成本档位必须逐一对应。
        for seats in [5, 6, 7, 9, 10, 12]:
            assert (stake_level(seats, DEFAULT_RISK_PARAMS) == 0) == \
                   (fixed_cost(seats, P) == P.small_fixed_cost)
            assert (stake_level(seats, DEFAULT_RISK_PARAMS) == 1) == \
                   (fixed_cost(seats, P) == P.medium_fixed_cost)
            assert (stake_level(seats, DEFAULT_RISK_PARAMS) == 2) == \
                   (fixed_cost(seats, P) == P.large_fixed_cost)


# ===========================================================================
# 二、两条保本线
# ===========================================================================

class TestFloorPrice:

    def test_等于每人变动成本(self):
        assert floor_price(P) == P.marginal_cost_per_seat

    def test_与人数和原价无关(self):
        # 这条线的定义里根本没有这两个参数 —— 结构上就保证了。
        assert floor_price(P) == 20.0


class TestBreakEvenPrice:

    def test_公式(self):
        # 10 人本：固定 480，变动 20/人
        assert break_even_price(10, 10, P) == pytest.approx(48.0 + 20.0)
        assert break_even_price(5, 10, P) == pytest.approx(96.0 + 20.0)
        assert break_even_price(2, 10, P) == pytest.approx(240.0 + 20.0)

    def test_人越少保本价越高(self):
        # 固定成本摊到更少的人头上。
        prices = [break_even_price(n, 10, P) for n in [10, 8, 5, 3, 1]]
        assert prices == sorted(prices)

    def test_人数为0或缺失返回None(self):
        # 除零无意义。注意这里返回 None 而不是 0 —— "算不出来"不等于"不用钱"。
        assert break_even_price(0, 10, P) is None
        assert break_even_price(None, 10, P) is None
        assert break_even_price(pd.NA, 10, P) is None

    def test_高于边际底价(self):
        # 固定成本永远为正，所以均摊保本价必然高于边际底价 —— 两条线不会交叉。
        for n in [1, 3, 5, 10]:
            assert break_even_price(n, 10, P) > floor_price(P)

    def test_只能按满员用(self):
        # 差 2 个人时均摊保本价 260 元，已经高于原价 168 —— 这时它不能当售卖下限用。
        # 这正是为什么必须另有一条与人数无关的 floor_price。
        assert break_even_price(2, 10, P) > 原价
        assert floor_price(P) < 原价


# ===========================================================================
# 三、折扣阶梯
# ===========================================================================

class TestDiscountTier:

    def test_四档划分(self):
        assert discount_tier(0.5, P) == (TIER_LIST, 1.0)
        assert discount_tier(1.2, P) == (TIER_LIGHT, P.light_discount)
        assert discount_tier(2.0, P) == (TIER_MEDIUM, P.medium_discount)
        assert discount_tier(4.0, P) == (TIER_HEAVY, P.heavy_discount)

    def test_边界值落在更轻的一档(self):
        # 用 <= ：倍数正好 1.0 时不打折（自然流量刚好够），正好 1.5 时还是轻让。
        # 与工具 4 的 time_level 同一取舍 —— 临界点上不做超出必要的事。
        assert discount_tier(1.0, P) == (TIER_LIST, 1.0)
        assert discount_tier(1.01, P)[0] == TIER_LIGHT
        assert discount_tier(P.light_multiple, P)[0] == TIER_LIGHT
        assert discount_tier(P.light_multiple + 0.01, P)[0] == TIER_MEDIUM
        assert discount_tier(P.medium_multiple, P)[0] == TIER_MEDIUM
        assert discount_tier(P.medium_multiple + 0.01, P)[0] == TIER_HEAVY

    def test_算不出来时不打折(self):
        # 同工具 4：数据缺失不施加压力，也不让利。
        assert discount_tier(None, P) == (TIER_LIST, 1.0)
        assert discount_tier(pd.NA, P) == (TIER_LIST, 1.0)

    def test_单调性_倍数越大折扣越狠(self):
        rates = [discount_tier(m, P)[1] for m in [0.2, 0.9, 1.0, 1.2, 1.5, 2.0, 2.5, 5.0, 20.0]]
        assert rates == sorted(rates, reverse=True)

    def test_折扣率永远不高于1(self):
        # 模型只会让利，不会加价 —— 这个工具没有"旺季提价"的能力，也不该有。
        for m in [0.1, 1.0, 1.5, 2.5, 100.0]:
            assert discount_tier(m, P)[1] <= 1.0


class TestContributionPerSeat:

    def test_等于售价减变动成本(self):
        assert contribution_per_seat(148, P) == pytest.approx(128.0)

    def test_缺失返回None(self):
        assert contribution_per_seat(None, P) is None
        assert contribution_per_seat(pd.NA, P) is None

    def test_默认折扣下恒为正(self):
        # 只要折扣价高于变动成本，多来一个人就是净赚。
        # 168 元的本在四档折扣下都远高于 20 元变动成本。
        for m in [0.5, 1.2, 2.0, 4.0]:
            tier, rate = discount_tier(m, P)
            assert contribution_per_seat(round(原价 * rate), P) > 0

    def test_售价低于变动成本时为负(self):
        # 极端情况：原价本身就很低（比如 20 元体验场打 8 折 = 16 元）。
        # 这时模型如实给出负数 —— "多来一个多亏一块"，不粉饰。
        assert contribution_per_seat(16, P) < 0


# ===========================================================================
# 四、与工具 4 的对齐（本轮核心）
# ===========================================================================

class TestToolAlignment:
    """同一场在两个工具里的结论**可以不同**，而且必须不同。

    工具 4 问「填不填得满」，工具 5 问「能不能少亏」。填不满不等于不该少亏。
    """

    def test_救不回来的场次也要给出折扣(self):
        # 6 人本差 4 人、剩 2 小时：需要的到客速率是常态的 4 倍。
        row = _定价一场(剩几小时=2, 总位=6, 已约=2)

        # 工具 4 的结论：救不回来，别浪费推广力气。
        assert row["rescue_level"] == 0
        assert row["priority_level"] == PRIORITY_NORMAL

        # 工具 5 的结论：但降价能少亏，要给重让。
        assert row["rate_multiple"] == pytest.approx(4.0)
        assert row["discount_tier"] == TIER_HEAVY
        assert row["recommended_price"] == 134        # 168 × 0.8 = 134.4 -> 134
        assert row["contribution_per_seat"] == pytest.approx(114.0)

    def test_要推广不等于要让利(self):
        # 8 人本差 1 人、剩 2 小时：倍数恰好 1.0（自然流量刚好够）。
        row = _定价一场(剩几小时=2, 总位=8, 已约=7)

        # 工具 4 的结论：现在就动手（紧迫 2 × 可救 2 = 4 分）。
        assert row["priority_level"] == PRIORITY_HIGH

        # 工具 5 的结论：但不用动价格 —— 发条朋友圈就够了。
        assert row["discount_tier"] == TIER_LIST
        assert row["recommended_price"] == 原价
        assert row["seat_saving"] == 0

    def test_两个工具都对时才是真正的甜点区(self):
        # 8 人本差 2 人、剩 2 小时：倍数 2.0。
        # 工具 4：紧迫 2 × 可救 1 = 2 分 -> 关注（该推）
        # 工具 5：中让 88 折（该让）
        row = _定价一场(剩几小时=2, 总位=8, 已约=6)
        assert row["priority_level"] == "关注"
        assert row["discount_tier"] == TIER_MEDIUM


# ===========================================================================
# 五、硬规则与兜底
# ===========================================================================

class TestHardRules:

    def test_满员不打折(self):
        # 没有空位可让 —— 折扣无处作用。差价为 0。
        row = _定价一场(剩几小时=0.5, 总位=6, 已约=6)
        assert row["remaining_seats"] == 0
        assert row["discount_tier"] == TIER_LIST
        assert row["recommended_price"] == 原价
        assert row["seat_saving"] == 0

    def test_超售也不打折(self):
        row = _定价一场(剩几小时=0.5, 总位=8, 已约=9)
        assert row["remaining_seats"] == -1
        assert row["discount_tier"] == TIER_LIST

    def test_满员时仍然给出账目(self):
        # 不打折不等于不给数字 —— 店长还是要看这场赚了多少。
        row = _定价一场(剩几小时=0.5, 总位=6, 已约=6)
        assert row["sunk_cost"] == 260
        assert not pd.isna(row["profit_if_sold_out"])

    def test_原价缺失时不猜(self):
        # 没有原价，折扣率无从谈起。给 None 并打标签，不默认成某个值。
        row = _定价一场(剩几小时=2, 总位=8, 已约=6, 原价=None)
        assert row["discount_tier"] == TIER_UNKNOWN
        assert pd.isna(row["recommended_price"])
        assert pd.isna(row["discount_rate"])
        assert row["price_note"] == "原价缺失"

    def test_原价缺失仍给出成本侧(self):
        # 定不了价，但"这场黄了亏多少"照样算得出来 —— 这个信息不依赖原价。
        row = _定价一场(剩几小时=2, 总位=8, 已约=6, 原价=None)
        assert row["sunk_cost"] == 340
        assert row["floor_price"] == 20

    def test_空位未知时不定价(self):
        row = _定价一场(剩几小时=2, 总位=None, 已约=0)
        assert row["discount_tier"] == TIER_UNKNOWN
        assert row["price_note"] == "空位未知"

    def test_时间缺失时不打折(self):
        # 倍数算不出来 -> 不给折扣。宁可不干预，也不要瞎定价。
        row = _定价一场(剩几小时=None, 总位=8, 已约=0)
        assert row["discount_tier"] == TIER_LIST

    def test_已开场的不出现在结果里(self):
        assert price_sessions(
            _表([_场(剩几小时=-3, 总位=8, 已约=1)]), now=NOW).empty

    def test_已取消的不出现在结果里(self):
        assert price_sessions(
            _表([_场(剩几小时=3, 总位=8, 已约=1, status=STATUS_CANCELLED)]),
            now=NOW).empty


# ===========================================================================
# 六、账目
# ===========================================================================

class TestProfit:

    def test_满座利润(self):
        # 8 人本、已约 6（原价 168）、剩 2 个按推荐价 148 卖出：
        #   收入 = 6×168 + 2×148 = 1304
        #   成本 = 固定 340 + 变动 20×8 = 500
        #   利润 = 804
        row = _定价一场(剩几小时=2, 总位=8, 已约=6)
        assert row["break_even_price"] == pytest.approx(340 / 8 + 20)
        assert row["profit_if_sold_out"] == pytest.approx(804.0)

    def test_满员时按原价算利润(self):
        # 6 人本满员：收入 6×168 = 1008，成本 260 + 120 = 380，利润 628。
        row = _定价一场(剩几小时=0.5, 总位=6, 已约=6)
        assert row["profit_if_sold_out"] == pytest.approx(628.0)

    def test_黄掉的损失就是固定成本(self):
        # 没人来 -> 变动成本为 0 -> 亏的正好是排班那笔固定成本。
        # 定金在流局时原路退还，不构成门店的现金损失，所以不计入。
        row = _定价一场(剩几小时=2, 总位=8, 已约=6)
        assert row["sunk_cost"] == fixed_cost(8, P)

    def test_折扣不会让满座利润翻负(self):
        # 只要推荐价高于变动成本，打折后的满座利润仍然为正。
        for kwargs in [
            dict(剩几小时=2, 总位=8, 已约=6),    # 中让
            dict(剩几小时=2, 总位=6, 已约=2),    # 重让
        ]:
            row = _定价一场(**kwargs)
            assert row["profit_if_sold_out"] > 0


# ===========================================================================
# 七、底价外泄防护
# ===========================================================================

class TestPublicView:
    """公共不变量：门店保本底价仅内部可见，不进入任何对外文案。"""

    @pytest.fixture
    def priced(self):
        return price_sessions(_表([
            _场(剩几小时=2, 总位=8, 已约=6, 名="中让的场"),
            _场(剩几小时=2, 总位=6, 已约=2, 名="重让的场"),
        ]), now=NOW)

    def test_对外视图不含任何内部财务列(self, priced):
        pub = public_view(priced)
        for col in ["floor_price", "break_even_price", "sunk_cost", "profit_if_sold_out"]:
            assert col not in pub.columns, f"{col} 泄露到对外视图了"

    def test_对外视图保留定价字段(self, priced):
        pub = public_view(priced)
        for col in ["script_name", "list_price", "recommended_price", "discount_tier"]:
            assert col in pub.columns

    def test_内部列清单都在定价列里(self):
        # 防手滑：INTERNAL_COLUMNS 里写错一个名字，public_view 就剔不掉它。
        assert set(INTERNAL_COLUMNS) <= set(PRICING_COLUMNS)

    def test_对外视图的行数不变(self, priced):
        assert len(public_view(priced)) == len(priced)

    def test_不改动入参(self, priced):
        before = priced.copy()
        public_view(priced)
        assert priced.equals(before)


# ===========================================================================
# 八、结果形状与顺序
# ===========================================================================

class TestResultShape:

    def test_列齐全(self):
        row = _定价一场(剩几小时=2, 总位=8, 已约=6)
        for col in PRICING_COLUMNS:
            assert col in row.index, f"缺列 {col}"

    def test_保留评估列(self):
        # 定价结果 = 工具 4 的评估结果 + 定价列，Agent 调一次就能拿到全部依据。
        row = _定价一场(剩几小时=2, 总位=8, 已约=6)
        for col in ["rate_multiple", "fail_risk", "priority_level", "remaining_seats"]:
            assert col in row.index

    def test_保留原表列(self):
        row = _定价一场(剩几小时=2, 总位=8, 已约=6, 名="青川旧事")
        assert row["script_name"] == "青川旧事"
        assert row["list_price"] == 原价

    def test_顺序沿用工具4的优先级(self):
        # 不按价格重新排 —— 最该动手的场次排在前面。
        df = price_sessions(_表([
            _场(剩几小时=48, 总位=10, 已约=0, 名="不急的"),
            _场(剩几小时=2, 总位=8, 已约=6, 名="该救的"),
        ]), now=NOW)
        assert list(df["script_name"]) == ["该救的", "不急的"]

    def test_空表返回带列的空表(self):
        empty = price_sessions(_表([]), now=NOW)
        assert empty.empty
        for col in PRICING_COLUMNS:
            assert col in empty.columns

    def test_结果不落盘_输入表不被改动(self):
        raw = _表([_场(剩几小时=2, 总位=8, 已约=6)])
        before = raw.copy()
        price_sessions(raw, now=NOW)
        assert "discount_tier" not in raw.columns
        assert raw.equals(before)


# ===========================================================================
# 九、参数
# ===========================================================================

class TestParams:

    def test_参数不可变(self):
        with pytest.raises(FrozenInstanceError):
            P.marginal_cost_per_seat = 999

    def test_默认参数与占位值一致(self):
        # 这些是占位值，唯一能自动校验的就是"别被无声地改掉"。
        assert P.small_fixed_cost == 260.0
        assert P.medium_fixed_cost == 340.0
        assert P.large_fixed_cost == 480.0
        assert P.marginal_cost_per_seat == 20.0
        assert P.light_discount == 0.95
        assert P.medium_discount == 0.88
        assert P.heavy_discount == 0.80

    def test_改变动成本会改变底价和每座贡献(self):
        cheap = PricingParams(marginal_cost_per_seat=5.0)
        pricey = PricingParams(marginal_cost_per_seat=60.0)
        assert floor_price(cheap) == 5.0 and floor_price(pricey) == 60.0
        assert contribution_per_seat(148, cheap) > contribution_per_seat(148, pricey)

    def test_改折扣率会改变推荐价(self):
        stingy = PricingParams(medium_discount=0.99)
        row = _定价一场(剩几小时=2, 总位=8, 已约=6)
        assert row["recommended_price"] == 148          # 默认 0.88
        row2 = price_sessions(_表([_场(剩几小时=2, 总位=8, 已约=6)]),
                              now=NOW, params=stingy).iloc[0]
        assert row2["recommended_price"] == 166         # 168 × 0.99 = 166.32 -> 166

    def test_改规模分档线会改变固定成本(self):
        wide = PricingParams(medium_room_seats=9, large_room_seats=12)
        assert fixed_cost(8, wide) == wide.small_fixed_cost      # 8 位现在算小本
        assert fixed_cost(8, P) == P.medium_fixed_cost           # 默认算中本

    def test_不传参数时用默认值(self):
        a = price_sessions(_表([_场(剩几小时=2, 总位=8, 已约=6)]), now=NOW)
        b = price_sessions(_表([_场(剩几小时=2, 总位=8, 已约=6)]), now=NOW,
                           params=DEFAULT_PRICING_PARAMS)
        assert a.equals(b)


# ===========================================================================
# 十、档位常量
# ===========================================================================

class TestTiers:

    def test_四个可让利档位(self):
        assert DISCOUNT_TIERS == [TIER_LIST, TIER_LIGHT, TIER_MEDIUM, TIER_HEAVY]

    def test_无法定价不在让利档位里(self):
        # 它不是"让得多"或"让得少"，是"算不出来" —— 混进去会让人以为它是某一档。
        assert TIER_UNKNOWN not in DISCOUNT_TIERS

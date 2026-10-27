"""工具 4 空场风险识别 —— 单元测试。

运行：
  PYTHONUTF8=1 "C:/Users/Lenovo/anaconda3/python.exe" -m pytest tests/ -v

和工具 3 一样：**不联网、不需要 API key、不依赖系统时钟**（now 一律显式注入）。

这些测试**不校验具体阈值是否合理** —— 「6 小时算紧」「常态到客速率 0.5」这件事
没有对错，只能由门店标定。测试校验的是**结构**：

  时间越少越紧迫、需要的人比平时来的多就越危险、满员没有空场风险、
  倍数算不出来时不报警、救不回来的场次不被丢弃……

这些不依赖具体数字，是本模块正确性的真正所在。

本轮新增的重点是**排序方向**（TestSortingDirection）—— 见那一节的说明。
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import timedelta

import pandas as pd
import pytest

from app.tools.risk import (
    DEFAULT_RISK_PARAMS,
    FAIL_HIGH,
    FAIL_LOW,
    FAIL_MEDIUM,
    PRIORITY_HIGH,
    PRIORITY_NORMAL,
    PRIORITY_WATCH,
    RiskParams,
    assess_sessions,
    fail_risk,
    gap_ratio,
    high_risk_only,
    hours_left,
    needed_rate,
    priority_level,
    rate_multiple,
    rescue_level,
    stake_level,
    time_level,
)
from app.tools.storage import STATUS_CANCELLED, STATUS_PENDING, _coerce_types

# 所有用例共用的"现在"
NOW = pd.Timestamp("2026-10-20 19:30:00")

P = DEFAULT_RISK_PARAMS


def _表(rows) -> pd.DataFrame:
    """把一批场次记录整形成规范表（借用 storage 自己的整形逻辑）。

    经过 _coerce_types 后，remaining_seats 会自动由 total - booked 算出来，
    正好模拟真实读取路径（工具 4 的入参是 load_sessions_with_counts 的结果）。
    """
    defaults = {
        "store_name": "迷雾剧场",
        "total_seats": 8,
        "booked_seats": 0,
        "status": STATUS_PENDING,
    }
    return _coerce_types(pd.DataFrame([{**defaults, **row} for row in rows]))


def _场(*, 剩几小时=12, 总位=10, 已约=0, 名="某场", **extra) -> dict:
    """按「距现在还有几小时」描述一场，比手写时间戳直观。

    剩几小时=None 用来构造"开场时间缺失"的场次。
    """
    start = None if 剩几小时 is None else NOW + timedelta(hours=剩几小时)
    return {
        "script_name": 名,
        "start_time": start,
        "total_seats": 总位,
        "booked_seats": 已约,
        **extra,
    }


def _评估一场(**kwargs) -> pd.Series:
    """建一场 -> 评估 -> 返回结果行。"""
    return assess_sessions(_表([_场(**kwargs)]), now=NOW).iloc[0]


# ===========================================================================
# 一、时间与紧迫度
# ===========================================================================

class TestTimeLevel:

    def test_三档划分(self):
        assert time_level(24.0, P) == 0     # 充裕
        assert time_level(5.0, P) == 1      # 偏紧
        assert time_level(1.0, P) == 2      # 紧急

    def test_边界值落在更严重的一侧(self):
        # 用 <= 而不是 <：正好 6 小时算"偏紧"，正好 2 小时算"紧急"。
        # 与工具 3 的 is_expired 同一取舍 —— 临界点上宁可更保守。
        assert time_level(6.5, P) == 0
        assert time_level(6.0, P) == 1
        assert time_level(2.0, P) == 2
        assert time_level(2.5, P) == 1

    def test_缺失时返回0(self):
        # 算术上 0 是"最不紧迫"，语义上其实是"不知道"。
        # 仍取 0：宁可不报警，也不因数据缺失造假警报。
        assert time_level(None, P) == 0
        assert time_level(pd.NA, P) == 0
        assert time_level(float("nan"), P) == 0

    def test_单调性_时间越少档位不降(self):
        levels = [time_level(h, P) for h in [72, 12, 6.1, 6, 3, 2, 1, 0.25]]
        assert levels == sorted(levels)


class TestHoursLeft:

    def test_正常计算(self):
        assert hours_left(NOW + timedelta(hours=3), now=NOW) == pytest.approx(3.0)

    def test_已开场为负(self):
        assert hours_left(NOW - timedelta(hours=2), now=NOW) == pytest.approx(-2.0)

    def test_缺失或无法解析返回None(self):
        assert hours_left(None, now=NOW) is None
        assert hours_left(pd.NaT, now=NOW) is None
        assert hours_left("不是时间", now=NOW) is None


# ===========================================================================
# 二、核心量：需要的到客速率
# ===========================================================================

class TestNeededRate:
    """needed_rate = 剩余空位 ÷ 剩余小时，单位「人/小时」。"""

    def test_正常计算(self):
        # 还差 4 人、还剩 2 小时 -> 每小时得来 2 个人
        assert needed_rate(4, 2.0) == pytest.approx(2.0)

    def test_绝对人数口径_与总位无关(self):
        # 这是上一版"缺口比例"被替换掉的原因：6 人本差 3 人和 10 人本差 3 人，
        # 需要的到客速率完全一样 —— 找人这件事不因为房间大就变难。
        # 函数签名里根本没有 total_seats，从结构上保证了这个性质。
        assert needed_rate(3, 3.0) == needed_rate(3, 3.0) == pytest.approx(1.0)

    def test_满员返回0而不是None(self):
        # 0 = "一个人都不用再来"；None = "算不出来"。两回事，不能混。
        assert needed_rate(0, 2.0) == 0.0

    def test_超售也返回0(self):
        # 约超了（空位为负）同样不需要再来人。
        # 超售本身是另一个问题，不该在这里报成"空场风险"。
        assert needed_rate(-1, 2.0) == 0.0

    def test_时间已到或已过返回None(self):
        # 已经开场了，再来人也来不及 —— 算不出来，按 None 处理。
        assert needed_rate(4, 0.0) is None
        assert needed_rate(4, -1.0) is None

    def test_时间为缺失返回None(self):
        assert needed_rate(4, None) is None
        assert needed_rate(4, pd.NA) is None

    def test_空位缺失返回None(self):
        assert needed_rate(None, 2.0) is None
        assert needed_rate(pd.NA, 2.0) is None

    def test_时间越多人均压力越小(self):
        rates = [needed_rate(4, h) for h in [1, 2, 4, 8]]
        assert rates == sorted(rates, reverse=True)


class TestRateMultiple:

    def test_相对常态的倍数(self):
        assert rate_multiple(2.0, P) == pytest.approx(2.0 / P.normal_arrival_rate)

    def test_算不出来则返回None(self):
        assert rate_multiple(None, P) is None
        assert rate_multiple(pd.NA, P) is None

    def test_常态速率为0时不除零(self):
        # 门店如果没填这个参数（填了 0），应该退化成"算不出来"，
        # 而不是抛 ZeroDivisionError 把整批评估带崩。
        bad = RiskParams(normal_arrival_rate=0.0)
        assert rate_multiple(2.0, bad) is None


# ===========================================================================
# 三、输出一：会不会黄
# ===========================================================================

class TestFailRisk:
    """fail_risk 由倍数**单调**决定 —— 缺得越狠、时间越少，越可能黄。"""

    def test_三档划分(self):
        assert fail_risk(0.5, P) == FAIL_LOW       # 自然流量就够
        assert fail_risk(1.5, P) == FAIL_MEDIUM    # 悬
        assert fail_risk(4.0, P) == FAIL_HIGH      # 大概率黄

    def test_边界值落在更严重的一侧(self):
        # 倍数 <= 1 是"自然流量刚好够"，这是推出来的线，不是拍的：
        # 需要的到客速率不超过门店常态，意味着不用干预也能填满。
        assert fail_risk(1.0, P) == FAIL_LOW
        assert fail_risk(1.01, P) == FAIL_MEDIUM
        assert fail_risk(P.hard_multiple, P) == FAIL_MEDIUM
        assert fail_risk(P.hard_multiple + 0.01, P) == FAIL_HIGH

    def test_算不出来时返回低(self):
        # 同 time_level 的取舍：不因数据缺失造假警报。
        assert fail_risk(None, P) == FAIL_LOW
        assert fail_risk(pd.NA, P) == FAIL_LOW

    def test_单调性_倍数越大等级不降(self):
        order = {FAIL_LOW: 0, FAIL_MEDIUM: 1, FAIL_HIGH: 2}
        levels = [order[fail_risk(m, P)] for m in [0.1, 0.9, 1.0, 1.5, 2.0, 3.0, 10.0]]
        assert levels == sorted(levels)

    def test_满员时倍数为0判低(self):
        assert _评估一场(剩几小时=0.5, 总位=6, 已约=6)["fail_risk"] == FAIL_LOW


# ===========================================================================
# 四、输出二：可救性与优先级
# ===========================================================================

class TestRescueLevel:
    """可救性和 fail_risk 用同一个倍数，但读的方向相反。"""

    def test_三档划分(self):
        assert rescue_level(0.5, 3, P) == 2    # 顺手推一把就成
        assert rescue_level(1.5, 3, P) == 1    # 要赶一赶
        assert rescue_level(4.0, 3, P) == 0    # 超出常态太多，别费劲了

    def test_与fail_risk方向相反(self):
        # 倍数 4.0：会不会黄 = 高（预警要响），可救性 = 0（不值得动手）。
        # 两者不矛盾 —— 一个回答"要不要提前通知客人"，一个回答"要不要现在推"。
        assert fail_risk(4.0, P) == FAIL_HIGH
        assert rescue_level(4.0, 3, P) == 0

    def test_满员可救性为0(self):
        # 没有空位要填，不存在"救"这回事。
        assert rescue_level(0.0, 0, P) == 0
        assert rescue_level(0.0, -1, P) == 0

    def test_算不出来时可救性为0(self):
        # 注意和 fail_risk 的取舍**不一样**：那边算不出来判"低"（不报警），
        # 这边算不出来判 0（不推荐去动手）。缺省动作都是"别折腾店长"。
        assert rescue_level(None, 3, P) == 0
        assert rescue_level(pd.NA, 3, P) == 0

    def test_空位缺失时可救性为0(self):
        assert rescue_level(1.0, None, P) == 0
        assert rescue_level(1.0, pd.NA, P) == 0

    def test_单调性_倍数越大可救性不升(self):
        levels = [rescue_level(m, 3, P) for m in [0.1, 0.9, 1.0, 1.5, 2.0, 3.0, 10.0]]
        assert levels == sorted(levels, reverse=True)


class TestStakeLevel:

    def test_三档划分(self):
        assert stake_level(6, P) == 0     # 小本
        assert stake_level(8, P) == 1     # 中本
        assert stake_level(12, P) == 2    # 大本

    def test_边界值落在更重的一侧(self):
        assert stake_level(6, P) == 0
        assert stake_level(P.medium_room_seats, P) == 1
        assert stake_level(P.large_room_seats - 1, P) == 1
        assert stake_level(P.large_room_seats, P) == 2

    def test_缺失时为0(self):
        assert stake_level(None, P) == 0
        assert stake_level(pd.NA, P) == 0
        assert stake_level(0, P) == 0

    def test_规模不影响优先级分数(self):
        # 同样的空位与时间，只改总位 —— 分数必须一模一样。
        # 反例：12 人本剩 1 人 2 小时后开场，若规模计入打分会被判"高危"，
        # 可它 92% 满。规模回答的是"空掉多疼"，不是"先不先救"。
        小 = _评估一场(剩几小时=3, 总位=6, 已约=5)
        大 = _评估一场(剩几小时=3, 总位=12, 已约=11)
        assert 小["priority_score"] == 大["priority_score"]
        assert 小["priority_level"] == 大["priority_level"]
        assert 小["rate_multiple"] == pytest.approx(大["rate_multiple"])
        assert 小["stake_level"] == 0 and 大["stake_level"] == 2


class TestPriorityLevel:
    """优先级 = 紧迫档 × 可救性档。"""

    def test_对照表(self):
        # 这张表就是本模块的核心主张：只有"快开场了 + 还救得回来"才叫高危。
        期望 = [
            # (紧迫, 可救, 等级, 分数)
            (2, 2, PRIORITY_HIGH, 4),      # 快开场了，顺手就能捞回来 -> 现在就动手
            (2, 1, PRIORITY_WATCH, 2),     # 快开场了，但得赶一赶
            (2, 0, PRIORITY_NORMAL, 0),    # 快开场了，但已经没戏 -> 别浪费力气
            (1, 2, PRIORITY_WATCH, 2),
            (1, 1, PRIORITY_WATCH, 1),     # 还来得及，但已经开始悬了
            (1, 0, PRIORITY_NORMAL, 0),
            (0, 2, PRIORITY_NORMAL, 0),    # 时间还多，它自己会满
            (0, 1, PRIORITY_NORMAL, 0),
            (0, 0, PRIORITY_NORMAL, 0),
        ]
        for t, r, level, score in 期望:
            assert priority_level(t, r, P) == (level, score), f"紧迫{t} 可救{r}"

    def test_相乘而不是取max(self):
        # 取 max 会把 紧迫1 + 可救1 判成 1 分；相乘得到 1 分，两者在这格相同。
        # 真正的区别在 (2,0) 和 (0,2)：max 都得到 2 分（该动手），
        # 相乘得到 0 分（歇着）。下面这两条钉住的就是这个差异。
        assert priority_level(2, 0, P)[0] == PRIORITY_NORMAL
        assert priority_level(0, 2, P)[0] == PRIORITY_NORMAL

    def test_分数等于两档之积(self):
        for t in range(3):
            for r in range(3):
                assert priority_level(t, r, P)[1] == t * r

    def test_只有两档都高才是高危(self):
        for t in range(3):
            for r in range(3):
                level = priority_level(t, r, P)[0]
                assert (level == PRIORITY_HIGH) == (t == 2 and r == 2)


# ===========================================================================
# 五、端到端：两个输出
# ===========================================================================

class TestTwoOutputs:
    """assess_sessions 同时给出"会不会黄"和"该先救哪场"。"""

    def test_一个结果两个维度(self):
        row = _评估一场(剩几小时=1, 总位=8, 已约=6)     # 差 2 人，剩 1 小时
        # 需要的速率 2.0，常态 0.5 -> 倍数 4.0
        assert row["needed_rate"] == pytest.approx(2.0)
        assert row["rate_multiple"] == pytest.approx(4.0)
        assert row["fail_risk"] == FAIL_HIGH        # 大概率黄
        assert row["rescue_level"] == 0             # 但救不回来
        assert row["priority_level"] == PRIORITY_NORMAL

    def test_救不回来的场次不被丢弃(self):
        # 它只是不排在前面，仍然在结果里，好让店长/看板按 fail_risk 单独筛出
        # 「该考虑流局的场次」。丢弃它等于把预警一起丢了。
        df = assess_sessions(_表([_场(剩几小时=2, 总位=6, 已约=2, 名="没戏的场")]), now=NOW)
        assert len(df) == 1
        assert df.iloc[0]["fail_risk"] == FAIL_HIGH

    def test_远场不误报(self):
        # 8 人本差 1 人，还剩两天 —— 它自己会满，不需要谁去推。
        # 上一版按"缺口比例"会把它标成需要关注；现在紧迫档是 0，直接正常。
        row = _评估一场(剩几小时=48, 总位=8, 已约=7)
        assert row["rate_multiple"] < 1.0
        assert row["fail_risk"] == FAIL_LOW
        assert row["priority_level"] == PRIORITY_NORMAL

    def test_空场且时间充裕也是正常(self):
        # 8 人本一个人都没约，但还有三天 —— 现在不该动手。
        row = _评估一场(剩几小时=72, 总位=8, 已约=0)
        assert row["priority_level"] == PRIORITY_NORMAL


# ===========================================================================
# 六、硬规则：满员
# ===========================================================================

class TestHardRules:

    def test_满员时所有档位归零(self):
        row = _评估一场(剩几小时=0.5, 总位=6, 已约=6)
        assert row["time_level"] == 0
        assert row["fail_risk"] == FAIL_LOW
        assert row["rescue_level"] == 0
        assert row["stake_level"] == 0
        assert row["priority_score"] == 0
        assert row["priority_level"] == PRIORITY_NORMAL

    def test_满员不看时间(self):
        # 30 分钟后开场 vs 两天后开场，结论完全一样 —— 满员就是满员。
        急 = _评估一场(剩几小时=0.5, 总位=6, 已约=6)
        缓 = _评估一场(剩几小时=48, 总位=6, 已约=6)
        for col in ["fail_risk", "rescue_level", "stake_level", "priority_level"]:
            assert 急[col] == 缓[col]

    def test_满员仍保留事实列(self):
        # hours_left 是**事实**，如实写着 0.5，不跟着档位一起归零。
        # 归零的只是"这项给风险贡献了几分"，不是事实本身。
        row = _评估一场(剩几小时=0.5, 总位=6, 已约=6)
        assert row["hours_left"] == pytest.approx(0.5)

    def test_超售也按满员处理(self):
        # 8 人本约了 9 个 —— 那是超售问题，不是空场问题，不该在这里报警。
        row = _评估一场(剩几小时=0.5, 总位=8, 已约=9)
        assert row["remaining_seats"] == -1
        assert row["priority_level"] == PRIORITY_NORMAL
        assert row["fail_risk"] == FAIL_LOW

    def test_满员的不出现在高危里(self):
        df = assess_sessions(
            _表([_场(剩几小时=0.5, 总位=6, 已约=6, 名="满员的场")]), now=NOW)
        assert high_risk_only(df).empty


# ===========================================================================
# 七、兜底：数据缺失 / 已开场 / 已取消
# ===========================================================================

class TestFallbacks:
    """原则：宁可不报警，也不因数据缺失造假警报。"""

    def test_开场时间缺失时不报警但保留行(self):
        row = _评估一场(剩几小时=None, 总位=8, 已约=1, 名="没时间的场")
        assert pd.isna(row["hours_left"])
        assert pd.isna(row["needed_rate"]) or row["needed_rate"] is None
        assert row["time_level"] == 0
        assert row["fail_risk"] == FAIL_LOW
        assert row["priority_level"] == PRIORITY_NORMAL

    def test_总位缺失时规模档为0(self):
        row = _评估一场(剩几小时=3, 总位=None, 已约=1)
        assert row["stake_level"] == 0

    def test_已开场的不出现在结果里(self):
        df = assess_sessions(
            _表([_场(剩几小时=-3, 总位=8, 已约=1, 名="昨天那场")]), now=NOW)
        assert df.empty

    def test_已取消的不出现在结果里(self):
        df = assess_sessions(
            _表([_场(剩几小时=3, 总位=8, 已约=1, 名="取消了的场",
                     status=STATUS_CANCELLED)]), now=NOW)
        assert df.empty

    def test_include_expired可以看到已开场的(self):
        # 兜底开关：确实需要评估全部场次时用。默认关着，
        # 因为调用方是 LLM，不该要求它记得先调工具 3。
        df = assess_sessions(
            _表([_场(剩几小时=-3, 总位=8, 已约=1, 名="昨天那场")]),
            now=NOW, include_expired=True)
        assert len(df) == 1

    def test_空表返回带列的空表(self):
        empty = assess_sessions(_表([]), now=NOW)
        assert empty.empty
        # 列必须齐全 —— 否则下游（工具 7）取列会 KeyError。
        for col in ["fail_risk", "priority_level", "priority_score", "stake_level"]:
            assert col in empty.columns


# ===========================================================================
# 八、排序
# ===========================================================================

class TestSorting:
    """排序：priority_score 降序 -> stake_level 降序 -> start_time 升序。"""

    def test_按优先级分降序(self):
        df = assess_sessions(_表([
            _场(剩几小时=3, 总位=10, 已约=9, 名="该救的"),      # 紧迫1 可救2 -> 2 分
            _场(剩几小时=48, 总位=10, 已约=0, 名="不急的"),      # 紧迫0 -> 0 分
        ]), now=NOW)
        assert list(df["script_name"]) == ["该救的", "不急的"]

    def test_同样该救的里面先救大的(self):
        # 分数相同，规模大的排前面。
        df = assess_sessions(_表([
            _场(剩几小时=3, 总位=8, 已约=7, 名="中本"),
            _场(剩几小时=3, 总位=12, 已约=11, 名="大本"),
        ]), now=NOW)
        assert df.iloc[0]["priority_score"] == df.iloc[1]["priority_score"]
        assert list(df["script_name"]) == ["大本", "中本"]

    def test_分数规模都相同时先救早开场的(self):
        df = assess_sessions(_表([
            _场(剩几小时=3, 总位=10, 已约=9, 名="晚的"),
            _场(剩几小时=1, 总位=10, 已约=9, 名="早的"),
        ]), now=NOW)
        # 两场分数都是 2、规模都是 2（紧迫档不同但乘积相同），靠 start_time 分先后。
        assert df.iloc[0]["priority_score"] == df.iloc[1]["priority_score"]
        assert df.iloc[0]["stake_level"] == df.iloc[1]["stake_level"]
        assert list(df["script_name"]) == ["早的", "晚的"]

    def test_结果不落盘_输入表不被改动(self):
        raw = _表([_场(剩几小时=3, 总位=10, 已约=9)])
        before = raw.copy()
        assess_sessions(raw, now=NOW)
        assert "fail_risk" not in raw.columns
        assert raw.equals(before)


class TestSortingDirection:
    """**本轮的核心回归** —— 排序方向。

    上一版只算了一个分数叫「危险度」，又拿它当排序依据。结果是：
    已经救不回来的场次排在最前面，推一把就锁死的场次排在末尾 —— 正好排反。

    根因是结构性的：缺口大 = 情况糟（危险度高），但同时 缺口大 = 难救（可救性低）。
    同一个变量回答两个方向相反的问题。所以现在拆成 fail_risk 和 priority_level，
    排序只按后者。

    下面这两场来自用户的实际评审，是这个重构的直接起因。
    """

    没戏的 = _场(剩几小时=2, 总位=6, 已约=2, 名="差4人剩2小时")     # 差 4 人
    该救的 = _场(剩几小时=3, 总位=8, 已约=7, 名="差1人剩3小时")     # 差 1 人

    def test_救得回来的必须排在救不回来的前面(self):
        df = assess_sessions(_表([self.没戏的, self.该救的]), now=NOW)
        assert list(df["script_name"]) == ["差1人剩3小时", "差4人剩2小时"]

    def test_两场的两个输出各自正确(self):
        df = assess_sessions(_表([self.没戏的, self.该救的]), now=NOW)
        row = df.set_index("script_name")

        没戏 = row.loc["差4人剩2小时"]
        assert 没戏["rate_multiple"] == pytest.approx(4.0)     # 需要的速率是常态的 4 倍
        assert 没戏["fail_risk"] == FAIL_HIGH                 # 会不会黄 -> 高（预警要响）
        assert 没戏["rescue_level"] == 0                      # 可救性 -> 没戏
        assert 没戏["priority_level"] == PRIORITY_NORMAL      # 优先级 -> 不动手

        该救 = row.loc["差1人剩3小时"]
        assert 该救["rate_multiple"] == pytest.approx(2 / 3, abs=0.01)
        assert 该救["fail_risk"] == FAIL_LOW                  # 会不会黄 -> 低
        assert 该救["rescue_level"] == 2                      # 可救性 -> 顺手
        assert 该救["priority_level"] == PRIORITY_WATCH       # 优先级 -> 该动手

    def test_危险度排序会把顺序排反(self):
        # 把两场的 priority_score 和 fail_risk 的序号并排看：
        # fail_risk（危险度）在"没戏的"那场更高，priority_score 却更低。
        # 这条测试的价值就在于把这个**反向**显式钉住 ——
        # 哪天有人把排序改回按 fail_risk 排，这里立刻会红。
        order = {FAIL_LOW: 0, FAIL_MEDIUM: 1, FAIL_HIGH: 2}
        df = assess_sessions(_表([self.没戏的, self.该救的]), now=NOW)
        row = df.set_index("script_name")

        危险度差 = order[row.loc["差4人剩2小时", "fail_risk"]]
        危险度差2 = order[row.loc["差1人剩3小时", "fail_risk"]]
        assert 危险度差 > 危险度差2                                       # 危险度更高
        assert row.loc["差4人剩2小时", "priority_score"] < row.loc["差1人剩3小时", "priority_score"]


class TestHighRiskOnly:

    def test_只挑出优先级高危的(self):
        # 高危要求两档都拉满：紧迫 2（剩 <= 2 小时）+ 可救 2（倍数 <= 1）。
        # 差 1 人、剩 2 小时 -> 需要的速率 0.5 = 常态 -> 倍数恰好 1.0。
        df = assess_sessions(_表([
            _场(剩几小时=2, 总位=8, 已约=7, 名="高危的"),      # 紧迫2 可救2 -> 4 分
            _场(剩几小时=3, 总位=8, 已约=7, 名="关注的"),      # 紧迫1 可救2 -> 2 分
            _场(剩几小时=48, 总位=8, 已约=0, 名="正常的"),
        ]), now=NOW)
        高危 = high_risk_only(df)
        assert list(高危["script_name"]) == ["高危的"]

    def test_保留原有排序(self):
        df = assess_sessions(_表([
            _场(剩几小时=2, 总位=8, 已约=7, 名="高危小本"),
            _场(剩几小时=2, 总位=12, 已约=11, 名="高危大本"),
        ]), now=NOW)
        assert list(high_risk_only(df)["script_name"]) == ["高危大本", "高危小本"]

    def test_没有高危时返回空表(self):
        df = assess_sessions(_表([_场(剩几小时=48, 总位=8, 已约=0)]), now=NOW)
        assert high_risk_only(df).empty


# ===========================================================================
# 九、参数
# ===========================================================================

class TestParams:

    def test_参数不可变(self):
        with pytest.raises(FrozenInstanceError):
            P.tight_hours = 99

    def test_默认参数与占位值一致(self):
        # 这些数字是占位值，唯一能自动校验的就是"别被无声地改掉"。
        assert P.tight_hours == 6.0
        assert P.urgent_hours == 2.0
        assert P.normal_arrival_rate == 0.5
        assert P.hard_multiple == 2.0

    def test_改常态到客速率会改变定级(self):
        # 同一场：差 2 人剩 1 小时，需要的速率是 2.0 人/小时。
        # 门店常态来 0.5 人/小时 -> 倍数 4.0 -> 救不回来；
        # 门店常态来 4 人/小时   -> 倍数 0.5 -> 顺手就成。
        # 谁高谁低完全取决于这个参数，所以门店接入时必须先标定它。
        慢店 = RiskParams(normal_arrival_rate=0.5)
        快店 = RiskParams(normal_arrival_rate=4.0)

        慢 = assess_sessions(_表([_场(剩几小时=1, 总位=8, 已约=6)]), now=NOW, params=慢店).iloc[0]
        快 = assess_sessions(_表([_场(剩几小时=1, 总位=8, 已约=6)]), now=NOW, params=快店).iloc[0]

        assert 慢["rescue_level"] == 0 and 快["rescue_level"] == 2
        assert 慢["fail_risk"] == FAIL_HIGH and 快["fail_risk"] == FAIL_LOW

    def test_改紧迫线会改变定级(self):
        宽 = RiskParams(urgent_hours=8.0, tight_hours=12.0)
        row = assess_sessions(_表([_场(剩几小时=5, 总位=8, 已约=7)]), now=NOW, params=宽).iloc[0]
        assert row["time_level"] == 2       # 按默认参数只有 1 档

    def test_不传参数时用默认值(self):
        a = assess_sessions(_表([_场(剩几小时=1, 总位=8, 已约=6)]), now=NOW)
        b = assess_sessions(_表([_场(剩几小时=1, 总位=8, 已约=6)]), now=NOW,
                            params=DEFAULT_RISK_PARAMS)
        assert a.equals(b)


# ===========================================================================
# 十、展示列
# ===========================================================================

class TestGapRatio:
    """gap_ratio 只作展示，不参与任何判断。"""

    def test_正常计算(self):
        assert gap_ratio(3, 8) == pytest.approx(0.375)

    def test_缺失时返回None(self):
        assert gap_ratio(None, 8) is None
        assert gap_ratio(3, None) is None
        assert gap_ratio(3, 0) is None

    def test_不影响任何判断(self):
        # 6 人本差 3 人（缺 50%）和 8 人本差 3 人（缺 37.5%）：
        # 缺口比例不同，但需要的到客速率、会不会黄、优先级**完全相同**。
        # 这是上一版那条错误论证（"小房间更危险"）被删掉的原因 ——
        # 两场都还差 3 个人，找人这件事不因为房间大就变难。
        小 = _评估一场(剩几小时=3, 总位=6, 已约=3)
        大 = _评估一场(剩几小时=3, 总位=8, 已约=5)

        assert 小["gap_ratio"] != 大["gap_ratio"]                    # 展示值确实不同
        assert 小["needed_rate"] == pytest.approx(大["needed_rate"])  # 但核心量相同
        assert 小["fail_risk"] == 大["fail_risk"]
        assert 小["priority_level"] == 大["priority_level"]


class TestAssessmentColumns:

    def test_结果列齐全(self):
        row = _评估一场(剩几小时=3, 总位=8, 已约=7)
        for col in ["hours_left", "gap_ratio", "needed_rate", "rate_multiple",
                    "time_level", "fail_risk", "rescue_level", "stake_level",
                    "priority_score", "priority_level"]:
            assert col in row.index, f"缺列 {col}"

    def test_保留原始场次列(self):
        # 评估结果是"原表 + 新列"，不是只剩评估列 —— 下游（工具 7）要拿
        # script_name / start_time / booked_seats 去写文案。
        # （_表 是直接整形的，没有 session_id；真实路径的 id 由 storage 生成。）
        row = _评估一场(剩几小时=3, 总位=8, 已约=7, 名="青川旧事")
        assert row["script_name"] == "青川旧事"
        assert row["booked_seats"] == 7
        assert row["remaining_seats"] == 1

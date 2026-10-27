"""工具 3 过期过滤 —— 单元测试。

运行：
  PYTHONUTF8=1 "C:/Users/Lenovo/anaconda3/python.exe" -m pytest tests/ -v

这些测试**不联网、不需要 API key**，而且**不依赖系统时钟**：
所有用例都把 now 显式注入。否则「开场时间恰好等于现在」这类边界根本写不出来，
而且测试会随运行时刻不同而时红时绿。
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd
import pytest

from app.tools.expiry import (
    derive_status,
    filter_active,
    is_cancelled,
    is_expired,
    split_expired,
    with_derived_status,
)
from app.tools.storage import (
    STATUS_CANCELLED,
    STATUS_PENDING,
    STATUS_STARTED,
    _coerce_types,
)

# 所有用例共用的"现在"
NOW = pd.Timestamp("2026-10-20 19:30:00")


def _表(rows) -> pd.DataFrame:
    """把一批场次记录整形成规范表（借用 storage 自己的整形逻辑）。"""
    defaults = {
        "store_name": "迷雾剧场",
        "total_seats": 8,
        "booked_seats": 0,
        "status": STATUS_PENDING,
    }
    return _coerce_types(pd.DataFrame([{**defaults, **row} for row in rows]))


# ===========================================================================
# 一、单值判定与边界
# ===========================================================================

class TestIsExpired:

    def test_开场时间恰好等于现在算过期(self):
        # 用 <= 而不是 <：已经开场就不能再卖票了，差一秒也不行。
        assert is_expired(NOW, now=NOW) is True

    def test_比现在早一秒算过期(self):
        assert is_expired(NOW - timedelta(seconds=1), now=NOW) is True

    def test_比现在晚一秒不算过期(self):
        assert is_expired(NOW + timedelta(seconds=1), now=NOW) is False

    def test_开场时间缺失算未过期(self):
        # "无法判定"不等于"已失效"。当成过期剔掉 = 静默吞掉一条真实场次。
        assert is_expired(None, now=NOW) is False
        assert is_expired(pd.NaT, now=NOW) is False

    def test_各种时间写法都能识别(self):
        assert is_expired("2026-10-20 19:29", now=NOW) is True
        assert is_expired("2026/10/20 19:31", now=NOW) is False
        assert is_expired(datetime(2026, 10, 20, 19, 29), now=NOW) is True
        assert is_expired("2026-10-20 19:29:59", now=NOW) is True

    def test_看不懂的时间不算过期而不是报错(self):
        # 读表阶段宽容：一条格式错误的数据不该让整批场次都处理不了
        assert is_expired("下周三晚上", now=NOW) is False


# ===========================================================================
# 二、整表切分
# ===========================================================================

class TestSplitExpired:

    @pytest.fixture
    def 混合场次(self):
        return _表([
            {"script_name": "昨天那场", "start_time": "2026-10-19 19:00"},
            {"script_name": "一小时后", "start_time": "2026-10-20 20:30"},
            {"script_name": "明天那场", "start_time": "2026-10-21 19:00"},
        ])

    def test_混合场次被正确切开(self, 混合场次):
        有效, 过期 = split_expired(混合场次, now=NOW)
        assert list(过期["script_name"]) == ["昨天那场"]
        assert list(有效["script_name"]) == ["一小时后", "明天那场"]

    def test_两张表行数之和等于输入行数(self, 混合场次):
        # 不丢不重 —— 这是切分函数最基本的正确性
        有效, 过期 = split_expired(混合场次, now=NOW)
        assert len(有效) + len(过期) == len(混合场次) == 3

    def test_全部过期时有效那张是空表(self):
        df = _表([
            {"script_name": "A", "start_time": "2026-10-18 19:00"},
            {"script_name": "B", "start_time": "2026-10-19 19:00"},
        ])
        有效, 过期 = split_expired(df, now=NOW)
        assert 有效.empty
        assert len(过期) == 2

    def test_开场时间缺失的场次保留在有效一侧(self):
        df = _表([
            {"script_name": "时间待定", "start_time": None},
            {"script_name": "昨天那场", "start_time": "2026-10-19 19:00"},
        ])
        有效, 过期 = split_expired(df, now=NOW)
        assert list(有效["script_name"]) == ["时间待定"]
        assert list(过期["script_name"]) == ["昨天那场"]

    def test_不修改输入表(self, 混合场次):
        原始行数 = len(混合场次)
        split_expired(混合场次, now=NOW)
        assert len(混合场次) == 原始行数


# ===========================================================================
# 三、filter_active —— 下游工具的直接入口
# ===========================================================================

class TestFilterActive:

    def test_排除已过期(self):
        df = _表([
            {"script_name": "昨天那场", "start_time": "2026-10-19 19:00"},
            {"script_name": "一小时后", "start_time": "2026-10-20 20:30"},
        ])
        assert list(filter_active(df, now=NOW)["script_name"]) == ["一小时后"]

    def test_排除已取消(self):
        df = _表([
            {"script_name": "被取消的", "start_time": "2026-10-21 19:00",
             "status": STATUS_CANCELLED},
            {"script_name": "正常的", "start_time": "2026-10-21 19:00"},
        ])
        assert list(filter_active(df, now=NOW)["script_name"]) == ["正常的"]

    def test_状态写着待开场但时间已过_仍然被排除(self):
        # 核心规则：时间说了算，不信任存储的 status 字段。
        # 和 storage 里 booked_seats 的处理是同一个原则。
        df = _表([
            {"script_name": "忘了改状态的", "start_time": "2026-10-19 19:00",
             "status": STATUS_PENDING},
        ])
        assert filter_active(df, now=NOW).empty

    def test_已取消但时间未到_不算过期(self):
        # split_expired 只按时间切，取消与否是另一条规则。
        # 两个规则混进一个函数，将来就会有人误以为"已取消 = 已过期"。
        df = _表([
            {"script_name": "取消了的未来场", "start_time": "2026-10-25 19:00",
             "status": STATUS_CANCELLED},
        ])
        有效, 过期 = split_expired(df, now=NOW)
        assert len(过期) == 0
        assert len(有效) == 1
        # 但它仍然不该出现在"可运营"的列表里
        assert filter_active(df, now=NOW).empty

    def test_状态为空时不报错(self):
        # status 为空时 pd.NA != "已取消" 得到的是 pd.NA，
        # 直接拿去索引会抛错。这条测试钉住这个坑。
        df = _表([{"script_name": "状态缺失", "start_time": "2026-10-21 19:00",
                   "status": None}])
        assert len(filter_active(df, now=NOW)) == 1


# ===========================================================================
# 四、状态推导
# ===========================================================================

class TestDeriveStatus:

    def test_时间未到是待开场(self):
        assert derive_status("2026-10-21 19:00", STATUS_PENDING, now=NOW) == STATUS_PENDING

    def test_时间已过是已开场(self):
        assert derive_status("2026-10-19 19:00", STATUS_PENDING, now=NOW) == STATUS_STARTED

    def test_已取消优先于时间_未来场次(self):
        # 人工意图优先：取消了就是取消了，跟时间无关
        assert derive_status("2026-10-21 19:00", STATUS_CANCELLED, now=NOW) == STATUS_CANCELLED

    def test_已取消优先于时间_已过场次(self):
        assert derive_status("2026-10-19 19:00", STATUS_CANCELLED, now=NOW) == STATUS_CANCELLED

    def test_状态为空按时间判断(self):
        assert derive_status("2026-10-21 19:00", None, now=NOW) == STATUS_PENDING
        assert derive_status("2026-10-19 19:00", None, now=NOW) == STATUS_STARTED

    def test_时间也无法判定时按待开场(self):
        assert derive_status(None, None, now=NOW) == STATUS_PENDING


class TestIsCancelled:

    def test_可以识别取消状态(self):
        assert is_cancelled(STATUS_CANCELLED) is True
        assert is_cancelled(" 已取消 ") is True   # 两侧空白不影响
        assert is_cancelled(STATUS_PENDING) is False
        assert is_cancelled(STATUS_STARTED) is False

    def test_空值不算取消(self):
        assert is_cancelled(None) is False
        assert is_cancelled(pd.NA) is False


# ===========================================================================
# 五、把推导结果落到表上
# ===========================================================================

class TestWithDerivedStatus:

    def test_三个分支都对(self):
        df = _表([
            {"script_name": "未来的", "start_time": "2026-10-21 19:00"},
            {"script_name": "过去的", "start_time": "2026-10-19 19:00"},
            {"script_name": "取消的", "start_time": "2026-10-21 19:00",
             "status": STATUS_CANCELLED},
        ])
        out = with_derived_status(df, now=NOW)
        结果 = dict(zip(out["script_name"], out["derived_status"]))
        assert 结果["未来的"] == STATUS_PENDING
        assert 结果["过去的"] == STATUS_STARTED
        assert 结果["取消的"] == STATUS_CANCELLED

    def test_不改动原始status列(self):
        # 原 status 里的人工输入是原始数据，推导结果是计算结果。
        # 混在一列里就分不清"被谁取消"和"因为到点才显示已开场"。
        df = _表([{"script_name": "过去的", "start_time": "2026-10-19 19:00",
                   "status": STATUS_PENDING}])
        out = with_derived_status(df, now=NOW)
        assert out.iloc[0]["status"] == STATUS_PENDING          # 原样保留
        assert out.iloc[0]["derived_status"] == STATUS_STARTED  # 推导结果

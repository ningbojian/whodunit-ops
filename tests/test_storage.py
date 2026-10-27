"""工具 6 表格读写 —— 单元测试。

运行：
  C:/Users/Lenovo/anaconda3/python.exe -m pytest tests/ -v

注意这些测试**不联网、不需要 API key**。
这正是把业务逻辑写成纯函数的好处：跑一次 pytest 不到一秒。
"""

from __future__ import annotations

from datetime import datetime

import pandas as pd
import pytest

from app.tools.storage import (
    BOOKINGS_COLUMNS,
    BOOKING_ACTIVE,
    BOOKING_CANCELLED,
    SESSIONS_COLUMNS,
    STATUS_CANCELLED,
    STATUS_PENDING,
    STATUS_STARTED,
    BOOKINGS_SPEC,
    _coerce_table,
    _coerce_types,
    cancel_booking,
    count_booked,
    find_orphan_bookings,
    get_session,
    list_bookings,
    load_bookings,
    load_sessions,
    load_sessions_with_counts,
    make_booking_id,
    make_session_id,
    normalize_start_time,
    query_sessions,
    record_booking_checked,
    save_bookings,
    save_sessions,
    upsert_booking,
    upsert_sessions,
)


# ===========================================================================
# 一、时间归一化
# ===========================================================================

class TestNormalizeStartTime:
    """同一时刻的不同写法，必须归一到同一个字符串。

    这是去重能成立的地基：归一化不一致 -> session_id 不一致 -> 去重失效。
    """

    def test_各种写法归一后完全相同(self):
        expected = "2026-10-20 19:30"
        assert normalize_start_time("2026-10-20 19:30") == expected
        assert normalize_start_time("2026/10/20 19:30") == expected
        assert normalize_start_time("2026-10-20 19:30:00") == expected      # 秒被丢掉
        assert normalize_start_time(datetime(2026, 10, 20, 19, 30)) == expected
        assert normalize_start_time("  2026-10-20 19:30  ") == expected    # 两侧空白

    def test_无法解析时明确报错而不是静默变NaT(self):
        # 静默失败会让一条格式错误的场次悄悄变成"永不重复"的新记录，
        # 比直接报错危险得多。
        with pytest.raises(Exception):
            normalize_start_time("下周三晚上")


# ===========================================================================
# 二、主键生成
# ===========================================================================

class TestMakeSessionId:

    def test_等价输入产生相同ID(self):
        a = make_session_id("迷雾剧场", "年轮", "2026-10-20 19:30")
        b = make_session_id("迷雾剧场", "年轮", "2026/10/20 19:30:00")
        c = make_session_id("迷雾剧场", "年轮", datetime(2026, 10, 20, 19, 30))
        assert a == b == c, "同一场次的三种写法应当产出同一个 session_id"

    def test_三个维度任一不同则ID不同(self):
        base = make_session_id("迷雾剧场", "年轮", "2026-10-20 19:30")
        assert base != make_session_id("星辰馆", "年轮", "2026-10-20 19:30")     # 换门店
        assert base != make_session_id("迷雾剧场", "蛊魂铃", "2026-10-20 19:30")  # 换剧本
        assert base != make_session_id("迷雾剧场", "年轮", "2026-10-20 20:30")   # 换时间

    def test_ID格式为12位十六进制(self):
        sid = make_session_id("迷雾剧场", "年轮", "2026-10-20 19:30")
        assert len(sid) == 12
        assert all(c in "0123456789abcdef" for c in sid)

    def test_两侧空白不影响ID(self):
        a = make_session_id(" 迷雾剧场 ", "年轮", "2026-10-20 19:30")
        b = make_session_id("迷雾剧场", " 年轮 ", "2026-10-20 19:30")
        assert a == b


# ===========================================================================
# 三、类型强制转换
# ===========================================================================

class TestCoerceTypes:

    def test_剩余空位由总数减已约重算(self):
        df = pd.DataFrame([{
            "total_seats": 8, "booked_seats": 3,
            "remaining_seats": 999,   # 故意传一个错的值
            "store_name": "迷雾剧场", "script_name": "年轮",
            "start_time": "2026-10-20 19:30",
        }])
        out = _coerce_types(df)
        # 传入的 999 必须被无条件覆盖 —— 这是防止数据不一致的关键
        assert out.loc[0, "remaining_seats"] == 5

    def test_缺失列被补齐_多余列被丢弃(self):
        df = pd.DataFrame([{"store_name": "迷雾剧场", "代码里没定义的列": "x"}])
        out = _coerce_types(df)
        assert list(out.columns) == SESSIONS_COLUMNS
        assert "代码里没定义的列" not in out.columns

    def test_字符串数字被转成数值(self):
        df = pd.DataFrame([{
            "total_seats": "8", "booked_seats": "3",
            "store_name": "迷雾剧场", "script_name": "年轮",
            "start_time": "2026-10-20 19:30",
        }])
        out = _coerce_types(df)
        assert out.loc[0, "total_seats"] == 8
        assert out.loc[0, "remaining_seats"] == 5

    def test_无法转换的数值变空而不是报错(self):
        df = pd.DataFrame([{
            "total_seats": "八人本", "booked_seats": 3,
            "store_name": "迷雾剧场", "script_name": "年轮",
            "start_time": "2026-10-20 19:30",
        }])
        out = _coerce_types(df)
        # 读表阶段宽容（不炸），把问题留给后面的字段校验环节去报告
        assert pd.isna(out.loc[0, "total_seats"])
        assert pd.isna(out.loc[0, "remaining_seats"])


# ===========================================================================
# 四、读写往返
# ===========================================================================

class TestLoadSave:

    def test_文件不存在时返回空表而不是报错(self, tmp_path):
        df = load_sessions(tmp_path / "还没有这个文件.xlsx")
        assert df.empty
        assert list(df.columns) == SESSIONS_COLUMNS

    def test_写入后读回内容一致(self, tmp_path):
        path = tmp_path / "sessions.xlsx"
        row = {
            "session_id": make_session_id("迷雾剧场", "年轮", "2026-10-20 19:30"),
            "store_name": "迷雾剧场",
            "script_name": "年轮",
            "script_type": "情感",
            "start_time": datetime(2026, 10, 20, 19, 30),
            "total_seats": 8,
            "booked_seats": 3,
            "list_price": 168.0,
            "status": STATUS_PENDING,
            "source_text": "年轮 20号19:30 8人本 还差5位",
        }
        save_sessions(pd.DataFrame([row]), path)

        out = load_sessions(path)
        assert len(out) == 1
        assert out.loc[0, "store_name"] == "迷雾剧场"
        assert out.loc[0, "script_type"] == "情感"
        assert out.loc[0, "remaining_seats"] == 5
        assert out.loc[0, "start_time"] == pd.Timestamp("2026-10-20 19:30:00")

    def test_写入是原子的_不留下临时文件(self, tmp_path):
        path = tmp_path / "sessions.xlsx"
        save_sessions(pd.DataFrame([{
            "store_name": "迷雾剧场", "script_name": "年轮",
            "start_time": "2026-10-20 19:30", "total_seats": 8, "booked_seats": 1,
        }]), path)

        leftovers = list(tmp_path.glob("*.tmp"))
        assert leftovers == [], f"写入后不应残留临时文件: {leftovers}"
        assert path.exists()

    def test_列顺序在多次写入后保持稳定(self, tmp_path):
        path = tmp_path / "sessions.xlsx"
        save_sessions(pd.DataFrame([{
            "store_name": "迷雾剧场", "script_name": "年轮",
            "start_time": "2026-10-20 19:30", "total_seats": 8, "booked_seats": 1,
        }]), path)
        first = list(load_sessions(path).columns)
        save_sessions(load_sessions(path), path)
        assert list(load_sessions(path).columns) == first == SESSIONS_COLUMNS


# ===========================================================================
# 五、测试用的小工具
# ===========================================================================

@pytest.fixture
def 表(tmp_path):
    """返回 (场次表路径, 预约表路径)，都指向临时目录，绝不碰真实数据。"""
    return tmp_path / "sessions.xlsx", tmp_path / "bookings.xlsx"


def _建场次(sessions_path, **overrides):
    """写入一个默认场次并返回它的 session_id。"""
    row = {
        "store_name": "迷雾剧场",
        "script_name": "年轮",
        "script_type": "情感",
        "start_time": "2026-10-20 19:30",
        "total_seats": 8,
        "list_price": 168.0,
        "status": STATUS_PENDING,
        **overrides,
    }
    out = upsert_sessions([row], sessions_path)
    return out.iloc[0]["session_id"]


def _预约表(rows) -> pd.DataFrame:
    """把一批预约记录整形成规范表，供纯函数测试直接使用。"""
    return _coerce_table(pd.DataFrame(rows), BOOKINGS_SPEC)


# ===========================================================================
# 六、场次表的 upsert
# ===========================================================================

class TestUpsertSessions:

    def test_不给session_id时自动补算(self, 表):
        sessions_path, _ = 表
        out = upsert_sessions([{
            "store_name": "迷雾剧场", "script_name": "年轮",
            "start_time": "2026-10-20 19:30", "total_seats": 8,
        }], sessions_path)
        assert out.iloc[0]["session_id"] == make_session_id("迷雾剧场", "年轮", "2026-10-20 19:30")

    def test_已有session_id不被覆盖(self, 表):
        sessions_path, _ = 表
        out = upsert_sessions([{
            "session_id": "手写的id", "store_name": "迷雾剧场", "script_name": "年轮",
            "start_time": "2026-10-20 19:30", "total_seats": 8,
        }], sessions_path)
        assert out.iloc[0]["session_id"] == "手写的id"

    def test_同一场次反复upsert只保留一行(self, 表):
        sessions_path, _ = 表
        _建场次(sessions_path)
        _建场次(sessions_path)
        assert len(load_sessions(sessions_path)) == 1

    def test_已存在场次的已约人数被保留(self, 表):
        sessions_path, _ = 表
        _建场次(sessions_path)
        # 手工把已约人数写成 5（模拟预约明细已经算出来的结果），
        # 再走一次 upsert —— 它手上没有这个数，绝不能把 5 抹掉。
        df = load_sessions(sessions_path)
        df.loc[0, "booked_seats"] = 5
        save_sessions(df, sessions_path)

        _建场次(sessions_path)  # 重新抽取一遍文案，重写同一场次
        assert load_sessions(sessions_path).iloc[0]["booked_seats"] == 5

    def test_新场次已约人数默认为0而不是空(self, 表):
        sessions_path, _ = 表
        _建场次(sessions_path)
        row = load_sessions(sessions_path).iloc[0]
        assert row["booked_seats"] == 0
        assert row["remaining_seats"] == row["total_seats"] == 8

    def test_新场次状态默认为待开场(self, 表):
        sessions_path, _ = 表
        # 不传 status
        upsert_sessions([{
            "store_name": "迷雾剧场", "script_name": "年轮",
            "start_time": "2026-10-20 19:30", "total_seats": 8,
        }], sessions_path)
        assert load_sessions(sessions_path).iloc[0]["status"] == STATUS_PENDING

    def test_已取消的场次不会被重新抽取文案复活(self, 表):
        sessions_path, _ = 表
        _建场次(sessions_path)

        # 店长手工取消了这一场
        df = load_sessions(sessions_path)
        df.loc[0, "status"] = STATUS_CANCELLED
        save_sessions(df, sessions_path)

        # 之后那份文案又被转发了一次，重新走一遍抽取 + 入库。
        # 文案里读不出"已经取消"，所以只能保留店长的决定。
        _建场次(sessions_path)
        assert load_sessions(sessions_path).iloc[0]["status"] == STATUS_CANCELLED

    def test_未被提及的旧场次原样保留(self, 表):
        sessions_path, _ = 表
        _建场次(sessions_path, script_name="年轮")
        _建场次(sessions_path, script_name="蛊魂铃")
        assert len(load_sessions(sessions_path)) == 2

    def test_同一批里重复的场次只留最后一条(self, 表):
        sessions_path, _ = 表
        upsert_sessions([
            {"store_name": "迷雾剧场", "script_name": "年轮",
             "start_time": "2026-10-20 19:30", "total_seats": 6},
            {"store_name": "迷雾剧场", "script_name": "年轮",
             "start_time": "2026-10-20 19:30", "total_seats": 8},  # 后发的文案更准
        ], sessions_path)
        df = load_sessions(sessions_path)
        assert len(df) == 1
        assert df.iloc[0]["total_seats"] == 8


# ===========================================================================
# 六之二、字段级合并的回归测试
# ===========================================================================

class TestUpsertMergeRegression:
    """复现并锁死两个「转发越多、数据丢得越干净」的旧缺陷。

    旧实现按**整行** drop_duplicates(keep="last")，于是：
      同批：后一条残缺转发把前一条完整字段整行顶掉；
      跨批：未提供的列被填成 NA，把旧表的价格、总位抹空。

    两个缺陷是同一个 bug 的两种表现，修复后合并以**字段**为单位。
    """

    def test_同批后一条残缺不丢前一条的完整字段(self, 表):
        sessions_path, _ = 表
        upsert_sessions([
            {"store_name": "迷雾剧场", "script_name": "年轮",
             "script_type": "情感", "start_time": "2026-10-20 19:30",
             "total_seats": 8, "list_price": 168.0},
            {"store_name": "迷雾剧场", "script_name": "年轮",
             "start_time": "2026-10-20 19:30",
             "source_text": "年轮 还差2人"},
        ], sessions_path)

        row = load_sessions(sessions_path).iloc[0]
        assert row["script_type"] == "情感"
        assert row["total_seats"] == 8
        assert row["list_price"] == 168.0
        assert row["source_text"] == "年轮 还差2人"

    def test_同批空值不覆盖非空值(self, 表):
        sessions_path, _ = 表
        upsert_sessions([
            {"store_name": "迷雾剧场", "script_name": "年轮",
             "start_time": "2026-10-20 19:30", "total_seats": 8, "list_price": 168.0},
            {"store_name": "迷雾剧场", "script_name": "年轮",
             "start_time": "2026-10-20 19:30", "total_seats": None, "list_price": None},
        ], sessions_path)

        row = load_sessions(sessions_path).iloc[0]
        assert row["total_seats"] == 8
        assert row["list_price"] == 168.0

    def test_同批冲突取最后一个非空值(self, 表):
        sessions_path, _ = 表
        upsert_sessions([
            {"store_name": "迷雾剧场", "script_name": "年轮",
             "start_time": "2026-10-20 19:30", "total_seats": 6, "list_price": 168.0},
            {"store_name": "迷雾剧场", "script_name": "年轮",
             "start_time": "2026-10-20 19:30", "total_seats": 8, "list_price": 158.0},
        ], sessions_path)

        row = load_sessions(sessions_path).iloc[0]
        assert row["total_seats"] == 8
        assert row["list_price"] == 158.0

    def test_跨批残缺复转不抹掉总位与价格(self, 表):
        """最严重的旧缺陷：复转一次残缺文案，旧表就被清空。"""
        sessions_path, _ = 表
        _建场次(sessions_path, script_type="情感", total_seats=8, list_price=168.0)

        upsert_sessions([{
            "store_name": "迷雾剧场", "script_name": "年轮",
            "start_time": "2026-10-20 19:30",
            "source_text": "年轮 还差2人",
        }], sessions_path)

        row = load_sessions(sessions_path).iloc[0]
        assert row["script_type"] == "情感"
        assert row["total_seats"] == 8
        assert row["list_price"] == 168.0
        assert row["source_text"] == "年轮 还差2人"

    def test_跨批空值不覆盖旧值(self, 表):
        sessions_path, _ = 表
        _建场次(sessions_path, script_type="情感", total_seats=8, list_price=168.0)

        upsert_sessions([{
            "store_name": "迷雾剧场", "script_name": "年轮",
            "start_time": "2026-10-20 19:30",
            "script_type": None, "total_seats": None, "list_price": None,
        }], sessions_path)

        row = load_sessions(sessions_path).iloc[0]
        assert row["script_type"] == "情感"
        assert row["total_seats"] == 8
        assert row["list_price"] == 168.0

    def test_跨批非空值仍然能覆盖旧值(self, 表):
        sessions_path, _ = 表
        _建场次(sessions_path, total_seats=8, list_price=168.0)

        upsert_sessions([{
            "store_name": "迷雾剧场", "script_name": "年轮",
            "start_time": "2026-10-20 19:30", "list_price": 158.0,
        }], sessions_path)

        assert load_sessions(sessions_path).iloc[0]["list_price"] == 158.0

    def test_跨批数值0仍被视为有效值(self, 表):
        sessions_path, _ = 表
        _建场次(sessions_path, list_price=168.0)

        upsert_sessions([{
            "store_name": "迷雾剧场", "script_name": "年轮",
            "start_time": "2026-10-20 19:30", "list_price": 0,
        }], sessions_path)

        assert load_sessions(sessions_path).iloc[0]["list_price"] == 0

    def test_算不出主键且无显式ID时抛ValueError(self, 表):
        sessions_path, _ = 表
        with pytest.raises(ValueError, match="算不出 session_id"):
            upsert_sessions([{
                "store_name": "迷雾剧场", "script_name": "年轮",
                "total_seats": 8,
            }], sessions_path)

    def test_拒绝写入时错误信息带剧本名(self, 表):
        sessions_path, _ = 表
        with pytest.raises(ValueError, match="年轮"):
            upsert_sessions([{
                "store_name": "迷雾剧场", "script_name": "年轮",
                "start_time": "下周三晚上", "total_seats": 8,
            }], sessions_path)

    def test_拒绝写入时不会留下半张表(self, 表):
        sessions_path, _ = 表
        with pytest.raises(ValueError):
            upsert_sessions([{
                "store_name": "迷雾剧场", "script_name": "年轮",
                "total_seats": 8,
            }], sessions_path)
        assert not sessions_path.exists()

    def test_显式session_id可以让缺时间的场次落盘(self, 表):
        sessions_path, _ = 表
        upsert_sessions([{
            "session_id": "manual-x",
            "store_name": "迷雾剧场", "script_name": "年轮",
            "total_seats": 8,
        }], sessions_path)
        assert load_sessions(sessions_path).iloc[0]["session_id"] == "manual-x"

    def test_已约人数与取消状态跨批仍保留(self, 表):
        """合并逻辑变了，这三条硬不变量不能跟着变。"""
        sessions_path, _ = 表
        _建场次(sessions_path)

        df = load_sessions(sessions_path)
        df.loc[0, "booked_seats"] = 3
        df.loc[0, "status"] = STATUS_CANCELLED
        save_sessions(df, sessions_path)

        upsert_sessions([{
            "store_name": "迷雾剧场", "script_name": "年轮",
            "start_time": "2026-10-20 19:30",
            "booked_seats": 0, "status": STATUS_PENDING, "list_price": 158.0,
        }], sessions_path)

        row = load_sessions(sessions_path).iloc[0]
        assert row["booked_seats"] == 3
        assert row["status"] == STATUS_CANCELLED
        assert row["list_price"] == 158.0


# ===========================================================================
# 七、场次查询
# ===========================================================================

class TestQuerySessions:

    @pytest.fixture
    def 三个场次(self, 表):
        sessions_path, bookings_path = 表
        upsert_sessions([
            {"store_name": "迷雾剧场", "script_name": "年轮", "script_type": "情感",
             "start_time": "2026-10-20 19:30", "total_seats": 8, "status": STATUS_PENDING},
            {"store_name": "迷雾剧场", "script_name": "蛊魂铃", "script_type": "恐怖",
             "start_time": "2026-10-21 20:00", "total_seats": 6, "status": STATUS_PENDING},
            {"store_name": "星辰馆", "script_name": "年轮", "script_type": "情感",
             "start_time": "2026-10-22 19:00", "total_seats": 8, "status": STATUS_STARTED},
        ], sessions_path)
        return sessions_path, bookings_path

    def test_按剧本类型筛选(self, 三个场次):
        sessions_path, bookings_path = 三个场次
        结果 = query_sessions(script_type="情感", path=sessions_path, bookings_path=bookings_path)
        assert len(结果) == 2
        assert set(结果["script_name"]) == {"年轮"}

    def test_按门店加剧本组合筛选(self, 三个场次):
        sessions_path, bookings_path = 三个场次
        结果 = query_sessions(store_name="星辰馆", script_name="年轮",
                            path=sessions_path, bookings_path=bookings_path)
        assert len(结果) == 1
        assert 结果.iloc[0]["script_type"] == "情感"

    def test_状态可以传一组值(self, 三个场次):
        sessions_path, bookings_path = 三个场次
        结果 = query_sessions(status={STATUS_STARTED}, path=sessions_path, bookings_path=bookings_path)
        assert len(结果) == 1
        assert 结果.iloc[0]["store_name"] == "星辰馆"

    def test_时间区间是闭区间_两头都算(self, 三个场次):
        sessions_path, bookings_path = 三个场次
        # 20 号到 21 号：两头都该包含，正好 2 场
        结果 = query_sessions(start_from="2026-10-20", start_to="2026-10-21 23:59",
                            path=sessions_path, bookings_path=bookings_path)
        assert len(结果) == 2

    def test_不传条件时返回全部并按时间排序(self, 三个场次):
        sessions_path, bookings_path = 三个场次
        结果 = query_sessions(path=sessions_path, bookings_path=bookings_path)
        assert list(结果["start_time"].dt.strftime("%Y-%m-%d")) == [
            "2026-10-20", "2026-10-21", "2026-10-22"
        ]

    def test_查不到时返回空表而不是报错(self, 三个场次):
        sessions_path, bookings_path = 三个场次
        结果 = query_sessions(store_name="不存在的店", path=sessions_path, bookings_path=bookings_path)
        assert 结果.empty

    def test_get_session返回字典_查不到返回None(self, 三个场次):
        sessions_path, bookings_path = 三个场次
        sid = make_session_id("迷雾剧场", "年轮", "2026-10-20 19:30")
        row = get_session(sid, path=sessions_path, bookings_path=bookings_path)
        assert isinstance(row, dict)
        assert row["script_name"] == "年轮"
        assert get_session("不存在", path=sessions_path, bookings_path=bookings_path) is None


# ===========================================================================
# 八、预约明细：幂等性（本轮的核心）
# ===========================================================================

class TestBookingId:

    def test_同一玩家同一场次ID相同(self):
        assert make_booking_id("abc123", "张三") == make_booking_id("abc123", "张三")

    def test_昵称两侧空白不影响ID(self):
        assert make_booking_id("abc123", " 张三 ") == make_booking_id("abc123", "张三")

    def test_换场次或换人则ID不同(self):
        base = make_booking_id("abc123", "张三")
        assert base != make_booking_id("def456", "张三")   # 换场次
        assert base != make_booking_id("abc123", "李四")   # 换人


class TestUpsertBooking:

    def test_新增一条预约(self, 表):
        sessions_path, bookings_path = 表
        sid = _建场次(sessions_path)
        out = upsert_booking(sid, "张三", path=bookings_path)
        assert len(out) == 1
        assert out.iloc[0]["status"] == BOOKING_ACTIVE
        assert out.iloc[0]["seats"] == 1

    def test_同一玩家重复预约只算一个人(self, 表):
        sessions_path, bookings_path = 表
        sid = _建场次(sessions_path)

        # 张三约了 -> 李四约了 -> 张三那条消息又被转发了一遍
        upsert_booking(sid, "张三", path=bookings_path)
        upsert_booking(sid, "李四", path=bookings_path)
        upsert_booking(sid, "张三", path=bookings_path)

        assert len(load_bookings(bookings_path)) == 2, "重复预约不该新增行"
        row = load_sessions_with_counts(sessions_path, bookings_path).iloc[0]
        assert row["booked_seats"] == 2, "转发两次不能把 1 个人算成 2 个"
        assert row["remaining_seats"] == 6

    def test_昵称带空白视为同一个人(self, 表):
        sessions_path, bookings_path = 表
        sid = _建场次(sessions_path)
        upsert_booking(sid, "张三", path=bookings_path)
        upsert_booking(sid, "  张三  ", path=bookings_path)
        assert len(load_bookings(bookings_path)) == 1

    def test_代朋友一起约占多个位子(self, 表):
        sessions_path, bookings_path = 表
        sid = _建场次(sessions_path)
        upsert_booking(sid, "张三", seats=3, path=bookings_path)
        assert load_sessions_with_counts(sessions_path, bookings_path).iloc[0]["booked_seats"] == 3

    def test_人数和昵称的非法输入明确报错(self, 表):
        sessions_path, bookings_path = 表
        sid = _建场次(sessions_path)
        with pytest.raises(ValueError):
            upsert_booking(sid, "张三", seats=0, path=bookings_path)
        with pytest.raises(ValueError):
            upsert_booking(sid, "   ", path=bookings_path)


class TestCancelBooking:

    def test_跳车后人数减少(self, 表):
        sessions_path, bookings_path = 表
        sid = _建场次(sessions_path)
        upsert_booking(sid, "张三", path=bookings_path)
        upsert_booking(sid, "李四", path=bookings_path)

        assert cancel_booking(sid, "张三", path=bookings_path) is True

        row = load_sessions_with_counts(sessions_path, bookings_path).iloc[0]
        assert row["booked_seats"] == 1
        assert row["remaining_seats"] == 7

    def test_跳车是软删除_行还在只是状态变了(self, 表):
        sessions_path, bookings_path = 表
        sid = _建场次(sessions_path)
        upsert_booking(sid, "张三", path=bookings_path)
        cancel_booking(sid, "张三", path=bookings_path)

        df = load_bookings(bookings_path)
        assert len(df) == 1, "行不该被删掉，否则查不出谁跳过车"
        assert df.iloc[0]["status"] == BOOKING_CANCELLED

    def test_跳车后又约回来恢复计数(self, 表):
        sessions_path, bookings_path = 表
        sid = _建场次(sessions_path)
        upsert_booking(sid, "张三", path=bookings_path)
        cancel_booking(sid, "张三", path=bookings_path)
        assert load_sessions_with_counts(sessions_path, bookings_path).iloc[0]["booked_seats"] == 0

        upsert_booking(sid, "张三", path=bookings_path)  # 反悔又约回来
        assert load_sessions_with_counts(sessions_path, bookings_path).iloc[0]["booked_seats"] == 1

    def test_取消不存在的预约返回False(self, 表):
        sessions_path, bookings_path = 表
        sid = _建场次(sessions_path)
        # 明确返回失败，不静默当成功——否则上游传错参数会被掩盖
        assert cancel_booking(sid, "从没约过的人", path=bookings_path) is False

    def test_重复取消返回False(self, 表):
        sessions_path, bookings_path = 表
        sid = _建场次(sessions_path)
        upsert_booking(sid, "张三", path=bookings_path)
        assert cancel_booking(sid, "张三", path=bookings_path) is True
        assert cancel_booking(sid, "张三", path=bookings_path) is False


class TestListBookings:

    def test_默认只看当前在座的人(self, 表):
        sessions_path, bookings_path = 表
        sid = _建场次(sessions_path)
        upsert_booking(sid, "张三", path=bookings_path)
        upsert_booking(sid, "李四", path=bookings_path)
        cancel_booking(sid, "张三", path=bookings_path)

        assert list(list_bookings(sid, path=bookings_path)["player_name"]) == ["李四"]

    def test_可以要求连跳车的一起看(self, 表):
        sessions_path, bookings_path = 表
        sid = _建场次(sessions_path)
        upsert_booking(sid, "张三", path=bookings_path)
        upsert_booking(sid, "李四", path=bookings_path)
        cancel_booking(sid, "张三", path=bookings_path)

        assert len(list_bookings(sid, include_cancelled=True, path=bookings_path)) == 2


# ===========================================================================
# 九、派生字段重算 —— 文件里的数字一律不信任
# ===========================================================================

class TestApplyBookingCounts:

    def test_文件里被手工改错的已约人数会被重算覆盖(self, 表):
        sessions_path, bookings_path = 表
        sid = _建场次(sessions_path)
        upsert_booking(sid, "张三", path=bookings_path)
        upsert_booking(sid, "李四", path=bookings_path)

        # 模拟店长在 Excel 里手改：把 2 个人改成 99
        df = load_sessions(sessions_path)
        df.loc[0, "booked_seats"] = 99
        save_sessions(df, sessions_path)
        assert load_sessions(sessions_path).iloc[0]["booked_seats"] == 99, "前提：脏值确实写进去了"

        row = load_sessions_with_counts(sessions_path, bookings_path).iloc[0]
        assert row["booked_seats"] == 2, "读出来必须按预约明细重算"
        assert row["remaining_seats"] == 6

    def test_一条预约都没有的场次算0人(self, 表):
        sessions_path, bookings_path = 表
        _建场次(sessions_path)
        row = load_sessions_with_counts(sessions_path, bookings_path).iloc[0]
        assert row["booked_seats"] == 0
        assert row["remaining_seats"] == 8

    def test_超额预约如实返回不截断(self, 表):
        sessions_path, bookings_path = 表
        sid = _建场次(sessions_path)  # 8 人本
        upsert_booking(sid, "张三", seats=5, path=bookings_path)
        upsert_booking(sid, "李四", seats=5, path=bookings_path)

        row = load_sessions_with_counts(sessions_path, bookings_path).iloc[0]
        # 10 > 8：不截断成 8，也不报错，如实返回负数剩余位数，
        # 由工具 4「高危识别」去预警 —— 数据层不替业务层做决定。
        assert row["booked_seats"] == 10
        assert row["remaining_seats"] == -2

    def test_count_booked只数有效预约(self):
        bookings = _预约表([
            {"session_id": "s1", "player_name": "张三", "seats": 2,
             "status": BOOKING_ACTIVE, "booking_id": make_booking_id("s1", "张三")},
            {"session_id": "s1", "player_name": "李四", "seats": 1,
             "status": BOOKING_CANCELLED, "booking_id": make_booking_id("s1", "李四")},
            {"session_id": "s2", "player_name": "王五", "seats": 1,
             "status": BOOKING_ACTIVE, "booking_id": make_booking_id("s2", "王五")},
        ])
        assert count_booked("s1", bookings) == 2, "已取消的不算"
        assert count_booked("s2", bookings) == 1, "别把别的场次算进来"
        assert count_booked("不存在的场次", bookings) == 0


# ===========================================================================
# 十、边界：孤儿预约
# ===========================================================================

class TestOrphanBookings:

    def test_孤儿预约不计入真实场次(self, 表):
        sessions_path, bookings_path = 表
        sid = _建场次(sessions_path)
        upsert_booking(sid, "张三", path=bookings_path)
        upsert_booking("指向不存在场次的id", "王五", path=bookings_path)

        row = load_sessions_with_counts(sessions_path, bookings_path).iloc[0]
        assert row["booked_seats"] == 1, "孤儿不能被算到任何一个真实场次头上"

    def test_孤儿可以被报出来而不是被静默吞掉(self, 表):
        sessions_path, bookings_path = 表
        sid = _建场次(sessions_path)
        upsert_booking(sid, "张三", path=bookings_path)
        upsert_booking("指向不存在场次的id", "王五", path=bookings_path)

        orphans = find_orphan_bookings(
            sessions=load_sessions(sessions_path),
            bookings=load_bookings(bookings_path),
        )
        assert len(orphans) == 1
        assert orphans.iloc[0]["player_name"] == "王五"

    def test_没有孤儿时返回空表(self, 表):
        sessions_path, bookings_path = 表
        sid = _建场次(sessions_path)
        upsert_booking(sid, "张三", path=bookings_path)
        orphans = find_orphan_bookings(
            sessions=load_sessions(sessions_path),
            bookings=load_bookings(bookings_path),
        )
        assert orphans.empty


# ===========================================================================
# 十一、预约表的读写本身
# ===========================================================================

class TestBookingsIO:

    def test_预约表文件不存在时返回空表(self, tmp_path):
        df = load_bookings(tmp_path / "还没有.xlsx")
        assert df.empty
        assert list(df.columns) == BOOKINGS_COLUMNS

    def test_预约表写入后读回内容一致(self, 表):
        sessions_path, bookings_path = 表
        sid = _建场次(sessions_path)
        upsert_booking(sid, "张三", seats=2, source_text="张三 2 位", path=bookings_path)

        out = load_bookings(bookings_path)
        assert len(out) == 1
        assert out.loc[0, "player_name"] == "张三"
        assert out.loc[0, "seats"] == 2
        assert out.loc[0, "source_text"] == "张三 2 位"

    def test_预约表写入不留临时文件(self, 表):
        sessions_path, bookings_path = 表
        sid = _建场次(sessions_path)
        upsert_booking(sid, "张三", path=bookings_path)
        assert list(bookings_path.parent.glob("*.tmp")) == []

    def test_预约表列顺序稳定(self, 表):
        sessions_path, bookings_path = 表
        sid = _建场次(sessions_path)
        upsert_booking(sid, "张三", path=bookings_path)
        first = list(load_bookings(bookings_path).columns)
        save_bookings(load_bookings(bookings_path), bookings_path)
        assert list(load_bookings(bookings_path).columns) == first == BOOKINGS_COLUMNS

    def test_两张表的数据互相独立(self, 表):
        sessions_path, bookings_path = 表
        sid = _建场次(sessions_path)
        upsert_booking(sid, "张三", path=bookings_path)
        # 写预约不应该动到场次表文件里的内容
        assert load_sessions(sessions_path).iloc[0]["booked_seats"] == 0


# ===========================================================================
# 十二、带容量守卫的预约写入
# ===========================================================================

class TestRecordBookingChecked:

    def test_满员后拒绝净增(self, 表):
        sessions_path, bookings_path = 表
        sid = _建场次(sessions_path, total_seats=2)

        first = record_booking_checked(sid, "张三", seats=2, path=bookings_path)
        assert first.ok is True
        assert first.booking_id
        assert first.session["booked_seats"] == 0  # 写入前快照

        overflow = record_booking_checked(sid, "李四", seats=1, path=bookings_path)
        assert overflow.ok is False
        assert overflow.error_code == "capacity_exceeded"
        assert overflow.session["booked_seats"] == 2
        assert load_bookings(bookings_path)["player_name"].tolist() == ["张三"]

    def test_同一玩家改人数按差额计算(self, 表):
        sessions_path, bookings_path = 表
        sid = _建场次(sessions_path, total_seats=3)
        assert record_booking_checked(sid, "张三", seats=1, path=bookings_path).ok

        increased = record_booking_checked(sid, "张三", seats=3, path=bookings_path)
        assert increased.ok is True
        assert count_booked(sid, load_bookings(bookings_path)) == 3

        refused = record_booking_checked(sid, "张三", seats=4, path=bookings_path)
        assert refused.ok is False
        assert refused.error_code == "capacity_exceeded"
        assert count_booked(sid, load_bookings(bookings_path)) == 3

        # 重复提交同一请求是幂等的，不应误判超卖。
        assert record_booking_checked(sid, "张三", seats=3, path=bookings_path).ok

    def test_未知人数只警告不阻塞(self, 表):
        sessions_path, bookings_path = 表
        sid = _建场次(sessions_path, total_seats=None)

        result = record_booking_checked(sid, "张三", seats=99, path=bookings_path)
        assert result.ok is True
        assert any("总人数未填写" in item for item in result.warnings)

    def test_场次不存在或已取消(self, 表):
        sessions_path, bookings_path = 表
        assert record_booking_checked("missing", "张三", path=bookings_path).error_code == "session_not_found"

        sid = _建场次(sessions_path, status=STATUS_CANCELLED)
        result = record_booking_checked(sid, "张三", path=bookings_path)
        assert result.ok is False
        assert result.error_code == "session_cancelled"

    def test_并发写入不会超卖(self, tmp_path):
        from concurrent.futures import ThreadPoolExecutor

        from app.store.excel import ExcelStore

        store = ExcelStore(
            sessions_path=tmp_path / "sessions.xlsx",
            bookings_path=tmp_path / "bookings.xlsx",
            scripts_path=tmp_path / "scripts.xlsx",
        )
        upsert_sessions(
            [
                {
                    "store_name": "迷雾剧场",
                    "script_name": "年轮",
                    "start_time": "2026-10-20 19:30",
                    "total_seats": 5,
                    "status": STATUS_PENDING,
                }
            ],
            store=store,
        )
        sid = load_sessions(store=store).iloc[0]["session_id"]

        def book(index: int) -> bool:
            return record_booking_checked(
                sid, f"玩家{index}", seats=1, store=store, now=pd.Timestamp("2026-10-20 12:00")
            ).ok

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(book, range(8)))

        assert sum(results) == 5
        assert count_booked(sid, load_bookings(store=store)) == 5
        assert load_sessions_with_counts(store=store).iloc[0]["remaining_seats"] == 0

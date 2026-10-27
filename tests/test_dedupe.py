"""工具 2 场次去重 —— 单元测试。

运行：
  PYTHONUTF8=1 "C:/Users/Lenovo/anaconda3/python.exe" -m pytest tests/ -v

不联网、不需要 API key、不依赖系统时钟、不碰真实 data/。

本模块没有可标定的阈值（「同一场次」是定义，不是经验值），所以测试
全部在验证**规则本身**：

  字段级合并（最后一个非空值胜出，空值永不覆盖）、
  残缺复转不丢信息、冲突必须可见、
  缺身份的行走放行而不是静默撞键、
  与现有表的四种比对动作。

核心回归在 TestBatchMerge：三条转发里后两条是残缺的，
合并结果必须保住第一条的完整字段 —— 这正是旧实现（整行 keep="last"）
会丢掉的东西。
"""

from __future__ import annotations

import pandas as pd
import pytest

from app.tools.dedupe import (
    ACTION_NEW,
    ACTION_UNCHANGED,
    ACTION_UNCOMPARED,
    ACTION_UPDATED,
    COMPARE_FIELDS,
    DEDUPE_COLUMNS,
    dedupe_sessions,
)
from app.tools.storage import (
    NOTE_ID_MISMATCH,
    NOTE_KEY_COLLISION,
    NOTE_SCRIPT_MISSING,
    NOTE_STORE_MISSING,
    NOTE_TIME_MISSING,
    NOTE_TIME_UNPARSABLE,
    SESSIONS_COLUMNS,
    STATUS_CANCELLED,
    STATUS_PENDING,
    _coerce_types,
    is_missing_value,
    load_sessions,
    make_session_id,
    session_identity,
    upsert_sessions,
)

STORE = "迷雾剧场"
SCRIPT = "年轮"
START = "2026-10-20 19:30"


def _场(**extra) -> dict:
    """一条默认完整的场次记录，只覆盖传入的字段。"""
    row = {"store_name": STORE, "script_name": SCRIPT, "start_time": START}
    row.update(extra)
    return row


def _结果(rows, **kwargs) -> pd.DataFrame:
    return dedupe_sessions(rows, **kwargs)


def _按剧本(df: pd.DataFrame, name: str) -> pd.Series:
    return df.set_index("script_name").loc[name]


def _现有(rows) -> pd.DataFrame:
    """造一张「已经落过盘」的现有表：每行都带算好的 session_id。

    upsert 落盘时主键一定非空，所以测试现有表也要模拟这个前提；
    否则比对永远落在「新增」上，测不到更新路径。
    """
    prepared = [
        {**row, "session_id": make_session_id(
            row["store_name"], row["script_name"], row["start_time"])}
        for row in rows
    ]
    return _coerce_types(pd.DataFrame(prepared))


# ===========================================================================
# 一、身份判定
# ===========================================================================

class TestSessionIdentity:

    def test_完整记录算出与make_session_id一致的键(self):
        sid, note = session_identity(_场())
        assert sid == make_session_id(STORE, SCRIPT, START)
        assert note == ""

    def test_等价的时间写法得到同一个键(self):
        写法 = ["2026-10-20 19:30", "2026/10/20 19:30", "2026-10-20 19:30:45",
                pd.Timestamp("2026-10-20 19:30:00")]
        keys = {session_identity(_场(start_time=value))[0] for value in 写法}
        assert len(keys) == 1

    def test_门店缺失(self):
        assert session_identity(_场(store_name=None)) == (None, NOTE_STORE_MISSING)

    def test_剧本缺失(self):
        assert session_identity(_场(script_name=None)) == (None, NOTE_SCRIPT_MISSING)

    @pytest.mark.parametrize("value", [None, pd.NA, pd.NaT, "", "   "])
    def test_开场时间缺失_各种空值都算缺失(self, value):
        assert session_identity(_场(start_time=value)) == (None, NOTE_TIME_MISSING)

    def test_开场时间无法解析与缺失分开报(self):
        assert session_identity(_场(start_time="下周三晚上")) == (None, NOTE_TIME_UNPARSABLE)

    @pytest.mark.parametrize("value", [None, pd.NA, "", "  "])
    def test_门店和剧本的空白字符串也算缺失(self, value):
        _, note = session_identity(_场(store_name=value))
        assert note == NOTE_STORE_MISSING
        _, note = session_identity(_场(script_name=value))
        assert note == NOTE_SCRIPT_MISSING

    def test_数值0不算缺失(self):
        assert not is_missing_value(0)
        assert not is_missing_value(0.0)


# ===========================================================================
# 二、批内合并（核心）
# ===========================================================================

class TestBatchMerge:

    def test_同一场转发三次合并成一行(self):
        df = _结果([
            _场(script_type="情感", total_seats=8, list_price=168, source_text="完整"),
            _场(source_text="年轮 还差2人"),
            _场(list_price=158, source_text="特价158"),
        ])
        assert len(df) == 1
        assert df.iloc[0]["merged_count"] == 3

    def test_后一条残缺不丢前一条的完整字段(self):
        """核心回归：这正是旧实现整行 keep="last" 会丢掉的东西。"""
        row = _结果([
            _场(script_type="情感", total_seats=8, list_price=168),
            _场(source_text="年轮 还差2人"),
        ]).iloc[0]
        assert row["script_type"] == "情感"
        assert inter(row["total_seats"]) == 8
        assert inter(row["list_price"]) == 168

    def test_后一条非空值覆盖前一条(self):
        row = _结果([_场(list_price=168), _场(list_price=158)]).iloc[0]
        assert inter(row["list_price"]) == 158

    def test_后一条空值不覆盖前一条(self):
        row = _结果([_场(list_price=168), _场(list_price=None)]).iloc[0]
        assert inter(row["list_price"]) == 168

    def test_完全相同的两行合并且没有冲突(self):
        row = _结果([_场(total_seats=8), _场(total_seats=8)]).iloc[0]
        assert row["merged_count"] == 2
        assert row["conflict_fields"] == ()

    def test_单行输入的合并计数为1(self):
        row = _结果([_场()]).iloc[0]
        assert row["merged_count"] == 1
        assert row["conflict_fields"] == ()

    def test_total_seats冲突被报告(self):
        row = _结果([_场(total_seats=8), _场(total_seats=6)]).iloc[0]
        assert row["conflict_fields"] == ("total_seats",)
        assert inter(row["total_seats"]) == 6

    def test_list_price冲突被报告(self):
        row = _结果([_场(list_price=168), _场(list_price=158)]).iloc[0]
        assert row["conflict_fields"] == ("list_price",)

    def test_冲突字段按固定顺序排列(self):
        row = _结果([
            _场(total_seats=8, list_price=168),
            _场(total_seats=6, list_price=158),
        ]).iloc[0]
        assert row["conflict_fields"] == ("total_seats", "list_price")

    def test_source_text不同不算冲突(self):
        row = _结果([_场(source_text="第一条"), _场(source_text="第二条")]).iloc[0]
        assert row["conflict_fields"] == ()
        assert row["source_text"] == "第二条"

    def test_时间写法不同不算冲突(self):
        row = _结果([
            _场(start_time="2026-10-20 19:30"),
            _场(start_time="2026/10/20 19:30"),
        ]).iloc[0]
        assert row["merged_count"] == 2
        assert row["conflict_fields"] == ()

    def test_数值0是有效值会参与合并(self):
        row = _结果([_场(list_price=168), _场(list_price=0)]).iloc[0]
        assert inter(row["list_price"]) == 0
        assert row["conflict_fields"] == ("list_price",)

    def test_不同剧本不会被合并(self):
        df = _结果([_场(script_name="年轮"), _场(script_name="蛊魂铃")])
        assert len(df) == 2

    def test_不同门店不会被合并(self):
        df = _结果([_场(store_name="迷雾剧场"), _场(store_name="沉浸剧场")])
        assert len(df) == 2

    def test_不同开场时间不会被合并(self):
        df = _结果([_场(start_time="2026-10-20 19:30"),
                    _场(start_time="2026-10-20 21:30")])
        assert len(df) == 2


# ===========================================================================
# 三、分组与显式 session_id
# ===========================================================================

class TestGrouping:

    def test_无session_id时按联合键合并(self):
        df = _结果([_场(), _场(source_text="又发一遍")])
        assert len(df) == 1

    def test_显式相同session_id时合并(self):
        df = _结果([_场(session_id="manual-x"), _场(session_id="manual-x")])
        assert len(df) == 1
        assert df.iloc[0]["session_id"] == "manual-x"

    def test_显式ID不同但联合键相同则不合并(self):
        """同店同剧本同时间可能是两个房间，显式 ID 是调用方的逃生舱。"""
        df = _结果([_场(session_id="manual-a"), _场(session_id="manual-b")])
        assert len(df) == 2
        assert set(df["session_id"]) == {"manual-a", "manual-b"}
        assert set(df["dedupe_note"]) == {NOTE_KEY_COLLISION}

    def test_显式ID与联合键不一致时保留显式值并标注(self):
        row = _结果([_场(session_id="manual-x")]).iloc[0]
        assert row["session_id"] == "manual-x"
        assert row["dedupe_note"] == NOTE_ID_MISMATCH

    def test_显式ID与联合键一致时无备注(self):
        sid = make_session_id(STORE, SCRIPT, START)
        row = _结果([_场(session_id=sid)]).iloc[0]
        assert row["session_id"] == sid
        assert row["dedupe_note"] == ""

    def test_显式ID相同但联合键不同时按显式ID合并(self):
        df = _结果([
            _场(session_id="manual-x", script_name="年轮"),
            _场(session_id="manual-x", script_name="蛊魂铃"),
        ])
        assert len(df) == 1
        assert df.iloc[0]["script_name"] == "蛊魂铃"   # 最后一个非空值胜出


# ===========================================================================
# 四、身份缺失的放行
# ===========================================================================

class TestMissingIdentity:

    @pytest.mark.parametrize("overrides,note", [
        ({"store_name": None}, NOTE_STORE_MISSING),
        ({"script_name": None}, NOTE_SCRIPT_MISSING),
        ({"start_time": None}, NOTE_TIME_MISSING),
        ({"start_time": "下周三晚上"}, NOTE_TIME_UNPARSABLE),
    ])
    def test_各类缺失都放行并标注(self, overrides, note):
        df = _结果([_场(**overrides)])
        assert len(df) == 1
        assert df.iloc[0]["dedupe_note"] == note

    def test_两个缺门店的行不会互相合并(self):
        """不拦的话 str(pd.NA) == "<NA>"，两条会算出同一个假键。"""
        df = _结果([
            _场(store_name=None, source_text="第一条"),
            _场(store_name=None, source_text="第二条"),
        ])
        assert len(df) == 2
        assert set(df["dedupe_note"]) == {NOTE_STORE_MISSING}

    def test_缺门店但有显式ID时按显式ID合并(self):
        df = _结果([
            _场(store_name=None, session_id="manual-x"),
            _场(store_name=None, session_id="manual-x"),
        ])
        assert len(df) == 1
        assert df.iloc[0]["dedupe_note"] == NOTE_STORE_MISSING

    def test_坏行不影响同批好行的合并(self):
        df = _结果([
            _场(),                                  # 正常
            _场(source_text="复转"),                 # 正常，同一场
            _场(store_name=None),                   # 坏行
        ])
        assert len(df) == 2
        good = _按剧本(df.dropna(subset=["store_name"]), "年轮")
        assert good["merged_count"] == 2

    def test_整批坏行也不会抛异常(self):
        df = _结果([
            {"source_text": "什么都没有"},
            {"store_name": STORE, "script_name": SCRIPT},
        ])
        assert len(df) == 2


# ===========================================================================
# 五、与现有表比对
# ===========================================================================

class TestCompareExisting:

    def test_未传现有表时全部未比对(self):
        row = _结果([_场()]).iloc[0]
        assert row["dedupe_action"] == ACTION_UNCOMPARED
        assert row["changed_fields"] == ()

    def test_现有表为空时全部新增(self):
        empty = _coerce_types(pd.DataFrame(columns=SESSIONS_COLUMNS))
        row = _结果([_场()], existing=empty).iloc[0]
        assert row["dedupe_action"] == ACTION_NEW

    def test_完全相同为无变化(self):
        existing = _现有([_场(total_seats=8, list_price=168)])
        row = _结果([_场(total_seats=8, list_price=168)], existing=existing).iloc[0]
        assert row["dedupe_action"] == ACTION_UNCHANGED
        assert row["changed_fields"] == ()

    def test_字段变化为更新(self):
        existing = _现有([_场(list_price=168)])
        row = _结果([_场(list_price=158)], existing=existing).iloc[0]
        assert row["dedupe_action"] == ACTION_UPDATED
        assert row["changed_fields"] == ("list_price",)

    def test_incoming空值不算变化(self):
        existing = _现有([_场(total_seats=8, list_price=168)])
        row = _结果([_场(source_text="只写了这一句")], existing=existing).iloc[0]
        # source_text 变了，所以是更新；但 total_seats / list_price 不在 changed_fields 里
        assert "total_seats" not in row["changed_fields"]
        assert "list_price" not in row["changed_fields"]

    def test_status差异不进changed_fields(self):
        existing = _现有([_场(status=STATUS_CANCELLED)])
        row = _结果([_场(status=STATUS_PENDING)], existing=existing).iloc[0]
        assert "status" not in row["changed_fields"]
        assert row["dedupe_action"] == ACTION_UNCHANGED

    def test_booked_seats差异不进changed_fields(self):
        existing = _现有([_场(booked_seats=5, total_seats=8)])
        row = _结果([_场(booked_seats=0, total_seats=8)], existing=existing).iloc[0]
        assert "booked_seats" not in row["changed_fields"]
        assert row["dedupe_action"] == ACTION_UNCHANGED

    def test_changed_fields按固定顺序排列(self):
        existing = _现有([
            _场(script_type="情感", total_seats=8, list_price=168)
        ])
        row = _结果([
            _场(script_type="推理", total_seats=6, list_price=158)
        ], existing=existing).iloc[0]
        assert row["changed_fields"] == ("script_type", "total_seats", "list_price")
        assert list(row["changed_fields"]) == [
            f for f in COMPARE_FIELDS if f in row["changed_fields"]
        ]

    def test_时间等价写法不算变化(self):
        existing = _现有([_场(start_time="2026-10-20 19:30")])
        row = _结果([_场(start_time="2026/10/20 19:30:00")], existing=existing).iloc[0]
        assert row["dedupe_action"] == ACTION_UNCHANGED

    def test_新场次为新增(self):
        existing = _现有([_场(script_name="蛊魂铃")])
        row = _结果([_场(script_name="年轮")], existing=existing).iloc[0]
        assert row["dedupe_action"] == ACTION_NEW


# ===========================================================================
# 六、主入口的返回形状
# ===========================================================================

class TestMainEntry:

    def test_空输入返回带完整列的空表(self):
        df = dedupe_sessions([])
        assert df.empty
        assert list(df.columns) == SESSIONS_COLUMNS + DEDUPE_COLUMNS

    def test_返回列固定(self):
        df = _结果([_场()])
        assert list(df.columns) == SESSIONS_COLUMNS + DEDUPE_COLUMNS

    def test_列表输入与DataFrame输入等价(self):
        rows = [_场(total_seats=8), _场(list_price=168)]
        from_list = dedupe_sessions(rows)
        from_frame = dedupe_sessions(pd.DataFrame(rows))
        assert from_list.shape == from_frame.shape
        assert inter(from_list.iloc[0]["total_seats"]) == inter(from_frame.iloc[0]["total_seats"])

    def test_不修改输入对象(self):
        frame = pd.DataFrame([_场(), _场(list_price=158)])
        before = frame.copy(deep=True)
        dedupe_sessions(frame)
        pd.testing.assert_frame_equal(frame, before)

    def test_按开场时间排序(self):
        df = _结果([
            _场(script_name="晚场", start_time="2026-10-21 21:30"),
            _场(script_name="早场", start_time="2026-10-20 19:30"),
        ])
        assert list(df["script_name"]) == ["早场", "晚场"]

    def test_无时间的行排在最后(self):
        df = _结果([
            _场(script_name="没时间", start_time=None),
            _场(script_name="有时间", start_time="2026-10-20 19:30"),
        ])
        assert list(df["script_name"]) == ["有时间", "没时间"]

    def test_重复调用结果稳定(self):
        rows = [_场(total_seats=8, list_price=168), _场(list_price=158)]
        first = _结果(rows)
        second = _结果(rows)
        assert first.iloc[0]["merged_count"] == second.iloc[0]["merged_count"]
        assert inter(first.iloc[0]["list_price"]) == inter(second.iloc[0]["list_price"])

    def test_分析列不会落盘(self, tmp_path):
        path = tmp_path / "sessions.xlsx"
        result = _结果([_场(total_seats=8)])
        upsert_sessions(result, path)
        stored = load_sessions(path)
        for col in DEDUPE_COLUMNS:
            assert col not in stored.columns


# ===========================================================================
# 七、与工具 6 共用同一份合并规则
# ===========================================================================

class TestSharedRuleWithStorage:
    """如果哪天有人把 dedupe 的合并逻辑另写一份，这些测试会失败。

    全项目只能有一份「同一场次怎么合并」的语义，否则两边迟早漂移。
    """

    def test_同一批合并后的业务字段一致(self, tmp_path):
        path = tmp_path / "sessions.xlsx"
        rows = [
            _场(script_type="情感", total_seats=8, list_price=168),
            _场(source_text="残缺复转"),
            _场(list_price=158),
        ]
        deduped = _结果(rows).iloc[0]
        upsert_sessions(rows, path)
        stored = load_sessions(path).iloc[0]

        for field in ["script_type", "total_seats", "list_price", "source_text"]:
            left, right = deduped[field], stored[field]
            if field in ("total_seats", "list_price"):
                assert float(left) == float(right)
            else:
                assert str(left) == str(right)

    def test_跨批残缺复转在storage中同样不丢字段(self, tmp_path):
        path = tmp_path / "sessions.xlsx"
        upsert_sessions(
            [_场(script_type="情感", total_seats=8, list_price=168)], path
        )
        upsert_sessions([_场(source_text="年轮 还差2人")], path)

        stored = load_sessions(path).iloc[0]
        assert stored["script_type"] == "情感"
        assert int(stored["total_seats"]) == 8
        assert float(stored["list_price"]) == 168

    def test_dedupe结果可以直接喂给upsert(self, tmp_path):
        path = tmp_path / "sessions.xlsx"
        result = _结果([_场(total_seats=8, list_price=168), _场(list_price=158)])
        upsert_sessions(result, path)
        stored = load_sessions(path).iloc[0]
        assert len(load_sessions(path)) == 1
        assert float(stored["list_price"]) == 158


def inter(value):
    """把 Int64 / numpy 整数转成 Python int，断言时不必关心 dtype。"""
    return None if is_missing_value(value) else int(value)

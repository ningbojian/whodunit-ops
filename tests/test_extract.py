"""工具 1 文本抽取 —— 单元测试。

运行：
  PYTHONUTF8=1 "C:/Users/Lenovo/anaconda3/python.exe" -m pytest tests/test_extract.py -v

不联网、不需要 API key、不依赖系统时钟、不碰真实 data/。

测试全部使用 FakeLLM：只要对象有 .invoke(messages) 就能注入。
autouse fixture 会阻止任何测试意外调用 app.config.get_model，
所以即使某条路径漏了 model，也不可能真的联网。

核心回归在 Test时间混合优先：
  原文能确定性解析时，模型给的时间再"合理"也不采用；
  原文解析不了时，才用模型推断，并且必须留下「时间由模型推断」的备注。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pandas as pd
import pytest

from app import llm
from app.tools.dedupe import DEDUPE_COLUMNS, dedupe_sessions
from app.tools.extract import (
    ACTION_BOOK,
    ACTION_CANCEL,
    BOOKING_FIELDS,
    ERROR_BAD_JSON,
    ERROR_BAD_ROOT,
    ERROR_CONFIG,
    ERROR_EMPTY_INPUT,
    ERROR_LLM_CALL,
    ERROR_MODEL_INIT,
    INTENT_AD,
    INTENT_BOOKING,
    INTENT_CANCELLATION,
    INTENT_MIXED,
    INTENT_UNKNOWN,
    NOTE_ACTION_FALLBACK,
    NOTE_ACTION_MISSING,
    NOTE_COUNT_INCONSISTENT,
    NOTE_EXCERPT_UNVERIFIED,
    NOTE_IDENTITY_INCOMPLETE,
    NOTE_INTENT_FALLBACK,
    NOTE_NO_RECORDS,
    NOTE_PLAYER_MISSING,
    NOTE_SCRIPT_UNVERIFIED,
    NOTE_SEATS_INVALID,
    NOTE_STORE_DEFAULTED,
    NOTE_STORE_MISSING,
    NOTE_STORE_UNVERIFIED,
    NOTE_TIME_AMBIGUOUS,
    NOTE_TIME_CONFLICT,
    NOTE_TIME_INVALID,
    NOTE_TIME_MODEL,
    NOTE_TIME_NO_CLOCK,
    NOTE_TIME_NO_DATE,
    NOTE_TIME_OUT_OF_RANGE,
    NOTE_TIME_TEXT_UNVERIFIED,
    SESSION_FIELDS,
    SESSION_PERSISTED_FIELDS,
    TIME_SOURCE_MODEL,
    TIME_SOURCE_TEXT,
    build_extraction_messages,
    build_system_prompt,
    clean_action,
    clean_int,
    clean_intent,
    clean_price,
    clean_script_type,
    clean_status,
    clean_text,
    derive_intent,
    extract_json_object,
    extract_message,
    normalize_booking,
    normalize_session,
    parse_extraction_response,
    parse_time_phrase,
    resolve_start_time,
    sessions_frame,
)
from app.tools.storage import (
    NOTE_TIME_MISSING,
    SESSIONS_COLUMNS,
    STATUS_CANCELLED,
    STATUS_PENDING,
    make_session_id,
    upsert_sessions,
)

NOW = pd.Timestamp("2026-10-18 12:00")  # 周日
STORE = "迷雾剧场"
SCRIPT = "年轮"
START = "2026-10-20 19:30"

SOURCE_AD = "【迷雾剧场】年轮 20号19:30 8人本 168/位 已约6人 还差2人"
SOURCE_BOOKING = "迷雾剧场 年轮 20号19:30 张三 约2人"


# ===========================================================================
# 测试基础设施
# ===========================================================================

@pytest.fixture(autouse=True)
def _block_real_model(monkeypatch):
    """任何测试都不许初始化真实模型 —— 一旦调用就直接失败。"""

    def _boom(*args, **kwargs):
        raise AssertionError("测试中不应初始化真实模型")

    monkeypatch.setattr("app.config.get_model", _boom)


class FakeLLM:
    def __init__(self, content: str = "", *, error: Exception | None = None):
        self.content = content
        self.error = error
        self.calls: list[list[dict[str, str]]] = []

    def invoke(self, messages):
        self.calls.append(messages)
        if self.error is not None:
            raise self.error
        return SimpleNamespace(content=self.content)


def _payload(
    *,
    sessions=None,
    bookings=None,
    intent=None,
    warnings=None,
) -> str:
    return json.dumps({
        "intent": intent,
        "sessions": [] if sessions is None else sessions,
        "bookings": [] if bookings is None else bookings,
        "warnings": [] if warnings is None else warnings,
    }, ensure_ascii=False)


def _session(**extra) -> dict:
    row = {
        "store_name": STORE,
        "script_name": SCRIPT,
        "script_type": "情感",
        "start_time": START,
        "start_time_text": "20号19:30",
        "total_seats": 8,
        "list_price": 168,
        "price_text": "168元/位",
        "count_text": "8人本",
        "remaining_reported": 2,
        "booked_reported": 6,
        "status": None,
        "source_excerpt": "年轮 20号19:30 8人本 168/位 已约6人 还差2人",
    }
    row.update(extra)
    return row


def _booking(**extra) -> dict:
    row = {
        "action": ACTION_BOOK,
        "player_name": "张三",
        "seats": 2,
        "store_name": STORE,
        "script_name": SCRIPT,
        "start_time": START,
        "start_time_text": "20号19:30",
        "source_excerpt": SOURCE_BOOKING,
    }
    row.update(extra)
    return row


def _parse(payload, *, source=SOURCE_AD, default_store=None):
    return parse_extraction_response(
        payload, source_text=source, now=NOW, default_store=default_store
    )


def _fmt(value) -> str | None:
    return None if value is None else pd.Timestamp(value).strftime("%Y-%m-%d %H:%M")


# ===========================================================================
# 一、JSON 定位
# ===========================================================================

class TestJSON提取:

    def test_纯JSON直接解析(self):
        obj = extract_json_object('{"sessions": []}')
        assert obj == {"sessions": []}

    def test_代码围栏里的JSON(self):
        raw = "```json\n" + _payload(sessions=[_session()]) + "\n```"
        result = _parse(raw)
        assert result.ok is True
        assert len(result.sessions) == 1

    def test_前后带解释文字的JSON(self):
        raw = "好的，结果如下：\n" + _payload(sessions=[_session()]) + "\n以上。"
        result = _parse(raw)
        assert result.ok is True
        assert len(result.sessions) == 1

    def test_坏JSON显式失败并保留原文(self):
        result = _parse('{"sessions": [')
        assert result.ok is False
        assert result.error_code == ERROR_BAD_JSON
        assert result.raw_snippet == '{"sessions": ['

    def test_空响应(self):
        result = _parse("")
        assert result.ok is False
        assert result.error_code == ERROR_BAD_JSON
        assert result.raw_snippet is None

    def test_顶层数组是bad_root(self):
        result = _parse("[1, 2]")
        assert result.ok is False
        assert result.error_code == ERROR_BAD_ROOT

    def test_extract_json_object遇到坏JSON抛ValueError(self):
        with pytest.raises(ValueError) as info:
            extract_json_object("{bad")
        assert info.value.code == ERROR_BAD_JSON


# ===========================================================================
# 二、顶层结构与意图
# ===========================================================================

class Test顶层结构与意图:

    def test_缺字段按空数组处理(self):
        result = _parse("{}")
        assert result.ok is True
        assert result.sessions == []
        assert result.bookings == []
        assert result.intent == INTENT_UNKNOWN
        assert NOTE_NO_RECORDS in result.warnings

    def test_null按空数组处理(self):
        result = _parse('{"sessions": null, "bookings": null}')
        assert result.ok is True
        assert result.sessions == []

    def test_单个对象包装成列表并警告(self):
        result = _parse(_payload(sessions=_session()))
        assert len(result.sessions) == 1
        assert any("单个对象" in item for item in result.warnings)

    def test_sessions不是数组时报bad_root(self):
        result = _parse('{"sessions": "没有", "bookings": []}')
        assert result.ok is False
        assert result.error_code == ERROR_BAD_ROOT

    def test_数组里的非对象条目被跳过(self):
        payload = _payload(sessions=[1, "坏条目", _session()])
        result = _parse(payload)
        assert result.ok is True
        assert len(result.sessions) == 1
        assert any("2 条不是对象" in item for item in result.warnings)

    def test_模型warnings被过滤并加前缀(self):
        payload = _payload(warnings=["价格不明确", 123, None, "人数可能是 7"])
        result = _parse(payload)
        assert result.warnings.count("模型提示：价格不明确") == 1
        assert "模型提示：人数可能是 7" in result.warnings
        assert all("123" not in item for item in result.warnings)

    def test_模型warnings最多保留10条(self):
        payload = _payload(warnings=[f"提示{i}" for i in range(12)])
        result = _parse(payload)
        model_notes = [item for item in result.warnings if item.startswith("模型提示：")]
        assert len(model_notes) == 10

    def test_意图按记录重算(self):
        result = _parse(_payload(sessions=[_session()], intent=INTENT_BOOKING))
        assert result.intent == INTENT_AD
        assert any("按记录重算" in item for item in result.warnings)

    def test_只有预约记录时意图是预约(self):
        result = _parse(_payload(bookings=[_booking()]), source=SOURCE_BOOKING)
        assert result.intent == INTENT_BOOKING

    def test_只有跳车记录时意图是跳车(self):
        result = _parse(
            _payload(bookings=[_booking(action=ACTION_CANCEL)]),
            source=SOURCE_BOOKING,
        )
        assert result.intent == INTENT_CANCELLATION

    def test_广告和预约同时存在是混合(self):
        result = _parse(
            _payload(sessions=[_session()], bookings=[_booking()]),
            source=SOURCE_AD + "\n" + SOURCE_BOOKING,
        )
        assert result.intent == INTENT_MIXED

    def test_无记录时用模型意图兜底(self):
        result = _parse(_payload(intent=INTENT_BOOKING))
        assert result.intent == INTENT_BOOKING
        assert NOTE_INTENT_FALLBACK in result.warnings
        assert NOTE_NO_RECORDS in result.warnings


# ===========================================================================
# 三、标量清洗
# ===========================================================================

class Test标量清洗:

    def test_文本折叠空白(self):
        assert clean_text("  年轮 \n 情感本  ") == "年轮 情感本"
        assert clean_text("   ") is None
        assert clean_text(None) is None

    def test_剧本类型列表合并(self):
        assert clean_script_type(["情感", " 还原 "]) == "情感/还原"

    def test_整数支持人和区间(self):
        assert clean_int("8人") == (8, None)
        assert clean_int("6-8人本") == (8, None)
        assert clean_int("０８人") == (8, None)

    def test_整数无法解析(self):
        value, note = clean_int("待定")
        assert value is None
        assert note is not None

    def test_整数范围校验(self):
        value, note = clean_int("0", label="总位", minimum=1)
        assert value is None
        assert "合理范围" in note

    def test_价格每位(self):
        assert clean_price("168元/位") == (168.0, None)

    def test_价格免费是0(self):
        assert clean_price("免费") == (0.0, None)

    def test_价格区间取上限(self):
        assert clean_price("168-198元/位") == (198.0, None)

    def test_原价特价取原价(self):
        assert clean_price("原价168 特价158") == (168.0, None)

    def test_只有总价不猜每位价格(self):
        value, note = clean_price("总价800")
        assert value is None
        assert "只有总价" in note

    def test_场次状态满员置空(self):
        value, note = clean_status("满员")
        assert value is None
        assert "不是场次状态" in note

    def test_场次状态别名(self):
        assert clean_status("拼车中") == (STATUS_PENDING, None)
        assert clean_status("已取消") == (STATUS_CANCELLED, None)

    def test_动作别名(self):
        assert clean_action("+1") == ACTION_BOOK
        assert clean_action("报名") == ACTION_BOOK
        assert clean_action("不去") == ACTION_CANCEL
        assert clean_action("-1") == ACTION_CANCEL

    def test_动作同时命中两类时不猜(self):
        assert clean_action("取消预约") is None

    def test_意图非法值(self):
        value, note = clean_intent("随便")
        assert value is None
        assert "无法识别" in note

    def test_意图合法值(self):
        assert clean_intent(INTENT_MIXED) == (INTENT_MIXED, None)


# ===========================================================================
# 四、时间的确定性解析
# ===========================================================================

class Test时间确定性解析:

    def test_完整日期横杠(self):
        ts, note = parse_time_phrase("2026-10-20 19:30", now=NOW)
        assert note is None
        assert _fmt(ts) == "2026-10-20 19:30"

    def test_完整日期中文(self):
        ts, note = parse_time_phrase("2026年10月20日 19:30", now=NOW)
        assert note is None
        assert _fmt(ts) == "2026-10-20 19:30"

    def test_完整日期斜杠(self):
        ts, note = parse_time_phrase("2026/10/20 19:30", now=NOW)
        assert note is None
        assert _fmt(ts) == "2026-10-20 19:30"

    def test_无年份月日(self):
        ts, note = parse_time_phrase("10月20日 19:30", now=NOW)
        assert note is None
        assert _fmt(ts) == "2026-10-20 19:30"

    def test_号数取当前月(self):
        ts, note = parse_time_phrase("20号19:30", now=NOW)
        assert note is None
        assert _fmt(ts) == "2026-10-20 19:30"

    def test_跨年顺延(self):
        now = pd.Timestamp("2026-12-30 12:00")
        ts, note = parse_time_phrase("1月5日 19:30", now=now)
        assert note is None
        assert _fmt(ts) == "2027-01-05 19:30"

    def test_号数跨月顺延(self):
        now = pd.Timestamp("2026-12-30 12:00")
        ts, note = parse_time_phrase("2号19:30", now=now)
        assert note is None
        assert _fmt(ts) == "2027-01-02 19:30"

    def test_全角数字和冒号(self):
        ts, note = parse_time_phrase("２０号１９：３０", now=NOW)
        assert note is None
        assert _fmt(ts) == "2026-10-20 19:30"

    def test_只有日期没有时刻(self):
        ts, note = parse_time_phrase("10月20日", now=NOW)
        assert ts is None
        assert note == NOTE_TIME_NO_CLOCK

    def test_只有时刻没有日期(self):
        ts, note = parse_time_phrase("19:30", now=NOW)
        assert ts is None
        assert note == NOTE_TIME_NO_DATE

    def test_没有时段且小时小于12不猜(self):
        ts, note = parse_time_phrase("20号 8点", now=NOW)
        assert ts is None
        assert note == NOTE_TIME_AMBIGUOUS

    def test_晚上修正为24小时制(self):
        ts, note = parse_time_phrase("10月20日晚上8点", now=NOW)
        assert note is None
        assert _fmt(ts) == "2026-10-20 20:00"

    def test_点半(self):
        ts, note = parse_time_phrase("10月20日 19点半", now=NOW)
        assert note is None
        assert _fmt(ts) == "2026-10-20 19:30"

    def test_非法日期(self):
        ts, note = parse_time_phrase("2月30日 19:30", now=NOW)
        assert ts is None
        assert note == NOTE_TIME_INVALID


# ===========================================================================
# 五、时间混合解析：原文优先
# ===========================================================================

class Test时间混合优先:

    def test_原文时间优先于模型时间(self):
        result = resolve_start_time(
            text="年轮 20号19:30",
            start_time_text="20号19:30",
            model_start_time="2026-10-21 19:30",
            now=NOW,
        )
        assert result.start_time == "2026-10-20 19:30"
        assert result.start_time_source == TIME_SOURCE_TEXT
        assert NOTE_TIME_CONFLICT in result.notes

    def test_原文与模型一致时没有冲突备注(self):
        result = resolve_start_time(
            text="年轮 20号19:30",
            start_time_text="20号19:30",
            model_start_time="2026-10-20 19:30",
            now=NOW,
        )
        assert result.start_time_source == TIME_SOURCE_TEXT
        assert NOTE_TIME_CONFLICT not in result.notes

    def test_原文解析不了才用模型推断(self):
        result = resolve_start_time(
            text="年轮 明晚8点",
            start_time_text="明晚8点",
            model_start_time="2026-10-19 20:00",
            now=NOW,
        )
        assert result.start_time == "2026-10-19 20:00"
        assert result.start_time_source == TIME_SOURCE_MODEL
        assert NOTE_TIME_MODEL in result.notes

    def test_时间短语不在原文中不采用(self):
        result = resolve_start_time(
            text="年轮 21号19:30",
            start_time_text="20号19:30",
            model_start_time="2026-10-20 19:30",
            now=NOW,
        )
        assert result.start_time_source == TIME_SOURCE_MODEL
        assert NOTE_TIME_TEXT_UNVERIFIED in result.notes

    def test_模型时间超出范围(self):
        result = resolve_start_time(
            text="年轮 明晚8点",
            start_time_text="明晚8点",
            model_start_time="2027-10-01 19:30",
            now=NOW,
        )
        assert result.start_time is None
        assert NOTE_TIME_OUT_OF_RANGE in result.notes

    def test_原文时间超出范围时改用模型(self):
        result = resolve_start_time(
            text="年轮 2025年1月1日 19:30",
            start_time_text="2025年1月1日 19:30",
            model_start_time="2026-10-20 19:30",
            now=NOW,
        )
        assert result.start_time == "2026-10-20 19:30"
        assert result.start_time_source == TIME_SOURCE_MODEL

    def test_没有时间短语也没有模型时间(self):
        result = resolve_start_time(
            text="年轮 还差2人",
            start_time_text=None,
            model_start_time=None,
            now=NOW,
        )
        assert result.start_time is None
        assert NOTE_TIME_MISSING in result.notes

    def test_时间短语能验证但解析不了(self):
        result = resolve_start_time(
            text="年轮 明天",
            start_time_text="明天",
            model_start_time=None,
            now=NOW,
        )
        assert result.start_time is None
        assert NOTE_TIME_NO_DATE in result.notes

    def test_模型时间格式非法(self):
        result = resolve_start_time(
            text="年轮 明天",
            start_time_text="明天",
            model_start_time="明晚八点",
            now=NOW,
        )
        assert result.start_time is None
        assert result.start_time_source is None
        assert NOTE_TIME_NO_DATE in result.notes


# ===========================================================================
# 六、场次会议归一
# ===========================================================================

class Test会话记录:

    def test_完整广告(self):
        result = _parse(_payload(sessions=[_session()]))
        row = result.sessions[0]

        assert result.ok is True
        assert row["store_name"] == STORE
        assert row["script_name"] == SCRIPT
        assert row["script_type"] == "情感"
        assert row["start_time"] == START
        assert row["start_time_source"] == TIME_SOURCE_TEXT
        assert row["total_seats"] == 8
        assert row["list_price"] == 168.0
        assert row["remaining_reported"] == 2
        assert row["booked_reported"] == 6
        assert row["status"] is None
        assert row["source_text"] == SOURCE_AD
        assert row["notes"] == ()
        assert list(row) == SESSION_FIELDS

    def test_默认门店补齐(self):
        source = "年轮 20号19:30 8人本 168/位"
        item = _session(store_name=None, source_excerpt=source,
                        remaining_reported=None, booked_reported=None)
        row = _parse(_payload(sessions=[item]), source=source,
                     default_store=STORE).sessions[0]
        assert row["store_name"] == STORE
        assert NOTE_STORE_DEFAULTED in row["notes"]
        assert NOTE_STORE_UNVERIFIED not in row["notes"]

    def test_没有默认门店时保持null(self):
        source = "年轮 20号19:30 8人本 168/位"
        item = _session(store_name=None, source_excerpt=source,
                        remaining_reported=None, booked_reported=None)
        row = _parse(_payload(sessions=[item]), source=source).sessions[0]
        assert row["store_name"] is None
        assert NOTE_STORE_MISSING in row["notes"]

    def test_门店不在原文时保留但提示核对(self):
        source = "年轮 20号19:30 8人本 168/位"
        item = _session(source_excerpt=source,
                        remaining_reported=None, booked_reported=None)
        row = _parse(_payload(sessions=[item]), source=source).sessions[0]
        assert row["store_name"] == STORE
        assert NOTE_STORE_UNVERIFIED in row["notes"]

    def test_剧本不在原文时保留但提示核对(self):
        source = "20号19:30 8人本 168/位"
        item = _session(store_name=None, source_excerpt=source,
                        remaining_reported=None, booked_reported=None)
        row = _parse(_payload(sessions=[item]), source=source,
                     default_store=STORE).sessions[0]
        assert row["script_name"] == SCRIPT
        assert NOTE_SCRIPT_UNVERIFIED in row["notes"]

    def test_提示人数原样保留不互相推算(self):
        item = _session(total_seats=None, booked_reported=3, remaining_reported=None)
        row = _parse(_payload(sessions=[item])).sessions[0]
        assert row["booked_reported"] == 3
        assert row["remaining_reported"] is None

    def test_人数对不上时提示核对(self):
        item = _session(total_seats=8, booked_reported=3, remaining_reported=2)
        row = _parse(_payload(sessions=[item])).sessions[0]
        assert NOTE_COUNT_INCONSISTENT in row["notes"]

    def test_满员不是场次状态(self):
        row = _parse(_payload(sessions=[_session(status="满员")])).sessions[0]
        assert row["status"] is None
        assert any("不是场次状态" in note for note in row["notes"])

    def test_多个场次分别抽取(self):
        source = SOURCE_AD + "\n蛊魂铃 21号21:00 6人本 198/位"
        second = _session(script_name="蛊魂铃", start_time_text="21号21:00",
                          start_time="2026-10-21 21:00", total_seats=6,
                          list_price=198, remaining_reported=None,
                          booked_reported=None,
                          source_excerpt="蛊魂铃 21号21:00 6人本 198/位")
        result = _parse(_payload(sessions=[_session(), second]), source=source)
        assert [row["script_name"] for row in result.sessions] == ["年轮", "蛊魂铃"]

    def test_原文片段找不到时置空(self):
        row = _parse(_payload(sessions=[_session(source_excerpt="完全不存在的片段")])).sessions[0]
        assert row["source_excerpt"] is None
        assert NOTE_EXCERPT_UNVERIFIED in row["notes"]

    def test_没有原文片段时不做校验(self):
        row = _parse(_payload(sessions=[_session(source_excerpt=None)])).sessions[0]
        assert row["source_excerpt"] is None
        assert NOTE_EXCERPT_UNVERIFIED not in row["notes"]


# ===========================================================================
# 七、预约记录归一
# ===========================================================================

class Test预约记录:

    def test_默认一人(self):
        item = _booking()
        item.pop("seats")
        row = _parse(_payload(bookings=[item]), source=SOURCE_BOOKING).bookings[0]
        assert row["seats"] == 1
        assert NOTE_SEATS_INVALID not in row["notes"]

    def test_多人预约(self):
        row = _parse(_payload(bookings=[_booking()]), source=SOURCE_BOOKING).bookings[0]
        assert row["seats"] == 2
        assert row["action"] == ACTION_BOOK

    def test_跳车(self):
        row = _parse(
            _payload(bookings=[_booking(action=ACTION_CANCEL)]),
            source=SOURCE_BOOKING,
        ).bookings[0]
        assert row["action"] == ACTION_CANCEL

    def test_缺昵称保留记录并提示(self):
        row = _parse(
            _payload(bookings=[_booking(player_name=None)]),
            source=SOURCE_BOOKING,
        ).bookings[0]
        assert row["player_name"] is None
        assert NOTE_PLAYER_MISSING in row["notes"]
        assert len(row) == len(BOOKING_FIELDS)

    def test_候选场次ID与make_session_id一致(self):
        row = _parse(_payload(bookings=[_booking()]), source=SOURCE_BOOKING).bookings[0]
        assert row["candidate_session_id"] == make_session_id(STORE, SCRIPT, START)

    def test_身份不全时没有候选ID(self):
        item = _booking(store_name=None)
        row = _parse(_payload(bookings=[item]), source=SOURCE_BOOKING).bookings[0]
        assert row["candidate_session_id"] is None
        assert NOTE_IDENTITY_INCOMPLETE in row["notes"]

    def test_动作缺失时用原文关键词兜底(self):
        source = "迷雾剧场 年轮 20号19:30 张三 跳车"
        item = _booking(action=None, source_excerpt=source)
        row = _parse(_payload(bookings=[item]), source=source).bookings[0]
        assert row["action"] == ACTION_CANCEL
        assert NOTE_ACTION_FALLBACK in row["notes"]

    def test_完全无法判断动作(self):
        source = "迷雾剧场 年轮 20号19:30 张三"
        item = _booking(action=None, source_excerpt=source)
        row = _parse(_payload(bookings=[item]), source=source).bookings[0]
        assert row["action"] is None
        assert NOTE_ACTION_MISSING in row["notes"]


# ===========================================================================
# 八、失败与降级
# ===========================================================================

class Test失败与降级:

    def test_空输入(self):
        result = extract_message("   ", model=FakeLLM(), now=NOW)
        assert result.ok is False
        assert result.error_code == ERROR_EMPTY_INPUT

    def test_模型调用异常(self):
        model = FakeLLM(error=RuntimeError("连接失败：sk-abcdef123456"))
        result = extract_message(SOURCE_AD, model=model, now=NOW)
        assert result.ok is False
        assert result.error_code == ERROR_LLM_CALL
        assert "sk-***" in result.error
        assert "abcdef123456" not in result.error

    def test_缺配置返回结构化失败(self, monkeypatch):
        def _raise(**kwargs):
            raise RuntimeError("未找到 DEEPSEEK_API_KEY sk-abcdef123456")

        monkeypatch.setattr(llm, "get_default_model", _raise)
        result = extract_message(SOURCE_AD, now=NOW)
        assert result.ok is False
        assert result.error_code == ERROR_CONFIG
        assert "sk-***" in result.error

    def test_模型初始化失败(self, monkeypatch):
        def _raise(**kwargs):
            raise ValueError("模型名不认识")

        monkeypatch.setattr(llm, "get_default_model", _raise)
        result = extract_message(SOURCE_AD, now=NOW)
        assert result.ok is False
        assert result.error_code == ERROR_MODEL_INIT

    def test_单个字段坏了但整体成功(self):
        result = _parse(_payload(sessions=[_session(total_seats="待定")]))
        assert result.ok is True
        assert result.sessions[0]["total_seats"] is None
        assert any("总位" in note for note in result.sessions[0]["notes"])

    def test_KeyboardInterrupt不被吞掉(self):
        model = FakeLLM(error=KeyboardInterrupt())
        with pytest.raises(KeyboardInterrupt):
            extract_message(SOURCE_AD, model=model, now=NOW)


# ===========================================================================
# 九、契约：能直接接工具 2 与工具 6
# ===========================================================================

class Test契约:

    def test_sessions_frame空表列齐全(self):
        frame = sessions_frame([])
        assert list(frame.columns) == SESSION_PERSISTED_FIELDS

    def test_sessions_frame不含提示字段(self):
        row = _parse(_payload(sessions=[_session()])).sessions[0]
        frame = sessions_frame([row])
        assert "remaining_reported" not in frame.columns
        assert "booked_reported" not in frame.columns
        assert "notes" not in frame.columns
        assert "source_excerpt" not in frame.columns

    def test_抽取结果可以直接喂dedupe(self):
        result = _parse(_payload(sessions=[_session()]))
        merged = dedupe_sessions(sessions_frame(result.sessions))
        assert list(merged.columns) == SESSIONS_COLUMNS + DEDUPE_COLUMNS
        assert len(merged) == 1
        assert merged.iloc[0]["source_text"] == SOURCE_AD

    def test_dedupe结果可以直接落盘且不含提示字段(self, tmp_path):
        result = _parse(_payload(sessions=[_session()]))
        merged = dedupe_sessions(sessions_frame(result.sessions))
        path = tmp_path / "sessions.xlsx"

        written = upsert_sessions(merged, path)
        assert len(written) == 1
        assert written.iloc[0]["total_seats"] == 8

        columns = pd.read_excel(path).columns.tolist()
        assert columns == SESSIONS_COLUMNS
        assert "remaining_reported" not in columns
        assert "notes" not in columns

    def test_to_dict可JSON序列化(self):
        result = _parse(_payload(sessions=[_session()], bookings=[_booking()]),
                        source=SOURCE_AD + "\n" + SOURCE_BOOKING)
        encoded = json.dumps(result.to_dict(), ensure_ascii=False)
        assert '"sessions"' in encoded
        assert '"notes"' in encoded


# ===========================================================================
# 十、Prompt
# ===========================================================================

class Test提示词:

    def test_包含当前时间和星期(self):
        prompt = build_system_prompt(now=NOW)
        assert "2026-10-18 12:00" in prompt
        assert "周日" in prompt

    def test_包含默认门店(self):
        prompt = build_system_prompt(now=NOW, default_store=STORE)
        assert STORE in prompt

    def test_反注入与null约束(self):
        prompt = build_system_prompt(now=NOW)
        assert "忽略以上规则" in prompt
        assert "一律填 null" in prompt
        assert "不得用默认值覆盖" in prompt

    def test_原文被包裹在用户消息里(self):
        messages = build_extraction_messages(SOURCE_AD, now=NOW)
        assert messages[0]["role"] == "system"
        assert messages[1]["role"] == "user"
        assert "【原文开始】" in messages[1]["content"]
        assert SOURCE_AD in messages[1]["content"]
        assert "【原文结束】" in messages[1]["content"]

    def test_结构示例是合法JSON(self):
        prompt = build_system_prompt(now=NOW)
        example = prompt.split("【结构示例】", 1)[1]
        example = example[example.find("{"):]
        obj = json.loads(example)
        assert obj["sessions"] == []
        assert obj["bookings"] == []

    def test_extract_message把消息交给模型(self):
        model = FakeLLM(_payload(sessions=[_session()]))
        extract_message(SOURCE_AD, model=model, now=NOW, default_store=STORE)

        assert len(model.calls) == 1
        assert model.calls[0][1]["content"].startswith("【原文开始】")
        assert STORE in model.calls[0][0]["content"]


# ===========================================================================
# 十一、几个纯函数直接调用的边界
# ===========================================================================

class Test纯函数边界:

    def test_derive_intent没有记录时用兜底(self):
        assert derive_intent([], [], INTENT_BOOKING) == INTENT_BOOKING
        assert derive_intent([], [], None) == INTENT_UNKNOWN

    def test_derive_intent预约和跳车混合(self):
        bookings = [{"action": ACTION_BOOK}, {"action": ACTION_CANCEL}]
        assert derive_intent([], bookings) == INTENT_MIXED

    def test_normalize_session接受最小输入(self):
        row = normalize_session({}, source_text="空消息", now=NOW)
        assert list(row) == SESSION_FIELDS
        assert row["store_name"] is None
        assert row["start_time"] is None

    def test_normalize_booking接受最小输入(self):
        row = normalize_booking({}, source_text="空消息", now=NOW)
        assert list(row) == BOOKING_FIELDS
        assert row["seats"] == 1
        assert row["action"] is None

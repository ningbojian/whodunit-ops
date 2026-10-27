"""工具 7 营销文案生成 —— 单元测试。

运行：
  PYTHONUTF8=1 "C:/Users/Lenovo/anaconda3/python.exe" -m pytest tests/test_copywriter.py -v

**不联网、不需要 API key、不依赖系统时钟**（now 一律显式注入，模型用 FakeLLM）。

重点回归四件事：

  1. 选场逐级回退：高危 → 关注 → 正常，高危一定排在前面；
  2. public_view() 仍保留 contribution_per_seat，工具 7 的白名单必须再挡一层；
  3. 内部财务词 / 未授权价格命中时，community 与 moments **同时为空**；
  4. 无 key、模型异常、坏 JSON 都返回结构化失败，不向调用方抛异常。
"""

from __future__ import annotations

import json

import pandas as pd
import pytest

from app.tools import copywriter
from app.tools.copywriter import (
    COPY_FIELDS,
    COPY_PROMPT_FIELDS,
    DEFAULT_TEMPERATURE,
    ERROR_BAD_COPY,
    ERROR_BAD_JSON,
    ERROR_BAD_REQUEST,
    ERROR_BAD_ROOT,
    ERROR_CONFIG,
    ERROR_INTERNAL_LEAK,
    ERROR_LLM_CALL,
    ERROR_MODEL_INIT,
    ERROR_NO_SESSIONS,
    ERROR_PRICE_MISMATCH,
    INTERNAL_TERMS,
    NOTE_FILL_NORMAL,
    NOTE_FILL_WATCH,
    NOTE_HIGH_TRUNCATED,
    NOTE_MULTI_STORE,
    NOTE_SKIP_FULL,
    NOTE_SKIP_INCOMPLETE,
    NOTE_STORE_EMPTY,
    NOTE_UNKNOWN_PRIORITY,
    CopyResult,
    build_copy_messages,
    generate_copy,
    parse_copy_response,
    select_copy_sessions,
)
from app.tools.pricing import price_sessions, public_view
from app.tools.storage import STATUS_CANCELLED, STATUS_PENDING, _coerce_types

NOW = pd.Timestamp("2026-10-20 18:00:00")
STORE = "迷雾剧场"
OTHER_STORE = "另一家店"


# ===========================================================================
# 基础设施
# ===========================================================================

class FakeLLM:
    """只实现 .invoke(messages) —— 工具 7 只依赖这一个方法。"""

    def __init__(self, content: str = "", *, error: Exception | None = None):
        self.content = content
        self.error = error
        self.calls: list[list[dict[str, str]]] = []

    def invoke(self, messages):
        self.calls.append(messages)
        if self.error is not None:
            raise self.error
        return self.content


@pytest.fixture(autouse=True)
def _禁止真实模型(monkeypatch):
    """任何测试意外走到真实 get_model 时立刻失败。"""
    def fail(*args, **kwargs):
        raise AssertionError("测试不应初始化真实模型")

    monkeypatch.setattr("app.config.get_model", fail)


def _表(rows) -> pd.DataFrame:
    """把一批场次记录整形成规范表（借用 storage 自己的整形逻辑）。"""
    defaults = {
        "store_name": STORE,
        "total_seats": 8,
        "booked_seats": 0,
        "list_price": 168,
        "status": STATUS_PENDING,
    }
    return _coerce_types(pd.DataFrame([{**defaults, **row} for row in rows]))


def _场(*, 剩几小时=24, 总位=8, 已约=0, 原价=168, 名="某场", 门店=STORE,
        状态=STATUS_PENDING) -> dict:
    return {
        "store_name": 门店,
        "script_name": 名,
        "start_time": None if 剩几小时 is None else NOW + pd.Timedelta(hours=剩几小时),
        "total_seats": 总位,
        "booked_seats": 已约,
        "list_price": 原价,
        "status": 状态,
    }


def _高危(名="高危本") -> dict:
    # 2 小时后开场、8 人本已约 7 人：倍数 1.0、时间档 2、可救档 2 -> 高危。
    return _场(名=名, 剩几小时=2, 已约=7)


def _关注(名="关注本", 剩几小时=4, 已约=4) -> dict:
    # 4 小时后、8 人本已约 4 人：倍数 2.0、时间档 1、可救档 1 -> 关注。
    # 同时命中中让 88%：168 -> 148，省 20，用于价格校验测试。
    return _场(名=名, 剩几小时=剩几小时, 已约=已约)


def _普通(名="普通本") -> dict:
    return _场(名=名)


def _选中名(selection) -> list[str]:
    return [record["script_name"] for record in selection.sessions]


def _公开记录(**overrides) -> dict:
    record = {
        "session_id": "s1",
        "store_name": STORE,
        "script_name": "年轮",
        "script_type": "情感",
        "start_time": "2026-10-20 20:00",
        "total_seats": 8,
        "booked_seats": 4,
        "remaining_seats": 4,
        "list_price": 168.0,
        "recommended_price": 148.0,
        "seat_saving": 20.0,
    }
    record.update(overrides)
    return record


def _回复(community: str, moments: str, warnings=None) -> str:
    return json.dumps(
        {"community": community, "moments": moments, "warnings": warnings or []},
        ensure_ascii=False,
    )


def _解析(community: str, moments: str, warnings=None, **overrides) -> CopyResult:
    return parse_copy_response(
        _回复(community, moments, warnings),
        selected_sessions=[_公开记录(**overrides)],
    )


# ===========================================================================
# 一、选场
# ===========================================================================

class TestSelection:

    def test_高危排在关注和普通前面(self):
        selection = select_copy_sessions(
            _表([_普通(), _关注(), _高危()]), now=NOW,
        )

        assert _选中名(selection) == ["高危本", "关注本", "普通本"]

    def test_没有高危时仍然能生成(self):
        selection = select_copy_sessions(
            _表([_普通(), _关注()]), now=NOW,
        )

        assert _选中名(selection) == ["关注本", "普通本"]
        assert NOTE_FILL_WATCH in selection.warnings
        assert NOTE_FILL_NORMAL in selection.warnings

    def test_高危不足时用关注补满(self):
        selection = select_copy_sessions(
            _表([_高危(), _关注("关注一", 剩几小时=4), _关注("关注二", 剩几小时=5, 已约=5), _普通()]),
            now=NOW, max_sessions=3,
        )

        assert _选中名(selection) == ["高危本", "关注一", "关注二"]
        assert NOTE_FILL_WATCH in selection.warnings
        assert NOTE_FILL_NORMAL not in selection.warnings

    def test_高危过多时截断(self):
        selection = select_copy_sessions(
            _表([_高危("高危一"), _高危("高危二"), _高危("高危三")]),
            now=NOW, max_sessions=2,
        )

        assert _选中名(selection) == ["高危一", "高危二"]
        assert NOTE_HIGH_TRUNCATED.format(total=3, take=2) in selection.warnings

    def test_默认最多三场(self):
        rows = [_普通(f"普通{i}") for i in range(5)]
        selection = select_copy_sessions(_表(rows), now=NOW)

        assert len(selection.sessions) == 3

    def test_max_sessions越界(self):
        with pytest.raises(ValueError):
            select_copy_sessions(_表([_普通()]), now=NOW, max_sessions=6)

    def test_max_sessions必须是整数(self):
        with pytest.raises(TypeError):
            select_copy_sessions(_表([_普通()]), now=NOW, max_sessions=2.0)

        with pytest.raises(TypeError):
            select_copy_sessions(_表([_普通()]), now=NOW, max_sessions=True)

    def test_满员场次被排除并提示(self):
        selection = select_copy_sessions(
            _表([_普通(), _场(名="满员本", 已约=8)]), now=NOW,
        )

        assert _选中名(selection) == ["普通本"]
        assert NOTE_SKIP_FULL.format(count=1) in selection.warnings

    def test_过期和已取消不进入选场(self):
        selection = select_copy_sessions(
            _表([
                _场(名="已过期", 剩几小时=-1),
                _场(名="已取消", 状态=STATUS_CANCELLED),
            ]),
            now=NOW,
        )

        assert selection.empty

    def test_信息不完整不进入选场(self):
        rows = [
            {**_场(名="缺门店"), "store_name": None},
            {**_场(名="缺剧本"), "script_name": None},
            _场(名="缺时间", 剩几小时=None),
            _场(名="缺空位", 总位=None),
        ]
        selection = select_copy_sessions(_表(rows), now=NOW)

        assert selection.empty
        assert NOTE_SKIP_INCOMPLETE.format(count=4) in selection.warnings

    def test_多个门店都有可推广场次时报错(self):
        with pytest.raises(ValueError, match="多个门店"):
            select_copy_sessions(
                _表([_普通(), _场(名="别家本", 门店=OTHER_STORE)]), now=NOW,
            )

    def test_多个门店但只有一家可推广时使用那一家(self):
        selection = select_copy_sessions(
            _表([_普通(), _场(名="别家满员", 门店=OTHER_STORE, 已约=8)]),
            now=NOW,
        )

        assert _选中名(selection) == ["普通本"]
        assert NOTE_MULTI_STORE.format(store=STORE) in selection.warnings

    def test_指定门店后只取该门店(self):
        selection = select_copy_sessions(
            _表([_普通(), _场(名="别家本", 门店=OTHER_STORE)]),
            now=NOW, store_name=OTHER_STORE,
        )

        assert _选中名(selection) == ["别家本"]
        assert all(s["store_name"] == OTHER_STORE for s in selection.sessions)

    def test_指定门店没有场次(self):
        selection = select_copy_sessions(
            _表([_普通()]), now=NOW, store_name="不存在的店",
        )

        assert selection.empty
        assert NOTE_STORE_EMPTY.format(store="不存在的店") in selection.warnings

    def test_门店空白视为未指定(self):
        selection = select_copy_sessions(_表([_普通()]), now=NOW, store_name="   ")

        assert _选中名(selection) == ["普通本"]

    def test_空表返回空选场(self):
        selection = select_copy_sessions(pd.DataFrame(), now=NOW)

        assert selection.empty
        assert selection.warnings == ()

    def test_记录只含白名单字段(self):
        selection = select_copy_sessions(_表([_高危()]), now=NOW)

        assert tuple(selection.sessions[0].keys()) == COPY_FIELDS

    def test_不修改输入表(self):
        frame = _表([_高危(), _关注(), _普通()])
        before = frame.copy(deep=True)

        select_copy_sessions(frame, now=NOW)

        pd.testing.assert_frame_equal(frame, before)

    def test_传入已定价表时会先剔除计算列再重算(self):
        frame = _表([_高危(), _关注()])
        priced = price_sessions(frame, now=NOW)
        assert "recommended_price" in priced.columns

        selection = select_copy_sessions(priced, now=NOW)

        assert _选中名(selection) == ["高危本", "关注本"]
        assert tuple(selection.sessions[0].keys()) == COPY_FIELDS

    def test_未知优先级按普通场次处理(self, monkeypatch):
        unknown = pd.DataFrame([{
            "store_name": STORE,
            "script_name": "未知本",
            "start_time": NOW + pd.Timedelta(hours=24),
            "total_seats": 8,
            "booked_seats": 0,
            "remaining_seats": 8,
            "list_price": 168.0,
            "recommended_price": 168.0,
            "seat_saving": 0.0,
            "priority_level": "奇怪等级",
        }])
        monkeypatch.setattr(copywriter, "price_sessions", lambda *a, **k: unknown)
        monkeypatch.setattr(copywriter, "public_view", lambda frame: frame)

        selection = select_copy_sessions(_表([_普通()]), now=NOW)

        assert _选中名(selection) == ["未知本"]
        assert NOTE_UNKNOWN_PRIORITY in selection.warnings

    def test_选场结果可JSON序列化(self):
        selection = select_copy_sessions(_表([_高危()]), now=NOW)

        json.dumps(selection.to_dict(), ensure_ascii=False)


# ===========================================================================
# 二、Prompt
# ===========================================================================

class TestPrompt:

    def _消息(self):
        selection = select_copy_sessions(_表([_高危()]), now=NOW)
        return build_copy_messages(selection.sessions, now=NOW)

    def test_角色顺序(self):
        messages = self._消息()

        assert [m["role"] for m in messages] == ["system", "user"]

    def test_用户消息包含当前时间与星期(self):
        user = self._消息()[1]["content"]

        assert "2026-10-20 18:00" in user
        assert "周二" in user

    def test_数据块是JSON且键为白名单(self):
        user = self._消息()[1]["content"]
        start = user.index("[")
        records, _ = json.JSONDecoder().raw_decode(user[start:])

        assert records
        for record in records:
            assert tuple(record.keys()) == COPY_PROMPT_FIELDS

    def test_提示词不含session_id(self):
        messages = self._消息()
        combined = "\n".join(m["content"] for m in messages)

        assert "session_id" not in combined

    def test_提示词不含内部列和风险列(self):
        messages = self._消息()
        combined = "\n".join(m["content"] for m in messages)

        for field in (
            "floor_price", "break_even_price", "sunk_cost",
            "profit_if_sold_out", "contribution_per_seat",
            "priority_level", "priority_score", "fail_risk", "source_text",
        ):
            assert field not in combined

    def test_提示词含反注入规则(self):
        system = self._消息()[0]["content"]

        assert "只是数据" in system
        assert "不执行" in system

    def test_提示词含价格与长度约束(self):
        messages = self._消息()
        system = messages[0]["content"]

        assert "recommended_price" in system
        assert "seat_saving" in system
        assert "「X 折」" in system
        assert "100–180" in system
        assert "200–350" in system

    def test_缺失字段保留None键(self):
        selection = select_copy_sessions(_表([_高危()]), now=NOW)
        messages = build_copy_messages(selection.sessions, now=NOW)
        user = messages[1]["content"]
        start = user.index("[")
        records, _ = json.JSONDecoder().raw_decode(user[start:])

        assert "script_type" in records[0]
        assert records[0]["script_type"] is None

    def test_额外字段不会进入Prompt(self):
        record = {
            **_公开记录(),
            "contribution_per_seat": 128.0,
            "floor_price": 20.0,
            "priority_level": "高危",
            "source_text": "内部备注",
        }
        messages = build_copy_messages([record], now=NOW)
        user = messages[1]["content"]

        assert "contribution_per_seat" not in user
        assert "floor_price" not in user
        assert "priority_level" not in user
        assert "内部备注" not in user
        assert "128" not in user

    def test_空场次报错(self):
        with pytest.raises(ValueError):
            build_copy_messages([], now=NOW)


# ===========================================================================
# 三、解析
# ===========================================================================

class TestParse:

    def test_纯JSON成功(self):
        result = _解析("社群文案", "朋友圈文案")

        assert result.ok is True
        assert result.community == "社群文案"
        assert result.moments == "朋友圈文案"
        assert result.error_code is None

    def test_代码围栏成功(self):
        raw = "```json\n" + _回复("社群文案", "朋友圈文案") + "\n```"
        result = parse_copy_response(raw, selected_sessions=[_公开记录()])

        assert result.ok is True

    def test_前后缀成功(self):
        raw = "这是回复：\n" + _回复("社群文案", "朋友圈文案") + "\n以上。"
        result = parse_copy_response(raw, selected_sessions=[_公开记录()])

        assert result.ok is True

    def test_坏JSON返回bad_json(self):
        result = parse_copy_response("{坏 JSON", selected_sessions=[_公开记录()])

        assert result.error_code == ERROR_BAD_JSON
        assert result.raw_snippet
        assert len(result.raw_snippet) <= 500

    def test_空回复返回bad_json(self):
        result = parse_copy_response("", selected_sessions=[_公开记录()])

        assert result.error_code == ERROR_BAD_JSON

    def test_顶层数组返回bad_root(self):
        raw = json.dumps([{"community": "a", "moments": "b"}], ensure_ascii=False)
        result = parse_copy_response(raw, selected_sessions=[_公开记录()])

        assert result.error_code == ERROR_BAD_ROOT

    def test_community缺失(self):
        raw = json.dumps({"moments": "朋友圈文案"}, ensure_ascii=False)
        result = parse_copy_response(raw, selected_sessions=[_公开记录()])

        assert result.error_code == ERROR_BAD_COPY
        assert "community" in result.error

    def test_moments缺失(self):
        raw = json.dumps({"community": "社群文案"}, ensure_ascii=False)
        result = parse_copy_response(raw, selected_sessions=[_公开记录()])

        assert result.error_code == ERROR_BAD_COPY
        assert "moments" in result.error

    def test_文案为空串(self):
        result = _解析("   ", "朋友圈文案")

        assert result.error_code == ERROR_BAD_COPY

    def test_文案不是字符串(self):
        result = _解析(123, "朋友圈文案")

        assert result.error_code == ERROR_BAD_COPY

    def test_换行统一为LF(self):
        result = _解析("第一行\r\n第二行", "第三行\r第四行")

        assert "\r" not in result.community
        assert "\r" not in result.moments
        assert result.community == "第一行\n第二行"

    def test_模型提示字符串转单元素(self):
        result = _解析("社群文案", "朋友圈文案", warnings="需要核对地址")

        assert result.warnings == ("模型提示：需要核对地址",)

    def test_模型提示数组过滤并限制十条(self):
        result = _解析(
            "社群文案", "朋友圈文案",
            warnings=[f"提示{i}" for i in range(12)] + [123],
        )

        assert len(result.warnings) == 10
        assert all(w.startswith("模型提示：") for w in result.warnings)

    def test_成功结果sessions走白名单(self):
        result = _解析(
            "社群文案", "朋友圈文案",
            contribution_per_seat=128.0, priority_level="高危",
            source_text="内部备注",
        )

        assert tuple(result.sessions[0].keys()) == COPY_FIELDS
        assert "contribution_per_seat" not in result.sessions[0]
        assert "内部备注" not in json.dumps(result.to_dict(), ensure_ascii=False)

    def test_to_dict可JSON序列化(self):
        result = _解析("社群文案", "朋友圈文案")

        json.dumps(result.to_dict(), ensure_ascii=False)

    def test_解析失败时sessions仍是公开字段(self):
        result = parse_copy_response("{坏", selected_sessions=[_公开记录()])

        assert tuple(result.sessions[0].keys()) == COPY_FIELDS

    def test_非结构错误不保留原文片段(self):
        assert _解析("", "朋友圈文案").raw_snippet is None
        assert _解析("这场保本价20元", "朋友圈文案").raw_snippet is None


# ===========================================================================
# 四、内部词与价格防护
# ===========================================================================

class TestLeakAndPrice:

    @pytest.mark.parametrize("term", INTERNAL_TERMS)
    def test_内部财务词整次失败(self, term):
        result = _解析(f"今晚这场 {term} 很低", "欢迎来玩")

        assert result.error_code == ERROR_INTERNAL_LEAK
        assert term in result.error
        assert result.community is None
        assert result.moments is None

    def test_英文列名大小写不敏感(self):
        result = _解析("FLOOR_PRICE=20", "欢迎来玩")

        assert result.error_code == ERROR_INTERNAL_LEAK

    def test_泄漏优先于价格(self):
        result = _解析("这场保本价 99 元", "欢迎来玩")

        assert result.error_code == ERROR_INTERNAL_LEAK
        assert result.error_code != ERROR_PRICE_MISMATCH

    @pytest.mark.parametrize("community", [
        "今晚年轮，原价168元，特价148元/位，立省20元，还剩4位。",
        "今晚年轮，¥148，还剩4位。",
        "今晚年轮，148/位，还剩4位。",
        "今晚年轮，148 一位，还剩4位。",
        "今晚年轮，原价168，现价148。",
    ])
    def test_合法价格写法通过(self, community):
        result = _解析(community, "年轮20:00开场，还剩4位。")

        assert result.ok is True

    @pytest.mark.parametrize("community", [
        "只要99元就能上车。",
        "只要 99 就能上车。",
        "今晚98元/位。",
    ])
    def test_未授权价格失败(self, community):
        result = _解析(community, "年轮20:00开场，还剩4位。")

        assert result.error_code == ERROR_PRICE_MISMATCH
        assert result.community is None
        assert result.moments is None

    def test_省下金额只能在省语境出现(self):
        assert _解析("今晚立省20元。", "年轮20:00开场。").ok is True
        assert _解析("今晚20元/位。", "年轮20:00开场。").error_code == ERROR_PRICE_MISMATCH

    @pytest.mark.parametrize("community", [
        "今晚打8折。",
        "今晚8 折。",
        "今晚8.5折。",
    ])
    def test_折扣表述失败(self, community):
        result = _解析(community, "年轮20:00开场，还剩4位。")

        assert result.error_code == ERROR_PRICE_MISMATCH

    def test_价格错误信息列出可用价格(self):
        result = _解析("只要99元。", "年轮20:00开场。")

        assert "99" in result.error
        assert "148" in result.error
        assert "168" in result.error

    def test_无价格数据时不提价格通过(self):
        result = _解析(
            "今晚年轮还有4位。", "年轮20:00开场，还剩4位。",
            list_price=None, recommended_price=None, seat_saving=None,
        )

        assert result.ok is True

    def test_无价格数据时提价格失败(self):
        result = _解析(
            "今晚年轮只要168元。", "年轮20:00开场。",
            list_price=None, recommended_price=None, seat_saving=None,
        )

        assert result.error_code == ERROR_PRICE_MISMATCH

    def test_剩余空位和时间数字不误判(self):
        result = _解析(
            "6人本还剩3位，20:00开场。", "年轮还剩3位，20:00开场。",
        )

        assert result.ok is True

    def test_失败时两版文案都为空(self):
        for community, moments in [
            ("这场保本价20元", "欢迎来玩"),
            ("只要99元", "欢迎来玩"),
            ("今晚打8折", "欢迎来玩"),
        ]:
            result = _解析(community, moments)
            assert result.ok is False
            assert result.community is None
            assert result.moments is None


# ===========================================================================
# 五、主入口
# ===========================================================================

class TestGenerate:

    def test_注入模型成功(self):
        model = FakeLLM(_回复("社群文案", "朋友圈文案"))
        result = generate_copy(_表([_高危()]), model=model, now=NOW)

        assert result.ok is True
        assert result.community == "社群文案"
        assert result.moments == "朋友圈文案"
        assert len(result.sessions) == 1
        assert len(model.calls) == 1

    def test_默认temperature为07(self, monkeypatch):
        calls = []
        model = FakeLLM(_回复("社群文案", "朋友圈文案"))

        def fake_get_model(*, temperature=0.0):
            calls.append(temperature)
            return model

        monkeypatch.setattr(copywriter.llm, "get_default_model", fake_get_model)

        generate_copy(_表([_高危()]), now=NOW)

        assert calls == [DEFAULT_TEMPERATURE]

    def test_显式temperature传递(self, monkeypatch):
        calls = []
        model = FakeLLM(_回复("社群文案", "朋友圈文案"))

        def fake_get_model(*, temperature=0.0):
            calls.append(temperature)
            return model

        monkeypatch.setattr(copywriter.llm, "get_default_model", fake_get_model)

        generate_copy(_表([_高危()]), now=NOW, temperature=0.2)

        assert calls == [0.2]

    def test_配置缺失返回config_error(self, monkeypatch):
        def fake_get_model(*, temperature=0.0):
            raise RuntimeError("未找到 DEEPSEEK_API_KEY。sk-secret123")

        monkeypatch.setattr(copywriter.llm, "get_default_model", fake_get_model)
        result = generate_copy(_表([_高危()]), now=NOW)

        assert result.error_code == ERROR_CONFIG
        assert result.community is None
        assert "sk-secret123" not in result.error
        assert "sk-***" in result.error

    def test_模型初始化失败返回model_init_error(self, monkeypatch):
        def fake_get_model(*, temperature=0.0):
            raise ValueError("init failed")

        monkeypatch.setattr(copywriter.llm, "get_default_model", fake_get_model)
        result = generate_copy(_表([_高危()]), now=NOW)

        assert result.error_code == ERROR_MODEL_INIT

    def test_模型调用异常脱敏(self):
        model = FakeLLM(error=RuntimeError("boom sk-abc12345"))
        result = generate_copy(_表([_高危()]), model=model, now=NOW)

        assert result.error_code == ERROR_LLM_CALL
        assert "sk-abc12345" not in result.error
        assert "sk-***" in result.error
        assert result.community is None

    def test_空表返回no_sessions(self):
        model = FakeLLM(_回复("社群文案", "朋友圈文案"))
        result = generate_copy(pd.DataFrame(), model=model, now=NOW)

        assert result.error_code == ERROR_NO_SESSIONS
        assert model.calls == []

    def test_全部满员返回no_sessions并保留warning(self):
        model = FakeLLM(_回复("社群文案", "朋友圈文案"))
        result = generate_copy(
            _表([_场(名="满员本", 已约=8)]), model=model, now=NOW,
        )

        assert result.error_code == ERROR_NO_SESSIONS
        assert NOTE_SKIP_FULL.format(count=1) in result.warnings
        assert model.calls == []

    def test_缺列返回bad_request(self):
        frame = pd.DataFrame([{"store_name": STORE}])
        result = generate_copy(frame, model=FakeLLM(), now=NOW)

        assert result.error_code == ERROR_BAD_REQUEST
        assert "缺少列" in result.error

    def test_非DataFrame返回bad_request(self):
        result = generate_copy([{"store_name": STORE}], model=FakeLLM(), now=NOW)

        assert result.error_code == ERROR_BAD_REQUEST

    def test_max_sessions越界返回bad_request(self):
        result = generate_copy(
            _表([_高危()]), model=FakeLLM(), now=NOW, max_sessions=6,
        )

        assert result.error_code == ERROR_BAD_REQUEST

    def test_多门店返回bad_request(self):
        result = generate_copy(
            _表([_普通(), _场(名="别家本", 门店=OTHER_STORE)]),
            model=FakeLLM(), now=NOW,
        )

        assert result.error_code == ERROR_BAD_REQUEST
        assert "多个门店" in result.error

    def test_非法now返回bad_request(self):
        result = generate_copy(
            _表([_高危()]), model=FakeLLM(), now="不是时间",
        )

        assert result.error_code == ERROR_BAD_REQUEST

    def test_store_name传递给选场(self):
        model = FakeLLM(_回复("社群文案", "朋友圈文案"))
        result = generate_copy(
            _表([_普通(), _场(名="别家本", 门店=OTHER_STORE)]),
            model=model, now=NOW, store_name=OTHER_STORE,
        )

        assert result.ok is True
        assert [s["script_name"] for s in result.sessions] == ["别家本"]

    def test_指定门店无可推广返回no_sessions(self):
        result = generate_copy(
            _表([_普通()]), model=FakeLLM(), now=NOW, store_name="不存在的店",
        )

        assert result.error_code == ERROR_NO_SESSIONS
        assert any("不存在的店" in w for w in result.warnings)

    def test_选场warning与模型提示合并(self):
        model = FakeLLM(_回复("社群文案", "朋友圈文案", warnings=["需要核对地址"]))
        result = generate_copy(
            _表([_场(名="满员本", 已约=8), _高危()]), model=model, now=NOW,
        )

        assert result.ok is True
        assert result.warnings[0] == NOTE_SKIP_FULL.format(count=1)
        assert "模型提示：需要核对地址" in result.warnings

    def test_KeyboardInterrupt原样抛出(self):
        model = FakeLLM(error=KeyboardInterrupt())
        with pytest.raises(KeyboardInterrupt):
            generate_copy(_表([_高危()]), model=model, now=NOW)

    def test_SystemExit原样抛出(self):
        model = FakeLLM(error=SystemExit())
        with pytest.raises(SystemExit):
            generate_copy(_表([_高危()]), model=model, now=NOW)

    def test_模型初始化KeyboardInterrupt原样抛出(self, monkeypatch):
        def fake_get_model(*, temperature=0.0):
            raise KeyboardInterrupt()

        monkeypatch.setattr(copywriter.llm, "get_default_model", fake_get_model)
        with pytest.raises(KeyboardInterrupt):
            generate_copy(_表([_高危()]), now=NOW)

    def test_空选场不调用模型(self):
        model = FakeLLM(_回复("社群文案", "朋友圈文案"))
        generate_copy(_表([_场(名="满员本", 已约=8)]), model=model, now=NOW)

        assert model.calls == []

    def test_结果sessions与选场一致(self):
        selection = select_copy_sessions(_表([_高危(), _关注()]), now=NOW)
        model = FakeLLM(_回复("社群文案", "朋友圈文案"))
        result = generate_copy(_表([_高危(), _关注()]), model=model, now=NOW)

        assert result.sessions == list(selection.sessions)

    def test_生成结果可JSON序列化(self):
        model = FakeLLM(_回复("社群文案", "朋友圈文案"))
        result = generate_copy(_表([_高危()]), model=model, now=NOW)

        json.dumps(result.to_dict(), ensure_ascii=False)


# ===========================================================================
# 六、契约回归
# ===========================================================================

class TestContract:

    def test_public_view仍保留contribution但工具白名单剔除(self):
        frame = _表([_关注()])
        public = public_view(price_sessions(frame, now=NOW))

        # 工具 5 的 public_view 只剔除 INTERNAL_COLUMNS，contribution_per_seat 还在。
        assert "contribution_per_seat" in public.columns

        selection = select_copy_sessions(frame, now=NOW)
        record = selection.sessions[0]

        assert "contribution_per_seat" not in record
        assert tuple(record.keys()) == COPY_FIELDS

    def test_内部值不进入用户Prompt(self):
        frame = _表([_关注()])
        priced = price_sessions(frame, now=NOW)
        row = priced.iloc[0]
        selection = select_copy_sessions(frame, now=NOW)
        messages = build_copy_messages(selection.sessions, now=NOW)
        user = messages[1]["content"]

        for internal in ("floor_price", "break_even_price", "sunk_cost",
                         "profit_if_sold_out", "contribution_per_seat"):
            assert internal not in user

        # 这些内部数值不应出现在数据块里。128 是 contribution_per_seat，
        # 340 是固定成本；公开价只有 168 和 148。
        # floor_price=20 不单独断言：日期 "2026-10-20" 本身就含 "20"。
        assert str(int(row["contribution_per_seat"])) not in user
        assert str(int(row["sunk_cost"])) not in user

    def test_prompt记录键为白名单(self):
        selection = select_copy_sessions(_表([_高危()]), now=NOW)
        messages = build_copy_messages(selection.sessions, now=NOW)
        user = messages[1]["content"]
        start = user.index("[")
        records, _ = json.JSONDecoder().raw_decode(user[start:])

        assert tuple(records[0].keys()) == COPY_PROMPT_FIELDS

    def test_选场顺序稳定(self):
        frame = _表([_普通(), _关注(), _高危()])

        first = select_copy_sessions(frame, now=NOW)
        second = select_copy_sessions(frame, now=NOW)

        assert _选中名(first) == _选中名(second)
        assert first.sessions == second.sessions

    def test_失败结果也保留公开sessions(self):
        model = FakeLLM(error=RuntimeError("boom"))
        result = generate_copy(_表([_高危()]), model=model, now=NOW)

        assert result.ok is False
        assert result.sessions
        assert tuple(result.sessions[0].keys()) == COPY_FIELDS

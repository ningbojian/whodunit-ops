"""chat_choices.build_choices 的分支覆盖测试（伪造 AgentTurn，不跑模型）。

场次草稿机制已删除：store_data 建场只有 quick_reply（追问 / 候选剧本）与
resolve（冲突选项）两类按钮，不再有 draft_select_script / draft_confirm。
"""

from __future__ import annotations

import json

from app.agent import AgentTurn
from app.web.chat_choices import build_choices


def _turn(*pairs: tuple[str, dict]) -> AgentTurn:
    """pairs: (tool_name, envelope_dict)；构造一一配对的 AgentTurn。"""
    calls = tuple({"name": name, "args": {}} for name, _ in pairs)
    results = tuple(
        {"name": name, "content": json.dumps(payload, ensure_ascii=False)}
        for name, payload in pairs
    )
    return AgentTurn(answer="好的", tool_calls=calls, tool_results=results)


def _envelope(data: dict, **extra) -> dict:
    """与 agent._envelope 相同的信封结构。"""
    payload = {
        "ok": True,
        "data": data,
        "warnings": [],
        "error_code": None,
        "error": None,
    }
    payload.update(extra)
    return payload


def _intake(status: str, **extra) -> dict:
    """store_data(action=create_session) 返回的 data（warnings 已上提到信封层）。"""
    data = {
        "ok": True,
        "status": status,
        "suggested_question": "",
        "ambiguities": [],
        "session": None,
        "auto_assign": None,
        **extra,
    }
    return data


def _needs_confirmation(candidates: list[dict], **extra) -> dict:
    return _envelope(
        _intake(
            "needs_confirmation",
            ambiguities=[{"field": "script_name", "candidates": candidates}],
            **extra,
        )
    )


class TestNeedsConfirmation:
    def test_候选取快捷回复按钮(self):
        envelope = _needs_confirmation(
            [
                {"script_name": "年轮", "script_id": "a"},
                {"script_name": "年轮（重制版）", "script_id": "b"},
            ]
        )
        choices = build_choices(_turn(("store_data", envelope)))
        assert len(choices) == 1
        group = choices[0]
        assert group["kind"] == "quick_reply"
        assert group["context"] == {}
        assert [b["label"] for b in group["buttons"]] == ["《年轮》", "《年轮（重制版）》"]
        assert all(b["action"] == "quick_reply" for b in group["buttons"])
        assert group["buttons"][0]["payload"] == {"text": "就开《年轮》"}

    def test_候选最多5个(self):
        candidates = [{"script_name": f"本{i}"} for i in range(8)]
        choices = build_choices(_turn(("store_data", _needs_confirmation(candidates))))
        assert len(choices[0]["buttons"]) == 5

    def test_候选带追问时不再追加追问按钮(self):
        # 候选已产出 quick_reply 组；suggested_question 不再重复成按钮。
        envelope = _needs_confirmation(
            [{"script_name": "年轮"}],
            suggested_question="是《年轮》还是《年轮（重制版）》？",
        )
        choices = build_choices(_turn(("store_data", envelope)))
        assert [g["kind"] for g in choices] == ["quick_reply"]
        assert [b["payload"] for b in choices[0]["buttons"]] == [
            {"text": "就开《年轮》"}
        ]

    def test_无候选取通用追问(self):
        # 候选为空（或字段缺失）时回落到 suggested_question 的纯文本追问。
        envelope = _envelope(
            _intake(
                "needs_confirmation",
                suggested_question="是《年轮》还是《年轮（重制版）》？",
                ambiguities=[],
            )
        )
        choices = build_choices(_turn(("store_data", envelope)))
        assert [g["kind"] for g in choices] == ["quick_reply"]
        assert choices[0]["buttons"][0]["payload"] == {
            "text": "是《年轮》还是《年轮（重制版）》？"
        }


class TestSuggestedQuestion:
    def test_通用追问按钮存活(self):
        question = "已经记下《雾隐山庄》，还差开场时间；这场定在几点？"
        envelope = _envelope(
            _intake(
                "incomplete",
                suggested_question=question,
                missing_required=["start_time"],
            )
        )
        choices = build_choices(_turn(("store_data", envelope)))
        assert len(choices) == 1
        group = choices[0]
        assert group["kind"] == "quick_reply"
        assert group["buttons"][0]["action"] == "quick_reply"
        assert group["buttons"][0]["payload"] == {"text": question}

    def test_无追问无冲突返回空(self):
        envelope = _envelope(_intake("promoted"))
        assert build_choices(_turn(("store_data", envelope))) == []


class TestPromotedConflicts:
    def test_嵌套auto_assign冲突(self):
        envelope = _envelope(
            _intake(
                "promoted",
                session={"session_id": "sess-1"},
                auto_assign={
                    "status": "conflict",
                    "conflicts": [
                        {
                            "session_id": "sess-1",
                            "options": [
                                {
                                    "type": "switch_room",
                                    "label": "可直接排到 2号房 / DM小明。",
                                    "target_id": "room-2",
                                },
                                {"type": "shift_time", "label": "改期"},
                                {
                                    "type": "switch_dm",
                                    "label": "换 DM小红",
                                    "target_id": "dm-3",
                                },
                                {
                                    "type": "keep_unassigned",
                                    "label": "先不排",
                                },
                            ],
                        }
                    ],
                },
            )
        )
        choices = build_choices(_turn(("store_data", envelope)))
        assert len(choices) == 1
        group = choices[0]
        assert group["kind"] == "resolve"
        # shift_time 只有叙述无按钮，其余三种可执行选项都出按钮。
        assert [b["payload"]["choice"] for b in group["buttons"]] == [
            "switch_room",
            "switch_dm",
            "keep_unassigned",
        ]
        assert group["buttons"][0]["payload"] == {
            "session_id": "sess-1",
            "choice": "switch_room",
            "target_id": "room-2",
        }
        assert group["context"] == {"session_id": "sess-1"}

    def test_无target_id时不含该键(self):
        envelope = _envelope(
            _intake(
                "promoted",
                auto_assign={
                    "conflicts": [
                        {
                            "session_id": "sess-1",
                            "options": [{"type": "keep_unassigned", "label": "先不排"}],
                        }
                    ]
                },
            )
        )
        choices = build_choices(_turn(("store_data", envelope)))
        assert "target_id" not in choices[0]["buttons"][0]["payload"]


class TestTopLevelConflicts:
    def test_schedule顶层conflicts(self):
        envelope = {
            "ok": True,
            "data": {
                "conflicts": [
                    {
                        "session_id": "sess-9",
                        "options": [
                            {
                                "type": "switch_dm",
                                "label": "换 DM小红",
                                "target_id": "dm-3",
                            },
                            {
                                "type": "cancel_or_waitlist",
                                "label": "取消或候补",
                            },
                        ],
                    }
                ]
            },
        }
        choices = build_choices(_turn(("schedule_sessions", envelope)))
        assert len(choices) == 1
        assert [b["payload"]["choice"] for b in choices[0]["buttons"]] == ["switch_dm"]


class TestNoDraftButtons:
    def test_按钮种类只有两类(self):
        turns = (
            ("store_data", _needs_confirmation([{"script_name": "年轮"}])),
            (
                "store_data",
                _envelope(
                    _intake("incomplete", suggested_question="还差开场时间；几点开场？")
                ),
            ),
            (
                "store_data",
                _envelope(
                    _intake(
                        "promoted",
                        auto_assign={
                            "conflicts": [
                                {
                                    "session_id": "sess-1",
                                    "options": [
                                        {"type": "switch_room", "label": "换房", "target_id": "r"},
                                        {"type": "keep_unassigned", "label": "不排"},
                                    ],
                                }
                            ]
                        },
                    )
                ),
            ),
            (
                "schedule_sessions",
                {
                    "ok": True,
                    "data": {
                        "conflicts": [
                            {
                                "session_id": "sess-2",
                                "options": [{"type": "switch_dm", "label": "换 DM", "target_id": "d"}],
                            }
                        ]
                    },
                },
            ),
        )
        choices = build_choices(_turn(*turns[-1:]))  # 逆序只看最后一个能命中的
        assert choices
        assert {g["kind"] for g in choices} <= {"quick_reply", "resolve"}
        assert {b["action"] for g in choices for b in g["buttons"]} <= {
            "quick_reply",
            "resolve",
        }
        # 逐个信封都不允许出现草稿类按钮。
        for pair in turns:
            for group in build_choices(_turn(pair)):
                assert group["kind"] in {"quick_reply", "resolve"}
                for button in group["buttons"]:
                    assert button["action"] in {"quick_reply", "resolve"}
                    assert "draft_id" not in button["payload"]


class TestBoundaries:
    def test_总按钮数不超过8(self):
        conflicts = [
            {
                "session_id": f"sess-{i}",
                "options": [
                    {"type": "switch_room", "label": f"换 {i}A", "target_id": "r"},
                    {"type": "switch_room", "label": f"换 {i}B", "target_id": "r2"},
                    {"type": "keep_unassigned", "label": "不排"},
                ],
            }
            for i in range(10)
        ]
        envelope = {"ok": True, "data": {"conflicts": conflicts}}
        choices = build_choices(_turn(("schedule_sessions", envelope)))
        total = sum(len(g["buttons"]) for g in choices)
        assert total <= 8

    def test_候选按钮计入总上限(self):
        candidates = [{"script_name": f"本{i}"} for i in range(8)]
        choices = build_choices(_turn(("store_data", _needs_confirmation(candidates))))
        total = sum(len(g["buttons"]) for g in choices)
        assert total <= 8
        assert total == 5  # 候选本身截到 5 个

    def test_label截断40字(self):
        long_label = "很" * 60
        envelope = _envelope(_intake("incomplete", suggested_question=long_label))
        choices = build_choices(_turn(("store_data", envelope)))
        label = choices[0]["buttons"][0]["label"]
        assert len(label) <= 40

    def test_坏JSON返回空(self):
        turn = AgentTurn(
            answer="",
            tool_calls=({"name": "store_data", "args": {}},),
            tool_results=({"name": "store_data", "content": "{broken"},),
        )
        assert build_choices(turn) == []

    def test_ok_false返回空(self):
        envelope = {"ok": False, "data": {"status": "incomplete"}}
        assert build_choices(_turn(("store_data", envelope))) == []

    def test_无工具调用返回空(self):
        assert build_choices(AgentTurn(answer="你好")) == []


class TestScanOrder:
    def test_逆序取第一个命中(self):
        first = _envelope(_intake("promoted"))  # 无按钮 → 被跳过
        second = _needs_confirmation([{"script_name": "年轮"}])
        choices = build_choices(_turn(("store_data", first), ("store_data", second)))
        # 逆序扫描：第二个（needs_confirmation 候选）先命中
        assert choices[0]["kind"] == "quick_reply"
        assert choices[0]["buttons"][0]["payload"] == {"text": "就开《年轮》"}

    def test_不匹配结果名跳过(self):
        turn = AgentTurn(
            answer="",
            tool_calls=({"name": "store_data", "args": {}},),
            tool_results=({"name": "extract_message", "content": json.dumps(
                _needs_confirmation([{"script_name": "年轮"}])
            )},),
        )
        # 结果名与调用名不一致 → 不配对 → 无按钮
        assert build_choices(turn) == []


class TestPayloadSafety:
    def test_payload_无泄漏额外字段(self):
        envelope = _needs_confirmation(
            [
                {
                    "script_name": "年轮",
                    "script_id": "secret-id",
                    "list_price": 168,
                    "内部财务": "不应出现",
                }
            ],
            source_text="原文不该出现",
            fields={"list_price": 168},
        )
        choices = build_choices(_turn(("store_data", envelope)))
        payload = choices[0]["buttons"][0]["payload"]
        assert set(payload) == {"text"}
        assert payload == {"text": "就开《年轮》"}
        dumped = json.dumps(choices, ensure_ascii=False)
        assert "原文不该出现" not in dumped
        assert "内部财务" not in dumped
        assert "secret-id" not in dumped

    def test_组结构固定键(self):
        envelope = _envelope(_intake("incomplete", suggested_question="几点开场？"))
        group = build_choices(_turn(("store_data", envelope)))[0]
        assert set(group) == {"kind", "buttons", "context"}
        assert set(group["buttons"][0]) == {"label", "action", "payload"}

"""增量场次录入：硬必填齐全当场建场，不齐只追问、不落任何数据（无草稿）。"""

from __future__ import annotations

import pandas as pd
import pytest

from app.store.engine import create_database_engine, ensure_schema
from app.store.excel import ExcelStore
from app.store.sql import SqlStore
from app.tools.scripts import upsert_script
from app.tools.session_intake import (
    SessionIntakeError,
    create_session_from_fields,
    update_session,
)
from app.tools.storage import load_sessions

NOW = pd.Timestamp("2026-10-20 12:00")
START = "2026-10-21 20:00"


@pytest.fixture(params=["excel", "sql"])
def store(request, tmp_path):
    if request.param == "excel":
        yield ExcelStore(
            sessions_path=tmp_path / "sessions.xlsx",
            bookings_path=tmp_path / "bookings.xlsx",
            scripts_path=tmp_path / "scripts.xlsx",
        )
        return
    engine = create_database_engine(sqlite_path=tmp_path / "store.sqlite3")
    ensure_schema(engine)
    value = SqlStore(engine, "test-intake")
    value.ensure_tenant()
    yield value
    engine.dispose()


def add_script(store, **overrides):
    payload = dict(
        script_name="雾隐山庄",
        aliases="WYSZ",
        script_type="推理",
        total_seats=6,
        duration_minutes=240,
        list_price=168,
        store=store,
        now=NOW,
    )
    payload.update(overrides)
    return upsert_script(**payload)


def _public_frame(*rows):
    from app.tools.public_library import coerce_public_scripts, make_public_script_id

    records = []
    for row in rows:
        records.append(
            {
                "script_id": make_public_script_id(row["script_name"]),
                "script_name": row["script_name"],
                "aliases": row.get("aliases", ""),
                "script_type": row.get("script_type"),
                "total_seats": row.get("total_seats"),
                "duration_minutes": row.get("duration_minutes"),
                "list_price": row.get("list_price"),
                "notes": "",
                "source_text": "",
                "created_at": NOW,
                "updated_at": NOW,
                "origin": "策展",
                "imported_at": NOW,
            }
        )
    return coerce_public_scripts(pd.DataFrame(records))


# ---------------------------------------------------------------------------
# 硬必填不齐：只追问、不落任何数据
# ---------------------------------------------------------------------------


def test_only_script_is_incomplete_and_persists_nothing(store):
    # 先有一条正常场次，确认 incomplete 调用不会改动已有数据。
    create_session_from_fields(
        fields={"script_name": "年轮", "start_time": START}, store=store, now=NOW
    )
    result = create_session_from_fields(
        fields={"script_name": "雾隐山庄"}, store=store, now=NOW
    )
    assert result["status"] == "incomplete"
    assert result["missing_required"] == ["start_time"]
    assert result["missing_recommended"]  # 人数/价格等软字段不影响
    assert "几点" in result["suggested_question"]
    assert result["session"] is None
    sessions = load_sessions(store=store)
    assert len(sessions) == 1  # 场次表未被追加
    assert set(sessions["script_name"]) == {"年轮"}


def test_only_time_asks_for_script(store):
    result = create_session_from_fields(
        fields={"start_time": START}, store=store, now=NOW
    )
    assert result["status"] == "incomplete"
    assert result["missing_required"] == ["script_name"]
    assert "哪个本" in result["suggested_question"]
    assert result["fields"]["start_time"] == "2026-10-21 20:00"
    assert load_sessions(store=store).empty


def test_unparsable_start_time_asks_again_and_persists_nothing(store):
    # 解析不了的开场时间保留原文，当作没给时间再问一轮；不建场。
    result = create_session_from_fields(
        fields={"script_name": "雾隐山庄", "start_time": "下周三晚上"},
        store=store,
        now=NOW,
    )
    assert result["status"] == "incomplete"
    assert "start_time" in result["missing_required"]
    assert result["fields"]["start_time"] is None
    assert result["fields"]["start_time_text"] == "下周三晚上"
    assert "几点" in result["suggested_question"]
    assert load_sessions(store=store).empty


def test_two_rounds_promote_without_draft_state(store):
    # 模拟 agent 的两轮对话：第一轮缺时间，第二轮带上已收集的完整字段。
    first = create_session_from_fields(
        fields={"script_name": "雾隐山庄"}, store=store, now=NOW
    )
    assert first["status"] == "incomplete"
    assert load_sessions(store=store).empty

    second = create_session_from_fields(
        fields={"script_name": "雾隐山庄", "start_time": START},
        store=store,
        now=NOW,
    )
    assert second["status"] == "promoted"
    assert second["missing_required"] == []
    assert second["session"]["script_name"] == "雾隐山庄"
    assert second["auto_assign"]["status"] == "resources_not_configured"
    assert second["auto_assign"]["trigger"] == "session_promoted"
    assert len(load_sessions(store=store)) == 1


def test_needs_confirmation_with_two_candidates_persists_nothing(store):
    # 一个缩写对上两个本才需要人确认。
    add_script(store)
    add_script(store, script_name="雾隐山庄前传", aliases="")
    result = create_session_from_fields(
        fields={"script_name": "雾隐", "start_time": START}, store=store, now=NOW
    )
    assert result["status"] == "needs_confirmation"
    assert result["script_match"] == "candidate"
    assert result["session"] is None
    assert result["ambiguities"][0]["field"] == "script_name"
    assert result["ambiguities"][0]["candidates"][0]["script_name"] == "雾隐山庄"
    assert "哪一个" in result["suggested_question"]
    assert load_sessions(store=store).empty


def test_default_store_is_filled(store):
    result = create_session_from_fields(
        fields={"script_name": "雾隐山庄", "start_time": START},
        default_store="测试门店",
        store=store,
        now=NOW,
    )
    assert result["session"]["store_name"] == "测试门店"


# ---------------------------------------------------------------------------
# 剧本匹配与元数据推断
# ---------------------------------------------------------------------------


def test_exact_match_infers_library_fields(store):
    add_script(store)
    result = create_session_from_fields(
        fields={"script_name": "雾隐山庄", "start_time": START}, store=store, now=NOW
    )
    assert result["status"] == "promoted"
    assert result["script_match"] == "exact"
    assert result["script_source"] == "私有"
    assert result["warnings"] == []
    assert result["missing_recommended"] == []
    assert set(result["inferred"]) >= {"script_type", "total_seats", "list_price"}
    assert result["fields"]["script_id"]  # 私有命中带 script_id
    session = result["session"]
    assert session["script_type"] == "推理"
    assert session["total_seats"] == 6
    assert session["list_price"] == 168
    assert session["script_name"] == "雾隐山庄"


def test_alias_is_canonicalized(store):
    add_script(store)
    result = create_session_from_fields(
        fields={"script_name": "WYSZ", "start_time": START}, store=store, now=NOW
    )
    assert result["status"] == "promoted"
    assert result["script_match"] == "alias"
    assert result["session"]["script_name"] == "雾隐山庄"


def test_unique_candidate_auto_confirms(store):
    """唯一候选快速通道（WP12）：一个缩写只对上一个本时免确认直接建场。"""
    add_script(store)
    result = create_session_from_fields(
        fields={"script_name": "雾隐", "start_time": START}, store=store, now=NOW
    )
    assert result["status"] == "promoted"
    assert result["script_match"] == "unique"
    assert result["session"]["script_name"] == "雾隐山庄"
    assert any("唯一匹配自动确认" in warning for warning in result["warnings"])
    assert result["ambiguities"] == []
    # 元数据照常从唯一候选借入。
    assert result["session"]["total_seats"] == 6
    assert result["session"]["script_type"] == "推理"
    assert result["auto_assign"]["trigger"] == "session_promoted"


def test_unknown_full_name_does_not_block_promotion(store):
    result = create_session_from_fields(
        fields={"script_name": "全新未建档剧本", "start_time": START},
        store=store,
        now=NOW,
    )
    assert result["status"] == "promoted"
    assert result["script_match"] == "none"
    assert result["session"]["script_name"] == "全新未建档剧本"
    assert result["missing_recommended"]  # 未建档时建议补人数/价格


def test_bad_numeric_fields_are_ignored(store):
    result = create_session_from_fields(
        fields={
            "script_name": "雾隐山庄",
            "start_time": START,
            "total_seats": "abc",
            "list_price": -5,
        },
        store=store,
        now=NOW,
    )
    assert result["status"] == "promoted"
    assert result["session"]["total_seats"] is None
    assert result["session"]["list_price"] is None


def test_two_complete_intakes_create_two_sessions(store):
    first = create_session_from_fields(
        fields={"script_name": "雾隐山庄", "start_time": START}, store=store, now=NOW
    )
    second = create_session_from_fields(
        fields={"script_name": "年轮", "start_time": START}, store=store, now=NOW
    )
    assert first["session"]["session_id"] != second["session"]["session_id"]
    assert len(load_sessions(store=store)) == 2


# ---------------------------------------------------------------------------
# 公共库接入：借元数据、script_id 永不指向公共行、私有遮蔽
# ---------------------------------------------------------------------------


def test_public_exact_match_borrows_metadata_with_no_script_id(store):
    public = _public_frame(
        {
            "script_name": "迷雾云使",
            "script_type": "情感",
            "total_seats": 6,
            "duration_minutes": 270,
            "list_price": 198,
        }
    )
    result = create_session_from_fields(
        fields={"script_name": "迷雾云使", "start_time": START},
        store=store,
        now=NOW,
        public=public,
    )
    assert result["status"] == "promoted"
    assert result["script_match"] == "exact"
    assert result["script_source"] == "公共库"
    assert "剧本信息来自公共库，请核对" in result["warnings"]
    assert result["fields"]["script_id"] is None  # 公共行不可被引用
    session = result["session"]
    assert session["script_name"] == "迷雾云使"
    assert session["script_type"] == "情感"
    assert session["list_price"] == 198


def test_public_unique_candidate_borrows_metadata(store):
    public = _public_frame(
        {
            "script_name": "迷雾云使",
            "script_type": "情感",
            "total_seats": 6,
            "duration_minutes": 270,
            "list_price": 198,
        }
    )
    result = create_session_from_fields(
        fields={"script_name": "云使", "start_time": START},
        store=store,
        now=NOW,
        public=public,
    )
    assert result["status"] == "promoted"
    assert result["script_match"] == "unique"
    assert result["script_source"] == "公共库"
    assert any("唯一匹配自动确认" in warning for warning in result["warnings"])
    assert any("公共库" in warning for warning in result["warnings"])
    assert result["fields"]["script_id"] is None
    assert result["session"]["script_name"] == "迷雾云使"
    assert result["session"]["script_type"] == "情感"


def test_single_character_title_cannot_hijack_a_long_misspelled_name(store):
    """公共库里有 49 个单字本名，长查询里含那个字就会被它们捞走。

    「唯一候选」会被当场提升成正式剧名，所以《病》会把一场
    「病娇男孩的精分曰记」静默改写成《病》——名字对不上、剧本信息全错，
    而且改完就看不出来了。
    """
    public = _public_frame(
        {"script_name": "病", "script_type": "恐怖", "total_seats": 6},
        {"script_name": "病娇男孩的精分日记", "script_type": "恐怖", "total_seats": 6},
    )
    result = create_session_from_fields(
        fields={"script_name": "病娇男孩的精分曰记", "start_time": START},
        store=store,
        now=NOW,
        public=public,
    )
    assert result["script_match"] == "none"
    assert result["session"]["script_name"] == "病娇男孩的精分曰记"


def test_private_shadows_public_same_name(store):
    add_script(store)  # 私有《雾隐山庄》推理/6人/168
    public = _public_frame(
        {
            "script_name": "雾隐山庄",
            "script_type": "情感",
            "total_seats": 7,
            "duration_minutes": 270,
            "list_price": 999,
        }
    )
    result = create_session_from_fields(
        fields={"script_name": "雾隐山庄", "start_time": START},
        store=store,
        now=NOW,
        public=public,
    )
    assert result["status"] == "promoted"
    assert result["script_source"] == "私有"
    assert result["warnings"] == []
    assert result["fields"]["script_id"]  # 私有行照常带 script_id
    session = result["session"]
    assert session["script_type"] == "推理"
    assert session["total_seats"] == 6
    assert session["list_price"] == 168


# ---------------------------------------------------------------------------
# 输入校验与落库细节
# ---------------------------------------------------------------------------


def test_room_and_dm_fields_are_rejected(store):
    with pytest.raises(SessionIntakeError) as excinfo:
        create_session_from_fields(
            fields={"script_name": "雾隐山庄", "room_id": "room-a"},
            store=store,
            now=NOW,
        )
    assert excinfo.value.code == "room_not_allowed"

    with pytest.raises(SessionIntakeError) as excinfo:
        create_session_from_fields(
            fields={"script_name": "雾隐山庄", "dm_id": "dm-a"},
            store=store,
            now=NOW,
        )
    assert excinfo.value.code == "room_not_allowed"


def test_empty_fields_are_rejected(store):
    with pytest.raises(SessionIntakeError) as excinfo:
        create_session_from_fields(fields={}, store=store, now=NOW)
    assert excinfo.value.code == "bad_request"


def test_source_text_redacts_phone_on_session(store):
    result = create_session_from_fields(
        fields={"script_name": "雾隐山庄", "start_time": START},
        source_text="联系电话 13800138000 " + "细" * 600,
        store=store,
        now=NOW,
    )
    assert result["status"] == "promoted"
    source = result["session"]["source_text"]
    assert "13800138000" not in source
    assert "[手机号]" in source
    assert len(source) <= 500
    stored = load_sessions(store=store)
    assert "[手机号]" in stored.iloc[0]["source_text"]


def test_update_session_still_works_after_promotion(store):
    session_id = create_session_from_fields(
        fields={"script_name": "雾隐山庄", "start_time": START, "total_seats": 6},
        store=store,
        now=NOW,
    )["session"]["session_id"]
    result = update_session(
        session_id,
        fields={"total_seats": 7, "list_price": 199},
        store=store,
        now=NOW,
    )
    assert result["session_id_changed"] is False
    assert result["session"]["total_seats"] == 7
    assert result["session"]["list_price"] == 199

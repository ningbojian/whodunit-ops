"""剧本库纯函数测试（全部离线，不读写真实 data/）。"""

from __future__ import annotations

import pandas as pd
import pytest

from app.tools.scripts import (
    PROFILE_COMPLETE,
    PROFILE_DRAFT,
    PROFILE_NAME_PENDING,
    find_scripts,
    get_script,
    list_scripts,
    load_scripts,
    make_script_id,
    missing_script_fields,
    script_to_dict,
    split_aliases,
    upsert_script,
)

NOW = pd.Timestamp("2026-10-20 18:00")


def _new(path):
    return upsert_script(
        script_name="年轮",
        aliases="NL / 年轮情感本",
        script_type="情感",
        total_seats="6人",
        duration_minutes="4小时",
        list_price="168元",
        path=path,
        now=NOW,
    )


def test_missing_file_returns_empty_frame(tmp_path):
    frame = load_scripts(tmp_path / "scripts.xlsx")
    assert frame.empty
    assert "script_name" in frame.columns


def test_upsert_partial_saves_draft_and_reports_missing_fields(tmp_path):
    path = tmp_path / "scripts.xlsx"

    result = upsert_script(aliases="NL", path=path, now=NOW)

    assert result["created"] is True
    assert result["profile_status"] == PROFILE_NAME_PENDING
    assert result["script_name"] == ""
    assert result["aliases"] == ["NL"]
    assert {"script_name", "script_type", "total_seats", "duration_minutes"} <= set(
        result["missing_fields"]
    )
    assert "还需要补" in result["next_question"]
    assert path.exists()
    assert len(load_scripts(path)) == 1


def test_draft_can_be_completed_round_by_round(tmp_path):
    path = tmp_path / "scripts.xlsx"
    draft = upsert_script(aliases="NL", path=path, now=NOW)

    second = upsert_script(
        script_id=draft["script_id"],
        script_name="年轮",
        script_type="情感",
        path=path,
        now=NOW,
    )
    assert second["script_id"] == draft["script_id"]
    assert second["profile_status"] == PROFILE_DRAFT
    assert "total_seats" in second["missing_fields"]

    third = upsert_script(
        script_id=draft["script_id"],
        total_seats="6人",
        duration_minutes="4小时",
        path=path,
        now=NOW,
    )
    assert third["profile_status"] == PROFILE_COMPLETE
    assert third["missing_fields"] == []
    assert third["aliases"] == ["NL"]
    assert third["script_name"] == "年轮"


def test_abbreviation_search_matches_alias_and_name(tmp_path):
    path = tmp_path / "scripts.xlsx"
    created = _new(path)

    by_alias = find_scripts("nl", path=path, sessions=pd.DataFrame())
    by_name = find_scripts("年轮", path=path, sessions=pd.DataFrame())

    assert [item["script_id"] for item in by_alias] == [created["script_id"]]
    assert by_alias[0]["source"] == "剧本库"
    assert [item["script_id"] for item in by_name] == [created["script_id"]]


def test_search_includes_session_history_without_writing_a_library_file(tmp_path):
    path = tmp_path / "scripts.xlsx"
    sessions = pd.DataFrame([
        {
            "script_name": "漓川怪谈簿",
            "script_type": "推理",
            "total_seats": 6,
            "list_price": 188,
        }
    ])

    results = find_scripts("漓川", path=path, sessions=sessions)

    assert len(results) == 1
    assert results[0]["script_name"] == "漓川怪谈簿"
    assert results[0]["source"] == "场次历史"
    assert not path.exists()


def test_session_history_fills_only_empty_fields_and_never_guesses_duration(tmp_path):
    path = tmp_path / "scripts.xlsx"
    sessions = pd.DataFrame([
        {
            "script_name": "年轮",
            "script_type": "情感",
            "total_seats": 8,
            "list_price": 168,
        }
    ])

    result = upsert_script(script_name="年轮", path=path, sessions=sessions, now=NOW)

    assert result["script_type"] == "情感"
    assert result["total_seats"] == 8
    assert result["list_price"] == 168
    assert result["duration_minutes"] is None
    assert "duration_minutes" in result["missing_fields"]
    assert set(result["used_session_history"]) == {"剧本类型", "适合人数", "参考原价"}
    assert any("场次历史" in item for item in result["warnings"])


def test_numeric_units_and_invalid_values(tmp_path):
    path = tmp_path / "scripts.xlsx"
    result = _new(path)
    assert (result["total_seats"], result["duration_minutes"], result["list_price"]) == (
        6,
        240,
        168,
    )

    bad = upsert_script(
        script_name="坏数据本",
        total_seats="很多人",
        duration_minutes="一会儿",
        list_price="很贵",
        path=path,
        now=NOW,
    )
    assert bad["invalid_fields"]
    assert set(bad["missing_fields"]) >= {
        "script_type",
        "total_seats",
        "duration_minutes",
    }
    assert "没识别" in bad["next_question"]


def test_duplicate_name_via_different_id_is_rejected(tmp_path):
    path = tmp_path / "scripts.xlsx"
    first = _new(path)
    second = upsert_script(
        script_name="漓川怪谈簿",
        script_type="推理",
        total_seats=6,
        duration_minutes=240,
        path=path,
        now=NOW,
    )
    assert second["script_id"] != first["script_id"]

    with pytest.raises(ValueError, match="避免创建重复档案"):
        upsert_script(
            script_id=first["script_id"],
            script_name="漓川怪谈簿",
            path=path,
            now=NOW,
        )


def test_name_correction_keeps_stable_id_and_aliases(tmp_path):
    path = tmp_path / "scripts.xlsx"
    created = _new(path)

    corrected = upsert_script(
        script_id=created["script_id"],
        script_name="年轮（重制版）",
        path=path,
        now=NOW,
    )

    assert corrected["script_id"] == created["script_id"]
    assert corrected["script_name"] == "年轮（重制版）"
    assert "NL" in corrected["aliases"]
    assert any("更新为" in item for item in corrected["warnings"])


def test_make_script_id_is_stable_for_whitespace_and_case():
    assert make_script_id(" 年轮 ") == make_script_id("年轮")
    assert make_script_id("NL") == make_script_id("nl")


def test_split_aliases_deduplicates_and_limits():
    aliases = split_aliases("NL、nl / 年轮 ; 情感本\nNL")
    assert aliases == ["NL", "年轮", "情感本"]


def test_get_and_list_scripts(tmp_path):
    path = tmp_path / "scripts.xlsx"
    created = _new(path)
    assert get_script(created["script_id"], path=path)["script_name"] == "年轮"
    assert get_script("年轮", path=path)["script_id"] == created["script_id"]
    assert get_script("不存在", path=path) is None

    upsert_script(aliases="WY", path=path, now=NOW)
    listed = list_scripts(path=path)
    assert len(listed) == 2
    assert {item["script_name"] for item in listed} == {"年轮", ""}


def test_missing_fields_does_not_treat_zero_as_present():
    record = {
        "script_name": "零人本",
        "script_type": "机制",
        "total_seats": 0,
        "duration_minutes": 0,
    }
    assert missing_script_fields(record) == ["total_seats", "duration_minutes"]
    assert script_to_dict(record)["profile_status"] == PROFILE_DRAFT

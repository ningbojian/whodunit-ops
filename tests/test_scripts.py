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


def _public_like_frame(rows: int = 5809) -> pd.DataFrame:
    """造一份与 data/public_scripts.xlsx 同规模的公共库帧。"""
    types = ["推理", "情感", "机制", "恐怖", "本格", "阵营"]
    names = [f"测试剧本{i:04d}" for i in range(rows)]
    return pd.DataFrame(
        {
            "script_id": [f"pub-{i:05d}" for i in range(rows)],
            "script_name": names,
            "aliases": [None] * rows,
            "script_type": [types[i % len(types)] for i in range(rows)],
            "total_seats": [5 + (i % 5) for i in range(rows)],
            "duration_minutes": [180 + (i % 3) * 60 for i in range(rows)],
            "list_price": [None] * rows,
            "notes": ["元数据占位"] * rows,
            "source_text": [None] * rows,
            "created_at": ["2026-01-01 00:00:00"] * rows,
            "updated_at": ["2026-01-01 00:00:00"] * rows,
        }
    )


def test_find_scripts_only_builds_profiles_for_returned_rows():
    """找本不能给全表每一行都建完整档案。

    ``find_scripts`` 原先对全部 5809 行调用 ``script_to_dict``（每行十几次
    字段归一化），实测 309ms，而最终最多只返回 limit（≤50）条。现在先按
    名字/别名打分，只给入选的行建档案。
    """
    frame = _public_like_frame()
    calls = {"count": 0}
    original = find_scripts.__globals__["script_to_dict"]

    def counting(record):
        calls["count"] += 1
        return original(record)

    find_scripts.__globals__["script_to_dict"] = counting
    try:
        results = find_scripts(
            "测试剧本0001", scripts=frame, sessions=pd.DataFrame(), limit=10
        )
    finally:
        find_scripts.__globals__["script_to_dict"] = original

    assert len(results) == 1
    assert results[0]["script_name"] == "测试剧本0001"
    # 只给命中的那一行建档案，不是 5809 行。
    assert calls["count"] <= 10, f"建了 {calls['count']} 次档案"


def test_find_scripts_empty_query_still_returns_first_limit_rows():
    """空查询命中全表，但只应给 limit 行建档案并返回。"""
    frame = _public_like_frame(rows=500)
    results = find_scripts("", scripts=frame, sessions=pd.DataFrame(), limit=10)
    assert len(results) == 10
    assert all(item["source"] == "公共库" for item in results)


def test_find_scripts_search_stays_fast_on_public_library_scale():
    """5.8k 行的搜索要保持在毫秒级，不能退回逐行建档案。

    原实现 309ms（精确名）/ 302ms（空查询）。这里给一个宽松上限，
    避免 CI 机器抖动导致误报，只为拦住数量级回退。
    """
    import time

    frame = _public_like_frame()
    find_scripts("测试剧本0001", scripts=frame, sessions=pd.DataFrame(), limit=10)
    best = None
    for _ in range(3):
        started = time.perf_counter()
        find_scripts("测试剧本0001", scripts=frame, sessions=pd.DataFrame(), limit=10)
        elapsed = time.perf_counter() - started
        best = elapsed if best is None else min(best, elapsed)
    assert best < 0.15, f"find_scripts 耗时 {best * 1000:.0f}ms，接近回退到 309ms"


# ---------------------------------------------------------------------------
# match_quality：让模型分得清「精确命中」和「碰巧撞上」
# ---------------------------------------------------------------------------


def _library(*names: str) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "script_id": [f"pub-{index}" for index in range(len(names))],
            "script_name": list(names),
            "aliases": [None] * len(names),
        }
    )


def test_match_quality_reports_match_strength():
    """四档正常匹配各自对应一个 match_quality，挨个钉住。"""
    cases = [
        ("年轮", ["年轮"], "exact", "年轮"),
        ("年轮", ["年轮3"], "prefix", "年轮3"),
        ("东君书院", ["金陵有座东君书院"], "partial", "金陵有座东君书院"),
        ("年轮完整版", ["年轮"], "loose", "年轮"),
    ]
    for query, names, quality, expected in cases:
        results = find_scripts(
            query, scripts=_library(*names), sessions=pd.DataFrame(), limit=5
        )
        assert results, query
        assert results[0]["script_name"] == expected, query
        assert results[0]["match_quality"] == quality, query


def test_empty_query_is_listed_not_mistaken_for_exact():
    """空查询的 score 恒为 0，绝不能据此标成 exact。"""
    results = find_scripts(
        "", scripts=_library("年轮", "青楼"), sessions=pd.DataFrame(), limit=5
    )
    assert len(results) == 2
    assert {item["match_quality"] for item in results} == {"list"}


def test_two_character_query_returns_nothing_rather_than_false_candidates():
    """短名 bigram 不可靠（青搂 / 青楼 零交集），宁可空手也不给假候选。"""
    results = find_scripts(
        "青搂", scripts=_library("青楼", "年轮"), sessions=pd.DataFrame(), limit=5
    )
    assert results == []


def test_typo_falls_back_to_a_labelled_suggestion():
    """一个字打错 → 走 bigram 兜底，并且明确标成 suggested（不是命中）。

    库里带上一个单字本名《病》是回归用例：它曾靠「名字 ⊂ 查询」抢走这一档，
    让真正的《病娇男孩的精分日记》永远进不了兜底。
    """
    results = find_scripts(
        "病娇男孩的精分曰记",
        scripts=_library("病", "病娇男孩的精分日记", "年轮"),
        sessions=pd.DataFrame(),
        limit=5,
    )
    assert results
    assert results[0]["script_name"] == "病娇男孩的精分日记"
    assert results[0]["match_quality"] == "suggested"


def test_suggestion_count_is_capped():
    """兜底最多给 3 条：再多模型也没法逐条找店长确认。"""
    names = [f"测试剧本第{i}号" for i in range(8)]
    results = find_scripts(
        "测试剧本弟0号", scripts=_library(*names), sessions=pd.DataFrame(), limit=10
    )
    assert len(results) <= 3
    assert all(item["match_quality"] == "suggested" for item in results)


def test_short_name_sharing_one_bigram_is_not_suggested():
    """只共用一个 bigram 的短名不能进候选列表。

    重叠系数的分母取 min 时，3 字名碰上一个 bigram 就能拿到 1/2 = 0.5，
    正好够到阈值，把「按字面相近猜的候选」变成一串误导性结果。所以另加
    了一条「至少共享 2 个 bigram」；《男孩们》和查询只有 1 个。

    《男孩们》刻意不是查询的子串——是子串的话会先被正常匹配捞走，
    测不到兜底这条路。
    """
    results = find_scripts(
        "病娇男孩的精分曰记",
        scripts=_library("病娇男孩的精分日记", "男孩们", "年轮"),
        sessions=pd.DataFrame(),
        limit=5,
    )
    assert [item["script_name"] for item in results] == ["病娇男孩的精分日记"]


def test_short_title_inside_a_long_query_does_not_suppress_the_fallback():
    """「名字 ⊂ 查询」只是弱匹配，不能因为它命中就不再给候选。

    库里有《男孩》时，「病娇男孩的精分曰记」会被它按 loose 捞走，于是
    兜底永不运行，真正的《病娇男孩的精分日记》连候选都进不了。现在只有
    loose 命中时会补一次兜底。
    """
    results = find_scripts(
        "病娇男孩的精分曰记",
        scripts=_library("男孩", "病娇男孩的精分日记", "年轮"),
        sessions=pd.DataFrame(),
        limit=5,
    )
    quality = {item["script_name"]: item["match_quality"] for item in results}
    assert quality == {
        "男孩": "loose",
        "病娇男孩的精分日记": "suggested",
    }


def test_fallback_is_skipped_when_any_real_match_exists():
    """有精确 / 前缀 / 包含命中时不跑兜底，避免把噪声混进确定的结果。"""
    results = find_scripts(
        "病娇男孩的精分日记",
        scripts=_library("男孩", "病娇男孩的精分日记", "年轮"),
        sessions=pd.DataFrame(),
        limit=5,
    )
    assert results[0]["match_quality"] == "exact"
    assert results[0]["script_name"] == "病娇男孩的精分日记"
    assert all(item["match_quality"] != "suggested" for item in results)


class _FakeLearned:
    """只实现 ``lookup`` 的全局记忆替身，顺手记下查了几次、查的是哪串。"""

    def __init__(self, mapping: dict[str, str] | None = None) -> None:
        self.mapping = dict(mapping or {})
        self.calls: list[str] = []

    def lookup(self, key: str) -> str | None:
        self.calls.append(key)
        return self.mapping.get(key)


def test_loose_rows_filling_the_limit_still_consult_the_learned_memory():
    """loose 行占满名额时也要兜底，且学到的命中排在 loose 前面。

    真实库里五个两字本（年轮 / 南海 / 青楼 / 病院 / 医院）能被一段长查询
    同时 loose 命中，正好占满 ``limit=5``。旧写法用 ``len(out) < limit``
    设门，这种「名额已满」的场合恰恰是唯一需要兜底的场合却一次都不查；
    即便查了，追加循环也会因为 ``len(out) >= limit`` 直接 break。
    """
    names = ("年轮", "南海", "青楼", "病院", "医院", "年轮奇缘")
    library = _library(*names)
    memory = _FakeLearned({"年轮南海青楼病院医院": "pub-5"})

    results = find_scripts(
        "年轮南海青楼病院医院",
        scripts=library,
        sessions=pd.DataFrame(),
        public=library,
        limit=5,
        learned_aliases=memory,
    )

    assert memory.calls == ["年轮南海青楼病院医院"]
    assert results[0]["script_name"] == "年轮奇缘"
    assert results[0]["match_quality"] == "learned"
    assert len(results) == 5


def test_learned_memory_wins_over_bigram_suggestions():
    """学到的是整串查询的精确键，比按字面猜的 bigram 候选更可信。

    两个都在 miss 路径上，但顺序不能反：反了以后「店长确认过的说法」
    会被一条猜测候选顶掉，而猜测恰恰是这一层最不可信的东西。
    """
    library = _library("年轮", "病娇男孩的精分日记")
    memory = _FakeLearned({"病娇男孩的精分曰记": "pub-0"})

    results = find_scripts(
        "病娇男孩的精分曰记",
        scripts=library,
        sessions=pd.DataFrame(),
        public=library,
        limit=5,
        learned_aliases=memory,
    )

    assert [item["match_quality"] for item in results] == ["learned"]
    assert results[0]["script_name"] == "年轮"


def test_session_history_rows_carry_match_quality_and_are_not_mutated():
    """场次历史条目也要带 match_quality，且不能原地改调用方的 dict。"""
    history = pd.DataFrame(
        [
            {
                "script_name": "漓川怪谈簿",
                "script_type": "推理",
                "total_seats": 6,
                "duration_minutes": 240,
                "store": "迷雾剧场",
                "start_time": "2026-10-25 19:00",
                "session_id": "sess-1",
            }
        ]
    )
    original = history.iloc[0].to_dict()
    results = find_scripts(
        "漓川怪谈簿",
        scripts=_library("年轮"),
        sessions=history,
        limit=5,
    )
    assert len(results) == 1
    assert results[0]["match_quality"] == "exact"
    assert "match_quality" not in original
    assert "match_quality" not in history.iloc[0].to_dict()



def test_loose_rows_do_not_crowd_the_suggestion_slots():
    """名字是查询子串的条目重叠系数恒为 1.0，排序上永远压过真正打错字的目标。

    候选若在「排除已出现条目」之前截断到 3 条，名额会被这三条 loose 占满，
    再去重时整批删掉，目标一条候选都拿不到。真实库上「山之神千灯烬爱自由
    病娇男孩的精分曰记」复现的就是这个：正确答案本来在候选池里排第四。
    """
    names = ("山之神", "千灯烬", "爱自由", "病娇男孩的精分日记", "年轮")
    results = find_scripts(
        "山之神千灯烬爱自由病娇男孩的精分曰记",
        scripts=_library(*names),
        sessions=pd.DataFrame(),
        limit=10,
    )
    quality = {item["script_name"]: item["match_quality"] for item in results}
    assert quality["病娇男孩的精分日记"] == "suggested"
    assert all(
        quality[name] == "loose" for name in ("山之神", "千灯烬", "爱自由")
    )

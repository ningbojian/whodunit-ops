"""剧本阶段模板：校验、排序、版本与运行快照。"""

from __future__ import annotations

import json

import pandas as pd
import pytest

from app.store.engine import create_database_engine, ensure_schema
from app.store.excel import ExcelStore
from app.store.sql import SqlStore
from app.tools.scripts import upsert_script
from app.tools.stages import (
    StageTemplateError,
    apply_sample_stages,
    duplicate_stages,
    get_stages,
    parse_hardware_scene,
    sample_stages,
    save_stages,
    snapshot_for_run,
    validate_stages,
)

NOW = pd.Timestamp("2026-10-20 12:00")


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
    value = SqlStore(engine, "test-stages")
    value.ensure_tenant()
    yield value
    engine.dispose()


def add_script(store, name="雾隐山庄", **overrides):
    payload = dict(
        script_name=name,
        aliases="WYSZ",
        script_type="推理",
        total_seats=6,
        duration_minutes=195,
        list_price=168,
        store=store,
        now=NOW,
    )
    payload.update(overrides)
    return upsert_script(**payload)


def stage(name="开场", **overrides):
    payload = {
        "name": name,
        "stage_type": "opening",
        "duration_minutes": 15,
        "warn_before_seconds": 60,
    }
    payload.update(overrides)
    return payload


def save_three(store, script_id):
    return save_stages(
        store,
        script_id=script_id,
        stages=[
            stage("开场", duration_minutes=15),
            stage("搜证", stage_type="search", duration_minutes=60),
            stage("复盘", stage_type="reveal", duration_minutes=30),
        ],
        now=NOW,
    )


# ---------------------------------------------------------------------------
# 保存与读取
# ---------------------------------------------------------------------------


def test_save_and_get_roundtrip(store):
    script_id = add_script(store)["script_id"]
    result = save_three(store, script_id)
    assert result["version"] == 1
    assert [item["seq"] for item in result["stages"]] == [1, 2, 3]

    view = get_stages(store, script_id=script_id)
    assert view["version"] == 1
    assert view["active_count"] == 3
    assert [item["name"] for item in view["stages"]] == ["开场", "搜证", "复盘"]
    assert view["stages"][1]["stage_type"] == "search"
    assert all(item["stage_id"] for item in view["stages"])
    assert view["script"]["script_name"] == "雾隐山庄"


def test_script_can_be_addressed_by_alias(store):
    add_script(store)
    save_stages(
        store, script_name="WYSZ", stages=[stage("开场")], now=NOW
    )
    view = get_stages(store, script_name="WYSZ")
    assert view["script"]["script_name"] == "雾隐山庄"
    assert len(view["stages"]) == 1


def test_unknown_script_is_rejected(store):
    with pytest.raises(StageTemplateError) as excinfo:
        save_stages(
            store, script_name="不存在的本", stages=[stage("开场")], now=NOW
        )
    assert excinfo.value.code == "script_not_found"

    add_script(store)
    with pytest.raises(StageTemplateError) as excinfo:
        get_stages(store, script_id="script-missing")
    assert excinfo.value.code == "script_not_found"


def test_version_increments_and_stage_id_is_stable(store):
    script_id = add_script(store)["script_id"]
    first = save_three(store, script_id)
    kept_id = first["stages"][0]["stage_id"]

    second = save_stages(
        store,
        script_id=script_id,
        stages=[
            stage("开场", stage_id=kept_id, duration_minutes=20),
            stage("搜证", duration_minutes=60),
            stage("复盘", duration_minutes=30),
        ],
        now=NOW,
    )
    assert second["version"] == 2
    assert second["stages"][0]["stage_id"] == kept_id
    view = get_stages(store, script_id=script_id)
    assert view["version"] == 2
    assert view["stages"][0]["duration_minutes"] == 20


def test_saving_one_script_does_not_touch_another(store):
    first_id = add_script(store, name="雾隐山庄")["script_id"]
    second_id = add_script(store, name="年轮", aliases="NL")["script_id"]
    save_three(store, first_id)
    save_stages(store, script_id=second_id, stages=[stage("开场")], now=NOW)

    save_three(store, first_id)
    assert get_stages(store, script_id=first_id)["version"] == 2
    other = get_stages(store, script_id=second_id)
    assert other["version"] == 1
    assert len(other["stages"]) == 1


def test_applying_sample_stages(store):
    script_id = add_script(store)["script_id"]
    result = apply_sample_stages(store, script_id=script_id, now=NOW)
    assert len(result["stages"]) == len(sample_stages()) == 5
    assert sum(item["duration_minutes"] for item in result["stages"]) == 195
    assert result["warnings"] == []

    # 示例模板共 195 分钟，300 分钟剧本的差距超过 20%。
    custom_id = add_script(store, name="年轮", aliases="NL", duration_minutes=300)[
        "script_id"
    ]
    other = apply_sample_stages(store, script_id=custom_id, now=NOW)
    assert any("相差超过 20%" in warning for warning in other["warnings"])


# ---------------------------------------------------------------------------
# 顺序校验
# ---------------------------------------------------------------------------


def test_seq_is_sorted_and_renumbered(store):
    script_id = add_script(store)["script_id"]
    result = save_stages(
        store,
        script_id=script_id,
        stages=[
            stage("复盘", seq=3),
            stage("开场", seq=1),
            stage("搜证", seq=2),
        ],
        now=NOW,
    )
    assert [item["name"] for item in result["stages"]] == ["开场", "搜证", "复盘"]
    assert [item["seq"] for item in result["stages"]] == [1, 2, 3]


def test_seq_must_be_contiguous(store):
    script_id = add_script(store)["script_id"]
    for stages in (
        [stage("开场", seq=1), stage("搜证", seq=3)],
        [stage("开场", seq=1), stage("搜证", seq=1)],
        [stage("开场", seq=1), stage("搜证", seq=2), stage("复盘")],
    ):
        with pytest.raises(StageTemplateError) as excinfo:
            save_stages(store, script_id=script_id, stages=stages, now=NOW)
        assert excinfo.value.code == "invalid_stage_sequence"
    assert store.load_script_stages().empty


def test_seq_without_values_is_renumbered(store):
    script_id = add_script(store)["script_id"]
    result = save_stages(
        store,
        script_id=script_id,
        stages=[stage("开场"), stage("搜证"), stage("复盘")],
        now=NOW,
    )
    assert [item["seq"] for item in result["stages"]] == [1, 2, 3]


# ---------------------------------------------------------------------------
# 字段边界
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        stage(""),
        stage("名" * 61),
        stage("开场", duration_minutes=0),
        stage("开场", duration_minutes=241),
        stage("开场", duration_minutes=15, warn_before_seconds=-1),
        stage("开场", duration_minutes=15, warn_before_seconds=901),
        stage("开场", stage_type="unknown"),
        stage("开场", cue_text="字" * 2001),
        stage("开场", prompt_text="字" * 201),
        stage("开场", notes="字" * 501),
    ],
)
def test_invalid_stage_values_are_rejected(store, payload):
    script_id = add_script(store)["script_id"]
    with pytest.raises(StageTemplateError) as excinfo:
        save_stages(store, script_id=script_id, stages=[payload], now=NOW)
    assert excinfo.value.code == "invalid_stage_value"
    assert store.load_script_stages().empty


def test_empty_template_is_rejected(store):
    script_id = add_script(store)["script_id"]
    with pytest.raises(StageTemplateError) as excinfo:
        save_stages(store, script_id=script_id, stages=[], now=NOW)
    assert excinfo.value.code == "empty_template"


def test_warn_defaults_and_boundary_accepted(store):
    script_id = add_script(store)["script_id"]
    no_warn = stage("开场", warn_before_seconds=None)
    no_warn.pop("warn_before_seconds")
    at_limit = stage("搜证", duration_minutes=10, warn_before_seconds=600)
    result = save_stages(
        store, script_id=script_id, stages=[no_warn, at_limit], now=NOW
    )
    assert result["stages"][0]["warn_before_seconds"] == 60
    assert result["stages"][1]["warn_before_seconds"] == 600


def test_duplicate_stage_id_is_rejected(store):
    script_id = add_script(store)["script_id"]
    with pytest.raises(StageTemplateError) as excinfo:
        save_stages(
            store,
            script_id=script_id,
            stages=[stage("开场", stage_id="dup-1"), stage("搜证", stage_id="dup-1")],
            now=NOW,
        )
    assert excinfo.value.code == "invalid_stage_value"


# ---------------------------------------------------------------------------
# 硬件场景
# ---------------------------------------------------------------------------


def test_hardware_scene_is_normalized(store):
    script_id = add_script(store)["script_id"]
    result = save_stages(
        store,
        script_id=script_id,
        stages=[
            stage(
                "开场",
                hardware_scene='{"light":"warm_low","sound":"intro_01","volume":40}',
            )
        ],
        now=NOW,
    )
    view = get_stages(store, script_id=script_id)
    assert view["stages"][0]["hardware_scene"] == {
        "light": "warm_low",
        "sound": "intro_01",
        "volume": 40,
    }
    assert result["stages"][0]["hardware_scene"]["volume"] == 40


@pytest.mark.parametrize(
    "value",
    [
        "not json",
        "[1,2,3]",
        '{"a":{"b":{"c":{"d":{"e":1}}}}}',
        json.dumps({"light": "x" * 3000}),
        '{"bad": {"nested": {"too": {"deep": {"value": true}}}}}',
    ],
)
def test_invalid_hardware_scene_rejected(store, value):
    script_id = add_script(store)["script_id"]
    with pytest.raises(StageTemplateError) as excinfo:
        save_stages(
            store, script_id=script_id, stages=[stage("开场", hardware_scene=value)], now=NOW
        )
    assert excinfo.value.code == "invalid_hardware_scene"
    assert store.load_script_stages().empty


def test_parse_hardware_scene_accepts_object_and_blank():
    assert parse_hardware_scene(None) is None
    assert parse_hardware_scene("") is None
    assert parse_hardware_scene({"light": "warm"}) == {"light": "warm"}
    assert parse_hardware_scene('{"a":1}') == {"a": 1}


# ---------------------------------------------------------------------------
# 停用与快照
# ---------------------------------------------------------------------------


def test_inactive_stage_is_kept_but_excluded_from_snapshot(store):
    script_id = add_script(store)["script_id"]
    save_stages(
        store,
        script_id=script_id,
        stages=[
            stage("开场", active=False),
            stage("搜证", stage_id="stage-b", duration_minutes=60),
            stage("复盘", stage_id="stage-c", duration_minutes=30),
        ],
        now=NOW,
    )
    full = get_stages(store, script_id=script_id)
    assert len(full["stages"]) == 3
    assert full["active_count"] == 2

    active_only = get_stages(store, script_id=script_id, include_inactive=False)
    assert [item["name"] for item in active_only["stages"]] == ["搜证", "复盘"]

    snapshot = snapshot_for_run(full["stages"], version=full["version"])
    assert snapshot["version"] == 1
    assert snapshot["script_id"] == script_id
    assert [item["name"] for item in snapshot["stages"]] == ["搜证", "复盘"]
    assert [item["seq"] for item in snapshot["stages"]] == [1, 2]


def test_all_inactive_returns_warning(store):
    script_id = add_script(store)["script_id"]
    result = save_stages(
        store, script_id=script_id, stages=[stage("开场", active=False)], now=NOW
    )
    assert any("没有启用中的阶段" in warning for warning in result["warnings"])


def test_snapshot_is_detached_from_later_edits(store):
    script_id = add_script(store)["script_id"]
    save_three(store, script_id)
    before = snapshot_for_run(get_stages(store, script_id=script_id)["stages"], version=1)

    save_stages(
        store,
        script_id=script_id,
        stages=[stage("全新开场", duration_minutes=20)],
        now=NOW,
    )
    after = snapshot_for_run(get_stages(store, script_id=script_id)["stages"], version=2)

    assert [item["name"] for item in before["stages"]] == ["开场", "搜证", "复盘"]
    assert before["version"] == 1
    assert [item["name"] for item in after["stages"]] == ["全新开场"]
    assert after["version"] == 2


def test_snapshot_can_take_dataframe_and_skips_inactive(store):
    script_id = add_script(store)["script_id"]
    save_three(store, script_id)
    frame = store.load_script_stages()
    frame.loc[frame["name"] == "搜证", "active"] = False
    store.save_script_stages(frame)

    snapshot = snapshot_for_run(store.load_script_stages(), version=1)
    assert [item["name"] for item in snapshot["stages"]] == ["开场", "复盘"]
    assert [item["seq"] for item in snapshot["stages"]] == [1, 2]


# ---------------------------------------------------------------------------
# 复制与校验函数
# ---------------------------------------------------------------------------


def test_duplicate_only_copies_active_stages(store):
    source_id = add_script(store, name="雾隐山庄")["script_id"]
    target_id = add_script(store, name="年轮", aliases="NL")["script_id"]
    save_stages(
        store,
        script_id=source_id,
        stages=[
            stage("开场", active=False),
            stage("搜证", stage_id="source-2", duration_minutes=60),
            stage("复盘", stage_id="source-3", duration_minutes=30),
        ],
        now=NOW,
    )

    result = duplicate_stages(
        store, source_script_id=source_id, target_script_id=target_id, now=NOW
    )
    assert result["script_id"] == target_id
    assert result["version"] == 1
    assert [item["name"] for item in result["stages"]] == ["搜证", "复盘"]
    assert [item["seq"] for item in result["stages"]] == [1, 2]
    # 复制会生成新的 stage_id，不与被复制模板共享行。
    assert result["stages"][0]["stage_id"] != "source-2"

    again = duplicate_stages(
        store,
        source_script_id=source_id,
        target_script_id=target_id,
        overwrite=True,
        now=NOW,
    )
    assert again["version"] == 2


def test_duplicate_rejects_existing_target_without_overwrite(store):
    source_id = add_script(store, name="雾隐山庄")["script_id"]
    target_id = add_script(store, name="年轮", aliases="NL")["script_id"]
    save_three(store, source_id)
    save_stages(store, script_id=target_id, stages=[stage("开场")], now=NOW)

    with pytest.raises(StageTemplateError) as excinfo:
        duplicate_stages(
            store, source_script_id=source_id, target_script_id=target_id, now=NOW
        )
    assert excinfo.value.code == "template_exists"


def test_duplicate_same_script_is_rejected(store):
    script_id = add_script(store)["script_id"]
    save_three(store, script_id)
    with pytest.raises(StageTemplateError):
        duplicate_stages(
            store, source_script_id=script_id, target_script_id=script_id, now=NOW
        )


def test_duplicate_source_without_stages(store):
    source_id = add_script(store, name="雾隐山庄")["script_id"]
    target_id = add_script(store, name="年轮", aliases="NL")["script_id"]
    with pytest.raises(StageTemplateError) as excinfo:
        duplicate_stages(
            store, source_script_id=source_id, target_script_id=target_id, now=NOW
        )
    assert excinfo.value.code == "template_missing"


def test_validate_stages_total_duration_warning():
    script = {"duration_minutes": 120}
    cleaned, warnings = validate_stages(
        [stage("开场", duration_minutes=15), stage("收官", duration_minutes=15)],
        script=script,
    )
    assert len(cleaned) == 2
    assert any("相差超过 20%" in warning for warning in warnings)


def test_validate_stages_requires_objects():
    with pytest.raises(StageTemplateError):
        validate_stages(["开场"])


def test_validate_sample_stages_is_clean():
    cleaned, warnings = validate_stages(sample_stages(), script={"duration_minutes": 195})
    assert len(cleaned) == 5
    assert warnings == []

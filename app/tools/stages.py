"""剧本阶段模板：校验、整表保存、版本递增、运行快照和复制。

模板只属于已在剧本库中的剧本。DM 端运行时永远读快照，不读正在编辑的表。
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any, Iterable

import pandas as pd

from app.tools.frame import coerce_frame, json_safe
from app.tools.frame_specs import SCRIPT_STAGES_COLUMNS, SCRIPT_STAGES_SPEC
from app.tools.scripts import load_scripts, normalize_script_name, split_aliases
from app.tools.storage import is_missing_value, store_lock

STAGE_TYPES = {
    "opening",
    "search",
    "discussion",
    "vote",
    "reveal",
    "ending",
    "custom",
}
MAX_SCENE_BYTES = 2048
MAX_SCENE_DEPTH = 4


class StageTemplateError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _now(now: Any = None) -> pd.Timestamp:
    if now is None:
        return pd.Timestamp.now().floor("s")
    return pd.Timestamp(now).floor("s")


def _text(value: Any) -> str:
    if is_missing_value(value):
        return ""
    return str(value).strip()


def _bool(value: Any, default: bool = True) -> bool:
    if is_missing_value(value):
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().casefold() in {"1", "true", "yes", "y", "是", "启用", "有效"}


def _int(value: Any, field: str) -> int:
    if is_missing_value(value):
        raise StageTemplateError("invalid_stage_value", f"{field} 不能为空。")
    try:
        return int(float(value))
    except (TypeError, ValueError):
        raise StageTemplateError("invalid_stage_value", f"{field} 必须是整数。") from None


def _int_or_zero(value: Any) -> int:
    if is_missing_value(value):
        return 0
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def _script_row(
    store: Any,
    *,
    script_id: str | None = None,
    script_name: str | None = None,
) -> dict[str, Any]:
    scripts = load_scripts(store=store)
    if scripts.empty:
        raise StageTemplateError("script_not_found", "剧本库里还没有这个剧本。")
    target_id = _text(script_id)
    if target_id:
        hit = scripts[scripts["script_id"].astype("string").fillna("") == target_id]
        if hit.empty:
            raise StageTemplateError("script_not_found", "剧本不存在。")
        return hit.iloc[0].to_dict()

    key = normalize_script_name(_text(script_name)).casefold()
    if not key:
        raise StageTemplateError("script_not_found", "缺少 script_id 或剧本全名。")
    for _, row in scripts.iterrows():
        if normalize_script_name(row.get("script_name")).casefold() == key:
            return row.to_dict()
    for _, row in scripts.iterrows():
        if any(
            normalize_script_name(alias).casefold() == key
            for alias in split_aliases(row.get("aliases"))
        ):
            return row.to_dict()
    raise StageTemplateError("script_not_found", "剧本库里没有匹配的剧本。")


def _scene_depth(value: Any, level: int = 0) -> None:
    if level > MAX_SCENE_DEPTH:
        raise StageTemplateError("invalid_hardware_scene", "硬件场景嵌套不能超过 4 层。")
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise StageTemplateError("invalid_hardware_scene", "硬件场景键名必须是字符串。")
            _scene_depth(item, level + 1)
    elif isinstance(value, list):
        for item in value:
            _scene_depth(item, level + 1)
    elif isinstance(value, (str, int, float, bool)) or value is None:
        return
    else:
        raise StageTemplateError("invalid_hardware_scene", "硬件场景含不支持的值类型。")


def parse_hardware_scene(value: Any) -> dict[str, Any] | None:
    """把模板里的硬件场景规范成 JSON 对象；空白返回 None。"""
    if is_missing_value(value):
        return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            value = json.loads(text)
        except (TypeError, ValueError):
            raise StageTemplateError(
                "invalid_hardware_scene", "硬件场景必须是合法 JSON。"
            ) from None
    if not isinstance(value, dict):
        raise StageTemplateError("invalid_hardware_scene", "硬件场景必须是 JSON 对象。")
    _scene_depth(value)
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > MAX_SCENE_BYTES:
        raise StageTemplateError("invalid_hardware_scene", "硬件场景不能超过 2KB。")
    return value


def _clean_stage(raw: dict[str, Any], seq: int) -> dict[str, Any]:
    name = _text(raw.get("name"))
    if not name:
        raise StageTemplateError("invalid_stage_value", "阶段名不能为空。")
    if len(name) > 60:
        raise StageTemplateError("invalid_stage_value", "阶段名不能超过 60 字。")

    duration = _int(raw.get("duration_minutes"), "duration_minutes")
    if duration < 1 or duration > 240:
        raise StageTemplateError("invalid_stage_value", "阶段时长必须在 1..240 分钟。")

    warn = _int(
        raw.get("warn_before_seconds", 60)
        if not is_missing_value(raw.get("warn_before_seconds"))
        else 60,
        "warn_before_seconds",
    )
    if warn < 0 or warn > duration * 60:
        raise StageTemplateError(
            "invalid_stage_value", "提前提醒秒数必须在 0..阶段时长之间。"
        )

    stage_type = _text(raw.get("stage_type")).lower() or None
    if stage_type is not None and stage_type not in STAGE_TYPES:
        raise StageTemplateError("invalid_stage_value", "阶段类型不合法。")

    cue = _text(raw.get("cue_text")) or None
    prompt = _text(raw.get("prompt_text")) or None
    notes = _text(raw.get("notes")) or None
    if cue is not None and len(cue) > 2000:
        raise StageTemplateError("invalid_stage_value", "cue_text 不能超过 2000 字。")
    if prompt is not None and len(prompt) > 200:
        raise StageTemplateError("invalid_stage_value", "prompt_text 不能超过 200 字。")
    if notes is not None and len(notes) > 500:
        raise StageTemplateError("invalid_stage_value", "notes 不能超过 500 字。")

    scene = parse_hardware_scene(raw.get("hardware_scene"))
    return {
        "stage_id": _text(raw.get("stage_id")) or None,
        "seq": seq,
        "name": name,
        "stage_type": stage_type,
        "duration_minutes": duration,
        "warn_before_seconds": warn,
        "cue_text": cue,
        "prompt_text": prompt,
        "hardware_scene": (
            json.dumps(scene, ensure_ascii=False, separators=(",", ":"))
            if scene is not None
            else None
        ),
        "notes": notes,
        "active": _bool(raw.get("active"), True),
    }


def validate_stages(
    stages: Iterable[dict[str, Any]],
    *,
    script: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """校验一整张模板，返回规范后的阶段和 warning。"""
    items = list(stages or [])
    if not items:
        raise StageTemplateError("empty_template", "至少需要 1 个阶段。")
    if len(items) > 60:
        raise StageTemplateError("invalid_stage_sequence", "阶段数量不能超过 60。")

    provided = [_text(item.get("seq")) for item in items if isinstance(item, dict)]
    if any(provided) and not all(provided):
        raise StageTemplateError(
            "invalid_stage_sequence", "seq 要么全部提供，要么全部不提供。"
        )
    if all(provided):
        seqs = []
        for literal in provided:
            try:
                seqs.append(int(literal))
            except ValueError:
                raise StageTemplateError(
                    "invalid_stage_sequence", "seq 必须是整数。"
                ) from None
        if sorted(seqs) != list(range(1, len(items) + 1)):
            raise StageTemplateError(
                "invalid_stage_sequence", "seq 必须从 1 连续到 N，不能重号或跳号。"
            )
        ordered = sorted(
            [item for item in items if isinstance(item, dict)],
            key=lambda item: int(_text(item.get("seq"))),
        )
    else:
        ordered = [item for item in items if isinstance(item, dict)]
        if len(ordered) != len(items):
            raise StageTemplateError("invalid_stage_value", "阶段必须是对象。")

    cleaned: list[dict[str, Any]] = []
    ids: set[str] = set()
    for index, item in enumerate(ordered, start=1):
        stage = _clean_stage(item, index)
        stage_id = stage["stage_id"]
        if stage_id:
            if stage_id in ids:
                raise StageTemplateError("invalid_stage_value", "stage_id 重复。")
            ids.add(stage_id)
        cleaned.append(stage)

    warnings: list[str] = []
    if not any(stage["active"] for stage in cleaned):
        warnings.append("模板里没有启用中的阶段，创建场控前需要至少启用 1 个。")
    if script is not None:
        planned = sum(stage["duration_minutes"] for stage in cleaned if stage["active"])
        stored = script.get("duration_minutes")
        if not is_missing_value(stored):
            try:
                expected = int(stored)
            except (TypeError, ValueError):
                expected = 0
            if expected > 0 and planned > 0:
                if abs(planned - expected) / expected > 0.2:
                    warnings.append(
                        f"阶段总时长 {planned} 分钟与剧本档案 {expected} 分钟相差超过 20%。"
                    )
    return cleaned, warnings


def next_version(stages: pd.DataFrame, script_id: str) -> int:
    if stages.empty:
        return 1
    hit = stages[stages["script_id"].astype("string").fillna("") == _text(script_id)]
    if hit.empty:
        return 1
    versions = pd.to_numeric(hit["version"], errors="coerce").dropna()
    return int(versions.max()) + 1 if not versions.empty else 1


def _load_all(store: Any) -> pd.DataFrame:
    frame = store.load_script_stages()
    return coerce_frame(frame, SCRIPT_STAGES_SPEC)


def _stage_payload(row: dict[str, Any]) -> dict[str, Any]:
    scene = parse_hardware_scene(row.get("hardware_scene"))
    return {
        "stage_id": json_safe(row.get("stage_id")),
        "script_id": json_safe(row.get("script_id")),
        "script_name": json_safe(row.get("script_name")),
        "seq": json_safe(row.get("seq")),
        "name": json_safe(row.get("name")),
        "stage_type": json_safe(row.get("stage_type")),
        "duration_minutes": json_safe(row.get("duration_minutes")),
        "warn_before_seconds": json_safe(row.get("warn_before_seconds")),
        "cue_text": json_safe(row.get("cue_text")),
        "prompt_text": json_safe(row.get("prompt_text")),
        "hardware_scene": scene,
        "notes": json_safe(row.get("notes")),
        "active": bool(row.get("active")) if not is_missing_value(row.get("active")) else False,
        "version": json_safe(row.get("version")),
        "created_at": json_safe(row.get("created_at")),
        "updated_at": json_safe(row.get("updated_at")),
    }


def get_stages(
    store: Any,
    *,
    script_id: str | None = None,
    script_name: str | None = None,
    include_inactive: bool = True,
) -> dict[str, Any]:
    script = _script_row(store, script_id=script_id, script_name=script_name)
    sid = _text(script.get("script_id"))
    frame = _load_all(store)
    if not frame.empty:
        hit = frame[frame["script_id"].astype("string").fillna("") == sid]
        hit = hit.sort_values("seq")
        if not include_inactive:
            hit = hit[hit["active"].fillna(False)]
    else:
        hit = frame
    rows = [_stage_payload(row) for row in hit.to_dict("records")] if not hit.empty else []
    version = max((row["version"] or 0 for row in rows), default=0)
    return {
        "script": {
            "script_id": sid,
            "script_name": json_safe(script.get("script_name")),
            "script_type": json_safe(script.get("script_type")),
            "duration_minutes": json_safe(script.get("duration_minutes")),
        },
        "version": version,
        "stages": rows,
        "active_count": sum(1 for row in rows if row["active"]),
    }


def _save_stages_impl(
    store: Any,
    *,
    script: dict[str, Any],
    stages: Iterable[dict[str, Any]],
    now: Any = None,
) -> dict[str, Any]:
    moment = _now(now)
    sid = _text(script.get("script_id"))
    cleaned, warnings = validate_stages(stages, script=script)
    all_stages = _load_all(store)
    version = next_version(all_stages, sid)
    existing = {}
    if not all_stages.empty:
        for row in all_stages.to_dict("records"):
            if _text(row.get("script_id")) == sid:
                existing[_text(row.get("stage_id"))] = row

    records: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for index, stage in enumerate(cleaned, start=1):
        stage_id = stage["stage_id"] or f"stage-{uuid.uuid4().hex[:12]}"
        if stage_id in seen_ids:
            raise StageTemplateError("invalid_stage_value", "stage_id 重复。")
        seen_ids.add(stage_id)
        previous = existing.get(stage_id, {})
        records.append(
            {
                **stage,
                "stage_id": stage_id,
                "script_id": sid,
                "script_name": _text(script.get("script_name")),
                "seq": index,
                "version": version,
                "created_at": previous.get("created_at") if previous else moment,
                "updated_at": moment,
            }
        )

    kept = (
        all_stages[all_stages["script_id"].astype("string").fillna("") != sid]
        if not all_stages.empty
        else all_stages
    )
    frame = coerce_frame(
        pd.concat([kept, pd.DataFrame(records)], ignore_index=True)
        if records
        else kept,
        SCRIPT_STAGES_SPEC,
    )
    store.save_script_stages(frame)
    return {
        "script_id": sid,
        "script_name": _text(script.get("script_name")),
        "version": version,
        "stages": [_stage_payload(row) for row in records],
        "warnings": warnings,
    }


def save_stages(
    store: Any,
    *,
    script_id: str | None = None,
    script_name: str | None = None,
    stages: Iterable[dict[str, Any]],
    now: Any = None,
) -> dict[str, Any]:
    script = _script_row(store, script_id=script_id, script_name=script_name)
    with store_lock(store):
        return _save_stages_impl(store, script=script, stages=stages, now=now)


def duplicate_stages(
    store: Any,
    *,
    source_script_id: str,
    target_script_id: str,
    overwrite: bool = False,
    now: Any = None,
) -> dict[str, Any]:
    source = _script_row(store, script_id=source_script_id)
    target = _script_row(store, script_id=target_script_id)
    if _text(source.get("script_id")) == _text(target.get("script_id")):
        raise StageTemplateError("invalid_stage_value", "源剧本和目标剧本不能相同。")
    all_stages = _load_all(store)
    if not all_stages.empty:
        target_id = _text(target.get("script_id"))
        has_target = all_stages["script_id"].astype("string").fillna("") == target_id
        if bool(has_target.any()) and not overwrite:
            raise StageTemplateError("template_exists", "目标剧本已有阶段模板。")
    rows = []
    if not all_stages.empty:
        source_id = _text(source.get("script_id"))
        hit = all_stages[
            (all_stages["script_id"].astype("string").fillna("") == source_id)
            & (all_stages["active"].fillna(False))
        ].sort_values("seq")
        for row in hit.to_dict("records"):
            rows.append(
                {
                    "name": row.get("name"),
                    "stage_type": row.get("stage_type"),
                    "duration_minutes": row.get("duration_minutes"),
                    "warn_before_seconds": row.get("warn_before_seconds"),
                    "cue_text": row.get("cue_text"),
                    "prompt_text": row.get("prompt_text"),
                    "hardware_scene": row.get("hardware_scene"),
                    "notes": row.get("notes"),
                    "active": True,
                }
            )
    if not rows:
        raise StageTemplateError("template_missing", "源剧本没有可复制的启用阶段。")
    with store_lock(store):
        return _save_stages_impl(store, script=target, stages=rows, now=now)


def sample_stages() -> list[dict[str, Any]]:
    return [
        {
            "name": "开场",
            "stage_type": "opening",
            "duration_minutes": 15,
            "warn_before_seconds": 120,
            "prompt_text": "开场还剩 2 分钟，确认玩家已就位。",
            "cue_text": "宣读背景，提醒手机静音；观察玩家状态。",
            "active": True,
        },
        {
            "name": "第一幕 · 阅读",
            "stage_type": "search",
            "duration_minutes": 45,
            "warn_before_seconds": 300,
            "prompt_text": "阅读还剩 5 分钟。",
            "cue_text": "确认每位玩家都读完，提醒可做笔记。",
            "active": True,
        },
        {
            "name": "第二幕 · 搜证",
            "stage_type": "search",
            "duration_minutes": 60,
            "warn_before_seconds": 300,
            "prompt_text": "搜证还剩 5 分钟。",
            "cue_text": "控制搜证节奏，留意落单玩家。",
            "active": True,
        },
        {
            "name": "讨论",
            "stage_type": "discussion",
            "duration_minutes": 45,
            "warn_before_seconds": 300,
            "prompt_text": "讨论还剩 5 分钟。",
            "cue_text": "推动玩家交换线索，避免长时间冷场。",
            "active": True,
        },
        {
            "name": "复盘",
            "stage_type": "reveal",
            "duration_minutes": 30,
            "warn_before_seconds": 60,
            "prompt_text": "复盘还剩 1 分钟。",
            "cue_text": "按时间线复盘，回答玩家问题。",
            "active": True,
        },
    ]


def apply_sample_stages(store: Any, *, script_id: str, now: Any = None) -> dict[str, Any]:
    return save_stages(store, script_id=script_id, stages=sample_stages(), now=now)


def _snapshot_scene(value: Any) -> dict[str, Any] | None:
    try:
        return parse_hardware_scene(value)
    except StageTemplateError:
        return None


def snapshot_for_run(stages: Any, *, version: int | None = None) -> dict[str, Any]:
    """只取 active 阶段，按 1..M 重新编号，返回可 JSON 序列化的快照。"""
    if isinstance(stages, pd.DataFrame):
        records = stages.to_dict("records")
    else:
        records = [dict(item) for item in (stages or [])]
    active = [
        item
        for item in records
        if not is_missing_value(item.get("active")) and bool(item.get("active"))
    ]
    active.sort(key=lambda item: int(item.get("seq") or 0))
    snapshot_stages: list[dict[str, Any]] = []
    for index, item in enumerate(active, start=1):
        snapshot_stages.append(
            {
                "stage_id": _text(item.get("stage_id")),
                "seq": index,
                "name": _text(item.get("name")),
                "stage_type": _text(item.get("stage_type")) or None,
                "duration_minutes": _int_or_zero(item.get("duration_minutes")),
                "warn_before_seconds": _int_or_zero(item.get("warn_before_seconds")),
                "cue_text": _text(item.get("cue_text")) or None,
                "prompt_text": _text(item.get("prompt_text")) or None,
                "hardware_scene": _snapshot_scene(item.get("hardware_scene")),
                "notes": _text(item.get("notes")) or None,
            }
        )
    script_id = _text(records[0].get("script_id")) if records else ""
    return {
        "version": version,
        "script_id": script_id,
        "stages": snapshot_stages,
    }


__all__ = [
    "MAX_SCENE_BYTES",
    "MAX_SCENE_DEPTH",
    "STAGE_TYPES",
    "StageTemplateError",
    "apply_sample_stages",
    "duplicate_stages",
    "get_stages",
    "next_version",
    "parse_hardware_scene",
    "sample_stages",
    "save_stages",
    "snapshot_for_run",
    "validate_stages",
]

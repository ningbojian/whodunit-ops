"""其余新业务表的列规格。

只放列类型，不放业务规则；Store 与工具层共用同一份定义，避免 SQL、Excel
和测试各自维护一份列顺序。
"""

from __future__ import annotations

from app.tools.frame import FrameSpec

BOOKING_REFS_COLUMNS = [
    "ref_id",
    "source",
    "external_id",
    "booking_id",
    "session_id",
    "seats",
    "status",
    "data_as_of",
    "imported_at",
    "raw_ref",
]
BOOKING_REFS_SPEC = FrameSpec(
    name="渠道预约引用表",
    columns=BOOKING_REFS_COLUMNS,
    integers=("seats",),
    datetimes=("data_as_of", "imported_at"),
    strings=(
        "ref_id",
        "source",
        "external_id",
        "booking_id",
        "session_id",
        "status",
        "raw_ref",
    ),
)

SCRIPT_STAGES_COLUMNS = [
    "stage_id",
    "script_id",
    "script_name",
    "seq",
    "name",
    "stage_type",
    "duration_minutes",
    "warn_before_seconds",
    "cue_text",
    "prompt_text",
    "hardware_scene",
    "notes",
    "active",
    "version",
    "created_at",
    "updated_at",
]
SCRIPT_STAGES_SPEC = FrameSpec(
    name="剧本阶段模板表",
    columns=SCRIPT_STAGES_COLUMNS,
    integers=(
        "seq",
        "duration_minutes",
        "warn_before_seconds",
        "version",
    ),
    datetimes=("created_at", "updated_at"),
    booleans=("active",),
    strings=(
        "stage_id",
        "script_id",
        "script_name",
        "name",
        "stage_type",
        "cue_text",
        "prompt_text",
        "hardware_scene",
        "notes",
    ),
    defaults={"warn_before_seconds": 60, "active": True, "version": 1},
)

DM_RUNS_COLUMNS = [
    "run_id",
    "session_id",
    "dm_id",
    "dm_name",
    "room_id",
    "room_name",
    "status",
    "mode",
    "template_version",
    "template_snapshot",
    "current_stage_id",
    "current_seq",
    "stage_started_at",
    "stage_ends_at",
    "paused_at",
    "paused_total_seconds",
    "remaining_seconds_at_pause",
    "last_activity_at",
    "hardware_degraded",
    "access_code_hash",
    "access_code_expires_at",
    "access_code_redeemed_at",
    "token_version",
    "state_version",
    "created_by",
    "created_at",
    "updated_at",
    "finished_at",
]
DM_RUNS_SPEC = FrameSpec(
    name="DM 场控运行表",
    columns=DM_RUNS_COLUMNS,
    integers=(
        "template_version",
        "current_seq",
        "paused_total_seconds",
        "remaining_seconds_at_pause",
        "token_version",
        "state_version",
    ),
    datetimes=(
        "stage_started_at",
        "stage_ends_at",
        "paused_at",
        "last_activity_at",
        "access_code_expires_at",
        "access_code_redeemed_at",
        "created_at",
        "updated_at",
        "finished_at",
    ),
    booleans=("hardware_degraded",),
    strings=(
        "run_id",
        "session_id",
        "dm_id",
        "dm_name",
        "room_id",
        "room_name",
        "status",
        "mode",
        "template_snapshot",
        "current_stage_id",
        "access_code_hash",
        "created_by",
    ),
    defaults={
        "status": "pending",
        "mode": "software",
        "paused_total_seconds": 0,
        "hardware_degraded": False,
        "token_version": 0,
        "state_version": 0,
    },
)

DM_RUN_EVENTS_COLUMNS = [
    "event_id",
    "run_id",
    "event_type",
    "stage_id",
    "stage_seq",
    "actor",
    "at",
    "payload",
    "created_at",
]
DM_RUN_EVENTS_SPEC = FrameSpec(
    name="DM 场控事件表",
    columns=DM_RUN_EVENTS_COLUMNS,
    integers=("stage_seq",),
    datetimes=("at", "created_at"),
    strings=(
        "event_id",
        "run_id",
        "event_type",
        "stage_id",
        "actor",
        "payload",
    ),
)


__all__ = [
    "BOOKING_REFS_COLUMNS",
    "BOOKING_REFS_SPEC",
    "DM_RUNS_COLUMNS",
    "DM_RUNS_SPEC",
    "DM_RUN_EVENTS_COLUMNS",
    "DM_RUN_EVENTS_SPEC",
    "SCRIPT_STAGES_COLUMNS",
    "SCRIPT_STAGES_SPEC",
]

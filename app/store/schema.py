"""SQLAlchemy Core 表定义，SQLite 与 Postgres 共用。

时间约定：
  - 业务时间（开场、预约）是不带时区的门店本地时间（UTC+8）；
  - 审批与用量是 UTC 的绝对时间，用 UTCDateTime 统一转换。

不落库的派生字段：
  booked_seats / remaining_seats 永远从 bookings 重算，数据库里没有这两列。
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    MetaData,
    Numeric,
    PrimaryKeyConstraint,
    String,
    Table,
    Text,
    TypeDecorator,
)

UTC = timezone.utc


class UTCDateTime(TypeDecorator[datetime]):
    """Postgres 用 TIMESTAMPTZ；SQLite 存 naive UTC，读回转成 aware UTC。"""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if not isinstance(value, datetime):
            raise TypeError("UTCDateTime 只接受 datetime")
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        value = value.astimezone(UTC)
        if dialect.name == "sqlite":
            return value.replace(tzinfo=None)
        return value

    def process_result_value(self, value, dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    def compare_values(self, x, y):  # pragma: no cover - SQLAlchemy 内部兜底
        return x == y

    @property
    def python_type(self) -> type[datetime]:
        return datetime


metadata = MetaData()

tenants = Table(
    "tenants",
    metadata,
    Column("tenant_id", String(32), primary_key=True),
    Column("email_hash", String(64), nullable=True, unique=True),
    Column("display_name", Text, nullable=True),
    Column("created_at", UTCDateTime(), nullable=False),
    Column("seeded_at", UTCDateTime(), nullable=True),
)

sessions = Table(
    "sessions",
    metadata,
    Column("tenant_id", String(32), nullable=False, index=True),
    Column("session_id", String(64), nullable=False),
    Column("store_name", Text, nullable=False),
    Column("script_name", Text, nullable=False),
    Column("script_type", Text, nullable=True),
    Column("start_time", DateTime(timezone=False), nullable=False),
    Column("total_seats", Integer, nullable=True),
    Column("list_price", Numeric(10, 2), nullable=True),
    Column("status", String(16), nullable=False),
    Column("source_text", Text, nullable=True),
    Column("updated_at", DateTime(timezone=False), nullable=False),
    PrimaryKeyConstraint("tenant_id", "session_id", name="pk_sessions"),
    ForeignKeyConstraint(
        ["tenant_id"], ["tenants.tenant_id"], ondelete="CASCADE", name="fk_sessions_tenant"
    ),
)
Index("idx_sessions_time", sessions.c.tenant_id, sessions.c.start_time)

bookings = Table(
    "bookings",
    metadata,
    Column("tenant_id", String(32), nullable=False, index=True),
    Column("booking_id", String(64), nullable=False),
    Column("session_id", String(64), nullable=False),
    Column("player_name", Text, nullable=False),
    Column("seats", Integer, nullable=False),
    Column("status", String(16), nullable=False),
    Column("booked_at", DateTime(timezone=False), nullable=False),
    Column("updated_at", DateTime(timezone=False), nullable=False),
    Column("source_text", Text, nullable=True),
    PrimaryKeyConstraint("tenant_id", "booking_id", name="pk_bookings"),
    ForeignKeyConstraint(
        ["tenant_id"], ["tenants.tenant_id"], ondelete="CASCADE", name="fk_bookings_tenant"
    ),
)
Index(
    "idx_bookings_session",
    bookings.c.tenant_id,
    bookings.c.session_id,
    bookings.c.status,
)

scripts = Table(
    "scripts",
    metadata,
    Column("tenant_id", String(32), nullable=False, index=True),
    Column("script_id", String(64), nullable=False),
    Column("script_name", Text, nullable=True),
    Column("name_key", String(80), nullable=True),
    Column("aliases", Text, nullable=True),
    Column("script_type", Text, nullable=True),
    Column("total_seats", Integer, nullable=True),
    Column("duration_minutes", Integer, nullable=True),
    Column("list_price", Numeric(10, 2), nullable=True),
    Column("notes", Text, nullable=True),
    Column("source_text", Text, nullable=True),
    Column("created_at", DateTime(timezone=False), nullable=False),
    Column("updated_at", DateTime(timezone=False), nullable=False),
    PrimaryKeyConstraint("tenant_id", "script_id", name="pk_scripts"),
    ForeignKeyConstraint(
        ["tenant_id"], ["tenants.tenant_id"], ondelete="CASCADE", name="fk_scripts_tenant"
    ),
)

# 同一租户内名称唯一。空名称的草稿不参与唯一约束。
_name_not_blank = scripts.c.name_key.isnot(None) & (scripts.c.name_key != "")
Index(
    "idx_scripts_name",
    scripts.c.tenant_id,
    scripts.c.name_key,
    unique=True,
    postgresql_where=_name_not_blank,
    sqlite_where=_name_not_blank,
)

# 公共剧本库：全局共享、商家只读。与 scripts 同列但无 tenant_id、无 FK；
# script_id 带 `pub-` 前缀；origin=来源标注（策展/crawler:vshang66:URL）。
public_scripts = Table(
    "public_scripts",
    metadata,
    Column("script_id", String(64), nullable=False),
    Column("script_name", Text, nullable=True),
    Column("name_key", String(80), nullable=True),
    Column("aliases", Text, nullable=True),
    Column("script_type", Text, nullable=True),
    Column("total_seats", Integer, nullable=True),
    Column("duration_minutes", Integer, nullable=True),
    Column("list_price", Numeric(10, 2), nullable=True),
    Column("notes", Text, nullable=True),
    Column("source_text", Text, nullable=True),
    Column("origin", Text, nullable=True),
    Column("imported_at", DateTime(timezone=False), nullable=True),
    Column("created_at", DateTime(timezone=False), nullable=False),
    Column("updated_at", DateTime(timezone=False), nullable=False),
    PrimaryKeyConstraint("script_id", name="pk_public_scripts"),
)

_public_name_not_blank = (
    public_scripts.c.name_key.isnot(None) & (public_scripts.c.name_key != "")
)
Index(
    "idx_public_scripts_name",
    public_scripts.c.name_key,
    unique=True,
    postgresql_where=_public_name_not_blank,
    sqlite_where=_public_name_not_blank,
)

# web_search 多租户日配额计数（每租户每天一行；全局配额可由 SUM 求得）。
search_usage_daily = Table(
    "search_usage_daily",
    metadata,
    Column("tenant_id", String(32), nullable=False),
    Column("day", String(10), nullable=False),
    Column("count", Integer, nullable=False, default=0),
    Column("updated_at", DateTime(timezone=False), nullable=False),
    PrimaryKeyConstraint("tenant_id", "day", name="pk_search_usage_daily"),
)

access_requests = Table(
    "access_requests",
    metadata,
    Column("id", Text, primary_key=True),
    Column("email", Text, nullable=False),
    Column("email_hash", String(64), nullable=False),
    Column("display_name", Text, nullable=False),
    Column("status", String(16), nullable=False),
    Column("created_at", UTCDateTime(), nullable=False),
    Column("updated_at", UTCDateTime(), nullable=False),
    Column("approval_expires_at", UTCDateTime(), nullable=False),
    Column("approved_at", UTCDateTime(), nullable=True),
    Column("access_expires_at", UTCDateTime(), nullable=True),
    Column("approval_token_hash", Text, nullable=False),
    Column("access_token_hash", Text, nullable=True),
    Column("browser_token_hash", Text, nullable=False),
    Column("requester_ip_hash", Text, nullable=True),
    Column("email_error", Text, nullable=True),
)
Index("idx_access_email", access_requests.c.email_hash, access_requests.c.created_at)
Index(
    "idx_access_status",
    access_requests.c.status,
    access_requests.c.access_expires_at,
)
Index(
    "idx_access_approval_token",
    access_requests.c.approval_token_hash,
    unique=True,
)
Index(
    "idx_access_access_token",
    access_requests.c.access_token_hash,
    unique=True,
)
Index("idx_access_ip", access_requests.c.requester_ip_hash, access_requests.c.created_at)
Index("idx_access_created", access_requests.c.created_at)

usage_global_daily = Table(
    "usage_global_daily",
    metadata,
    Column("day", String(10), primary_key=True),
    Column("turns", Integer, nullable=False),
    Column("updated_at", UTCDateTime(), nullable=False),
)

import_runs = Table(
    "import_runs",
    metadata,
    Column("id", Text, primary_key=True),
    Column("source", Text, nullable=False),
    Column("tenant_id", String(32), nullable=False),
    Column("started_at", UTCDateTime(), nullable=False),
    Column("finished_at", UTCDateTime(), nullable=True),
    Column("status", String(16), nullable=False),
    Column("session_rows", Integer, nullable=False, default=0),
    Column("booking_rows", Integer, nullable=False, default=0),
    Column("script_rows", Integer, nullable=False, default=0),
    Column("warnings", Text, nullable=True),
)

rooms = Table(
    "rooms",
    metadata,
    Column("tenant_id", String(32), nullable=False, index=True),
    Column("room_id", String(64), nullable=False),
    Column("name", Text, nullable=False),
    Column("capacity", Integer, nullable=True),
    Column("tags", Text, nullable=True),
    Column("hourly_cost", Numeric(10, 2), nullable=True),
    Column("buffer_minutes", Integer, nullable=False, default=15),
    Column("active", Boolean, nullable=False, default=True),
    Column("notes", Text, nullable=True),
    Column("source_text", Text, nullable=True),
    Column("created_at", DateTime(timezone=False), nullable=False),
    Column("updated_at", DateTime(timezone=False), nullable=False),
    PrimaryKeyConstraint("tenant_id", "room_id", name="pk_rooms"),
    ForeignKeyConstraint(
        ["tenant_id"], ["tenants.tenant_id"], ondelete="CASCADE", name="fk_rooms_tenant"
    ),
)

dms = Table(
    "dms",
    metadata,
    Column("tenant_id", String(32), nullable=False, index=True),
    Column("dm_id", String(64), nullable=False),
    Column("name", Text, nullable=False),
    Column("skills", Text, nullable=True),
    Column("hourly_cost", Numeric(10, 2), nullable=True),
    Column("max_sessions_per_day", Integer, nullable=True),
    Column("active", Boolean, nullable=False, default=True),
    Column("notes", Text, nullable=True),
    Column("source_text", Text, nullable=True),
    Column("created_at", DateTime(timezone=False), nullable=False),
    Column("updated_at", DateTime(timezone=False), nullable=False),
    PrimaryKeyConstraint("tenant_id", "dm_id", name="pk_dms"),
    ForeignKeyConstraint(
        ["tenant_id"], ["tenants.tenant_id"], ondelete="CASCADE", name="fk_dms_tenant"
    ),
)

dm_shifts = Table(
    "dm_shifts",
    metadata,
    Column("tenant_id", String(32), nullable=False, index=True),
    Column("shift_id", String(64), nullable=False),
    Column("dm_id", String(64), nullable=False),
    Column("weekday", Integer, nullable=False),
    Column("start_minute", Integer, nullable=False),
    Column("end_minute", Integer, nullable=False),
    Column("active", Boolean, nullable=False, default=True),
    Column("updated_at", DateTime(timezone=False), nullable=False),
    PrimaryKeyConstraint("tenant_id", "shift_id", name="pk_dm_shifts"),
    ForeignKeyConstraint(
        ["tenant_id"],
        ["tenants.tenant_id"],
        ondelete="CASCADE",
        name="fk_dm_shifts_tenant",
    ),
)
Index("idx_dm_shifts_dm", dm_shifts.c.tenant_id, dm_shifts.c.dm_id, dm_shifts.c.weekday)

dm_leaves = Table(
    "dm_leaves",
    metadata,
    Column("tenant_id", String(32), nullable=False, index=True),
    Column("leave_id", String(64), nullable=False),
    Column("dm_id", String(64), nullable=False),
    Column("day", String(10), nullable=False),
    Column("note", String(200), nullable=True),
    Column("created_at", DateTime(timezone=False), nullable=False),
    Column("updated_at", DateTime(timezone=False), nullable=False),
    PrimaryKeyConstraint("tenant_id", "leave_id", name="pk_dm_leaves"),
    ForeignKeyConstraint(
        ["tenant_id"],
        ["tenants.tenant_id"],
        ondelete="CASCADE",
        name="fk_dm_leaves_tenant",
    ),
)
Index(
    "idx_dm_leaves_dm_day",
    dm_leaves.c.tenant_id,
    dm_leaves.c.dm_id,
    dm_leaves.c.day,
)

session_assignments = Table(
    "session_assignments",
    metadata,
    Column("tenant_id", String(32), nullable=False, index=True),
    Column("assignment_id", String(64), nullable=False),
    Column("session_id", String(64), nullable=False),
    Column("resource_kind", String(8), nullable=False),
    Column("resource_id", String(64), nullable=False),
    Column("role", String(16), nullable=False),
    Column("source", String(16), nullable=False),
    Column("assigned_at", DateTime(timezone=False), nullable=False),
    Column("updated_at", DateTime(timezone=False), nullable=False),
    PrimaryKeyConstraint("tenant_id", "assignment_id", name="pk_session_assignments"),
    ForeignKeyConstraint(
        ["tenant_id"],
        ["tenants.tenant_id"],
        ondelete="CASCADE",
        name="fk_session_assignments_tenant",
    ),
)
Index(
    "idx_assignments_session",
    session_assignments.c.tenant_id,
    session_assignments.c.session_id,
)
Index(
    "idx_assignments_unique",
    session_assignments.c.tenant_id,
    session_assignments.c.session_id,
    session_assignments.c.resource_kind,
    session_assignments.c.resource_id,
    unique=True,
)

booking_intake_refs = Table(
    "booking_intake_refs",
    metadata,
    Column("tenant_id", String(32), nullable=False, index=True),
    Column("ref_id", String(64), nullable=False),
    Column("source", String(16), nullable=False),
    Column("external_id", String(128), nullable=False),
    Column("booking_id", String(64), nullable=False),
    Column("session_id", String(64), nullable=False),
    Column("seats", Integer, nullable=False),
    Column("status", String(16), nullable=False),
    Column("data_as_of", UTCDateTime(), nullable=False),
    Column("imported_at", UTCDateTime(), nullable=False),
    Column("raw_ref", Text, nullable=True),
    PrimaryKeyConstraint("tenant_id", "ref_id", name="pk_booking_intake_refs"),
    ForeignKeyConstraint(
        ["tenant_id"],
        ["tenants.tenant_id"],
        ondelete="CASCADE",
        name="fk_booking_intake_refs_tenant",
    ),
)
Index(
    "idx_booking_refs_external",
    booking_intake_refs.c.tenant_id,
    booking_intake_refs.c.source,
    booking_intake_refs.c.external_id,
    unique=True,
)
Index(
    "idx_booking_refs_booking",
    booking_intake_refs.c.tenant_id,
    booking_intake_refs.c.booking_id,
)

script_stages = Table(
    "script_stages",
    metadata,
    Column("tenant_id", String(32), nullable=False, index=True),
    Column("stage_id", String(64), nullable=False),
    Column("script_id", String(64), nullable=False),
    Column("script_name", Text, nullable=False),
    Column("seq", Integer, nullable=False),
    Column("name", Text, nullable=False),
    Column("stage_type", String(16), nullable=True),
    Column("duration_minutes", Integer, nullable=False),
    Column("warn_before_seconds", Integer, nullable=False, default=60),
    Column("cue_text", Text, nullable=True),
    Column("prompt_text", Text, nullable=True),
    Column("hardware_scene", Text, nullable=True),
    Column("notes", Text, nullable=True),
    Column("active", Boolean, nullable=False, default=True),
    Column("version", Integer, nullable=False, default=1),
    Column("created_at", DateTime(timezone=False), nullable=False),
    Column("updated_at", DateTime(timezone=False), nullable=False),
    PrimaryKeyConstraint("tenant_id", "stage_id", name="pk_script_stages"),
    ForeignKeyConstraint(
        ["tenant_id"],
        ["tenants.tenant_id"],
        ondelete="CASCADE",
        name="fk_script_stages_tenant",
    ),
)
Index(
    "idx_script_stages_seq",
    script_stages.c.tenant_id,
    script_stages.c.script_id,
    script_stages.c.seq,
    unique=True,
)
Index(
    "idx_script_stages_list",
    script_stages.c.tenant_id,
    script_stages.c.script_id,
    script_stages.c.active,
)

dm_runs = Table(
    "dm_runs",
    metadata,
    Column("tenant_id", String(32), nullable=False, index=True),
    Column("run_id", String(64), nullable=False),
    Column("session_id", String(64), nullable=False),
    Column("dm_id", String(64), nullable=True),
    Column("dm_name", Text, nullable=True),
    Column("room_id", String(64), nullable=True),
    Column("room_name", Text, nullable=True),
    Column("status", String(16), nullable=False),
    Column("mode", String(16), nullable=False),
    Column("template_version", Integer, nullable=True),
    Column("template_snapshot", Text, nullable=True),
    Column("current_stage_id", String(64), nullable=True),
    Column("current_seq", Integer, nullable=True),
    Column("stage_started_at", UTCDateTime(), nullable=True),
    Column("stage_ends_at", UTCDateTime(), nullable=True),
    Column("paused_at", UTCDateTime(), nullable=True),
    Column("paused_total_seconds", Integer, nullable=False, default=0),
    Column("remaining_seconds_at_pause", Integer, nullable=True),
    Column("last_activity_at", UTCDateTime(), nullable=True),
    Column("hardware_degraded", Boolean, nullable=False, default=False),
    Column("access_code_hash", Text, nullable=True),
    Column("access_code_expires_at", UTCDateTime(), nullable=True),
    Column("access_code_redeemed_at", UTCDateTime(), nullable=True),
    Column("token_version", Integer, nullable=False, default=0),
    Column("state_version", Integer, nullable=False, default=0),
    Column("created_by", String(64), nullable=True),
    Column("created_at", UTCDateTime(), nullable=False),
    Column("updated_at", UTCDateTime(), nullable=False),
    Column("finished_at", UTCDateTime(), nullable=True),
    PrimaryKeyConstraint("tenant_id", "run_id", name="pk_dm_runs"),
    ForeignKeyConstraint(
        ["tenant_id"], ["tenants.tenant_id"], ondelete="CASCADE", name="fk_dm_runs_tenant"
    ),
)
Index("idx_dm_runs_session", dm_runs.c.tenant_id, dm_runs.c.session_id, dm_runs.c.status)
Index("idx_dm_runs_dm", dm_runs.c.tenant_id, dm_runs.c.dm_id, dm_runs.c.status)
# 兑换时还不知道租户，必须能用码哈希直接定位；NULL 不参与唯一性。
Index("idx_dm_runs_code", dm_runs.c.access_code_hash, unique=True)

dm_run_events = Table(
    "dm_run_events",
    metadata,
    Column("tenant_id", String(32), nullable=False, index=True),
    Column("event_id", String(64), nullable=False),
    Column("run_id", String(64), nullable=False),
    Column("event_type", String(32), nullable=False),
    Column("stage_id", String(64), nullable=True),
    Column("stage_seq", Integer, nullable=True),
    Column("actor", String(16), nullable=False),
    Column("at", UTCDateTime(), nullable=False),
    Column("payload", Text, nullable=True),
    Column("created_at", UTCDateTime(), nullable=False),
    PrimaryKeyConstraint("tenant_id", "event_id", name="pk_dm_run_events"),
    ForeignKeyConstraint(
        ["tenant_id"],
        ["tenants.tenant_id"],
        ondelete="CASCADE",
        name="fk_dm_run_events_tenant",
    ),
)
Index(
    "idx_dm_run_events_run",
    dm_run_events.c.tenant_id,
    dm_run_events.c.run_id,
    dm_run_events.c.at,
)

# 这些表需要建表；供 ensure_schema 与迁移脚本共用。
ALL_TABLES: tuple[Table, ...] = (
    tenants,
    sessions,
    bookings,
    scripts,
    public_scripts,
    search_usage_daily,
    access_requests,
    usage_global_daily,
    import_runs,
    rooms,
    dms,
    dm_shifts,
    dm_leaves,
    session_assignments,
    booking_intake_refs,
    script_stages,
    dm_runs,
    dm_run_events,
)


__all__ = [
    "ALL_TABLES",
    "UTCDateTime",
    "access_requests",
    "booking_intake_refs",
    "bookings",
    "dm_run_events",
    "dm_runs",
    "dm_leaves",
    "dm_shifts",
    "dms",
    "import_runs",
    "metadata",
    "public_scripts",
    "rooms",
    "script_stages",
    "scripts",
    "search_usage_daily",
    "session_assignments",
    "sessions",
    "tenants",
    "usage_global_daily",
]

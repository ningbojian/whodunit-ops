"""SQL 后端：同一份实现服务 SQLite（本地/测试）与 Postgres（Neon）。"""

from __future__ import annotations

import hashlib
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

import pandas as pd
from sqlalchemy import Engine, delete, insert, select, text, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from app.store.schema import (
    booking_intake_refs,
    bookings,
    dm_run_events,
    dm_runs,
    dm_leaves,
    dm_shifts,
    dms,
    rooms,
    script_stages,
    scripts,
    session_assignments,
    sessions,
    tenants,
)
from app.tools.frame import coerce_frame, empty_frame
from app.tools.frame_specs import (
    BOOKING_REFS_COLUMNS,
    BOOKING_REFS_SPEC,
    DM_RUNS_COLUMNS,
    DM_RUNS_SPEC,
    DM_RUN_EVENTS_COLUMNS,
    DM_RUN_EVENTS_SPEC,
    SCRIPT_STAGES_COLUMNS,
    SCRIPT_STAGES_SPEC,
)
from app.tools.resources import (
    ASSIGNMENTS_COLUMNS,
    ASSIGNMENTS_SPEC,
    DMS_COLUMNS,
    DMS_SPEC,
    DM_LEAVES_COLUMNS,
    DM_LEAVES_SPEC,
    ROOMS_COLUMNS,
    ROOMS_SPEC,
    SHIFTS_COLUMNS,
    SHIFTS_SPEC,
)
from app.tools.scripts import (
    coerce_scripts,
    empty_scripts,
    normalize_script_name,
)
from app.tools.storage import (
    coerce_bookings,
    coerce_sessions,
    empty_bookings,
    empty_sessions,
    is_missing_value,
)

_SESSION_DB_COLUMNS = [
    "session_id",
    "store_name",
    "script_name",
    "script_type",
    "start_time",
    "total_seats",
    "list_price",
    "status",
    "source_text",
    "updated_at",
]
_BOOKING_DB_COLUMNS = [
    "booking_id",
    "session_id",
    "player_name",
    "seats",
    "status",
    "booked_at",
    "updated_at",
    "source_text",
]
_SCRIPT_DB_COLUMNS = [
    "script_id",
    "script_name",
    "aliases",
    "script_type",
    "total_seats",
    "duration_minutes",
    "list_price",
    "notes",
    "source_text",
    "created_at",
    "updated_at",
]


def to_db_value(value: Any) -> Any:
    """把 pandas 的空值 / numpy 标量转成数据库可接受的原生值。

    0 是有效值，绝不能当空值处理。
    """
    if is_missing_value(value):
        return None
    if isinstance(value, pd.Timestamp):
        return value.to_pydatetime()
    if hasattr(value, "to_pydatetime"):
        try:
            return value.to_pydatetime()
        except (TypeError, ValueError):
            pass
    item = getattr(value, "item", None)
    if callable(item):
        try:
            return item()
        except (TypeError, ValueError, AttributeError):
            pass
    return value


class TenantConflictError(ValueError):
    """tenant_id 前缀与另一条完整邮箱哈希冲突。绝不共用租户数据。"""


def _advisory_key(tenant_id: str) -> int:
    """租户写锁的 64 位 advisory lock key（跨进程一致）。"""
    digest = hashlib.blake2b(tenant_id.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, "big", signed=True)


def _check_duplicate_keys(frame: pd.DataFrame, key: str, label: str) -> None:
    if frame.empty:
        return
    duplicated = frame[key].astype("string").fillna("")
    duplicated = duplicated[duplicated != ""]
    dup = duplicated[duplicated.duplicated(keep=False)]
    if not dup.empty:
        raise ValueError(f"{label}存在重复主键：{dup.iloc[0]}")


def _check_duplicate_composite(
    frame: pd.DataFrame,
    fields: tuple[str, ...],
    label: str,
    *,
    skip_missing: bool = False,
) -> None:
    if frame.empty:
        return
    values = frame[list(fields)].astype("string").fillna("")
    if skip_missing:
        values = values[(values != "").all(axis=1)]
    duplicated = values[values.duplicated(keep=False)]
    if not duplicated.empty:
        sample = " / ".join(str(item) for item in duplicated.iloc[0].tolist())
        raise ValueError(f"{label}存在重复组合键：{sample}")


class SqlStore:
    """按租户读写 SQL 表。

    save_* 语义与 Excel 一致：在事务内删除该租户旧行，再写入整张表。
    """

    def __init__(self, engine: Engine, tenant_id: str) -> None:
        if not isinstance(tenant_id, str) or not tenant_id.strip():
            raise ValueError("tenant_id 不能为空")
        self.engine = engine
        self.tenant_id = tenant_id.strip()
        self._local = threading.local()
        self._lock = self._engine_lock(engine)

    @staticmethod
    def _engine_lock(engine: Engine) -> threading.RLock:
        lock = getattr(engine, "_jbs_store_lock", None)
        if lock is None:
            lock = threading.RLock()
            setattr(engine, "_jbs_store_lock", lock)
        return lock

    # ------------------------------------------------------------------
    # 写锁
    # ------------------------------------------------------------------
    @contextmanager
    def write_lock(self) -> Iterator[None]:
        """同一租户的写操作串行。

        - SQLite：进程内可重入锁（单 worker 部署）；
        - Postgres：进程内锁 + 事务级 advisory lock，为将来多进程保留正确性。
          刻意不用 `tenants ... FOR UPDATE`：写事务在另一条连接上插入带外键的
          子表行，会请求 parent row 的 KEY SHARE，与 FOR UPDATE 互斥；外层锁
          事务要等写事务返回才提交，会形成死锁。advisory lock 不参与行锁矩阵。
        嵌套调用直接复用外层锁，避免同一线程二次加锁死锁。
        """
        depth = int(getattr(self._local, "depth", 0))
        if depth:
            self._local.depth = depth + 1
            try:
                yield
            finally:
                self._local.depth = depth
            return

        with self._lock:
            self._local.depth = 1
            try:
                if self.engine.dialect.name == "postgresql":
                    with self.engine.connect() as conn:
                        with conn.begin():
                            conn.execute(
                                text("SELECT pg_advisory_xact_lock(:key)"),
                                {"key": _advisory_key(self.tenant_id)},
                            )
                            yield
                else:
                    yield
            finally:
                self._local.depth = 0

    # ------------------------------------------------------------------
    # sessions
    # ------------------------------------------------------------------
    def load_sessions(self) -> pd.DataFrame:
        query = (
            select(
                sessions.c.session_id,
                sessions.c.store_name,
                sessions.c.script_name,
                sessions.c.script_type,
                sessions.c.start_time,
                sessions.c.total_seats,
                sessions.c.list_price,
                sessions.c.status,
                sessions.c.source_text,
                sessions.c.updated_at,
            )
            .where(sessions.c.tenant_id == self.tenant_id)
            .order_by(sessions.c.start_time, sessions.c.session_id)
        )
        with self.engine.connect() as conn:
            rows = [dict(row) for row in conn.execute(query).mappings()]
        if not rows:
            return empty_sessions()
        frame = pd.DataFrame(rows)
        # booked_seats 是派生字段，数据库不存；先补 0，后续由预约明细重算。
        frame["booked_seats"] = 0
        return coerce_sessions(frame)

    def save_sessions(self, frame: pd.DataFrame) -> None:
        clean = coerce_sessions(frame)
        _check_duplicate_keys(clean, "session_id", "场次表")
        records: list[dict[str, Any]] = []
        for row in clean.to_dict("records"):
            records.append(
                {
                    "tenant_id": self.tenant_id,
                    **{
                        column: to_db_value(row.get(column))
                        for column in _SESSION_DB_COLUMNS
                    },
                }
            )
        with self.engine.begin() as conn:
            conn.execute(delete(sessions).where(sessions.c.tenant_id == self.tenant_id))
            if records:
                conn.execute(insert(sessions), records)

    # ------------------------------------------------------------------
    # bookings
    # ------------------------------------------------------------------
    def load_bookings(self) -> pd.DataFrame:
        query = (
            select(
                bookings.c.booking_id,
                bookings.c.session_id,
                bookings.c.player_name,
                bookings.c.seats,
                bookings.c.status,
                bookings.c.booked_at,
                bookings.c.updated_at,
                bookings.c.source_text,
            )
            .where(bookings.c.tenant_id == self.tenant_id)
            .order_by(bookings.c.booked_at, bookings.c.booking_id)
        )
        with self.engine.connect() as conn:
            rows = [dict(row) for row in conn.execute(query).mappings()]
        if not rows:
            return empty_bookings()
        return coerce_bookings(pd.DataFrame(rows))

    def save_bookings(self, frame: pd.DataFrame) -> None:
        clean = coerce_bookings(frame)
        _check_duplicate_keys(clean, "booking_id", "预约明细表")
        records: list[dict[str, Any]] = []
        for row in clean.to_dict("records"):
            records.append(
                {
                    "tenant_id": self.tenant_id,
                    **{
                        column: to_db_value(row.get(column))
                        for column in _BOOKING_DB_COLUMNS
                    },
                }
            )
        with self.engine.begin() as conn:
            conn.execute(delete(bookings).where(bookings.c.tenant_id == self.tenant_id))
            if records:
                conn.execute(insert(bookings), records)

    # ------------------------------------------------------------------
    # scripts
    # ------------------------------------------------------------------
    def load_scripts(self) -> pd.DataFrame:
        query = (
            select(
                scripts.c.script_id,
                scripts.c.script_name,
                scripts.c.aliases,
                scripts.c.script_type,
                scripts.c.total_seats,
                scripts.c.duration_minutes,
                scripts.c.list_price,
                scripts.c.notes,
                scripts.c.source_text,
                scripts.c.created_at,
                scripts.c.updated_at,
            )
            .where(scripts.c.tenant_id == self.tenant_id)
            .order_by(scripts.c.created_at, scripts.c.script_id)
        )
        with self.engine.connect() as conn:
            rows = [dict(row) for row in conn.execute(query).mappings()]
        if not rows:
            return empty_scripts()
        return coerce_scripts(pd.DataFrame(rows))

    def save_scripts(self, frame: pd.DataFrame) -> None:
        clean = coerce_scripts(frame)
        _check_duplicate_keys(clean, "script_id", "剧本库")
        records: list[dict[str, Any]] = []
        for row in clean.to_dict("records"):
            name_key = normalize_script_name(row.get("script_name")).casefold() or None
            record = {
                "tenant_id": self.tenant_id,
                "name_key": name_key,
                **{
                    column: to_db_value(row.get(column))
                    for column in _SCRIPT_DB_COLUMNS
                },
            }
            records.append(record)
        with self.engine.begin() as conn:
            conn.execute(delete(scripts).where(scripts.c.tenant_id == self.tenant_id))
            if records:
                conn.execute(insert(scripts), records)

    # ------------------------------------------------------------------
    # 新业务表：通用整表读写
    # ------------------------------------------------------------------
    def _load_table(
        self,
        table,
        columns: list[str],
        spec,
        order_by: list,
    ) -> pd.DataFrame:
        query = (
            select(*(table.c[column] for column in columns))
            .where(table.c.tenant_id == self.tenant_id)
            .order_by(*order_by)
        )
        with self.engine.connect() as conn:
            rows = [dict(row) for row in conn.execute(query).mappings()]
        if not rows:
            return empty_frame(spec)
        return coerce_frame(pd.DataFrame(rows), spec)

    def _replace_table(
        self,
        table,
        frame: pd.DataFrame,
        spec,
        columns: list[str],
        label: str,
        key: str,
        *,
        composite: tuple[tuple[str, ...], ...] = (),
        skip_missing_composite: bool = False,
    ) -> None:
        clean = coerce_frame(frame, spec)
        _check_duplicate_keys(clean, key, label)
        for fields in composite:
            _check_duplicate_composite(
                clean,
                fields,
                label,
                skip_missing=skip_missing_composite,
            )
        records = [
            {
                "tenant_id": self.tenant_id,
                **{
                    column: to_db_value(row.get(column))
                    for column in columns
                },
            }
            for row in clean.to_dict("records")
        ]
        with self.engine.begin() as conn:
            conn.execute(delete(table).where(table.c.tenant_id == self.tenant_id))
            if records:
                conn.execute(insert(table), records)

    # ------------------------------------------------------------------
    # 房间 / DM / 班次 / 分配
    # ------------------------------------------------------------------
    def load_rooms(self) -> pd.DataFrame:
        return self._load_table(
            rooms,
            ROOMS_COLUMNS,
            ROOMS_SPEC,
            [rooms.c.name, rooms.c.room_id],
        )

    def save_rooms(self, frame: pd.DataFrame) -> None:
        self._replace_table(rooms, frame, ROOMS_SPEC, ROOMS_COLUMNS, "房间表", "room_id")

    def load_dms(self) -> pd.DataFrame:
        return self._load_table(
            dms,
            DMS_COLUMNS,
            DMS_SPEC,
            [dms.c.name, dms.c.dm_id],
        )

    def save_dms(self, frame: pd.DataFrame) -> None:
        self._replace_table(dms, frame, DMS_SPEC, DMS_COLUMNS, "DM 表", "dm_id")

    def load_dm_shifts(self) -> pd.DataFrame:
        return self._load_table(
            dm_shifts,
            SHIFTS_COLUMNS,
            SHIFTS_SPEC,
            [
                dm_shifts.c.dm_id,
                dm_shifts.c.weekday,
                dm_shifts.c.start_minute,
                dm_shifts.c.shift_id,
            ],
        )

    def save_dm_shifts(self, frame: pd.DataFrame) -> None:
        self._replace_table(
            dm_shifts,
            frame,
            SHIFTS_SPEC,
            SHIFTS_COLUMNS,
            "DM 班次表",
            "shift_id",
        )

    def load_dm_leaves(self) -> pd.DataFrame:
        return self._load_table(
            dm_leaves,
            DM_LEAVES_COLUMNS,
            DM_LEAVES_SPEC,
            [dm_leaves.c.dm_id, dm_leaves.c.day, dm_leaves.c.leave_id],
        )

    def save_dm_leaves(self, frame: pd.DataFrame) -> None:
        self._replace_table(
            dm_leaves,
            frame,
            DM_LEAVES_SPEC,
            DM_LEAVES_COLUMNS,
            "DM 请假表",
            "leave_id",
        )

    def load_assignments(self) -> pd.DataFrame:
        return self._load_table(
            session_assignments,
            ASSIGNMENTS_COLUMNS,
            ASSIGNMENTS_SPEC,
            [
                session_assignments.c.session_id,
                session_assignments.c.resource_kind,
                session_assignments.c.resource_id,
            ],
        )

    def save_assignments(self, frame: pd.DataFrame) -> None:
        self._replace_table(
            session_assignments,
            frame,
            ASSIGNMENTS_SPEC,
            ASSIGNMENTS_COLUMNS,
            "场次分配表",
            "assignment_id",
            composite=(("session_id", "resource_kind", "resource_id"),),
        )

    # ------------------------------------------------------------------
    # 渠道单
    # ------------------------------------------------------------------
    def load_booking_refs(self) -> pd.DataFrame:
        return self._load_table(
            booking_intake_refs,
            BOOKING_REFS_COLUMNS,
            BOOKING_REFS_SPEC,
            [booking_intake_refs.c.imported_at, booking_intake_refs.c.ref_id],
        )

    def save_booking_refs(self, frame: pd.DataFrame) -> None:
        self._replace_table(
            booking_intake_refs,
            frame,
            BOOKING_REFS_SPEC,
            BOOKING_REFS_COLUMNS,
            "渠道预约引用表",
            "ref_id",
            composite=(("source", "external_id"),),
        )

    # ------------------------------------------------------------------
    # 阶段模板 / DM 场控
    # ------------------------------------------------------------------
    def load_script_stages(self) -> pd.DataFrame:
        return self._load_table(
            script_stages,
            SCRIPT_STAGES_COLUMNS,
            SCRIPT_STAGES_SPEC,
            [script_stages.c.script_id, script_stages.c.seq],
        )

    def save_script_stages(self, frame: pd.DataFrame) -> None:
        self._replace_table(
            script_stages,
            frame,
            SCRIPT_STAGES_SPEC,
            SCRIPT_STAGES_COLUMNS,
            "剧本阶段模板表",
            "stage_id",
            composite=(("script_id", "seq"),),
        )

    def load_dm_runs(self) -> pd.DataFrame:
        return self._load_table(
            dm_runs,
            DM_RUNS_COLUMNS,
            DM_RUNS_SPEC,
            [dm_runs.c.created_at, dm_runs.c.run_id],
        )

    def save_dm_runs(self, frame: pd.DataFrame) -> None:
        self._replace_table(
            dm_runs,
            frame,
            DM_RUNS_SPEC,
            DM_RUNS_COLUMNS,
            "DM 场控运行表",
            "run_id",
            composite=(("access_code_hash",),),
            skip_missing_composite=True,
        )

    def load_dm_run_events(self) -> pd.DataFrame:
        return self._load_table(
            dm_run_events,
            DM_RUN_EVENTS_COLUMNS,
            DM_RUN_EVENTS_SPEC,
            [dm_run_events.c.at, dm_run_events.c.event_id],
        )

    def save_dm_run_events(self, frame: pd.DataFrame) -> None:
        self._replace_table(
            dm_run_events,
            frame,
            DM_RUN_EVENTS_SPEC,
            DM_RUN_EVENTS_COLUMNS,
            "DM 场控事件表",
            "event_id",
            composite=(("run_id", "event_id"),),
        )

    def find_dm_run_by_code_hash(self, code_hash: str) -> dict[str, Any] | None:
        """跨租户按场控码哈希定位 run；兑换时还不知道 tenant。"""
        target = str(code_hash or "").strip()
        if not target:
            return None
        query = select(dm_runs).where(dm_runs.c.access_code_hash == target).limit(1)
        with self.engine.connect() as conn:
            row = conn.execute(query).mappings().first()
        return dict(row) if row is not None else None

    # ------------------------------------------------------------------
    # 场控细粒度写：避免每按一次键就整表替换
    # ------------------------------------------------------------------
    def update_dm_run(
        self,
        run_id: str,
        values: dict[str, Any],
        *,
        expected_state_version: int | None = None,
    ) -> bool:
        allowed = {key: value for key, value in values.items() if key in DM_RUNS_COLUMNS}
        if not allowed:
            return True
        statement = (
            update(dm_runs)
            .where(
                dm_runs.c.tenant_id == self.tenant_id,
                dm_runs.c.run_id == run_id,
            )
            .values({key: to_db_value(value) for key, value in allowed.items()})
        )
        if expected_state_version is not None:
            statement = statement.where(
                dm_runs.c.state_version == int(expected_state_version)
            )
        with self.engine.begin() as conn:
            result = conn.execute(statement)
        return result.rowcount == 1

    def append_dm_run_event(self, record: dict[str, Any]) -> bool:
        row = {
            column: to_db_value(record.get(column))
            for column in DM_RUN_EVENTS_COLUMNS
        }
        row["tenant_id"] = self.tenant_id
        try:
            with self.engine.begin() as conn:
                conn.execute(insert(dm_run_events), [row])
        except IntegrityError:
            with self.engine.connect() as conn:
                exists = conn.execute(
                    select(dm_run_events.c.event_id).where(
                        dm_run_events.c.tenant_id == self.tenant_id,
                        dm_run_events.c.run_id == row.get("run_id"),
                        dm_run_events.c.event_id == row.get("event_id"),
                    )
                ).first()
            if exists is not None:
                return False
            raise
        return True

    # ------------------------------------------------------------------
    # 租户
    # ------------------------------------------------------------------
    def ensure_tenant(
        self,
        email_hash: str | None = None,
        display_name: str | None = None,
        *,
        probe: dict[str, bool | None] | None = None,
    ) -> None:
        """幂等创建租户行，并检测 tenant_id 前缀碰撞。

        tenant_id 取完整邮箱哈希的前 16 位；若另一条完整哈希占用了同一
        前缀，则拒绝，而不是把两个邮箱的数据混在一起。

        ``probe`` 是给调用方的回填出口：这里本来就 SELECT 了整行租户记录，
        把 seeded 状态一并取回，上层就不必为了 ``is_seeded()`` 再查一次。
        跨境部署下每条往返都是 55ms 量级，登录后的每个页面都要走这条路径。
        """
        normalized = (email_hash or "").strip().lower() or None
        if normalized is not None and len(normalized) != 64:
            raise ValueError("email_hash 必须是 64 位十六进制字符串")
        name = (display_name or "").strip() or None
        now = datetime.now(timezone.utc)

        try:
            with self.engine.begin() as conn:
                row = conn.execute(
                    select(
                        tenants.c.email_hash,
                        tenants.c.display_name,
                        tenants.c.seeded_at,
                    ).where(tenants.c.tenant_id == self.tenant_id)
                ).first()
                if row is None:
                    conn.execute(
                        insert(tenants).values(
                            tenant_id=self.tenant_id,
                            email_hash=normalized,
                            display_name=name,
                            created_at=now,
                        )
                    )
                    if probe is not None:
                        probe["seeded"] = False
                    return

                if probe is not None:
                    probe["seeded"] = row.seeded_at is not None

                existing_hash = row.email_hash
                if normalized and existing_hash and existing_hash != normalized:
                    raise TenantConflictError("租户标识冲突，请换一个账号")

                values: dict[str, Any] = {}
                if normalized and not existing_hash:
                    values["email_hash"] = normalized
                if name and not row.display_name:
                    values["display_name"] = name
                if values:
                    conn.execute(
                        update(tenants)
                        .where(tenants.c.tenant_id == self.tenant_id)
                        .values(**values)
                    )
        except IntegrityError:
            self._verify_existing_tenant(normalized)

    def _verify_existing_tenant(self, normalized: str | None) -> None:
        with self.engine.connect() as conn:
            row = conn.execute(
                select(tenants.c.email_hash).where(
                    tenants.c.tenant_id == self.tenant_id
                )
            ).first()
            if row is not None:
                if normalized and row.email_hash and row.email_hash != normalized:
                    raise TenantConflictError("租户标识冲突，请换一个账号")
                return
            other = None
            if normalized:
                other = conn.execute(
                    select(tenants.c.tenant_id).where(
                        tenants.c.email_hash == normalized
                    )
                ).first()
        if other is not None:
            raise TenantConflictError("租户邮箱哈希已被其他租户占用")
        raise TenantConflictError("租户创建冲突，请稍后重试")

    def is_seeded(self) -> bool:
        with self.engine.connect() as conn:
            row = conn.execute(
                select(tenants.c.seeded_at).where(
                    tenants.c.tenant_id == self.tenant_id
                )
            ).first()
        return bool(row is not None and row.seeded_at is not None)

    def mark_seeded(self, when: datetime | None = None) -> None:
        moment = when or datetime.now(timezone.utc)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        with self.engine.begin() as conn:
            conn.execute(
                update(tenants)
                .where(tenants.c.tenant_id == self.tenant_id)
                .values(seeded_at=moment.astimezone(timezone.utc))
            )

    # ------------------------------------------------------------------
    # 运维
    # ------------------------------------------------------------------
    def tenant_exists(self) -> bool:
        with self.engine.connect() as conn:
            row = conn.execute(
                select(tenants.c.tenant_id).where(
                    tenants.c.tenant_id == self.tenant_id
                )
            ).first()
        return row is not None

    def is_empty(self) -> bool:
        with self.engine.connect() as conn:
            for table in (
                sessions,
                bookings,
                scripts,
                rooms,
                dms,
                dm_shifts,
                session_assignments,
                booking_intake_refs,
                script_stages,
                dm_runs,
                dm_run_events,
            ):
                row = conn.execute(
                    select(table.c.tenant_id).where(
                        table.c.tenant_id == self.tenant_id
                    ).limit(1)
                ).first()
                if row is not None:
                    return False
        return True

    def ping(self) -> bool:
        try:
            with self.engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            return True
        except SQLAlchemyError:
            return False

    def close(self) -> None:
        # 引擎由应用生命周期管理，Store 不单独关闭共享连接池。
        return None


__all__ = ["SqlStore", "TenantConflictError", "to_db_value"]

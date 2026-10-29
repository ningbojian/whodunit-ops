"""Excel / 旧 SQLite → SQLite 或 Neon Postgres 的导入工具。

用法：

  python -m app.store.migrate --init
  python -m app.store.migrate --source data --tenant local --dry-run
  python -m app.store.migrate --source data --tenant local
  python -m app.store.migrate --source data/web_users/users --all --dry-run
  python -m app.store.migrate --state data/web_state.sqlite3 --state-only

规则：
  - 默认可重复执行：按主键合并，重复导入不产生重复行；
  - `--force` 才整表覆盖该租户的对应表；
  - 源目录里没有的表不动，避免只导入场次时误删剧本库；
  - 旧备份只有三个 xlsx 时，缺失的资源/草稿/阶段/场控 sidecar 按空表处理；
  - 同名不同 script_id 的剧本直接拒绝该租户导入，不自动合并；
  - 每次实际写入记录 import_runs（仅原有三张表的行数），dry-run 不写数据库。
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import pandas as pd
from dotenv import load_dotenv
from sqlalchemy import Engine, insert, select, update

from app.config import PROJECT_ROOT
from app.store.engine import (
    create_database_engine,
    ensure_schema,
    redact_database_url,
)
from app.store.schema import (
    access_requests,
    import_runs,
    usage_global_daily,
)
from app.store.sql import SqlStore, TenantConflictError
from app.tools.frame import FrameSpec, coerce_frame, load_frame
from app.tools.frame_specs import (
    BOOKING_REFS_SPEC,
    DM_RUNS_SPEC,
    DM_RUN_EVENTS_SPEC,
    SCRIPT_STAGES_SPEC,
)
from app.tools.resources import (
    ASSIGNMENTS_SPEC,
    DM_LEAVES_SPEC,
    DMS_SPEC,
    ROOMS_SPEC,
    SHIFTS_SPEC,
)
from app.tools.scripts import (
    coerce_scripts,
    load_scripts,
    normalize_script_name,
)
from app.tools.storage import (
    BOOKING_ACTIVE,
    coerce_bookings,
    coerce_sessions,
    load_bookings,
    load_sessions,
    make_booking_id,
    make_session_id_from_record,
)

UTC = timezone.utc
# 导入顺序：剧本与阶段模板先于场次，场次先于预约/排期/场控。
_FILES = {
    "scripts": "scripts.xlsx",
    "script_stages": "script_stages.xlsx",
    "rooms": "rooms.xlsx",
    "dms": "dms.xlsx",
    "dm_shifts": "dm_shifts.xlsx",
    "dm_leaves": "dm_leaves.xlsx",
    "sessions": "sessions.xlsx",
    "bookings": "bookings.xlsx",
    "assignments": "assignments.xlsx",
    "booking_refs": "booking_intake_refs.xlsx",
    "dm_runs": "dm_runs.xlsx",
    "dm_run_events": "dm_run_events.xlsx",
}

# name -> (spec, key, 需要补 now 的时间列)
_EXTRA_TABLES: dict[str, tuple[FrameSpec, str, tuple[str, ...]]] = {
    "script_stages": (SCRIPT_STAGES_SPEC, "stage_id", ("created_at", "updated_at")),
    "rooms": (ROOMS_SPEC, "room_id", ("created_at", "updated_at")),
    "dms": (DMS_SPEC, "dm_id", ("created_at", "updated_at")),
    "dm_shifts": (SHIFTS_SPEC, "shift_id", ("updated_at",)),
    "dm_leaves": (DM_LEAVES_SPEC, "leave_id", ("created_at", "updated_at")),
    "assignments": (ASSIGNMENTS_SPEC, "assignment_id", ("assigned_at", "updated_at")),
    "booking_refs": (BOOKING_REFS_SPEC, "ref_id", ("data_as_of", "imported_at")),
    "dm_runs": (DM_RUNS_SPEC, "run_id", ("created_at", "updated_at")),
    "dm_run_events": (DM_RUN_EVENTS_SPEC, "event_id", ("at", "created_at")),
}


class MigrationError(RuntimeError):
    """导入失败；消息可以直接展示给操作者。"""


# ===========================================================================
# 读取与校验
# ===========================================================================
def _now() -> datetime:
    return datetime.now(UTC)


def _as_business_moment(value: datetime) -> pd.Timestamp:
    moment = pd.Timestamp(value)
    if moment.tzinfo is not None:
        moment = moment.tz_convert("Asia/Shanghai").tz_localize(None)
    return moment.floor("s")


def _prepare_sessions(frame: pd.DataFrame, now: pd.Timestamp) -> pd.DataFrame:
    frame = coerce_sessions(frame)
    if frame.empty:
        return frame
    missing_start = frame["start_time"].isna()
    if missing_start.any():
        raise MigrationError("存在缺少 start_time 的场次，无法导入")
    frame["updated_at"] = frame["updated_at"].fillna(now)
    ids = frame["session_id"].fillna("").astype("string").str.strip()
    for index in frame.index[ids == ""]:
        generated = make_session_id_from_record(frame.loc[index])
        if not generated:
            raise MigrationError("存在无法生成 session_id 的场次")
        frame.at[index, "session_id"] = generated
    if frame["session_id"].duplicated().any():
        raise MigrationError("源场次表存在重复 session_id")
    return frame


def _prepare_bookings(frame: pd.DataFrame, now: pd.Timestamp) -> pd.DataFrame:
    frame = coerce_bookings(frame)
    if frame.empty:
        return frame
    frame["updated_at"] = frame["updated_at"].fillna(now)
    missing = frame["booked_at"].isna()
    if missing.any():
        raise MigrationError("存在缺少 booked_at 的预约，无法导入")
    ids = frame["booking_id"].fillna("").astype("string").str.strip()
    for index in frame.index[ids == ""]:
        generated = make_booking_id(
            str(frame.at[index, "session_id"]),
            str(frame.at[index, "player_name"]),
        )
        frame.at[index, "booking_id"] = generated
    if frame["booking_id"].duplicated().any():
        raise MigrationError("源预约表存在重复 booking_id")
    return frame


def _prepare_scripts(frame: pd.DataFrame, now: pd.Timestamp) -> pd.DataFrame:
    frame = coerce_scripts(frame)
    if frame.empty:
        return frame
    frame["created_at"] = frame["created_at"].fillna(now)
    frame["updated_at"] = frame["updated_at"].fillna(now)
    ids = frame["script_id"].fillna("").astype("string").str.strip()
    for index in frame.index[ids == ""]:
        frame.at[index, "script_id"] = uuid.uuid4().hex[:12]

    keys = frame["script_name"].map(
        lambda value: normalize_script_name(value).casefold()
    )
    named = keys.fillna("") != ""
    duplicated = keys[named][keys[named].duplicated(keep=False)]
    if not duplicated.empty:
        names = frame.loc[duplicated.index, "script_name"].astype(str).unique()
        raise MigrationError(
            "源剧本库存在同名不同档案：" + "、".join(names) + "；已停止导入，请先人工合并"
        )
    return frame


def _prepare_extra(
    frame: pd.DataFrame,
    spec: FrameSpec,
    key: str,
    datetime_columns: tuple[str, ...],
    now: pd.Timestamp,
) -> pd.DataFrame:
    clean = coerce_frame(frame, spec)
    if clean.empty:
        return clean
    for column in datetime_columns:
        if column in clean.columns:
            clean[column] = clean[column].fillna(now)
    ids = clean[key].fillna("").astype("string").str.strip()
    if (ids == "").any():
        raise MigrationError(f"{spec.name}存在缺少 {key} 的行，无法导入")
    clean[key] = ids
    if ids.duplicated().any():
        raise MigrationError(f"源{spec.name}存在重复 {key}")
    return clean


def _read_source(source: Path) -> dict[str, pd.DataFrame]:
    if not source.is_dir():
        raise MigrationError(f"源目录不存在：{source}")
    now = _as_business_moment(_now())
    result: dict[str, pd.DataFrame] = {}
    for name, filename in _FILES.items():
        path = source / filename
        if not path.exists():
            continue
        if name == "sessions":
            frame = _prepare_sessions(load_sessions(path), now)
        elif name == "bookings":
            frame = _prepare_bookings(load_bookings(path), now)
        elif name == "scripts":
            frame = _prepare_scripts(load_scripts(path), now)
        else:
            spec, key, datetime_columns = _EXTRA_TABLES[name]
            frame = _prepare_extra(
                load_frame(path, path, spec), spec, key, datetime_columns, now
            )
        result[name] = frame
    if not result:
        raise MigrationError(f"源目录里没有可导入的 xlsx：{source}")
    return result


# ===========================================================================
# 合并
# ===========================================================================
def _merge_frames(existing: pd.DataFrame, incoming: pd.DataFrame, key: str) -> pd.DataFrame:
    if existing.empty:
        return incoming
    if incoming.empty:
        return existing
    combined = pd.concat([existing, incoming], ignore_index=True)
    return combined.drop_duplicates(subset=[key], keep="last", ignore_index=True)


def _import_into_store(
    store: SqlStore,
    frames: dict[str, pd.DataFrame],
    *,
    force: bool,
) -> dict[str, int]:
    counts: dict[str, int] = {}
    if "sessions" in frames:
        incoming = frames["sessions"]
        target = incoming if force else _merge_frames(
            store.load_sessions(), incoming, "session_id"
        )
        store.save_sessions(coerce_sessions(target))
        counts["sessions"] = len(incoming)
    if "bookings" in frames:
        incoming = frames["bookings"]
        target = incoming if force else _merge_frames(
            store.load_bookings(), incoming, "booking_id"
        )
        store.save_bookings(coerce_bookings(target))
        counts["bookings"] = len(incoming)
    if "scripts" in frames:
        incoming = frames["scripts"]
        target = incoming if force else _merge_frames(
            store.load_scripts(), incoming, "script_id"
        )
        store.save_scripts(coerce_scripts(target))
        counts["scripts"] = len(incoming)
    for name, (spec, key, _datetime_columns) in _EXTRA_TABLES.items():
        if name not in frames:
            continue
        incoming = frames[name]
        existing = getattr(store, f"load_{name}")()
        target = incoming if force else _merge_frames(existing, incoming, key)
        getattr(store, f"save_{name}")(coerce_frame(target, spec))
        counts[name] = len(incoming)
    return counts


def _verify(store: SqlStore, frames: dict[str, pd.DataFrame], *, force: bool) -> list[str]:
    warnings: list[str] = []
    if "sessions" in frames:
        actual = len(store.load_sessions())
        expected = len(frames["sessions"])
        if force and actual != expected:
            raise MigrationError(f"校验失败：场次行数 {actual} != 源 {expected}")
        if not force and actual < expected:
            raise MigrationError(f"校验失败：场次行数 {actual} < 源 {expected}")
    if "bookings" in frames:
        actual = len(store.load_bookings())
        expected = len(frames["bookings"])
        if force and actual != expected:
            raise MigrationError(f"校验失败：预约行数 {actual} != 源 {expected}")
        if not force and actual < expected:
            raise MigrationError(f"校验失败：预约行数 {actual} < 源 {expected}")
        source_seats = int(
            frames["bookings"].loc[
                frames["bookings"]["status"] == BOOKING_ACTIVE, "seats"
            ].sum()
        )
        actual_seats = int(
            store.load_bookings()
            .loc[lambda df: df["status"] == BOOKING_ACTIVE, "seats"]
            .sum()
        )
        if force and actual_seats != source_seats:
            warnings.append(
                f"有效预约人数校验不一致：数据库 {actual_seats}，源 {source_seats}"
            )
    if "scripts" in frames:
        actual = len(store.load_scripts())
        expected = len(frames["scripts"])
        if force and actual != expected:
            raise MigrationError(f"校验失败：剧本行数 {actual} != 源 {expected}")
        if not force and actual < expected:
            raise MigrationError(f"校验失败：剧本行数 {actual} < 源 {expected}")
    for name in _EXTRA_TABLES:
        if name not in frames:
            continue
        actual = len(getattr(store, f"load_{name}")())
        expected = len(frames[name])
        if force and actual != expected:
            raise MigrationError(
                f"校验失败：{_EXTRA_TABLES[name][0].name}行数 {actual} != 源 {expected}"
            )
        if not force and actual < expected:
            raise MigrationError(
                f"校验失败：{_EXTRA_TABLES[name][0].name}行数 {actual} < 源 {expected}"
            )
    return warnings


# ===========================================================================
# 旧审批/用量 SQLite
# ===========================================================================
def _read_old_state(path: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if not path.exists():
        raise MigrationError(f"旧状态库不存在：{path}")
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    try:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        requests: list[dict[str, Any]] = []
        usage: list[dict[str, Any]] = []
        if "access_requests" in tables:
            requests = [dict(row) for row in conn.execute("SELECT * FROM access_requests")]
        if "usage_global_daily" in tables:
            usage = [
                dict(row)
                for row in conn.execute("SELECT * FROM usage_global_daily")
            ]
        return requests, usage
    finally:
        conn.close()


def _parse_old_dt(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    return datetime.fromisoformat(str(value))


def _import_state(engine: Engine, path: Path, *, force: bool) -> tuple[int, int]:
    requests, usage = _read_old_state(path)
    imported_requests = 0
    imported_usage = 0
    with engine.begin() as conn:
        for row in requests:
            values = {
                "id": row["id"],
                "email": row["email"],
                "email_hash": row["email_hash"],
                "display_name": row["display_name"],
                "status": row["status"],
                "created_at": _parse_old_dt(row["created_at"]),
                "updated_at": _parse_old_dt(row["updated_at"]),
                "approval_expires_at": _parse_old_dt(row["approval_expires_at"]),
                "approved_at": _parse_old_dt(row.get("approved_at")),
                "access_expires_at": _parse_old_dt(row.get("access_expires_at")),
                "approval_token_hash": row["approval_token_hash"],
                "access_token_hash": row.get("access_token_hash"),
                "browser_token_hash": row["browser_token_hash"],
                # 旧状态库里的这个字段是**裸 SHA-256**（IPv4 只有 2^32 个取值，
                # 库泄露后可以枚举反推地址）。导入时拿不到 IP 本身，没法按新密钥
                # 重算，所以直接丢弃。它只服务「同一 IP 每小时的申请上限」，计数
                # 窗口是 1 小时，历史值对新库没有用处，丢掉不影响限流。
                "requester_ip_hash": None,
                "email_error": row.get("email_error"),
            }
            existing = conn.execute(
                select(access_requests.c.id).where(
                    access_requests.c.id == values["id"]
                )
            ).first()
            if existing is None:
                conn.execute(insert(access_requests).values(**values))
                imported_requests += 1
            elif force:
                conn.execute(
                    update(access_requests)
                    .where(access_requests.c.id == values["id"])
                    .values(**values)
                )
                imported_requests += 1

        for row in usage:
            day = row["day"]
            turns = int(row["turns"])
            updated_at = _parse_old_dt(row["updated_at"]) or _now()
            existing = conn.execute(
                select(usage_global_daily.c.turns).where(
                    usage_global_daily.c.day == day
                )
            ).first()
            if existing is None:
                conn.execute(
                    insert(usage_global_daily).values(
                        day=day, turns=turns, updated_at=updated_at
                    )
                )
                imported_usage += 1
            else:
                # 导入不把当天计数改小。
                merged = max(int(existing.turns), turns)
                conn.execute(
                    update(usage_global_daily)
                    .where(usage_global_daily.c.day == day)
                    .values(turns=merged, updated_at=updated_at)
                )
                imported_usage += 1
    return imported_requests, imported_usage


# ===========================================================================
# 租户解析与主流程
# ===========================================================================
def _full_hash_for_tenant(tenant_id: str, state_db: Path | None) -> str | None:
    if state_db is None or len(tenant_id) != 16:
        return None
    if not state_db.exists():
        return None
    conn = sqlite3.connect(str(state_db))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT DISTINCT email_hash FROM access_requests "
            "WHERE email_hash LIKE ?",
            (tenant_id + "%",),
        ).fetchall()
        hashes = {
            str(row["email_hash"])
            for row in rows
            if str(row["email_hash"]).startswith(tenant_id)
        }
        return next(iter(hashes)) if len(hashes) == 1 else None
    except sqlite3.Error:
        return None
    finally:
        conn.close()


def _record_run(
    engine: Engine,
    *,
    run_id: str,
    source: str,
    tenant_id: str,
    started_at: datetime,
    status: str,
    counts: dict[str, int] | None = None,
    warnings: Iterable[str] = (),
) -> None:
    counts = counts or {}
    with engine.begin() as conn:
        existing = conn.execute(
            select(import_runs.c.id).where(import_runs.c.id == run_id)
        ).first()
        values = {
            "source": source,
            "tenant_id": tenant_id,
            "started_at": started_at,
            "finished_at": _now(),
            "status": status,
            "session_rows": int(counts.get("sessions", 0)),
            "booking_rows": int(counts.get("bookings", 0)),
            "script_rows": int(counts.get("scripts", 0)),
            "warnings": "\n".join(str(item) for item in warnings) or None,
        }
        if existing is None:
            conn.execute(
                insert(import_runs).values(id=run_id, **values)
            )
        else:
            conn.execute(
                update(import_runs)
                .where(import_runs.c.id == run_id)
                .values(**values)
            )


def _import_one(
    engine: Engine,
    source: Path,
    tenant_id: str,
    *,
    email_hash: str | None,
    force: bool,
    dry_run: bool,
) -> dict[str, Any]:
    if not tenant_id or len(tenant_id) > 32:
        raise MigrationError(f"租户 ID 不合法：{tenant_id!r}")
    frames = _read_source(source)
    store = SqlStore(engine, tenant_id)
    plan = {name: len(frame) for name, frame in frames.items()}

    if dry_run:
        return {
            "tenant_id": tenant_id,
            "source": str(source),
            "rows": plan,
            "warnings": ["dry-run：未写入数据库"],
            "dry_run": True,
        }

    started = _now()
    run_id = uuid.uuid4().hex
    _record_run(
        engine,
        run_id=run_id,
        source=str(source),
        tenant_id=tenant_id,
        started_at=started,
        status="running",
    )
    try:
        store.ensure_tenant(email_hash=email_hash)
        with store.write_lock():
            counts = _import_into_store(store, frames, force=force)
            warnings = _verify(store, frames, force=force)
        _record_run(
            engine,
            run_id=run_id,
            source=str(source),
            tenant_id=tenant_id,
            started_at=started,
            status="success",
            counts=counts,
            warnings=warnings,
        )
        return {
            "tenant_id": tenant_id,
            "source": str(source),
            "rows": counts,
            "warnings": warnings,
            "dry_run": False,
        }
    except Exception as exc:
        try:
            _record_run(
                engine,
                run_id=run_id,
                source=str(source),
                tenant_id=tenant_id,
                started_at=started,
                status="failed",
                warnings=[str(exc)],
            )
        except Exception:  # noqa: BLE001 - 审计失败不能盖住原始错误
            pass
        raise


def _iter_tenant_dirs(source: Path) -> list[Path]:
    if not source.is_dir():
        raise MigrationError(f"源目录不存在：{source}")
    return sorted(path for path in source.iterdir() if path.is_dir())


def run_migration(args: argparse.Namespace) -> int:
    if args.init:
        engine = _engine_from_args(args)
        ensure_schema(engine)
        print("建表完成。")
        engine.dispose()
        return 0

    if args.state_only:
        if not args.state:
            raise MigrationError("--state-only 需要 --state 指向旧 SQLite 文件")
        state_path = Path(args.state).expanduser().resolve()
        engine = _engine_from_args(args)
        ensure_schema(engine)
        requests, usage = (
            _import_state(engine, state_path, force=args.force)
            if not args.dry_run
            else (0, 0)
        )
        if args.dry_run:
            old_requests, old_usage = _read_old_state(state_path)
            print(
                f"dry-run：审批 {len(old_requests)} 行，用量 {len(old_usage)} 行；未写入。"
            )
        else:
            print(f"已导入审批 {requests} 行，用量 {usage} 行。")
        engine.dispose()
        return 0

    if not args.source:
        raise MigrationError("需要 --source，或使用 --init / --state-only")

    source = Path(args.source).expanduser().resolve()
    engine = _engine_from_args(args)
    ensure_schema(engine)
    state_db_arg = args.state_db or args.state
    state_db = (
        Path(state_db_arg).expanduser().resolve() if state_db_arg else None
    )
    results: list[dict[str, Any]] = []

    try:
        if args.all:
            if args.tenant:
                raise MigrationError("--all 与 --tenant 不能同时使用")
            targets = [
                (path.name, path, _full_hash_for_tenant(path.name, state_db))
                for path in _iter_tenant_dirs(source)
            ]
            if not targets:
                raise MigrationError(f"{source} 下没有租户目录")
        else:
            tenant_id = args.tenant or "local"
            targets = [
                (tenant_id, source, _full_hash_for_tenant(tenant_id, state_db))
            ]

        for tenant_id, path, email_hash in targets:
            try:
                result = _import_one(
                    engine,
                    path,
                    tenant_id,
                    email_hash=email_hash,
                    force=args.force,
                    dry_run=args.dry_run,
                )
                results.append(result)
            except (MigrationError, TenantConflictError, ValueError) as exc:
                results.append(
                    {
                        "tenant_id": tenant_id,
                        "source": str(path),
                        "error": str(exc),
                    }
                )
    finally:
        engine.dispose()

    for result in results:
        label = result["tenant_id"]
        if "error" in result:
            print(f"[失败] {label}: {result['error']}")
            continue
        rows = result["rows"]
        prefix = "dry-run" if result.get("dry_run") else "完成"
        summary = "，".join(f"{name} {count}" for name, count in rows.items())
        print(f"[{prefix}] {label}: {summary}")
        for warning in result.get("warnings", []):
            print(f"  - {warning}")

    return 1 if any("error" in item for item in results) else 0


def _engine_from_args(args: argparse.Namespace) -> Engine:
    # 显式参数优先于 .env：测试和本地演练传 --sqlite 时不能被生产连接串带走。
    if args.database_url:
        return create_database_engine(
            database_url=args.database_url,
            pool_size=args.pool_size,
            max_overflow=args.max_overflow,
        )
    if args.sqlite:
        return create_database_engine(sqlite_path=Path(args.sqlite).expanduser())
    # 迁移优先使用 Neon 的直连地址：PgBouncer 事务池不适合长时间 DDL。
    database_url = (
        os.environ.get("DATABASE_URL_UNPOOLED")
        or os.environ.get("DATABASE_URL")
        or None
    )
    if database_url:
        return create_database_engine(
            database_url=database_url,
            pool_size=args.pool_size,
            max_overflow=args.max_overflow,
        )
    raise MigrationError(
        "缺少数据库连接：设置 DATABASE_URL，或为本地测试使用 --sqlite"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.store.migrate",
        description="把 Excel / 旧 SQLite 状态导入 SQLite 或 Neon Postgres。",
    )
    parser.add_argument("--init", action="store_true", help="只建表")
    parser.add_argument("--source", help="Excel 所在目录")
    parser.add_argument("--tenant", help="单个租户 ID；默认 local")
    parser.add_argument("--all", action="store_true", help="导入源目录下每个子目录")
    parser.add_argument("--state", help="旧 web_state.sqlite3 路径")
    parser.add_argument("--state-db", help="用于反查完整邮箱哈希的旧状态库")
    parser.add_argument("--state-only", action="store_true", help="只导入审批与用量")
    parser.add_argument("--dry-run", action="store_true", help="只校验和统计，不写库")
    parser.add_argument("--force", action="store_true", help="覆盖该租户已有数据")
    parser.add_argument(
        "--database-url",
        help="数据库连接串；默认优先读 DATABASE_URL_UNPOOLED，再读 DATABASE_URL",
    )
    parser.add_argument("--sqlite", help="本地 SQLite 文件路径，测试用")
    parser.add_argument("--pool-size", type=int, default=3)
    parser.add_argument("--max-overflow", type=int, default=2)
    return parser


def main(argv: list[str] | None = None) -> int:
    load_dotenv(PROJECT_ROOT / ".env")
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return run_migration(args)
    except (MigrationError, TenantConflictError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 - 命令行的最终兜底
        # 不打印连接串；只显示异常类型与简短消息。
        print(
            f"导入失败：{type(exc).__name__}: {redact_database_url(exc)}",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["MigrationError", "build_parser", "main", "run_migration"]

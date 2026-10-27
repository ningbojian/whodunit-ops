"""把数据库里的某个租户导出成 Excel 文件（备份 / 回滚用）。

用法：

  python -m app.store.export --tenant local --out backups/local
  python -m app.store.export --tenant <16位> --out backups/user --sqlite data/web.sqlite3

共 12 张表：场次、预约、剧本，以及资源、排期、请假、阶段模板、场控与事件。
导出不包含派生字段（booked_seats / remaining_seats），下次导入或打开时
由预约明细重新计算。
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Callable

import pandas as pd
from dotenv import load_dotenv

from app.config import PROJECT_ROOT
from app.store.engine import create_database_engine, redact_database_url
from app.store.sql import SqlStore
from app.tools.frame import save_frame
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
from app.tools.scripts import save_scripts
from app.tools.storage import save_bookings, save_sessions


class ExportError(RuntimeError):
    pass


# (计数 key, 文件名, 读取方法, 写入方法)
_TABLES: tuple[tuple[str, str, str, Callable[[pd.DataFrame, Path], None]], ...] = (
    ("sessions", "sessions.xlsx", "load_sessions",
     lambda frame, path: save_sessions(frame, path)),
    ("bookings", "bookings.xlsx", "load_bookings",
     lambda frame, path: save_bookings(frame, path)),
    ("scripts", "scripts.xlsx", "load_scripts",
     lambda frame, path: save_scripts(frame, path)),
    ("rooms", "rooms.xlsx", "load_rooms",
     lambda frame, path: save_frame(frame, path, path, ROOMS_SPEC)),
    ("dms", "dms.xlsx", "load_dms",
     lambda frame, path: save_frame(frame, path, path, DMS_SPEC)),
    ("dm_shifts", "dm_shifts.xlsx", "load_dm_shifts",
     lambda frame, path: save_frame(frame, path, path, SHIFTS_SPEC)),
    ("dm_leaves", "dm_leaves.xlsx", "load_dm_leaves",
     lambda frame, path: save_frame(frame, path, path, DM_LEAVES_SPEC)),
    ("assignments", "assignments.xlsx", "load_assignments",
     lambda frame, path: save_frame(frame, path, path, ASSIGNMENTS_SPEC)),
    ("booking_refs", "booking_intake_refs.xlsx", "load_booking_refs",
     lambda frame, path: save_frame(frame, path, path, BOOKING_REFS_SPEC)),
    ("script_stages", "script_stages.xlsx", "load_script_stages",
     lambda frame, path: save_frame(frame, path, path, SCRIPT_STAGES_SPEC)),
    ("dm_runs", "dm_runs.xlsx", "load_dm_runs",
     lambda frame, path: save_frame(frame, path, path, DM_RUNS_SPEC)),
    ("dm_run_events", "dm_run_events.xlsx", "load_dm_run_events",
     lambda frame, path: save_frame(frame, path, path, DM_RUN_EVENTS_SPEC)),
)


def export_tenant(
    *,
    database_url: str | None,
    sqlite_path: Path | None,
    tenant_id: str,
    out_dir: Path,
) -> dict[str, int]:
    if not tenant_id or len(tenant_id) > 32:
        raise ExportError("租户 ID 不合法")
    if database_url:
        engine = create_database_engine(database_url=database_url)
    elif sqlite_path is not None:
        engine = create_database_engine(sqlite_path=sqlite_path)
    else:
        raise ExportError("缺少数据库连接：DATABASE_URL 或 --sqlite")

    try:
        store = SqlStore(engine, tenant_id)
        if not store.tenant_exists():
            raise ExportError(f"租户不存在：{tenant_id}")

        out_dir.mkdir(parents=True, exist_ok=True)
        counts: dict[str, int] = {}
        for name, filename, loader, writer in _TABLES:
            frame: pd.DataFrame = getattr(store, loader)()
            writer(frame, out_dir / filename)
            counts[name] = len(frame)
        return counts
    finally:
        engine.dispose()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.store.export",
        description="把数据库租户导出为 Excel 备份。",
    )
    parser.add_argument("--tenant", required=True, help="租户 ID")
    parser.add_argument("--out", required=True, help="输出目录")
    parser.add_argument(
        "--database-url",
        help="数据库连接串；默认优先读 DATABASE_URL_UNPOOLED，再读 DATABASE_URL",
    )
    parser.add_argument("--sqlite", help="本地 SQLite 文件路径")
    return parser


def main(argv: list[str] | None = None) -> int:
    load_dotenv(PROJECT_ROOT / ".env")
    args = build_parser().parse_args(argv)
    # 显式参数优先；导出走直连地址，避免 PgBouncer 事务池上的长查询。
    if args.database_url:
        database_url = args.database_url
    elif args.sqlite:
        database_url = None
    else:
        database_url = (
            os.environ.get("DATABASE_URL_UNPOOLED")
            or os.environ.get("DATABASE_URL")
            or None
        )
    try:
        counts = export_tenant(
            database_url=database_url,
            sqlite_path=Path(args.sqlite).expanduser() if args.sqlite else None,
            tenant_id=args.tenant.strip(),
            out_dir=Path(args.out).expanduser().resolve(),
        )
    except Exception as exc:  # noqa: BLE001 - 命令行兜底
        print(
            f"导出失败：{type(exc).__name__}: {redact_database_url(exc)}",
            file=sys.stderr,
        )
        return 1
    summary = "，".join(f"{name} {count}" for name, count in counts.items())
    print(f"已导出到 {args.out}：{summary}。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["ExportError", "export_tenant", "main"]

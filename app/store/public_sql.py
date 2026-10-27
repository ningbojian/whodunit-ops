"""公共剧本库的 SQL 后端：全局表读写与种子灌入。

表结构见 app/store/schema.py 的 public_scripts（无 tenant_id）。
合并规则与 Excel 后端共用 public_library.apply_merge——两个后端
只有「读全表 / 写全表」不同，语义完全一致。表很小（几千行量级），
整表替换是可接受的写法（与 SqlStore.save_scripts 同风格）。
"""

from __future__ import annotations

import threading
from typing import Any

import pandas as pd
from sqlalchemy import Engine, delete, insert, select

from app.store.schema import public_scripts
from app.store.sql import to_db_value
from app.tools.public_library import (
    PUBLIC_COLUMNS,
    apply_merge,
    coerce_public_scripts,
    empty_public_scripts,
    ensure_public_seed,
)

_PUBLIC_DB_COLUMNS: tuple[str, ...] = (
    "script_id",
    "script_name",
    "aliases",
    "script_type",
    "total_seats",
    "duration_minutes",
    "list_price",
    "notes",
    "source_text",
    "origin",
    "created_at",
    "updated_at",
    "imported_at",
)

# 每个 engine 只做一次种子检查；跨进程并发下种子合并按 name_key 幂等。
_SEED_SQL_GUARD: set[int] = set()
_SEED_SQL_LOCK = threading.Lock()


class SqlPublicLibrary:
    """公共库的数据库后端；duck-type 与 Excel 后端对齐（load/merge）。"""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def load(self) -> pd.DataFrame:
        query = select(
            public_scripts.c.script_id,
            public_scripts.c.script_name,
            public_scripts.c.aliases,
            public_scripts.c.script_type,
            public_scripts.c.total_seats,
            public_scripts.c.duration_minutes,
            public_scripts.c.list_price,
            public_scripts.c.notes,
            public_scripts.c.source_text,
            public_scripts.c.origin,
            public_scripts.c.imported_at,
            public_scripts.c.created_at,
            public_scripts.c.updated_at,
        ).order_by(public_scripts.c.created_at, public_scripts.c.script_id)
        with self.engine.connect() as conn:
            rows = [dict(row) for row in conn.execute(query).mappings()]
        if not rows:
            return empty_public_scripts()
        return coerce_public_scripts(pd.DataFrame(rows))

    def merge(self, incoming: pd.DataFrame, *, now: Any = None) -> tuple[dict[str, int], list[dict[str, Any]]]:
        existing = self.load()
        merged, stats, errors = apply_merge(existing, incoming, now=now)
        if stats["added"] or stats["updated"]:
            self._write_all(merged)
        return stats, errors

    def _write_all(self, frame: pd.DataFrame) -> None:
        clean = coerce_public_scripts(frame)
        records: list[dict[str, Any]] = []
        for row in clean.to_dict("records"):
            name_key = (
                str(row.get("script_name") or "").strip().casefold() or None
            )
            record = {
                "name_key": name_key,
                **{column: to_db_value(row.get(column)) for column in _PUBLIC_DB_COLUMNS},
            }
            records.append(record)
        with self.engine.begin() as conn:
            conn.execute(delete(public_scripts))
            if records:
                conn.execute(insert(public_scripts), records)

    def ensure_seed(self, *, now: Any = None) -> dict[str, Any]:
        """空库时灌种子；进程级守卫 + 幂等合并。"""
        guard_key = id(self.engine)
        with _SEED_SQL_LOCK:
            if guard_key in _SEED_SQL_GUARD:
                return {
                    "seeded": False,
                    "reason": "already_checked",
                    "added": 0,
                    "updated": 0,
                    "skipped": 0,
                }
        if not self.load().empty:
            with _SEED_SQL_LOCK:
                _SEED_SQL_GUARD.add(guard_key)
            return {
                "seeded": False,
                "reason": "not_empty",
                "added": 0,
                "updated": 0,
                "skipped": 0,
            }
        result = _seed_from_csv(self, now=now)
        with _SEED_SQL_LOCK:
            _SEED_SQL_GUARD.add(guard_key)
        return result


def _seed_from_csv(library: SqlPublicLibrary, *, now: Any = None) -> dict[str, Any]:
    """把策展种子 CSV 灌进数据库公共库（复用 Excel 侧的种子读取）。"""
    from app.config import PUBLIC_SEED_CSV
    from app.tools.public_library import _read_seed_frame

    seed = PUBLIC_SEED_CSV
    if not seed.exists():
        return {
            "seeded": False,
            "reason": "seed_file_missing",
            "added": 0,
            "updated": 0,
            "skipped": 0,
        }
    seed_frame = _read_seed_frame(seed)
    stats, errors = library.merge(seed_frame, now=now)
    return {
        "seeded": True,
        "added": stats["added"],
        "updated": stats["updated"],
        "skipped": stats["skipped"],
        "errors": errors,
    }


def build_public_library(
    *,
    uses_database: bool,
    engine: Engine | None = None,
    path: Any = None,
) -> Any:
    """按存储模式返回公共库后端（load/merge/ensure_seed 鸭子类型）。"""
    if uses_database:
        if engine is None:
            raise ValueError("数据库模式需要 engine")
        return SqlPublicLibrary(engine)
    return ExcelPublicLibrary(path)


class ExcelPublicLibrary:
    """公共库的 Excel 后端（共享文件，与租户目录无关）。"""

    def __init__(self, path: Any = None) -> None:
        self._path = path

    def load(self) -> pd.DataFrame:
        from app.tools.public_library import load_public_scripts

        return load_public_scripts(self._path)

    def merge(self, incoming: pd.DataFrame, *, now: Any = None) -> tuple[dict[str, int], list[dict[str, Any]]]:
        from app.tools.public_library import merge_public_scripts

        return merge_public_scripts(incoming, path=self._path, now=now)

    def ensure_seed(self, *, now: Any = None) -> dict[str, Any]:
        return ensure_public_seed(path=self._path, now=now)


def reset_sql_seed_guard() -> None:
    """测试用：清空数据库侧种子守卫。"""
    with _SEED_SQL_LOCK:
        _SEED_SQL_GUARD.clear()


__all__ = [
    "PUBLIC_COLUMNS",
    "ExcelPublicLibrary",
    "SqlPublicLibrary",
    "build_public_library",
    "reset_sql_seed_guard",
]

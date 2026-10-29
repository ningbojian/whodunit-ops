"""全站每日模型轮数计数器。

为什么还需要它：
  用户获批后访问期内（默认 30 天）“不限轮数”，但如果账号被盗或页面脚本异常循环，
  仍然可能把 DeepSeek 额度刷穿。这里保留一个全站总闸：
  只有真正进入模型执行的一轮才预占一次；模型失败/超时不退还，
  避免用“故意失败”绕过总闸。

日期按 UTC+8 计算；SQLite 事务内检查并自增；数据库不可用时 fail closed。
"""

from __future__ import annotations

import math
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

UTC = timezone.utc
CST = timezone(timedelta(hours=8))


class UsageError(RuntimeError):
    """计数器不可用；调用方应返回 503，不能放行模型。"""


class UsageLimitReached(UsageError):
    def __init__(self, day: str, used: int, limit: int, retry_after_seconds: int) -> None:
        self.day = day
        self.used = used
        self.limit = limit
        self.retry_after_seconds = max(1, retry_after_seconds)
        super().__init__("全站今日模型轮数已用完")


@dataclass(frozen=True, slots=True)
class UsageReservation:
    day: str
    used: int
    limit: int
    remaining: int


def cst_day_key(now: datetime) -> str:
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    return now.astimezone(CST).strftime("%Y-%m-%d")


def _next_cst_midnight(now: datetime) -> datetime:
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    local = now.astimezone(CST)
    tomorrow = (local + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return tomorrow.astimezone(UTC)


class UsageStore:
    def __init__(
        self,
        db_path: str | Path,
        *,
        daily_limit: int,
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        if daily_limit < 1:
            raise ValueError("daily_limit 必须大于 0")
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.daily_limit = daily_limit
        self._now_fn = now_fn or (lambda: datetime.now(UTC))
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        with self._lock, self._conn:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS usage_global_daily (
                    day TEXT PRIMARY KEY,
                    turns INTEGER NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _now(self) -> datetime:
        value = self._now_fn()
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    def reserve(self) -> UsageReservation:
        """检查并自增一次；超过上限时抛 UsageLimitReached。"""
        now = self._now()
        day = cst_day_key(now)
        try:
            with self._lock, self._conn:
                row = self._conn.execute(
                    "SELECT turns FROM usage_global_daily WHERE day = ?",
                    (day,),
                ).fetchone()
                used = int(row["turns"]) if row else 0
                if used >= self.daily_limit:
                    retry = math.ceil(
                        (_next_cst_midnight(now) - now).total_seconds()
                    )
                    raise UsageLimitReached(day, used, self.daily_limit, retry)
                used += 1
                self._conn.execute(
                    """
                    INSERT INTO usage_global_daily(day, turns, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(day) DO UPDATE SET
                        turns = excluded.turns,
                        updated_at = excluded.updated_at
                    """,
                    (day, used, now.isoformat(timespec="seconds")),
                )
                return UsageReservation(
                    day=day,
                    used=used,
                    limit=self.daily_limit,
                    remaining=max(0, self.daily_limit - used),
                )
        except UsageLimitReached:
            raise
        except sqlite3.Error as exc:
            raise UsageError("每日用量数据库不可用") from exc
        except Exception as exc:  # noqa: BLE001 - 计数器必须 fail closed
            raise UsageError("每日用量计数器异常") from exc

    def current(self) -> tuple[str, int]:
        now = self._now()
        day = cst_day_key(now)
        try:
            with self._lock:
                row = self._conn.execute(
                    "SELECT turns FROM usage_global_daily WHERE day = ?",
                    (day,),
                ).fetchone()
                return day, int(row["turns"]) if row else 0
        except sqlite3.Error as exc:
            raise UsageError("每日用量数据库不可用") from exc

    def ping(self) -> bool:
        try:
            with self._lock:
                self._conn.execute("SELECT 1").fetchone()
            return True
        except sqlite3.Error:
            return False

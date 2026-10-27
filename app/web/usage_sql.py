"""全站每日模型轮数计数器（SQLite / Postgres 共用）。

`reserve()` 用单条原子 upsert + RETURNING 自增：
  - 未达上限才更新；
  - 达到上限时 UPDATE 不生效，RETURNING 没有行，调用方得到 UsageLimitReached；
  - 失败一律 fail closed，不放行模型。
"""

from __future__ import annotations

import math
import threading
from datetime import datetime, timezone
from typing import Callable

from sqlalchemy import Engine, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import SQLAlchemyError

from app.store.schema import usage_global_daily
from app.web.usage import (
    UsageError,
    UsageLimitReached,
    UsageReservation,
    _next_cst_midnight,
    cst_day_key,
)

UTC = timezone.utc


class DatabaseUsageStore:
    def __init__(
        self,
        engine: Engine,
        *,
        daily_limit: int,
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        if daily_limit < 1:
            raise ValueError("daily_limit 必须大于 0")
        self.engine = engine
        self.daily_limit = daily_limit
        self._now_fn = now_fn or (lambda: datetime.now(UTC))
        self._lock = threading.RLock()
        self._postgres = engine.dialect.name == "postgresql"

    def close(self) -> None:
        return None

    def _now(self) -> datetime:
        value = self._now_fn()
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    def reserve(self) -> UsageReservation:
        now = self._now()
        day = cst_day_key(now)
        try:
            with self._lock, self.engine.begin() as conn:
                if self._postgres:
                    base = pg_insert(usage_global_daily).values(
                        day=day, turns=1, updated_at=now
                    )
                else:
                    base = sqlite_insert(usage_global_daily).values(
                        day=day, turns=1, updated_at=now
                    )
                statement = base.on_conflict_do_update(
                    index_elements=["day"],
                    set_={
                        "turns": usage_global_daily.c.turns + 1,
                        "updated_at": now,
                    },
                    where=usage_global_daily.c.turns < self.daily_limit,
                ).returning(usage_global_daily.c.turns)
                row = conn.execute(statement).first()
                if row is None:
                    used = conn.execute(
                        select(usage_global_daily.c.turns).where(
                            usage_global_daily.c.day == day
                        )
                    ).scalar_one_or_none()
                    used = int(used or 0)
                    retry = math.ceil(
                        (_next_cst_midnight(now) - now).total_seconds()
                    )
                    raise UsageLimitReached(day, used, self.daily_limit, retry)
                used = int(row[0])
                return UsageReservation(
                    day=day,
                    used=used,
                    limit=self.daily_limit,
                    remaining=max(0, self.daily_limit - used),
                )
        except UsageLimitReached:
            raise
        except SQLAlchemyError as exc:
            raise UsageError("每日用量数据库不可用") from exc
        except Exception as exc:  # noqa: BLE001 - 计数器必须 fail closed
            raise UsageError("每日用量计数器异常") from exc

    def current(self) -> tuple[str, int]:
        now = self._now()
        day = cst_day_key(now)
        try:
            with self._lock, self.engine.connect() as conn:
                value = conn.execute(
                    select(usage_global_daily.c.turns).where(
                        usage_global_daily.c.day == day
                    )
                ).scalar_one_or_none()
                return day, int(value or 0)
        except SQLAlchemyError as exc:
            raise UsageError("每日用量数据库不可用") from exc

    def ping(self) -> bool:
        try:
            with self.engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            return True
        except Exception:  # noqa: BLE001 - 健康检查不能向上抛
            return False


__all__ = ["DatabaseUsageStore"]

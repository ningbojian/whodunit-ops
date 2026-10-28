"""数据库租户工作区：创建租户、首次播种、并发去重。

与 Excel 工作区不同，这里没有目录和 `.seeded` 文件：
  - `tenants.seeded_at` 是唯一的播种标记；
  - 业务表主键与 `tenant_id` 一起构成隔离边界；
  - 首次进入时只播种一次，已有数据（例如导入的旧工作区）绝不覆盖。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

import pandas as pd
from sqlalchemy import Engine

from app.store.sql import SqlStore, TenantConflictError
from app.tools.demo import seed_demo_store
from app.tools.scripts import upsert_script
from app.tools.storage import (
    STATUS_PENDING,
    load_sessions,
    upsert_booking,
    upsert_sessions,
)

UTC = timezone.utc
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")


class TenantWorkspaceError(RuntimeError):
    """租户工作区创建或播种失败。"""


class TenantCollisionError(TenantWorkspaceError):
    """邮箱哈希前缀碰撞，拒绝共用租户。"""


@dataclass(frozen=True, slots=True)
class TenantWorkspace:
    tenant_id: str
    store: SqlStore
    # 兼容 Excel 工作区对象的最小形状；数据库模式没有本地目录。
    directory: None = None


class DatabaseWorkspaceManager:
    def __init__(
        self,
        *,
        engine: Engine,
        default_store: str,
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        self.engine = engine
        self.default_store = default_store
        self._now_fn = now_fn or (lambda: datetime.now(UTC))

    @staticmethod
    def _key(email_hash: str) -> str:
        if not isinstance(email_hash, str) or not _HASH_RE.fullmatch(email_hash):
            raise TenantWorkspaceError("非法用户标识")
        return email_hash[:16]

    def store_for(self, email_hash: str) -> tuple[SqlStore, str]:
        key = self._key(email_hash)
        return SqlStore(self.engine, key), key

    def ensure(self, email_hash: str) -> TenantWorkspace:
        # ensure_tenant 本来就 SELECT 整行租户，顺手把 seeded 状态取回来，
        # 省掉紧随其后的一次 is_seeded() 查询。
        probe: dict[str, bool | None] = {"seeded": None}
        try:
            store, key = self.store_for(email_hash)
            store.ensure_tenant(email_hash=email_hash, probe=probe)
        except TenantConflictError as exc:
            raise TenantCollisionError(str(exc)) from None
        except TenantWorkspaceError:
            raise
        except Exception as exc:  # noqa: BLE001 - 统一映射为 web 503
            raise TenantWorkspaceError("租户创建失败") from exc

        try:
            with store.write_lock():
                seeded = probe["seeded"]
                if seeded is None:
                    # 只有在 ensure_tenant 走了 IntegrityError 回退时才会落到这里。
                    seeded = store.is_seeded()
                if seeded:
                    self._ensure_public_library()
                    return TenantWorkspace(tenant_id=key, store=store)
                if store.is_empty():
                    self._seed(store)
                # 有数据（旧工作区已导入）时只补标记，不覆盖。
                store.mark_seeded(self._now_fn())
            self._ensure_public_library()
            return TenantWorkspace(tenant_id=key, store=store)
        except TenantWorkspaceError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise TenantWorkspaceError("工作区初始化失败") from exc

    def _ensure_public_library(self) -> None:
        """公共库种子灌入（进程级守卫，只在库为空时写入）。

        失败只记 warning 不阻塞登录——公共库是增强能力，不是登录前置。
        """
        from app.store.public_sql import SqlPublicLibrary

        try:
            SqlPublicLibrary(self.engine).ensure_seed()
        except Exception:  # noqa: BLE001 - 种子失败不阻塞登录
            pass

    def _seed(self, store: SqlStore) -> None:
        now = self._now_fn()
        if now.tzinfo is None:
            now = now.replace(tzinfo=UTC)
        moment = pd.Timestamp(now).tz_convert("Asia/Shanghai").tz_localize(None)
        day = moment.normalize()
        if moment.hour >= 19:
            day = day + pd.Timedelta(days=1)

        rows = [
            {
                "store_name": self.default_store,
                "script_name": "雾隐山庄",
                "script_type": "推理",
                "start_time": (day + pd.Timedelta(hours=19)).strftime("%Y-%m-%d %H:%M"),
                "total_seats": 6,
                "list_price": 168,
                "status": STATUS_PENDING,
                "source_text": "演示数据：首次进入自动生成",
            },
            {
                "store_name": self.default_store,
                "script_name": "长夜将明",
                "script_type": "情感",
                "start_time": (day + pd.Timedelta(days=1, hours=14)).strftime("%Y-%m-%d %H:%M"),
                "total_seats": 7,
                "list_price": 188,
                "status": STATUS_PENDING,
                "source_text": "演示数据：首次进入自动生成",
            },
            {
                "store_name": self.default_store,
                "script_name": "第七码头",
                "script_type": "机制",
                "start_time": (day + pd.Timedelta(days=1, hours=20)).strftime("%Y-%m-%d %H:%M"),
                "total_seats": 8,
                "list_price": 198,
                "status": STATUS_PENDING,
                "source_text": "演示数据：首次进入自动生成",
            },
            {
                "store_name": self.default_store,
                "script_name": "山鬼",
                "script_type": "恐怖",
                "start_time": (day + pd.Timedelta(days=2, hours=19)).strftime("%Y-%m-%d %H:%M"),
                "total_seats": 6,
                "list_price": 178,
                "status": STATUS_PENDING,
                "source_text": "演示数据：首次进入自动生成",
            },
        ]
        durations = {
            "雾隐山庄": 240,
            "长夜将明": 270,
            "第七码头": 300,
            "山鬼": 240,
        }

        upsert_sessions(rows, store=store)
        for row in rows:
            upsert_script(
                script_name=row["script_name"],
                script_type=row["script_type"],
                total_seats=row["total_seats"],
                duration_minutes=durations.get(row["script_name"]),
                list_price=row["list_price"],
                source_text="演示数据：首次进入自动生成",
                now=moment,
                store=store,
            )
        seeded = load_sessions(store=store)
        first = seeded[seeded["script_name"] == "雾隐山庄"].iloc[0]
        second = seeded[seeded["script_name"] == "长夜将明"].iloc[0]
        upsert_booking(
            str(first["session_id"]),
            "阿青",
            seats=1,
            now=moment,
            store=store,
        )
        upsert_booking(
            str(second["session_id"]),
            "小满",
            seats=2,
            now=moment,
            store=store,
        )
        try:
            seed_demo_store(store, now=moment)
        except Exception:  # noqa: BLE001 - 演示资源失败不阻塞租户创建
            pass


__all__ = [
    "DatabaseWorkspaceManager",
    "TenantCollisionError",
    "TenantWorkspace",
    "TenantWorkspaceError",
]

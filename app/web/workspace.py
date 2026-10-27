"""每邮箱独立工作区：目录、Excel 路径、首次演示数据播种。

隔离边界：
  服务端只根据签名 Session 中的 email_hash 构造路径；
  前端请求体里没有邮箱、uid 或文件路径，无法指定别人的工作区。

播种规则：
  - 首次进入且三个 Excel 都不存在时，生成 4 个未来场次 + 2 条预约，
    并从这 4 个剧本生成同名的剧本档案（演示时长是合成值）；
  - 只要任一 Excel 已存在，就绝不覆盖，只补 `.seeded` 标记；
  - `.seeded` 标记避免每次请求重复播种。
"""

from __future__ import annotations

import os
import re
import threading
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import pandas as pd

from app.store.excel import ExcelStore
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
_TENANT_RE = re.compile(r"^[0-9a-f]{16}$")


class WorkspaceError(RuntimeError):
    """工作区创建或播种失败。"""


@dataclass(frozen=True, slots=True)
class WorkspacePaths:
    directory: Path
    sessions_path: Path
    bookings_path: Path
    scripts_path: Path
    marker_path: Path
    store: Any = None


class WorkspaceManager:
    def __init__(
        self,
        *,
        data_root: str | Path,
        default_store: str,
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        self.data_root = Path(data_root)
        self.default_store = default_store
        self._now_fn = now_fn or (lambda: datetime.now(UTC))
        self._lock = threading.RLock()
        self._ensured: set[str] = set()

    @staticmethod
    def _key(email_hash: str) -> str:
        if not isinstance(email_hash, str) or not _HASH_RE.fullmatch(email_hash):
            raise WorkspaceError("非法用户标识")
        return email_hash[:16]

    def paths_for(self, email_hash: str) -> WorkspacePaths:
        key = self._key(email_hash)
        directory = self.data_root / "users" / key
        return WorkspacePaths(
            directory=directory,
            sessions_path=directory / "sessions.xlsx",
            bookings_path=directory / "bookings.xlsx",
            scripts_path=directory / "scripts.xlsx",
            marker_path=directory / ".seeded",
        )

    def _with_store(self, paths: WorkspacePaths) -> WorkspacePaths:
        return replace(
            paths,
            store=ExcelStore(
                sessions_path=paths.sessions_path,
                bookings_path=paths.bookings_path,
                scripts_path=paths.scripts_path,
                tenant_id=paths.directory.name,
            ),
        )

    def store_for_tenant(self, tenant_id: str) -> ExcelStore:
        """按 16 位租户前缀定位已存在的 Excel 工作区（DM 兑换用）。

        DM token 不含完整邮箱哈希，只含 tenant_id；这里只允许已存在的
        目录，不创建新租户，也不触碰其他目录。
        """
        if not isinstance(tenant_id, str) or not _TENANT_RE.fullmatch(tenant_id):
            raise WorkspaceError("非法租户标识")
        directory = self.data_root / "users" / tenant_id
        if not directory.is_dir():
            raise WorkspaceError("租户工作区不存在")
        return ExcelStore(
            sessions_path=directory / "sessions.xlsx",
            bookings_path=directory / "bookings.xlsx",
            scripts_path=directory / "scripts.xlsx",
            tenant_id=tenant_id,
        )

    def find_store_by_code_hash(self, code_hash: str) -> Any:
        """Excel 模式没有全局索引，按目录扫描场控码哈希。"""
        from app.tools.dm_control import find_run_by_code_hash

        root = self.data_root / "users"
        if not root.is_dir():
            return None
        for directory in root.iterdir():
            if not directory.is_dir() or not _TENANT_RE.fullmatch(directory.name):
                continue
            if not (directory / "dm_runs.xlsx").exists():
                continue
            store = ExcelStore(
                sessions_path=directory / "sessions.xlsx",
                bookings_path=directory / "bookings.xlsx",
                scripts_path=directory / "scripts.xlsx",
                tenant_id=directory.name,
            )
            if find_run_by_code_hash(store, code_hash) is not None:
                return store
        return None

    def ensure(self, email_hash: str) -> WorkspacePaths:
        paths = self._with_store(self.paths_for(email_hash))
        key = self._key(email_hash)
        with self._lock:
            if key in self._ensured:
                return paths
            try:
                paths.directory.mkdir(parents=True, exist_ok=True)
                if paths.marker_path.exists():
                    self._ensured.add(key)
                    self._ensure_public_library()
                    return paths
                if (
                    paths.sessions_path.exists()
                    or paths.bookings_path.exists()
                    or paths.scripts_path.exists()
                ):
                    # 用户已有数据（或旧版本留下的数据），绝不重新播种。
                    paths.marker_path.write_text("existing\n", encoding="utf-8")
                    self._ensured.add(key)
                    self._ensure_public_library()
                    return paths

                self._seed(paths)
                paths.marker_path.write_text("seeded\n", encoding="utf-8")
                self._ensured.add(key)
                self._ensure_public_library()
                return paths
            except WorkspaceError:
                raise
            except Exception as exc:  # noqa: BLE001 - 统一映射为 503
                raise WorkspaceError("工作区初始化失败") from exc

    def _ensure_public_library(self) -> None:
        """公共库种子灌入（进程级守卫，只在库为空时写入）。

        公共库与租户目录无关：生产 data_root=data/web_users，公共库在
        data/ 根（data_root.parent）；测试传 tmp 目录时同样落在 tmp，互不污染。
        失败只记 warning 不阻塞登录——公共库是增强能力，不是登录前置。
        """
        from app.config import PUBLIC_SCRIPTS_XLSX
        from app.tools.public_library import ensure_public_seed

        try:
            ensure_public_seed(path=self.data_root.parent / PUBLIC_SCRIPTS_XLSX.name)
        except Exception:  # noqa: BLE001 - 种子失败不阻塞登录
            pass

    def _seed(self, paths: WorkspacePaths) -> None:
        now = self._now_fn()
        if now.tzinfo is None:
            now = now.replace(tzinfo=UTC)
        # 业务时间按 UTC+8 解释；Excel 不接受带时区的 datetime，
        # 播种写入的 booked_at/updated_at 统一转成不带时区的本地时间。
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

        # 剧本库的演示档案。时长是演示值；真实使用时由店长补充或修正。
        durations = {
            "雾隐山庄": 240,
            "长夜将明": 270,
            "第七码头": 300,
            "山鬼": 240,
        }

        tmp_sessions = paths.directory / ".sessions.seed.tmp.xlsx"
        tmp_bookings = paths.directory / ".bookings.seed.tmp.xlsx"
        tmp_scripts = paths.directory / ".scripts.seed.tmp.xlsx"
        try:
            upsert_sessions(rows, path=tmp_sessions)
            for row in rows:
                upsert_script(
                    script_name=row["script_name"],
                    script_type=row["script_type"],
                    total_seats=row["total_seats"],
                    duration_minutes=durations.get(row["script_name"]),
                    list_price=row["list_price"],
                    source_text="演示数据：首次进入自动生成",
                    path=tmp_scripts,
                    now=moment,
                )
            seeded = load_sessions(tmp_sessions)
            first = seeded[seeded["script_name"] == "雾隐山庄"].iloc[0]
            second = seeded[seeded["script_name"] == "长夜将明"].iloc[0]
            upsert_booking(
                str(first["session_id"]),
                "阿青",
                seats=1,
                path=tmp_bookings,
                now=moment,
            )
            upsert_booking(
                str(second["session_id"]),
                "小满",
                seats=2,
                path=tmp_bookings,
                now=moment,
            )
            os.replace(tmp_sessions, paths.sessions_path)
            os.replace(tmp_bookings, paths.bookings_path)
            os.replace(tmp_scripts, paths.scripts_path)
            # 资源、班次、阶段模板与演示分配；失败不让整个工作区不可用。
            try:
                seed_demo_store(paths.store, now=moment)
            except Exception:  # noqa: BLE001 - 演示数据失败不阻塞登录
                pass
        finally:
            for tmp in (tmp_sessions, tmp_bookings, tmp_scripts):
                try:
                    if tmp.exists():
                        tmp.unlink()
                except OSError:
                    pass

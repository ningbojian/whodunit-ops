"""Excel 后端的 Store 适配器。

保留原有 Excel 文件读写语义：本地 CLI、演示、离线测试和导入导出继续使用。
它不是 Web 生产环境的主存储。
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

import pandas as pd

from app.config import BOOKINGS_XLSX, SCRIPTS_XLSX, SESSIONS_XLSX
from app.tools import scripts as script_tools
from app.tools import storage as storage_tools
from app.tools.frame import load_frame, save_frame
from app.tools.frame_specs import (
    BOOKING_REFS_SPEC,
    DM_RUNS_SPEC,
    DM_RUN_EVENTS_SPEC,
    SCRIPT_STAGES_SPEC,
)
from app.tools.resources import (
    load_assignments,
    load_dm_leaves,
    load_dm_shifts,
    load_dms,
    load_rooms,
    save_assignments,
    save_dm_leaves,
    save_dm_shifts,
    save_dms,
    save_rooms,
)

_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()


def _lock_for(path: Path) -> threading.RLock:
    key = str(Path(path).resolve()).casefold()
    with _LOCKS_GUARD:
        lock = _LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _LOCKS[key] = lock
        return lock


@dataclass
class ExcelStore:
    sessions_path: Path = SESSIONS_XLSX
    bookings_path: Path = BOOKINGS_XLSX
    scripts_path: Path = SCRIPTS_XLSX
    tenant_id: str = "local"

    def _sidecar(self, name: str) -> Path:
        return Path(self.sessions_path).with_name(name)

    def load_sessions(self) -> pd.DataFrame:
        return storage_tools.load_sessions(self.sessions_path)

    def save_sessions(self, frame: pd.DataFrame) -> None:
        storage_tools.save_sessions(frame, self.sessions_path)

    def load_bookings(self) -> pd.DataFrame:
        return storage_tools.load_bookings(self.bookings_path)

    def save_bookings(self, frame: pd.DataFrame) -> None:
        storage_tools.save_bookings(frame, self.bookings_path)

    def load_scripts(self) -> pd.DataFrame:
        return script_tools.load_scripts(self.scripts_path)

    def save_scripts(self, frame: pd.DataFrame) -> None:
        script_tools.save_scripts(frame, self.scripts_path)

    # ------------------------------------------------------------------
    # sidecar：资源与排期
    # ------------------------------------------------------------------
    def load_rooms(self) -> pd.DataFrame:
        return load_rooms(self._sidecar("rooms.xlsx"))

    def save_rooms(self, frame: pd.DataFrame) -> None:
        save_rooms(frame, self._sidecar("rooms.xlsx"))

    def load_dms(self) -> pd.DataFrame:
        return load_dms(self._sidecar("dms.xlsx"))

    def save_dms(self, frame: pd.DataFrame) -> None:
        save_dms(frame, self._sidecar("dms.xlsx"))

    def load_dm_shifts(self) -> pd.DataFrame:
        return load_dm_shifts(self._sidecar("dm_shifts.xlsx"))

    def save_dm_shifts(self, frame: pd.DataFrame) -> None:
        save_dm_shifts(frame, self._sidecar("dm_shifts.xlsx"))

    def load_dm_leaves(self) -> pd.DataFrame:
        return load_dm_leaves(self._sidecar("dm_leaves.xlsx"))

    def save_dm_leaves(self, frame: pd.DataFrame) -> None:
        save_dm_leaves(frame, self._sidecar("dm_leaves.xlsx"))

    def load_assignments(self) -> pd.DataFrame:
        return load_assignments(self._sidecar("assignments.xlsx"))

    def save_assignments(self, frame: pd.DataFrame) -> None:
        save_assignments(frame, self._sidecar("assignments.xlsx"))

    # ------------------------------------------------------------------
    # sidecar：草稿、渠道单、阶段与场控
    # ------------------------------------------------------------------
    def load_booking_refs(self) -> pd.DataFrame:
        return load_frame(
            self._sidecar("booking_intake_refs.xlsx"),
            self._sidecar("booking_intake_refs.xlsx"),
            BOOKING_REFS_SPEC,
        )

    def save_booking_refs(self, frame: pd.DataFrame) -> None:
        save_frame(
            frame,
            self._sidecar("booking_intake_refs.xlsx"),
            self._sidecar("booking_intake_refs.xlsx"),
            BOOKING_REFS_SPEC,
        )

    def load_script_stages(self) -> pd.DataFrame:
        return load_frame(
            self._sidecar("script_stages.xlsx"),
            self._sidecar("script_stages.xlsx"),
            SCRIPT_STAGES_SPEC,
        )

    def save_script_stages(self, frame: pd.DataFrame) -> None:
        save_frame(
            frame,
            self._sidecar("script_stages.xlsx"),
            self._sidecar("script_stages.xlsx"),
            SCRIPT_STAGES_SPEC,
        )

    def load_dm_runs(self) -> pd.DataFrame:
        return load_frame(
            self._sidecar("dm_runs.xlsx"),
            self._sidecar("dm_runs.xlsx"),
            DM_RUNS_SPEC,
        )

    def save_dm_runs(self, frame: pd.DataFrame) -> None:
        save_frame(
            frame,
            self._sidecar("dm_runs.xlsx"),
            self._sidecar("dm_runs.xlsx"),
            DM_RUNS_SPEC,
        )

    def find_dm_run_by_code_hash(self, code_hash: str) -> dict[str, Any] | None:
        frame = self.load_dm_runs()
        if frame.empty:
            return None
        hit = frame[
            frame["access_code_hash"].astype("string").fillna("") == str(code_hash or "")
        ]
        return hit.iloc[0].to_dict() if not hit.empty else None

    def load_dm_run_events(self) -> pd.DataFrame:
        return load_frame(
            self._sidecar("dm_run_events.xlsx"),
            self._sidecar("dm_run_events.xlsx"),
            DM_RUN_EVENTS_SPEC,
        )

    def save_dm_run_events(self, frame: pd.DataFrame) -> None:
        save_frame(
            frame,
            self._sidecar("dm_run_events.xlsx"),
            self._sidecar("dm_run_events.xlsx"),
            DM_RUN_EVENTS_SPEC,
        )

    @contextmanager
    def write_lock(self) -> Iterator[None]:
        # Excel 只保证同进程串行；跨进程写同一文件仍不在本期范围。
        with _lock_for(self.sessions_path):
            yield

    def ping(self) -> bool:
        return True

    def close(self) -> None:
        return None

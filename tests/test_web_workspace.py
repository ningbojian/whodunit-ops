"""每邮箱工作区与演示数据播种的离线测试。"""

from __future__ import annotations

import threading
from datetime import datetime, timezone

import pandas as pd
import pytest

from app.tools.scripts import load_scripts, script_to_dict, upsert_script
from app.tools.storage import (
    STATUS_PENDING,
    load_bookings,
    load_sessions,
    upsert_sessions,
)
from app.web.security import email_hash
from app.web.serialization import snapshot_for_paths
from app.web.workspace import WorkspaceError, WorkspaceManager
from tests.web_helpers import MutableClock

UTC = timezone.utc


def _manager(tmp_path, clock):
    return WorkspaceManager(
        data_root=tmp_path / "web_users",
        default_store="迷雾剧场",
        now_fn=clock.now,
    )


def test_paths_are_derived_from_email_hash(tmp_path):
    clock = MutableClock()
    manager = _manager(tmp_path, clock)
    digest = email_hash("user@example.com")

    paths = manager.paths_for(digest)

    assert paths.directory == tmp_path / "web_users" / "users" / digest[:16]
    assert paths.sessions_path.name == "sessions.xlsx"
    assert paths.bookings_path.name == "bookings.xlsx"
    assert paths.scripts_path.name == "scripts.xlsx"
    assert paths.marker_path.name == ".seeded"


def test_ensure_seeds_four_sessions_and_two_bookings(tmp_path):
    clock = MutableClock()
    manager = _manager(tmp_path, clock)

    paths = manager.ensure(email_hash("user@example.com"))

    sessions = load_sessions(paths.sessions_path)
    bookings = load_bookings(paths.bookings_path)
    assert len(sessions) == 4
    assert len(bookings) == 2
    assert set(sessions["script_name"]) == {"雾隐山庄", "长夜将明", "第七码头", "山鬼"}
    assert set(sessions["status"]) == {STATUS_PENDING}
    scripts = load_scripts(paths.scripts_path)
    assert set(scripts["script_name"]) == {"雾隐山庄", "长夜将明", "第七码头", "山鬼"}
    assert all(
        script_to_dict(row)["profile_status"] == "已完整"
        for _, row in scripts.iterrows()
    )
    assert paths.marker_path.exists()


def test_seeded_dates_are_future_store_local_dates(tmp_path):
    clock = MutableClock(datetime(2026, 10, 20, 4, 0, tzinfo=UTC))
    manager = _manager(tmp_path, clock)

    paths = manager.ensure(email_hash("user@example.com"))
    sessions = load_sessions(paths.sessions_path)
    starts = pd.to_datetime(sessions["start_time"])

    assert starts.min() >= pd.Timestamp("2026-10-20 12:00")
    assert starts.max() <= pd.Timestamp("2026-10-23 23:59")


def test_seed_is_idempotent_and_never_overwritten(tmp_path):
    clock = MutableClock()
    manager = _manager(tmp_path, clock)
    digest = email_hash("user@example.com")
    paths = manager.ensure(digest)
    before = paths.sessions_path.read_bytes()

    manager.ensure(digest)
    after = paths.sessions_path.read_bytes()

    assert before == after
    assert len(load_sessions(paths.sessions_path)) == 4


def test_existing_excel_is_preserved_without_marker(tmp_path):
    clock = MutableClock()
    manager = _manager(tmp_path, clock)
    digest = email_hash("existing@example.com")
    paths = manager.paths_for(digest)
    paths.directory.mkdir(parents=True)

    upsert_sessions(
        [
            {
                "store_name": "迷雾剧场",
                "script_name": "老数据",
                "script_type": "推理",
                "start_time": "2026-10-21 19:00",
                "total_seats": 6,
                "list_price": 168,
                "status": STATUS_PENDING,
            }
        ],
        path=paths.sessions_path,
    )

    returned = manager.ensure(digest)

    sessions = load_sessions(returned.sessions_path)
    assert len(sessions) == 1
    assert sessions.iloc[0]["script_name"] == "老数据"
    assert returned.marker_path.exists()


def test_existing_script_library_is_preserved_without_marker(tmp_path):
    clock = MutableClock()
    manager = _manager(tmp_path, clock)
    digest = email_hash("scripts@example.com")
    paths = manager.paths_for(digest)
    paths.directory.mkdir(parents=True)

    upsert_script(
        script_name="旧剧本",
        script_type="推理",
        total_seats=6,
        duration_minutes=240,
        path=paths.scripts_path,
    )

    returned = manager.ensure(digest)

    scripts = load_scripts(returned.scripts_path)
    assert len(scripts) == 1
    assert scripts.iloc[0]["script_name"] == "旧剧本"
    # 只要任一数据文件已存在就视为老工作区，不补齐其他演示文件。
    assert not returned.sessions_path.exists()
    assert not returned.bookings_path.exists()
    assert returned.marker_path.exists()


def test_two_emails_get_separate_workspaces(tmp_path):
    clock = MutableClock()
    manager = _manager(tmp_path, clock)

    first = manager.ensure(email_hash("first@example.com"))
    second = manager.ensure(email_hash("second@example.com"))

    assert first.directory != second.directory
    assert first.sessions_path.parent != second.sessions_path.parent
    assert first.scripts_path.parent != second.scripts_path.parent
    assert first.sessions_path.exists() and second.sessions_path.exists()
    assert first.scripts_path.exists() and second.scripts_path.exists()
    assert first.scripts_path != second.scripts_path


def test_same_email_reuses_workspace(tmp_path):
    clock = MutableClock()
    digest = email_hash("USER@example.com")
    first = _manager(tmp_path, clock).ensure(digest)
    second = _manager(tmp_path, clock).ensure(email_hash("user@example.com"))

    assert first.directory == second.directory


def test_invalid_email_hash_rejected(tmp_path):
    manager = _manager(tmp_path, MutableClock())

    with pytest.raises(WorkspaceError):
        manager.ensure("short")
    with pytest.raises(WorkspaceError):
        manager.paths_for("../escape")


def test_concurrent_ensure_same_user_is_safe(tmp_path):
    clock = MutableClock()
    manager = _manager(tmp_path, clock)
    digest = email_hash("parallel@example.com")
    errors: list[Exception] = []

    def worker():
        try:
            manager.ensure(digest)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(10)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    paths = manager.paths_for(digest)
    assert len(load_sessions(paths.sessions_path)) == 4
    assert len(load_bookings(paths.bookings_path)) == 2


def test_snapshot_only_exposes_safe_fields(tmp_path):
    clock = MutableClock()
    manager = _manager(tmp_path, clock)
    paths = manager.ensure(email_hash("user@example.com"))

    items = snapshot_for_paths(paths, now=pd.Timestamp("2026-10-20 18:00"), limit=3)

    assert len(items) == 3
    allowed = {
        "store_name",
        "script_name",
        "script_type",
        "start_time",
        "total_seats",
        "booked_seats",
        "remaining_seats",
        "list_price",
        "status",
    }
    for item in items:
        assert set(item) == allowed
        assert item["remaining_seats"] == item["total_seats"] - item["booked_seats"]
        assert item["store_name"] == "迷雾剧场"

"""把工作区数据转换成前端可安全展示的 JSON。

只暴露“剧本、时间、人数、价格、状态”这些店长本来就该看到的字段；
不返回内部财务指标，也不返回工具返回的原始 data。
"""

from __future__ import annotations

import math
from datetime import date, datetime
from typing import Any

import pandas as pd

from app.tools.expiry import with_derived_status
from app.tools.storage import (
    STATUS_PENDING,
    is_missing_value,
    load_sessions_with_counts,
)
from app.web.workspace import WorkspacePaths


def _jsonable(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if is_missing_value(value):
        return None
    if isinstance(value, (pd.Timestamp, datetime, date)):
        return pd.Timestamp(value).strftime("%Y-%m-%d %H:%M")
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    item = getattr(value, "item", None)
    if callable(item):
        try:
            return _jsonable(item())
        except (ValueError, AttributeError):
            pass
    return str(value)


def _int_or_none(value: Any) -> int | None:
    plain = _jsonable(value)
    if plain is None or isinstance(plain, bool):
        return None
    try:
        return int(plain)
    except (TypeError, ValueError):
        return None


def _float_or_none(value: Any) -> float | None:
    plain = _jsonable(value)
    if plain is None or isinstance(plain, bool):
        return None
    try:
        return round(float(plain), 2)
    except (TypeError, ValueError):
        return None


def _snapshot(sessions: pd.DataFrame, *, now: Any, limit: int) -> list[dict[str, Any]]:
    if sessions.empty:
        return []
    marked = with_derived_status(sessions, now=now)
    active = marked[marked["derived_status"] == STATUS_PENDING].copy()
    if active.empty:
        return []
    active = active.sort_values(["start_time", "session_id"], kind="stable").head(limit)

    items: list[dict[str, Any]] = []
    for row in active.to_dict("records"):
        items.append(
            {
                "store_name": _jsonable(row.get("store_name")),
                "script_name": _jsonable(row.get("script_name")),
                "script_type": _jsonable(row.get("script_type")),
                "start_time": _jsonable(row.get("start_time")),
                "total_seats": _int_or_none(row.get("total_seats")),
                "booked_seats": _int_or_none(row.get("booked_seats")),
                "remaining_seats": _int_or_none(row.get("remaining_seats")),
                "list_price": _float_or_none(row.get("list_price")),
                "status": _jsonable(row.get("derived_status")),
            }
        )
    return items


def snapshot_for_paths(
    paths: WorkspacePaths,
    *,
    now: Any = None,
    limit: int = 8,
) -> list[dict[str, Any]]:
    """Excel 模式：从工作区路径读取后渲染。"""
    sessions = load_sessions_with_counts(paths.sessions_path, paths.bookings_path)
    return _snapshot(sessions, now=now, limit=limit)


def snapshot_for_store(
    store: Any,
    *,
    now: Any = None,
    limit: int = 8,
) -> list[dict[str, Any]]:
    """数据库模式：只通过租户 Store 读取，绝不跨租户。"""
    sessions = load_sessions_with_counts(store=store)
    return _snapshot(sessions, now=now, limit=limit)

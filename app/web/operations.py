"""商家侧运营服务。

Web Cookie 路由和 Flutter Bearer 路由都调用这一层，避免复制草稿、排期和
容量校验逻辑。所有方法只接受 email_hash，不接受请求体里的租户或路径。
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

import pandas as pd

from app.integrations.hardware import build_hardware_adapter
from app.tools import dm_control
from app.tools.assignment import (
    auto_assign as auto_assign_sessions,
    list_schedule,
    load_schedule_context,
    resolve_conflict as resolve_schedule_conflict,
)
from app.tools.demo import seed_demo_resources as seed_demo_resource_rows
from app.tools.demo import seed_demo_stages as seed_demo_stage_rows
from app.tools.frame import coerce_frame, json_safe
from app.tools.frame_specs import BOOKING_REFS_SPEC
from app.tools.intake import (
    SOURCE_DIANPING,
    commit_import,
    parse_upload,
    quick_booking as quick_booking_tool,
    validate_dianping_rows,
)
from app.tools.resources import (
    _upsert_dms_impl,
    _upsert_rooms_impl,
    delete_dm_leave,
    dm_script_history,
    dms_to_dict,
    load_assignments,
    load_dms,
    load_dm_leaves,
    load_dm_shifts,
    load_rooms,
    replace_assignments,
    replace_dm_shifts,
    rooms_to_dict,
    schedule_dm_inputs,
    shifts_to_dict,
    setup_checklist,
    upsert_dm_leave,
    upsert_dms,
    upsert_rooms,
)
from app.tools.scheduling import DEFAULT_DURATION_MINUTES, build_schedule
from app.tools.scripts import load_scripts
from app.tools.session_intake import (
    create_session_from_fields as create_session_tool,
    update_session as update_session_tool,
)
from app.tools.stages import (
    StageTemplateError,
    get_stages,
    sample_stages,
    save_stages as save_stages_tool,
)
from app.tools.storage import (
    load_bookings,
    load_sessions_with_counts,
)

UTC = timezone.utc
BUSINESS_TZ = "Asia/Shanghai"


class OperationsError(ValueError):
    """可映射为 JSON 信封的业务错误。"""

    def __init__(self, code: str, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


def _text(value: Any) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    try:
        if pd.isna(value):
            return ""
    except (TypeError, ValueError):
        pass
    return str(value).strip()


def _as_bool(value: Any, default: bool = True) -> bool:
    if value is None or _text(value).casefold() in {"", "nan", "none"}:
        return default
    if isinstance(value, bool):
        return value
    return _text(value).casefold() in {"1", "true", "yes", "y", "是", "启用", "on"}


def _to_int(value: Any) -> int | None:
    text = _text(value)
    if not text:
        return None
    try:
        return int(float(text))
    except (TypeError, ValueError):
        return None


def _to_float(value: Any) -> float | None:
    text = _text(value)
    if not text:
        return None
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def _header_key(value: Any) -> str:
    """CSV 表头归一化：去空白/下划线/连字符后 casefold（与 intake._norm_header 一致）。"""
    return re.sub(r"[\s_\-]+", "", str(value or "")).casefold()


def _start_text(value: Any) -> str:
    """场次开始时间 → 「YYYY-MM-DD HH:MM」（与 shortage.start 同格式）。"""
    if value is None or _text(value) == "" or pd.isna(value):
        return ""
    return pd.Timestamp(value).strftime("%Y-%m-%d %H:%M")


# 每个模板允许的逻辑列别名（归一化后的表头 -> 逻辑键）。
# 中文表头是新模板；英文别名继续兼容旧模板与 xlsx sidecar 回导。
_ROOM_HEADER_ALIASES: dict[str, set[str]] = {
    "room_id": {"房间编号", "房间id", "room_id", "roomid", "id"},
    "name": {"房间名称", "房间名", "名称", "name"},
    "capacity": {"可容纳人数", "容量", "人数上限", "capacity"},
    "tags": {"标签", "tags", "tag"},
    "hourly_cost": {"每小时成本", "场地成本", "hourly_cost", "hourlycost", "cost"},
    "buffer_minutes": {"缓冲分钟", "间隔分钟", "buffer_minutes", "bufferminutes", "buffer"},
    "active": {"是否启用", "启用", "active", "enabled"},
    "notes": {"备注", "notes", "note"},
}
_DM_HEADER_ALIASES: dict[str, set[str]] = {
    "dm_id": {"DM编号", "dm_id", "dmid", "id"},
    "name": {"DM名称", "DM名", "DM姓名", "姓名", "名称", "dm_name", "dmname", "name"},
    "skills": {"擅长类型", "技能", "skills", "skill"},
    "hourly_cost": {"每小时成本", "DM成本", "hourly_cost", "hourlycost", "cost"},
    "max_sessions_per_day": {
        "每日最多场次",
        "每日上限",
        "max_sessions_per_day",
        "maxsessionsperday",
        "max_sessions",
    },
    "active": {"是否启用", "启用", "active", "enabled"},
    "notes": {"备注", "notes", "note"},
}
_SHIFT_HEADER_ALIASES: dict[str, set[str]] = {
    "dm_name": {"DM名称", "DM名", "DM姓名", "dm_name", "dmname"},
    "weekday": {"星期", "周几", "weekday"},
    "start": {"上班时间", "开始时间", "start"},
    "end": {"下班时间", "结束时间", "end"},
}
_HEADER_ALIASES: dict[str, dict[str, set[str]]] = {
    "rooms": _ROOM_HEADER_ALIASES,
    "dms": _DM_HEADER_ALIASES,
    "shifts": _SHIFT_HEADER_ALIASES,
}
# 每个模板导入前必须能映射到的逻辑键（缺则整份拒绝，不做行级猜测）。
_IMPORT_REQUIRED: dict[str, tuple[tuple[str, str], ...]] = {
    "rooms": (("name", "房间名称"),),
    "dms": (("name", "DM名称"),),
    "shifts": (
        ("dm_name", "DM名称"),
        ("weekday", "星期"),
        ("start", "上班时间"),
        ("end", "下班时间"),
    ),
}


def _resolve_columns(
    columns: Iterable[Any], kind: str
) -> dict[str, str]:
    """把表头映射到逻辑键；未知列忽略；同逻辑键取先出现的列。"""
    aliases = _HEADER_ALIASES.get(kind, {})
    lookup: dict[str, str] = {}
    for logical, options in aliases.items():
        for option in options:
            lookup.setdefault(_header_key(option), logical)
    resolved: dict[str, str] = {}
    for column in columns:
        logical = lookup.get(_header_key(column))
        if logical and logical not in resolved:
            resolved[logical] = str(column)
    return resolved


def _clean_scene(value: Any) -> Any:
    if isinstance(value, dict):
        return value
    if not _text(value):
        return None
    return value


class OperationsService:
    def __init__(
        self,
        *,
        settings: Any,
        workspace: Any,
        now_fn: Callable[[], datetime] | None = None,
        public_library: Any = None,
    ) -> None:
        self.settings = settings
        self._workspace = workspace
        self._now_fn = now_fn or (lambda: datetime.now(UTC))
        # 公共剧本库后端（load() -> DataFrame）；None 时各视图仅用私有库。
        self._public_library = public_library

    def _public_scripts_frame(self) -> pd.DataFrame | None:
        """加载公共库帧；失败/为空返回 None（不阻塞页面）。"""
        if self._public_library is None:
            return None
        try:
            frame = self._public_library.load()
        except Exception:  # noqa: BLE001 - 公共库读失败不阻塞页面
            return None
        if frame is None or getattr(frame, "empty", True):
            return None
        return frame

    def _merged_scripts(self, store: Any) -> pd.DataFrame:
        """私有库 + 公共库遮蔽合并（仅读路径；写入仍只进私有库）。"""
        from app.tools.public_library import merge_with_shadow

        private = load_scripts(store=store)
        public = self._public_scripts_frame()
        if public is None:
            return private
        return merge_with_shadow(private, public)

    # ------------------------------------------------------------------
    # 时间与租户
    # ------------------------------------------------------------------
    def business_now(self) -> pd.Timestamp:
        moment = pd.Timestamp(self._now_fn())
        if moment.tzinfo is not None:
            moment = moment.tz_convert(BUSINESS_TZ).tz_localize(None)
        return moment.floor("s")

    def utc_now(self) -> pd.Timestamp:
        moment = pd.Timestamp(self._now_fn())
        if moment.tzinfo is not None:
            moment = moment.tz_convert("UTC").tz_localize(None)
        return moment.floor("s")

    @property
    def default_store(self) -> str:
        return getattr(self.settings, "default_store", "迷雾剧场") or "迷雾剧场"

    def _store(self, email_hash: str) -> Any:
        return self._workspace.ensure(email_hash).store

    # ------------------------------------------------------------------
    # 资源
    # ------------------------------------------------------------------
    @staticmethod
    def _script_row(row: dict[str, Any]) -> dict[str, Any]:
        """单行剧本序列化（resources 页与 /api/scripts 共用）。"""
        from app.tools.public_library import is_public_script_id

        is_public = is_public_script_id(row.get("script_id"))
        return {
            "script_id": _text(row.get("script_id")),
            "script_name": _text(row.get("script_name")),
            "script_type": _text(row.get("script_type")) or None,
            "total_seats": json_safe(row.get("total_seats")),
            "duration_minutes": json_safe(row.get("duration_minutes")),
            "list_price": json_safe(row.get("list_price")),
            "aliases": _text(row.get("aliases")) or None,
            "source": "公共库" if is_public else "私有",
            "read_only": is_public,
        }

    def list_scripts(
        self,
        email_hash: str,
        *,
        q: str = "",
        script_type: str = "",
        seats_min: Any = None,
        seats_max: Any = None,
        duration_min: Any = None,
        duration_max: Any = None,
        source: str = "",
        page: Any = 1,
        page_size: Any = 50,
    ) -> dict[str, Any]:
        """剧本筛选 + 分页（5.8k 行纯 pandas 向量化，不缓存）。"""
        store = self._store(email_hash)
        return self._page_scripts(
            self._merged_scripts(store),
            q=q,
            script_type=script_type,
            seats_min=seats_min,
            seats_max=seats_max,
            duration_min=duration_min,
            duration_max=duration_max,
            source=source,
            page=page,
            page_size=page_size,
        )

    def _page_scripts(
        self,
        scripts: pd.DataFrame,
        *,
        q: str = "",
        script_type: str = "",
        seats_min: Any = None,
        seats_max: Any = None,
        duration_min: Any = None,
        duration_max: Any = None,
        source: str = "",
        page: Any = 1,
        page_size: Any = 50,
    ) -> dict[str, Any]:
        if scripts is None or scripts.empty:
            return {"items": [], "total": 0, "page": 1, "page_size": page_size}

        mask = pd.Series(True, index=scripts.index)
        needle = _text(q).strip().lower()
        if needle:
            names = scripts["script_name"].astype("string").fillna("").str.lower()
            aliases = (
                scripts["aliases"].astype("string").fillna("").str.lower()
                if "aliases" in scripts.columns
                else pd.Series("", index=scripts.index)
            )
            mask &= names.str.contains(needle, regex=False) | aliases.str.contains(
                needle, regex=False
            )
        wanted_type = _text(script_type).strip()
        if wanted_type:
            mask &= (
                scripts["script_type"].astype("string").fillna("") == wanted_type
            )
        mask &= self._numeric_mask(scripts, "total_seats", seats_min, seats_max)
        mask &= self._numeric_mask(
            scripts, "duration_minutes", duration_min, duration_max
        )
        wanted_source = _text(source).strip()
        if wanted_source in {"私有", "公共库"}:
            from app.tools.public_library import is_public_script_id

            is_public = scripts["script_id"].map(is_public_script_id)
            mask &= is_public if wanted_source == "公共库" else ~is_public

        filtered = scripts[mask]
        size = self._clamp_int(page_size, 10, 200, 50)
        current = max(self._clamp_int(page, 1, 10**9, 1), 1)
        total = int(len(filtered))
        pages = max((total + size - 1) // size, 1)
        if current > pages:
            current = pages
        start = (current - 1) * size
        items = [
            self._script_row(row)
            for row in filtered.iloc[start : start + size].to_dict("records")
        ]
        return {
            "items": items,
            "total": total,
            "page": current,
            "page_size": size,
            "pages": pages,
        }

    @staticmethod
    def _clamp_int(value: Any, low: int, high: int, default: int) -> int:
        try:
            number = int(value)
        except (TypeError, ValueError):
            return default
        return max(low, min(number, high))

    @staticmethod
    def _numeric_mask(
        frame: pd.DataFrame, column: str, low: Any, high: Any
    ) -> pd.Series:
        if low in (None, "") and high in (None, ""):
            return pd.Series(True, index=frame.index)
        if column not in frame.columns:
            return pd.Series(False, index=frame.index)
        values = pd.to_numeric(frame[column], errors="coerce")
        mask = pd.Series(True, index=frame.index)
        try:
            if low not in (None, ""):
                mask &= values >= float(low)
            if high not in (None, ""):
                mask &= values <= float(high)
        except (TypeError, ValueError):
            return pd.Series(False, index=frame.index)
        return mask.fillna(False)

    def resources(self, email_hash: str) -> dict[str, Any]:
        store = self._store(email_hash)
        rooms = load_rooms(store=store)
        dms = load_dms(store=store)
        shifts = load_dm_shifts(store=store)
        scripts = self._merged_scripts(store)
        stages = store.load_script_stages()
        sessions = load_sessions_with_counts(store=store)
        room_rows = [rooms_to_dict(row) for row in rooms.to_dict("records")]
        dm_rows = [dms_to_dict(row) for row in dms.to_dict("records")]
        shift_rows = [shifts_to_dict(row) for row in shifts.to_dict("records")]
        by_dm: dict[str, list[dict[str, Any]]] = {}
        for row in shift_rows:
            by_dm.setdefault(_text(row.get("dm_id")), []).append(row)
        # WP13B：请假 + 带本历史并进 DM 卡（历史不含 script_counts，控体积）。
        leaves = load_dm_leaves(store=store)
        leaves_by_dm: dict[str, list[dict[str, Any]]] = {}
        if leaves is not None and not leaves.empty:
            for row in leaves.sort_values("day").to_dict("records"):
                leaves_by_dm.setdefault(_text(row.get("dm_id")), []).append(
                    {
                        "leave_id": _text(row.get("leave_id")),
                        "day": _text(row.get("day")),
                        "note": _text(row.get("note")) or None,
                    }
                )
        history = dm_script_history(store=store)
        for item in dm_rows:
            item["shifts"] = sorted(
                by_dm.get(_text(item.get("dm_id")), []),
                key=lambda row: (int(row.get("weekday") or 0), int(row.get("start_minute") or 0)),
            )
            item["leaves"] = leaves_by_dm.get(_text(item.get("dm_id")), [])
            bucket = history.get(_text(item.get("dm_id"))) or {}
            item["past_runs"] = int(bucket.get("past_runs") or 0)
            item["distinct_scripts"] = int(bucket.get("distinct_scripts") or 0)
            item["recent_scripts"] = bucket.get("recent") or []
        script_rows = [
            self._script_row(row)
            for row in scripts.to_dict("records") if not scripts.empty
        ]
        checklist = setup_checklist(
            rooms=rooms,
            dms=dms,
            shifts=shifts,
            scripts=scripts,
            stages=stages,
            sessions=sessions,
        )
        return {
            "rooms": room_rows,
            "dms": dm_rows,
            "shifts": shift_rows,
            "scripts": script_rows,
            # 首屏分页兜底：无 JS 时资源页也能读第一页（筛选走 /api/scripts）。
            "scripts_page": self._page_scripts(scripts),
            "setup": checklist,
            "counts": {
                "rooms": len(room_rows),
                "dms": len(dm_rows),
                "scripts": len(script_rows),
                "sessions": 0 if sessions.empty else len(sessions),
            },
            "as_of": self.business_now().strftime("%Y-%m-%d %H:%M"),
        }

    def save_room(self, email_hash: str, payload: dict[str, Any]) -> dict[str, Any]:
        store = self._store(email_hash)
        name = _text(payload.get("name"))
        if not name:
            raise OperationsError("bad_request", "房间名不能为空。")
        clean: dict[str, Any] = {
            "name": name,
            "active": _as_bool(payload.get("active"), True),
        }
        if _text(payload.get("room_id")):
            clean["room_id"] = _text(payload.get("room_id"))
        capacity = _to_int(payload.get("capacity"))
        if capacity is not None:
            if capacity < 1 or capacity > 200:
                raise OperationsError("bad_request", "房间容量必须在 1..200 之间。")
            clean["capacity"] = capacity
        buffer_minutes = _to_int(payload.get("buffer_minutes"))
        if buffer_minutes is not None:
            if buffer_minutes < 0 or buffer_minutes > 120:
                raise OperationsError("bad_request", "翻台缓冲必须在 0..120 分钟之间。")
            clean["buffer_minutes"] = buffer_minutes
        hourly_cost = _to_float(payload.get("hourly_cost"))
        if hourly_cost is not None:
            if hourly_cost < 0:
                raise OperationsError("bad_request", "房间时薪不能为负数。")
            clean["hourly_cost"] = hourly_cost
        for key in ("tags", "notes"):
            if _text(payload.get(key)):
                clean[key] = _text(payload.get(key))[:500]
        rooms, warnings = upsert_rooms([clean], store=store, now=self.business_now())
        match = rooms[rooms["name"].astype("string").fillna("") == name]
        return {
            "room": rooms_to_dict(match.iloc[0].to_dict()) if not match.empty else None,
            "warnings": warnings,
            "resources": self.resources(email_hash),
        }

    def save_dm(self, email_hash: str, payload: dict[str, Any]) -> dict[str, Any]:
        store = self._store(email_hash)
        name = _text(payload.get("name"))
        if not name:
            raise OperationsError("bad_request", "DM 名不能为空。")
        clean: dict[str, Any] = {
            "name": name,
            "active": _as_bool(payload.get("active"), True),
        }
        if _text(payload.get("dm_id")):
            clean["dm_id"] = _text(payload.get("dm_id"))
        if _text(payload.get("skills")):
            clean["skills"] = _text(payload.get("skills"))[:120]
        hourly_cost = _to_float(payload.get("hourly_cost"))
        if hourly_cost is not None:
            if hourly_cost < 0:
                raise OperationsError("bad_request", "DM 时薪不能为负数。")
            clean["hourly_cost"] = hourly_cost
        max_sessions = _to_int(payload.get("max_sessions_per_day"))
        if max_sessions is not None:
            if max_sessions < 1 or max_sessions > 20:
                raise OperationsError("bad_request", "每日场次上限必须在 1..20 之间。")
            clean["max_sessions_per_day"] = max_sessions
        if _text(payload.get("notes")):
            clean["notes"] = _text(payload.get("notes"))[:500]
        dms, warnings = upsert_dms([clean], store=store, now=self.business_now())
        match = dms[dms["name"].astype("string").fillna("") == name]
        dm_id = _text(match.iloc[0].get("dm_id")) if not match.empty else ""
        if "shifts" in payload and dm_id:
            shifts = payload.get("shifts") or []
            try:
                replace_dm_shifts(
                    dm_id, shifts, store=store, now=self.business_now()
                )
            except ValueError as exc:
                raise OperationsError("invalid_shift", str(exc)) from None
        return {
            "dm": dms_to_dict(match.iloc[0].to_dict()) if not match.empty else None,
            "warnings": warnings,
            "resources": self.resources(email_hash),
        }

    def save_dm_leave(
        self, email_hash: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        """登记 DM 请假（幂等 by dm_id+day）。"""
        store = self._store(email_hash)
        dm_id = _text(payload.get("dm_id"))
        if not dm_id:
            raise OperationsError("bad_request", "缺少 dm_id。")
        day = _text(payload.get("day"))
        if not day:
            raise OperationsError("bad_request", "缺少请假日期。")
        try:
            result = upsert_dm_leave(
                dm_id,
                day,
                note=_text(payload.get("note")),
                store=store,
                now=self.business_now(),
            )
        except (TypeError, ValueError) as exc:
            raise OperationsError("bad_request", str(exc)) from None
        return {**result, "resources": self.resources(email_hash)}

    def drop_dm_leave(
        self, email_hash: str, leave_id: str
    ) -> dict[str, Any]:
        store = self._store(email_hash)
        try:
            result = delete_dm_leave(leave_id, store=store)
        except ValueError as exc:
            raise OperationsError("bad_request", str(exc)) from None
        if not result.get("removed"):
            raise OperationsError("not_found", "该请假记录不存在。")
        return {**result, "resources": self.resources(email_hash)}

    # ------------------------------------------------------------------
    # 资源导入
    # ------------------------------------------------------------------
    def resource_template(self, kind: str) -> tuple[str, str]:
        kind = str(kind or "").removesuffix(".csv")
        templates = {
            "rooms": (
                "rooms.csv",
                "房间编号,房间名称,可容纳人数,标签,每小时成本,缓冲分钟,是否启用,备注\n"
                ",A 房,6,推理/小房,0,15,是,靠窗\n",
            ),
            "dms": (
                "dms.csv",
                "DM编号,DM名称,擅长类型,每小时成本,每日最多场次,是否启用,备注\n"
                ",小北,推理/机制,0,3,是,夜班\n",
            ),
            "shifts": (
                "shifts.csv",
                "DM名称,星期,上班时间,下班时间\n"
                "小北,周一,12:00,23:30\n",
            ),
        }
        key = "rooms" if kind in {"room", "rooms"} else "dms" if kind in {"dm", "dms"} else "shifts"
        return templates[key]

    def import_resources(
        self,
        email_hash: str,
        kind: str,
        filename: str,
        content: bytes,
        *,
        confirm: bool = False,
    ) -> dict[str, Any]:
        store = self._store(email_hash)
        key = "rooms" if kind in {"room", "rooms"} else "dms" if kind in {"dm", "dms"} else "shifts" if kind in {"shift", "shifts"} else ""
        if not key:
            raise OperationsError("bad_request", "资源类型必须是 rooms / dms / shifts。")
        frame = parse_upload(filename, content, max_bytes=getattr(self.settings, "max_import_bytes", 2 * 1024 * 1024))
        columns = _resolve_columns(frame.columns, key)

        # 缺必需列提前报错（不逐行猜），提示可重新下载模板。
        required_labels = [label for _, label in _IMPORT_REQUIRED[key]]
        missing = [
            label
            for logical, label in _IMPORT_REQUIRED[key]
            if logical not in columns
        ]
        if missing:
            raise OperationsError(
                "missing_columns",
                "缺少必需列："
                f"{'、'.join(missing)}（模板表头：{'、'.join(required_labels)}）。可重新下载模板。",
            )

        errors: list[dict[str, Any]] = []
        warnings: list[str] = []

        def cell(row: pd.Series, name: str) -> str:
            column = columns.get(name)
            return _text(row.get(column)) if column else ""

        rows: list[dict[str, Any]] = []
        if key == "rooms":
            for number, (_, row) in enumerate(frame.iterrows(), start=2):
                name = cell(row, "name")
                if not name:
                    errors.append({"row": number, "error": "缺少房间名 name。"})
                    continue
                capacity = _to_int(cell(row, "capacity"))
                if capacity is not None and (capacity < 1 or capacity > 200):
                    errors.append({"row": number, "error": "capacity 必须在 1..200。"})
                    continue
                buffer_minutes = _to_int(cell(row, "buffer_minutes"))
                if buffer_minutes is not None and not 0 <= buffer_minutes <= 120:
                    errors.append({"row": number, "error": "buffer_minutes 必须在 0..120。"})
                    continue
                rows.append(
                    {
                        "room_id": cell(row, "room_id") or None,
                        "name": name,
                        "capacity": capacity,
                        "tags": cell(row, "tags") or None,
                        "hourly_cost": _to_float(cell(row, "hourly_cost")),
                        "buffer_minutes": buffer_minutes if buffer_minutes is not None else 15,
                        "active": _as_bool(cell(row, "active"), True),
                        "notes": cell(row, "notes") or None,
                        "source_text": f"资源导入:{Path(filename).name}",
                    }
                )
        elif key == "dms":
            for number, (_, row) in enumerate(frame.iterrows(), start=2):
                name = cell(row, "name")
                if not name:
                    errors.append({"row": number, "error": "缺少 DM 名 name。"})
                    continue
                max_sessions = _to_int(cell(row, "max_sessions_per_day"))
                if max_sessions is not None and not 1 <= max_sessions <= 20:
                    errors.append({"row": number, "error": "max_sessions_per_day 必须在 1..20。"})
                    continue
                rows.append(
                    {
                        "dm_id": cell(row, "dm_id") or None,
                        "name": name,
                        "skills": cell(row, "skills") or None,
                        "hourly_cost": _to_float(cell(row, "hourly_cost")),
                        "max_sessions_per_day": max_sessions,
                        "active": _as_bool(cell(row, "active"), True),
                        "notes": cell(row, "notes") or None,
                        "source_text": f"资源导入:{Path(filename).name}",
                    }
                )
        else:
            dms = load_dms(store=store)
            names = {
                _text(row.get("name")).casefold(): _text(row.get("dm_id"))
                for row in dms.to_dict("records")
            } if not dms.empty else {}
            for number, (_, row) in enumerate(frame.iterrows(), start=2):
                dm_name = cell(row, "dm_name")
                dm_id = names.get(dm_name.casefold())
                if not dm_id:
                    errors.append(
                        {"row": number, "error": f"找不到 DM：{dm_name or '（空）'}；请先建 DM。"}
                    )
                    continue
                try:
                    weekday = int(cell(row, "weekday"))
                except (TypeError, ValueError):
                    weekday_text = cell(row, "weekday")
                    mapping = {"周一": 0, "周二": 1, "周三": 2, "周四": 3, "周五": 4, "周六": 5, "周日": 6}
                    weekday = mapping.get(weekday_text, -1)
                if not 0 <= weekday <= 6:
                    errors.append({"row": number, "error": "weekday 必须是 0..6 或周一..周日。"})
                    continue
                start, end = cell(row, "start"), cell(row, "end")
                rows.append(
                    {
                        "dm_id": dm_id,
                        "dm_name": dm_name,
                        "weekday": weekday,
                        "start": start,
                        "end": end,
                    }
                )

        if errors:
            warnings.append("有行未通过校验，确认导入时只写入通过的行。")
        result = {
            "kind": key,
            "rows": rows,
            "errors": errors,
            "warnings": warnings,
            "total_rows": len(frame),
            "valid_rows": len(rows),
            "confirmed": False,
        }
        if not confirm:
            return result
        if not rows:
            raise OperationsError("invalid_import", "没有可导入的有效行。")
        with store.write_lock():
            if key == "rooms":
                _upsert_rooms_impl(rows, store=store, now=self.business_now())
            elif key == "dms":
                _upsert_dms_impl(rows, store=store, now=self.business_now())
            else:
                grouped: dict[str, list[dict[str, Any]]] = {}
                for row in rows:
                    grouped.setdefault(row["dm_id"], []).append(
                        {"weekday": row["weekday"], "start": row["start"], "end": row["end"]}
                    )
                for dm_id, shifts in grouped.items():
                    try:
                        replace_dm_shifts(
                            dm_id, shifts, store=store, now=self.business_now()
                        )
                    except ValueError as exc:
                        raise OperationsError("invalid_shift", str(exc)) from None
        result["confirmed"] = True
        result["counts"] = {"imported": len(rows), "errors": len(errors)}
        return result

    def seed_demo_resources(self, email_hash: str, *, confirm: bool = False) -> dict[str, Any]:
        if not confirm:
            raise OperationsError("confirm_required", "载入演示资源需要确认。")
        store = self._store(email_hash)
        result = seed_demo_resource_rows(
            store, now=self.business_now(), only_if_empty=True, assign=True
        )
        stages = seed_demo_stage_rows(
            store, now=self.business_now(), only_missing=True
        )
        return {"resources": result, "stages": stages, "resources_view": self.resources(email_hash)}

    # ------------------------------------------------------------------
    # 场次建场与修正
    # ------------------------------------------------------------------
    def update_session(
        self, email_hash: str, session_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        return update_session_tool(
            session_id=session_id,
            fields=payload,
            store=self._store(email_hash),
            now=self.business_now(),
        )

    def create_session(self, email_hash: str, payload: dict[str, Any]) -> dict[str, Any]:
        # 房间/DM 可选：先按原逻辑建场并自动分配，再用手动指定覆盖；
        # 手动失败不回滚场次，只显式报告（见 WP16-A 决策 7）。
        wanted_room_id = _text(payload.get("room_id"))
        wanted_dm_id = _text(payload.get("dm_id"))
        fields = {
            key: value
            for key, value in payload.items()
            if key
            in {
                "store_name",
                "script_name",
                "script_type",
                "start_time",
                "start_time_text",
                "total_seats",
                "list_price",
                "notes",
            }
        }
        store = self._store(email_hash)
        try:
            result = create_session_tool(
                fields=fields,
                source_text=_text(payload.get("source_text")) or None,
                default_store=self.default_store,
                store=store,
                now=self.business_now(),
                # Web 快速建场也要能命中公共库剧本（只借元数据，写入仍只进私有库）。
                public=self._public_scripts_frame(),
            )
        except ValueError as exc:
            raise OperationsError(
                getattr(exc, "code", None) or "bad_request", str(exc)
            ) from exc
        if result.get("status") in {"incomplete", "needs_confirmation"}:
            raise OperationsError(
                "incomplete_session",
                result.get("suggested_question") or "场次信息还不完整。",
            )
        if not wanted_room_id and not wanted_dm_id:
            result["manual_assign"] = {"status": "skipped"}
            return result
        session_id = _text((result.get("session") or {}).get("session_id"))
        try:
            self._apply_assignments_impl(
                store,
                [
                    {
                        "session_id": session_id,
                        "room_id": wanted_room_id or None,
                        "dm_id": wanted_dm_id or None,
                    }
                ],
                now=self.business_now(),
            )
        except OperationsError as exc:
            # 建场与自动分配已落库；手动指定失败只追加警告，不回滚。
            result["warnings"] = list(result.get("warnings") or []) + [
                f"已建场，但手动指定未生效：{exc.message}；已保留自动分配结果"
            ]
            result["manual_assign"] = {
                "status": "failed",
                "code": exc.code,
                "message": exc.message,
            }
        else:
            result["manual_assign"] = {
                "status": "applied",
                "room_id": wanted_room_id or None,
                "dm_id": wanted_dm_id or None,
            }
        return result

    # ------------------------------------------------------------------
    # 排期
    # ------------------------------------------------------------------
    def _window(
        self, start_from: Any = None, start_to: Any = None
    ) -> tuple[pd.Timestamp, pd.Timestamp]:
        now = self.business_now()
        start = pd.Timestamp(start_from) if start_from not in (None, "") else now
        end = (
            pd.Timestamp(start_to)
            if start_to not in (None, "")
            else now.normalize() + pd.Timedelta(days=7)
        )
        return start, end

    def schedule_view(
        self,
        email_hash: str,
        start_from: Any = None,
        start_to: Any = None,
        *,
        include_scripts: bool = True,
    ) -> dict[str, Any]:
        store = self._store(email_hash)
        start, end = self._window(start_from, start_to)
        listed = list_schedule(
            store, start_from=start, start_to=end, now=self.business_now()
        )
        rooms = {
            _text(row.get("room_id")): _text(row.get("name"))
            for row in load_rooms(store=store).to_dict("records")
        }
        dms = {
            _text(row.get("dm_id")): _text(row.get("name"))
            for row in load_dms(store=store).to_dict("records")
        }
        # 时长映射：看板（web 时间轴 / Flutter 时间轴）需要知道每场占多久。
        # 用剧本名精确匹配，缺失或非法时退回全局默认 240 分钟。
        scripts = self._merged_scripts(store)
        duration_map: dict[str, int] = {}
        if not scripts.empty:
            for row in scripts.to_dict("records"):
                name = _text(row.get("script_name"))
                if not name or name in duration_map:
                    continue
                try:
                    minutes = int(row.get("duration_minutes"))
                except (TypeError, ValueError):
                    continue
                if minutes > 0:
                    duration_map[name] = minutes
        for session in listed["sessions"]:
            session["room_name"] = rooms.get(_text(session.get("room_id")))
            session["dm_name"] = dms.get(_text(session.get("dm_id")))
            session["duration_minutes"] = duration_map.get(
                _text(session.get("script_name")), DEFAULT_DURATION_MINUTES
            )
        from app.tools.public_library import is_public_script_id

        # include_scripts=False（Web 看板刷新）不序列化全量合并剧本库；
        # scripts 帧仍要加载，时长映射依赖它。响应形状保持 "scripts": []。
        script_rows = [
            {
                "script_id": _text(row.get("script_id")),
                "script_name": _text(row.get("script_name")),
                "script_type": _text(row.get("script_type")) or None,
                "total_seats": json_safe(row.get("total_seats")),
                "duration_minutes": json_safe(row.get("duration_minutes")),
                "list_price": json_safe(row.get("list_price")),
                "source": "公共库" if is_public_script_id(row.get("script_id")) else "私有",
            }
            for row in scripts.to_dict("records")
        ] if include_scripts and not scripts.empty else []
        # WP13B：看板换 DM 弹层也要能看到请假与带本记录（不含 script_counts）。
        leaves = load_dm_leaves(store=store)
        leaves_by_dm: dict[str, list[dict[str, Any]]] = {}
        if leaves is not None and not leaves.empty:
            for row in leaves.sort_values("day").to_dict("records"):
                leaves_by_dm.setdefault(_text(row.get("dm_id")), []).append(
                    {
                        "leave_id": _text(row.get("leave_id")),
                        "day": _text(row.get("day")),
                        "note": _text(row.get("note")) or None,
                    }
                )
        history = dm_script_history(store=store)
        dm_rows = []
        for row in load_dms(store=store).to_dict("records"):
            item = dms_to_dict(row)
            dm_id = _text(item.get("dm_id"))
            bucket = history.get(dm_id) or {}
            item["leaves"] = leaves_by_dm.get(dm_id, [])
            item["past_runs"] = int(bucket.get("past_runs") or 0)
            item["distinct_scripts"] = int(bucket.get("distinct_scripts") or 0)
            item["recent_scripts"] = bucket.get("recent") or []
            dm_rows.append(item)
        return {
            "sessions": listed["sessions"],
            "scripts": script_rows,
            "rooms": [rooms_to_dict(row) for row in load_rooms(store=store).to_dict("records")],
            "dms": dm_rows,
            "as_of": listed["as_of"],
            "window": {
                "start": start.strftime("%Y-%m-%d %H:%M"),
                "end": end.strftime("%Y-%m-%d %H:%M"),
            },
        }

    def plan_schedule(
        self, email_hash: str, start_from: Any = None, start_to: Any = None
    ) -> dict[str, Any]:
        store = self._store(email_hash)
        start, end = self._window(start_from, start_to)
        context = load_schedule_context(store)
        leave_rows, familiarities = schedule_dm_inputs(store=store)
        result = build_schedule(
            sessions=context.sessions,
            rooms=context.rooms,
            dms=context.dms,
            shifts=context.shifts,
            assignments=context.assignments,
            scripts=context.scripts,
            public=self._public_scripts_frame(),
            now=self.business_now(),
            start_from=start,
            start_to=end,
            dm_leaves=leave_rows,
            familiarities=familiarities,
        )
        payload = result.to_dict()
        payload["as_of"] = payload.get("as_of")
        # 纯计算只读：方案附带 before/after 明细，写入由 /api/schedule/apply 完成。
        payload["proposals"] = self._proposal_rows(
            store, payload.get("assignments") or [], context=context, source="plan"
        )
        return payload

    def _proposal_rows(
        self,
        store: Any,
        items: list[dict[str, Any]],
        *,
        context: Any = None,
        source: str,
    ) -> list[dict[str, Any]]:
        """把分配条目渲染成「原值 → 新值」审核行（自动预览与优化方案共用）。

        before 取自 context.assignments + 房间/DM 名称映射（无分配则为空串）；
        after 取自条目本身（缺 script_name/start 时回退到场次帧）。
        """
        if context is None:
            context = load_schedule_context(store)
        rooms = {
            _text(row.get("room_id")): _text(row.get("name"))
            for row in context.rooms.to_dict("records")
        } if context.rooms is not None and not context.rooms.empty else {}
        dms = {
            _text(row.get("dm_id")): _text(row.get("name"))
            for row in context.dms.to_dict("records")
        } if context.dms is not None and not context.dms.empty else {}
        sessions_meta = (
            {
                _text(row.get("session_id")): row
                for row in context.sessions.to_dict("records")
            }
            if context.sessions is not None and not context.sessions.empty
            else {}
        )
        current: dict[str, dict[str, str]] = {}
        if context.assignments is not None and not context.assignments.empty:
            for row in context.assignments.to_dict("records"):
                sid = _text(row.get("session_id"))
                bucket = current.setdefault(
                    sid, {"room_id": "", "room_name": "", "dm_id": "", "dm_name": ""}
                )
                kind = _text(row.get("resource_kind"))
                if kind == "room":
                    bucket["room_id"] = _text(row.get("resource_id"))
                elif kind == "dm":
                    bucket["dm_id"] = _text(row.get("resource_id"))
        for bucket in current.values():
            bucket["room_name"] = rooms.get(bucket["room_id"], "") if bucket["room_id"] else ""
            bucket["dm_name"] = dms.get(bucket["dm_id"], "") if bucket["dm_id"] else ""

        proposals: list[dict[str, Any]] = []
        for item in items:
            sid = _text(item.get("session_id"))
            before = dict(
                current.get(sid)
                or {"room_id": "", "room_name": "", "dm_id": "", "dm_name": ""}
            )
            after_room_id = _text(item.get("room_id"))
            after_dm_id = _text(item.get("dm_id"))
            after = {
                "room_id": after_room_id,
                "room_name": _text(item.get("room_name"))
                or (rooms.get(after_room_id, "") if after_room_id else ""),
                "dm_id": after_dm_id,
                "dm_name": _text(item.get("dm_name"))
                or (dms.get(after_dm_id, "") if after_dm_id else ""),
            }
            meta = sessions_meta.get(sid) or {}
            proposals.append(
                {
                    "session_id": sid,
                    "script_name": _text(item.get("script_name"))
                    or _text(meta.get("script_name")),
                    "start": _text(item.get("start"))
                    or _start_text(meta.get("start_time")),
                    "before": before,
                    "after": after,
                    "changed": (
                        after_room_id != before["room_id"]
                        or after_dm_id != before["dm_id"]
                    ),
                    "source": source,
                }
            )
        return proposals

    def auto_assign(
        self,
        email_hash: str,
        session_ids: Iterable[str] | None = None,
        *,
        trigger: str = "web_auto",
        preview: bool = False,
    ) -> dict[str, Any]:
        store = self._store(email_hash)
        context = load_schedule_context(store)
        result = auto_assign_sessions(
            store,
            session_ids=session_ids,
            now=self.business_now(),
            trigger=trigger,
            dry_run=preview,
        )
        result["preview"] = preview
        if preview:
            # 预览只计算不写库；逐条「原值 → 新值」供前端勾选后走 /apply。
            result["proposals"] = self._proposal_rows(
                store, result.get("assigned") or [], context=context, source="auto"
            )
        return result

    def resolve_conflict(
        self, email_hash: str, session_id: str, choice: str, target_id: str | None = None
    ) -> dict[str, Any]:
        return resolve_schedule_conflict(
            self._store(email_hash),
            session_id=session_id,
            choice=choice,
            target_id=target_id,
            now=self.business_now(),
        )

    def _apply_assignments_impl(
        self,
        store: Any,
        assignments: list[dict[str, Any]],
        *,
        now: pd.Timestamp,
    ) -> dict[str, Any]:
        # 读取 + 校验 + 写入整体在同一把（可重入）写锁里，避免校验到写入之间
        # 被并发改动导致「看的是一套、写的是另一套」。
        with store.write_lock():
            context = load_schedule_context(store)
            sessions = context.sessions.copy()
            if sessions.empty:
                raise OperationsError("session_not_found", "场次不存在。")
            sessions["start_time"] = pd.to_datetime(sessions["start_time"], errors="coerce")
            by_id = {
                _text(row.get("session_id")): row
                for row in sessions.to_dict("records")
            }
            rooms = {
                _text(row.get("room_id")): row
                for row in context.rooms.to_dict("records")
            }
            dms = {
                _text(row.get("dm_id")): row
                for row in context.dms.to_dict("records")
            }
            scripts = {
                _text(row.get("script_name")): row
                for row in context.scripts.to_dict("records")
            }
            # 当前分配（预览过期保护的比对基线）。
            current_room: dict[str, str] = {}
            current_dm: dict[str, str] = {}
            if not context.assignments.empty:
                for row in context.assignments.to_dict("records"):
                    sid = _text(row.get("session_id"))
                    kind = _text(row.get("resource_kind"))
                    if kind == "room":
                        current_room[sid] = _text(row.get("resource_id"))
                    elif kind == "dm":
                        current_dm[sid] = _text(row.get("resource_id"))

            def label(session_id: str) -> str:
                """《剧本名》开始时间（缺则 session_id），便于勾选列表定位。"""
                row = by_id.get(session_id)
                if not row:
                    return session_id or "（空）"
                script = _text(row.get("script_name"))
                start = _start_text(row.get("start_time"))
                if script and start:
                    return f"《{script}》{start}"
                if script:
                    return f"《{script}》"
                return session_id

            targets: dict[str, dict[str, str]] = {}
            for item in assignments:
                session_id = _text(item.get("session_id"))
                if not session_id or session_id not in by_id:
                    raise OperationsError("session_not_found", f"场次不存在：{session_id or '（空）'}")
                if session_id in targets:
                    raise OperationsError("bad_request", f"{label(session_id)}：同一场次只能提交一条分配。")
                # 预览过期保护：键存在即校验（空串 = 当时确实没有分配）。
                if "expect_room_id" in item:
                    expected = _text(item.get("expect_room_id"))
                    if expected != current_room.get(session_id, ""):
                        raise OperationsError(
                            "preview_stale",
                            f"{label(session_id)} 的当前房间与预览时不一致，请重新生成预览。",
                            409,
                        )
                if "expect_dm_id" in item:
                    expected = _text(item.get("expect_dm_id"))
                    if expected != current_dm.get(session_id, ""):
                        raise OperationsError(
                            "preview_stale",
                            f"{label(session_id)} 的当前 DM 与预览时不一致，请重新生成预览。",
                            409,
                        )
                room_id = _text(item.get("room_id"))
                dm_id = _text(item.get("dm_id"))
                if not room_id and not dm_id:
                    raise OperationsError("bad_request", f"{label(session_id)}：换房或换 DM 至少要提供一个。")
                if room_id:
                    room = rooms.get(room_id)
                    if room is None or not _as_bool(room.get("active"), True):
                        raise OperationsError("resource_not_found", f"{label(session_id)}：房间不存在或已停用。")
                    total = _to_int(by_id[session_id].get("total_seats"))
                    capacity = _to_int(room.get("capacity"))
                    if capacity is None:
                        raise OperationsError("room_capacity_missing", f"{label(session_id)}：房间缺容量，无法分配。")
                    if total is not None and capacity < total:
                        raise OperationsError("room_capacity", f"{label(session_id)}：房间容量小于场次人数。")
                if dm_id:
                    dm = dms.get(dm_id)
                    if dm is None or not _as_bool(dm.get("active"), True):
                        raise OperationsError("resource_not_found", f"{label(session_id)}：DM 不存在或已停用。")
                targets[session_id] = {"room_id": room_id, "dm_id": dm_id}

            def duration(row: dict[str, Any]) -> int:
                script = scripts.get(_text(row.get("script_name")))
                value = _to_int(script.get("duration_minutes")) if script else None
                return value if value and value > 0 else 240

            # 现有分配中，不在本次目标里的行锁定为占用。
            occupied_rooms: dict[str, list[tuple[pd.Timestamp, pd.Timestamp]]] = {}
            occupied_dms: dict[str, list[tuple[pd.Timestamp, pd.Timestamp]]] = {}
            if not context.assignments.empty:
                for row in context.assignments.to_dict("records"):
                    sid = _text(row.get("session_id"))
                    if sid in targets or sid not in by_id:
                        continue
                    source_session = by_id[sid]
                    # 已取消场次不占资源：求解器已不看它们，apply 必须一致，
                    # 否则预览说「可写入」、应用却因残留分配行报 schedule_conflict。
                    if _text(source_session.get("status")) == "已取消":
                        continue
                    start = pd.Timestamp(source_session.get("start_time"))
                    if pd.isna(start):
                        continue
                    span = pd.Timedelta(minutes=duration(source_session))
                    kind = _text(row.get("resource_kind"))
                    rid = _text(row.get("resource_id"))
                    if kind == "room":
                        room = rooms.get(rid, {})
                        span += pd.Timedelta(minutes=_to_int(room.get("buffer_minutes")) or 0)
                        occupied_rooms.setdefault(rid, []).append((start, start + span))
                    elif kind == "dm":
                        span += pd.Timedelta(minutes=30)
                        occupied_dms.setdefault(rid, []).append((start, start + span))

            rows: list[dict[str, Any]] = []
            proposed_rooms: dict[str, list[tuple[pd.Timestamp, pd.Timestamp]]] = {}
            proposed_dms: dict[str, list[tuple[pd.Timestamp, pd.Timestamp]]] = {}
            target_ids = list(targets)
            existing = context.assignments
            for session_id in target_ids:
                row = by_id[session_id]
                start = pd.Timestamp(row.get("start_time"))
                if pd.isna(start):
                    raise OperationsError("start_time_invalid", f"{label(session_id)}：场次缺少开场时间。")
                span = pd.Timedelta(minutes=duration(row))
                current = existing[
                    existing["session_id"].astype("string").fillna("") == session_id
                ] if not existing.empty else existing
                prev_room = _text(current[current["resource_kind"] == "room"].iloc[0]["resource_id"]) if (not current.empty and (current["resource_kind"] == "room").any()) else ""
                prev_dm = _text(current[current["resource_kind"] == "dm"].iloc[0]["resource_id"]) if (not current.empty and (current["resource_kind"] == "dm").any()) else ""
                room_id = targets[session_id]["room_id"] or prev_room
                dm_id = targets[session_id]["dm_id"] or prev_dm
                if room_id:
                    room = rooms[room_id]
                    room_span = span + pd.Timedelta(minutes=_to_int(room.get("buffer_minutes")) or 0)
                    interval = (start, start + room_span)
                    for other in occupied_rooms.get(room_id, []) + proposed_rooms.get(room_id, []):
                        if interval[0] < other[1] and other[0] < interval[1]:
                            raise OperationsError("schedule_conflict", f"{label(session_id)}：房间在该时段已被占用。")
                    proposed_rooms.setdefault(room_id, []).append(interval)
                    rows.append(
                        {
                            "session_id": session_id,
                            "resource_kind": "room",
                            "resource_id": room_id,
                            "role": "main",
                            "source": "manual",
                        }
                    )
                if dm_id:
                    dm_span = span + pd.Timedelta(minutes=30)
                    interval = (start, start + dm_span)
                    for other in occupied_dms.get(dm_id, []) + proposed_dms.get(dm_id, []):
                        if interval[0] < other[1] and other[0] < interval[1]:
                            raise OperationsError("schedule_conflict", f"{label(session_id)}：DM 在该时段已被占用。")
                    proposed_dms.setdefault(dm_id, []).append(interval)
                    rows.append(
                        {
                            "session_id": session_id,
                            "resource_kind": "dm",
                            "resource_id": dm_id,
                            "role": "main",
                            "source": "manual",
                        }
                    )

            replace_assignments(target_ids, rows, store=store, now=now)
        return {"status": "applied", "session_ids": target_ids, "assignments": rows}

    def apply_schedule(
        self,
        email_hash: str,
        assignments: list[dict[str, Any]],
        *,
        confirm: bool = False,
    ) -> dict[str, Any]:
        if not confirm:
            raise OperationsError("confirm_required", "应用排期需要店长确认。")
        if not assignments:
            raise OperationsError("bad_request", "没有可应用的分配。")
        return self._apply_assignments_impl(
            self._store(email_hash), assignments, now=self.business_now()
        )

    def assign_one(
        self,
        email_hash: str,
        session_id: str,
        *,
        room_id: str | None = None,
        dm_id: str | None = None,
    ) -> dict[str, Any]:
        return self._apply_assignments_impl(
            self._store(email_hash),
            [{"session_id": session_id, "room_id": room_id, "dm_id": dm_id}],
            now=self.business_now(),
        )

    # ------------------------------------------------------------------
    # 预约与渠道单
    # ------------------------------------------------------------------
    def quick_booking(self, email_hash: str, payload: dict[str, Any]) -> dict[str, Any]:
        return quick_booking_tool(
            self._store(email_hash), payload, now=self.business_now()
        )

    def preview_dianping(
        self,
        email_hash: str,
        filename: str,
        content: bytes,
        *,
        data_as_of: Any = None,
    ) -> dict[str, Any]:
        store = self._store(email_hash)
        frame = parse_upload(filename, content, max_bytes=getattr(self.settings, "max_import_bytes", 2 * 1024 * 1024))
        from app.tools.intake import detect_dianping_mapping

        mapping = detect_dianping_mapping(frame.columns)
        preview = validate_dianping_rows(
            frame,
            mapping,
            load_sessions_with_counts(store=store),
            data_as_of=data_as_of,
            bookings=load_bookings(store=store),
            refs=coerce_frame(store.load_booking_refs(), BOOKING_REFS_SPEC),
            now=self.business_now(),
        )
        payload = preview.to_dict()
        payload["filename"] = Path(filename).name
        return payload

    def commit_dianping(
        self,
        email_hash: str,
        filename: str,
        content: bytes,
        *,
        data_as_of: Any = None,
        confirm: bool = False,
    ) -> dict[str, Any]:
        if not confirm:
            raise OperationsError("confirm_required", "导入前请确认预览结果。")
        store = self._store(email_hash)
        frame = parse_upload(filename, content, max_bytes=getattr(self.settings, "max_import_bytes", 2 * 1024 * 1024))
        from app.tools.intake import detect_dianping_mapping

        mapping = detect_dianping_mapping(frame.columns)
        preview = validate_dianping_rows(
            frame,
            mapping,
            load_sessions_with_counts(store=store),
            data_as_of=data_as_of,
            bookings=load_bookings(store=store),
            refs=coerce_frame(store.load_booking_refs(), BOOKING_REFS_SPEC),
            now=self.business_now(),
        )
        result = commit_import(preview.plan, store=store, now=self.business_now())
        payload = result.to_dict()
        payload.update(
            {
                "source": SOURCE_DIANPING,
                "data_as_of": preview.data_as_of,
                "stale": preview.stale,
                "valid_rows": preview.valid_rows,
                "total_rows": preview.total_rows,
            }
        )
        if not self._record_dianping_run(store, result):
            payload["warnings"].append("导入已完成，但审计记录写入失败。")
        return payload

    def _record_dianping_run(self, store: Any, result: Any) -> bool:
        """数据库模式记录一次渠道导入；Excel 模式没有审计表，直接跳过。"""
        engine = getattr(store, "engine", None)
        tenant_id = getattr(store, "tenant_id", None)
        if engine is None or not tenant_id or not self.settings.uses_database:
            return True
        from sqlalchemy import insert

        from app.store.schema import import_runs

        moment = datetime.now(UTC)
        status = "success" if int(result.rejected or 0) == 0 else "partial"
        try:
            with engine.begin() as conn:
                conn.execute(
                    insert(import_runs).values(
                        id=f"imp-{uuid.uuid4().hex[:12]}",
                        source="dianping_csv",
                        tenant_id=str(tenant_id),
                        started_at=moment,
                        finished_at=moment,
                        status=status,
                        session_rows=0,
                        booking_rows=int(result.imported)
                        + int(result.updated)
                        + int(result.cancelled),
                        script_rows=0,
                        warnings="\n".join(result.warnings) or None,
                    )
                )
        except Exception:  # noqa: BLE001 - 审计失败不影响已完成的预约写入
            return False
        return True

    # ------------------------------------------------------------------
    # 阶段模板
    # ------------------------------------------------------------------
    def stages(self, email_hash: str, script_id: str | None = None) -> dict[str, Any]:
        store = self._store(email_hash)
        scripts = load_scripts(store=store)
        all_stages = store.load_script_stages()
        with_stages = set(
            all_stages["script_id"].astype("string").fillna("")
        ) if not all_stages.empty else set()
        entries = []
        for row in scripts.to_dict("records") if not scripts.empty else []:
            sid = _text(row.get("script_id"))
            entries.append(
                {
                    "script_id": sid,
                    "script_name": _text(row.get("script_name")),
                    "script_type": _text(row.get("script_type")) or None,
                    "duration_minutes": json_safe(row.get("duration_minutes")),
                    "has_template": sid in with_stages,
                }
            )
        selected_id = _text(script_id) or (entries[0]["script_id"] if entries else "")
        selected = None
        if selected_id:
            selected = get_stages(store, script_id=selected_id)
        return {
            "scripts": entries,
            "selected": selected,
            "sample": sample_stages(),
            "as_of": self.business_now().strftime("%Y-%m-%d %H:%M"),
        }

    def save_stages(
        self,
        email_hash: str,
        script_id: str,
        stages: list[dict[str, Any]],
        *,
        confirm: bool = False,
    ) -> dict[str, Any]:
        if not confirm:
            raise OperationsError("confirm_required", "保存阶段模板需要确认。")
        result = save_stages_tool(
            self._store(email_hash),
            script_id=script_id,
            stages=stages,
            now=self.business_now(),
        )
        return result

    # ------------------------------------------------------------------
    # DM 场控（商家侧只读 + 发码）
    # ------------------------------------------------------------------
    def _hardware(self) -> Any:
        return build_hardware_adapter(
            url=getattr(self.settings, "hardware_skill_url", None),
            token=getattr(self.settings, "hardware_skill_token", None),
            timeout_seconds=getattr(self.settings, "hardware_timeout_seconds", None),
        )

    def create_run(
        self,
        email_hash: str,
        session_id: str,
        *,
        dm_id: str | None = None,
        mode: str = "software",
    ) -> dict[str, Any]:
        return dm_control.create_run(
            self._store(email_hash),
            session_id=session_id,
            created_by=email_hash,
            dm_id=dm_id,
            mode=mode,
            now=self.utc_now(),
            secret=getattr(self.settings, "session_secret", None),
            code_ttl_minutes=getattr(self.settings, "dm_code_ttl_minutes", 30),
            hardware=self._hardware(),
        )

    def issue_run_code(
        self,
        email_hash: str,
        run_id: str,
        *,
        dm_id: str | None = None,
        dm_name: str | None = None,
    ) -> dict[str, Any]:
        return dm_control.issue_run_code(
            self._store(email_hash),
            run_id=run_id,
            dm_id=dm_id,
            dm_name=dm_name,
            now=self.utc_now(),
            secret=getattr(self.settings, "session_secret", None),
            code_ttl_minutes=getattr(self.settings, "dm_code_ttl_minutes", 30),
        )

    def revoke_run_token(self, email_hash: str, run_id: str) -> dict[str, Any]:
        return dm_control.revoke_run_token(
            self._store(email_hash), run_id, now=self.utc_now()
        )

    def abandon_run(self, email_hash: str, run_id: str) -> dict[str, Any]:
        return dm_control.abandon_run(
            self._store(email_hash), run_id, now=self.utc_now()
        )

    def run_view(self, email_hash: str, run_id: str) -> dict[str, Any]:
        return dm_control.get_run(
            self._store(email_hash), run_id, for_dm=False, now=self.utc_now()
        )

    def run_report(self, email_hash: str, run_id: str) -> dict[str, Any]:
        return dm_control.run_report(
            self._store(email_hash), run_id=run_id, now=self.utc_now()
        )

    def runs_today(self, email_hash: str, day: Any = None) -> dict[str, Any]:
        target = pd.Timestamp(day).date() if day not in (None, "") else self.business_now().date()
        runs = dm_control.list_runs_today(
            self._store(email_hash), day=target, now=self.utc_now()
        )
        return {
            "day": target.isoformat(),
            "runs": runs,
            "server_now": dm_control._iso(self.utc_now()),
        }


__all__ = ["OperationsError", "OperationsService"]

"""快速预约、容量守卫与大众点评订单文件导入。

边界：
  - 只接受人工导出/上传的 CSV/XLSX，不抓取、不登录商家后台；
  - 手机号列只识别、不落库；
  - 容量检查与写入在同一个租户写锁内；
  - 点评导入按场次聚合净增座位，超容量整组拒绝，不做部分超售。
"""

from __future__ import annotations

import io
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import pandas as pd

from app.tools.frame import coerce_frame, json_safe
from app.tools.frame_specs import BOOKING_REFS_SPEC
from app.tools.storage import (
    BOOKING_ACTIVE,
    BOOKING_CANCELLED,
    STATUS_CANCELLED,
    apply_booking_counts,
    is_missing_value,
    load_bookings,
    load_sessions,
    load_sessions_with_counts,
    make_booking_id,
    save_bookings,
    store_lock,
)

MAX_UPLOAD_BYTES = 2 * 1024 * 1024
MAX_ROWS = 5000
MAX_COLUMNS = 50
STALE_AFTER = pd.Timedelta(hours=2)
CANCELLED_TOKENS = ("取消", "退款", "关闭", "失效", "退单", "cancel", "refund", "closed")
SOURCE_DIANPING = "dianping"
SOURCE_QUICK = "quick"
CLIENT_REF_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")

MAPPING_ALIASES: dict[str, tuple[str, ...]] = {
    "external_order_id": (
        "订单号",
        "订单编号",
        "订单id",
        "order_id",
        "order_no",
        "orderid",
    ),
    "session_id": ("场次id", "场次编号", "session_id", "sessionid"),
    "script_name": ("商品名称", "剧本名", "剧本", "script_name", "商品"),
    "start_time": ("消费时间", "到店时间", "预约时间", "开场时间", "使用时间"),
    "seats": ("数量", "人数", "份数", "座位数", "seats"),
    "player_name": ("联系人", "昵称", "顾客姓名", "玩家昵称", "player_name"),
    "status": ("订单状态", "状态", "status"),
    "data_as_of": ("导出时间", "数据截止时间", "data_as_of"),
    "store_name": ("门店", "门店名称", "store_name"),
    "phone": ("手机号", "电话", "联系电话", "手机"),
    "rating": ("评价", "评分", "评论"),
}


class IntakeError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# ---------------------------------------------------------------------------
# 基础函数
# ---------------------------------------------------------------------------


def _text(value: Any) -> str:
    if is_missing_value(value):
        return ""
    return str(value).strip()


def _id_text(value: Any) -> str:
    text = _text(value)
    if text.endswith(".0") and text[:-2].isdigit():
        return text[:-2]
    return text


def _int(value: Any) -> int | None:
    if is_missing_value(value):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    match = re.search(r"-?\d+", str(value))
    return int(match.group()) if match else None


def _parse_moment(value: Any) -> pd.Timestamp | None:
    if is_missing_value(value):
        return None
    try:
        moment = pd.to_datetime(value, errors="coerce")
    except (TypeError, ValueError, OverflowError):
        return None
    if pd.isna(moment):
        return None
    return pd.Timestamp(moment)


def _iso(value: Any) -> str | None:
    moment = _parse_moment(value)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ") if moment is not None else None


def _local_text(value: Any) -> str | None:
    moment = _parse_moment(value)
    return moment.strftime("%Y-%m-%d %H:%M") if moment is not None else None


def _now(now: Any = None) -> pd.Timestamp:
    if now is None:
        return pd.Timestamp.now().floor("s")
    return pd.Timestamp(now).floor("s")


def _status_is_cancelled(value: Any) -> bool:
    text = _text(value).casefold()
    return any(token in text for token in CANCELLED_TOKENS)


def _phone_scrub(value: Any) -> str:
    text = _text(value)
    return re.sub(r"1\d{10}", "[手机号]", text)[:200]


# ---------------------------------------------------------------------------
# 快速预约
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class QuickBookingRequest:
    session_id: str
    seats: int
    client_ref: str
    player_name: str | None = None


def validate_client_ref(value: Any) -> bool:
    return bool(CLIENT_REF_RE.fullmatch(_text(value)))


def make_quick_booking_id(session_id: str, client_ref: str) -> str:
    return make_booking_id(session_id, f"quick:{_text(client_ref)}")


def plan_quick_booking(
    session: dict[str, Any] | None,
    active_seats: int,
    seats: int,
) -> tuple[bool, str | None]:
    """只判断能不能加座，不写库。返回 (ok, error_code_or_reason)。"""
    if session is None:
        return False, "session_not_found"
    if _text(session.get("status")) == STATUS_CANCELLED:
        return False, "session_cancelled"
    if int(seats) < 1:
        return False, "bad_request"
    total = session.get("total_seats")
    if is_missing_value(total):
        return True, "capacity_unknown"
    if int(active_seats) + int(seats) > int(total):
        return False, "capacity_exceeded"
    return True, None


def _session_by_id(store: Any, session_id: str) -> dict[str, Any] | None:
    sessions = load_sessions_with_counts(store=store)
    if sessions.empty:
        return None
    hit = sessions[
        sessions["session_id"].astype("string").fillna("") == _text(session_id)
    ]
    return hit.iloc[0].to_dict() if not hit.empty else None


def _load_refs(store: Any) -> pd.DataFrame:
    return coerce_frame(store.load_booking_refs(), BOOKING_REFS_SPEC)


def quick_booking(
    store: Any,
    request: QuickBookingRequest | dict[str, Any],
    *,
    now: Any = None,
) -> dict[str, Any]:
    if isinstance(request, dict):
        request = QuickBookingRequest(
            session_id=_text(request.get("session_id")),
            seats=int(request.get("seats") or 0),
            client_ref=_text(request.get("client_ref")),
            player_name=request.get("player_name"),
        )
    moment = _now(now)
    if not validate_client_ref(request.client_ref):
        return {
            "ok": False,
            "code": "bad_request",
            "error": "client_ref 必须是 8..64 位字母、数字、下划线或短横线。",
        }
    if int(request.seats) < 1:
        return {"ok": False, "code": "bad_request", "error": "至少预约 1 人。"}

    with store_lock(store):
        refs = _load_refs(store)
        existing_ref = None
        if not refs.empty:
            hit = refs[
                (refs["source"].astype("string").fillna("") == SOURCE_QUICK)
                & (
                    refs["external_id"].astype("string").fillna("")
                    == request.client_ref
                )
            ]
            if not hit.empty:
                existing_ref = hit.iloc[0].to_dict()

        session = _session_by_id(store, request.session_id)
        if session is None:
            return {
                "ok": False,
                "code": "session_not_found",
                "error": "场次不存在，请先建场次。",
            }
        if existing_ref is not None:
            return {
                "ok": True,
                "unchanged": True,
                "booking_id": _text(existing_ref.get("booking_id")) or None,
                "session": _session_payload(session),
                "warnings": [],
            }

        ok, reason = plan_quick_booking(session, int(session.get("booked_seats") or 0), request.seats)
        if not ok:
            if reason == "capacity_exceeded":
                return {
                    "ok": False,
                    "code": "capacity_exceeded",
                    "error": "已满，建议候补或改场。",
                    "remaining_seats": int(session.get("remaining_seats") or 0),
                    "total_seats": json_safe(session.get("total_seats")),
                    "session": _session_payload(session),
                }
            if reason == "session_cancelled":
                return {
                    "ok": False,
                    "code": "session_cancelled",
                    "error": "场次已取消，不能加预约。",
                    "session": _session_payload(session),
                }
            return {"ok": False, "code": reason or "bad_request", "error": "无法预约。"}

        player_name = _text(request.player_name) or f"散客-{request.client_ref[:4]}"
        booking_id = make_quick_booking_id(request.session_id, request.client_ref)
        bookings = load_bookings(store=store)
        fresh = {
            "booking_id": booking_id,
            "session_id": request.session_id,
            "player_name": player_name,
            "seats": int(request.seats),
            "status": BOOKING_ACTIVE,
            "booked_at": moment,
            "updated_at": moment,
            "source_text": f"快速占位:{request.client_ref}",
        }
        bookings = (
            pd.DataFrame([fresh])
            if bookings.empty
            else pd.concat([bookings, pd.DataFrame([fresh])], ignore_index=True)
        )
        save_bookings(bookings, store=store)

        ref_row = {
            "ref_id": f"ref-{uuid.uuid4().hex[:12]}",
            "source": SOURCE_QUICK,
            "external_id": request.client_ref,
            "booking_id": booking_id,
            "session_id": request.session_id,
            "seats": int(request.seats),
            "status": "active",
            "data_as_of": moment,
            "imported_at": moment,
            "raw_ref": "quick",
        }
        refs = (
            pd.DataFrame([ref_row])
            if refs.empty
            else pd.concat([refs, pd.DataFrame([ref_row])], ignore_index=True)
        )
        store.save_booking_refs(coerce_frame(refs, BOOKING_REFS_SPEC))

        refreshed = _session_by_id(store, request.session_id)
        warnings = list(
            [reason] if reason == "capacity_unknown" else []
        )
        return {
            "ok": True,
            "unchanged": False,
            "booking_id": booking_id,
            "session": _session_payload(refreshed or session),
            "warnings": warnings,
        }


def _session_payload(session: dict[str, Any]) -> dict[str, Any]:
    return {
        "session_id": _text(session.get("session_id")),
        "store_name": _text(session.get("store_name")) or None,
        "script_name": _text(session.get("script_name")) or None,
        "script_type": _text(session.get("script_type")) or None,
        "start_time": _local_text(session.get("start_time")),
        "status": _text(session.get("status")) or None,
        "total_seats": json_safe(session.get("total_seats")),
        "booked_seats": int(session.get("booked_seats") or 0),
        "remaining_seats": json_safe(session.get("remaining_seats")),
    }


# ---------------------------------------------------------------------------
# 上传解析与列映射
# ---------------------------------------------------------------------------


def parse_upload(
    filename: str,
    content: bytes,
    *,
    max_rows: int = MAX_ROWS,
    max_columns: int = MAX_COLUMNS,
    max_bytes: int = MAX_UPLOAD_BYTES,
) -> pd.DataFrame:
    name = _text(filename).casefold()
    if not name.endswith((".csv", ".xlsx")):
        raise IntakeError("bad_file", "只支持 UTF-8/GB18030 CSV 或 XLSX 文件。")
    if not content:
        raise IntakeError("bad_file", "文件为空。")
    if len(content) > max_bytes:
        raise IntakeError("file_too_large", "文件超过上传大小限制。")

    if name.endswith(".csv"):
        frame = None
        for encoding in ("utf-8-sig", "gb18030"):
            try:
                frame = pd.read_csv(
                    io.BytesIO(content),
                    dtype=str,
                    keep_default_na=False,
                    encoding=encoding,
                    nrows=max_rows + 1,
                )
                break
            except UnicodeDecodeError:
                continue
            except (pd.errors.ParserError, ValueError):
                raise IntakeError("bad_file", "CSV 无法解析，请检查分隔符和编码。") from None
        if frame is None:
            raise IntakeError("bad_file", "CSV 编码无法识别。")
    else:
        try:
            frame = pd.read_excel(
                io.BytesIO(content),
                dtype=str,
                nrows=max_rows + 1,
                engine="openpyxl",
            )
        except Exception:  # noqa: BLE001 - 第三方解析异常统一映射
            raise IntakeError("bad_file", "XLSX 无法解析，请重新导出。") from None

    if frame is None or len(frame.columns) == 0:
        raise IntakeError("bad_file", "文件里没有可识别的表格。")
    if len(frame) > max_rows:
        raise IntakeError("too_many_rows", f"单次最多导入 {max_rows} 行。")
    if len(frame.columns) > max_columns:
        raise IntakeError("too_many_columns", f"单次最多 {max_columns} 列。")
    return frame


def _norm_header(value: Any) -> str:
    return re.sub(r"[\s_\-]+", "", str(value or "")).casefold()


def detect_dianping_mapping(columns: Iterable[Any]) -> dict[str, str]:
    by_norm = {}
    for column in columns:
        key = _norm_header(column)
        if key and key not in by_norm:
            by_norm[key] = str(column)
    mapping: dict[str, str] = {}
    for target, aliases in MAPPING_ALIASES.items():
        for alias in aliases:
            found = by_norm.get(_norm_header(alias))
            if found is not None:
                mapping[target] = found
                break
    return mapping


def _cell(row: pd.Series, mapping: dict[str, str], target: str) -> str:
    column = mapping.get(target)
    if not column or column not in row:
        return ""
    return _text(row.get(column))


# ---------------------------------------------------------------------------
# 点评行规范化与场次匹配
# ---------------------------------------------------------------------------


def _session_index(sessions: pd.DataFrame) -> list[dict[str, Any]]:
    return sessions.to_dict("records") if not sessions.empty else []


def _match_session(
    row: pd.Series,
    mapping: dict[str, str],
    sessions: pd.DataFrame,
) -> tuple[dict[str, Any] | None, str | None]:
    explicit = _id_text(_cell(row, mapping, "session_id"))
    candidates = _session_index(sessions)
    if explicit:
        hit = [
            item
            for item in candidates
            if _text(item.get("session_id")) == explicit
        ]
        if not hit:
            return None, "session_not_found"
        return hit[0], None

    script = _cell(row, mapping, "script_name")
    start = _cell(row, mapping, "start_time")
    if not script or not start:
        return None, "session_not_found"
    target_start = _parse_moment(start)
    if target_start is None:
        return None, "invalid_start_time"
    store_name = _cell(row, mapping, "store_name")
    normalized_script = re.sub(r"\s+", "", script).casefold()
    hits = []
    for item in candidates:
        if re.sub(r"\s+", "", _text(item.get("script_name"))).casefold() != normalized_script:
            continue
        item_start = _parse_moment(item.get("start_time"))
        if item_start is None or item_start.floor("min") != target_start.floor("min"):
            continue
        if store_name and _text(item.get("store_name")) != store_name:
            continue
        hits.append(item)
    if not hits:
        return None, "session_not_found"
    if len(hits) > 1:
        return None, "ambiguous_session"
    return hits[0], None


@dataclass
class ImportPreview:
    mapping: dict[str, str]
    rows: list[dict[str, Any]]
    errors: list[dict[str, Any]]
    warnings: list[str]
    session_changes: list[dict[str, Any]]
    unmatched: list[dict[str, Any]]
    over_capacity: list[dict[str, Any]]
    data_as_of: str | None
    stale: bool
    total_rows: int
    valid_rows: int
    recognized_rating: bool
    plan: "ImportPlan" = field(repr=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "mapping": self.mapping,
            "preview": self.rows[:10],
            "rows": self.rows,
            "errors": self.errors,
            "warnings": self.warnings,
            "session_changes": self.session_changes,
            "unmatched": self.unmatched,
            "over_capacity": self.over_capacity,
            "data_as_of": self.data_as_of,
            "stale": self.stale,
            "total_rows": self.total_rows,
            "valid_rows": self.valid_rows,
            "recognized_rating": self.recognized_rating,
        }


@dataclass
class ImportPlan:
    accepted: list[dict[str, Any]]
    rejected: list[dict[str, Any]]
    warnings: list[str]
    session_changes: list[dict[str, Any]]
    data_as_of: pd.Timestamp | None
    total_rows: int
    over_capacity: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "accepted": len(self.accepted),
            "rejected": len(self.rejected),
            "warnings": self.warnings,
            "session_changes": self.session_changes,
            "over_capacity": self.over_capacity,
            "total_rows": self.total_rows,
        }


def _normalize_row(
    row: pd.Series,
    mapping: dict[str, str],
    sessions: pd.DataFrame,
    refs: pd.DataFrame,
    *,
    row_number: int,
) -> dict[str, Any]:
    external_id = _id_text(_cell(row, mapping, "external_order_id"))
    seats = _int(_cell(row, mapping, "seats"))
    raw_status = _cell(row, mapping, "status")
    player_name = _cell(row, mapping, "player_name") or (
        f"点评-{external_id[-4:]}" if external_id else ""
    )
    result: dict[str, Any] = {
        "row_number": row_number,
        "external_id": external_id,
        "seats": seats,
        "player_name": player_name[:40],
        "status": "cancelled" if _status_is_cancelled(raw_status) else "active",
        "status_text": raw_status,
        "session_id": None,
        "script_name": _cell(row, mapping, "script_name") or None,
        "start_time": _cell(row, mapping, "start_time") or None,
        "action": "invalid",
        "error": None,
        "error_code": None,
        "old_ref": None,
    }
    if not external_id:
        result["error_code"] = "missing_order_id"
        result["error"] = "缺少订单号。"
        return result
    if external_id.startswith("-"):
        result["error_code"] = "missing_order_id"
        result["error"] = "订单号无效。"
        return result
    if seats is None or seats < 1:
        result["error_code"] = "invalid_seats"
        result["error"] = "人数缺失或小于 1。"
        return result

    session, error = _match_session(row, mapping, sessions)
    if session is None:
        result["error_code"] = error or "session_not_found"
        result["error"] = (
            "匹配到多个场次，请补 session_id。"
            if error == "ambiguous_session"
            else "找不到对应场次，不会自动新建。"
        )
        return result
    result["session_id"] = _text(session.get("session_id"))

    existing_ref = None
    if not refs.empty:
        hit = refs[
            (refs["source"].astype("string").fillna("") == SOURCE_DIANPING)
            & (refs["external_id"].astype("string").fillna("") == external_id)
        ]
        if not hit.empty:
            existing_ref = hit.iloc[0].to_dict()
    result["old_ref"] = {
        "booking_id": _text(existing_ref.get("booking_id")) if existing_ref else None,
        "session_id": _text(existing_ref.get("session_id")) if existing_ref else None,
        "seats": _int(existing_ref.get("seats")) if existing_ref else None,
        "status": _text(existing_ref.get("status")) if existing_ref else None,
    }
    old_active = bool(existing_ref) and result["old_ref"]["status"] == "active"
    if result["status"] == "cancelled":
        result["action"] = "cancel" if old_active else "unchanged"
    elif old_active:
        same_session = result["old_ref"]["session_id"] == result["session_id"]
        if same_session and result["old_ref"]["seats"] == seats:
            result["action"] = "unchanged"
        elif same_session:
            result["action"] = "update"
        else:
            result["action"] = "move"
    else:
        result["action"] = "create"
    return result


def _net_delta(row: dict[str, Any]) -> tuple[str | None, int]:
    old = row.get("old_ref") or {}
    old_active = old.get("status") == "active"
    old_seats = int(old.get("seats") or 0) if old_active else 0
    old_session = old.get("session_id") if old_active else None
    action = row.get("action")
    if action == "create":
        return row.get("session_id"), int(row.get("seats") or 0)
    if action == "update":
        return row.get("session_id"), int(row.get("seats") or 0) - old_seats
    if action == "cancel":
        return old_session, -old_seats
    if action == "move":
        return row.get("session_id"), int(row.get("seats") or 0)
    return row.get("session_id"), 0


def _move_release(row: dict[str, Any]) -> tuple[str | None, int]:
    old = row.get("old_ref") or {}
    if row.get("action") == "move" and old.get("status") == "active":
        return old.get("session_id"), -int(old.get("seats") or 0)
    return None, 0


def plan_import(
    rows: Iterable[dict[str, Any]],
    sessions: pd.DataFrame,
    bookings: pd.DataFrame | None = None,
    refs: pd.DataFrame | None = None,
) -> ImportPlan:
    """按场次聚合净增座位；任何一个场次超额，整组拒绝。"""
    sessions = (
        apply_booking_counts(sessions, bookings)
        if bookings is not None
        else sessions
    )
    index = {
        _text(item.get("session_id")): item
        for item in _session_index(sessions)
    }
    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    warnings: list[str] = []
    changes: dict[str, dict[str, Any]] = {}

    for row in rows:
        if row.get("error"):
            rejected.append(row)
            continue
        session_id, delta = _net_delta(row)
        move_from, release = _move_release(row)
        if move_from and move_from in index and release:
            entry = changes.setdefault(
                move_from,
                {
                    "session_id": move_from,
                    "script_name": _text(index[move_from].get("script_name")),
                    "current_booked": int(index[move_from].get("booked_seats") or 0),
                    "net_delta": 0,
                    "total_seats": json_safe(index[move_from].get("total_seats")),
                },
            )
            entry["net_delta"] += release
        if session_id and delta:
            entry = changes.setdefault(
                session_id,
                {
                    "session_id": session_id,
                    "script_name": _text(index.get(session_id, {}).get("script_name")),
                    "current_booked": int(index.get(session_id, {}).get("booked_seats") or 0),
                    "net_delta": 0,
                    "total_seats": json_safe(index.get(session_id, {}).get("total_seats")),
                },
            )
            entry["net_delta"] += delta
        accepted.append(row)

    over_capacity: list[dict[str, Any]] = []
    for session_id, entry in changes.items():
        total = entry.get("total_seats")
        projected = entry["current_booked"] + entry["net_delta"]
        entry["projected_booked"] = projected
        if not is_missing_value(total) and projected > int(total):
            entry["over_by"] = projected - int(total)
            over_capacity.append(entry)

    over_ids = {entry["session_id"] for entry in over_capacity}
    if over_ids:
        remaining: list[dict[str, Any]] = []
        for row in accepted:
            related = {row.get("session_id")}
            old = row.get("old_ref") or {}
            if old.get("session_id"):
                related.add(old["session_id"])
            if related & over_ids:
                row = dict(row)
                row["error_code"] = "capacity_exceeded"
                row["error"] = "该场次按本批净增后超过总人数，整组未导入。"
                rejected.append(row)
            else:
                remaining.append(row)
        accepted = remaining
        changes = {
            key: value for key, value in changes.items() if key not in over_ids
        }
        warnings.append("有场次超出容量，已按场次整组拒绝，其他场次继续。")

    for entry in changes.values():
        if is_missing_value(entry.get("total_seats")):
            warnings.append(
                f"场次 {entry['session_id']} 未填总人数，导入后无法校验容量。"
            )

    data_as_of = None
    for row in accepted:
        if row.get("data_as_of"):
            data_as_of = row["data_as_of"]
            break
    return ImportPlan(
        accepted=accepted,
        rejected=rejected,
        warnings=warnings,
        session_changes=list(changes.values()),
        data_as_of=data_as_of,
        total_rows=len(accepted) + len(rejected),
        over_capacity=over_capacity,
    )


def validate_dianping_rows(
    frame: pd.DataFrame,
    mapping: dict[str, str],
    sessions: pd.DataFrame,
    *,
    data_as_of: Any = None,
    bookings: pd.DataFrame | None = None,
    refs: pd.DataFrame | None = None,
    now: Any = None,
) -> ImportPreview:
    moment = _now(now)
    refs = refs if refs is not None else pd.DataFrame(columns=BOOKING_REFS_SPEC.columns)
    required = {"external_order_id", "seats"}
    missing = [target for target in required if target not in mapping]
    if missing:
        raise IntakeError(
            "invalid_mapping",
            "缺少必需列：订单号、人数；请检查导出文件。",
        )
    if "session_id" not in mapping and not (
        "script_name" in mapping and "start_time" in mapping
    ):
        raise IntakeError(
            "invalid_mapping",
            "必须提供场次ID，或同时提供剧本名和开场时间。",
        )

    parsed_as_of = _parse_moment(data_as_of)
    if parsed_as_of is None:
        parsed_as_of = moment
    stale = (moment - parsed_as_of) > STALE_AFTER

    rows: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    unmatched: list[dict[str, Any]] = []
    for position, (_, source_row) in enumerate(frame.iterrows(), start=2):
        row = _normalize_row(
            source_row, mapping, sessions, refs, row_number=position
        )
        row["data_as_of"] = parsed_as_of
        if row.get("error_code") in {"session_not_found", "ambiguous_session"}:
            unmatched.append(
                {
                    "row_number": position,
                    "external_id": row.get("external_id"),
                    "script_name": row.get("script_name"),
                    "start_time": row.get("start_time"),
                    "code": row.get("error_code"),
                    "error": row.get("error"),
                }
            )
        if row.get("error"):
            errors.append(
                {
                    "row_number": position,
                    "external_id": row.get("external_id"),
                    "code": row.get("error_code"),
                    "error": row.get("error"),
                }
            )
        rows.append(row)

    plan = plan_import(rows, sessions, bookings=bookings, refs=refs)
    preview_rows = []
    for row in rows:
        preview_rows.append(
            {
                "row_number": row["row_number"],
                "external_id": row["external_id"],
                "session_id": row["session_id"],
                "script_name": row["script_name"],
                "start_time": row["start_time"],
                "seats": row["seats"],
                "player_name": row["player_name"],
                "status": row["status"],
                "action": row["action"],
                "error": row["error"],
            }
        )
    warnings = list(plan.warnings)
    if stale:
        warnings.append("数据已过期，请重新导出。")
    recognized_rating = any(
        _norm_header(column).startswith(("评价", "评分", "评论"))
        for column in frame.columns
    )
    if recognized_rating:
        warnings.append("识别到评价列，但本版不做评价分析。")
    if "phone" in mapping:
        warnings.append("电话/手机号列只识别、不落库。")

    return ImportPreview(
        mapping={key: value for key, value in mapping.items() if key != "phone"},
        rows=preview_rows,
        errors=errors,
        warnings=warnings,
        session_changes=plan.session_changes,
        unmatched=unmatched,
        over_capacity=plan.over_capacity,
        data_as_of=_iso(parsed_as_of),
        stale=bool(stale),
        total_rows=len(frame),
        valid_rows=len(plan.accepted),
        recognized_rating=recognized_rating,
        plan=plan,
    )


# ---------------------------------------------------------------------------
# 提交导入
# ---------------------------------------------------------------------------


@dataclass
class ImportResult:
    imported: int
    updated: int
    cancelled: int
    unchanged: int
    rejected: int
    warnings: list[str]
    session_changes: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "imported": self.imported,
            "updated": self.updated,
            "cancelled": self.cancelled,
            "unchanged": self.unchanged,
            "rejected": self.rejected,
            "warnings": self.warnings,
            "session_changes": self.session_changes,
        }


def _find_booking(bookings: pd.DataFrame, booking_id: str) -> int | None:
    if bookings.empty:
        return None
    hit = bookings["booking_id"].astype("string").fillna("") == _text(booking_id)
    return int(bookings.index[hit][0]) if hit.any() else None


def _align_datetime(column: pd.Series, value: Any) -> Any:
    """让写入值匹配现有列时区，避免 pandas 的混合 dtype 警告。"""
    stamp = _parse_moment(value)
    if stamp is None:
        return value
    dtype = column.dtype
    if isinstance(dtype, pd.DatetimeTZDtype):
        return (
            stamp.tz_localize(dtype.tz)
            if stamp.tzinfo is None
            else stamp.tz_convert(dtype.tz)
        )
    return stamp.tz_localize(None) if stamp.tzinfo is not None else stamp


def _upsert_ref(refs: pd.DataFrame, row: dict[str, Any], *, moment: pd.Timestamp) -> tuple[pd.DataFrame, str]:
    external_id = _text(row.get("external_id"))
    hit = refs[
        (refs["source"].astype("string").fillna("") == SOURCE_DIANPING)
        & (refs["external_id"].astype("string").fillna("") == external_id)
    ] if not refs.empty else refs
    booking_id = (
        _text(hit.iloc[0].get("booking_id"))
        if not hit.empty
        else make_booking_id(_text(row.get("session_id")), f"dianping:{external_id}")
    )
    raw_ref = _phone_scrub(
        f"order={external_id} status={row.get('status_text') or row.get('status')}"
    )
    values = {
        "source": SOURCE_DIANPING,
        "external_id": external_id,
        "booking_id": booking_id,
        "session_id": _text(row.get("session_id")),
        "seats": int(row.get("seats") or 0),
        "status": "cancelled" if row.get("action") == "cancel" or row.get("status") == "cancelled" else "active",
        "data_as_of": _align_datetime(
            refs["data_as_of"], row.get("data_as_of") or moment
        ),
        "imported_at": _align_datetime(refs["imported_at"], moment),
        "raw_ref": raw_ref,
    }
    if hit.empty:
        values["ref_id"] = f"ref-{uuid.uuid4().hex[:12]}"
        refs = pd.concat([refs, pd.DataFrame([values])], ignore_index=True)
    else:
        idx = hit.index[0]
        values["ref_id"] = _text(refs.at[idx, "ref_id"]) or f"ref-{uuid.uuid4().hex[:12]}"
        for key, value in values.items():
            refs.at[idx, key] = value
    return coerce_frame(refs, BOOKING_REFS_SPEC), booking_id


def commit_import(
    plan: ImportPlan,
    *,
    store: Any,
    now: Any = None,
) -> ImportResult:
    moment = _now(now)
    imported = updated = cancelled = unchanged = 0
    warnings = list(plan.warnings)
    with store_lock(store):
        sessions = load_sessions_with_counts(store=store)
        bookings = load_bookings(store=store)
        refs = _load_refs(store)
        # 提交前重新按场次聚合一次，防止预览后又有新预约。
        refreshed = plan_import(
            plan.accepted + plan.rejected,
            sessions,
            bookings=bookings,
            refs=refs,
        )
        rejected = list(refreshed.rejected)
        warnings.extend(refreshed.warnings)

        for row in refreshed.accepted:
            action = row.get("action")
            if action == "unchanged":
                unchanged += 1
                continue
            old = row.get("old_ref") or {}
            old_booking_id = old.get("booking_id")
            if action in {"cancel", "update", "move"} and old_booking_id:
                idx = _find_booking(bookings, old_booking_id)
                if idx is not None:
                    if action == "cancel" or action == "move":
                        bookings.at[idx, "status"] = BOOKING_CANCELLED
                    else:
                        bookings.at[idx, "seats"] = int(row.get("seats") or 1)
                    bookings.at[idx, "updated_at"] = moment
                    if action == "cancel":
                        cancelled += 1
                    elif action == "update":
                        updated += 1
                elif action in {"cancel", "update"}:
                    warnings.append(
                        f"订单 {row.get('external_id')} 的历史预约不存在，只更新引用。"
                    )
            if action in {"create", "update", "move"}:
                refs, booking_id = _upsert_ref(refs, row, moment=moment)
                if action in {"create", "move"}:
                    if action == "move":
                        row = dict(row)
                        row["session_id"] = _text(row.get("session_id"))
                    fresh = {
                        "booking_id": booking_id,
                        "session_id": _text(row.get("session_id")),
                        "player_name": _text(row.get("player_name")) or f"点评-{row.get('external_id')[-4:]}",
                        "seats": int(row.get("seats") or 1),
                        "status": BOOKING_ACTIVE,
                        "booked_at": moment,
                        "updated_at": moment,
                        "source_text": f"点评导入:{row.get('external_id')}",
                    }
                    idx = _find_booking(bookings, booking_id)
                    if idx is None:
                        bookings = pd.concat([bookings, pd.DataFrame([fresh])], ignore_index=True)
                    else:
                        for key, value in fresh.items():
                            bookings.at[idx, key] = value
                    imported += 1
            elif action == "cancel":
                refs, _ = _upsert_ref(refs, row, moment=moment)

        save_bookings(bookings, store=store)
        store.save_booking_refs(refs)
        return ImportResult(
            imported=imported,
            updated=updated,
            cancelled=cancelled,
            unchanged=unchanged,
            rejected=len(rejected),
            warnings=warnings,
            session_changes=refreshed.session_changes,
        )


__all__ = [
    "CLIENT_REF_RE",
    "ImportPlan",
    "ImportPreview",
    "ImportResult",
    "IntakeError",
    "QuickBookingRequest",
    "commit_import",
    "detect_dianping_mapping",
    "make_quick_booking_id",
    "parse_upload",
    "plan_import",
    "plan_quick_booking",
    "quick_booking",
    "validate_client_ref",
    "validate_dianping_rows",
]

"""快速预约、容量守卫与点评 CSV 导入。"""

from __future__ import annotations

import pandas as pd
import pytest

from app.store.engine import create_database_engine, ensure_schema
from app.store.excel import ExcelStore
from app.store.sql import SqlStore
from app.tools.intake import (
    IntakeError,
    commit_import,
    detect_dianping_mapping,
    parse_upload,
    plan_quick_booking,
    quick_booking,
    validate_dianping_rows,
    validate_client_ref,
)
from app.tools.storage import (
    BOOKING_CANCELLED,
    load_bookings,
    load_sessions_with_counts,
    upsert_sessions,
)

NOW = pd.Timestamp("2026-10-20 12:00")


@pytest.fixture(params=["excel", "sql"])
def store(request, tmp_path):
    if request.param == "excel":
        yield ExcelStore(
            sessions_path=tmp_path / "sessions.xlsx",
            bookings_path=tmp_path / "bookings.xlsx",
            scripts_path=tmp_path / "scripts.xlsx",
        )
        return
    engine = create_database_engine(sqlite_path=tmp_path / "store.sqlite3")
    ensure_schema(engine)
    value = SqlStore(engine, "test-intake-2")
    value.ensure_tenant()
    yield value
    engine.dispose()


def add_session(store, *, session_id="s-1", seats=6, script="雾隐山庄", start="2026-10-21 20:00", status="待开场"):
    upsert_sessions(
        [
            {
                "session_id": session_id,
                "store_name": "迷雾剧场",
                "script_name": script,
                "script_type": "推理",
                "start_time": start,
                "total_seats": seats,
                "list_price": 168,
                "status": status,
            }
        ],
        store=store,
    )


def counts(store, session_id="s-1"):
    frame = load_sessions_with_counts(store=store)
    row = frame[frame["session_id"] == session_id].iloc[0]
    return int(row["booked_seats"]), int(row["remaining_seats"])


# ---------------------------------------------------------------------------
# 快速预约
# ---------------------------------------------------------------------------


def test_client_ref_validation():
    assert validate_client_ref("abcd1234")
    assert validate_client_ref("a-b_C_123")
    assert not validate_client_ref("short")
    assert not validate_client_ref("带中文的编号")
    assert not validate_client_ref("")


def test_plan_quick_booking_capacity():
    ok, reason = plan_quick_booking({"total_seats": 6, "status": "待开场"}, 4, 2)
    assert ok is True and reason is None

    ok, reason = plan_quick_booking({"total_seats": 6, "status": "待开场"}, 4, 3)
    assert ok is False and reason == "capacity_exceeded"

    ok, reason = plan_quick_booking({"total_seats": None, "status": "待开场"}, 4, 3)
    assert ok is True and reason == "capacity_unknown"

    ok, reason = plan_quick_booking(None, 0, 1)
    assert ok is False and reason == "session_not_found"


def test_quick_booking_adds_seats_and_is_idempotent(store):
    add_session(store)
    first = quick_booking(
        store,
        {"session_id": "s-1", "seats": 2, "client_ref": "ref-000001"},
        now=NOW,
    )
    assert first["ok"] is True
    assert first["unchanged"] is False
    assert counts(store) == (2, 4)

    again = quick_booking(
        store,
        {"session_id": "s-1", "seats": 2, "client_ref": "ref-000001"},
        now=NOW,
    )
    assert again["ok"] is True
    assert again["unchanged"] is True
    assert again["booking_id"] == first["booking_id"]
    assert counts(store) == (2, 4)
    assert len(load_bookings(store=store)) == 1


def test_quick_booking_rejects_when_full(store):
    add_session(store)
    assert quick_booking(
        store, {"session_id": "s-1", "seats": 4, "client_ref": "ref-full01"}, now=NOW
    )["ok"]
    result = quick_booking(
        store, {"session_id": "s-1", "seats": 3, "client_ref": "ref-full02"}, now=NOW
    )
    assert result["ok"] is False
    assert result["code"] == "capacity_exceeded"
    assert result["remaining_seats"] == 2
    assert counts(store) == (4, 2)


def test_quick_booking_unknown_session(store):
    result = quick_booking(
        store, {"session_id": "s-missing", "seats": 1, "client_ref": "ref-000001"}, now=NOW
    )
    assert result["ok"] is False
    assert result["code"] == "session_not_found"


def test_quick_booking_cancelled_session(store):
    add_session(store, status="已取消")
    result = quick_booking(
        store, {"session_id": "s-1", "seats": 1, "client_ref": "ref-000001"}, now=NOW
    )
    assert result["ok"] is False
    assert result["code"] == "session_cancelled"


def test_quick_booking_unknown_capacity_warns(store):
    add_session(store, seats=None)
    result = quick_booking(
        store, {"session_id": "s-1", "seats": 2, "client_ref": "ref-000001"}, now=NOW
    )
    assert result["ok"] is True
    assert "capacity_unknown" in result["warnings"]


def test_quick_booking_bad_client_ref(store):
    add_session(store)
    result = quick_booking(
        store, {"session_id": "s-1", "seats": 1, "client_ref": "x"}, now=NOW
    )
    assert result["ok"] is False
    assert result["code"] == "bad_request"


# ---------------------------------------------------------------------------
# 文件解析与列映射
# ---------------------------------------------------------------------------


def to_csv(rows, columns, encoding="utf-8-sig"):
    return pd.DataFrame(rows, columns=columns).to_csv(index=False).encode(encoding)


DIANPING_COLUMNS = ["订单号", "场次ID", "数量", "联系人", "订单状态", "消费时间"]


def dianping_csv(rows, encoding="utf-8-sig"):
    return to_csv(rows, DIANPING_COLUMNS, encoding)


def test_detect_mapping_chinese_headers():
    mapping = detect_dianping_mapping(
        ["订单号", "场次ID", "商品名称", "消费时间", "人数", "联系人", "订单状态", "导出时间"]
    )
    assert mapping["external_order_id"] == "订单号"
    assert mapping["session_id"] == "场次ID"
    assert mapping["script_name"] == "商品名称"
    assert mapping["start_time"] == "消费时间"
    assert mapping["seats"] == "人数"
    assert mapping["player_name"] == "联系人"
    assert mapping["status"] == "订单状态"
    assert mapping["data_as_of"] == "导出时间"


def test_parse_upload_accepts_utf8_and_gb18030():
    rows = [["DP-1", "s-1", "2", "阿青", "已完成", "2026-10-21 20:00"]]
    utf8 = parse_upload("orders.csv", dianping_csv(rows))
    assert len(utf8) == 1

    gb = parse_upload("orders.csv", dianping_csv(rows, encoding="gb18030"))
    assert len(gb) == 1
    assert gb.iloc[0]["订单号"] == "DP-1"


def test_parse_upload_rejects_bad_files():
    with pytest.raises(IntakeError) as excinfo:
        parse_upload("orders.txt", b"x")
    assert excinfo.value.code == "bad_file"

    with pytest.raises(IntakeError) as excinfo:
        parse_upload("orders.csv", b"")
    assert excinfo.value.code == "bad_file"

    with pytest.raises(IntakeError) as excinfo:
        parse_upload("orders.csv", b"a,b\n1,2\n3,4\n", max_rows=1)
    assert excinfo.value.code == "too_many_rows"

    with pytest.raises(IntakeError):
        parse_upload("orders.csv", b"a,b\n1,2\n", max_columns=1)


def test_validate_requires_mapping_columns(store):
    add_session(store)
    frame = parse_upload("orders.csv", dianping_csv([["DP-1", "s-1", "2", "阿青", "", ""]]))
    mapping = detect_dianping_mapping(frame.columns)
    mapping.pop("external_order_id")
    with pytest.raises(IntakeError) as excinfo:
        validate_dianping_rows(frame, mapping, load_sessions_with_counts(store=store))
    assert excinfo.value.code == "invalid_mapping"


# ---------------------------------------------------------------------------
# 预览与提交
# ---------------------------------------------------------------------------


def dianping_preview(store, rows, *, data_as_of=None):
    frame = parse_upload("orders.csv", dianping_csv(rows))
    mapping = detect_dianping_mapping(frame.columns)
    return validate_dianping_rows(
        frame,
        mapping,
        load_sessions_with_counts(store=store),
        data_as_of=data_as_of,
        bookings=load_bookings(store=store),
        refs=store.load_booking_refs(),
        now=NOW,
    )


def test_preview_does_not_write(store):
    add_session(store)
    preview = dianping_preview(
        store, [["DP-1", "s-1", "2", "阿青", "已完成", "2026-10-21 20:00"]]
    )
    assert preview.valid_rows == 1
    assert preview.rows[0]["action"] == "create"
    assert preview.session_changes[0]["net_delta"] == 2
    assert load_bookings(store=store).empty
    assert counts(store) == (0, 6)


def test_commit_creates_booking_and_ref(store):
    add_session(store)
    preview = dianping_preview(
        store, [["DP-1", "s-1", "2", "阿青", "已完成", "2026-10-21 20:00"]]
    )
    result = commit_import(preview.plan, store=store, now=NOW)
    assert result.imported == 1
    assert result.rejected == 0
    assert counts(store) == (2, 4)

    bookings = load_bookings(store=store)
    assert len(bookings) == 1
    assert bookings.iloc[0]["player_name"] == "阿青"
    refs = store.load_booking_refs()
    assert len(refs) == 1
    assert refs.iloc[0]["source"] == "dianping"
    assert refs.iloc[0]["external_id"] == "DP-1"


def test_reimport_same_file_is_unchanged(store):
    add_session(store)
    rows = [["DP-1", "s-1", "2", "阿青", "已完成", "2026-10-21 20:00"]]
    first = dianping_preview(store, rows)
    commit_import(first.plan, store=store, now=NOW)

    second = dianping_preview(store, rows)
    assert second.valid_rows == 1
    assert second.rows[0]["action"] == "unchanged"
    result = commit_import(second.plan, store=store, now=NOW)
    assert result.unchanged == 1
    assert result.imported == 0
    assert counts(store) == (2, 4)
    assert len(store.load_booking_refs()) == 1


def test_cancel_order_releases_seats(store):
    add_session(store)
    commit_import(
        dianping_preview(
            store, [["DP-1", "s-1", "2", "阿青", "已完成", "2026-10-21 20:00"]]
        ).plan,
        store=store,
        now=NOW,
    )
    cancelled = dianping_preview(
        store, [["DP-1", "s-1", "2", "阿青", "已取消", "2026-10-21 20:00"]]
    )
    assert cancelled.rows[0]["action"] == "cancel"

    result = commit_import(cancelled.plan, store=store, now=NOW)
    assert result.cancelled == 1
    assert counts(store) == (0, 6)
    bookings = load_bookings(store=store)
    assert bookings.iloc[0]["status"] == BOOKING_CANCELLED


def test_over_capacity_rejects_whole_session_group(store):
    add_session(store, seats=6)
    add_session(store, session_id="s-2", script="年轮", seats=8)
    preview = dianping_preview(
        store,
        [
            ["DP-1", "s-1", "4", "阿青", "已完成", "2026-10-21 20:00"],
            ["DP-2", "s-1", "4", "阿岚", "已完成", "2026-10-21 20:00"],
            ["DP-3", "s-2", "2", "小北", "已完成", "2026-10-21 20:00"],
        ],
    )
    assert preview.valid_rows == 1
    assert preview.over_capacity
    assert preview.over_capacity[0]["session_id"] == "s-1"

    result = commit_import(preview.plan, store=store, now=NOW)
    assert result.imported == 1
    assert result.rejected == 2
    assert counts(store, "s-1") == (0, 6)
    assert counts(store, "s-2") == (2, 6)


def test_unmatched_session_is_reported_not_created(store):
    add_session(store)
    preview = dianping_preview(
        store, [["DP-1", "s-missing", "2", "阿青", "已完成", "2026-10-21 20:00"]]
    )
    assert preview.valid_rows == 0
    assert preview.unmatched[0]["code"] == "session_not_found"
    assert len(load_sessions_with_counts(store=store)) == 1


def test_stale_data_warns(store):
    add_session(store)
    preview = dianping_preview(
        store,
        [["DP-1", "s-1", "2", "阿青", "已完成", "2026-10-21 20:00"]],
        data_as_of=NOW - pd.Timedelta(hours=3),
    )
    assert preview.stale is True
    assert any("数据已过期" in warning for warning in preview.warnings)


def test_phone_and_rating_columns_are_not_stored(store):
    add_session(store)
    columns = ["订单号", "场次ID", "数量", "联系人", "手机号", "评价", "订单状态"]
    frame = parse_upload(
        "orders.csv",
        to_csv(
            [["DP-1", "s-1", "2", "阿青", "13800138000", "很好", "已完成"]],
            columns,
        ),
    )
    mapping = detect_dianping_mapping(frame.columns)
    preview = validate_dianping_rows(
        frame,
        mapping,
        load_sessions_with_counts(store=store),
        now=NOW,
    )
    assert "phone" not in preview.mapping
    assert preview.recognized_rating is True
    assert any("电话" in warning for warning in preview.warnings)
    assert any("评价" in warning for warning in preview.warnings)

    commit_import(preview.plan, store=store, now=NOW)
    assert "13800138000" not in store.load_booking_refs().to_string()
    assert "13800138000" not in load_bookings(store=store).to_string()

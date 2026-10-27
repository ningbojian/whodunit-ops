"""场控码与 run-scoped token：单次、过期、统一错误、换码交接。"""

from __future__ import annotations

import pandas as pd
import pytest

from app.store.engine import create_database_engine, ensure_schema
from app.store.sql import SqlStore
from app.tools.dm_control import (
    DMControlError,
    abandon_run,
    apply_action,
    create_run,
    generate_access_code,
    get_run,
    issue_run_code,
    parse_dm_token,
    redeem_code,
    revoke_run_token,
    verify_dm_token,
)
from app.tools.scripts import upsert_script
from app.tools.stages import save_stages
from app.tools.storage import upsert_sessions

SECRET = "auth-test-secret"
NOW = pd.Timestamp("2026-10-20 12:00")


@pytest.fixture()
def store(tmp_path):
    engine = create_database_engine(sqlite_path=tmp_path / "store.sqlite3")
    ensure_schema(engine)
    value = SqlStore(engine, "test-dm-auth")
    value.ensure_tenant()
    yield value
    engine.dispose()


def add_session(store, session_id, start_time):
    upsert_sessions(
        [
            {
                "session_id": session_id,
                "store_name": "迷雾剧场",
                "script_name": "雾隐山庄",
                "start_time": start_time,
                "total_seats": 6,
                "status": "待开场",
            }
        ],
        store=store,
    )


def seed(store):
    upsert_script(
        script_name="雾隐山庄",
        aliases="WYSZ",
        duration_minutes=45,
        store=store,
        now=NOW,
    )
    save_stages(
        store,
        script_name="雾隐山庄",
        stages=[{"name": "开场", "duration_minutes": 10}, {"name": "复盘", "duration_minutes": 15}],
        now=NOW,
    )
    add_session(store, "s-1", "2026-10-20 20:00")


def build(store):
    seed(store)
    return create_run(
        store,
        session_id="s-1",
        created_by="tenant-auth",
        now=NOW,
        secret=SECRET,
    )


# ---------------------------------------------------------------------------
# 场控码
# ---------------------------------------------------------------------------


def test_generated_codes_are_readable_and_unique():
    codes = {generate_access_code() for _ in range(50)}
    assert len(codes) == 50
    alphabet = set("ABCDEFGHJKLMNPQRSTUVWXYZ23456789")
    for code in codes:
        assert len(code) == 8
        assert set(code) <= alphabet
        assert not set(code) & set("0O1I")


def test_code_is_stored_hashed_only(store):
    created = build(store)
    runs = store.load_dm_runs()
    assert created["code"] not in runs.to_string()
    assert len(str(runs.iloc[0]["access_code_hash"])) == 64


def test_code_is_single_use(store):
    created = build(store)
    first = redeem_code(store, code=created["code"], now=NOW, secret=SECRET)
    assert first["token"]

    with pytest.raises(DMControlError) as excinfo:
        redeem_code(store, code=created["code"], now=NOW, secret=SECRET)
    assert excinfo.value.code == "code_invalid"


def test_code_is_case_and_space_insensitive(store):
    created = build(store)
    messy = f" {created['code'][:4].lower()} {created['code'][4:]} "
    redeemed = redeem_code(store, code=messy, now=NOW, secret=SECRET)
    assert redeemed["token"]


def test_code_expires(store):
    seed(store)
    created = create_run(
        store,
        session_id="s-1",
        created_by="tenant-auth",
        now=NOW,
        secret=SECRET,
        code_ttl_minutes=5,
    )
    valid = redeem_code(
        store, code=created["code"], now=NOW + pd.Timedelta(minutes=4), secret=SECRET
    )
    assert valid["token"]

    # 换一场验证过期：ttl 恰好到点即失效。
    add_session(store, "s-2", "2026-10-20 21:00")
    created2 = create_run(
        store,
        session_id="s-2",
        created_by="tenant-auth",
        now=NOW,
        secret=SECRET,
        code_ttl_minutes=5,
    )
    with pytest.raises(DMControlError) as excinfo:
        redeem_code(
            store,
            code=created2["code"],
            now=NOW + pd.Timedelta(minutes=5),
            secret=SECRET,
        )
    assert excinfo.value.code == "code_invalid"


def test_all_code_failures_share_one_message(store):
    created = build(store)
    messages = []

    with pytest.raises(DMControlError) as excinfo:
        redeem_code(store, code="WRONG234", now=NOW, secret=SECRET)
    messages.append((excinfo.value.code, excinfo.value.message))

    redeem_code(store, code=created["code"], now=NOW, secret=SECRET)
    with pytest.raises(DMControlError) as excinfo:
        redeem_code(store, code=created["code"], now=NOW, secret=SECRET)
    messages.append((excinfo.value.code, excinfo.value.message))

    add_session(store, "s-2", "2026-10-20 21:00")
    expired = create_run(
        store,
        session_id="s-2",
        created_by="tenant-auth",
        now=NOW,
        secret=SECRET,
        code_ttl_minutes=5,
    )
    with pytest.raises(DMControlError) as excinfo:
        redeem_code(
            store,
            code=expired["code"],
            now=NOW + pd.Timedelta(minutes=6),
            secret=SECRET,
        )
    messages.append((excinfo.value.code, excinfo.value.message))

    assert {code for code, _ in messages} == {"code_invalid"}
    assert len({message for _, message in messages}) == 1


def test_finished_run_rejects_code(store):
    created = build(store)
    redeemed = redeem_code(store, code=created["code"], now=NOW, secret=SECRET)
    run_id = redeemed["run"]["run_id"]
    apply_action(
        store,
        run_id=run_id,
        action="start",
        client_event_id="evt-start",
        expected_version=redeemed["run"]["state_version"],
        now=NOW,
    )
    apply_action(
        store,
        run_id=run_id,
        action="finish",
        client_event_id="evt-finish",
        now=NOW,
    )
    new_code = None
    with pytest.raises(DMControlError):
        issue_run_code(store, run_id=run_id, now=NOW, secret=SECRET)
    with pytest.raises(DMControlError) as excinfo:
        # 已完成场控的旧哈希已被清空；兑换时必须统一返回 code_invalid。
        redeem_code(store, code=created["code"], now=NOW, secret=SECRET)
    assert excinfo.value.code == "code_invalid"
    assert new_code is None


def test_abandon_invalidates_code(store):
    created = build(store)
    abandon_run(store, run_id=created["run"]["run_id"], now=NOW)
    with pytest.raises(DMControlError) as excinfo:
        redeem_code(store, code=created["code"], now=NOW, secret=SECRET)
    assert excinfo.value.code == "code_invalid"


def test_reissue_invalidates_old_code(store):
    created = build(store)
    reissued = issue_run_code(
        store, run_id=created["run"]["run_id"], now=NOW, secret=SECRET
    )
    with pytest.raises(DMControlError) as excinfo:
        redeem_code(store, code=created["code"], now=NOW, secret=SECRET)
    assert excinfo.value.code == "code_invalid"

    redeemed = redeem_code(store, code=reissued["code"], now=NOW, secret=SECRET)
    assert redeemed["run"]["status"] == "pending"


def test_reissue_keeps_run_state(store):
    created = build(store)
    redeemed = redeem_code(store, code=created["code"], now=NOW, secret=SECRET)
    run_id = redeemed["run"]["run_id"]
    apply_action(
        store,
        run_id=run_id,
        action="start",
        client_event_id="evt-start",
        expected_version=redeemed["run"]["state_version"],
        now=NOW,
    )
    moved = apply_action(
        store,
        run_id=run_id,
        action="next",
        client_event_id="evt-next",
        expected_version=redeemed["run"]["state_version"] + 1,
        now=NOW,
    )
    assert moved["ok"] is True and moved["run"]["current_seq"] == 2

    handoff = issue_run_code(store, run_id=run_id, now=NOW, secret=SECRET)
    new_redeem = redeem_code(store, code=handoff["code"], now=NOW, secret=SECRET)
    assert new_redeem["run"]["current_seq"] == 2
    assert new_redeem["run"]["status"] == "active"


# ---------------------------------------------------------------------------
# DM token
# ---------------------------------------------------------------------------


def test_token_payload_is_scoped(store):
    created = build(store)
    redeemed = redeem_code(store, code=created["code"], now=NOW, secret=SECRET)
    payload = parse_dm_token(redeemed["token"], secret=SECRET, now=NOW)
    assert payload["v"] == 1
    assert payload["t"] == "dm"
    assert payload["tenant"] == "test-dm-auth"
    assert payload["run"] == created["run"]["run_id"]
    assert payload["exp"] > int(NOW.timestamp())


def test_token_is_run_scoped(store):
    created = build(store)
    first = redeem_code(store, code=created["code"], now=NOW, secret=SECRET)

    # 同一场次的另一个 run：用第一个 token 访问第二个 run 必须被拒绝。
    apply_action(
        store,
        run_id=first["run"]["run_id"],
        action="start",
        client_event_id="evt-start",
        expected_version=first["run"]["state_version"],
        now=NOW,
    )
    apply_action(
        store,
        run_id=first["run"]["run_id"],
        action="finish",
        client_event_id="evt-finish",
        now=NOW,
    )
    second = build(store)
    with pytest.raises(DMControlError) as excinfo:
        verify_dm_token(
            store,
            first["token"],
            run_id=second["run"]["run_id"],
            secret=SECRET,
            now=NOW,
        )
    assert excinfo.value.code in {"not_your_run", "token_invalid"}


def test_token_rejects_other_tenant(store, tmp_path):
    created = build(store)
    redeemed = redeem_code(store, code=created["code"], now=NOW, secret=SECRET)

    engine = create_database_engine(sqlite_path=tmp_path / "other.sqlite3")
    ensure_schema(engine)
    other = SqlStore(engine, "another-tenant")
    other.ensure_tenant()
    try:
        with pytest.raises(DMControlError) as excinfo:
            verify_dm_token(store=other, token=redeemed["token"], secret=SECRET, now=NOW)
        assert excinfo.value.code == "not_your_run"
    finally:
        engine.dispose()


def test_token_expiry(store):
    created = build(store)
    redeemed = redeem_code(
        store,
        code=created["code"],
        now=NOW,
        secret=SECRET,
        token_ttl_hours=1,
    )
    with pytest.raises(DMControlError) as excinfo:
        verify_dm_token(
            store,
            redeemed["token"],
            secret=SECRET,
            now=NOW + pd.Timedelta(hours=2),
        )
    assert excinfo.value.code == "token_invalid"


def test_revoked_token_is_rejected(store):
    created = build(store)
    redeemed = redeem_code(store, code=created["code"], now=NOW, secret=SECRET)
    revoke_run_token(store, run_id=created["run"]["run_id"], now=NOW)
    with pytest.raises(DMControlError) as excinfo:
        verify_dm_token(store, redeemed["token"], secret=SECRET, now=NOW)
    assert excinfo.value.code == "token_invalid"
    assert get_run(store, created["run"]["run_id"], now=NOW)["run"]["status"] == "pending"


def test_tampered_token_is_rejected(store):
    created = build(store)
    redeemed = redeem_code(store, code=created["code"], now=NOW, secret=SECRET)
    header, body, signature = redeemed["token"].split(".")
    tampered = ".".join([header, body[:-2] + ("AA" if body[-2:] != "AA" else "BB"), signature])
    with pytest.raises(DMControlError) as excinfo:
        parse_dm_token(tampered, secret=SECRET, now=NOW)
    assert excinfo.value.code == "token_invalid"

    for raw in ("", "abc", "dm1.abc", "bearer x.y.z"):
        with pytest.raises(DMControlError) as excinfo:
            parse_dm_token(raw, secret=SECRET, now=NOW)
        assert excinfo.value.code == "token_invalid"


def test_wrong_secret_is_rejected(store):
    created = build(store)
    redeemed = redeem_code(store, code=created["code"], now=NOW, secret=SECRET)
    with pytest.raises(DMControlError) as excinfo:
        parse_dm_token(redeemed["token"], secret="different-secret", now=NOW)
    assert excinfo.value.code == "token_invalid"

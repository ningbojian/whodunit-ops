"""DM 场控 API：场控码兑换、run-scoped token、动作与事件。

DM 端不需要 Cookie/CSRF；所有请求用 Bearer token，且 token 只能访问
它绑定的那一场 run。
"""

from __future__ import annotations

import hashlib
import threading
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse
from itsdangerous import URLSafeTimedSerializer

from app.integrations.hardware import build_hardware_adapter
from app.store.sql import SqlStore
from app.tools import dm_control
from app.tools.dm_control import DMControlError
from app.web.app_api import _SALT as MERCHANT_SALT

UTC = timezone.utc
router = APIRouter(prefix="/api/dm")

_ATTEMPTS: dict[str, deque[float]] = {}
_ATTEMPTS_LOCK = threading.Lock()
_WINDOW_SECONDS = 600
_MAX_ATTEMPTS = 5


def _settings(request: Request):
    return request.app.state.settings


def _origin_ok(request: Request) -> bool:
    origin = request.headers.get("origin")
    if not origin:
        return True  # Flutter 原生请求没有 Origin
    try:
        expected = urlsplit(_settings(request).public_base_url)
        actual = urlsplit(origin)
    except ValueError:
        return False
    return (actual.scheme, actual.netloc) == (expected.scheme, expected.netloc)


def _deny(code: str, message: str, status_code: int, **extra: Any) -> JSONResponse:
    return JSONResponse(
        {"ok": False, "code": code, "error": message, **extra},
        status_code=status_code,
        headers={"Cache-Control": "no-store"},
    )


def _token_error(exc: DMControlError) -> JSONResponse:
    status = {
        "token_invalid": 401,
        "code_invalid": 401,
        "forbidden": 403,
        "not_your_run": 404,
        "run_not_found": 404,
        "run_finished": 409,
        "stale_state": 409,
        "run_paused": 409,
    }.get(exc.code, 400)
    return _deny(exc.code, exc.message, status)


def _bearer(request: Request) -> str:
    header = request.headers.get("authorization", "")
    if not header.casefold().startswith("bearer "):
        raise DMControlError("token_invalid", "缺少场控令牌。")
    return header[7:].strip()


def _is_merchant_token(request: Request, token: str) -> bool:
    """商家 Bearer 调 DM 接口时明确返回 403，而不是含糊的 401。"""
    if not token or token.startswith("dm1."):
        return False
    try:
        payload = URLSafeTimedSerializer(
            _settings(request).session_secret, salt=MERCHANT_SALT
        ).loads(token, max_age=_settings(request).session_seconds)
    except Exception:  # noqa: BLE001 - 任何解析失败都按普通无效 token 处理
        return False
    return isinstance(payload, dict) and payload.get("t") == "merchant"


def _utc_now(request: Request) -> Any:
    import pandas as pd

    moment = pd.Timestamp(request.app.state.now_fn())
    if moment.tzinfo is not None:
        moment = moment.tz_convert("UTC").tz_localize(None)
    return moment.floor("s")


def _store_for_tenant(request: Request, tenant: str) -> Any:
    settings = _settings(request)
    try:
        if settings.uses_database:
            return SqlStore(request.app.state.engine, tenant)
        return request.app.state.workspace.store_for_tenant(tenant)
    except Exception:  # noqa: BLE001 - 不向调用方泄露租户目录是否存在
        raise DMControlError("token_invalid", "场控令牌无效。") from None


def _verified_run(request: Request, run_id: str | None = None) -> tuple[Any, dict[str, Any], dict[str, Any]]:
    token = _bearer(request)
    try:
        payload = dm_control.parse_dm_token(
            token, secret=_settings(request).session_secret, now=_utc_now(request)
        )
    except DMControlError:
        if _is_merchant_token(request, token):
            raise DMControlError(
                "forbidden", "商家令牌不能调用 DM 接口。"
            ) from None
        raise
    store = _store_for_tenant(request, str(payload.get("tenant") or ""))
    payload, run = dm_control.verify_dm_token(
        store,
        token,
        run_id=run_id or str(payload.get("run") or ""),
        secret=_settings(request).session_secret,
        now=_utc_now(request),
    )
    return store, payload, run


def _attempt_key(request: Request, device_id: str) -> str:
    ip = request.client.host if request.client else "unknown"
    raw = f"{ip}|{device_id}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _allow_attempt(key: str) -> bool:
    now = time.monotonic()
    with _ATTEMPTS_LOCK:
        bucket = _ATTEMPTS.setdefault(key, deque())
        while bucket and now - bucket[0] > _WINDOW_SECONDS:
            bucket.popleft()
        if len(bucket) >= _MAX_ATTEMPTS:
            return False
        bucket.append(now)
        return True


def _hardware(request: Request):
    settings = _settings(request)
    return build_hardware_adapter(
        url=settings.hardware_skill_url,
        token=settings.hardware_skill_token,
        timeout_seconds=settings.hardware_timeout_seconds,
    )


@router.post("/redeem")
async def redeem(request: Request):
    if not _origin_ok(request):
        return _deny("origin_denied", "请求来源不合法。", 403)
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001
        return _deny("bad_request", "请求体必须是 JSON。", 400)
    if not isinstance(payload, dict):
        return _deny("bad_request", "请求体必须是 JSON 对象。", 400)
    code = dm_control.normalize_code(payload.get("code"))
    device_id = str(payload.get("device_id") or "")[:128]
    if not code:
        return _deny("code_invalid", "场控码无效、已过期或已使用。", 401)
    if not _allow_attempt(_attempt_key(request, device_id)):
        return _deny("rate_limited", "尝试次数过多，请稍后再试。", 429)

    settings = _settings(request)
    code_hash = dm_control.hash_access_code(code, secret=settings.session_secret)
    store = None
    if settings.uses_database:
        placeholder = SqlStore(request.app.state.engine, "0" * 16)
        run = dm_control.find_run_by_code_hash(placeholder, code_hash)
        if run is not None:
            store = SqlStore(request.app.state.engine, str(run.get("tenant_id") or ""))
    else:
        store = request.app.state.workspace.find_store_by_code_hash(code_hash)
    if store is None:
        return _deny("code_invalid", "场控码无效、已过期或已使用。", 401)
    try:
        result = dm_control.redeem_code(
            store,
            code=code,
            device_id=device_id,
            now=_utc_now(request),
            secret=settings.session_secret,
            token_ttl_hours=settings.dm_token_hours,
        )
    except DMControlError as exc:
        return _token_error(exc)
    return JSONResponse(
        {"ok": True, **result},
        headers={"Cache-Control": "no-store"},
    )


@router.get("/run")
def get_run(request: Request):
    try:
        store, payload, _run = _verified_run(request)
        result = dm_control.get_run(
            store, str(payload["run"]), for_dm=True, now=_utc_now(request)
        )
    except DMControlError as exc:
        return _token_error(exc)
    return JSONResponse(
        {"ok": True, **result}, headers={"Cache-Control": "no-store"}
    )


@router.post("/run/action")
async def run_action(request: Request):
    if not _origin_ok(request):
        return _deny("origin_denied", "请求来源不合法。", 403)
    try:
        store, payload, _run = _verified_run(request)
    except DMControlError as exc:
        return _token_error(exc)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _deny("bad_request", "请求体必须是 JSON。", 400)
    if not isinstance(body, dict):
        return _deny("bad_request", "请求体必须是 JSON 对象。", 400)
    action = str(body.get("action") or "")
    if not action:
        return _deny("invalid_action", "缺少 action。", 400)
    result = dm_control.apply_action(
        store,
        run_id=str(payload["run"]),
        action=action,
        client_event_id=body.get("client_event_id"),
        expected_version=body.get("expected_version"),
        at=body.get("at"),
        minutes=body.get("minutes"),
        mode=body.get("mode"),
        replay=False,
        hardware=_hardware(request),
        now=_utc_now(request),
    )
    status = 200
    if not result.get("ok"):
        error = str(result.get("code") or "")
        if error == "stale_state":
            status = 409
        elif error in {"run_finished", "run_active", "dm_busy"}:
            status = 409
        elif error == "run_not_found":
            status = 404
        else:
            status = 400
    return JSONResponse(
        result, status_code=status, headers={"Cache-Control": "no-store"}
    )


@router.post("/run/sync")
async def sync_run(request: Request):
    if not _origin_ok(request):
        return _deny("origin_denied", "请求来源不合法。", 403)
    try:
        store, payload, _run = _verified_run(request)
    except DMControlError as exc:
        return _token_error(exc)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _deny("bad_request", "请求体必须是 JSON。", 400)
    actions = body.get("actions") if isinstance(body, dict) else None
    if not isinstance(actions, list):
        return _deny("bad_request", "actions 必须是数组。", 400)
    if len(actions) > 200:
        return _deny("bad_request", "一次最多补传 200 条。", 400)
    result = dm_control.sync_actions(
        store,
        run_id=str(payload["run"]),
        actions=[item for item in actions if isinstance(item, dict)],
        now=_utc_now(request),
    )
    return JSONResponse(
        result, status_code=200, headers={"Cache-Control": "no-store"}
    )


@router.get("/run/events")
def run_events(
    request: Request,
    after: str | None = Query(None),
):
    try:
        store, payload, _run = _verified_run(request)
    except DMControlError as exc:
        return _token_error(exc)
    events = dm_control.list_events(
        store, run_id=str(payload["run"]), after=after, include_payload=False
    )
    return JSONResponse(
        {
            "ok": True,
            "events": events,
            "server_now": dm_control._iso(_utc_now(request)),
        },
        headers={"Cache-Control": "no-store"},
    )

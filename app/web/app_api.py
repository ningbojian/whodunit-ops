"""Flutter 商家端 Bearer API。

与 Web 页面共用 OperationsService；区别只在鉴权：这里不读 Cookie、
不走 CSRF，而是校验 `role=merchant` 的签名 token。DM token 调这些接口
一律 403，商家 token 调 /api/dm/* 也一律 403。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import JSONResponse
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from starlette.concurrency import run_in_threadpool

from app.web.access import (
    AccessExpired,
    AccessNotReady,
    BrowserTokenInvalid,
    RequestCooldown,
    RequestLimitExceeded,
    RequestNotFound,
)
from app.web.auth import _approval_email
from app.web.mailer import EmailSendError

logger = logging.getLogger(__name__)
from app.web.operations import OperationsError
from app.web.security import (
    InvalidEmail,
    constant_time_equal,
    hash_token,
    mask_email,
    new_token,
    normalize_display_name,
    normalize_email,
)

UTC = timezone.utc
router = APIRouter(prefix="/api/app")
_SALT = "jbs-app-merchant"


@dataclass(frozen=True, slots=True)
class MerchantAccess:
    request_id: str
    email_hash: str
    display_name: str
    email_masked: str
    access_expires_at: datetime

    def remaining_seconds(self, now: datetime | None = None) -> int:
        moment = now or datetime.now(UTC)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=UTC)
        return max(0, int((self.access_expires_at - moment).total_seconds()))


def _settings(request: Request):
    return request.app.state.settings


def _now(request: Request) -> datetime:
    """与 Web 会话共用同一个应用时钟，便于测试注入。"""
    moment = request.app.state.now_fn()
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment


def _serializer(request: Request) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(
        _settings(request).session_secret, salt=_SALT
    )


def _issue_token(request: Request, record, now: datetime) -> tuple[str, datetime]:
    settings = _settings(request)
    expires = min(
        now + timedelta(seconds=settings.session_seconds),
        record.access_expires_at,
    )
    token = _serializer(request).dumps(
        {
            "t": "merchant",
            "request": record.id,
            "email_hash": record.email_hash,
            "exp": int(expires.timestamp()),
            "tv": 1,
        }
    )
    return token, expires


def _bearer(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    if not header.casefold().startswith("bearer "):
        return None
    return header[7:].strip()


def _origin_ok(request: Request) -> bool:
    origin = request.headers.get("origin")
    if not origin:
        return True
    try:
        expected = urlsplit(_settings(request).public_base_url)
        actual = urlsplit(origin)
    except ValueError:
        return False
    return (actual.scheme, actual.netloc) == (expected.scheme, expected.netloc)


def _deny(code: str, message: str, status_code: int) -> JSONResponse:
    return JSONResponse(
        {"ok": False, "code": code, "error": message},
        status_code=status_code,
        headers={"Cache-Control": "no-store"},
    )


def require_merchant(request: Request) -> MerchantAccess:
    token = _bearer(request)
    if not token:
        raise OperationsError("unauthorized", "缺少商家令牌。", 401)
    if token.startswith("dm1."):
        # DM 场控令牌只对 /api/dm/* 有效，不能拿来读商家数据。
        raise OperationsError("forbidden", "场控令牌不能调用商家接口。", 403)
    try:
        payload = _serializer(request).loads(
            token, max_age=_settings(request).session_seconds
        )
    except SignatureExpired:
        raise OperationsError("token_expired", "商家令牌已过期。", 401) from None
    except BadSignature:
        raise OperationsError("token_invalid", "商家令牌无效。", 401) from None
    if not isinstance(payload, dict) or payload.get("t") != "merchant":
        raise OperationsError("token_invalid", "商家令牌无效。", 401)
    settings = _settings(request)
    now = _now(request)
    if int(payload.get("exp") or 0) <= int(now.timestamp()):
        raise OperationsError("token_expired", "商家令牌已过期。", 401)
    record = request.app.state.access_store.get_request(str(payload.get("request") or ""))
    if record is None or record.email_hash != payload.get("email_hash"):
        raise OperationsError("token_invalid", "商家令牌无效。", 401)
    if record.effective_status(now) != "approved":
        raise OperationsError("access_expired", "访问已到期，请重新申请。", 401)
    if record.access_expires_at is None:
        raise OperationsError("access_expired", "访问已到期，请重新申请。", 401)
    return MerchantAccess(
        request_id=record.id,
        email_hash=record.email_hash,
        display_name=record.display_name,
        email_masked=mask_email(record.email),
        access_expires_at=record.access_expires_at,
    )


def _ops(request: Request):
    return request.app.state.operations


def _ok(data: dict[str, Any], status_code: int = 200) -> JSONResponse:
    return JSONResponse(
        {"ok": True, **data},
        status_code=status_code,
        headers={"Cache-Control": "no-store"},
    )


async def _body(request: Request) -> dict[str, Any]:
    if not _origin_ok(request):
        raise OperationsError("origin_denied", "请求来源不合法。", 403)
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001
        raise OperationsError("bad_request", "请求体必须是 JSON。") from None
    if not isinstance(payload, dict):
        raise OperationsError("bad_request", "请求体必须是 JSON 对象。")
    return payload


# ---------------------------------------------------------------------------
# 登录与访问
# ---------------------------------------------------------------------------


@router.post("/auth/request")
async def auth_request(request: Request):
    if not _origin_ok(request):
        return _deny("origin_denied", "请求来源不合法。", 403)
    payload = await _body(request)
    settings = _settings(request)
    try:
        email = normalize_email(str(payload.get("email") or ""))
        display = normalize_display_name(
            str(payload.get("display_name") or ""), fallback_email=email
        )
    except InvalidEmail as exc:
        return _deny("invalid_email", str(exc), 400)
    browser_token = new_token(32)
    client_ip = request.client.host if request.client else None
    try:
        created = request.app.state.access_store.create_request(
            email,
            display,
            browser_token=browser_token,
            requester_ip=client_ip,
            approval_ttl_hours=settings.approval_ttl_hours,
            cooldown_minutes=settings.request_cooldown_minutes,
            ip_limit_per_hour=settings.requests_per_ip_per_hour,
            global_limit_per_day=settings.requests_global_per_day,
        )
    except RequestCooldown:
        return _deny("request_cooldown", "申请过于频繁，请稍后再试。", 429)
    except RequestLimitExceeded:
        return _deny("request_limited", "提交次数过多，请稍后再试。", 429)
    except ValueError:
        return _deny("invalid_email", "邮箱格式不正确。", 400)
    sent = True
    try:
        # mailer.send 内部是同步 httpx.post（超时 10 秒）。这条路由是
        # async def，直接调用会卡住整个事件循环，单 worker 下所有并发
        # 请求（页面轮询、静态资源）一起排队。放进线程池即可。
        await run_in_threadpool(
            request.app.state.mailer.send,
            _approval_email(settings, created.request, created.approval_token),
        )
    except EmailSendError as exc:
        sent = False
        # 移动端没有页面兜底，owner 只能从日志知道审批邮件没发出去。
        logger.warning(
            "approval email send failed request=%s reason=%s",
            created.request.id,
            exc.safe_reason(),
        )
        request.app.state.access_store.mark_email_failed(
            created.request.id, exc.safe_reason()
        )
    return _ok(
        {
            "request_id": created.request.id,
            "browser_token": browser_token,
            "status": created.request.effective_status(_now(request)),
            "approval_email_sent": sent,
        },
        status_code=201,
    )


@router.get("/auth/status")
def auth_status(request: Request):
    request_id = request.query_params.get("request_id", "")
    browser_token = request.headers.get("x-browser-token", "")
    record = request.app.state.access_store.get_request(request_id)
    if (
        record is None
        or not browser_token
        or not constant_time_equal(hash_token(browser_token), record.browser_token_hash)
    ):
        return _deny("request_not_found", "申请不存在。", 404)
    now = _now(request)
    status = record.effective_status(now)
    return _ok(
        {
            "status": status,
            "claimable": status == "approved",
            "access_expires_at": (
                record.access_expires_at.isoformat(timespec="seconds")
                if record.access_expires_at
                else None
            ),
        }
    )


@router.post("/auth/claim")
async def auth_claim(request: Request):
    payload = await _body(request)
    request_id = str(payload.get("request_id") or "")
    browser_token = str(payload.get("browser_token") or "")
    try:
        record = request.app.state.access_store.claim(request_id, browser_token)
    except RequestNotFound:
        return _deny("claim_denied", "申请不存在。", 403)
    except BrowserTokenInvalid:
        return _deny("claim_denied", "浏览器凭证不匹配。", 403)
    except AccessNotReady as exc:
        return _deny("not_ready", "申请尚未批准。", 409)
    except AccessExpired:
        return _deny("access_expired", "访问已到期，请重新申请。", 401)
    now = _now(request)
    token, expires = _issue_token(request, record, now)
    return _ok(
        {
            "token": token,
            "token_expires_at": expires.isoformat(timespec="seconds"),
            "access_expires_at": record.access_expires_at.isoformat(timespec="seconds"),
            "remaining_seconds": record.access_remaining_seconds(now),
            "email_masked": mask_email(record.email),
            "display_name": record.display_name,
        }
    )


@router.get("/me")
def me(request: Request, merchant: MerchantAccess = Depends(require_merchant)):
    return _ok(
        {
            "email_masked": merchant.email_masked,
            "display_name": merchant.display_name,
            "access_expires_at": merchant.access_expires_at.isoformat(timespec="seconds"),
            "remaining_seconds": merchant.remaining_seconds(_now(request)),
        }
    )


# ---------------------------------------------------------------------------
# 商家业务
# ---------------------------------------------------------------------------


@router.post("/chat")
async def chat(
    request: Request,
    merchant: MerchantAccess = Depends(require_merchant),
):
    payload = await _body(request)
    message = payload.get("message")
    if not isinstance(message, str):
        raise OperationsError("bad_request", "消息不能为空。")
    result = await run_in_threadpool(
        request.app.state.runtime.chat, merchant.email_hash, message
    )
    return _ok(
        {
            "answer": result.answer,
            "trace": result.trace,
            "choices": list(result.choices),
            "remaining_seconds": merchant.remaining_seconds(),
        }
    )


@router.get("/resources")
def resources(
    request: Request,
    merchant: MerchantAccess = Depends(require_merchant),
):
    return _ok(_ops(request).resources(merchant.email_hash))


@router.get("/schedule")
def schedule(
    request: Request,
    merchant: MerchantAccess = Depends(require_merchant),
    start: str | None = None,
    end: str | None = None,
):
    return _ok(_ops(request).schedule_view(merchant.email_hash, start, end))


@router.post("/sessions")
async def create_session(
    request: Request,
    merchant: MerchantAccess = Depends(require_merchant),
):
    payload = await _body(request)
    # 兼容 {fields: {...}} 包装与平铺两种请求体（旧客户端字段形状）。
    if isinstance(payload.get("fields"), dict):
        merged = {k: v for k, v in payload.items() if k != "fields"}
        merged.update(payload["fields"])
        payload = merged
    return _ok(_ops(request).create_session(merchant.email_hash, payload), 201)


@router.patch("/sessions/{session_id}")
async def update_session(
    request: Request,
    session_id: str,
    merchant: MerchantAccess = Depends(require_merchant),
):
    payload = await _body(request)
    return _ok(
        _ops(request).update_session(merchant.email_hash, session_id, payload)
    )


def _preview_flag(payload: dict[str, Any]) -> bool:
    """preview 默认 True（保护未升级客户端）；显式 false 才进入写入模式。"""
    value = payload.get("preview", True)
    if isinstance(value, str):
        return value.casefold() not in {"0", "false", "no"}
    return bool(value)


@router.post("/schedule/auto-assign")
async def auto_assign(
    request: Request,
    merchant: MerchantAccess = Depends(require_merchant),
):
    payload = await _body(request)
    ids = payload.get("session_ids")
    if ids is not None and not isinstance(ids, list):
        raise OperationsError("bad_request", "session_ids 必须是数组。")
    preview = _preview_flag(payload)
    if not preview and not payload.get("confirm"):
        raise OperationsError(
            "confirm_required", "直接写入分配需要 confirm:true；建议先用预览审核。"
        )
    return _ok(
        _ops(request).auto_assign(
            merchant.email_hash,
            [str(item) for item in ids] if ids else None,
            trigger="app_auto",
            preview=preview,
        )
    )


@router.post("/schedule/apply")
async def apply_schedule(
    request: Request,
    merchant: MerchantAccess = Depends(require_merchant),
):
    payload = await _body(request)
    assignments = payload.get("assignments")
    if not isinstance(assignments, list):
        raise OperationsError("bad_request", "assignments 必须是数组。")
    return _ok(
        _ops(request).apply_schedule(
            merchant.email_hash,
            [item for item in assignments if isinstance(item, dict)],
            confirm=bool(payload.get("confirm")),
        )
    )


@router.post("/schedule/resolve")
async def resolve(
    request: Request,
    merchant: MerchantAccess = Depends(require_merchant),
):
    payload = await _body(request)
    return _ok(
        _ops(request).resolve_conflict(
            merchant.email_hash,
            str(payload.get("session_id") or ""),
            str(payload.get("choice") or ""),
            payload.get("target_id"),
        )
    )


@router.post("/bookings/quick")
async def quick_booking(
    request: Request,
    merchant: MerchantAccess = Depends(require_merchant),
):
    payload = await _body(request)
    result = _ops(request).quick_booking(merchant.email_hash, payload)
    if not result.get("ok"):
        status = 409 if result.get("code") == "capacity_exceeded" else 400
        return JSONResponse(result, status_code=status)
    return _ok(result)


async def _upload(request: Request, file: UploadFile) -> tuple[str, bytes]:
    limit = int(getattr(request.app.state.settings, "max_import_bytes", 2 * 1024 * 1024))
    content = await file.read(limit + 1)
    if len(content) > limit:
        raise OperationsError("file_too_large", "文件超过上传大小限制。", 413)
    return file.filename or "upload.csv", content


@router.post("/imports/dianping/preview")
async def preview_import(
    request: Request,
    file: UploadFile = File(...),
    data_as_of: str | None = Form(None),
    merchant: MerchantAccess = Depends(require_merchant),
):
    filename, content = await _upload(request, file)
    return _ok(
        _ops(request).preview_dianping(
            merchant.email_hash, filename, content, data_as_of=data_as_of
        )
    )


@router.post("/imports/dianping/commit")
async def commit_import(
    request: Request,
    file: UploadFile = File(...),
    data_as_of: str | None = Form(None),
    confirm: str = Form("false"),
    merchant: MerchantAccess = Depends(require_merchant),
):
    filename, content = await _upload(request, file)
    return _ok(
        _ops(request).commit_dianping(
            merchant.email_hash,
            filename,
            content,
            data_as_of=data_as_of,
            confirm=str(confirm).casefold() in {"1", "true", "yes", "on"},
        )
    )


@router.get("/runs")
def runs(
    request: Request,
    merchant: MerchantAccess = Depends(require_merchant),
    day: str | None = None,
):
    return _ok(_ops(request).runs_today(merchant.email_hash, day))


@router.post("/runs")
async def create_run(
    request: Request,
    merchant: MerchantAccess = Depends(require_merchant),
):
    payload = await _body(request)
    return _ok(
        _ops(request).create_run(
            merchant.email_hash,
            str(payload.get("session_id") or ""),
            dm_id=payload.get("dm_id") or None,
            mode=str(payload.get("mode") or "software"),
        ),
        201,
    )


@router.get("/runs/{run_id}")
def run_detail(
    request: Request,
    run_id: str,
    merchant: MerchantAccess = Depends(require_merchant),
):
    return _ok(_ops(request).run_view(merchant.email_hash, run_id))


@router.post("/runs/{run_id}/code")
async def run_code(
    request: Request,
    run_id: str,
    merchant: MerchantAccess = Depends(require_merchant),
):
    payload = await _body(request)
    return _ok(
        _ops(request).issue_run_code(
            merchant.email_hash,
            run_id,
            dm_id=payload.get("dm_id") or None,
            dm_name=payload.get("dm_name") or None,
        )
    )


@router.post("/runs/{run_id}/revoke")
def run_revoke(
    request: Request,
    run_id: str,
    merchant: MerchantAccess = Depends(require_merchant),
):
    return _ok(_ops(request).revoke_run_token(merchant.email_hash, run_id))

"""申请、等待、审批、备用链接、退出，以及 Session 依赖。

访问控制的核心：
  - 未登录只能看到申请页；
  - 申请后 Session 只保存 pending 申请 ID 与浏览器 token；
  - 开发者批准后，用户页面轮询 claim，或使用审批页生成的备用链接；
  - 正式 Session 只保存 email_hash / request_id / 到期时间 / CSRF，
    不保存明文邮箱和访问 token；
  - 每个请求都回查 SQLite，访问到期或被撤销后立即失效。
"""

from __future__ import annotations

import html
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from app.web.access import (
    AccessExpired as StoreAccessExpired,
    AccessNotReady,
    AccessRequest,
    AccessStore,
    AccessTokenInvalid,
    ApprovalTokenExpired,
    ApprovalTokenInvalid,
    AlreadyProcessed,
    BrowserTokenInvalid,
    RequestCooldown,
    RequestLimitExceeded,
    RequestNotFound,
)
from app.web.mailer import EmailMessage, EmailSendError
from app.web.security import (
    InvalidEmail,
    csrf_ok,
    mask_email,
    new_token,
    normalize_display_name,
    normalize_email,
    same_origin_from_headers,
)

logger = logging.getLogger("app.web.auth")
CST = timezone(timedelta(hours=8))
UTC = timezone.utc


def hours_label(hours: float) -> str:
    """把小时数换成给人看的说法：整天的用「天」，其余仍用「小时」。

    默认访问时长是一个月（720 小时），页面上写「720 小时」既难读也容易
    被当成笔误；同时保留小数与非整天的原样输出，避免出现「1.5 天」。
    """
    if hours >= 48 and hours % 24 == 0:
        return f"{int(hours // 24)} 天"
    return f"{hours:g} 小时"


def _granted_hours(record: "AccessRequest | None") -> str:
    """这条记录实际被批准的访问时长（用于到期页文案）。

    不能拿当前配置顶替：改过 `WEB_ACCESS_HOURS` 之后，旧记录仍然按批准
    当时的时长到期，报配置里的数字就等于对用户说假话。拿不到记录时返回
    空串，调用方改用不提具体时长的说法。
    """
    if record is None or record.approved_at is None or record.access_expires_at is None:
        return ""
    seconds = (
        _as_utc(record.access_expires_at) - _as_utc(record.approved_at)
    ).total_seconds()
    if seconds <= 0:
        return ""
    return hours_label(seconds / 3600)


class AuthError(RuntimeError):
    status_code = 401
    code = "unauthorized"
    redirect_to = "/login"

    def __init__(self, message: str = "请先申请访问。") -> None:
        self.message = message
        super().__init__(message)


class SessionRequired(AuthError):
    status_code = 401
    code = "login_required"
    redirect_to = "/login"


class SessionExpired(AuthError):
    status_code = 401
    code = "access_expired"
    redirect_to = "/login?expired=1"

    def __init__(self) -> None:
        super().__init__("访问已到期，请重新申请。")


class CsrfError(RuntimeError):
    status_code = 403
    code = "csrf_failed"

    def __init__(self) -> None:
        super().__init__("请求校验失败，请刷新页面后重试。")


@dataclass(frozen=True, slots=True)
class AccessContext:
    record: AccessRequest
    email_hash: str
    display_name: str
    access_expires_at: datetime

    @property
    def email_masked(self) -> str:
        return mask_email(self.record.email)

    def remaining_seconds(self, now: datetime | None = None) -> int:
        moment = _as_utc(now) if now is not None else datetime.now(UTC)
        return max(0, int((self.access_expires_at - moment).total_seconds()))


@dataclass(frozen=True, slots=True)
class PendingContext:
    record: AccessRequest
    request_id: str
    browser_token: str


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _now(request: Request) -> datetime:
    value = request.app.state.now_fn()
    return _as_utc(value)


def _store(request: Request) -> AccessStore:
    return request.app.state.access_store


def _settings(request: Request):
    return request.app.state.settings


def _templates(request: Request):
    return request.app.state.templates


def _ensure_csrf(request: Request) -> str:
    """确保当前 Session 有 CSRF token；登录/审批页没有登录态也要有。"""
    csrf = request.session.get("csrf")
    if not csrf:
        csrf = new_token(24)
        request.session["csrf"] = csrf
    return str(csrf)


def _render(
    request: Request,
    name: str,
    context: dict,
    *,
    status_code: int = 200,
    headers: dict[str, str] | None = None,
) -> HTMLResponse:
    settings = _settings(request)
    body = {
        "request": request,
        "product_name": "剧本杀拼场运营助手",
        "csrf": _ensure_csrf(request),
        "access_label": hours_label(settings.access_hours),
        # 本地开发用 console 模式时，邮件只打印在服务端终端；页面明确提示，
        # 避免把“没收到邮件”误判成发送失败。生产环境禁止 console。
        "console_email": settings.email_backend == "console",
        **context,
    }
    final_headers = {"Cache-Control": "no-store"}
    if headers:
        final_headers.update(headers)
    return _templates(request).TemplateResponse(
        request=request,
        name=name,
        context=body,
        status_code=status_code,
        headers=final_headers,
    )


def _set_access_session(request: Request, record: AccessRequest) -> None:
    request.session.clear()
    request.session["request_id"] = record.id
    request.session["email_hash"] = record.email_hash
    request.session["display_name"] = record.display_name
    request.session["access_expires_at"] = _as_utc(record.access_expires_at).isoformat(
        timespec="seconds"
    )
    request.session["csrf"] = new_token(24)


def _set_pending_session(
    request: Request,
    record: AccessRequest,
    browser_token: str,
) -> None:
    request.session.clear()
    request.session["pending_request_id"] = record.id
    request.session["pending_browser_token"] = browser_token
    request.session["csrf"] = new_token(24)


def _build_access(request: Request) -> AccessContext:
    session = request.session
    request_id = session.get("request_id")
    email_hash = session.get("email_hash")
    expiry_raw = session.get("access_expires_at")
    if not request_id or not email_hash or not expiry_raw:
        raise SessionRequired()
    try:
        expiry = _as_utc(datetime.fromisoformat(str(expiry_raw)))
    except (TypeError, ValueError):
        session.clear()
        raise SessionRequired() from None

    now = _now(request)
    if expiry <= now:
        session.clear()
        raise SessionExpired()

    record = _store(request).get_request(str(request_id))
    if record is None or record.email_hash != email_hash:
        session.clear()
        raise SessionRequired()
    if record.effective_status(now) != "approved":
        session.clear()
        raise SessionExpired()

    access_expiry = record.access_expires_at
    if access_expiry is None or _as_utc(access_expiry) <= now:
        session.clear()
        raise SessionExpired()
    return AccessContext(
        record=record,
        email_hash=record.email_hash,
        display_name=record.display_name,
        access_expires_at=_as_utc(access_expiry),
    )


def current_access(request: Request) -> AccessContext | None:
    try:
        return _build_access(request)
    except AuthError:
        return None


def require_access(request: Request) -> AccessContext:
    return _build_access(request)


def _pending(request: Request) -> PendingContext | None:
    session = request.session
    request_id = session.get("pending_request_id")
    browser_token = session.get("pending_browser_token")
    if not request_id or not browser_token:
        return None
    record = _store(request).get_request(str(request_id))
    if record is None:
        session.pop("pending_request_id", None)
        session.pop("pending_browser_token", None)
        return None
    if not session.get("csrf"):
        session["csrf"] = new_token(24)
    return PendingContext(
        record=record,
        request_id=record.id,
        browser_token=str(browser_token),
    )


def require_pending(request: Request) -> PendingContext:
    pending = _pending(request)
    if pending is None:
        raise SessionRequired("没有待审批的申请，请重新提交。")
    return pending


def _live_pending(request: Request) -> PendingContext | None:
    """只返回仍可等待/可领取的申请。

    已拒绝、已过期、发信失败的申请仍让 /pending 展示原因，但不能再把
    用户从 /login 或“重新申请”入口弹回等待页，否则会形成跳转循环。
    """
    pending = _pending(request)
    if pending is None:
        return None
    status = pending.record.effective_status(_now(request))
    if status in ("pending", "approved"):
        return pending
    return None


def require_csrf(request: Request) -> None:
    expected = request.session.get("csrf")
    provided = request.headers.get("x-csrf-token")
    if not csrf_ok(provided, expected):
        raise CsrfError()


def _format_cst(value: datetime | None) -> str:
    if value is None:
        return "—"
    return _as_utc(value).astimezone(CST).strftime("%Y-%m-%d %H:%M")


def _approval_email(settings, record: AccessRequest, token: str) -> EmailMessage:
    approve_url = f"{settings.public_base_url}/approve/{quote(token, safe='')}"
    text = (
        "收到一条网页版访问申请。\n\n"
        f"申请编号：{record.id}\n"
        f"称呼：{record.display_name}\n"
        f"邮箱：{record.email}\n"
        f"申请时间（UTC+8）：{_format_cst(record.created_at)}\n\n"
        f"审批链接：{approve_url}\n\n"
        f"批准后该用户可在 {hours_label(settings.access_hours)}内使用；"
        "到期后需要重新申请并由你再次批准。\n"
        f"审批链接 {settings.approval_ttl_hours:g} 小时内有效，且只能处理一次。\n"
    )
    safe_name = html.escape(record.display_name)
    safe_email = html.escape(record.email)
    safe_url = html.escape(approve_url, quote=True)
    body = (
        "<p>收到一条网页版访问申请。</p>"
        f"<p>申请编号：{html.escape(record.id)}<br>"
        f"称呼：{safe_name}<br>"
        f"邮箱：{safe_email}<br>"
        f"申请时间（UTC+8）：{html.escape(_format_cst(record.created_at))}</p>"
        f'<p><a href="{safe_url}">打开审批页</a></p>'
        f"<p>批准后 {hours_label(settings.access_hours)}内可用；"
        f"审批链接 {settings.approval_ttl_hours:g} 小时内有效，只能处理一次。</p>"
    )
    return EmailMessage(
        to=settings.owner_email,
        subject="【剧本杀运营助手】新的网页版访问申请",
        text=text,
        html=body,
    )


router = APIRouter()


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, expired: int = 0):
    if current_access(request) is not None:
        return RedirectResponse("/", status_code=303)
    if _live_pending(request) is not None:
        return RedirectResponse("/pending", status_code=303)
    notice = "访问已到期，请重新提交申请。" if expired else ""
    return _render(request, "login.html", {"notice": notice, "error": ""})


@router.post("/request", response_class=HTMLResponse)
def submit_request(
    request: Request,
    email: str = Form(""),
    display_name: str = Form(""),
    csrf: str = Form(""),
):
    # 表单里的 CSRF token 与同源检查二者其一通过即可。
    # 浏览器经端口转发 / IDE 预览时，Origin 可能被代理重写；只要页面
    # 能拿到服务端签发的 token，仍然可以证明表单来自本应用。
    if not (
        csrf_ok(csrf, request.session.get("csrf"))
        or same_origin_from_headers(request.headers)
    ):
        return _render(
            request,
            "login.html",
            {"error": "请求来源校验失败，请刷新页面后重试。"},
            status_code=403,
        )
    if current_access(request) is not None:
        return RedirectResponse("/", status_code=303)

    settings = _settings(request)
    try:
        normalized = normalize_email(email)
        display = normalize_display_name(display_name, fallback_email=normalized)
    except InvalidEmail as exc:
        return _render(
            request,
            "login.html",
            {"error": str(exc), "email": email, "display_name": display_name},
            status_code=400,
        )

    browser_token = new_token(32)
    client_ip = request.client.host if request.client else None
    try:
        created = _store(request).create_request(
            normalized,
            display,
            browser_token=browser_token,
            requester_ip=client_ip,
            approval_ttl_hours=settings.approval_ttl_hours,
            cooldown_minutes=settings.request_cooldown_minutes,
            ip_limit_per_hour=settings.requests_per_ip_per_hour,
            global_limit_per_day=settings.requests_global_per_day,
        )
    except RequestCooldown as exc:
        live = _live_pending(request)
        if live is not None and live.request_id == exc.request.id:
            return RedirectResponse("/pending", status_code=303)
        minutes = max(1, exc.retry_after_seconds // 60) if exc.retry_after_seconds else 0
        latest_status = exc.request.effective_status(_now(request))
        message = {
            "pending": "该邮箱已有一条待审批申请，请等待开发者处理。",
            "rejected": "上一条申请已被拒绝，需要重新申请。",
            "expired": "上一条申请已过期，需要重新申请。",
            "email_failed": "上次审批邮件发送失败，需要重新提交。",
        }.get(latest_status, "该邮箱刚提交过申请，请稍后再试。")
        if minutes:
            message += f"（约 {minutes} 分钟后可再次提交）"
        return _render(
            request,
            "login.html",
            {"error": message, "email": normalized, "display_name": display},
            status_code=429,
        )
    except RequestLimitExceeded as exc:
        minutes = max(1, exc.retry_after_seconds // 60)
        scope = "该网络" if exc.scope == "ip" else "全站"
        return _render(
            request,
            "login.html",
            {
                "error": f"{scope}申请次数已用完，请约 {minutes} 分钟后重试。",
                "email": normalized,
                "display_name": display,
            },
            status_code=429,
        )
    except ValueError as exc:
        return _render(
            request,
            "login.html",
            {"error": str(exc), "email": email, "display_name": display_name},
            status_code=400,
        )

    try:
        request.app.state.mailer.send(
            _approval_email(settings, created.request, created.approval_token)
        )
    except EmailSendError as exc:
        _store(request).mark_email_failed(created.request.id, exc.safe_reason())
        logger.warning("approval email failed request=%s type=%s", created.request.id, exc.reason)
        return _render(
            request,
            "login.html",
            {"error": "审批邮件暂时发送失败，请稍后重试。"},
            status_code=503,
        )
    except Exception as exc:  # noqa: BLE001
        _store(request).mark_email_failed(created.request.id, type(exc).__name__)
        logger.warning(
            "approval email failed request=%s type=%s",
            created.request.id,
            type(exc).__name__,
        )
        return _render(
            request,
            "login.html",
            {"error": "审批邮件暂时发送失败，请稍后重试。"},
            status_code=503,
        )

    _set_pending_session(request, created.request, browser_token)
    if created.replaces_active is not None:
        # 只影响这一页的解释文案；访问本身仍要等开发者批准。
        request.session["pending_replaces_active"] = True
    return RedirectResponse("/pending", status_code=303)


@router.get("/pending", response_class=HTMLResponse)
def pending_page(request: Request):
    if current_access(request) is not None:
        return RedirectResponse("/", status_code=303)
    pending = _pending(request)
    if pending is None:
        return RedirectResponse("/login", status_code=303)
    return _render(
        request,
        "pending.html",
        {
            "record": pending.record,
            "status": pending.record.effective_status(_now(request)),
            "email_masked": mask_email(pending.record.email),
            "created_at": _format_cst(pending.record.created_at),
            "approval_expires_at": _format_cst(pending.record.approval_expires_at),
            "replaces_active": bool(request.session.pop("pending_replaces_active", False)),
        },
    )


@router.get("/api/access/status")
def access_status(request: Request):
    access = current_access(request)
    if access is not None:
        return JSONResponse(
            {
                "ok": True,
                "status": "approved",
                "redirect": "/",
                "access_expires_at": access.access_expires_at.isoformat(
                    timespec="seconds"
                ),
                "remaining_seconds": access.remaining_seconds(_now(request)),
            }
        )

    pending = _pending(request)
    if pending is None:
        return JSONResponse({"ok": True, "status": "none"})
    now = _now(request)
    payload = pending.record.public_dict(now)
    payload.update({"ok": True, "claimable": payload["status"] == "approved"})
    return JSONResponse(payload)


@router.post("/api/access/claim")
def claim_access(
    request: Request,
    pending: PendingContext = Depends(require_pending),
    _csrf: None = Depends(require_csrf),
):
    try:
        record = _store(request).claim(pending.request_id, pending.browser_token)
    except RequestNotFound:
        return JSONResponse(
            {"ok": False, "code": "claim_denied", "error": "申请不存在。"},
            status_code=403,
        )
    except BrowserTokenInvalid:
        return JSONResponse(
            {"ok": False, "code": "claim_denied", "error": "浏览器凭证不匹配。"},
            status_code=403,
        )
    except AccessNotReady as exc:
        return JSONResponse(
            {
                "ok": False,
                "code": "not_ready",
                "status": exc.status,
                "error": "申请尚未批准。",
            },
            status_code=409,
        )
    except StoreAccessExpired:
        return JSONResponse(
            {"ok": False, "code": "access_expired", "error": "访问已到期，请重新申请。"},
            status_code=401,
        )

    _set_access_session(request, record)
    now = _now(request)
    return JSONResponse(
        {
            "ok": True,
            "redirect": "/",
            "access_expires_at": _as_utc(record.access_expires_at).isoformat(
                timespec="seconds"
            ),
            "remaining_seconds": record.access_remaining_seconds(now),
        }
    )


def _approve_context(
    settings,
    record: AccessRequest | None,
    *,
    status: str,
    magic_link: str | None = None,
    message: str = "",
) -> dict:
    return {
        "record": record,
        "status": status,
        "valid": record is not None,
        "email": record.email if record else "",
        "email_masked": mask_email(record.email) if record else "",
        "created_at": _format_cst(record.created_at) if record else "—",
        "approval_expires_at": (
            _format_cst(record.approval_expires_at) if record else "—"
        ),
        "access_expires_at": (
            _format_cst(record.access_expires_at) if record and record.access_expires_at else "—"
        ),
        "access_label": hours_label(settings.access_hours),
        "magic_link": magic_link,
        "message": message,
    }


@router.get("/approve/{token}", response_class=HTMLResponse)
def approve_page(request: Request, token: str):
    settings = _settings(request)
    record = _store(request).get_by_approval_token(token)
    if record is None:
        return _render(
            request,
            "approve.html",
            _approve_context(settings, None, status="invalid", message="审批链接无效。"),
            status_code=404,
        )
    status = record.effective_status(_now(request))
    message = {
        "pending": "请确认申请信息后选择批准或拒绝。",
        "approved": "这条申请已经批准过；备用链接只在批准时显示一次。",
        "rejected": "这条申请已被拒绝。",
        "expired": "审批链接已过期，请让用户重新申请。",
        "email_failed": "审批邮件发送失败，申请不可用。",
    }.get(status, "当前状态不可处理。")
    return _render(
        request,
        "approve.html",
        _approve_context(settings, record, status=status, message=message),
    )


@router.post("/approve/{token}", response_class=HTMLResponse)
def process_approval(
    request: Request,
    token: str,
    action: str = Form(""),
    csrf: str = Form(""),
):
    settings = _settings(request)
    if not (
        csrf_ok(csrf, request.session.get("csrf"))
        or same_origin_from_headers(request.headers)
    ):
        return _render(
            request,
            "approve.html",
            _approve_context(
                settings,
                None,
                status="invalid",
                message="请求来源校验失败，请重新打开邮件里的审批链接。",
            ),
            status_code=403,
        )
    if action not in {"approve", "reject"}:
        record = _store(request).get_by_approval_token(token)
        return _render(
            request,
            "approve.html",
            _approve_context(
                settings,
                record,
                status=record.effective_status(_now(request)) if record else "invalid",
                message="未知操作。",
            ),
            status_code=400,
        )

    try:
        if action == "approve":
            approved = _store(request).approve(
                token,
                access_hours=settings.access_hours,
            )
            magic = (
                f"{settings.public_base_url}/unlock?"
                f"token={quote(approved.access_token, safe='')}"
            )
            return _render(
                request,
                "approve.html",
                _approve_context(
                    settings,
                    approved.request,
                    status="approved",
                    magic_link=magic,
                    message="已批准。用户页面会自动解锁；也可以把备用链接发给用户。",
                ),
            )
        rejected = _store(request).reject(token)
        return _render(
            request,
            "approve.html",
            _approve_context(
                settings,
                rejected,
                status="rejected",
                message="已拒绝这条申请。",
            ),
        )
    except AlreadyProcessed as exc:
        return _render(
            request,
            "approve.html",
            _approve_context(
                settings,
                exc.request,
                status=exc.request.effective_status(_now(request)),
                message="这条申请已经处理过，不能重复操作。",
            ),
            status_code=409,
        )
    except ApprovalTokenExpired:
        record = _store(request).get_by_approval_token(token)
        return _render(
            request,
            "approve.html",
            _approve_context(
                settings,
                record,
                status="expired",
                message="审批链接已过期，请让用户重新申请。",
            ),
            status_code=410,
        )
    except ApprovalTokenInvalid:
        return _render(
            request,
            "approve.html",
            _approve_context(
                settings,
                None,
                status="invalid",
                message="审批链接无效。",
            ),
            status_code=404,
        )


@router.get("/unlock")
def unlock(request: Request, token: str = Query("")):
    try:
        record = _store(request).validate_access_token(token)
    except AccessTokenInvalid:
        return _render(
            request,
            "error.html",
            {"title": "链接无效", "message": "这个访问链接不存在或已被替换。"},
            status_code=404,
        )
    except StoreAccessExpired:
        # 用这条记录自己被批准的时长，而不是当前配置。改过
        # WEB_ACCESS_HOURS 之后，旧记录的实际时长和配置并不相同。
        granted = _granted_hours(_store(request).get_by_access_token(token))
        message = (
            f"这次访问已超过 {granted}，请重新申请。"
            if granted
            else "访问已到期，请重新申请。"
        )
        return _render(
            request,
            "error.html",
            {"title": "访问已到期", "message": message},
            status_code=401,
        )
    _set_access_session(request, record)
    return RedirectResponse("/", status_code=303)


@router.post("/logout")
def logout(request: Request, _csrf: None = Depends(require_csrf)):
    request.session.clear()
    if "application/json" in request.headers.get("accept", ""):
        return JSONResponse({"ok": True})
    return RedirectResponse("/login", status_code=303)

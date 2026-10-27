"""登录后的 JSON API。

身份只来自签名 Session 里的 email_hash；请求体里即使带了 email、
uid 或文件路径也不会被使用。所有 POST 都要求 CSRF 头。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from app.web.auth import (
    AccessContext,
    _as_utc,
    _now,
    require_access,
    require_csrf,
)
from app.web.runtime import BadMessage, ChatRuntime

router = APIRouter(prefix="/api")


def _runtime(request: Request) -> ChatRuntime:
    return request.app.state.runtime


def _remaining(request: Request, access: AccessContext) -> int:
    return access.remaining_seconds(_now(request))


@router.get("/me")
def me(
    request: Request,
    access: AccessContext = Depends(require_access),
):
    settings = request.app.state.settings
    sessions = _runtime(request).snapshot(access.email_hash)
    return {
        "ok": True,
        "display_name": access.display_name,
        "email": access.email_masked,
        "access_expires_at": _as_utc(access.access_expires_at).isoformat(
            timespec="seconds"
        ),
        "remaining_seconds": _remaining(request, access),
        "default_store": settings.default_store,
        "limits": {
            "max_input_chars": settings.max_input_chars,
            "max_history_messages": settings.max_history_messages,
            "access_hours": settings.access_hours,
        },
        "sessions": sessions,
    }


@router.get("/sessions")
def sessions(
    request: Request,
    access: AccessContext = Depends(require_access),
):
    return {
        "ok": True,
        "sessions": _runtime(request).snapshot(access.email_hash),
    }


@router.post("/chat")
async def chat(
    request: Request,
    access: AccessContext = Depends(require_access),
    _csrf: None = Depends(require_csrf),
):
    try:
        payload = await request.json()
    except (ValueError, UnicodeDecodeError):
        raise BadMessage("请求内容不是合法 JSON。") from None
    if not isinstance(payload, dict):
        raise BadMessage("请求内容格式不正确。")
    message = payload.get("message")
    if not isinstance(message, str):
        raise BadMessage("消息不能为空。")

    # ChatRuntime 内部使用同步锁并执行阻塞模型调用；放进线程池，
    # 否则单 worker 下会卡住事件循环，等待页轮询和静态资源也会停住。
    result = await run_in_threadpool(
        _runtime(request).chat,
        access.email_hash,
        message,
    )
    return {
        "ok": True,
        "answer": result.answer,
        "trace": result.trace,
        "choices": list(result.choices),
        "remaining_seconds": _remaining(request, access),
    }


@router.post("/reset")
def reset(
    request: Request,
    access: AccessContext = Depends(require_access),
    _csrf: None = Depends(require_csrf),
):
    _runtime(request).reset(access.email_hash)
    return JSONResponse({"ok": True})


@router.get("/healthz")
def api_healthz(request: Request):
    # 这个路径放在 /api 下只是方便前端探测；真正的健康检查在 /healthz。
    ok = request.app.state.usage.ping()
    return JSONResponse(
        {"ok": ok},
        status_code=200 if ok else 503,
    )

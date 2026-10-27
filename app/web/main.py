"""FastAPI 应用工厂：中间件、页面、API、健康检查。

单进程部署是刻意的：LangGraph 的 InMemorySaver 只存在于当前进程，
Excel 写入也没有跨进程锁。多 worker 会带来会话记忆丢失和并发写冲突，
所以 Docker 与文档都固定为 1 个 Uvicorn worker。
"""

from __future__ import annotations

import logging
import mimetypes
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.sessions import SessionMiddleware

from app.store.engine import (
    create_database_engine,
    dispose_engine,
    ensure_schema,
    probe_database,
)
from app.store.workspace import DatabaseWorkspaceManager
from app.tools.dm_control import DMControlError
from app.tools.intake import IntakeError
from app.tools.session_intake import SessionIntakeError
from app.tools.stages import StageTemplateError
from app.web import api, app_api, auth, dm, imports, operations_routes
from app.web.access import AccessStore
from app.web.access_sql import DatabaseAccessStore
from app.web.auth import (
    AccessContext,
    AuthError,
    CsrfError,
    _as_utc,
    _now,
    require_access,
)
from app.web.config import WebSettings
from app.web.mailer import EmailSender, build_email_sender
from app.web.models import DeepSeekModelProvider, ModelProvider
from app.web.operations import OperationsError, OperationsService
from app.web.runtime import ChatError, ChatRuntime
from app.web.usage import UsageStore
from app.web.usage_sql import DatabaseUsageStore
from app.web.workspace import WorkspaceManager

logger = logging.getLogger("app.web.main")

# Windows 的 mimetypes 数据库通常不认识 .webmanifest；显式注册，
# 否则“添加到主屏幕”在部分浏览器里会因 MIME 不符被拒绝。
mimetypes.add_type("application/manifest+json", ".webmanifest")
UTC = timezone.utc
WEB_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = WEB_DIR / "templates"
STATIC_DIR = WEB_DIR / "static"

CSP = (
    "default-src 'self'; "
    "base-uri 'self'; "
    "script-src 'self'; "
    "style-src 'self'; "
    "img-src 'self' data:; "
    "connect-src 'self'; "
    "form-action 'self'; "
    "frame-ancestors 'none'"
)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, *, production: bool = False) -> None:
        super().__init__(app)
        self.production = production

    async def dispatch(self, request: Request, call_next):
        response: Response = await call_next(request)
        response.headers.setdefault("Content-Security-Policy", CSP)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault(
            "Permissions-Policy",
            "geolocation=(), microphone=(), camera=(), payment=()",
        )
        response.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
        if self.production:
            response.headers.setdefault(
                "Strict-Transport-Security",
                "max-age=31536000; includeSubDomains",
            )
        if not request.url.path.startswith("/static/"):
            response.headers.setdefault("Cache-Control", "no-store")
        return response


class PayloadTooLarge(Exception):
    pass


class BodySizeLimitMiddleware:
    """先看 Content-Length，再在 receive 里累计实际字节数。

    仅信任 Content-Length 不够：分块请求没有这个头。超过上限时
    在应用读取请求体阶段中断，返回统一的 413。
    """

    def __init__(
        self,
        app,
        *,
        max_bytes: int,
        large_paths: tuple[str, ...] = (),
        large_max_bytes: int | None = None,
    ) -> None:
        self.app = app
        self.max_bytes = max_bytes
        self.large_paths = large_paths
        self.large_max_bytes = large_max_bytes

    def _limit_for(self, path: str) -> int:
        if self.large_max_bytes is not None and self.large_paths:
            if any(path.startswith(prefix) for prefix in self.large_paths):
                return max(self.max_bytes, self.large_max_bytes)
        return self.max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        limit = self._limit_for(str(scope.get("path") or ""))
        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        raw_length = headers.get(b"content-length")
        if raw_length:
            try:
                length = int(raw_length)
            except ValueError:
                length = -1
            if length < 0 or length > limit:
                response = _payload_too_large_response()
                await response(scope, receive, send)
                return

        received = 0
        response_started = False

        async def limited_receive():
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    raise PayloadTooLarge()
            return message

        async def tracked_send(message):
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, tracked_send)
        except PayloadTooLarge:
            if not response_started:
                response = _payload_too_large_response()
                await response(scope, receive, send)


def _payload_too_large_response() -> JSONResponse:
    return JSONResponse(
        {"ok": False, "code": "payload_too_large", "error": "请求内容太大。"},
        status_code=413,
        headers={"Cache-Control": "no-store"},
    )


def _wants_json(request: Request) -> bool:
    return request.url.path.startswith("/api/") or "application/json" in request.headers.get(
        "accept", ""
    )


def _remaining_text(seconds: int) -> str:
    seconds = max(0, int(seconds))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def create_app(
    *,
    settings: WebSettings | None = None,
    model_provider: ModelProvider | None = None,
    mailer: EmailSender | None = None,
    now_fn: Callable[[], datetime] | None = None,
) -> FastAPI:
    config = settings or WebSettings.from_env()
    config.ensure_directories()
    # 线上排障只能看平台日志，INFO 级别的业务审计和完整 traceback 必须留下。
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    clock = now_fn or (lambda: datetime.now(UTC))

    engine = None
    if config.uses_database:
        engine = create_database_engine(
            database_url=config.database_url,
            sqlite_path=None if config.database_url else config.database_sqlite_path,
            pool_size=config.database_pool_size,
            max_overflow=config.database_max_overflow,
            pool_timeout=config.database_pool_timeout,
        )
        try:
            ensure_schema(engine)
        except Exception as exc:  # noqa: BLE001 - 启动失败时由 /healthz 反映
            # 不打印连接串或密码；traceback 里 SQLAlchemy 会自动隐藏密码。
            # 进程仍启动，健康检查返回 503，而不是静默回退到 Excel。
            logger.exception("database schema init failed: %s", type(exc).__name__)
        access_store = DatabaseAccessStore(engine, now_fn=clock)
        usage = DatabaseUsageStore(
            engine,
            daily_limit=config.max_turns_global_per_day,
            now_fn=clock,
        )
        workspace = DatabaseWorkspaceManager(
            engine=engine,
            default_store=config.default_store,
            now_fn=clock,
        )
    else:
        access_store = AccessStore(config.state_db, now_fn=clock)
        usage = UsageStore(
            config.state_db,
            daily_limit=config.max_turns_global_per_day,
            now_fn=clock,
        )
        workspace = WorkspaceManager(
            data_root=config.data_root,
            default_store=config.default_store,
            now_fn=clock,
        )
    models = model_provider or DeepSeekModelProvider(
        max_tokens=config.model_max_tokens,
        timeout=config.model_timeout_seconds,
    )
    # 公共剧本库（全局只读）：数据库模式走全局表，Excel 模式走 data 根目录共享文件。
    from app.store.public_sql import build_public_library

    public_library = build_public_library(
        uses_database=config.uses_database,
        engine=engine,
        path=None if config.uses_database else config.data_root.parent / "public_scripts.xlsx",
    )
    # web_search 日配额计数器：与业务数据同源（SQL 表或 data 根目录 sidecar JSON）。
    from app.integrations.web_search import build_search_usage

    search_usage = build_search_usage(
        uses_database=config.uses_database,
        engine=engine,
        data_dir=config.data_root.parent,
    )
    sender = mailer or build_email_sender(config)
    runtime = ChatRuntime(
        settings=config,
        model_provider=models,
        workspace=workspace,
        usage=usage,
        now_fn=clock,
        public_library=public_library,
        search_usage=search_usage,
    )
    operations = OperationsService(
        settings=config,
        workspace=workspace,
        now_fn=clock,
        public_library=public_library,
    )
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if config.email_backend == "console":
            logger.warning(
                "EMAIL_BACKEND=console：审批邮件不会真正发送，"
                "审批链接只打印在当前终端。"
            )
        yield
        try:
            access_store.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            usage.close()
        except Exception:  # noqa: BLE001
            pass
        dispose_engine(engine)

    app = FastAPI(
        title="剧本杀拼场运营助手",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.settings = config
    app.state.access_store = access_store
    app.state.usage = usage
    app.state.workspace = workspace
    app.state.mailer = sender
    app.state.model_provider = models
    app.state.runtime = runtime
    app.state.operations = operations
    app.state.now_fn = clock
    app.state.templates = templates
    app.state.engine = engine
    app.state.storage_backend = config.storage_backend

    # 添加顺序决定嵌套顺序：最后添加的 SessionMiddleware 在最外层，
    # 这样安全响应头也能覆盖请求体限制中间件生成的 413。
    app.add_middleware(
        BodySizeLimitMiddleware,
        max_bytes=config.max_body_bytes,
        large_paths=(
            "/api/imports/",
            "/api/resources/import",
            "/api/app/imports/",
        ),
        large_max_bytes=config.max_import_bytes,
    )
    app.add_middleware(SecurityHeadersMiddleware, production=config.is_production)
    app.add_middleware(
        SessionMiddleware,
        secret_key=config.session_secret,
        session_cookie=config.session_cookie,
        max_age=config.session_seconds,
        same_site="lax",
        https_only=config.cookie_secure,
    )

    def render_error(
        request: Request,
        title: str,
        message: str,
        status_code: int,
    ) -> HTMLResponse:
        return templates.TemplateResponse(
            request=request,
            name="error.html",
            context={
                "request": request,
                "product_name": "剧本杀拼场运营助手",
                "csrf": request.session.get("csrf", "") if "session" in request.scope else "",
                "title": title,
                "message": message,
            },
            status_code=status_code,
            headers={"Cache-Control": "no-store"},
        )

    @app.exception_handler(AuthError)
    async def handle_auth_error(request: Request, exc: AuthError):
        if _wants_json(request):
            return JSONResponse(
                {"ok": False, "code": exc.code, "error": exc.message},
                status_code=exc.status_code,
                headers={"Cache-Control": "no-store"},
            )
        return RedirectResponse(exc.redirect_to, status_code=303)

    @app.exception_handler(CsrfError)
    async def handle_csrf_error(request: Request, exc: CsrfError):
        if _wants_json(request):
            return JSONResponse(
                {"ok": False, "code": exc.code, "error": str(exc)},
                status_code=exc.status_code,
            )
        return render_error(request, "请求校验失败", str(exc), exc.status_code)

    @app.exception_handler(ChatError)
    async def handle_chat_error(request: Request, exc: ChatError):
        headers = {"Cache-Control": "no-store"}
        if exc.retry_after_seconds is not None:
            headers["Retry-After"] = str(max(1, int(exc.retry_after_seconds)))
        return JSONResponse(
            {"ok": False, "code": exc.code, "error": exc.message},
            status_code=exc.status_code,
            headers=headers,
        )

    @app.exception_handler(OperationsError)
    async def handle_operations_error(request: Request, exc: OperationsError):
        return JSONResponse(
            {"ok": False, "code": exc.code, "error": exc.message},
            status_code=exc.status_code,
            headers={"Cache-Control": "no-store"},
        )

    async def handle_business_error(request: Request, exc: Exception):
        code = getattr(exc, "code", "bad_request")
        message = getattr(exc, "message", "请求无法完成。")
        status = 401 if code in {"token_invalid", "code_invalid", "token_expired"} else 400
        return JSONResponse(
            {"ok": False, "code": code, "error": message},
            status_code=status,
            headers={"Cache-Control": "no-store"},
        )

    @app.exception_handler(SessionIntakeError)
    @app.exception_handler(StageTemplateError)
    @app.exception_handler(IntakeError)
    @app.exception_handler(DMControlError)
    async def handle_tool_error(request: Request, exc: Exception):
        return await handle_business_error(request, exc)

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(request: Request, exc: RequestValidationError):
        if _wants_json(request):
            return JSONResponse(
                {"ok": False, "code": "bad_request", "error": "请求参数不合法。"},
                status_code=400,
            )
        return render_error(request, "请求不合法", "请求参数不合法。", 400)

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_error(request: Request, exc: StarletteHTTPException):
        if _wants_json(request):
            return JSONResponse(
                {"ok": False, "code": "http_error", "error": "请求无法完成。"},
                status_code=exc.status_code,
            )
        if exc.status_code == 404:
            return render_error(request, "页面不存在", "你访问的页面不存在。", 404)
        return render_error(request, "请求无法完成", "请稍后重试。", exc.status_code)

    @app.exception_handler(Exception)
    async def handle_unexpected(request: Request, exc: Exception):
        # /approve/<token> 的 token 在路径里；日志中只保留路由形状。
        path = request.url.path
        if path.startswith("/approve/"):
            path = "/approve/<redacted>"
        logger.exception(
            "unhandled error path=%s type=%s",
            path,
            type(exc).__name__,
        )
        if _wants_json(request):
            return JSONResponse(
                {"ok": False, "code": "internal_error", "error": "服务暂时不可用。"},
                status_code=500,
            )
        try:
            return render_error(request, "服务暂时不可用", "请稍后重试。", 500)
        except Exception:  # noqa: BLE001 - 最后兜底不能再抛
            return Response("服务暂时不可用。", status_code=500)

    app.include_router(auth.router)
    app.include_router(api.router)
    app.include_router(operations_routes.router)
    app.include_router(imports.router)
    app.include_router(dm.router)
    app.include_router(app_api.router)

    @app.get("/", response_class=HTMLResponse)
    def chat_page(
        request: Request,
        access: AccessContext = Depends(require_access),
    ):
        sessions: list[dict[str, Any]] = []
        try:
            sessions = runtime.snapshot(access.email_hash)
        except ChatError:
            sessions = []
        remaining = access.remaining_seconds(_now(request))
        return templates.TemplateResponse(
            request=request,
            name="chat.html",
            context={
                "request": request,
                "product_name": "剧本杀拼场运营助手",
                "csrf": request.session.get("csrf", ""),
                "access": access,
                "email_masked": access.email_masked,
                "active_page": "chat",
                "remaining_text": _remaining_text(remaining),
                "remaining_seconds": remaining,
                "access_expires_at": _as_utc(access.access_expires_at).isoformat(
                    timespec="seconds"
                ),
                "sessions": sessions,
                "max_input_chars": config.max_input_chars,
                "default_store": config.default_store,
            },
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/livez")
    def livez(request: Request):
        """进程存活检查：不碰数据库。

        容器的 HEALTHCHECK 用这个。如果拿 /healthz 当探针，每次探测都会真的
        连一次库，Neon 免费版因此永远不会挂起，计算时长几天就会被烧光。
        """
        return JSONResponse(
            {"ok": True},
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/healthz")
    def healthz(request: Request):
        # 数据库模式下只用一次取连接、一条 SQL 就拿到「连得上」和「表存在」。
        # 原先走四次独立 connect()，每次还要付 pre-ping 和 ROLLBACK，跨境部署
        # 会放大成十几倍往返（实测 0.82s，合并后约 0.1s）。
        if engine is not None:
            db_ok, schema_ok = probe_database(engine)
        else:
            # Excel 兼容模式没有引擎，只能问各自的存储。
            db_ok = usage.ping() and access_store.ping()
            schema_ok = True
        model_ok = bool(os.environ.get("DEEPSEEK_API_KEY", "").strip())
        # 模型 key 只约束生产：离线测试与本地演示不需要它。
        healthy = db_ok and schema_ok and (model_ok or not config.is_production)
        return JSONResponse(
            {
                "ok": healthy,
                "status": "ok" if healthy else "degraded",
                "db": db_ok,
                "schema": schema_ok,
                "model": model_ok,
            },
            status_code=200 if healthy else 503,
            headers={"Cache-Control": "no-store"},
        )

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    return app


def main() -> None:
    import uvicorn

    uvicorn.run(
        "app.web.main:create_app",
        factory=True,
        host="0.0.0.0",
        port=8000,
        workers=1,
        proxy_headers=True,
        # 审批链接和备用链接的 token 在 URL 中，关闭访问日志避免
        # Uvicorn 把完整路径写进控制台或平台日志。
        access_log=False,
    )


if __name__ == "__main__":
    main()

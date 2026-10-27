"""网页版配置：从环境变量读取并做启动期校验。

原则：
  - 生产环境缺少会话密钥、开发者邮箱、Resend key、HTTPS 公网地址时，
    宁可启动失败，也不要带着不安全的默认值上线。
  - 所有数值上限都有下限/上限，避免一个手误把限制关掉。
  - 本模块只解析配置，不创建数据库、不发邮件。
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping
from urllib.parse import urlsplit

from app.config import PROJECT_ROOT
from app.web.security import InvalidEmail, normalize_email

VALID_ENVS = frozenset({"development", "test", "production"})
VALID_EMAIL_BACKENDS = frozenset({"console", "resend"})
VALID_STORAGE_BACKENDS = frozenset({"excel", "database"})


class WebConfigError(ValueError):
    """网页版配置错误。消息会直接打印给部署者。"""


def _text(environ: Mapping[str, str], name: str) -> str:
    value = environ.get(name)
    if value is None:
        return ""
    return str(value).strip()


def _int(
    environ: Mapping[str, str],
    name: str,
    default: int,
    *,
    minimum: int,
    maximum: int,
) -> int:
    raw = _text(environ, name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise WebConfigError(f"{name} 必须是整数，当前为 {raw!r}") from exc
    if not minimum <= value <= maximum:
        raise WebConfigError(f"{name} 必须在 {minimum}..{maximum} 之间，当前为 {value}")
    return value


def _float(
    environ: Mapping[str, str],
    name: str,
    default: float,
    *,
    minimum: float,
    maximum: float,
) -> float:
    raw = _text(environ, name)
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise WebConfigError(f"{name} 必须是数字，当前为 {raw!r}") from exc
    if not math.isfinite(value) or not minimum <= value <= maximum:
        raise WebConfigError(f"{name} 必须在 {minimum}..{maximum} 之间，当前为 {value}")
    return value


def _path(environ: Mapping[str, str], name: str, default: Path) -> Path:
    raw = _text(environ, name)
    path = Path(raw).expanduser() if raw else default
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path.resolve()


def _base_url(environ: Mapping[str, str], *, production: bool) -> str:
    raw = _text(environ, "PUBLIC_BASE_URL")
    if not raw:
        if production:
            raise WebConfigError(
                "生产环境必须设置 PUBLIC_BASE_URL，例如 https://your-app.onrender.com"
            )
        raw = "http://127.0.0.1:8000"

    try:
        parsed = urlsplit(raw)
        # 访问 .port 会把非法端口提前变成 ValueError。
        parsed.port
    except ValueError as exc:
        raise WebConfigError("PUBLIC_BASE_URL 不是合法 URL") from exc

    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise WebConfigError("PUBLIC_BASE_URL 必须以 http:// 或 https:// 开头")
    if parsed.username or parsed.password:
        raise WebConfigError("PUBLIC_BASE_URL 不能包含用户名或密码")
    if parsed.query or parsed.fragment:
        raise WebConfigError("PUBLIC_BASE_URL 不能带查询参数或锚点")
    if production and parsed.scheme != "https":
        raise WebConfigError("生产环境 PUBLIC_BASE_URL 必须是 https://")

    base = f"{parsed.scheme}://{parsed.netloc}{parsed.path.rstrip('/')}"
    return base


@dataclass(frozen=True, slots=True)
class WebSettings:
    session_secret: str = field(repr=False)
    owner_email: str
    env: str = "development"
    email_backend: str = "console"
    resend_api_key: str | None = field(default=None, repr=False)
    email_from: str = "onboarding@resend.dev"
    public_base_url: str = "http://127.0.0.1:8000"

    data_root: Path = PROJECT_ROOT / "data" / "web_users"
    state_db: Path = PROJECT_ROOT / "data" / "web_state.sqlite3"
    database_sqlite_path: Path = PROJECT_ROOT / "data" / "web.sqlite3"
    default_store: str = "迷雾剧场"

    storage_backend: str = "excel"
    database_url: str | None = field(default=None, repr=False)
    database_pool_size: int = 3
    database_max_overflow: int = 2
    database_pool_timeout: float = 10.0

    access_hours: float = 8.0
    approval_ttl_hours: float = 24.0
    session_hours: float = 24.0

    max_input_chars: int = 2000
    max_body_bytes: int = 32768
    max_import_bytes: int = 2 * 1024 * 1024
    max_history_messages: int = 100
    max_history_chars: int = 50000
    max_concurrent_turns: int = 3
    max_turns_global_per_day: int = 300
    model_max_tokens: int = 1500
    model_timeout_seconds: float = 60.0

    request_cooldown_minutes: int = 10
    requests_per_ip_per_hour: int = 5
    requests_global_per_day: int = 50

    dm_code_ttl_minutes: int = 30
    dm_token_hours: int = 12
    hardware_skill_url: str | None = None
    hardware_skill_token: str | None = field(default=None, repr=False)
    hardware_timeout_seconds: float = 2.0

    session_cookie: str = "jbs_web_session"

    @property
    def is_production(self) -> bool:
        return self.env == "production"

    @property
    def uses_database(self) -> bool:
        return self.storage_backend == "database"

    @property
    def cookie_secure(self) -> bool:
        return self.is_production

    @property
    def access_seconds(self) -> int:
        return int(self.access_hours * 3600)

    @property
    def approval_ttl_seconds(self) -> int:
        return int(self.approval_ttl_hours * 3600)

    @property
    def session_seconds(self) -> int:
        return int(self.session_hours * 3600)

    def ensure_directories(self) -> None:
        """创建本地数据目录；不创建数据库文件本身。"""
        self.data_root.mkdir(parents=True, exist_ok=True)
        self.state_db.parent.mkdir(parents=True, exist_ok=True)
        self.database_sqlite_path.parent.mkdir(parents=True, exist_ok=True)

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "WebSettings":
        env_map = os.environ if environ is None else environ

        env_name = _text(env_map, "WEB_ENV").lower() or "development"
        env_name = {"dev": "development", "prod": "production"}.get(env_name, env_name)
        if env_name not in VALID_ENVS:
            raise WebConfigError(
                f"WEB_ENV 只能是 {sorted(VALID_ENVS)}，当前为 {env_name!r}"
            )
        production = env_name == "production"

        session_secret = _text(env_map, "WEB_SESSION_SECRET")
        if not session_secret:
            raise WebConfigError(
                "缺少 WEB_SESSION_SECRET。生成方式：\n"
                '  python -c "import secrets; print(secrets.token_urlsafe(48))"'
            )
        if len(session_secret) < 32:
            raise WebConfigError("WEB_SESSION_SECRET 至少需要 32 个字符")

        owner_raw = _text(env_map, "OWNER_EMAIL")
        if not owner_raw:
            raise WebConfigError("缺少 OWNER_EMAIL（审批邮件的收件人）")
        try:
            owner_email = normalize_email(owner_raw)
        except InvalidEmail as exc:
            raise WebConfigError(f"OWNER_EMAIL 不合法：{exc}") from exc

        email_backend = _text(env_map, "EMAIL_BACKEND").lower() or "console"
        if email_backend not in VALID_EMAIL_BACKENDS:
            raise WebConfigError(
                f"EMAIL_BACKEND 只能是 {sorted(VALID_EMAIL_BACKENDS)}，"
                f"当前为 {email_backend!r}"
            )
        if production and email_backend == "console":
            raise WebConfigError(
                "生产环境不能使用 EMAIL_BACKEND=console：审批链接会写进服务端日志。"
                "请配置 EMAIL_BACKEND=resend。"
            )

        resend_api_key = _text(env_map, "RESEND_API_KEY") or None
        if email_backend == "resend" and not resend_api_key:
            raise WebConfigError("EMAIL_BACKEND=resend 时必须设置 RESEND_API_KEY")

        email_from = _text(env_map, "EMAIL_FROM") or "onboarding@resend.dev"
        public_base_url = _base_url(env_map, production=production)

        hardware_skill_url = _text(env_map, "HARDWARE_SKILL_URL") or None
        if hardware_skill_url:
            try:
                parsed = urlsplit(hardware_skill_url)
                parsed.port
            except ValueError:
                raise WebConfigError("HARDWARE_SKILL_URL 不是合法 URL") from None
            if parsed.scheme not in ("http", "https") or not parsed.hostname:
                raise WebConfigError("HARDWARE_SKILL_URL 必须以 http:// 或 https:// 开头")
            if parsed.username or parsed.password:
                raise WebConfigError("HARDWARE_SKILL_URL 不能包含用户名或密码")
            if production and parsed.scheme != "https":
                raise WebConfigError("生产环境 HARDWARE_SKILL_URL 必须是 https://")
            if parsed.scheme == "http" and parsed.hostname not in {
                "127.0.0.1",
                "localhost",
                "::1",
            }:
                raise WebConfigError(
                    "HARDWARE_SKILL_URL 使用 http:// 时只允许本机地址；"
                    "远程硬件服务必须使用 https://"
                )
        hardware_skill_token = _text(env_map, "HARDWARE_SKILL_TOKEN") or None

        storage_backend = _text(env_map, "WEB_STORAGE_BACKEND").lower()
        if not storage_backend:
            storage_backend = "database" if production else "excel"
        if storage_backend not in VALID_STORAGE_BACKENDS:
            raise WebConfigError(
                f"WEB_STORAGE_BACKEND 只能是 {sorted(VALID_STORAGE_BACKENDS)}，"
                f"当前为 {storage_backend!r}"
            )

        database_url = _text(env_map, "DATABASE_URL") or None
        if storage_backend == "database":
            if production and not database_url:
                raise WebConfigError(
                    "生产环境使用数据库存储时必须设置 DATABASE_URL"
                    "（Neon/PostgreSQL 连接串）。"
                )
            if database_url:
                from app.store.engine import DatabaseConfigError, normalize_database_url

                try:
                    normalize_database_url(database_url)
                except DatabaseConfigError:
                    raise WebConfigError("DATABASE_URL 格式不合法") from None

        default_data_root = PROJECT_ROOT / "data" / "web_users"
        default_state_db = PROJECT_ROOT / "data" / "web_state.sqlite3"
        default_database_sqlite = PROJECT_ROOT / "data" / "web.sqlite3"

        settings = cls(
            env=env_name,
            session_secret=session_secret,
            owner_email=owner_email,
            email_backend=email_backend,
            resend_api_key=resend_api_key,
            email_from=email_from,
            public_base_url=public_base_url,
            data_root=_path(env_map, "WEB_DATA_ROOT", default_data_root),
            state_db=_path(env_map, "WEB_STATE_DB", default_state_db),
            database_sqlite_path=_path(
                env_map, "WEB_DATABASE_SQLITE_PATH", default_database_sqlite
            ),
            default_store=_text(env_map, "WEB_DEFAULT_STORE") or "迷雾剧场",
            storage_backend=storage_backend,
            database_url=database_url,
            database_pool_size=_int(
                env_map, "DATABASE_POOL_SIZE", 3, minimum=1, maximum=20
            ),
            database_max_overflow=_int(
                env_map, "DATABASE_MAX_OVERFLOW", 2, minimum=0, maximum=20
            ),
            database_pool_timeout=_float(
                env_map, "DATABASE_POOL_TIMEOUT", 10.0, minimum=1.0, maximum=60.0
            ),
            access_hours=_float(
                env_map, "WEB_ACCESS_HOURS", 8.0, minimum=0.1, maximum=72.0
            ),
            approval_ttl_hours=_float(
                env_map, "WEB_APPROVAL_TTL_HOURS", 24.0, minimum=1.0, maximum=168.0
            ),
            session_hours=_float(
                env_map, "WEB_SESSION_HOURS", 24.0, minimum=1.0, maximum=72.0
            ),
            max_input_chars=_int(
                env_map, "WEB_MAX_INPUT_CHARS", 2000, minimum=1, maximum=10000
            ),
            max_body_bytes=_int(
                env_map,
                "WEB_MAX_BODY_BYTES",
                32768,
                minimum=1024,
                maximum=1_048_576,
            ),
            max_import_bytes=_int(
                env_map,
                "WEB_MAX_IMPORT_BYTES",
                2 * 1024 * 1024,
                minimum=32 * 1024,
                maximum=10 * 1024 * 1024,
            ),
            max_history_messages=_int(
                env_map, "WEB_MAX_HISTORY_MESSAGES", 100, minimum=2, maximum=1000
            ),
            max_history_chars=_int(
                env_map,
                "WEB_MAX_HISTORY_CHARS",
                50000,
                minimum=1000,
                maximum=1_000_000,
            ),
            max_concurrent_turns=_int(
                env_map, "WEB_MAX_CONCURRENT_TURNS", 3, minimum=1, maximum=20
            ),
            max_turns_global_per_day=_int(
                env_map,
                "WEB_MAX_TURNS_GLOBAL_PER_DAY",
                300,
                minimum=1,
                maximum=100_000,
            ),
            model_max_tokens=_int(
                env_map, "WEB_MODEL_MAX_TOKENS", 1500, minimum=64, maximum=8192
            ),
            model_timeout_seconds=_float(
                env_map,
                "WEB_MODEL_TIMEOUT_SECONDS",
                60.0,
                minimum=5.0,
                maximum=300.0,
            ),
            request_cooldown_minutes=_int(
                env_map,
                "WEB_REQUEST_COOLDOWN_MINUTES",
                10,
                minimum=0,
                maximum=1440,
            ),
            requests_per_ip_per_hour=_int(
                env_map,
                "WEB_REQUESTS_PER_IP_PER_HOUR",
                5,
                minimum=0,
                maximum=1000,
            ),
            requests_global_per_day=_int(
                env_map,
                "WEB_REQUESTS_GLOBAL_PER_DAY",
                50,
                minimum=0,
                maximum=10000,
            ),
            dm_code_ttl_minutes=_int(
                env_map, "DM_CODE_TTL_MINUTES", 30, minimum=5, maximum=120
            ),
            dm_token_hours=_int(
                env_map, "DM_TOKEN_HOURS", 12, minimum=1, maximum=24
            ),
            hardware_skill_url=hardware_skill_url,
            hardware_skill_token=hardware_skill_token,
            hardware_timeout_seconds=_float(
                env_map,
                "HARDWARE_TIMEOUT_SECONDS",
                2.0,
                minimum=0.5,
                maximum=5.0,
            ),
        )
        return settings

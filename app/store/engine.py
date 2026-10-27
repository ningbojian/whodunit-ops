"""数据库引擎创建、连接池参数与幂等建表。"""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import SQLAlchemyError

from app.store.schema import metadata

_DATABASE_URL_RE = re.compile(
    r"(?i)\b(?:postgres(?:ql)?(?:\+psycopg)?|sqlite(?:\+pysqlite)?)://[^\s'\"<>]+"
)


class DatabaseConfigError(ValueError):
    """数据库连接配置不合法。消息里不包含连接串或密码。"""


def redact_database_url(value: Any) -> str:
    """错误信息落盘或打印前抹掉连接串，避免密码进入终端/日志。"""
    if value is None:
        return ""
    return _DATABASE_URL_RE.sub("<database-url>", str(value))


def normalize_database_url(raw: str) -> URL:
    """把常见连接串转成 SQLAlchemy URL。

    接受 Neon 控制台复制的 postgresql://，统一改用 psycopg 3 驱动。
    """
    value = str(raw or "").strip()
    if not value:
        raise DatabaseConfigError("数据库连接串为空")
    scheme, _, rest = value.partition("://")
    if scheme in {"postgres", "postgresql"}:
        value = "postgresql+psycopg://" + rest
    try:
        url = make_url(value)
    except Exception:  # noqa: BLE001 - SQLAlchemy 的异常文本可能回显敏感信息
        raise DatabaseConfigError("DATABASE_URL 格式不合法") from None
    if url.drivername not in {"postgresql+psycopg", "sqlite+pysqlite"}:
        raise DatabaseConfigError("DATABASE_URL 只支持 PostgreSQL 或 SQLite")
    return url


def sqlite_url(path) -> URL:
    """Windows 绝对路径也安全地转成 SQLite URL。"""
    return URL.create("sqlite+pysqlite", database=str(path))


def create_database_engine(
    *,
    database_url: str | None = None,
    sqlite_path=None,
    pool_size: int = 3,
    max_overflow: int = 2,
    pool_timeout: float = 10.0,
    echo: bool = False,
) -> Engine:
    """按环境创建引擎；SQLite 用于离线测试/本地 Web，Postgres 用于生产。"""
    if database_url:
        url = normalize_database_url(database_url)
    elif sqlite_path is not None:
        url = sqlite_url(sqlite_path)
    else:
        raise DatabaseConfigError("缺少数据库连接配置")

    kwargs: dict[str, Any] = {"echo": echo, "future": True}
    if url.get_backend_name() == "sqlite":
        kwargs.update(
            connect_args={"check_same_thread": False, "timeout": 10},
        )
    else:
        kwargs.update(
            pool_size=max(1, int(pool_size)),
            max_overflow=max(0, int(max_overflow)),
            pool_timeout=float(pool_timeout),
            pool_pre_ping=True,
            # 300 秒会把「还活着并且正在用」的连接也主动作废，下次再付一次
            # 跨洋握手（DNS 120ms + TCP 55ms + TLS）。Neon 挂起导致的坏连接
            # 由 pool_pre_ping 兜住，所以这里可以放宽到 30 分钟。
            pool_recycle=1800,
            # Neon 的 DATABASE_URL 是 PgBouncer 事务模式（-pooler）。
            # psycopg 3 默认在重复执行 5 次后使用服务端 prepared statement，
            # 事务模式会在下一次事务换后端时报 "prepared statement already exists"。
            # 关闭它，连接池本身仍由 SQLAlchemy 管理。
            connect_args={"connect_timeout": 10, "prepare_threshold": None},
        )

    engine = create_engine(url, **kwargs)
    if url.get_backend_name() == "sqlite":
        _install_sqlite_pragmas(engine)
    return engine


def _install_sqlite_pragmas(engine: Engine) -> None:
    @event.listens_for(engine, "connect")
    def _set_sqlite_pragma(dbapi_connection, _connection_record):  # pragma: no cover
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA busy_timeout=5000")
        finally:
            cursor.close()


def ensure_schema(engine: Engine) -> None:
    """幂等建表；不负责修改已经存在的表结构。"""
    metadata.create_all(engine)


def ping_engine(engine: Engine | None) -> bool:
    if engine is None:
        return False
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except SQLAlchemyError:
        return False
    except Exception:  # noqa: BLE001 - 健康检查不能向上抛
        return False


_REQUIRED_TABLES = ("access_requests", "tenants")


def schema_ready(engine: Engine | None) -> bool:
    """健康检查用：两张核心表都存在才算 schema 就绪。

    只判断表是否存在，不比对列。`create_all` 不会修改已存在的表，
    列漂移要靠人工核对（见 docs/deployment.md）。
    """
    if engine is None:
        return False
    try:
        with engine.connect() as conn:
            if engine.dialect.name == "postgresql":
                ready = conn.execute(
                    text(
                        "SELECT to_regclass('public.tenants') IS NOT NULL "
                        "AND to_regclass('public.access_requests') IS NOT NULL"
                    )
                ).scalar()
                return bool(ready)
            rows = conn.execute(
                text(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name IN ('tenants', 'access_requests')"
                )
            ).fetchall()
            return len(rows) == len(_REQUIRED_TABLES)
    except SQLAlchemyError:
        return False
    except Exception:  # noqa: BLE001 - 健康检查不能向上抛
        return False


def probe_database(engine: Engine | None) -> tuple[bool, bool]:
    """一次取连接、一条查询，同时判断连通性与核心表是否就绪。

    健康检查原先在同一个引擎上做了四次独立的 ``connect()``（usage、access、
    ping、schema），每次 checkout 还要多付 ``pool_pre_ping`` 和归还时的
    ROLLBACK。跨境部署下每次往返都是 55ms 量级，四次探测会放大成十几倍。
    这条 SQL 本身失败就说明连不上，成功就说明连得上——不需要再单独 ping。

    返回 ``(db_ok, schema_ok)``；连不上时两者均为 False。
    """
    if engine is None:
        return False, False
    try:
        # AUTOCOMMIT：只读探测不进事务，连接归还池时就不会再发一次 ROLLBACK。
        with engine.connect().execution_options(
            isolation_level="AUTOCOMMIT"
        ) as conn:
            if engine.dialect.name == "postgresql":
                ready = conn.execute(
                    text(
                        "SELECT to_regclass('public.tenants') IS NOT NULL "
                        "AND to_regclass('public.access_requests') IS NOT NULL"
                    )
                ).scalar()
                return True, bool(ready)
            rows = conn.execute(
                text(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name IN ('tenants', 'access_requests')"
                )
            ).fetchall()
            return True, len(rows) == len(_REQUIRED_TABLES)
    except SQLAlchemyError:
        return False, False
    except Exception:  # noqa: BLE001 - 健康检查不能向上抛
        return False, False


def dispose_engine(engine: Engine | None) -> None:
    if engine is None:
        return
    try:
        engine.dispose()
    except Exception:  # noqa: BLE001 - 关闭阶段不能影响退出
        pass


__all__ = [
    "DatabaseConfigError",
    "create_database_engine",
    "dispose_engine",
    "ensure_schema",
    "normalize_database_url",
    "ping_engine",
    "probe_database",
    "redact_database_url",
    "schema_ready",
    "sqlite_url",
]

"""访问申请的数据库存储（SQLite / Postgres 共用）。

与旧的 SQLite 专用 `AccessStore` 保持同一组公开方法，Web 层无需感知后端。
审批和拒绝使用条件 UPDATE（`WHERE status='pending'`），保证并发下只有
一个请求能成功；创建申请在 Postgres 上额外加事务级 advisory lock。
"""

from __future__ import annotations

import re
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Callable, Iterator

from sqlalchemy import Engine, delete, func, insert, select, text, update
from sqlalchemy.engine import RowMapping
from sqlalchemy.exc import SQLAlchemyError

from app.store.schema import access_requests
from app.web.access import (
    AccessExpired,
    AccessNotReady,
    AccessRequest,
    AccessTokenInvalid,
    AlreadyProcessed,
    ApprovalTokenExpired,
    ApprovalTokenInvalid,
    BrowserTokenInvalid,
    CreatedRequest,
    ApprovedRequest,
    RequestCooldown,
    RequestLimitExceeded,
    RequestNotFound,
    _as_utc,
    _cst_day_start,
    _retry_after,
)
from app.web.security import (
    constant_time_equal,
    email_hash as hash_email,
    hash_ip,
    hash_token,
    new_token,
    normalize_display_name,
    normalize_email,
)

UTC = timezone.utc
_ADVISORY_LOCK_KEY = 0x4A4253415050


class DatabaseAccessStore:
    """访问申请存储；所有时间以 UTC 写入，读取时补时区。"""

    def __init__(
        self,
        engine: Engine,
        *,
        now_fn: Callable[[], datetime] | None = None,
        ip_hash_secret: str,
    ) -> None:
        if not isinstance(ip_hash_secret, str) or not ip_hash_secret:
            raise ValueError("ip_hash_secret 不能为空")
        self.engine = engine
        self._now_fn = now_fn or (lambda: datetime.now(UTC))
        self._ip_hash_secret = ip_hash_secret
        self._lock = threading.RLock()
        self._postgres = engine.dialect.name == "postgresql"

    def __enter__(self) -> "DatabaseAccessStore":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def close(self) -> None:
        # 引擎由应用生命周期管理。
        return None

    def _now(self) -> datetime:
        return _as_utc(self._now_fn())

    @contextmanager
    def _write(self) -> Iterator:
        """写事务；SQLite 用进程锁，Postgres 再加事务级 advisory lock。"""
        with self._lock:
            with self.engine.connect() as conn:
                with conn.begin():
                    if self._postgres:
                        conn.execute(
                            text("SELECT pg_advisory_xact_lock(:key)"),
                            {"key": _ADVISORY_LOCK_KEY},
                        )
                    yield conn

    @staticmethod
    def _row(row: RowMapping | None) -> AccessRequest | None:
        if row is None:
            return None

        def moment(value):
            if value is None:
                return None
            if isinstance(value, datetime):
                return _as_utc(value)
            return _as_utc(datetime.fromisoformat(str(value)))

        return AccessRequest(
            id=row["id"],
            email=row["email"],
            email_hash=row["email_hash"],
            display_name=row["display_name"],
            status=row["status"],
            created_at=moment(row["created_at"]),
            updated_at=moment(row["updated_at"]),
            approval_expires_at=moment(row["approval_expires_at"]),
            approved_at=moment(row["approved_at"]),
            access_expires_at=moment(row["access_expires_at"]),
            approval_token_hash=row["approval_token_hash"],
            access_token_hash=row["access_token_hash"],
            browser_token_hash=row["browser_token_hash"],
            requester_ip_hash=row["requester_ip_hash"],
            email_error=row["email_error"],
        )

    def _fetch_one(self, conn, statement) -> AccessRequest | None:
        return self._row(conn.execute(statement.limit(1)).mappings().first())

    def _get_by(self, column: str, value: str) -> AccessRequest | None:
        if column not in {"id", "approval_token_hash", "access_token_hash"}:
            raise ValueError("非法查询列")
        field = access_requests.c[column]
        with self.engine.connect() as conn:
            return self._fetch_one(
                conn, select(access_requests).where(field == value)
            )

    def _get_by_conn(self, conn, column: str, value: str) -> AccessRequest | None:
        if column not in {"id", "approval_token_hash", "access_token_hash"}:
            raise ValueError("非法查询列")
        field = access_requests.c[column]
        return self._fetch_one(conn, select(access_requests).where(field == value))

    def _find_active(self, conn, email_hash_value: str, now: datetime):
        return self._fetch_one(
            conn,
            select(access_requests)
            .where(
                access_requests.c.email_hash == email_hash_value,
                access_requests.c.status == "approved",
                access_requests.c.access_expires_at.isnot(None),
                access_requests.c.access_expires_at > now,
            )
            .order_by(
                access_requests.c.created_at.desc(),
                access_requests.c.id.desc(),
            ),
        )

    def _find_pending(self, conn, email_hash_value: str, now: datetime):
        return self._fetch_one(
            conn,
            select(access_requests)
            .where(
                access_requests.c.email_hash == email_hash_value,
                access_requests.c.status == "pending",
                access_requests.c.approval_expires_at > now,
            )
            .order_by(
                access_requests.c.created_at.desc(),
                access_requests.c.id.desc(),
            ),
        )

    def _latest_for_email(self, conn, email_hash_value: str):
        return self._fetch_one(
            conn,
            select(access_requests)
            .where(access_requests.c.email_hash == email_hash_value)
            .order_by(
                access_requests.c.created_at.desc(),
                access_requests.c.id.desc(),
            ),
        )

    def create_request(
        self,
        email: str,
        display_name: str | None,
        *,
        browser_token: str,
        requester_ip: str | None = None,
        approval_ttl_hours: float = 24.0,
        cooldown_minutes: int = 10,
        ip_limit_per_hour: int = 5,
        global_limit_per_day: int = 50,
    ) -> CreatedRequest:
        normalized = normalize_email(email)
        if not isinstance(browser_token, str) or len(browser_token) < 16:
            raise ValueError("browser_token 熵过低")
        if approval_ttl_hours <= 0:
            raise ValueError("approval_ttl_hours 必须大于 0")

        display = normalize_display_name(display_name, fallback_email=normalized)
        email_hash_value = hash_email(normalized)
        browser_hash = hash_token(browser_token)
        ip_hash_value = (
            hash_ip(requester_ip.strip(), self._ip_hash_secret)
            if isinstance(requester_ip, str) and requester_ip.strip()
            else None
        )
        now = self._now()
        approval_expires = now + timedelta(hours=approval_ttl_hours)

        with self._write() as conn:
            # 与 AccessStore 保持一致：已有有效访问不再拦截新申请，否则用户
            # 换浏览器或清了 Cookie 之后只能等整个访问期结束。
            superseded = self._find_active(conn, email_hash_value, now)

            pending = self._find_pending(conn, email_hash_value, now)
            if pending is not None:
                raise RequestCooldown(pending)

            latest = self._latest_for_email(conn, email_hash_value)
            # 与 AccessStore 保持一致：发送失败不算一次有效申请，不设冷却。
            if (
                latest is not None
                and latest.status != "email_failed"
                and cooldown_minutes > 0
            ):
                cooldown = timedelta(minutes=cooldown_minutes)
                if now - latest.created_at < cooldown:
                    retry = _retry_after(latest.created_at + cooldown, now)
                    raise RequestCooldown(latest, retry)

            if ip_hash_value and ip_limit_per_hour > 0:
                hour_ago = now - timedelta(hours=1)
                count = conn.execute(
                    select(func.count())
                    .select_from(access_requests)
                    .where(
                        access_requests.c.requester_ip_hash == ip_hash_value,
                        access_requests.c.created_at > hour_ago,
                    )
                ).scalar_one()
                if int(count) >= ip_limit_per_hour:
                    oldest = conn.execute(
                        select(func.min(access_requests.c.created_at)).where(
                            access_requests.c.requester_ip_hash == ip_hash_value,
                            access_requests.c.created_at > hour_ago,
                        )
                    ).scalar_one()
                    target = _as_utc(oldest or now) + timedelta(hours=1)
                    raise RequestLimitExceeded("ip", _retry_after(target, now))

            if global_limit_per_day > 0:
                day_start = _cst_day_start(now)
                count = conn.execute(
                    select(func.count())
                    .select_from(access_requests)
                    .where(access_requests.c.created_at >= day_start)
                ).scalar_one()
                if int(count) >= global_limit_per_day:
                    target = day_start + timedelta(days=1)
                    raise RequestLimitExceeded(
                        "global", _retry_after(target, now)
                    )

            request_id = uuid.uuid4().hex
            approval_token = new_token(32)
            conn.execute(
                insert(access_requests).values(
                    id=request_id,
                    email=normalized,
                    email_hash=email_hash_value,
                    display_name=display,
                    status="pending",
                    created_at=now,
                    updated_at=now,
                    approval_expires_at=approval_expires,
                    approved_at=None,
                    access_expires_at=None,
                    approval_token_hash=hash_token(approval_token),
                    access_token_hash=None,
                    browser_token_hash=browser_hash,
                    requester_ip_hash=ip_hash_value,
                    email_error=None,
                )
            )
            created = self._get_by_conn(conn, "id", request_id)
            assert created is not None
            return CreatedRequest(
                request=created,
                approval_token=approval_token,
                browser_token=browser_token,
                replaces_active=superseded,
            )

    def get_request(self, request_id: str) -> AccessRequest | None:
        if not request_id:
            return None
        return self._get_by("id", request_id)

    def get_by_approval_token(self, token: str | None) -> AccessRequest | None:
        if not isinstance(token, str) or not token:
            return None
        return self._get_by("approval_token_hash", hash_token(token))

    def get_by_access_token(self, token: str | None) -> AccessRequest | None:
        if not isinstance(token, str) or not token:
            return None
        return self._get_by("access_token_hash", hash_token(token))

    def status_for_request(self, request_id: str) -> str | None:
        request = self.get_request(request_id)
        return request.effective_status(self._now()) if request else None

    def mark_email_failed(self, request_id: str, reason: str) -> bool:
        safe = re.sub(r"[^A-Za-z0-9_.:-]", "_", str(reason))[:120] or "unknown"
        with self._write() as conn:
            result = conn.execute(
                update(access_requests)
                .where(
                    access_requests.c.id == request_id,
                    access_requests.c.status == "pending",
                )
                .values(
                    status="email_failed",
                    updated_at=self._now(),
                    email_error=safe,
                )
            )
            return result.rowcount > 0

    def approve(
        self,
        approval_token: str | None,
        *,
        access_hours: float = 720.0,
    ) -> ApprovedRequest:
        if not isinstance(approval_token, str) or not approval_token:
            raise ApprovalTokenInvalid("审批链接无效")
        if access_hours <= 0:
            raise ValueError("access_hours 必须大于 0")

        now = self._now()
        expired = False
        processed: AccessRequest | None = None
        updated: AccessRequest | None = None
        access_token = new_token(32)

        with self._write() as conn:
            request = self._get_by_conn(
                conn, "approval_token_hash", hash_token(approval_token)
            )
            if request is None:
                raise ApprovalTokenInvalid("审批链接无效")
            if request.status in {"approved", "rejected", "email_failed"}:
                raise AlreadyProcessed(request)
            if request.status != "pending":
                raise ApprovalTokenExpired("审批链接已过期")
            if request.approval_expires_at <= now:
                conn.execute(
                    update(access_requests)
                    .where(
                        access_requests.c.id == request.id,
                        access_requests.c.status == "pending",
                    )
                    .values(status="expired", updated_at=now)
                )
                expired = True
            else:
                result = conn.execute(
                    update(access_requests)
                    .where(
                        access_requests.c.id == request.id,
                        access_requests.c.status == "pending",
                    )
                    .values(
                        status="approved",
                        approved_at=now,
                        access_expires_at=now + timedelta(hours=access_hours),
                        access_token_hash=hash_token(access_token),
                        updated_at=now,
                    )
                )
                if result.rowcount != 1:
                    processed = self._get_by_conn(conn, "id", request.id)
                else:
                    updated = self._get_by_conn(conn, "id", request.id)

        if expired:
            raise ApprovalTokenExpired("审批链接已过期")
        if processed is not None:
            raise AlreadyProcessed(processed)
        assert updated is not None
        return ApprovedRequest(request=updated, access_token=access_token)

    def reject(self, approval_token: str | None) -> AccessRequest:
        if not isinstance(approval_token, str) or not approval_token:
            raise ApprovalTokenInvalid("审批链接无效")

        now = self._now()
        expired = False
        processed: AccessRequest | None = None
        updated: AccessRequest | None = None

        with self._write() as conn:
            request = self._get_by_conn(
                conn, "approval_token_hash", hash_token(approval_token)
            )
            if request is None:
                raise ApprovalTokenInvalid("审批链接无效")
            if request.status in {"approved", "rejected", "email_failed"}:
                raise AlreadyProcessed(request)
            if request.status != "pending":
                raise ApprovalTokenExpired("审批链接已过期")
            if request.approval_expires_at <= now:
                conn.execute(
                    update(access_requests)
                    .where(
                        access_requests.c.id == request.id,
                        access_requests.c.status == "pending",
                    )
                    .values(status="expired", updated_at=now)
                )
                expired = True
            else:
                result = conn.execute(
                    update(access_requests)
                    .where(
                        access_requests.c.id == request.id,
                        access_requests.c.status == "pending",
                    )
                    .values(status="rejected", updated_at=now)
                )
                if result.rowcount != 1:
                    processed = self._get_by_conn(conn, "id", request.id)
                else:
                    updated = self._get_by_conn(conn, "id", request.id)

        if expired:
            raise ApprovalTokenExpired("审批链接已过期")
        if processed is not None:
            raise AlreadyProcessed(processed)
        assert updated is not None
        return updated

    def claim(self, request_id: str, browser_token: str | None) -> AccessRequest:
        if not isinstance(browser_token, str) or not browser_token:
            raise BrowserTokenInvalid("浏览器凭证缺失")

        request = self.get_request(request_id)
        if request is None:
            raise RequestNotFound("申请不存在")
        if not constant_time_equal(
            hash_token(browser_token), request.browser_token_hash
        ):
            raise BrowserTokenInvalid("浏览器凭证不匹配")

        status = request.effective_status(self._now())
        if status == "expired":
            raise AccessExpired("访问已到期")
        if status != "approved":
            raise AccessNotReady(status)
        return request

    def validate_access_token(self, token: str | None) -> AccessRequest:
        request = self.get_by_access_token(token)
        if request is None:
            raise AccessTokenInvalid("访问链接无效")
        if request.effective_status(self._now()) != "approved":
            raise AccessExpired("访问已到期")
        return request

    def ping(self) -> bool:
        try:
            with self.engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            return True
        except SQLAlchemyError:
            return False
        except Exception:  # noqa: BLE001 - 健康检查不能向上抛
            return False

    def cleanup(self, *, retention_days: int = 30) -> int:
        if retention_days < 1:
            raise ValueError("retention_days 必须大于等于 1")
        cutoff = self._now() - timedelta(days=retention_days)
        with self._write() as conn:
            result = conn.execute(
                delete(access_requests).where(
                    access_requests.c.created_at < cutoff
                )
            )
            return int(result.rowcount or 0)


__all__ = ["DatabaseAccessStore"]

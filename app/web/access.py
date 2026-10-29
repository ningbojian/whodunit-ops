"""访问申请的 SQLite 存储。

一条申请的生命周期：

  pending ──批准──> approved ──8h 后──> 视为 expired
     │
     ├──拒绝──> rejected
     └──发信失败──> email_failed

数据库只存 token 的 SHA-256 哈希。审批 token、访问 token、浏览器 token
的明文只出现在创建它们的那一次返回值里，然后交给邮件/签名 Session。
"""

from __future__ import annotations

import math
import re
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from app.web.security import (
    constant_time_equal,
    email_hash as hash_email,
    hash_ip,
    hash_token,
    mask_email,
    new_token,
    normalize_display_name,
    normalize_email,
)

UTC = timezone.utc
CST = timezone(timedelta(hours=8))


class AccessError(RuntimeError):
    """访问申请相关的业务错误基类。"""


class RequestCooldown(AccessError):
    def __init__(
        self,
        request: "AccessRequest",
        retry_after_seconds: int = 0,
    ) -> None:
        self.request = request
        self.retry_after_seconds = max(0, retry_after_seconds)
        super().__init__("申请过于频繁")


class RequestLimitExceeded(AccessError):
    def __init__(self, scope: str, retry_after_seconds: int) -> None:
        self.scope = scope
        self.retry_after_seconds = max(1, retry_after_seconds)
        super().__init__("申请超过限流")


class ApprovalError(AccessError):
    """审批链接不可用。"""


class ApprovalTokenInvalid(ApprovalError):
    pass


class ApprovalTokenExpired(ApprovalError):
    pass


class AlreadyProcessed(ApprovalError):
    def __init__(self, request: "AccessRequest") -> None:
        self.request = request
        super().__init__(f"申请已处理：{request.status}")


class RequestNotFound(AccessError):
    pass


class BrowserTokenInvalid(AccessError):
    pass


class AccessNotReady(AccessError):
    def __init__(self, status: str) -> None:
        self.status = status
        super().__init__(f"访问尚未生效：{status}")


class AccessTokenInvalid(AccessError):
    pass


class AccessExpired(AccessError):
    pass


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _iso(value: datetime) -> str:
    # 固定 UTC、精确到秒，字符串排序与时间排序一致。
    return _as_utc(value).isoformat(timespec="seconds")


def _parse_dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _cst_day_start(now: datetime) -> datetime:
    local = _as_utc(now).astimezone(CST)
    start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return start.astimezone(UTC)


def _retry_after(target: datetime, now: datetime) -> int:
    return max(1, math.ceil((target - now).total_seconds()))


@dataclass(frozen=True, slots=True)
class AccessRequest:
    id: str
    email: str
    email_hash: str
    display_name: str
    status: str
    created_at: datetime
    updated_at: datetime
    approval_expires_at: datetime
    approved_at: datetime | None
    access_expires_at: datetime | None
    approval_token_hash: str
    access_token_hash: str | None
    browser_token_hash: str
    requester_ip_hash: str | None
    email_error: str | None

    def effective_status(self, now: datetime | None = None) -> str:
        """把时间也纳入判断，避免依赖后台定时任务改状态。"""
        moment = _as_utc(now) if now is not None else datetime.now(UTC)
        if self.status == "pending" and self.approval_expires_at <= moment:
            return "expired"
        if self.status == "approved":
            if not self.access_expires_at or self.access_expires_at <= moment:
                return "expired"
        return self.status

    def approval_remaining_seconds(self, now: datetime | None = None) -> int:
        moment = _as_utc(now) if now is not None else datetime.now(UTC)
        return max(0, int((self.approval_expires_at - moment).total_seconds()))

    def access_remaining_seconds(self, now: datetime | None = None) -> int:
        moment = _as_utc(now) if now is not None else datetime.now(UTC)
        if self.status != "approved" or not self.access_expires_at:
            return 0
        return max(0, int((self.access_expires_at - moment).total_seconds()))

    def public_dict(self, now: datetime | None = None) -> dict:
        """只包含可以展示给用户/审批页的字段。"""
        moment = _as_utc(now) if now is not None else datetime.now(UTC)
        return {
            "id": self.id,
            "email": mask_email(self.email),
            "display_name": self.display_name,
            "status": self.effective_status(moment),
            "created_at": _iso(self.created_at),
            "approval_expires_at": _iso(self.approval_expires_at),
            "access_expires_at": (
                _iso(self.access_expires_at) if self.access_expires_at else None
            ),
            "access_remaining_seconds": self.access_remaining_seconds(moment),
        }


@dataclass(frozen=True, slots=True)
class CreatedRequest:
    request: AccessRequest
    approval_token: str
    browser_token: str
    # 这个邮箱此前已被批准且仍在访问期内。不是错误：用户换了浏览器、清了
    # Cookie 或误点了「退出」时，只能靠重新申请找回访问。带上它只为让页面
    # 说清楚「你之前的授权还有效，这次是重新通知开发者」。
    replaces_active: "AccessRequest | None" = None


@dataclass(frozen=True, slots=True)
class ApprovedRequest:
    request: AccessRequest
    access_token: str


class AccessStore:
    """访问申请存储。所有 SQL 都在进程内串行执行。"""

    def __init__(
        self,
        db_path: str | Path,
        *,
        now_fn: Callable[[], datetime] | None = None,
        ip_hash_secret: str,
    ) -> None:
        if not isinstance(ip_hash_secret, str) or not ip_hash_secret:
            raise ValueError("ip_hash_secret 不能为空")
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._now_fn = now_fn or (lambda: datetime.now(UTC))
        self._ip_hash_secret = ip_hash_secret
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        with self._lock, self._conn:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS access_requests (
                    id TEXT PRIMARY KEY,
                    email TEXT NOT NULL,
                    email_hash TEXT NOT NULL,
                    display_name TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    approval_expires_at TEXT NOT NULL,
                    approved_at TEXT,
                    access_expires_at TEXT,
                    approval_token_hash TEXT NOT NULL,
                    access_token_hash TEXT,
                    browser_token_hash TEXT NOT NULL,
                    requester_ip_hash TEXT,
                    email_error TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_access_email
                    ON access_requests(email_hash, created_at);
                CREATE INDEX IF NOT EXISTS idx_access_status
                    ON access_requests(status, access_expires_at);
                CREATE INDEX IF NOT EXISTS idx_access_approval_token
                    ON access_requests(approval_token_hash);
                CREATE INDEX IF NOT EXISTS idx_access_access_token
                    ON access_requests(access_token_hash);
                CREATE INDEX IF NOT EXISTS idx_access_ip
                    ON access_requests(requester_ip_hash, created_at);
                CREATE INDEX IF NOT EXISTS idx_access_created
                    ON access_requests(created_at);
                """
            )

    def __enter__(self) -> "AccessStore":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _now(self) -> datetime:
        return _as_utc(self._now_fn())

    @staticmethod
    def _row(row: sqlite3.Row | None) -> AccessRequest | None:
        if row is None:
            return None
        return AccessRequest(
            id=row["id"],
            email=row["email"],
            email_hash=row["email_hash"],
            display_name=row["display_name"],
            status=row["status"],
            created_at=_parse_dt(row["created_at"]),
            updated_at=_parse_dt(row["updated_at"]),
            approval_expires_at=_parse_dt(row["approval_expires_at"]),
            approved_at=_parse_dt(row["approved_at"]),
            access_expires_at=_parse_dt(row["access_expires_at"]),
            approval_token_hash=row["approval_token_hash"],
            access_token_hash=row["access_token_hash"],
            browser_token_hash=row["browser_token_hash"],
            requester_ip_hash=row["requester_ip_hash"],
            email_error=row["email_error"],
        )

    def _get_by(self, column: str, value: str) -> AccessRequest | None:
        # column 只由本模块的常量传入，不能来自用户输入。
        if column not in {"id", "approval_token_hash", "access_token_hash"}:
            raise ValueError("非法查询列")
        row = self._conn.execute(
            f"SELECT * FROM access_requests WHERE {column} = ? LIMIT 1",
            (value,),
        ).fetchone()
        return self._row(row)

    def _find_active(self, email_hash_value: str, now: datetime) -> AccessRequest | None:
        row = self._conn.execute(
            """
            SELECT * FROM access_requests
            WHERE email_hash = ? AND status = 'approved'
              AND access_expires_at IS NOT NULL AND access_expires_at > ?
            ORDER BY created_at DESC, rowid DESC LIMIT 1
            """,
            (email_hash_value, _iso(now)),
        ).fetchone()
        return self._row(row)

    def _find_pending(self, email_hash_value: str, now: datetime) -> AccessRequest | None:
        row = self._conn.execute(
            """
            SELECT * FROM access_requests
            WHERE email_hash = ? AND status = 'pending'
              AND approval_expires_at > ?
            ORDER BY created_at DESC, rowid DESC LIMIT 1
            """,
            (email_hash_value, _iso(now)),
        ).fetchone()
        return self._row(row)

    def _latest_for_email(self, email_hash_value: str) -> AccessRequest | None:
        row = self._conn.execute(
            """
            SELECT * FROM access_requests
            WHERE email_hash = ?
            ORDER BY created_at DESC, rowid DESC LIMIT 1
            """,
            (email_hash_value,),
        ).fetchone()
        return self._row(row)

    def _count_ip_since(self, ip_hash_value: str, since: datetime) -> int:
        row = self._conn.execute(
            """
            SELECT COUNT(*) AS n FROM access_requests
            WHERE requester_ip_hash = ? AND created_at > ?
            """,
            (ip_hash_value, _iso(since)),
        ).fetchone()
        return int(row["n"])

    def _oldest_ip_since(self, ip_hash_value: str, since: datetime) -> datetime | None:
        row = self._conn.execute(
            """
            SELECT MIN(created_at) AS first_at FROM access_requests
            WHERE requester_ip_hash = ? AND created_at > ?
            """,
            (ip_hash_value, _iso(since)),
        ).fetchone()
        return _parse_dt(row["first_at"]) if row and row["first_at"] else None

    def _count_since(self, since: datetime) -> int:
        # 当天的日界是包含的：00:00:00 创建的申请属于新的一天。
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM access_requests WHERE created_at >= ?",
            (_iso(since),),
        ).fetchone()
        return int(row["n"])

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
        """创建 pending 申请；返回一次性明文 token。

        检查顺序：有效访问 > 待审批 > 邮箱冷却 > IP 限流 > 全站日限流。
        所有检查与写入在同一个事务和进程锁里完成。
        """
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

        with self._lock, self._conn:
            # 已有有效访问**不**拦新申请：审批权始终在开发者手里，而用户
            # 一旦换了浏览器、清了 Cookie 或误点「退出」，就再没有任何自助
            # 找回访问的办法。旧实现会在这里直接报错，访问期只有 8 小时时
            # 最多等 8 小时；默认改成 30 天后，等于把人锁在外面一个月。
            superseded = self._find_active(email_hash_value, now)

            pending = self._find_pending(email_hash_value, now)
            if pending is not None:
                raise RequestCooldown(pending)

            latest = self._latest_for_email(email_hash_value)
            # email_failed 说明邮件没发出去，不该让用户等冷却；
            # 允许立刻重试，否则一次 Resend 抖动会锁死 10 分钟。
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
                if self._count_ip_since(ip_hash_value, hour_ago) >= ip_limit_per_hour:
                    oldest = self._oldest_ip_since(ip_hash_value, hour_ago)
                    target = (oldest or now) + timedelta(hours=1)
                    raise RequestLimitExceeded("ip", _retry_after(target, now))

            if global_limit_per_day > 0:
                day_start = _cst_day_start(now)
                if self._count_since(day_start) >= global_limit_per_day:
                    target = day_start + timedelta(days=1)
                    raise RequestLimitExceeded("global", _retry_after(target, now))

            request_id = uuid.uuid4().hex
            approval_token = new_token(32)
            self._conn.execute(
                """
                INSERT INTO access_requests (
                    id, email, email_hash, display_name, status,
                    created_at, updated_at, approval_expires_at,
                    approved_at, access_expires_at,
                    approval_token_hash, access_token_hash,
                    browser_token_hash, requester_ip_hash, email_error
                ) VALUES (?, ?, ?, ?, 'pending', ?, ?, ?, NULL, NULL, ?, NULL, ?, ?, NULL)
                """,
                (
                    request_id,
                    normalized,
                    email_hash_value,
                    display,
                    _iso(now),
                    _iso(now),
                    _iso(approval_expires),
                    hash_token(approval_token),
                    browser_hash,
                    ip_hash_value,
                ),
            )
            created = self._get_by("id", request_id)
            return CreatedRequest(
                request=created,
                approval_token=approval_token,
                browser_token=browser_token,
                replaces_active=superseded,
            )

    def get_request(self, request_id: str) -> AccessRequest | None:
        if not request_id:
            return None
        with self._lock:
            return self._get_by("id", request_id)

    def get_by_approval_token(self, token: str | None) -> AccessRequest | None:
        if not isinstance(token, str) or not token:
            return None
        with self._lock:
            return self._get_by("approval_token_hash", hash_token(token))

    def get_by_access_token(self, token: str | None) -> AccessRequest | None:
        if not isinstance(token, str) or not token:
            return None
        with self._lock:
            return self._get_by("access_token_hash", hash_token(token))

    def status_for_request(self, request_id: str) -> str | None:
        request = self.get_request(request_id)
        return request.effective_status(self._now()) if request else None

    def mark_email_failed(self, request_id: str, reason: str) -> bool:
        """只把仍在 pending 的申请标记为发信失败，避免覆盖审批竞态。"""
        safe = re.sub(r"[^A-Za-z0-9_.:-]", "_", str(reason))[:120] or "unknown"
        with self._lock, self._conn:
            cursor = self._conn.execute(
                """
                UPDATE access_requests
                SET status = 'email_failed', updated_at = ?, email_error = ?
                WHERE id = ? AND status = 'pending'
                """,
                (_iso(self._now()), safe, request_id),
            )
            return cursor.rowcount > 0

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
        with self._lock, self._conn:
            request = self._get_by("approval_token_hash", hash_token(approval_token))
            if request is None:
                raise ApprovalTokenInvalid("审批链接无效")
            if request.status in {"approved", "rejected", "email_failed"}:
                raise AlreadyProcessed(request)
            if request.status != "pending":
                raise ApprovalTokenExpired("审批链接已过期")
            if request.approval_expires_at <= now:
                self._conn.execute(
                    "UPDATE access_requests SET status = 'expired', updated_at = ? "
                    "WHERE id = ? AND status = 'pending'",
                    (_iso(now), request.id),
                )
                # 这是审计状态，不是成功路径；先提交再抛错，
                # 否则 with self._conn 会因异常回滚这次更新。
                self._conn.commit()
                raise ApprovalTokenExpired("审批链接已过期")

            access_token = new_token(32)
            access_expires = now + timedelta(hours=access_hours)
            self._conn.execute(
                """
                UPDATE access_requests
                SET status = 'approved', approved_at = ?, access_expires_at = ?,
                    access_token_hash = ?, updated_at = ?
                WHERE id = ? AND status = 'pending'
                """,
                (
                    _iso(now),
                    _iso(access_expires),
                    hash_token(access_token),
                    _iso(now),
                    request.id,
                ),
            )
            updated = self._get_by("id", request.id)
            return ApprovedRequest(request=updated, access_token=access_token)

    def reject(self, approval_token: str | None) -> AccessRequest:
        if not isinstance(approval_token, str) or not approval_token:
            raise ApprovalTokenInvalid("审批链接无效")

        now = self._now()
        with self._lock, self._conn:
            request = self._get_by("approval_token_hash", hash_token(approval_token))
            if request is None:
                raise ApprovalTokenInvalid("审批链接无效")
            if request.status in {"approved", "rejected", "email_failed"}:
                raise AlreadyProcessed(request)
            if request.status != "pending":
                raise ApprovalTokenExpired("审批链接已过期")
            if request.approval_expires_at <= now:
                self._conn.execute(
                    "UPDATE access_requests SET status = 'expired', updated_at = ? "
                    "WHERE id = ? AND status = 'pending'",
                    (_iso(now), request.id),
                )
                self._conn.commit()
                raise ApprovalTokenExpired("审批链接已过期")

            self._conn.execute(
                "UPDATE access_requests SET status = 'rejected', updated_at = ? "
                "WHERE id = ? AND status = 'pending'",
                (_iso(now), request.id),
            )
            return self._get_by("id", request.id)

    def claim(self, request_id: str, browser_token: str | None) -> AccessRequest:
        """用户在等待页确认审批通过后，用 session 里的浏览器 token 领会话。"""
        if not isinstance(browser_token, str) or not browser_token:
            raise BrowserTokenInvalid("浏览器凭证缺失")

        with self._lock:
            request = self._get_by("id", request_id)
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
        """校验备用链接里的访问 token。"""
        request = self.get_by_access_token(token)
        if request is None:
            raise AccessTokenInvalid("访问链接无效")
        if request.effective_status(self._now()) != "approved":
            raise AccessExpired("访问已到期")
        return request

    def ping(self) -> bool:
        try:
            with self._lock:
                self._conn.execute("SELECT 1").fetchone()
            return True
        except sqlite3.Error:
            return False

    def cleanup(self, *, retention_days: int = 30) -> int:
        """删除过老的申请记录，默认保留 30 天。"""
        if retention_days < 1:
            raise ValueError("retention_days 必须大于等于 1")
        cutoff = self._now() - timedelta(days=retention_days)
        with self._lock, self._conn:
            cursor = self._conn.execute(
                "DELETE FROM access_requests WHERE created_at < ?",
                (_iso(cutoff),),
            )
            return cursor.rowcount

"""网页版安全工具：邮箱规范化、哈希、随机 token、CSRF 与同源判断。

这里刻意不依赖 FastAPI / Starlette，方便用纯函数测试。
真正读取请求头、Cookie 的代码放在后续的 auth.py。
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import unicodedata
from typing import Mapping
from urllib.parse import urlsplit

# 邮箱长度上限来自 RFC 5321。
MAX_EMAIL_LENGTH = 254
MAX_LOCAL_PART_LENGTH = 64

# 只接受常见 ASCII 邮箱。刻意不接受带显示名的 "张三 <a@b.com>"，
# 也不接受 Unicode 域名，避免同一邮箱出现多种写法。
_LOCAL_PART = r"[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+(?:\.[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+)*"
_DOMAIN_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
_EMAIL_RE = re.compile(rf"^{_LOCAL_PART}@{_DOMAIN_LABEL}(?:\.{_DOMAIN_LABEL})+$")


class InvalidEmail(ValueError):
    """邮箱格式不合法。"""


def normalize_email(value: str) -> str:
    """校验并规范化邮箱：去首尾空白 + 转小写。

    同一邮箱的大小写差异必须落到同一个哈希、同一个工作区。
    """
    if not isinstance(value, str):
        raise InvalidEmail("邮箱必须是字符串")

    normalized = value.strip().lower()
    if not normalized:
        raise InvalidEmail("请填写邮箱")
    if len(normalized) > MAX_EMAIL_LENGTH:
        raise InvalidEmail("邮箱过长")
    if not _EMAIL_RE.fullmatch(normalized):
        raise InvalidEmail("邮箱格式不正确")

    local_part, _, domain = normalized.partition("@")
    if len(local_part) > MAX_LOCAL_PART_LENGTH:
        raise InvalidEmail("邮箱过长")
    # 纯数字顶级域名不是有效邮箱，但也可能是内网域名；这里只拦明显错误。
    if domain.startswith(".") or domain.endswith(".") or ".." in domain:
        raise InvalidEmail("邮箱格式不正确")
    return normalized


def is_valid_email(value: str) -> bool:
    try:
        normalize_email(value)
    except (InvalidEmail, TypeError):
        return False
    return True


def email_hash(email: str) -> str:
    """邮箱的稳定 SHA-256 哈希，用于目录名，不直接暴露明文邮箱。"""
    normalized = normalize_email(email)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def workspace_key(email: str) -> str:
    """每个邮箱的数据目录名：哈希前 16 位，足够避免日常碰撞。"""
    return email_hash(email)[:16]


def hash_token(token: str) -> str:
    """token 只存哈希；数据库泄露时原始 token 仍不可用。"""
    if not isinstance(token, str) or not token:
        raise ValueError("token 不能为空")
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def hash_ip(address: str, secret: str) -> str:
    """按 IP 限流用的键：**加盐**哈希，库泄露后不能反推地址。

    ``hash_token`` 对高熵 token 够用（256 位猜不出来），对 IP 不够：IPv4
    只有 2^32 个取值，裸 SHA-256 建一张表几分钟就能枚举回来——存下来的
    就不是「匿名计数」，而是地址本身。这里改成 HMAC。

    密钥取 ``WEB_SESSION_SECRET``：它必须跨重启、跨实例稳定，否则同一个
    IP 每次都被算成不同的键，限流直接失效。反过来，轮换这个密钥会让当前
    这一小时的 IP 计数归零，代价可以忽略。
    """
    if not isinstance(address, str) or not address:
        raise ValueError("address 不能为空")
    if not isinstance(secret, str) or not secret:
        raise ValueError("IP 哈希需要密钥")
    return hmac.new(
        secret.encode("utf-8"), address.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def new_token(nbytes: int = 32) -> str:
    """生成 URL 安全的随机 token。

    32 字节 ≈ 256 位熵，作为审批/访问链接的能力凭证足够。
    """
    if nbytes < 16:
        raise ValueError("token 熵过低")
    return secrets.token_urlsafe(nbytes)


def constant_time_equal(left: str | None, right: str | None) -> bool:
    """常量时间比较，避免通过响应时间猜测 token。"""
    if not isinstance(left, str) or not isinstance(right, str):
        return False
    if not left or not right:
        return False
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


def csrf_ok(provided: str | None, expected: str | None) -> bool:
    return constant_time_equal(provided, expected)


def mask_email(email: str) -> str:
    """展示用脱敏邮箱：a***@example.com。"""
    normalized = normalize_email(email)
    local_part, _, domain = normalized.partition("@")
    if len(local_part) <= 1:
        masked = "*"
    else:
        masked = f"{local_part[0]}***"
    return f"{masked}@{domain}"


def normalize_display_name(
    value: str | None,
    *,
    fallback_email: str | None = None,
    max_length: int = 40,
) -> str:
    """清理称呼：去控制字符、折叠空白、限长。

    Jinja 会再做 HTML 转义；这里主要是防止换行/控制字符破坏页面和日志。
    """
    text = value.strip() if isinstance(value, str) else ""
    cleaned = "".join(
        ch for ch in text if not unicodedata.category(ch).startswith("C")
    )
    cleaned = " ".join(cleaned.split())
    if cleaned:
        return cleaned[:max_length]
    if fallback_email:
        return normalize_email(fallback_email).partition("@")[0][:max_length]
    return "访客"


def _origin_matches(origin: str, host: str) -> bool:
    """判断一个 Origin/Referer 是否属于当前 Host。

    Host 可能带端口，也可能不带；Origin 一定带 scheme。
    只比较 hostname 和显式端口，不接受 userinfo。
    """
    try:
        parsed = urlsplit(origin)
        host_parsed = urlsplit(f"//{host}")
        origin_port = parsed.port
        host_port = host_parsed.port
    except ValueError:
        return False

    if parsed.scheme not in ("http", "https"):
        return False
    if parsed.username or parsed.password:
        return False
    if not parsed.hostname or not host_parsed.hostname:
        return False
    if parsed.hostname.lower().rstrip(".") != host_parsed.hostname.lower().rstrip("."):
        return False
    # Host 没写端口时，不推断 scheme 的默认端口；只比 hostname。
    if host_port is None:
        return True
    default_port = 443 if parsed.scheme == "https" else 80
    return (origin_port or default_port) == host_port


def same_origin_from_headers(
    headers: Mapping[str, str],
    *,
    allow_missing: bool = False,
) -> bool:
    """检查 Origin，缺失时回退 Referer。

    浏览器提交表单时至少会带一个。默认两者都缺失视为不通过；
    仅本地脚本/测试可以显式 allow_missing=True。
    """
    origin = headers.get("origin") or headers.get("Origin")
    referer = headers.get("referer") or headers.get("Referer")
    host = headers.get("host") or headers.get("Host")
    if not host:
        return False
    candidate = origin or referer
    if not candidate:
        return allow_missing
    return _origin_matches(candidate, host)

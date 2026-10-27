"""Tavily 联网搜索适配器 + 三层成本闸门（Agent 工具 12 的实现）。

成本边界（2026-10-22 从 tavily.com/pricing 核实）：
  - Free 档 $0 / 1,000 credits/月，用完即停不会自动扣费；
  - 1 credit ≈ 1 次 basic search（本模块固定 max_results=3）；
  - 默认配额（租户 10/日、全局 30/日 ≈ 900/月）结构性低于免费档。

三道硬闸全部在调用 Tavily 之前于服务端执行，被拒绝的请求零成本：
  1. validate_search_query：长度 + 拒绝名单 + 剧本域正向信号；
  2. 租户日配额（WEB_SEARCH_TENANT_DAILY_LIMIT，默认 10，0=不限）；
  3. 全局日配额（WEB_SEARCH_GLOBAL_DAILY_LIMIT，默认 30，0=不限）。
计数只在 provider 真正调用成功后递增（失败 / 超时不计，不惩罚用户；
缓存命中不计——那次调用已经付过费）。

安全边界：
  - API key 只来自服务端环境变量，绝不入日志 / 信封 / 错误消息；
  - 未配置 key 或 provider=none/未知 → NullProvider → not_configured；
  - 审计日志只记放行 / 拒绝与截断 40 字的 query，不含密钥与用户身份。
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Protocol

logger = logging.getLogger("app.integrations.web_search")

UTC = timezone.utc

TAVILY_URL = "https://api.tavily.com/search"
SNIPPET_MAX_CHARS = 200

# ---------------------------------------------------------------------------
# 入参校验（纯函数，可单测）
# ---------------------------------------------------------------------------

# 剧本域正向信号：命中其一才放行（外加《》书名号与「XX剧本杀」形态）。
DOMAIN_KEYWORDS: tuple[str, ...] = (
    "剧本", "剧本杀", "几人本", "人本", "时长", "类型", "发行", "工作室", "作者",
    "测评", "评测", "推荐", "硬核", "情感", "机制", "恐怖", "变格", "新本格",
    "阵营", "推理", "开放", "沉浸", "欢乐", "悬疑", "惊悚", "还原", "本格",
    "博弈", "桌游", "狼人杀", "血染钟楼", "剧本圈", "独家", "盒装", "城限",
)

# 明显无关话题的拒绝名单（正则，命中即拒）。
_DENY_RE = re.compile(
    r"天气|气温|下雨|台风|股票|基金|理财|彩票|双色球|大乐透|汇率|房价|比特币"
    r"|时政|新闻|两会|选举|战争|明星|八卦|绯闻|热搜|吃瓜"
    r"|NBA|足球|世界杯|奥运|CBA|英超|西甲|篮球|网球|F1"
    r"|高考|考研|公务员|四六级|雅思|托福"
    r"|怎么做饭|菜谱|旅游攻略|机票|酒店预订",
)

# 「XX剧本杀」形态（XX 后紧跟「剧本杀」，前面是名称性文字）。
_SCRIPTSHAPE_RE = re.compile(r"[一-鿿A-Za-z0-9]{2,12}剧本杀")
_TITLE_RE = re.compile(r"《[^》]{1,40}》")


class SearchInputRejected(ValueError):
    """入参校验失败；message 可直接给店长看。"""


def validate_search_query(query: Any) -> str:
    """校验查询词；通过返回规范化文本，失败抛 SearchInputRejected。

    纯函数：不读环境、不联网、不写日志（审计由调用方负责）。
    """
    text = str(query or "").strip()
    if len(text) < 2:
        raise SearchInputRejected("查询词太短，至少 2 个字符。")
    if len(text) > 120:
        raise SearchInputRejected("查询词太长，最多 120 个字符。")
    if _DENY_RE.search(text):
        raise SearchInputRejected("网络搜索仅支持剧本相关公开信息查询。")
    positive = (
        any(word in text for word in DOMAIN_KEYWORDS)
        or bool(_TITLE_RE.search(text))
        or bool(_SCRIPTSHAPE_RE.search(text))
    )
    if not positive:
        raise SearchInputRejected("网络搜索仅支持剧本相关公开信息查询。")
    return text


# ---------------------------------------------------------------------------
# Provider 契约与实现
# ---------------------------------------------------------------------------


class SearchProviderError(RuntimeError):
    """provider 调用失败（非 2xx / 超时 / 响应不可解析）。"""


class SearchProvider(Protocol):
    name: str

    @property
    def configured(self) -> bool: ...

    def search(self, query: str, *, max_results: int = 3) -> list[dict[str, str]]: ...


class NullProvider:
    """未配置时的空实现；configured=False，工具层转 not_configured。"""

    name = "null"
    configured = False

    def search(self, query: str, *, max_results: int = 3) -> list[dict[str, str]]:
        raise SearchProviderError("not_configured")


def _http_post_json(url: str, payload: dict[str, Any], *, timeout: float) -> dict[str, Any]:
    """HTTPS POST JSON；测试可 monkeypatch 本函数做请求形状断言。"""
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "Accept": "application/json",
            "User-Agent": "mystery-store-agent/1.0",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - 固定 https 域名
        raw = response.read(1 << 20)
    parsed = json.loads(raw.decode("utf-8", errors="replace"))
    if not isinstance(parsed, dict):
        raise SearchProviderError("搜索服务返回了无法解析的响应。")
    return parsed


@dataclass(frozen=True)
class TavilyProvider:
    api_key: str
    timeout_seconds: float = 8.0
    name: str = "tavily"

    def __post_init__(self) -> None:
        if not str(self.api_key).strip():
            raise ValueError("TavilyProvider 需要非空 api_key")
        if self.timeout_seconds <= 0 or self.timeout_seconds > 30:
            raise ValueError("Tavily 超时必须在 0..30 秒。")

    @property
    def configured(self) -> bool:
        return bool(str(self.api_key).strip())

    def search(self, query: str, *, max_results: int = 3) -> list[dict[str, str]]:
        payload = {
            "api_key": self.api_key,
            "query": query,
            "max_results": max(1, min(int(max_results), 5)),
            "include_answer": False,
        }
        try:
            body = _http_post_json(
                TAVILY_URL, payload, timeout=self.timeout_seconds
            )
        except urllib.error.HTTPError as exc:
            # 不把 key 或响应原文放进错误消息。
            raise SearchProviderError(f"搜索服务返回 HTTP {exc.code}") from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            reason = getattr(exc, "reason", None) or exc.__class__.__name__
            raise SearchProviderError(f"搜索服务不可达：{reason}") from None
        except ValueError:
            raise SearchProviderError("搜索服务返回了无法解析的响应。") from None

        results: list[dict[str, str]] = []
        for item in body.get("results") or []:
            if not isinstance(item, dict):
                continue
            content = str(item.get("content") or "")
            results.append(
                {
                    "title": str(item.get("title") or "")[:120],
                    "url": str(item.get("url") or ""),
                    "snippet": content[:SNIPPET_MAX_CHARS],
                }
            )
            if len(results) >= max(1, min(int(max_results), 5)):
                break
        return results


_provider_lock = threading.Lock()
_provider_cache: dict[tuple[str, bool], Any] = {}


def get_provider(*, reload: bool = False) -> Any:
    """按 env 构造（并缓存）provider；key 未设或 provider 未知 → Null。"""
    name = (os.getenv("WEB_SEARCH_PROVIDER", "") or "tavily").strip().lower()
    key = (os.getenv("WEB_SEARCH_API_KEY", "") or "").strip()
    cache_key = (name, bool(key))
    with _provider_lock:
        if not reload and cache_key in _provider_cache:
            return _provider_cache[cache_key]
        if name in {"none", "null"} or not key:
            provider: Any = NullProvider()
        elif name == "tavily":
            provider = TavilyProvider(api_key=key)
        else:
            provider = NullProvider()
        _provider_cache[cache_key] = provider
        return provider


def reset_provider_cache() -> None:
    """测试用：清空 provider 单例缓存。"""
    with _provider_lock:
        _provider_cache.clear()


# ---------------------------------------------------------------------------
# 配额计数（Excel sidecar JSON + SQL 表）
# ---------------------------------------------------------------------------


class SearchQuotaExceeded(RuntimeError):
    def __init__(self, scope: str, used: int, limit: int) -> None:
        self.scope = scope  # "tenant" | "global"
        self.used = used
        self.limit = limit
        super().__init__(f"{scope} quota exceeded: {used}/{limit}")


class SearchUsage(Protocol):
    def used(self, tenant_id: str, day: str) -> int: ...

    def global_used(self, day: str) -> int: ...

    def increment(self, tenant_id: str, day: str) -> None: ...


class JsonSearchUsage:
    """Excel 模式的 sidecar 计数器：data/search_usage_daily.json。

    频率极低（每天最多几十次写），整文件原子替换即可。
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()

    def _load(self) -> dict[str, dict[str, int]]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError, OSError):
            return {}
        if not isinstance(raw, dict):
            return {}
        out: dict[str, dict[str, int]] = {}
        for day, tenants in raw.items():
            if not isinstance(tenants, dict):
                continue
            counts: dict[str, int] = {}
            for tenant, count in tenants.items():
                try:
                    counts[str(tenant)] = int(count)
                except (TypeError, ValueError):
                    continue
            out[str(day)] = counts
        return out

    def used(self, tenant_id: str, day: str) -> int:
        with self._lock:
            return self._load().get(str(day), {}).get(str(tenant_id), 0)

    def global_used(self, day: str) -> int:
        with self._lock:
            return sum(self._load().get(str(day), {}).values())

    def increment(self, tenant_id: str, day: str) -> None:
        with self._lock:
            data = self._load()
            tenants = data.setdefault(str(day), {})
            tenants[str(tenant_id)] = int(tenants.get(str(tenant_id), 0)) + 1
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(
                json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8"
            )
            tmp.replace(self.path)


class SqlSearchUsage:
    """数据库模式计数器：public search_usage_daily 表（无租户隔离，全局共享）。"""

    def __init__(self, engine: Any) -> None:
        self.engine = engine
        self._lock = threading.RLock()

    def used(self, tenant_id: str, day: str) -> int:
        from sqlalchemy import text

        with self._lock:
            with self.engine.connect() as conn:
                row = conn.execute(
                    text(
                        "SELECT count FROM search_usage_daily "
                        "WHERE tenant_id = :tenant AND day = :day"
                    ),
                    {"tenant": str(tenant_id), "day": str(day)},
                ).fetchone()
                return int(row[0]) if row else 0

    def global_used(self, day: str) -> int:
        from sqlalchemy import text

        with self._lock:
            with self.engine.connect() as conn:
                row = conn.execute(
                    text(
                        "SELECT COALESCE(SUM(count), 0) FROM search_usage_daily "
                        "WHERE day = :day"
                    ),
                    {"day": str(day)},
                ).fetchone()
                return int(row[0]) if row else 0

    def increment(self, tenant_id: str, day: str) -> None:
        from sqlalchemy import text

        now = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")
        with self._lock:
            with self.engine.begin() as conn:
                conn.execute(
                    text(
                        "INSERT INTO search_usage_daily(tenant_id, day, count, updated_at) "
                        "VALUES (:tenant, :day, 1, :now) "
                        "ON CONFLICT (tenant_id, day) DO UPDATE SET "
                        "count = search_usage_daily.count + 1, updated_at = :now"
                    ),
                    {"tenant": str(tenant_id), "day": str(day), "now": now},
                )


def build_search_usage(
    *, uses_database: bool, engine: Any = None, data_dir: Any = None
) -> Any:
    """按存储模式构造计数器；database 优先，Excel 落 sidecar JSON。"""
    if uses_database and engine is not None:
        return SqlSearchUsage(engine)
    from app.config import DATA_DIR

    target = Path(data_dir) if data_dir else DATA_DIR
    return JsonSearchUsage(target / "search_usage_daily.json")


# ---------------------------------------------------------------------------
# 搜索执行（纯函数 + 缓存 + 配额编排）
# ---------------------------------------------------------------------------


class SearchError(RuntimeError):
    """工具层失败；code 直接进 envelope 的 error_code。"""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


_cache_lock = threading.Lock()
_cache: dict[str, tuple[float, dict[str, Any]]] = {}

# 配额闸门串行锁：把「检查 → 调用 provider → 计数」包成一个临界区，
# 防止同进程并发线程都在计数递增前通过检查、导致全局配额超发。
# 量级极小（全局 ≤500 次/日，provider 超时上限 8s），持锁跨网络调用可接受。
_quota_gate_lock = threading.Lock()


def _cache_key(query: str) -> str:
    return " ".join(query.split()).casefold()


def reset_search_cache() -> None:
    """测试用：清空查询缓存。"""
    with _cache_lock:
        _cache.clear()


def web_search(
    query: str,
    *,
    provider: Any = None,
    max_results: int = 3,
    cache_ttl: float = 600,
    now: Callable[[], datetime] | None = None,
) -> dict[str, Any]:
    """执行一次搜索（带模块级缓存）。provider 失败抛 SearchProviderError。

    本函数不做入参校验与配额——那是 run_web_search 的职责；
    单测可直接注入 provider 测请求形状与缓存。
    """
    resolved = provider if provider is not None else get_provider()
    if not getattr(resolved, "configured", False):
        raise SearchProviderError("not_configured")

    key = _cache_key(query)
    clock = now or (lambda: datetime.now(UTC))
    with _cache_lock:
        hit = _cache.get(key)
        if hit is not None and time.monotonic() - hit[0] <= cache_ttl:
            payload = dict(hit[1])
            payload["cached"] = True
            return payload

    results = resolved.search(query, max_results=max_results)
    payload = {
        "query": query,
        "results": results,
        "provider": getattr(resolved, "name", "unknown"),
        "cached": False,
        "fetched_at": clock().isoformat(timespec="seconds"),
    }
    with _cache_lock:
        _cache[key] = (time.monotonic(), dict(payload))
    return payload


def _clamp_limit(raw: Any, default: int, lo: int, hi: int) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, value))


def run_web_search(
    query: Any,
    *,
    provider: Any = None,
    usage: Any = None,
    tenant_id: str = "default",
    day: str | None = None,
    max_results: int = 3,
    cache_ttl: float = 600,
    now: Callable[[], datetime] | None = None,
) -> dict[str, Any]:
    """三层闸门 + 搜索编排；业务失败抛 SearchError（code + 可读 message）。

    顺序：入参校验 → provider 已配置 → 租户配额 → 全局配额 → 调用 → 成功后计数。
    配额检查到计数递增整体持有进程级锁：并发线程不会都在计数前通过检查，
    全局配额（默认 30/日）不会被同进程并发超发。
    """
    from app.config import (
        WEB_SEARCH_GLOBAL_DAILY_LIMIT,
        WEB_SEARCH_TENANT_DAILY_LIMIT,
    )

    tenant_limit = _clamp_limit(WEB_SEARCH_TENANT_DAILY_LIMIT, 10, 0, 100)
    global_limit = _clamp_limit(WEB_SEARCH_GLOBAL_DAILY_LIMIT, 30, 0, 500)

    try:
        text = validate_search_query(query)
    except SearchInputRejected as exc:
        logger.info("web_search denied query=%.40s reason=length_or_deny", text=str(query)[:40])
        raise SearchError("query_rejected", str(exc)) from None
    logger.info("web_search allowed query=%.40s", text[:40])

    resolved = provider if provider is not None else get_provider()
    if not getattr(resolved, "configured", False):
        raise SearchError(
            "not_configured",
            "未配置网络搜索（WEB_SEARCH_API_KEY 未设置），请使用本地 script_library。",
        )

    if day is None:
        clock = now or (lambda: datetime.now(UTC))
        day = clock().strftime("%Y-%m-%d")
    day = str(day).strip() or datetime.now(UTC).strftime("%Y-%m-%d")
    tenant_id = str(tenant_id or "default").strip() or "default"

    with _quota_gate_lock:
        if usage is not None and tenant_limit > 0:
            used = int(usage.used(tenant_id, day))
            if used >= tenant_limit:
                raise SearchError(
                    "quota_exceeded",
                    f"今日网络搜索次数已用完（每商家每日 {tenant_limit} 次），"
                    "请优先使用本地剧本库，明天再试。",
                )
        if usage is not None and global_limit > 0:
            gused = int(usage.global_used(day))
            if gused >= global_limit:
                raise SearchError(
                    "quota_exceeded",
                    f"今日全站网络搜索次数已用完（每日 {global_limit} 次），请明天再试。",
                )

        try:
            payload = web_search(
                text,
                provider=resolved,
                max_results=max_results,
                cache_ttl=cache_ttl,
                now=now,
            )
        except SearchProviderError as exc:
            logger.warning("web_search failed type=%s", type(exc).__name__)
            if str(exc) == "not_configured":
                raise SearchError(
                    "not_configured",
                    "未配置网络搜索（WEB_SEARCH_API_KEY 未设置），请使用本地 script_library。",
                ) from None
            raise SearchError("search_failed", "网络搜索失败，请稍后再试。") from None

        # 只在真正调用了 provider（非缓存命中）且成功后计数。
        if usage is not None and not payload.get("cached"):
            try:
                usage.increment(tenant_id, day)
            except Exception:  # noqa: BLE001 - 计数失败不惩罚用户，结果照常返回
                logger.warning("web_search usage increment failed")
    return payload


__all__ = [
    "DOMAIN_KEYWORDS",
    "NullProvider",
    "SearchError",
    "SearchInputRejected",
    "SearchProviderError",
    "SearchQuotaExceeded",
    "TavilyProvider",
    "JsonSearchUsage",
    "SqlSearchUsage",
    "build_search_usage",
    "get_provider",
    "reset_provider_cache",
    "reset_search_cache",
    "run_web_search",
    "validate_search_query",
    "web_search",
]

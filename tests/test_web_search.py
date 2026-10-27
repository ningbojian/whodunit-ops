"""web_search 工具与三层成本闸门的测试。

全部离线：HTTP 通过 monkeypatch _http_post_json 截断，不发真实请求。
"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timezone

import pytest

from app.integrations.web_search import (
    JsonSearchUsage,
    NullProvider,
    SearchError,
    SearchInputRejected,
    SearchProviderError,
    TavilyProvider,
    build_search_usage,
    get_provider,
    reset_provider_cache,
    reset_search_cache,
    run_web_search,
    validate_search_query,
    web_search,
)

UTC = timezone.utc


@pytest.fixture(autouse=True)
def _clean_caches():
    reset_provider_cache()
    reset_search_cache()
    yield
    reset_provider_cache()
    reset_search_cache()


@pytest.fixture
def fake_http(monkeypatch):
    """替换 _http_post_json，记录请求并返回可编程响应。"""
    calls: list[dict] = []

    def _install(response):
        def _fake(url, payload, *, timeout):
            calls.append({"url": url, "payload": payload, "timeout": timeout})
            if isinstance(response, Exception):
                raise response
            return response

        monkeypatch.setattr(
            "app.integrations.web_search._http_post_json", _fake
        )
        return calls

    return _install


TAVILY_OK = {
    "results": [
        {"title": "年轮 剧本测评", "url": "https://example.com/a", "content": "情感本" * 100},
        {"title": "第二条", "url": "https://example.com/b", "content": "内容"},
    ]
}


class TestValidateQuery:
    def test_剧本关键词放行(self):
        assert validate_search_query("年轮 剧本杀 测评") == "年轮 剧本杀 测评"

    def test_书名号放行(self):
        assert validate_search_query("《雾隐山庄》怎么样") == "《雾隐山庄》怎么样"

    def test_XX剧本杀形态放行(self):
        assert validate_search_query("年轮剧本杀 几人本") == "年轮剧本杀 几人本"

    def test_太短拒绝(self):
        with pytest.raises(SearchInputRejected):
            validate_search_query("本")

    def test_太长拒绝(self):
        with pytest.raises(SearchInputRejected):
            validate_search_query("剧本" + "测" * 120)

    @pytest.mark.parametrize(
        "query",
        ["北京今天天气怎么样", "特斯拉股票多少钱", "NBA今天谁赢了", "怎么做番茄炒蛋"],
    )
    def test_无关话题拒绝(self, query):
        with pytest.raises(SearchInputRejected):
            validate_search_query(query)

    def test_无正向信号拒绝(self):
        with pytest.raises(SearchInputRejected):
            validate_search_query("随便问问 世界和平")


class TestProviders:
    def test_未配置_env_返回_null(self, monkeypatch):
        monkeypatch.delenv("WEB_SEARCH_API_KEY", raising=False)
        monkeypatch.setenv("WEB_SEARCH_PROVIDER", "tavily")
        provider = get_provider()
        assert isinstance(provider, NullProvider)
        assert provider.configured is False

    def test_provider_none_返回_null(self, monkeypatch):
        monkeypatch.setenv("WEB_SEARCH_PROVIDER", "none")
        monkeypatch.setenv("WEB_SEARCH_API_KEY", "tvly-x")
        assert isinstance(get_provider(), NullProvider)

    def test_未知_provider_返回_null(self, monkeypatch):
        monkeypatch.setenv("WEB_SEARCH_PROVIDER", "bing")
        monkeypatch.setenv("WEB_SEARCH_API_KEY", "tvly-x")
        assert isinstance(get_provider(), NullProvider)

    def test_tavily_已配置(self, monkeypatch):
        monkeypatch.setenv("WEB_SEARCH_PROVIDER", "tavily")
        monkeypatch.setenv("WEB_SEARCH_API_KEY", "tvly-test-key")
        provider = get_provider()
        assert isinstance(provider, TavilyProvider)
        assert provider.configured is True

    def test_tavily_请求形状(self, fake_http):
        calls = fake_http(TAVILY_OK)
        provider = TavilyProvider(api_key="tvly-secret")
        results = provider.search("年轮 剧本测评", max_results=3)
        assert len(calls) == 1
        assert calls[0]["url"] == "https://api.tavily.com/search"
        body = calls[0]["payload"]
        assert body["api_key"] == "tvly-secret"
        assert body["query"] == "年轮 剧本测评"
        assert body["max_results"] == 3
        assert body["include_answer"] is False
        assert results[0]["title"] == "年轮 剧本测评"

    def test_snippet_截断_200字(self, fake_http):
        fake_http(TAVILY_OK)
        provider = TavilyProvider(api_key="tvly-secret")
        results = provider.search("年轮 剧本测评")
        assert len(results[0]["snippet"]) == 200

    def test_http错误_不泄漏_key(self, fake_http, caplog):
        import urllib.error

        fake_http(urllib.error.HTTPError(
            "https://api.tavily.com/search", 401, "Unauthorized", {}, None
        ))
        provider = TavilyProvider(api_key="tvly-secret")
        with pytest.raises(SearchProviderError) as excinfo:
            provider.search("年轮 剧本测评")
        assert "tvly-secret" not in str(excinfo.value)
        assert "401" in str(excinfo.value)

    def test超时_search_failed(self, fake_http):
        fake_http(TimeoutError("timed out"))
        provider = TavilyProvider(api_key="tvly-secret")
        with pytest.raises(SearchProviderError):
            provider.search("年轮 剧本测评")


class TestWebSearchFn:
    def test_null_provider_not_configured(self):
        with pytest.raises(SearchProviderError) as excinfo:
            web_search("年轮 剧本测评", provider=NullProvider())
        assert str(excinfo.value) == "not_configured"

    def test_缓存命中_cached_true(self, fake_http):
        calls = fake_http(TAVILY_OK)
        provider = TavilyProvider(api_key="tvly-secret")
        first = web_search("年轮 剧本测评", provider=provider)
        second = web_search(" 年轮 剧本测评 ", provider=provider)
        assert first["cached"] is False
        assert second["cached"] is True
        assert len(calls) == 1  # 第二次走缓存，不再请求

    def test_信封字段完整(self, fake_http):
        fake_http(TAVILY_OK)
        provider = TavilyProvider(api_key="tvly-secret")
        payload = web_search("年轮 剧本测评", provider=provider)
        assert set(payload) == {"query", "results", "provider", "cached", "fetched_at"}
        assert payload["provider"] == "tavily"


class _CountingUsage:
    """测试用内存计数器（实现 SearchUsage 协议）。"""

    def __init__(self):
        self.rows: dict[tuple[str, str], int] = {}
        self.increments = 0

    def used(self, tenant_id, day):
        return self.rows.get((tenant_id, day), 0)

    def global_used(self, day):
        return sum(count for (_, d), count in self.rows.items() if d == day)

    def increment(self, tenant_id, day):
        self.increments += 1
        self.rows[(tenant_id, day)] = self.used(tenant_id, day) + 1


class TestRunWebSearch:
    def test_未配置_not_configured(self, monkeypatch):
        monkeypatch.delenv("WEB_SEARCH_API_KEY", raising=False)
        with pytest.raises(SearchError) as excinfo:
            run_web_search("年轮 剧本测评")
        assert excinfo.value.code == "not_configured"

    def test_拒绝名单_query_rejected(self, monkeypatch):
        monkeypatch.setenv("WEB_SEARCH_API_KEY", "tvly-x")
        with pytest.raises(SearchError) as excinfo:
            run_web_search("今天天气怎么样")
        assert excinfo.value.code == "query_rejected"

    def test_租户配额_exceeded(self, monkeypatch):
        monkeypatch.setenv("WEB_SEARCH_API_KEY", "tvly-x")
        usage = _CountingUsage()
        usage.rows[("tenant-a", "2026-10-22")] = 10  # 默认租户上限 10
        with pytest.raises(SearchError) as excinfo:
            run_web_search(
                "年轮 剧本测评", usage=usage, tenant_id="tenant-a", day="2026-10-22"
            )
        assert excinfo.value.code == "quota_exceeded"
        assert "10" in excinfo.value.message

    def test_全局配额_exceeded(self, monkeypatch):
        monkeypatch.setenv("WEB_SEARCH_API_KEY", "tvly-x")
        usage = _CountingUsage()
        usage.rows[("other", "2026-10-22")] = 30  # 默认全局上限 30
        with pytest.raises(SearchError) as excinfo:
            run_web_search(
                "年轮 剧本测评", usage=usage, tenant_id="tenant-a", day="2026-10-22"
            )
        assert excinfo.value.code == "quota_exceeded"

    def test_成功后计数(self, fake_http, monkeypatch):
        monkeypatch.setenv("WEB_SEARCH_API_KEY", "tvly-x")
        fake_http(TAVILY_OK)
        usage = _CountingUsage()
        payload = run_web_search(
            "年轮 剧本测评",
            provider=TavilyProvider(api_key="tvly-secret"),
            usage=usage,
            tenant_id="tenant-a",
            day="2026-10-22",
        )
        assert payload["results"]
        assert usage.used("tenant-a", "2026-10-22") == 1

    def test_失败不计数(self, fake_http, monkeypatch):
        monkeypatch.setenv("WEB_SEARCH_API_KEY", "tvly-x")
        fake_http(TimeoutError("timed out"))
        usage = _CountingUsage()
        with pytest.raises(SearchError) as excinfo:
            run_web_search(
                "年轮 剧本测评",
                provider=TavilyProvider(api_key="tvly-secret"),
                usage=usage,
                tenant_id="tenant-a",
                day="2026-10-22",
            )
        assert excinfo.value.code == "search_failed"
        assert usage.increments == 0

    def test_缓存命中不计数(self, fake_http, monkeypatch):
        monkeypatch.setenv("WEB_SEARCH_API_KEY", "tvly-x")
        fake_http(TAVILY_OK)
        usage = _CountingUsage()
        provider = TavilyProvider(api_key="tvly-secret")
        run_web_search(
            "年轮 剧本测评", provider=provider, usage=usage,
            tenant_id="tenant-a", day="2026-10-22",
        )
        run_web_search(
            "年轮 剧本测评", provider=provider, usage=usage,
            tenant_id="tenant-a", day="2026-10-22",
        )
        assert usage.increments == 1  # 第二次走缓存，不重复计数

    def test_审计日志不含_key(self, fake_http, monkeypatch, caplog):
        monkeypatch.setenv("WEB_SEARCH_API_KEY", "tvly-secret-key")
        fake_http(TAVILY_OK)
        with caplog.at_level("INFO", logger="app.integrations.web_search"):
            run_web_search(
                "年轮 剧本测评",
                provider=TavilyProvider(api_key="tvly-secret-key"),
            )
        joined = " ".join(record.getMessage() for record in caplog.records)
        assert "tvly-secret-key" not in joined

    def test_并发下全局配额不超发(self, monkeypatch):
        """check→call→increment 持进程级锁：并发不得突破全局上限。"""
        def _slow_ok(url, payload, *, timeout):
            time.sleep(0.1)  # 拉开并发窗口，无锁时三个线程都会通过检查
            return TAVILY_OK

        monkeypatch.setattr("app.integrations.web_search._http_post_json", _slow_ok)
        usage = _CountingUsage()
        usage.rows[("other", "2026-10-22")] = 29  # 全局默认上限 30，只剩 1 次
        results: list[str] = []

        def _call(i: int) -> None:
            try:
                run_web_search(
                    f"年轮 剧本测评 {i}",  # 不同 query 规避缓存
                    provider=TavilyProvider(api_key="tvly-secret"),
                    usage=usage,
                    tenant_id=f"tenant-{i}",
                    day="2026-10-22",
                )
                results.append("ok")
            except SearchError as exc:
                results.append(exc.code)

        threads = [threading.Thread(target=_call, args=(i,)) for i in range(3)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert results.count("ok") == 1
        assert results.count("quota_exceeded") == 2
        assert usage.global_used("2026-10-22") == 30


class TestJsonUsage:
    def test_roundtrip(self, tmp_path):
        store = JsonSearchUsage(tmp_path / "search_usage_daily.json")
        assert store.used("t1", "2026-10-22") == 0
        store.increment("t1", "2026-10-22")
        store.increment("t1", "2026-10-22")
        store.increment("t2", "2026-10-22")
        assert store.used("t1", "2026-10-22") == 2
        assert store.used("t2", "2026-10-22") == 1
        assert store.global_used("2026-10-22") == 3
        assert store.global_used("2026-10-23") == 0

    def test_坏文件_当空表(self, tmp_path):
        path = tmp_path / "search_usage_daily.json"
        path.write_text("not json", encoding="utf-8")
        store = JsonSearchUsage(path)
        assert store.used("t1", "2026-10-22") == 0

    def test_文件内容可解析(self, tmp_path):
        store = JsonSearchUsage(tmp_path / "search_usage_daily.json")
        store.increment("t1", "2026-10-22")
        raw = json.loads((tmp_path / "search_usage_daily.json").read_text(encoding="utf-8"))
        assert raw["2026-10-22"]["t1"] == 1

    def test_工厂_excel模式(self, tmp_path):
        store = build_search_usage(uses_database=False, data_dir=tmp_path)
        assert isinstance(store, JsonSearchUsage)
        assert store.path == tmp_path / "search_usage_daily.json"


class TestAgentToolIntegration:
    """web_search 作为第 12 个工具的 envelope 行为（在 agent 层）。"""

    def test_工具注册存在(self):
        from app.agent import build_store_tools

        names = [item.name for item in build_store_tools()]
        assert "web_search" in names

    def test_未配置_envelope(self, tmp_path, monkeypatch):
        monkeypatch.delenv("WEB_SEARCH_API_KEY", raising=False)
        from app.agent import AgentDeps, build_store_tools

        deps = AgentDeps(
            sessions_path=tmp_path / "s.xlsx",
            bookings_path=tmp_path / "b.xlsx",
            scripts_path=tmp_path / "sc.xlsx",
            search_usage=JsonSearchUsage(tmp_path / "usage.json"),
        )
        tools = {item.name: item for item in build_store_tools(deps)}
        payload = json.loads(tools["web_search"].invoke({"query": "年轮 剧本测评"}))
        assert payload["ok"] is False
        assert payload["error_code"] == "not_configured"

    def test_成功_envelope_带非官方警示(self, tmp_path, fake_http):
        from app.agent import AgentDeps, build_store_tools

        fake_http(TAVILY_OK)
        deps = AgentDeps(
            sessions_path=tmp_path / "s.xlsx",
            bookings_path=tmp_path / "b.xlsx",
            scripts_path=tmp_path / "sc.xlsx",
            tenant_id="tenant-test",
            search_usage=JsonSearchUsage(tmp_path / "usage.json"),
            web_search_provider=TavilyProvider(api_key="tvly-secret"),
        )
        tools = {item.name: item for item in build_store_tools(deps)}
        payload = json.loads(tools["web_search"].invoke({"query": "年轮 剧本测评"}))
        assert payload["ok"] is True
        assert payload["data"]["results"]
        assert any("非官方" in item for item in payload["warnings"])

    def test_拒绝名单_envelope(self, tmp_path, monkeypatch):
        monkeypatch.setenv("WEB_SEARCH_API_KEY", "tvly-x")
        from app.agent import AgentDeps, build_store_tools

        deps = AgentDeps(
            sessions_path=tmp_path / "s.xlsx",
            bookings_path=tmp_path / "b.xlsx",
            scripts_path=tmp_path / "sc.xlsx",
            search_usage=JsonSearchUsage(tmp_path / "usage.json"),
        )
        tools = {item.name: item for item in build_store_tools(deps)}
        payload = json.loads(tools["web_search"].invoke({"query": "明天天气"}))
        assert payload["ok"] is False
        assert payload["error_code"] == "query_rejected"

    def test_未注入_search_usage_仍走配额闸门(self, tmp_path, fake_http, monkeypatch):
        """deps.search_usage=None 时回退 Excel sidecar 计数器，配额不旁路。

        回归锚点：工具体曾把 deps.search_usage（None）传给 run_web_search，
        丢掉回退出的 usage，导致 CLI/未接线场景配额与计数完全失效。
        """
        from app.agent import AgentDeps, build_store_tools

        fake_http(TAVILY_OK)
        monkeypatch.setattr("app.config.DATA_DIR", tmp_path)
        monkeypatch.setattr("app.config.WEB_SEARCH_TENANT_DAILY_LIMIT", 1)
        deps = AgentDeps(
            sessions_path=tmp_path / "s.xlsx",
            bookings_path=tmp_path / "b.xlsx",
            scripts_path=tmp_path / "sc.xlsx",
            tenant_id="tenant-test",
            web_search_provider=TavilyProvider(api_key="tvly-secret"),
            # search_usage 故意不传：必须回退到 DATA_DIR 下的 sidecar
        )
        tools = {item.name: item for item in build_store_tools(deps)}
        first = json.loads(tools["web_search"].invoke({"query": "年轮 剧本测评"}))
        assert first["ok"] is True
        # 回退计数器已落盘（配额计数真的发生了）
        sidecar = tmp_path / "search_usage_daily.json"
        assert sidecar.exists()
        second = json.loads(tools["web_search"].invoke({"query": "雾隐山庄 剧本测评"}))
        assert second["ok"] is False
        assert second["error_code"] == "quota_exceeded"


class TestSystemPrompt:
    def test_提示词含路由N与第13条(self):
        from app.agent import SYSTEM_PROMPT

        assert "网络信息非官方" in SYSTEM_PROMPT
        assert "N. 新公开信息" in SYSTEM_PROMPT
        assert "web_search" in SYSTEM_PROMPT

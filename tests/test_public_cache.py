"""公共库帧缓存的行为测试（离线，不碰真实 data/）。"""

from __future__ import annotations

import pandas as pd
import pytest

from app.web.public_cache import PublicScriptsCache


class _CountingLibrary:
    """按调用次数返回固定帧的假公共库后端。"""

    def __init__(self, frame: pd.DataFrame | None = None, *, raises: bool = False):
        self.calls = 0
        self._frame = frame
        self._raises = raises

    def load(self) -> pd.DataFrame | None:
        self.calls += 1
        if self._raises:
            raise RuntimeError("boom")
        return self._frame


def _frame() -> pd.DataFrame:
    return pd.DataFrame({"script_id": ["pub-1"], "script_name": ["雾雨山"]})


def test_second_call_uses_the_cached_frame():
    library = _CountingLibrary(_frame())
    cache = PublicScriptsCache(library)

    first = cache.frame()
    second = cache.frame()

    assert first is not None and len(first) == 1
    assert second is not None and len(second) == 1
    assert library.calls == 1


def test_invalidate_forces_a_reload():
    library = _CountingLibrary(_frame())
    cache = PublicScriptsCache(library)

    cache.frame()
    cache.invalidate()
    cache.frame()

    assert library.calls == 2


def test_empty_library_caches_none():
    """公共库为空时也算加载过，不要每次请求都再读一遍。"""
    library = _CountingLibrary(pd.DataFrame())
    cache = PublicScriptsCache(library)

    assert cache.frame() is None
    assert cache.frame() is None
    assert library.calls == 1


def test_load_failure_is_swallowed_and_cached():
    """公共库读失败不该抛到页面 / 对话，且失败结果也要缓存。"""
    library = _CountingLibrary(raises=True)
    cache = PublicScriptsCache(library)

    assert cache.frame() is None
    assert cache.frame() is None
    assert library.calls == 1


def test_no_loader_returns_none_without_loading():
    assert PublicScriptsCache(None).frame() is None


def test_zero_ttl_disables_caching():
    library = _CountingLibrary(_frame())
    cache = PublicScriptsCache(library, ttl_seconds=0)

    cache.frame()
    cache.frame()

    assert library.calls == 2


def test_first_call_is_not_mistaken_for_a_fresh_cache(monkeypatch):
    """monotonic() 参考点未定义：进程刚启动时它可能很小。

    哨兵如果用 0.0，``monotonic() < ttl`` 会把「从未加载」误判成
    「缓存新鲜」，直接返回 None 而不去加载。
    """
    import app.web.public_cache as module

    monkeypatch.setattr(module.time, "monotonic", lambda: 0.5)
    library = _CountingLibrary(_frame())
    cache = PublicScriptsCache(library, ttl_seconds=300)

    frame = cache.frame()

    assert frame is not None
    assert library.calls == 1

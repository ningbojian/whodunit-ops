"""进程内共享的公共剧本库帧缓存。

公共库是只读、全局共享、几乎不变的数据：5809 行元数据，Web 侧没有写入口
（变更是运营方离线导入）。但两个消费方每个请求 / 每个用户 Agent 都会调
``load()``，而它内部是整表 ``SELECT`` + ``DataFrame`` 重建。

这里把结果缓存起来，由 OperationsService 与 ChatRuntime 共用一个实例——
两者生命周期都是应用级的，同一份额外的数据没必要各存一份。
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

import pandas as pd

logger = logging.getLogger(__name__)


class PublicScriptsCache:
    """带 TTL 的公共库帧缓存；线程安全。

    ``ttl_seconds=0`` 时退化为不缓存（每次调用都重新加载），便于需要
    强一致的调用方或测试使用。
    """

    def __init__(
        self,
        loader: Any = None,
        *,
        ttl_seconds: float = 300.0,
        empty_ttl_seconds: float = 30.0,
    ) -> None:
        self._loader = loader
        self._ttl = ttl_seconds
        # 空结果用更短的 TTL：公共库种子是在首次登录时灌的，万一某个请求
        # 早于种子跑完、把「空」缓存了 300 秒，整站会看起来没有公共库。
        self._empty_ttl = empty_ttl_seconds
        self._frame: pd.DataFrame | None = None
        # 用 None 而不是 0.0 当「还没加载过」的哨兵：time.monotonic() 的
        # 参考点是未定义的（Windows 是开机以来，别处可能接近 0），
        # 拿 0 当哨兵会在进程刚启动时把「未加载」误判成「缓存新鲜」。
        self._loaded_at: float | None = None
        self._lock = threading.Lock()

    def frame(self) -> pd.DataFrame | None:
        """返回公共库帧；失败或为空返回 None。

        None 也会被缓存，避免公共库不可用时每个请求都重走一次异常路径。
        """
        if self._loader is None:
            return None
        if self._ttl <= 0:
            return self._load_once()
        with self._lock:
            if self._loaded_at is not None:
                ttl = self._ttl if self._frame is not None else self._empty_ttl
                if time.monotonic() - self._loaded_at < ttl:
                    return self._frame
        cached = self._load_once()
        with self._lock:
            self._frame = cached
            self._loaded_at = time.monotonic()
        return cached

    def invalidate(self) -> None:
        """丢弃缓存；导入新数据后需要立刻生效时调用。"""
        with self._lock:
            self._frame = None
            self._loaded_at = None

    def _load_once(self) -> pd.DataFrame | None:
        try:
            frame = self._loader.load()
        except Exception as exc:  # noqa: BLE001 - 公共库读失败不该阻塞页面或对话
            logger.warning("public library load failed: %s", type(exc).__name__)
            return None
        if frame is None or getattr(frame, "empty", True):
            return None
        return frame

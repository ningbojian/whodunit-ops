"""模型提供器：生产用 DeepSeek，测试用静态模型。

模型只在第一次真正聊天时初始化，避免仅打开登录页就读取 .env / 建立客户端。
三个模型实例共享给所有用户；对话状态与数据仍然按用户隔离。
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Any, Protocol

from app import llm

logger = logging.getLogger("app.web.models")


class ModelProviderError(RuntimeError):
    """模型初始化失败；消息不包含 key 或上游原始异常。"""


@dataclass(frozen=True, slots=True)
class ModelBundle:
    main: Any
    extraction: Any
    copy: Any


class ModelProvider(Protocol):
    def get(self) -> ModelBundle: ...


class StaticModelProvider:
    """测试注入：三个位置都用同一个脚本模型。"""

    def __init__(
        self,
        model: Any,
        *,
        extraction_model: Any = None,
        copy_model: Any = None,
    ) -> None:
        self._bundle = ModelBundle(
            main=model,
            extraction=extraction_model or model,
            copy=copy_model or model,
        )

    def get(self) -> ModelBundle:
        return self._bundle


class DeepSeekModelProvider:
    def __init__(
        self,
        *,
        max_tokens: int,
        timeout: float,
        max_retries: int = 1,
    ) -> None:
        self._max_tokens = max_tokens
        self._timeout = timeout
        self._max_retries = max_retries
        self._lock = threading.Lock()
        self._bundle: ModelBundle | None = None

    def get(self) -> ModelBundle:
        with self._lock:
            if self._bundle is not None:
                return self._bundle
            try:
                common = {
                    "max_tokens": self._max_tokens,
                    "timeout": self._timeout,
                    "max_retries": self._max_retries,
                }
                main = llm.get_default_model(temperature=0.0, **common)
                extraction = llm.get_default_model(temperature=0.0, **common)
                copy = llm.get_default_model(temperature=0.7, **common)
            except Exception as exc:  # noqa: BLE001 - 初始化失败统一转 503
                logger.warning("model init failed: %s", type(exc).__name__)
                raise ModelProviderError("模型初始化失败") from None
            self._bundle = ModelBundle(
                main=main,
                extraction=extraction,
                copy=copy,
            )
            return self._bundle

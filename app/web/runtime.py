"""网页版聊天运行时：每用户 Agent、锁、并发闸门、历史检查、全站计数。

隔离边界：
  - 每个邮箱哈希一个 Agent / InMemorySaver / thread_id；
  - 每个用户一把锁，同一用户上一轮未结束时拒绝新轮；
  - 全站 BoundedSemaphore 限制同时执行的轮数；
  - 只有真正要调用模型时才预占全站每日轮数。

这里不接收邮箱明文或文件路径，只接收服务端从签名 Session 中取出的 email_hash。
"""

from __future__ import annotations

import logging
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

import pandas as pd

from app.agent import AgentDeps, build_agent, run_turn
from app.llm import message_text
from app.store.workspace import TenantWorkspaceError
from app.web.models import ModelProvider, ModelProviderError
from app.web.serialization import snapshot_for_paths, snapshot_for_store
from app.web.trace import safe_trace
from app.web.usage import UsageError, UsageLimitReached, UsageStore
from app.web.workspace import WorkspaceError, WorkspaceManager, WorkspacePaths

_WORKSPACE_ERRORS = (WorkspaceError, TenantWorkspaceError)

logger = logging.getLogger("app.web.runtime")
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")


class ChatError(RuntimeError):
    status_code = 400
    code = "chat_error"

    def __init__(
        self,
        message: str,
        *,
        retry_after_seconds: int | None = None,
    ) -> None:
        self.message = message
        self.retry_after_seconds = retry_after_seconds
        super().__init__(message)


class BadMessage(ChatError):
    status_code = 400
    code = "bad_message"


class TurnBusy(ChatError):
    status_code = 409
    code = "turn_in_progress"

    def __init__(self) -> None:
        super().__init__("上一轮还在处理，请稍后再试。")


class ConcurrencyFull(ChatError):
    status_code = 409
    code = "server_busy"

    def __init__(self) -> None:
        super().__init__("当前访问人数较多，请稍后再试。")


class HistoryTooLong(ChatError):
    status_code = 409
    code = "history_too_long"

    def __init__(self) -> None:
        super().__init__("会话记录太长，请点击“新会话”后再继续。")


class DailyLimitReached(ChatError):
    status_code = 429
    code = "daily_limit"

    def __init__(self, retry_after_seconds: int) -> None:
        super().__init__(
            "今天全站模型额度已用完，请明天再试。",
            retry_after_seconds=retry_after_seconds,
        )


class ModelUnavailable(ChatError):
    status_code = 503
    code = "model_unavailable"

    def __init__(self) -> None:
        super().__init__("模型服务暂时不可用，请稍后再试。")


class ModelFailure(ChatError):
    status_code = 502
    code = "model_error"

    def __init__(self) -> None:
        super().__init__("本轮处理失败，请稍后重试或新建会话。")


class WorkspaceUnavailable(ChatError):
    status_code = 503
    code = "workspace_unavailable"

    def __init__(self) -> None:
        super().__init__("你的数据工作区暂时不可用，请稍后再试。")


@dataclass(frozen=True, slots=True)
class ChatResult:
    answer: str
    trace: list[dict[str, Any]]
    thread_id: str
    choices: tuple[dict[str, Any], ...] = ()


class _UserRuntime:
    def __init__(
        self,
        email_hash: str,
        paths: WorkspacePaths,
        agent: Any,
        *,
        generation: int = 0,
    ) -> None:
        self.email_hash = email_hash
        self.paths = paths
        self.agent = agent
        self.generation = generation
        self.lock = threading.Lock()
        self.thread_id = self._thread_id(email_hash, generation)

    @staticmethod
    def _thread_id(email_hash: str, generation: int) -> str:
        return f"web-{email_hash[:16]}-{generation}"


class ChatRuntime:
    def __init__(
        self,
        *,
        settings,
        model_provider: ModelProvider,
        workspace: WorkspaceManager,
        usage: UsageStore,
        now_fn: Callable[[], datetime] | None = None,
        public_library: Any = None,
        search_usage: Any = None,
    ) -> None:
        self.settings = settings
        self._models = model_provider
        self._workspace = workspace
        self._usage = usage
        self._now_fn = now_fn or (lambda: datetime.now(timezone.utc))
        # 公共剧本库后端（load() -> DataFrame）；None 时 Agent 仅用私有库。
        self._public_library = public_library
        # web_search 日配额计数器（Json/Sql SearchUsage）；None 时工具内建 Excel sidecar。
        self._search_usage = search_usage
        self._cache_lock = threading.RLock()
        self._users: dict[str, _UserRuntime] = {}
        self._semaphore = threading.BoundedSemaphore(settings.max_concurrent_turns)

    def public_scripts_frame(self) -> pd.DataFrame | None:
        """加载公共库帧；失败返回 None（公共库是增强能力，不阻塞对话）。"""
        if self._public_library is None:
            return None
        try:
            frame = self._public_library.load()
        except Exception as exc:  # noqa: BLE001 - 公共库读失败不阻塞对话
            logger.warning("public library load failed: %s", type(exc).__name__)
            return None
        if frame is None or getattr(frame, "empty", True):
            return None
        return frame

    def _pandas_now(self) -> pd.Timestamp:
        """业务层使用不带时区的门店本地时间（UTC+8）。

        Excel 只存 naive datetime，工具层也按 naive 比较；如果把带时区的
        Timestamp 直接传进去，pandas 会拒绝比较。
        """
        moment = pd.Timestamp(self._now_fn())
        if moment.tzinfo is not None:
            moment = moment.tz_convert("Asia/Shanghai").tz_localize(None)
        return moment.floor("s")

    def business_now(self) -> pd.Timestamp:
        """公开给 Web/Flutter 服务层复用同一门店本地时钟。"""
        return self._pandas_now()

    def _build_agent(self, workspace: WorkspacePaths | Any, email_hash: str = ""):
        try:
            bundle = self._models.get()
        except ModelProviderError:
            raise ModelUnavailable() from None
        store = getattr(workspace, "store", None)
        public_frame = self.public_scripts_frame()
        # web_search 配额租户键：email_hash 前 16 位（与 thread_id 同源，不含邮箱明文）。
        tenant_id = email_hash[:16] if isinstance(email_hash, str) else ""
        if store is not None:
            deps = AgentDeps(
                store=store,
                now=self._pandas_now,
                extraction_model=bundle.extraction,
                copy_model=bundle.copy,
                default_store=self.settings.default_store,
                read_only=False,
                public_scripts=public_frame,
                tenant_id=tenant_id or None,
                search_usage=self._search_usage,
            )
        else:
            deps = AgentDeps(
                sessions_path=workspace.sessions_path,
                bookings_path=workspace.bookings_path,
                scripts_path=workspace.scripts_path,
                now=self._pandas_now,
                extraction_model=bundle.extraction,
                copy_model=bundle.copy,
                default_store=self.settings.default_store,
                read_only=False,
                public_scripts=public_frame,
                tenant_id=tenant_id or None,
                search_usage=self._search_usage,
            )
        try:
            return build_agent(model=bundle.main, deps=deps)
        except Exception as exc:  # noqa: BLE001
            logger.warning("agent build failed: %s", type(exc).__name__)
            raise ModelUnavailable() from None

    def _get_user(self, email_hash: str) -> _UserRuntime:
        if not isinstance(email_hash, str) or not _HASH_RE.fullmatch(email_hash):
            raise ValueError("非法用户标识")
        with self._cache_lock:
            existing = self._users.get(email_hash)
            if existing is not None:
                return existing
            try:
                workspace = self._workspace.ensure(email_hash)
            except _WORKSPACE_ERRORS:
                raise WorkspaceUnavailable() from None
            user = _UserRuntime(
                email_hash,
                workspace,
                self._build_agent(workspace, email_hash),
            )
            self._users[email_hash] = user
            return user

    @staticmethod
    def _history_stats(messages: list[Any]) -> tuple[int, int]:
        chars = 0
        for message in messages:
            chars += len(message_text(message))
        return len(messages), chars

    def snapshot(self, email_hash: str, *, limit: int = 8) -> list[dict[str, Any]]:
        try:
            workspace = self._workspace.ensure(email_hash)
            store = getattr(workspace, "store", None)
            if store is not None:
                return snapshot_for_store(
                    store,
                    now=self._pandas_now(),
                    limit=limit,
                )
            return snapshot_for_paths(
                workspace,
                now=self._pandas_now(),
                limit=limit,
            )
        except _WORKSPACE_ERRORS:
            raise WorkspaceUnavailable() from None
        except Exception as exc:  # noqa: BLE001
            logger.warning("snapshot failed: %s", type(exc).__name__)
            raise WorkspaceUnavailable() from None

    def chat(self, email_hash: str, text: str) -> ChatResult:
        if not isinstance(text, str) or not text.strip():
            raise BadMessage("消息不能为空。")
        if len(text) > self.settings.max_input_chars:
            raise BadMessage(f"单条消息不能超过 {self.settings.max_input_chars} 字。")

        user = self._get_user(email_hash)
        if not user.lock.acquire(blocking=False):
            raise TurnBusy()
        try:
            if not self._semaphore.acquire(blocking=False):
                raise ConcurrencyFull()
            try:
                config = {
                    "configurable": {"thread_id": user.thread_id},
                    "recursion_limit": 12,
                }
                try:
                    state = user.agent.get_state(config)
                    messages = list(state.values.get("messages", []))
                except Exception as exc:  # noqa: BLE001
                    logger.warning("state read failed: %s", type(exc).__name__)
                    raise ModelFailure() from None

                message_count, char_count = self._history_stats(messages)
                if (
                    message_count + 1 > self.settings.max_history_messages
                    or char_count + len(text) > self.settings.max_history_chars
                ):
                    raise HistoryTooLong()

                try:
                    self._usage.reserve()
                except UsageLimitReached as exc:
                    raise DailyLimitReached(exc.retry_after_seconds) from None
                except UsageError:
                    raise WorkspaceUnavailable() from None

                try:
                    turn = run_turn(user.agent, text, config=config)
                except (KeyboardInterrupt, SystemExit):
                    raise
                except Exception as exc:  # noqa: BLE001
                    # 不记录消息正文、工具参数或 key，只记录异常类型。
                    logger.warning(
                        "turn failed user=%s type=%s",
                        email_hash[:16],
                        type(exc).__name__,
                    )
                    raise ModelFailure() from None

                answer = turn.answer.strip()
                if not answer:
                    answer = "模型没有返回文本，请重试或新建会话。"
                from app.web.chat_choices import build_choices

                return ChatResult(
                    answer=answer,
                    trace=safe_trace(turn),
                    thread_id=user.thread_id,
                    choices=tuple(build_choices(turn)),
                )
            finally:
                self._semaphore.release()
        finally:
            user.lock.release()

    def reset(self, email_hash: str) -> None:
        user = self._get_user(email_hash)
        if not user.lock.acquire(blocking=False):
            raise TurnBusy()
        try:
            # 重建 Agent 同时丢弃旧的 InMemorySaver，避免旧线程历史继续占内存。
            user.agent = self._build_agent(user.paths, user.email_hash)
            user.generation += 1
            user.thread_id = _UserRuntime._thread_id(
                user.email_hash,
                user.generation,
            )
        finally:
            user.lock.release()

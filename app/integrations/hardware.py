"""灯光/音效硬件 Skill 的通用 HTTP 适配器。

当前没有具体厂商协议，因此这里只定义契约和通用 HTTP 调用：
  - 软件模式：NullHardwareAdapter，返回 skipped；
  - 硬件模式：POST JSON 到 HARDWARE_SKILL_URL，失败由调用方降级为文字提示。

安全边界：
  - URL/token 只来自服务端环境变量，绝不接受客户端传入；
  - 生产只允许 https，本地调试允许 127.0.0.1/localhost 的 http；
  - 不跟随重定向；错误摘要截断且清除 token。
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class HardwareResult:
    ok: bool
    status: str  # ok / skipped / failed / not_configured
    status_code: int | None = None
    latency_ms: int | None = None
    message: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "status": self.status,
            "status_code": self.status_code,
            "latency_ms": self.latency_ms,
            "message": self.message,
        }


class HardwareConfigError(ValueError):
    pass


class HardwareAdapter(Protocol):
    name: str

    @property
    def configured(self) -> bool: ...

    def send_scene(
        self,
        *,
        run_id: str,
        session_id: str,
        room_id: str | None,
        stage: dict[str, Any],
        event_id: str | None = None,
    ) -> HardwareResult: ...


class NullHardwareAdapter:
    name = "null"
    configured = False

    def send_scene(
        self,
        *,
        run_id: str,
        session_id: str,
        room_id: str | None,
        stage: dict[str, Any],
        event_id: str | None = None,
    ) -> HardwareResult:
        return HardwareResult(ok=False, status="skipped", message="软件模式不发硬件指令。")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


def _sanitize(message: str | None, token: str) -> str | None:
    if not message:
        return None
    text = str(message).replace(token, "[已隐藏]") if token else str(message)
    text = " ".join(text.split())
    return text[:200] or None


def _validated_url(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise HardwareConfigError("HARDWARE_SKILL_URL 必须是 http(s) 地址。")
    if parsed.scheme == "http" and parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise HardwareConfigError("生产硬件地址必须使用 https。")
    return url


@dataclass(frozen=True)
class HttpHardwareAdapter:
    url: str
    token: str = ""
    timeout_seconds: float = 2.0
    name: str = "http"

    def __post_init__(self) -> None:
        object.__setattr__(self, "url", _validated_url(self.url.rstrip("/")))
        if self.timeout_seconds < 0.5 or self.timeout_seconds > 5:
            raise HardwareConfigError("硬件超时必须在 0.5..5 秒。")

    @property
    def configured(self) -> bool:
        return bool(self.url)

    def send_scene(
        self,
        *,
        run_id: str,
        session_id: str,
        room_id: str | None,
        stage: dict[str, Any],
        event_id: str | None = None,
    ) -> HardwareResult:
        scene = stage.get("hardware_scene")
        if not isinstance(scene, dict) or not scene:
            return HardwareResult(
                ok=False, status="skipped", message="当前阶段没有配置硬件场景。"
            )

        payload = {
            "request_id": f"{run_id}:{stage.get('stage_id')}:{event_id or ''}",
            "run_id": run_id,
            "session_id": session_id,
            "room_id": room_id,
            "stage_id": stage.get("stage_id"),
            "stage_seq": stage.get("seq"),
            "scene": scene,
        }
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "Accept": "application/json",
            "User-Agent": "mystery-store-dm/1.0",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        request = urllib.request.Request(
            self.url, data=data, headers=headers, method="POST"
        )
        opener = urllib.request.build_opener(_NoRedirect)
        started = time.perf_counter()
        try:
            with opener.open(request, timeout=self.timeout_seconds) as response:
                status_code = int(getattr(response, "status", 200))
                raw = response.read(65536)
        except urllib.error.HTTPError as exc:
            latency = int((time.perf_counter() - started) * 1000)
            return HardwareResult(
                ok=False,
                status="failed",
                status_code=int(exc.code),
                latency_ms=latency,
                message=f"硬件返回 HTTP {exc.code}",
            )
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            latency = int((time.perf_counter() - started) * 1000)
            reason = getattr(exc, "reason", None) or exc.__class__.__name__
            return HardwareResult(
                ok=False,
                status="failed",
                status_code=None,
                latency_ms=latency,
                message=_sanitize(f"硬件调用失败：{reason}", self.token),
            )
        latency = int((time.perf_counter() - started) * 1000)

        body = raw.decode("utf-8", errors="replace").strip()
        if body:
            try:
                parsed = json.loads(body)
            except ValueError:
                return HardwareResult(
                    ok=False,
                    status="failed",
                    status_code=status_code,
                    latency_ms=latency,
                    message="硬件返回了无法解析的响应。",
                )
            if isinstance(parsed, dict) and parsed.get("ok") is False:
                return HardwareResult(
                    ok=False,
                    status="failed",
                    status_code=status_code,
                    latency_ms=latency,
                    message=_sanitize(parsed.get("message"), self.token)
                    or "硬件 Skill 返回失败。",
                )
            if isinstance(parsed, dict):
                return HardwareResult(
                    ok=True,
                    status="ok",
                    status_code=status_code,
                    latency_ms=latency,
                    message=_sanitize(parsed.get("message"), self.token),
                )
        if 200 <= status_code < 300:
            return HardwareResult(
                ok=True, status="ok", status_code=status_code, latency_ms=latency
            )
        return HardwareResult(
            ok=False,
            status="failed",
            status_code=status_code,
            latency_ms=latency,
            message=f"硬件返回 HTTP {status_code}",
        )


def build_hardware_adapter(
    *,
    url: str | None = None,
    token: str | None = None,
    timeout_seconds: float | None = None,
) -> HardwareAdapter:
    """从显式参数或服务端环境变量构造适配器；未配置时返回 Null。"""
    target = (url if url is not None else os.getenv("HARDWARE_SKILL_URL", "")).strip()
    secret = (token if token is not None else os.getenv("HARDWARE_SKILL_TOKEN", "")).strip()
    raw_timeout = (
        timeout_seconds
        if timeout_seconds is not None
        else os.getenv("HARDWARE_TIMEOUT_SECONDS", "2")
    )
    try:
        timeout = float(raw_timeout)
    except (TypeError, ValueError):
        timeout = 2.0
    if not target:
        return NullHardwareAdapter()
    return HttpHardwareAdapter(url=target, token=secret, timeout_seconds=timeout)


def hardware_configured(*, url: str | None = None) -> bool:
    target = (url if url is not None else os.getenv("HARDWARE_SKILL_URL", "")).strip()
    return bool(target)


__all__ = [
    "HardwareAdapter",
    "HardwareConfigError",
    "HardwareResult",
    "HttpHardwareAdapter",
    "NullHardwareAdapter",
    "build_hardware_adapter",
    "hardware_configured",
]

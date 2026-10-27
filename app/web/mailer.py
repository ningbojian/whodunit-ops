"""邮件发送器。

三种实现：
  ResendEmailSender    生产环境，走 Resend HTTPS API（443 端口）
  ConsoleEmailSender   本地开发，把邮件打到控制台
  RecordingEmailSender 测试用，把邮件记在内存里，不联网

Resend 用 HTTPS API 而不是 SMTP：Render 免费层封锁了 25/465/587 端口，
但 443 正常。错误只保留状态码或异常类型，不记录 key、正文和收件人明文。
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Callable, Protocol

import httpx

RESEND_ENDPOINT = "https://api.resend.com/emails"


class EmailSendError(RuntimeError):
    """邮件发送失败。str(exc) 只包含安全信息。"""

    def __init__(self, reason: str, *, status_code: int | None = None) -> None:
        self.reason = reason
        self.status_code = status_code
        super().__init__(reason)

    def safe_reason(self) -> str:
        if self.status_code is not None:
            return f"{self.reason}:{self.status_code}"
        return self.reason


@dataclass(frozen=True, slots=True)
class EmailMessage:
    to: str
    subject: str
    text: str
    html: str | None = None


class EmailSender(Protocol):
    def send(self, message: EmailMessage) -> None: ...


class ResendEmailSender:
    """通过 https://api.resend.com/emails 发送邮件。"""

    def __init__(
        self,
        api_key: str,
        sender: str,
        *,
        timeout: float = 10.0,
        post: Callable[..., httpx.Response] | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("Resend API key 不能为空")
        if not sender:
            raise ValueError("发件人不能为空")
        self._api_key = api_key
        self._sender = sender
        self._timeout = timeout
        self._post = post or httpx.post

    def send(self, message: EmailMessage) -> None:
        payload = {
            "from": self._sender,
            "to": [message.to],
            "subject": message.subject,
            "text": message.text,
        }
        if message.html:
            payload["html"] = message.html

        try:
            response = self._post(
                RESEND_ENDPOINT,
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=self._timeout,
            )
        except httpx.HTTPError as exc:
            # 不把异常文本打给用户：其中的 URL/请求头可能带敏感信息。
            raise EmailSendError(f"network_{type(exc).__name__}") from None
        except OSError as exc:
            raise EmailSendError(f"network_{type(exc).__name__}") from None

        status = int(getattr(response, "status_code", 0) or 0)
        if not 200 <= status < 300:
            raise EmailSendError("http_error", status_code=status)


def _console_output(text: str) -> None:
    """Windows 控制台/重定向文件可能是 GBK，遇到底层不支持的字符时降级输出。

    控制台邮件本身仍应成功，否则本地开发会误判为“审批邮件发送失败”。
    """
    try:
        print(text, flush=True)
        return
    except UnicodeEncodeError:
        pass
    stream = sys.stdout
    encoding = getattr(stream, "encoding", None) or "utf-8"
    buffer = getattr(stream, "buffer", None)
    if buffer is None:
        print(text.encode(encoding, errors="replace").decode(encoding))
        return
    buffer.write((text + "\n").encode(encoding, errors="replace"))
    buffer.flush()


class ConsoleEmailSender:
    """本地开发：把完整的审批链接打印到控制台。"""

    def __init__(self, output_fn: Callable[[str], None] | None = None) -> None:
        self._output = output_fn or _console_output

    def send(self, message: EmailMessage) -> None:
        lines = [
            "",
            "=" * 68,
            "[本地邮件] 未联网，仅打印在控制台",
            f"收件人: {message.to}",
            f"主题: {message.subject}",
            "-" * 68,
            message.text,
            "=" * 68,
            "",
        ]
        self._output("\n".join(lines))


class RecordingEmailSender:
    """测试用：保留发送记录，便于断言。"""

    def __init__(self) -> None:
        self.messages: list[EmailMessage] = []
        self.fail_with: Exception | None = None

    def send(self, message: EmailMessage) -> None:
        if self.fail_with is not None:
            raise self.fail_with
        self.messages.append(message)

    @property
    def last(self) -> EmailMessage | None:
        return self.messages[-1] if self.messages else None


def build_email_sender(settings) -> EmailSender:
    """按配置创建发送器。配置对象由调用方注入，便于测试。"""
    if settings.email_backend == "console":
        return ConsoleEmailSender()
    if settings.email_backend == "resend":
        return ResendEmailSender(
            api_key=settings.resend_api_key or "",
            sender=settings.email_from,
            timeout=10.0,
        )
    raise ValueError(f"未知 EMAIL_BACKEND: {settings.email_backend!r}")

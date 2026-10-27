"""邮件发送器测试：全部离线，不调用真实 Resend。"""

from __future__ import annotations

import io
from types import SimpleNamespace

import httpx
import pytest

from app.web.mailer import (
    ConsoleEmailSender,
    EmailMessage,
    EmailSendError,
    RecordingEmailSender,
    ResendEmailSender,
    build_email_sender,
)
from tests.web_helpers import make_settings


class FakeResponse:
    def __init__(self, status_code: int = 200) -> None:
        self.status_code = status_code


def _message() -> EmailMessage:
    return EmailMessage(
        to="owner@example.com",
        subject="新的访问申请",
        text="申请编号：abc\n审批链接：https://example.com/approve/xyz",
        html="<p>申请编号：abc</p>",
    )


class TestRecordingSender:
    def test_records(self):
        sender = RecordingEmailSender()
        assert sender.last is None
        sender.send(_message())
        assert len(sender.messages) == 1
        assert sender.last is not None
        assert sender.last.subject == "新的访问申请"

    def test_can_simulate_failure(self):
        sender = RecordingEmailSender()
        sender.fail_with = EmailSendError("boom")
        with pytest.raises(EmailSendError):
            sender.send(_message())
        assert sender.messages == []


class TestConsoleSender:
    def test_prints_all_fields(self):
        output: list[str] = []
        sender = ConsoleEmailSender(output_fn=output.append)
        sender.send(_message())
        text = "\n".join(output)
        assert "owner@example.com" in text
        assert "新的访问申请" in text
        assert "https://example.com/approve/xyz" in text
        assert "本地邮件" in text

    def test_survives_terminal_that_cannot_encode_chinese(self, monkeypatch):
        class GbkStream:
            encoding = "gbk"

            def __init__(self):
                self.buffer = io.BytesIO()

            def write(self, _text):
                raise UnicodeEncodeError("gbk", "x", 0, 1, "unsupported")

            def flush(self):
                pass

        stream = GbkStream()
        monkeypatch.setattr("sys.stdout", stream)
        ConsoleEmailSender().send(_message())
        assert b"https://example.com/approve/xyz" in stream.buffer.getvalue()


class TestResendSender:
    def test_request_shape(self):
        calls = []

        def fake_post(url, *, headers, json, timeout):
            calls.append(
                {
                    "url": url,
                    "headers": headers,
                    "json": json,
                    "timeout": timeout,
                }
            )
            return FakeResponse(200)

        sender = ResendEmailSender(
            "re_test_key",
            "onboarding@resend.dev",
            timeout=7.5,
            post=fake_post,
        )
        sender.send(_message())

        assert len(calls) == 1
        call = calls[0]
        assert call["url"] == "https://api.resend.com/emails"
        assert call["headers"]["Authorization"] == "Bearer re_test_key"
        assert call["headers"]["Content-Type"] == "application/json"
        assert call["timeout"] == 7.5
        assert call["json"]["from"] == "onboarding@resend.dev"
        assert call["json"]["to"] == ["owner@example.com"]
        assert call["json"]["subject"] == "新的访问申请"
        assert "审批链接" in call["json"]["text"]
        assert call["json"]["html"] == "<p>申请编号：abc</p>"

    def test_html_optional(self):
        calls = []

        def fake_post(url, *, headers, json, timeout):
            calls.append(json)
            return FakeResponse(200)

        sender = ResendEmailSender("re_x", "a@b.com", post=fake_post)
        sender.send(EmailMessage(to="c@d.com", subject="s", text="t"))
        assert "html" not in calls[0]

    def test_http_error_keeps_status_only(self):
        sender = ResendEmailSender(
            "re_secret",
            "a@b.com",
            post=lambda *args, **kwargs: FakeResponse(422),
        )
        with pytest.raises(EmailSendError) as caught:
            sender.send(_message())
        assert caught.value.status_code == 422
        assert caught.value.safe_reason() == "http_error:422"
        assert "re_secret" not in str(caught.value)

    @pytest.mark.parametrize(
        "exception",
        [
            httpx.ConnectError("connection refused"),
            httpx.ReadTimeout("timed out"),
            OSError("socket error"),
        ],
    )
    def test_network_errors_are_sanitized(self, exception):
        def fake_post(*args, **kwargs):
            raise exception

        sender = ResendEmailSender("re_secret", "a@b.com", post=fake_post)
        with pytest.raises(EmailSendError) as caught:
            sender.send(_message())
        assert caught.value.status_code is None
        assert caught.value.reason.startswith("network_")
        assert "re_secret" not in str(caught.value)
        assert "socket error" not in str(caught.value)

    def test_requires_key_and_sender(self):
        with pytest.raises(ValueError, match="API key"):
            ResendEmailSender("", "a@b.com")
        with pytest.raises(ValueError, match="发件人"):
            ResendEmailSender("re_x", "")


class TestBuildSender:
    def test_console(self, tmp_path):
        sender = build_email_sender(make_settings(tmp_path))
        assert isinstance(sender, ConsoleEmailSender)

    def test_resend(self, tmp_path):
        settings = make_settings(
            tmp_path,
            email_backend="resend",
            resend_api_key="re_x",
            public_base_url="https://example.com",
        )
        sender = build_email_sender(settings)
        assert isinstance(sender, ResendEmailSender)

    def test_unknown(self):
        with pytest.raises(ValueError, match="未知"):
            build_email_sender(SimpleNamespace(email_backend="smtp"))

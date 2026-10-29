"""网页版安全纯函数测试。"""

from __future__ import annotations

import pytest

from app.web.security import (
    InvalidEmail,
    constant_time_equal,
    csrf_ok,
    email_hash,
    hash_ip,
    hash_token,
    is_valid_email,
    mask_email,
    new_token,
    normalize_display_name,
    normalize_email,
    same_origin_from_headers,
    workspace_key,
)


class TestEmail:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("user@example.com", "user@example.com"),
            ("  User@Example.COM  ", "user@example.com"),
            ("a.b+tag@sub.example.co", "a.b+tag@sub.example.co"),
            ("x_y@example.com", "x_y@example.com"),
        ],
    )
    def test_normalize_valid(self, raw, expected):
        assert normalize_email(raw) == expected

    @pytest.mark.parametrize(
        "raw",
        [
            "",
            "   ",
            "user",
            "user@",
            "@example.com",
            "user@example",
            "user@@example.com",
            "user@.example.com",
            "user@example..com",
            "user@example.com.",
            "user..name@example.com",
            ".user@example.com",
            "user.@example.com",
            "user name@example.com",
            "user@-example.com",
            "user@example-.com",
            "user@exam ple.com",
            "张三 <user@example.com>",
            "user@例子.中国",
        ],
    )
    def test_normalize_invalid(self, raw):
        with pytest.raises(InvalidEmail):
            normalize_email(raw)

    def test_too_long(self):
        local = "a" * 65
        with pytest.raises(InvalidEmail):
            normalize_email(f"{local}@example.com")
        with pytest.raises(InvalidEmail):
            normalize_email("a@" + ("b" * 250) + ".com")

    def test_non_string(self):
        with pytest.raises(InvalidEmail):
            normalize_email(None)

    def test_is_valid(self):
        assert is_valid_email("a@b.co") is True
        assert is_valid_email("a@b") is False

    def test_hash_is_case_insensitive_and_stable(self):
        first = email_hash("User@Example.COM")
        second = email_hash(" user@example.com ")
        assert first == second
        assert len(first) == 64
        assert first != email_hash("other@example.com")

    def test_workspace_key(self):
        key = workspace_key("user@example.com")
        assert len(key) == 16
        assert key == email_hash("user@example.com")[:16]

    def test_mask(self):
        assert mask_email("alice@example.com") == "a***@example.com"
        assert mask_email("a@example.com") == "*@example.com"
        assert "alice" not in mask_email("alice@example.com")


class TestTokens:
    def test_hash_token_not_plaintext(self):
        token = new_token()
        digest = hash_token(token)
        assert token not in digest
        assert len(digest) == 64
        assert digest == hash_token(token)
        assert digest != hash_token(new_token())

    def test_hash_token_rejects_empty(self):
        with pytest.raises(ValueError):
            hash_token("")

    def test_new_token_is_unique_and_long_enough(self):
        tokens = {new_token() for _ in range(50)}
        assert len(tokens) == 50
        assert all(len(token) >= 43 for token in tokens)

    def test_new_token_rejects_low_entropy(self):
        with pytest.raises(ValueError):
            new_token(8)

    def test_constant_time_equal(self):
        assert constant_time_equal("abc", "abc") is True
        assert constant_time_equal("abc", "abd") is False
        assert constant_time_equal("", "") is False
        assert constant_time_equal(None, "abc") is False
        assert constant_time_equal("abc", None) is False

    def test_csrf_ok(self):
        assert csrf_ok("abc", "abc") is True
        assert csrf_ok("abc", "def") is False
        assert csrf_ok(None, "abc") is False


class TestIpHash:
    secret = "s" * 48

    def test_same_ip_and_secret_give_the_same_key(self):
        first = hash_ip("203.0.113.7", self.secret)
        assert first == hash_ip("203.0.113.7", self.secret)
        assert len(first) == 64
        assert first != hash_ip("203.0.113.8", self.secret)

    def test_key_is_not_a_bare_sha256_of_the_address(self):
        """裸 SHA-256 能被彩虹表逐条枚举回地址，所以必须带密钥。"""
        assert hash_ip("203.0.113.7", self.secret) != hash_token("203.0.113.7")

    def test_rotating_the_secret_rotates_every_key(self):
        assert hash_ip("203.0.113.7", self.secret) != hash_ip(
            "203.0.113.7", "t" * 48
        )

    def test_rejects_empty_address_or_secret(self):
        with pytest.raises(ValueError):
            hash_ip("", self.secret)
        with pytest.raises(ValueError):
            hash_ip("203.0.113.7", "")


class TestDisplayName:
    def test_strips_control_chars_and_collapses_space(self):
        assert normalize_display_name("  小\n明\t ") == "小明"
        assert normalize_display_name("小  明") == "小 明"

    def test_fallback_to_email_local_part(self):
        assert normalize_display_name("", fallback_email="alice@example.com") == "alice"
        assert normalize_display_name(None, fallback_email="bob@example.com") == "bob"

    def test_default_and_max_length(self):
        assert normalize_display_name("") == "访客"
        assert len(normalize_display_name("x" * 100, max_length=10)) == 10


class TestSameOrigin:
    def test_matching_origin(self):
        headers = {"host": "example.com", "origin": "https://example.com"}
        assert same_origin_from_headers(headers) is True

    def test_mismatched_origin(self):
        headers = {"host": "example.com", "origin": "https://evil.example"}
        assert same_origin_from_headers(headers) is False

    def test_referer_fallback(self):
        headers = {"host": "example.com", "referer": "https://example.com/login"}
        assert same_origin_from_headers(headers) is True

    def test_missing_both_is_rejected_by_default(self):
        assert same_origin_from_headers({"host": "example.com"}) is False
        assert (
            same_origin_from_headers({"host": "example.com"}, allow_missing=True)
            is True
        )

    def test_missing_host(self):
        assert same_origin_from_headers({"origin": "https://example.com"}) is False

    def test_port_matching(self):
        headers = {"host": "example.com:8443", "origin": "https://example.com:8443"}
        assert same_origin_from_headers(headers) is True
        headers = {"host": "example.com:8443", "origin": "https://example.com"}
        assert same_origin_from_headers(headers) is False

    def test_default_https_port(self):
        headers = {"host": "example.com:443", "origin": "https://example.com"}
        assert same_origin_from_headers(headers) is True

    def test_rejects_null_userinfo_and_bad_scheme(self):
        assert (
            same_origin_from_headers({"host": "example.com", "origin": "null"})
            is False
        )
        assert (
            same_origin_from_headers(
                {"host": "example.com", "origin": "https://u:p@example.com"}
            )
            is False
        )
        assert (
            same_origin_from_headers(
                {"host": "example.com", "origin": "ftp://example.com"}
            )
            is False
        )

    def test_invalid_port(self):
        assert (
            same_origin_from_headers(
                {"host": "example.com", "origin": "https://example.com:bad"}
            )
            is False
        )

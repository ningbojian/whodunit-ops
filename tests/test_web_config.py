"""网页版配置解析与启动期校验。"""

from __future__ import annotations

import pytest

from app.web.config import WebConfigError, WebSettings
from tests.web_helpers import env_base


class TestDefaults:
    def test_development_console_defaults(self, tmp_path):
        settings = WebSettings.from_env(
            env_base(
                WEB_DATA_ROOT=str(tmp_path / "users"),
                WEB_STATE_DB=str(tmp_path / "state.sqlite3"),
            )
        )
        assert settings.env == "development"
        assert settings.storage_backend == "excel"
        assert settings.uses_database is False
        assert settings.email_backend == "console"
        assert settings.owner_email == "owner@example.com"
        assert settings.public_base_url == "http://127.0.0.1:8000"
        assert settings.cookie_secure is False
        assert settings.access_seconds == 8 * 3600
        assert settings.approval_ttl_seconds == 24 * 3600
        assert settings.session_seconds == 24 * 3600
        assert settings.max_input_chars == 2000
        assert settings.max_body_bytes == 32768
        assert settings.request_cooldown_minutes == 10
        assert settings.max_import_bytes == 2 * 1024 * 1024
        assert settings.dm_code_ttl_minutes == 30
        assert settings.dm_token_hours == 12
        assert settings.hardware_skill_url is None
        assert settings.hardware_skill_token is None
        assert settings.hardware_timeout_seconds == 2.0

    def test_production_requires_https_and_resend(self, tmp_path):
        settings = WebSettings.from_env(
            env_base(
                WEB_ENV="production",
                EMAIL_BACKEND="resend",
                RESEND_API_KEY="re_test",
                PUBLIC_BASE_URL="https://example.com/",
                DATABASE_URL="postgresql://user:pass@example.neon.tech/neondb",
                WEB_DATA_ROOT=str(tmp_path / "users"),
                WEB_STATE_DB=str(tmp_path / "state.sqlite3"),
            )
        )
        assert settings.is_production is True
        assert settings.storage_backend == "database"
        assert settings.uses_database is True
        assert settings.cookie_secure is True
        assert settings.public_base_url == "https://example.com"
        assert settings.resend_api_key == "re_test"

    def test_env_aliases(self):
        assert WebSettings.from_env(env_base(WEB_ENV="dev")).env == "development"
        settings = WebSettings.from_env(
            env_base(
                WEB_ENV="prod",
                EMAIL_BACKEND="resend",
                RESEND_API_KEY="re_test",
                PUBLIC_BASE_URL="https://example.com",
                DATABASE_URL="postgresql://user:pass@example.neon.tech/neondb",
            )
        )
        assert settings.env == "production"

    def test_relative_paths_resolve_under_project(self):
        settings = WebSettings.from_env(env_base(WEB_DATA_ROOT="data/test-web-users"))
        assert settings.data_root.is_absolute()
        assert settings.data_root.name == "test-web-users"

    def test_ensure_directories(self, tmp_path):
        settings = WebSettings.from_env(
            env_base(
                WEB_DATA_ROOT=str(tmp_path / "a" / "users"),
                WEB_STATE_DB=str(tmp_path / "b" / "state.sqlite3"),
            )
        )
        settings.ensure_directories()
        assert (tmp_path / "a" / "users").is_dir()
        assert (tmp_path / "b").is_dir()


class TestStorageBackend:
    def test_development_database_without_url_uses_local_sqlite(self, tmp_path):
        settings = WebSettings.from_env(
            env_base(
                WEB_STORAGE_BACKEND="database",
                WEB_DATABASE_SQLITE_PATH=str(tmp_path / "web.sqlite3"),
            )
        )
        assert settings.uses_database is True
        assert settings.database_url is None
        assert settings.database_sqlite_path == (tmp_path / "web.sqlite3").resolve()

    def test_production_requires_database_url(self):
        with pytest.raises(WebConfigError, match="DATABASE_URL"):
            WebSettings.from_env(
                env_base(
                    WEB_ENV="production",
                    EMAIL_BACKEND="resend",
                    RESEND_API_KEY="re_test",
                    PUBLIC_BASE_URL="https://example.com",
                )
            )

    def test_production_rejects_excel_backend(self, tmp_path):
        # 生产容器磁盘是临时的，Excel 模式会让重启丢掉全部数据。
        with pytest.raises(WebConfigError, match="WEB_STORAGE_BACKEND"):
            WebSettings.from_env(
                env_base(
                    WEB_ENV="production",
                    EMAIL_BACKEND="resend",
                    RESEND_API_KEY="re_test",
                    PUBLIC_BASE_URL="https://example.com",
                    WEB_STORAGE_BACKEND="excel",
                    WEB_DATA_ROOT=str(tmp_path / "users"),
                )
            )

    def test_production_requires_deepseek_key(self):
        with pytest.raises(WebConfigError, match="DEEPSEEK_API_KEY"):
            WebSettings.from_env(
                env_base(
                    WEB_ENV="production",
                    EMAIL_BACKEND="resend",
                    RESEND_API_KEY="re_test",
                    PUBLIC_BASE_URL="https://example.com",
                    DEEPSEEK_API_KEY="",
                    WEB_STORAGE_BACKEND="database",
                    DATABASE_URL="postgresql://user:pass@example.neon.tech/neondb",
                )
            )

    def test_unknown_backend_rejected(self):
        with pytest.raises(WebConfigError, match="WEB_STORAGE_BACKEND"):
            WebSettings.from_env(env_base(WEB_STORAGE_BACKEND="mysql"))

    def test_invalid_database_url_rejected(self):
        with pytest.raises(WebConfigError, match="DATABASE_URL"):
            WebSettings.from_env(
                env_base(
                    WEB_STORAGE_BACKEND="database",
                    DATABASE_URL="mysql://user:pass@example.com/db",
                )
            )

    def test_database_url_hidden_from_repr(self):
        url = "postgresql://user:super_secret@example.neon.tech/neondb"
        settings = WebSettings.from_env(
            env_base(WEB_STORAGE_BACKEND="database", DATABASE_URL=url)
        )
        text = repr(settings)
        assert "super_secret" not in text
        assert url not in text

    @pytest.mark.parametrize(
        "name,value",
        [
            ("DATABASE_POOL_SIZE", "0"),
            ("DATABASE_MAX_OVERFLOW", "-1"),
            ("DATABASE_POOL_TIMEOUT", "0"),
        ],
    )
    def test_pool_limits(self, name, value):
        with pytest.raises(WebConfigError, match=name):
            WebSettings.from_env(
                env_base(WEB_STORAGE_BACKEND="database", **{name: value})
            )


class TestSecretHandling:
    def test_missing_session_secret(self):
        env = env_base()
        env.pop("WEB_SESSION_SECRET")
        with pytest.raises(WebConfigError, match="WEB_SESSION_SECRET"):
            WebSettings.from_env(env)

    def test_short_session_secret(self):
        with pytest.raises(WebConfigError, match="至少需要 32"):
            WebSettings.from_env(env_base(WEB_SESSION_SECRET="short"))

    def test_missing_owner_email(self):
        env = env_base()
        env.pop("OWNER_EMAIL")
        with pytest.raises(WebConfigError, match="OWNER_EMAIL"):
            WebSettings.from_env(env)

    def test_invalid_owner_email(self):
        with pytest.raises(WebConfigError, match="OWNER_EMAIL"):
            WebSettings.from_env(env_base(OWNER_EMAIL="not-an-email"))

    def test_repr_hides_secrets(self, tmp_path):
        settings = WebSettings.from_env(
            env_base(
                EMAIL_BACKEND="resend",
                RESEND_API_KEY="re_super_secret_value",
                PUBLIC_BASE_URL="https://example.com",
                WEB_DATA_ROOT=str(tmp_path / "users"),
                WEB_STATE_DB=str(tmp_path / "state.sqlite3"),
            )
        )
        text = repr(settings)
        assert settings.session_secret not in text
        assert "re_super_secret_value" not in text


class TestEmailBackend:
    def test_unknown_backend(self):
        with pytest.raises(WebConfigError, match="EMAIL_BACKEND"):
            WebSettings.from_env(env_base(EMAIL_BACKEND="smtp"))

    def test_resend_requires_key(self):
        with pytest.raises(WebConfigError, match="RESEND_API_KEY"):
            WebSettings.from_env(
                env_base(
                    EMAIL_BACKEND="resend",
                    PUBLIC_BASE_URL="https://example.com",
                )
            )

    def test_production_rejects_console(self):
        with pytest.raises(WebConfigError, match="不能使用 EMAIL_BACKEND=console"):
            WebSettings.from_env(
                env_base(
                    WEB_ENV="production",
                    PUBLIC_BASE_URL="https://example.com",
                )
            )


class TestPublicBaseUrl:
    def test_production_requires_public_url(self):
        with pytest.raises(WebConfigError, match="PUBLIC_BASE_URL"):
            WebSettings.from_env(
                env_base(
                    WEB_ENV="production",
                    EMAIL_BACKEND="resend",
                    RESEND_API_KEY="re_test",
                )
            )

    def test_production_rejects_http(self):
        with pytest.raises(WebConfigError, match="https"):
            WebSettings.from_env(
                env_base(
                    WEB_ENV="production",
                    EMAIL_BACKEND="resend",
                    RESEND_API_KEY="re_test",
                    PUBLIC_BASE_URL="http://example.com",
                )
            )

    @pytest.mark.parametrize(
        "url",
        [
            "ftp://example.com",
            "https://user:pass@example.com",
            "https://example.com?x=1",
            "https://example.com/#frag",
            "://bad",
            "https://",
        ],
    )
    def test_invalid_urls(self, url):
        with pytest.raises(WebConfigError, match="PUBLIC_BASE_URL"):
            WebSettings.from_env(env_base(PUBLIC_BASE_URL=url))


class TestNumericLimits:
    @pytest.mark.parametrize(
        "name,value",
        [
            ("WEB_MAX_INPUT_CHARS", "0"),
            ("WEB_MAX_INPUT_CHARS", "abc"),
            ("WEB_MAX_INPUT_CHARS", "999999"),
            ("WEB_MAX_BODY_BYTES", "10"),
            ("WEB_MAX_HISTORY_MESSAGES", "1"),
            ("WEB_MAX_CONCURRENT_TURNS", "0"),
            ("WEB_MODEL_MAX_TOKENS", "100000"),
            ("WEB_MODEL_TIMEOUT_SECONDS", "nan"),
            ("WEB_MODEL_TIMEOUT_SECONDS", "1"),
            ("WEB_ACCESS_HOURS", "0"),
            ("WEB_APPROVAL_TTL_HOURS", "-1"),
            ("WEB_REQUEST_COOLDOWN_MINUTES", "-1"),
        ],
    )
    def test_out_of_range(self, name, value):
        with pytest.raises(WebConfigError, match=name):
            WebSettings.from_env(env_base(**{name: value}))

    def test_zero_disables_request_limits(self):
        settings = WebSettings.from_env(
            env_base(
                WEB_REQUEST_COOLDOWN_MINUTES="0",
                WEB_REQUESTS_PER_IP_PER_HOUR="0",
                WEB_REQUESTS_GLOBAL_PER_DAY="0",
            )
        )
        assert settings.request_cooldown_minutes == 0
        assert settings.requests_per_ip_per_hour == 0
        assert settings.requests_global_per_day == 0

    def test_invalid_env(self):
        with pytest.raises(WebConfigError, match="WEB_ENV"):
            WebSettings.from_env(env_base(WEB_ENV="staging"))


class TestImportAndDMConfig:
    @pytest.mark.parametrize(
        "name,value",
        [
            ("WEB_MAX_IMPORT_BYTES", "1024"),
            ("WEB_MAX_IMPORT_BYTES", "abc"),
            ("WEB_MAX_IMPORT_BYTES", "11534336"),
            ("DM_CODE_TTL_MINUTES", "4"),
            ("DM_CODE_TTL_MINUTES", "121"),
            ("DM_TOKEN_HOURS", "0"),
            ("DM_TOKEN_HOURS", "25"),
            ("HARDWARE_TIMEOUT_SECONDS", "0.4"),
            ("HARDWARE_TIMEOUT_SECONDS", "5.5"),
            ("HARDWARE_TIMEOUT_SECONDS", "nan"),
        ],
    )
    def test_out_of_range(self, name, value):
        with pytest.raises(WebConfigError, match=name):
            WebSettings.from_env(env_base(**{name: value}))

    def test_values_are_parsed(self):
        settings = WebSettings.from_env(
            env_base(
                WEB_MAX_IMPORT_BYTES="1048576",
                DM_CODE_TTL_MINUTES="45",
                DM_TOKEN_HOURS="8",
                HARDWARE_TIMEOUT_SECONDS="1.5",
            )
        )
        assert settings.max_import_bytes == 1024 * 1024
        assert settings.dm_code_ttl_minutes == 45
        assert settings.dm_token_hours == 8
        assert settings.hardware_timeout_seconds == 1.5


def _production_env(**overrides) -> dict[str, str]:
    env = env_base(
        WEB_ENV="production",
        EMAIL_BACKEND="resend",
        RESEND_API_KEY="re_test",
        PUBLIC_BASE_URL="https://example.com",
        DATABASE_URL="postgresql://user:pass@example.neon.tech/neondb",
    )
    env.update(overrides)
    return env


class TestHardwareConfig:
    def test_local_http_url_is_allowed_in_development(self):
        settings = WebSettings.from_env(
            env_base(HARDWARE_SKILL_URL="http://127.0.0.1:9100/scene")
        )
        assert settings.hardware_skill_url == "http://127.0.0.1:9100/scene"
        assert settings.hardware_skill_token is None

    def test_remote_https_url_with_token(self):
        settings = WebSettings.from_env(
            env_base(
                HARDWARE_SKILL_URL="https://hardware.example.com/webhook",
                HARDWARE_SKILL_TOKEN="hw_secret_token",
            )
        )
        assert settings.hardware_skill_url == "https://hardware.example.com/webhook"
        assert settings.hardware_skill_token == "hw_secret_token"

    @pytest.mark.parametrize(
        "url",
        [
            "ftp://hardware.example.com/hook",
            "https://user:pass@hardware.example.com/hook",
            "http://hardware.example.com/hook",
            "not-a-url",
            "https://",
            "https://hardware.example.com:bad/hook",
        ],
    )
    def test_invalid_urls_rejected(self, url):
        with pytest.raises(WebConfigError, match="HARDWARE_SKILL_URL"):
            WebSettings.from_env(env_base(HARDWARE_SKILL_URL=url))

    def test_production_requires_https_even_for_localhost(self):
        with pytest.raises(WebConfigError, match="HARDWARE_SKILL_URL"):
            WebSettings.from_env(
                _production_env(HARDWARE_SKILL_URL="http://127.0.0.1:9100/scene")
            )

    def test_token_hidden_from_repr(self):
        settings = WebSettings.from_env(
            env_base(
                HARDWARE_SKILL_URL="https://hardware.example.com/hook",
                HARDWARE_SKILL_TOKEN="hw_super_secret_value",
            )
        )
        text = repr(settings)
        assert "hw_super_secret_value" not in text
        assert "hardware.example.com" in text

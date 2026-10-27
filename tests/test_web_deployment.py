"""部署文件的静态检查：容器、Render、环境变量与文档。

本机没有 Docker，这些测试不构建镜像，只验证配置中容易写错、
又不容易在本地暴露的约束：单 worker、非 root、端口、健康检查、
密钥占位与 README 的部署状态表述。
"""

from __future__ import annotations

import re
from pathlib import Path

from app.config import PROJECT_ROOT

README = PROJECT_ROOT / "README.md"
DOCKERFILE = PROJECT_ROOT / "Dockerfile"
DOCKERIGNORE = PROJECT_ROOT / ".dockerignore"
RENDER = PROJECT_ROOT / "render.yaml"
ENV_EXAMPLE = PROJECT_ROOT / ".env.example"
GITIGNORE = PROJECT_ROOT / ".gitignore"
REQUIREMENTS = PROJECT_ROOT / "requirements.txt"
HF_SPACE_README = PROJECT_ROOT / "deploy" / "huggingface" / "README.md"
HF_PUBLISH_SCRIPT = PROJECT_ROOT / "scripts" / "publish_hf_space.py"
VPS_COMPOSE = PROJECT_ROOT / "deploy" / "vps" / "docker-compose.yml"
VPS_CADDYFILE = PROJECT_ROOT / "deploy" / "vps" / "Caddyfile"
CLOUDFLARE_COMPOSE = PROJECT_ROOT / "deploy" / "cloudflare" / "docker-compose.yml"
CLOUDFLARE_README = PROJECT_ROOT / "deploy" / "cloudflare" / "README.md"

# 真实 DeepSeek / Resend key 的随机主体远长于测试里的演示串；
# 阈值取 24，避免把 sk-demo123456 这类故意保留的脱敏样例误报。
SECRET_LIKE = re.compile(r"\b(?:sk-|re_)[A-Za-z0-9_-]{24,}\b")


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _without_comments(text: str) -> str:
    """去掉整行注释，避免说明文字里的「ports:」被误判成真实配置。"""
    return "\n".join(
        line for line in text.splitlines() if not line.strip().startswith("#")
    )


def test_dockerfile_runs_single_non_root_worker():
    text = _read(DOCKERFILE)

    assert "FROM python:3.13-slim" in text
    # Hugging Face Spaces 要求容器以 UID 1000 运行；同时保持非 root。
    assert "useradd" in text and "--uid 1000" in text
    assert "USER user" in text
    # WORKDIR 由 root 创建，必须改回 user，否则 UID 1000 启动时建不了 data/。
    assert "chown -R user:user /home/user" in text
    assert text.index("USER user") > text.index("chown -R user:user")
    assert "--factory" in text
    assert "--workers 1" in text
    assert "--no-access-log" in text
    assert not re.search(r"--workers\s+[2-9]", text)
    assert "${PORT:-8000}" in text
    assert "/healthz" in text
    assert "app.web.main:create_app" in text
    assert "HEALTHCHECK" in text
    # 大陆机器直连 pypi.org 会慢到超时，构建时必须能换源。
    assert "ARG PIP_INDEX_URL" in text
    assert '--index-url "${PIP_INDEX_URL}"' in text
    # 健康检查也必须跟随平台注入的端口，不能写死 8000。
    healthcheck = text.split("HEALTHCHECK", 1)[1]
    assert "PORT" in healthcheck


def test_dockerignore_excludes_secrets_and_runtime_data():
    entries = {
        line.strip()
        for line in _read(DOCKERIGNORE).splitlines()
        if line.strip() and not line.strip().startswith("#")
    }

    assert ".env" in entries
    assert "data/" in entries
    assert "tests/" in entries
    assert "scripts/" in entries
    assert "*.xlsx" in entries
    assert "*.sqlite3" in entries
    assert "*.tmp" in entries
    # Flutter 只做客户端源码，不进入 Web 镜像。
    for entry in (
        ".claude/",
        "skills/",
        "mobile_app/",
        "**/.dart_tool/",
        "**/build/",
        "*.apk",
        "*.ipa",
    ):
        assert entry in entries, entry


def test_render_blueprint_matches_app_and_uses_placeholders():
    text = _read(RENDER)

    assert "env: docker" in text
    assert "dockerfilePath: ./Dockerfile" in text
    assert "healthCheckPath: /healthz" in text
    assert "plan: free" in text
    for key in (
        "WEB_ENV",
        "EMAIL_BACKEND",
        "EMAIL_FROM",
        "WEB_DEFAULT_STORE",
        "WEB_SESSION_SECRET",
        "OWNER_EMAIL",
        "PUBLIC_BASE_URL",
        "DEEPSEEK_API_KEY",
        "RESEND_API_KEY",
        "WEB_STORAGE_BACKEND",
        "DATABASE_URL",
    ):
        assert f"key: {key}" in text, key
    assert "value: production" in text
    assert "value: resend" in text
    assert "value: database" in text
    assert "generateValue: true" in text
    assert text.count("sync: false") >= 6
    assert "免费层磁盘是临时的" in text
    assert "WEB_DATA_ROOT" not in text
    assert "WEB_STATE_DB" not in text
    # 场控与导入默认值写进 blueprint；硬件凭证必须由控制台填写。
    for key, value in (
        ("WEB_MAX_IMPORT_BYTES", "2097152"),
        ("DM_CODE_TTL_MINUTES", "30"),
        ("DM_TOKEN_HOURS", "12"),
        ("HARDWARE_TIMEOUT_SECONDS", "2"),
    ):
        assert f"key: {key}" in text, key
        assert f'value: "{value}"' in text, key
    for key in ("HARDWARE_SKILL_URL", "HARDWARE_SKILL_TOKEN"):
        block = text.split(f"key: {key}", 1)[1].split("- key:", 1)[0]
        assert "sync: false" in block, key


def test_env_example_documents_web_without_real_secrets():
    text = _read(ENV_EXAMPLE)

    for name in (
        "DEEPSEEK_API_KEY",
        "WEB_SESSION_SECRET",
        "OWNER_EMAIL",
        "RESEND_API_KEY",
        "PUBLIC_BASE_URL",
        "WEB_DATA_ROOT",
        "WEB_STATE_DB",
        "WEB_DEFAULT_STORE",
        "WEB_STORAGE_BACKEND",
        "WEB_DATABASE_SQLITE_PATH",
    ):
        assert re.search(rf"^{name}=", text, re.MULTILINE), name
    assert "EMAIL_BACKEND=console" in text
    assert "EMAIL_FROM=onboarding@resend.dev" in text
    assert "WEB_ENV=development" in text
    assert "WEB_STORAGE_BACKEND=excel" in text
    assert "# DATABASE_URL=" in text

    for name in ("DEEPSEEK_API_KEY", "WEB_SESSION_SECRET", "OWNER_EMAIL", "RESEND_API_KEY"):
        assert re.search(rf"^{name}=\s*$", text, re.MULTILINE), name
    for name in (
        "WEB_MAX_IMPORT_BYTES",
        "DM_CODE_TTL_MINUTES",
        "DM_TOKEN_HOURS",
        "HARDWARE_SKILL_URL",
        "HARDWARE_SKILL_TOKEN",
        "HARDWARE_TIMEOUT_SECONDS",
    ):
        assert re.search(rf"^#?\s*{name}=", text, re.MULTILINE), name
    assert not SECRET_LIKE.search(text)


def test_gitignore_excludes_env_and_web_user_data():
    text = _read(GITIGNORE)

    assert re.search(r"^\.env$", text, re.MULTILINE)
    # 本地数据整体忽略：公共库采集结果、sqlite、临时文件都不入库；
    # 只保留体积很小的策展种子，便于新克隆复现。
    assert "data/*" in text
    assert "!data/public_scripts_seed.csv" in text
    assert "backups/" in text
    assert re.search(r"^\*\.log$", text, re.MULTILINE)
    for entry in (
        "mobile_app/.dart_tool/",
        "mobile_app/build/",
        "mobile_app/android/.gradle/",
        "mobile_app/android/build/",
        "mobile_app/android/local.properties",
        "mobile_app/ios/Pods/",
        "*.apk",
        "*.ipa",
    ):
        assert entry in text, entry
    # 保留锁文件，保证可复现构建。
    assert "mobile_app/pubspec.lock" not in text


def test_web_dependencies_are_pinned():
    lines = {
        line.strip()
        for line in _read(REQUIREMENTS).splitlines()
        if line.strip() and not line.strip().startswith("#")
    }
    for package in ("fastapi", "uvicorn", "jinja2", "python-multipart", "itsdangerous", "httpx"):
        matches = [line for line in lines if line.lower().startswith(f"{package}==")]
        assert len(matches) == 1, package


def test_readme_documents_web_honestly():
    text = _read(README)

    assert "代码已实现，尚未公网部署" in text
    assert "当前没有公网实例" in text
    assert "尚未实际公网部署" in text
    assert "Neon 项目 `shiyuan`（分支 `production`）已创建" in text
    assert "生产业务数据尚未正式导入" in text
    assert "注册 Resend" in text
    assert "8 小时" in text
    assert "EMAIL_BACKEND=resend" in text
    assert "onboarding@resend.dev" in text
    assert "每邮箱数据隔离" in text
    assert "磁盘普遍是临时的" in text
    assert "1 个 Uvicorn worker" in text
    assert "本轮不做" in text
    # 数据库迁移的部署说明必须保留，避免 README 回退成纯 Excel 版本。
    assert "Neon" in text
    assert "DATABASE_URL" in text
    assert "WEB_STORAGE_BACKEND" in text
    assert "对话记忆不持久" in text
    assert "python -m app.store.migrate" in text
    assert "python -m app.store.export" in text


def test_readme_documents_new_operations_honestly():
    """README 必须介绍新能力，同时如实标注真实状态：已构建 debug APK、未做 release 签名、未真机联调、未上架、未公网部署。"""
    text = _read(README)

    for phrase in (
        "12 个业务工具",
        "Flutter 双角色客户端",
        "增量录入",
        "大众点评导出文件导入",
        "互补，而非替代",
        "DM 拥有阶段最高控制权",
        "倒计时只提醒，绝不推进",
        # Flutter：SDK 已装、analyze/test 已跑，且 2026-10-22 已构建 debug APK；
        # README 必须如实写成“已构建 debug、未签名、未上架、未联调”。
        "Android debug APK 已构建",
        "未做 release 签名、未真机联调、尚未上架",
        "82 项全部通过",
        "当前没有公网实例",
        "通用 HTTP 适配器契约",
        "未对接任何具体灯光 / 音效厂商",
        "不抓取公开页",
        "不承诺实时同步",
        "不做微信小程序",
        "十二个 xlsx",
        # 2026-10-22 新能力的诚实边界也必须写进 README。
        "公共剧本库",
        "not_configured",
        "绝不根据搜索结果自动写入剧本库",
        "Web 端诚实降级",
        "不做成服务端常驻任务",
        "场次永不指向公共行",
        "未真机联调（DM 端 10 步测试清单",
    ):
        assert phrase in text, phrase

    # 不能把设计/未验证写成已完成。
    assert "已上架" not in text
    assert "客户端已跑通" not in text
    assert "已部署到 Render" not in text
    # 反向：旧的过时表述（SDK 未装、未构建）一旦回潮也必须被抓住。
    assert "未安装 Android SDK" not in text
    assert "未执行 `flutter build apk`" not in text
    assert "已构建 APK" not in text
    assert "已真机联调" not in text


def test_huggingface_space_card_matches_dockerfile():
    """Space 卡片的 SDK 与端口必须和 Dockerfile 对齐，否则 HF 连不上应用。"""
    text = _read(HF_SPACE_README)

    assert text.startswith("---\n")
    assert "sdk: docker" in text
    assert "app_port: 8000" in text
    assert not SECRET_LIKE.search(text)


def test_publish_script_only_ships_runtime_files():
    """公开 Space 只能收到运行所需文件，不能把 data/、tests/、.env 推上去。"""
    text = _read(HF_PUBLISH_SCRIPT)

    assert 'COPY_DIRS = ("app",)' in text
    assert 'COPY_FILES = ("requirements.txt", "Dockerfile")' in text
    assert 'os.environ.get("HF_TOKEN"' in text
    assert "SPACE_README" in text
    assert not SECRET_LIKE.search(text)


def test_vps_compose_only_exposes_caddy():
    """应用端口不能直接对公网开放，只有 80/443 由 Caddy 承担。"""
    text = _read(VPS_COMPOSE)

    assert "80:80" in text and "443:443" in text
    assert "restart: unless-stopped" in text
    web_block = text.split("web:", 1)[1].split("caddy:", 1)[0]
    assert "expose:" in web_block and '"8000"' in web_block
    assert "ports:" not in web_block
    assert "SITE_ADDRESS" in text
    assert "PIP_INDEX_URL: ${PIP_INDEX_URL:-https://pypi.org/simple}" in text


def test_vps_caddyfile_keeps_access_logs_off():
    """审批与备用链接的 token 在 URL 里，Caddy 不能启用访问日志。"""
    text = _read(VPS_CADDYFILE)

    assert "reverse_proxy web:8000" in text
    assert "{$SITE_ADDRESS}" in text
    assert re.search(r"^\s*log(\s|$)", text, re.MULTILINE) is None


def test_cloudflare_tunnel_compose_publishes_no_ports():
    """隧道是出站连接：宿主机不能发布任何端口，否则应用会绕过隧道暴露到公网。"""
    text = _read(CLOUDFLARE_COMPOSE)
    body = _without_comments(text)

    assert "cloudflare/cloudflared" in body
    assert "tunnel" in body and "run" in body
    assert "TUNNEL_TOKEN" in body
    assert "expose:" in body and '"8000"' in body
    # Docker 发布端口会直接写 iptables、绕过 ufw，这里必须一个都没有。
    assert "ports:" not in body
    assert body.count("restart: unless-stopped") == 2
    # 大陆网络对 QUIC 不稳，默认走 http2。
    assert "--protocol http2" in body
    # TLS 由 Cloudflare 边缘终结，不需要 Caddy。
    assert "caddy" not in body.lower()
    assert "PIP_INDEX_URL: ${PIP_INDEX_URL:-https://pypi.org/simple}" in body


def test_cloudflare_tunnel_readme_documents_origin():
    """Public Hostname 配在 Cloudflare 控制台，重建隧道会丢，必须写进仓库备查。"""
    text = _read(CLOUDFLARE_README)

    assert "http://web:8000" in text
    assert "demo.ghtrending.cyou" in text
    assert "TUNNEL_TOKEN" in text
    assert "--workers 1" in text
    assert not SECRET_LIKE.search(text)


def test_repository_contains_no_live_secret_shapes():
    """源码、测试、模板和文档中不应出现真实的密钥形态。"""
    roots = [PROJECT_ROOT / "app", PROJECT_ROOT / "tests", PROJECT_ROOT / "scripts"]
    files: list[Path] = []
    for root in roots:
        files.extend(
            path
            for path in root.rglob("*")
            if path.is_file() and path.suffix in {".py", ".html", ".css", ".js", ".md"}
        )
    files.extend([README, DOCKERFILE, RENDER, ENV_EXAMPLE])

    for path in files:
        match = SECRET_LIKE.search(_read(path))
        assert match is None, f"{path.relative_to(PROJECT_ROOT)} 疑似包含明文密钥"

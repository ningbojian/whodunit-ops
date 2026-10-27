"""把当前仓库同步到一个 Hugging Face Space，并触发 Space 重建。

用法（在项目根目录）：

    # PowerShell
    $env:HF_TOKEN = "hf_xxx"
    $env:HF_SPACE = "你的用户名/space名"
    python scripts/publish_hf_space.py

    # bash
    HF_TOKEN=hf_xxx HF_SPACE=你的用户名/space名 python scripts/publish_hf_space.py

前置条件：Space 已在 https://huggingface.co/new-space 创建，SDK 选 Docker，模板选 Blank，
并且是 Public（免费账号的 Space 必须是公开的）。

HF_TOKEN 只从环境变量读取：不写文件、不进命令行参数、不打印。
Space 里只会放运行所需的内容（app/、requirements.txt、Dockerfile 和 Space 卡片），
不会同步 tests/、data/、docs/ 或 .env。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
COPY_DIRS = ("app",)
COPY_FILES = ("requirements.txt", "Dockerfile")
SPACE_README = ROOT / "deploy" / "huggingface" / "README.md"
IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache")


def run(cmd: list[str], *, cwd: Path | None = None, env: dict | None = None) -> subprocess.CompletedProcess:
    result = subprocess.run(cmd, cwd=cwd, env=env, text=True, capture_output=True)
    if result.returncode != 0:
        sys.stderr.write(result.stdout)
        sys.stderr.write(result.stderr)
        # 命令本身不含 token，可以安全回显。
        raise SystemExit(f"命令失败：{' '.join(cmd[:3])}")
    return result


def main() -> int:
    token = os.environ.get("HF_TOKEN", "").strip()
    space = os.environ.get("HF_SPACE", "").strip()
    if not token or not space:
        print("需要环境变量 HF_TOKEN 与 HF_SPACE（格式：用户名/space名）", file=sys.stderr)
        return 2
    if "/" not in space:
        print("HF_SPACE 格式应为「用户名/space名」", file=sys.stderr)
        return 2
    if not SPACE_README.exists():
        print(f"缺少 Space 卡片：{SPACE_README}", file=sys.stderr)
        return 2

    space_url = f"https://huggingface.co/spaces/{space}"

    with tempfile.TemporaryDirectory(prefix="hf-space-") as tmp:
        target = Path(tmp)

        try:
            run(["git", "clone", "--depth", "1", space_url, str(target)])
        except SystemExit:
            print(
                f"无法克隆 {space_url}。请先在 Hugging Face 上创建这个 Space"
                "（SDK 选 Docker、模板选 Blank），再重试。",
                file=sys.stderr,
            )
            return 2

        # 清空除 .git 之外的内容，避免旧文件残留。
        for item in target.iterdir():
            if item.name == ".git":
                continue
            if item.is_dir():
                shutil.rmtree(item)
            else:
                item.unlink()

        for name in COPY_DIRS:
            shutil.copytree(ROOT / name, target / name, ignore=IGNORE)
        for name in COPY_FILES:
            shutil.copy2(ROOT / name, target / name)
        shutil.copy2(SPACE_README, target / "README.md")

        run(["git", "add", "-A"], cwd=target)
        commit = subprocess.run(
            [
                "git",
                "-c", "user.name=whodunit-sync",
                "-c", "user.email=sync@users.noreply.github.com",
                "commit", "-m", "sync from GitHub",
            ],
            cwd=target,
            text=True,
            capture_output=True,
        )
        if commit.returncode != 0 and "nothing to commit" not in commit.stdout:
            sys.stderr.write(commit.stderr)
            raise SystemExit("提交失败")

        # 通过 GIT_CONFIG_* 传认证头，token 不进入 argv / 不写进 .git/config。
        env = os.environ.copy()
        env["GIT_CONFIG_COUNT"] = "1"
        env["GIT_CONFIG_KEY_0"] = "http.https://huggingface.co/.extraheader"
        env["GIT_CONFIG_VALUE_0"] = f"Authorization: Bearer {token}"
        run(["git", "push", "origin", "HEAD:main"], cwd=target, env=env)

    print(f"已推送：{space_url}")
    print("Space 会自动重建镜像，进度见该页面的「Logs」标签。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""全局配置：读取 .env，提供统一的模型实例与路径常量。

为什么要单独抽一个模块：
  1. 后续工具 + 最终 Agent 都要用 model，集中一处，
     将来从 DeepSeek 换到通义千问只需改这一行常量。
  2. 路径统一用「项目根目录」为基准，避免从不同目录运行时找不到文件。
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# 路径
# ---------------------------------------------------------------------------

# app/config.py -> app/ -> 项目根目录
PROJECT_ROOT = Path(__file__).resolve().parent.parent

DATA_DIR = PROJECT_ROOT / "data"
SESSIONS_XLSX = DATA_DIR / "sessions.xlsx"  # 场次表：有哪些场、什么时间、几位
BOOKINGS_XLSX = DATA_DIR / "bookings.xlsx"  # 预约明细表：谁约了哪一场（工具6 使用）
SCRIPTS_XLSX = DATA_DIR / "scripts.xlsx"    # 剧本库：剧本档案、别名、类型/人数/时长

# 资源、排期、增量录入、DM 场控的 sidecar。Excel 模式与三张主表同目录。
ROOMS_XLSX = DATA_DIR / "rooms.xlsx"
DMS_XLSX = DATA_DIR / "dms.xlsx"
DM_SHIFTS_XLSX = DATA_DIR / "dm_shifts.xlsx"
DM_LEAVES_XLSX = DATA_DIR / "dm_leaves.xlsx"
ASSIGNMENTS_XLSX = DATA_DIR / "assignments.xlsx"
BOOKING_INTAKE_REFS_XLSX = DATA_DIR / "booking_intake_refs.xlsx"
SCRIPT_STAGES_XLSX = DATA_DIR / "script_stages.xlsx"
DM_RUNS_XLSX = DATA_DIR / "dm_runs.xlsx"
DM_RUN_EVENTS_XLSX = DATA_DIR / "dm_run_events.xlsx"

# 公共剧本库（全局共享，无租户）：Excel 模式落 data 根目录；
# 种子 CSV 随仓库提交（.gitignore 只忽略 data/*.xlsx，不忽略 csv）。
PUBLIC_SCRIPTS_XLSX = DATA_DIR / "public_scripts.xlsx"
PUBLIC_SEED_CSV = DATA_DIR / "public_scripts_seed.csv"

# ---------------------------------------------------------------------------
# 环境变量
# ---------------------------------------------------------------------------

# 显式指定 .env 的绝对路径：
# 否则 load_dotenv() 会沿着「当前工作目录」向上找，
# 从 app/ 运行和从项目根运行的结果会不一样。
load_dotenv(PROJECT_ROOT / ".env")


def _env_int(name: str, default: int, lo: int, hi: int) -> int:
    """读环境变量整数并夹取到 [lo, hi]；缺失或非法用默认值。"""
    raw = os.getenv(name, "")
    try:
        value = int(str(raw).strip() or default)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, value))


# web_search 联网搜索的多租户成本闸门（0 = 不限；细节见 integrations/web_search.py）。
# 必须在 load_dotenv 之后解析，.env 里的值才生效；范围夹取防止误配放大成本。
WEB_SEARCH_TENANT_DAILY_LIMIT = _env_int("WEB_SEARCH_TENANT_DAILY_LIMIT", 10, 0, 100)
WEB_SEARCH_GLOBAL_DAILY_LIMIT = _env_int("WEB_SEARCH_GLOBAL_DAILY_LIMIT", 30, 0, 500)


def get_api_key() -> str:
    """读取 DeepSeek API key。

    缺失时抛出带操作指引的异常，而不是让下游报一个难懂的 KeyError。
    """
    key = os.getenv("DEEPSEEK_API_KEY", "").strip()
    if not key:
        raise RuntimeError(
            "未找到 DEEPSEEK_API_KEY。\n"
            f"  1. 把 {PROJECT_ROOT / '.env.example'} 复制为 .env\n"
            "  2. 在 .env 中填入 DEEPSEEK_API_KEY=sk-xxxx\n"
            "  申请地址：https://platform.deepseek.com/api_keys"
        )
    return key


# ---------------------------------------------------------------------------
# 模型
# ---------------------------------------------------------------------------

# 本项目统一使用 DeepSeek 的对话模型。
# 换后端时改这一行即可（例如 "qwen-plus"、"glm-4"）。
DEEPSEEK_MODEL = "deepseek:deepseek-chat"


def get_model(
    *,
    temperature: float = 0.0,
    max_tokens: int | None = None,
    timeout: float | None = None,
    max_retries: int | None = None,
):
    """返回一个已配置好的 chat model。

    temperature 默认 0：
      - 后面的「文本抽取工具」要求同一段文案每次抽出同样的结果，必须可复现；
      - 「营销文案生成工具」需要发散创意时，再单独传更高的值。

    max_tokens / timeout / max_retries 是网页版的可选安全上限。
    默认 None 表示不传，保持命令行版原有行为完全不变；
    只有调用方显式传入时才写进 init_chat_model 的参数。

    若 init_chat_model 不认识 "deepseek:" 前缀，退回到直接实例化：
        from langchain_deepseek import ChatDeepSeek
        return ChatDeepSeek(model="deepseek-chat", api_key=get_api_key(),
                            temperature=temperature)
    """
    from langchain.chat_models import init_chat_model

    kwargs: dict = {"temperature": temperature}
    if max_tokens is not None:
        kwargs["max_tokens"] = int(max_tokens)
    if timeout is not None:
        kwargs["timeout"] = float(timeout)
    if max_retries is not None:
        kwargs["max_retries"] = int(max_retries)

    return init_chat_model(
        DEEPSEEK_MODEL,
        api_key=get_api_key(),
        **kwargs,
    )

"""LLM 调用适配层：把「模型实例」和「回复文本」这两件事收在一处。

为什么单独抽一层：

  1. 工具 1（文本抽取）和工具 7（营销文案）都要调用模型。两处各写一遍
     `response.content` 的取值逻辑，迟早出现「一边支持 content blocks、
     另一边只认字符串」的漂移。
  2. 测试不联网。业务代码只依赖「有 .invoke(messages) 方法的对象」，
     测试注入 FakeLLM 即可，不需要 API key、不花钱、毫秒级。
  3. **本模块导入时不读 key**。`get_default_model()` 被调用时才去
     找 `app.config.get_api_key()` —— 否则只 import 一下 extract.py
     就会因为没配 .env 而失败，单元测试根本没法收集。

这个文件刻意不出现任何 LangChain 的业务封装：
  - 不继承 Runnable；
  - 不用 with_structured_output；
  - 只做「拿模型」和「把回复变成字符串」两件小事。
业务层的字段校验、JSON 定位、失败降级都在 app/tools/extract.py，
这样它们可以用纯函数测透。
"""

from __future__ import annotations

import re
from typing import Any

# API key 的形状：sk- 开头，后面是一串字母数字。错误信息里如果粘到了 key，
# 用它脱敏后再交给调用方 / 打进日志。
_SECRET_RE = re.compile(r"sk-[A-Za-z0-9_\-]{4,}")


def get_default_model(
    *,
    temperature: float = 0.0,
    max_tokens: int | None = None,
    timeout: float | None = None,
    max_retries: int | None = None,
):
    """返回项目默认的 chat model。

    延迟 import `app.config`：
      config 在模块级会 load_dotenv（读 .env 文件），而本模块的测试只关心
      「回复怎么取值」。把 import 放进函数里，测试 monkeypatch 这个函数
      就能完全绕开配置，也不会在 import 阶段碰 .env。

    可选参数只在非 None 时透传，保证原有调用路径（控制台 / 工具默认模型）
    的行为和参数完全不变。
    """
    from app.config import get_model

    kwargs: dict = {"temperature": temperature}
    if max_tokens is not None:
        kwargs["max_tokens"] = max_tokens
    if timeout is not None:
        kwargs["timeout"] = timeout
    if max_retries is not None:
        kwargs["max_retries"] = max_retries
    return get_model(**kwargs)


def message_text(response: Any) -> str:
    """把模型回复统一变成字符串。

    兼容三种常见形态：
      - 普通字符串（FakeLLM 的返回值、部分模型的 content）；
      - LangChain 的 AIMessage / 任何带 .content 的对象；
      - content blocks：[{"type": "text", "text": "..."}, ...]（多模态模型）。

    取不到文本时返回空字符串，由上层决定这是失败还是空结果。
    """
    if response is None:
        return ""
    if isinstance(response, str):
        return response

    content = getattr(response, "content", response)
    return _content_text(content)


def _content_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content

    if isinstance(content, (list, tuple)):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
                continue
            if isinstance(block, dict):
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
                continue
            text = getattr(block, "text", None)
            if isinstance(text, str):
                parts.append(text)
        return "".join(parts)

    # 兜底：至少别把异常吞掉，让上层看到内容的字符串形态。
    return str(content)


def invoke_text(model: Any, messages: list[dict[str, str]]) -> str:
    """调用模型并取出文本。

    只依赖 `.invoke(messages)` 这一个方法：
      LangChain 的 chat model、OpenAI 兼容客户端、测试用的 FakeLLM
      都满足这个接口。调用失败由上层（extract_message）捕获并转成
     结构化错误，不在这里吞异常 —— 这一层不知道业务上该怎么降级。
    """
    return message_text(model.invoke(messages))


def redact_secrets(text: Any) -> str:
    """把文本里的 sk-... 替换成 sk-***，避免错误信息泄露 key。"""
    if text is None:
        return ""
    return _SECRET_RE.sub("sk-***", str(text))

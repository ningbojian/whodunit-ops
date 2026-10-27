"""LLM 适配层 —— 单元测试。

运行：
  PYTHONUTF8=1 "C:/Users/Lenovo/anaconda3/python.exe" -m pytest tests/test_llm.py -v

不联网、不需要 API key。所有测试都在验证「回复怎么变成字符串」和
「模型实例怎么拿到」，不涉及任何业务 Prompt。
"""

from __future__ import annotations

from types import SimpleNamespace

from app import llm


class FakeModel:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def invoke(self, messages):
        self.calls.append(messages)
        return self.response


# ===========================================================================
# 一、message_text
# ===========================================================================

def test_字符串原样返回():
    assert llm.message_text("hello") == "hello"


def test_没有内容时返回空串():
    assert llm.message_text(None) == ""
    assert llm.message_text(SimpleNamespace(content=None)) == ""


def test_读取对象的content属性():
    response = SimpleNamespace(content="模型回复")
    assert llm.message_text(response) == "模型回复"


def test_拼接content_blocks():
    response = SimpleNamespace(content=[
        {"type": "text", "text": "第一段"},
        {"type": "text", "text": "第二段"},
    ])
    assert llm.message_text(response) == "第一段第二段"


def test_content_blocks支持对象块():
    block = SimpleNamespace(text="对象块")
    response = SimpleNamespace(content=[block, "尾巴"])
    assert llm.message_text(response) == "对象块尾巴"


# ===========================================================================
# 二、invoke_text
# ===========================================================================

def test_invoke_text把messages原样传给模型():
    model = FakeModel(SimpleNamespace(content="ok"))
    messages = [{"role": "user", "content": "你好"}]

    assert llm.invoke_text(model, messages) == "ok"
    assert model.calls == [messages]


# ===========================================================================
# 三、redact_secrets
# ===========================================================================

def test_脱敏sk开头的key():
    text = "配置失败：DEEPSEEK_API_KEY=sk-abcdef123456"
    assert "sk-***" in llm.redact_secrets(text)
    assert "abcdef123456" not in llm.redact_secrets(text)


def test_没有key时原样返回():
    assert llm.redact_secrets("普通错误") == "普通错误"
    assert llm.redact_secrets(None) == ""


# ===========================================================================
# 四、get_default_model 是延迟调用
# ===========================================================================

def test_get_default_model委托给config(monkeypatch):
    calls = []

    def fake_get_model(*, temperature=0.0):
        calls.append(temperature)
        return "假模型"

    monkeypatch.setattr("app.config.get_model", fake_get_model)

    assert llm.get_default_model(temperature=0.3) == "假模型"
    assert calls == [0.3]

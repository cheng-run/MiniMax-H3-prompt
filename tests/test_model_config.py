from __future__ import annotations

from unittest.mock import Mock

from minimax_h3_prompt import model_factory
from minimax_h3_prompt.config import ModelSettings


def test_build_deepseek_from_config(monkeypatch):
    settings = ModelSettings(
        provider="deepseek",
        model="deepseek-v4-flash",
        base_url="https://api.deepseek.com/v1",
        api_key_env="DEEPSEEK_API_KEY",
        purpose="主模型",
    )
    monkeypatch.setattr(model_factory.config, "primary_model", settings)
    monkeypatch.setattr(model_factory.config, "resolve_api_key", lambda _: "secret")
    constructor = Mock()
    monkeypatch.setattr(model_factory, "ChatOpenAI", constructor)

    model_factory.build_chat_model()

    constructor.assert_called_once_with(
        model="deepseek-v4-flash", api_key="secret", base_url="https://api.deepseek.com/v1",
        timeout=600, max_retries=2,
    )


def test_build_vision_uses_config(monkeypatch):
    from minimax_h3_prompt.tools import reference_auditor

    settings = ModelSettings(
        provider="dashscope",
        model="qwen3.7-plus",
        base_url="https://vision.example/v1",
        api_key_env="DASHSCOPE_API_KEY",
        purpose="视觉模型",
    )
    monkeypatch.setattr(reference_auditor.config, "vision_model", settings)
    monkeypatch.setattr(reference_auditor.config, "resolve_api_key", lambda _: "secret")
    constructor = Mock()
    monkeypatch.setattr(reference_auditor, "ChatOpenAI", constructor)

    reference_auditor._build_vision_model()

    constructor.assert_called_once_with(
        model="qwen3.7-plus", api_key="secret", base_url="https://vision.example/v1",
        timeout=600, max_retries=2,
    )

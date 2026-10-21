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
        timeout=900, max_retries=0,
    )


def test_build_custom_openai_compatible_provider(monkeypatch):
    """未内置的 provider（自建网关/中转站）按 OpenAI 兼容端点建，不再被白名单拦下。

    2026-10-20 实测：`config/agent.yaml` 写 provider=opencode（opencode.ai/zen/go）
    时 `build_chat_model` 抛 `ValueError: 未知 LLM_PROVIDER`。而 config.py 早就为任意
    provider 生成 `{PROVIDER}_API_KEY` 默认名、工厂里的视觉侧（reference_auditor）也从
    不校验 provider 名——白名单只活在主模型这一处，属于自相矛盾。
    """
    settings = ModelSettings(
        provider="opencode",
        model="deepseek-v4.1-flash",
        base_url="https://opencode.ai/zen/go",
        api_key_env="OPENCODE_API_KEY",
        purpose="主模型",
    )
    monkeypatch.setattr(model_factory.config, "primary_model", settings)
    monkeypatch.setattr(model_factory.config, "resolve_api_key", lambda _: "secret")
    constructor = Mock()
    monkeypatch.setattr(model_factory, "ChatOpenAI", constructor)

    model_factory.build_chat_model()

    constructor.assert_called_once_with(
        model="deepseek-v4.1-flash", api_key="secret", base_url="https://opencode.ai/zen/go",
        timeout=900, max_retries=0,
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
        timeout=900, max_retries=0,
    )

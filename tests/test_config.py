from __future__ import annotations

import pytest

from minimax_h3_prompt import config as config_module
from minimax_h3_prompt.config import Config


@pytest.fixture
def config_without_env(monkeypatch):
    names = (
        "LLM_PROVIDER",
        "DEEPSEEK_API_KEY",
        "DEEPSEEK_MODEL",
        "DEEPSEEK_MODEL_NAME",
        "DEEPSEEK_BASE_URL",
        "DASHSCOPE_API_KEY",
        "DASHSCOPE_MODEL",
        "DASHSCOPE_MODEL_NAME",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
    )
    for name in names:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.setattr(config_module, "load_dotenv", lambda *_args, **_kwargs: None)
    return Config


def test_loads_primary_and_vision_from_yaml(config_without_env):
    cfg = config_without_env()
    assert cfg.primary_model.provider == "deepseek"
    assert cfg.primary_model.model == "deepseek-v4-flash"
    assert cfg.primary_model.base_url == "https://api.deepseek.com/v1"
    assert cfg.vision_model.provider == "dashscope"
    assert cfg.vision_model.model == "qwen3.7-plus"
    assert cfg.vision_model.base_url == "https://dashscope.aliyuncs.com/compatible-mode/v1"


def test_api_keys_are_resolved_from_environment(config_without_env, monkeypatch):
    cfg = config_without_env()
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-primary-secret")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-vision-secret")
    assert cfg.resolve_api_key(cfg.primary_model) == "test-primary-secret"
    assert cfg.resolve_api_key(cfg.vision_model) == "test-vision-secret"
    assert "test-primary-secret" not in repr(cfg.primary_model)
    assert "test-vision-secret" not in repr(cfg.vision_model)
    assert "api_key" not in cfg.raw.get("models", {}).get("primary", {})


def test_missing_key_names_environment_variable(config_without_env):
    cfg = config_without_env()
    with pytest.raises(ValueError, match="DEEPSEEK_API_KEY"):
        cfg.resolve_api_key(cfg.primary_model)


def test_legacy_environment_overrides_yaml(config_without_env, monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "deepseek")
    monkeypatch.setenv("DEEPSEEK_MODEL_NAME", "legacy-model")
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "https://legacy.example/v1")
    cfg = config_without_env()
    assert cfg.primary_model.model == "legacy-model"
    assert cfg.primary_model.base_url == "https://legacy.example/v1"


def test_anthropic_auth_token_fallback(config_without_env, monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "test-token")
    cfg = config_without_env()
    assert cfg.resolve_api_key(cfg.primary_model) == "test-token"

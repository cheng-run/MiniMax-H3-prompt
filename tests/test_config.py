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
    # 上面是内置 provider 的固定清单，换网关（provider: opencode 等）后会漏删：
    # 模块级 `config = Config()` 导入时已 load_dotenv 过，真 key 就留在 os.environ 里，
    # 于是「缺 key 应当报错」的用例静默通过、断言值的用例拿到真 key。按当前 YAML
    # 实际用的 key 名再删一遍（2026-10-20 实测 provider 换成 opencode 时踩到）。
    probe = Config()
    for name in (probe.primary_model.api_key_env, probe.vision_model.api_key_env):
        monkeypatch.delenv(name, raising=False)
    return Config


def test_loads_primary_and_vision_from_yaml(config_without_env):
    cfg = config_without_env()
    # 直接引用 YAML 中的值，而不是硬编码模型名——YAML 改模型，测试无需同步改。
    yaml_primary = cfg.raw.get("models", {}).get("primary", {})
    yaml_vision = cfg.raw.get("models", {}).get("vision", {})
    assert cfg.primary_model.provider == yaml_primary.get("provider", "dashscope")
    assert cfg.primary_model.model == yaml_primary.get("model")
    assert cfg.primary_model.base_url == yaml_primary.get("base_url")
    assert cfg.vision_model.provider == yaml_vision.get("provider", "dashscope")
    assert cfg.vision_model.model == yaml_vision.get("model")
    assert cfg.vision_model.base_url == yaml_vision.get("base_url")


def test_api_keys_are_resolved_from_environment(config_without_env, monkeypatch):
    cfg = config_without_env()
    # key 名跟着 YAML 走，不硬编码某个 provider——理由同 test_loads_primary_and_vision_from_yaml
    monkeypatch.setenv(cfg.primary_model.api_key_env, "test-secret")
    monkeypatch.setenv(cfg.vision_model.api_key_env, "test-secret")
    assert cfg.resolve_api_key(cfg.primary_model) == "test-secret"
    assert cfg.resolve_api_key(cfg.vision_model) == "test-secret"
    assert "test-secret" not in repr(cfg.primary_model)
    assert "test-secret" not in repr(cfg.vision_model)
    assert "api_key" not in cfg.raw.get("models", {}).get("primary", {})


def test_missing_key_names_environment_variable(config_without_env):
    cfg = config_without_env()
    with pytest.raises(ValueError, match=cfg.primary_model.api_key_env):
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

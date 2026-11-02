"""配置 schema（体检 §4.4，P3）：未知键报错、节形状、视觉档 provider 退役。

反例都是真实踩过的形态：

- ``provider`` 拼成 ``provder`` → 旧实现静默回落到环境探测，用户以为换了网关；
- ``models.vision.provider`` 写成 ``anthropic`` → 客户端仍是 ChatOpenAI，
  只有报错文案会说错（2026-10-24 视觉网关事故的同族）。

变异检验：把 ``validate_schema`` 从 ``Config.__init__`` 里摘掉，
``test_construction_fails_loudly`` 当场红。
"""
from __future__ import annotations

import pytest
import yaml

from minimax_h3_prompt import config as config_module
from minimax_h3_prompt.config import CONFIG_PATH, Config, validate_schema
from minimax_h3_prompt.errors import ConfigError


def test_unknown_top_level_key_is_rejected_with_a_suggestion():
    with pytest.raises(ConfigError, match="pipelin") as excinfo:
        validate_schema({"pipelin": {"default_duration": 5}})
    assert "pipeline" in str(excinfo.value), "报错要给最接近的候选键"


def test_typo_inside_a_section_is_rejected():
    with pytest.raises(ConfigError, match="max_qa_iteration"):
        validate_schema({"pipeline": {"max_qa_iteration": 2}})


def test_typo_in_model_block_suggests_provider():
    with pytest.raises(ConfigError) as excinfo:
        validate_schema({"models": {"primary": {"provder": "deepseek"}}})
    assert "provider" in str(excinfo.value)


def test_vision_provider_is_retired_with_an_explanation():
    with pytest.raises(ConfigError, match="models.vision.provider 已退役"):
        validate_schema({"models": {"vision": {"provider": "dashscope"}}})


@pytest.mark.parametrize("raw", [{"pipeline": []}, {"director": "736x1280"},
                                 {"models": {"primary": []}}])
def test_sections_must_be_mappings(raw):
    with pytest.raises(ConfigError, match="必须是映射"):
        validate_schema(raw)


def test_empty_sections_and_none_values_are_allowed():
    """``pipeline:`` 留空是仓库出货形态之外但合法的写法，不该因为「没写」报错。"""
    validate_schema({})
    validate_schema(None)
    validate_schema({"pipeline": None, "models": {}})


def test_shipped_config_passes_validation():
    """盘点一遍出货文件：加新键必须同步 ``_KNOWN_*``，否则这里先红。"""
    raw = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    validate_schema(raw)
    assert "provider" not in (raw.get("models", {}).get("vision") or {}), (
        "出货 agent.yaml 又写回了视觉档 provider：它已退役，写了会报错")


def test_construction_fails_loudly_on_unknown_key(tmp_path, monkeypatch):
    yaml_path = tmp_path / "agent.yaml"
    yaml_path.write_text("pipeline:\n  max_qa_iteration: 2\n", encoding="utf-8")
    monkeypatch.setattr(config_module, "CONFIG_PATH", yaml_path)
    monkeypatch.setattr(config_module, "load_dotenv", lambda *a, **k: None)

    with pytest.raises(ConfigError, match="max_qa_iteration"):
        Config()

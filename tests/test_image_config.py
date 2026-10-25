"""生图模型三档分配（issue #87 / 父 spec #84 D8）。

**接缝**：``image_config.load_image_settings`` / ``image_model_for``。断言的是外部行为——
生效配置从哪个文件读、三档分别是谁、写错了报不报。

为什么值得单开一层配置：分配是**产物规格**（人物用哪个模型出参考图），不是代码常量。
写死在三处调用里，换模型就得改代码；而「用户以为改了、实际没生效」正是本仓反复要防的
静默失败——所以缺键回落默认（没写≠走不下去），**写了的非法值一律大声报**。
"""
from __future__ import annotations

import pytest

from minimax_h3_prompt import image_config


def _write(tmp_path, text: str):
    path = tmp_path / "image.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_built_in_defaults_match_the_spec_assignment():
    """代码默认：人物 Qwen、道具与场景 Z-Image（spec #84 D8）。

    期望值是**字面量**，不从 ``DEFAULT_IMAGE_MODELS`` 现算——从实现推导期望就等于
    「自己和自己比」，默认值被改成什么都绿。

    刻意**不读仓库里的 ``config/image.yaml``**：那是用户会改的部署配置（同
    ``agent.yaml`` 换模型、``library.yaml`` 换盘），断言它的内容会让「用户合法地改了
    配置」变成基线红。仓库那份只验「能被读通」（见下一条）。
    """
    defaults = image_config.DEFAULT_IMAGE_MODELS

    assert defaults["character"] == "qwen"
    assert defaults["prop"] == "z_image"
    assert defaults["scene"] == "z_image"


def test_shipped_config_file_is_readable():
    """仓库里那份 ``config/image.yaml`` 至少要是合法配置（语法、键名、值域）。

    不断言它的**取值**（用户会改），只断言它读得通——拼错键名或写错的模型名仍然报。
    """
    settings = image_config.load_image_settings(image_config.IMAGE_CONFIG_PATH)

    for kind in image_config.IMAGE_KINDS:
        assert settings.model_for(kind) in image_config.IMAGE_MODEL_KEYS


def test_defaults_apply_when_the_config_file_is_absent(tmp_path, monkeypatch):
    """没写配置文件不构成流程走不下去：三档回落默认（同 workflow.yaml 口径）。"""
    monkeypatch.setattr(image_config, "IMAGE_CONFIG_PATH", tmp_path / "missing.yaml")

    assert image_config.image_model_for("character") == "qwen"
    assert image_config.image_model_for("prop") == "z_image"
    assert image_config.image_model_for("scene") == "z_image"


def test_config_file_decides_every_tier(tmp_path, monkeypatch):
    """改配置文件即改三档（读取口在调用时读盘，不缓存）。"""
    path = _write(tmp_path, "character: moss\nprop: qwen\nscene: z_image\n")
    monkeypatch.setattr(image_config, "IMAGE_CONFIG_PATH", path)

    assert image_config.image_model_for("character") == "moss"
    assert image_config.image_model_for("prop") == "qwen"
    assert image_config.image_model_for("scene") == "z_image"


def test_partial_config_keeps_the_other_tiers_default(tmp_path, monkeypatch):
    """只写一档时其余两档仍是默认——漏写不等于清空。"""
    path = _write(tmp_path, "character: moss\n")
    monkeypatch.setattr(image_config, "IMAGE_CONFIG_PATH", path)

    assert image_config.image_model_for("character") == "moss"
    assert image_config.image_model_for("prop") == "z_image"
    assert image_config.image_model_for("scene") == "z_image"


def test_unknown_model_value_is_loud(tmp_path, monkeypatch):
    """写错模型名的后果是「出的图不是按这个模型的口径写的」，静默回落会把笔误藏起来。"""
    path = _write(tmp_path, "character: sd3\n")
    monkeypatch.setattr(image_config, "IMAGE_CONFIG_PATH", path)

    with pytest.raises(ValueError, match="sd3"):
        image_config.load_image_settings()


def test_unknown_tier_key_is_loud(tmp_path, monkeypatch):
    """拼错的档名（如 ``characters``）若静默略过，用户会以为改了、实际还是默认。"""
    path = _write(tmp_path, "characters: qwen\n")
    monkeypatch.setattr(image_config, "IMAGE_CONFIG_PATH", path)

    with pytest.raises(ValueError, match="characters"):
        image_config.load_image_settings()


def test_kind_keys_match_the_asset_kind_labels():
    """档名与 ``generation.KIND_LABELS`` 的键是同一组：节点靠那张表把中文类别逆映射成档名。

    两处各写一份时，改一处漏一处就是「节点去取一个不存在的档」——装配期不报，出词时
    才炸（或更糟：静默按默认档出词）。
    """
    from minimax_h3_prompt.generation import KIND_LABELS

    assert tuple(KIND_LABELS) == tuple(image_config.IMAGE_KINDS)


def test_every_discipline_model_is_selectable():
    """三档值域与纪律文件的键是同一组：能配出来的模型都必须有纪律可加载。

    两处各写一份清单时，新增一个模型会只改到一边（配置认它、纪律文件没有），
    到出词那一步才炸。这里把两份清单钉成同一组。
    """
    from minimax_h3_prompt.tools import image_prompt_discipline as discipline

    assert tuple(image_config.IMAGE_MODEL_KEYS) == tuple(discipline.DISCIPLINE_MODELS)
    for key in image_config.IMAGE_MODEL_KEYS:
        assert image_config.image_model_label(key)

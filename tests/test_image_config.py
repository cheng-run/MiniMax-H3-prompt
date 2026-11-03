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
    # 同一份文件里的 ``frame:`` 也要读得通（值不钉死——用户会改）。
    assert image_config.load_frame_image_model(
        image_config.IMAGE_CONFIG_PATH) in image_config.IMAGE_MODEL_KEYS


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


# --- 旧键位 zimage 只作读取别名（issue #146 AC1/AC4）---------------------------
#
# 统一键位之前，画布键、image.yaml 与旧 bundle 里都有 ``zimage``。改名的代价不能落给
# 已经跑过的会话：它们读回来必须仍是 Z-Image，而不是「未知模型」或静默变成别的档。

def test_legacy_zimage_reads_as_the_canonical_key(tmp_path):
    """配置文件写旧键位仍读得通，读出来的值已归一成 ``z_image``。"""
    path = _write(tmp_path, "character: zimage\nframe: zimage\n")

    assert image_config.load_image_settings(path).model_for("character") == "z_image"
    assert image_config.load_frame_image_model(path) == "z_image"
    assert image_config.canonical_image_model("zimage") == "z_image"
    assert image_config.canonical_image_model("z_image") == "z_image"


def test_legacy_zimage_label_still_reads_as_z_image():
    """旧产物行里的 ``zimage`` 家族名照旧有展示名（渲染层按它出标题）。"""
    from minimax_h3_prompt.generation import IMAGE_FAMILY_LABELS

    assert IMAGE_FAMILY_LABELS["zimage"] == "Z-Image"
    assert IMAGE_FAMILY_LABELS["z_image"] == "Z-Image"


# --- 关键帧生图模型（2026-10-30 用户裁定：默认 Qwen-Image 2.1）----------------
#
# 为什么关键帧要单独一档、而不是复用三档资产分配：2026-10-30 实跑现场，关键帧词是按
# Z-Image 的口径出来的中文长文（707/771 字），而用户本地出图用的是 Qwen-Image 2.1
# （要英文长文、不写负面）——写错档不报错，只是质量掉一档。三档资产分配描述的是
# 「人物/道具/场景各用哪个模型」，与「关键帧用哪个模型」是两个问题，不能共用。

def test_frame_model_defaults_to_qwen(tmp_path):
    """没写 ``frame:`` 时按 2026-10-30 的用户裁定取 Qwen-Image 2.1。

    期望值是**字面量**：从实现现算就等于自己和自己比，默认值被改成什么都绿。
    """
    assert image_config.DEFAULT_FRAME_IMAGE_MODEL == "qwen"
    assert image_config.load_frame_image_model(tmp_path / "none.yaml") == "qwen"


def test_frame_model_reads_the_same_config_file(tmp_path):
    """``frame:`` 与三档资产分配住在同一个 image.yaml 里（换模型只改一处）。"""
    path = _write(tmp_path, "frame: moss\n")

    assert image_config.load_frame_image_model(path) == "moss"


def test_frame_model_rejects_unknown_value(tmp_path):
    """写错模型名必须大声报：静默回默认会让用户以为换了、实际一直按旧档出词。"""
    path = _write(tmp_path, "frame: sd3\n")

    with pytest.raises(ValueError, match="sd3"):
        image_config.load_frame_image_model(path)


def test_frame_key_does_not_break_the_asset_tiers(tmp_path):
    """``frame:`` 是第四个键、但不是第四档资产：读三档时不许把它当未知字段拦下。"""
    path = _write(tmp_path, "frame: qwen\ncharacter: moss\n")

    settings = image_config.load_image_settings(path)

    assert settings.model_for("character") == "moss"
    assert "frame" not in image_config.IMAGE_KINDS

"""生图提示词按模型纪律块分支（issue #87 / 父 spec #84 D7）。

三档生图模型（``z_image`` / ``qwen`` / ``moss``）各有一份纪律块，出词请求的**系统消息**
按该档模型追加对应的一份。本文件钉四件事，全部是外部行为：

1. **一档一份**：三份纪律文本互不相同，且各自带着该模型的写法特征（中文正向／英文长文不写负面／英文＋负面标签）。
2. **不再混进公共段**：语言与负向这两条原本写在角色 .md 的公共部分，现在必须按模型走——
   留在公共段就是给 Qwen 写反话（同一段系统提示词里两个相反指令，模型只会挑一个）。
3. **到达性**：装配出的 system 消息里**真的有**该档那一份（用记录型桩模型抓出站消息，
   而不是断言某个常量——常量对了、装配时漏掉，照样不报红）。
4. **变异检验**：删掉纪律块、或把模型换错，上面的断言会红。

JSON 契约与锁死清单规则**不因换模型而变**：三档的 system 消息里都必须有同一份输出格式，
节点对该契约的校验也不因模型分叉。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from minimax_h3_prompt.agents import load_role_prompt
from minimax_h3_prompt.tools import image_prompt_discipline as discipline
from minimax_h3_prompt.tools.image_prompt_discipline import (
    DEFAULT_IMAGE_MODEL,
    DISCIPLINE_MODELS,
    discipline_text,
)

PROMPTS_DIR = Path(__file__).parents[1] / "src" / "minimax_h3_prompt" / "prompts"
ROLE_MD = PROMPTS_DIR / "image_prompt_engineer.md"


def _rule_lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if re.match(r"^\d+\.\s", line)]


# ---------------------------------------------------------------------------
# 1. 一档一份纪律，各带该模型的写法特征
# ---------------------------------------------------------------------------

def test_three_models_have_three_distinct_disciplines():
    """三份文本互不相同——同一份内容换个文件名就等于没有分支。"""
    texts = {model: discipline_text(model) for model in DISCIPLINE_MODELS}

    assert len(texts) == 3
    assert len(set(texts.values())) == 3, "有两档纪律文本完全相同（分支没生效）"
    for model, text in texts.items():
        assert text.startswith(discipline.DISCIPLINE_HEADING_PREFIX), \
            f"{model} 档的纪律不是以标题起头的完整小节"


def test_zimage_discipline_keeps_the_eight_turbo_rules():
    """Z-Image 档保持现状八条（spec #84 D7「保持现状」）：换布局不等于重写规则。"""
    text = discipline_text("z_image")

    assert "Z-Image Turbo" in text
    assert len(_rule_lines(text)) == 8, f"Z-Image 档的编号纪律不是 8 条：{_rule_lines(text)}"
    # 语言必须先在这一档里说清：只读纪律块的调用点（AI 改词）拿不到别处的说明，
    # 而「正文中文」正是 Z-Image 与另外两档最直观的差别（spec #84 D7）。
    assert "中文" in text
    assert "正向" in text and "600" in text


def test_qwen_discipline_demands_english_long_form_without_negatives():
    """Qwen 档：英文、约 20 句长文、固定项原样保留、文字逐条写、光影单独一句、不写负面。"""
    text = discipline_text("qwen")

    assert "Qwen-Image 2.1" in text
    assert "英文" in text
    assert "20 句" in text
    assert "负面" in text and "空串" in text
    # cfg 1 的依据要写在这档自己的文本里：搬运维常识而不给依据，下一档会照抄错。
    assert "CFG=1" in text or "cfg 1" in text.lower()


def test_moss_discipline_keeps_the_negative_tags():
    """MOSS 档：负面标签保留并生效——这是它与另外两档最本质的差别。"""
    text = discipline_text("moss")

    assert "MOSS-Image" in text
    assert "英文" in text
    for tag in ("nsfw", "worst quality", "bad anatomy", "watermark", "jpeg artifact"):
        assert tag in text, f"MOSS 档没写默认负面标签 {tag!r}"
    assert "negative_prompt" in text


def test_unknown_model_and_broken_files_are_loud(tmp_path, monkeypatch):
    """未知模型、缺文件、文件被覆写成非纪律文本，三种都要响——静默返回空串＝没有纪律。"""
    with pytest.raises(ValueError, match="flux"):
        discipline_text("flux")

    monkeypatch.setattr(discipline, "DISCIPLINES_DIR", tmp_path)
    with pytest.raises(RuntimeError, match="缺少"):
        discipline_text("qwen")

    (tmp_path / "qwen.md").write_text("这不是一节纪律。\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="不是一节生图提示词纪律"):
        discipline_text("qwen")


# ---------------------------------------------------------------------------
# 2. 公共段只剩模型无关的内容（语言与负向按模型走）
# ---------------------------------------------------------------------------

def test_the_common_role_prompt_no_longer_prescribes_one_model():
    """语言与负向两条必须离开公共段：留在这里就是给 Qwen／MOSS 写反话。

    变异检验：把「正文语言使用中文」重新写回公共段，本用例必须红——那正是「换模型
    只改了配置、没改写法」的复发形态。
    """
    common = ROLE_MD.read_text(encoding="utf-8")

    assert "正文语言使用中文" not in common, "公共段又规定死中文了（Qwen／MOSS 会被写反话）"
    assert "默认不输出负向提示词" not in common, "公共段又把负向写死了（MOSS 这一档要用负面）"
    assert "Z-Image Turbo 文生图提示词" not in common, "公共段又把出图模型写死成 Z-Image 了"


def test_the_json_contract_stays_in_the_common_part():
    """JSON 契约与被锁死清单的规则留在公共段：换模型不改输出格式（票面验收）。"""
    common = ROLE_MD.read_text(encoding="utf-8")

    for key in ('"name"', '"positive_prompt"', '"negative_prompt"', '"locked_features"'):
        assert key in common, f"公共段丢了输出字段 {key}"
    assert "逐字摘自" in common, "公共段丢了「锁死清单逐字摘自正文」的规则"


# ---------------------------------------------------------------------------
# 3. 到达性：装配出的系统提示词按档取那一份
# ---------------------------------------------------------------------------

def test_role_prompt_carries_exactly_its_own_discipline():
    """每档的系统提示词只含自己那一份纪律，且含公共段（输出格式三档共用）。"""
    common = ROLE_MD.read_text(encoding="utf-8")

    for model in DISCIPLINE_MODELS:
        prompt = load_role_prompt("image_prompt_engineer", image_model=model)
        assert discipline_text(model) in prompt, f"{model} 档的纪律没进系统提示词"
        assert common.strip() in prompt, f"{model} 档的系统提示词丢了公共段"
        for other in DISCIPLINE_MODELS:
            if other == model:
                continue
            assert discipline_text(other) not in prompt, \
                f"{model} 档的系统提示词里混进了 {other} 档的纪律"


def test_the_default_discipline_is_still_zimage():
    """不带模型参数的调用（AI 改词等历史路径）沿用 Z-Image——本票之前只有这一档。"""
    assert load_role_prompt("image_prompt_engineer") == \
        load_role_prompt("image_prompt_engineer", image_model=DEFAULT_IMAGE_MODEL)
    assert discipline_text(DEFAULT_IMAGE_MODEL) in load_role_prompt("image_prompt_engineer")


def test_other_roles_do_not_get_an_image_discipline():
    """纪律块只追加给资产图提示词工程师：别的角色拿到的提示词里没有它。"""
    needle = _rule_lines(discipline_text("z_image"))[0]
    for role in ("frame_prompt_engineer", "prompt_engineer", "art_director"):
        assert needle not in load_role_prompt(role), f"{role} 拿到了生图纪律块"

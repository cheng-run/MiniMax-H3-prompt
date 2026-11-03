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


def test_asset_discipline_block_declares_precedence_over_common_clauses():
    """#139：资产图路径的纪律块必须自带「以本节为准」压盖语。

    现场：32 条已落盘资产条目（19 条 moss + 13 条 qwen）正文 100% 中文、moss 档
    ``negative_prompt`` 全空——公共段「正文中文／默认不写负向」把模型纪律盖掉了。
    关键帧路径在请求侧已有这句（``graph/nodes.py`` 的 ``_frame_discipline_block``），
    资产图路径走系统提示词，之前没有。

    变异检验：删掉这句 → 本用例红。
    """
    for model in DISCIPLINE_MODELS:
        prompt = load_role_prompt("image_prompt_engineer", image_model=model)
        assert "以本节为准" in prompt, f"{model} 档缺压盖语"
        assert prompt.index("以本节为准") < prompt.index(discipline_text(model)), \
            f"{model} 档的压盖语没排在纪律正文之前"


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


# ---------------------------------------------------------------------------
# 4. 同一份纪律被两个任务共用时的范围限定（#132 / D1、L5 Q6）
# ---------------------------------------------------------------------------

def test_qwen_text_rule_is_scoped_to_asset_images():
    """「画面里的文字逐条写」带适用范围：关键帧按角色提示词的 ≤2 上限（#132 D1）。

    为什么必须钉：纪律块随**帧请求**下发（``graph/nodes.py`` 的 ``_frame_discipline_block``），
    且写着「与角色提示词冲突时以本节为准」——不带范围，关键帧那条「必须可读的文字 ≤2 条」
    永远被这一句压过；而这条纪律同时还是资产图三节点的系统提示词，不能直接删。
    """
    text = discipline_text("qwen")

    assert "资产图" in text, "Qwen 纪律的文字条款没说清只管资产图"
    assert "关键帧" in text and "≤2" in text, \
        "Qwen 纪律没给关键帧留下「≤2」这条对应上限"
    assert "角色提示词" in text, "没指回关键帧角色提示词里那条上限"


def test_zimage_length_limit_is_a_local_experience_value():
    """「约 600 字」是本仓经验值，不是模型上限（#132 / L5 Q6）。

    ComfyUI 编码器不截断（ADR 0018 第 3 条事实），600 字只是「长文稀释注意力」的经验。
    写成硬上限，模型会为了凑长度删掉该写的细节，后来者也会以为机器会截断。
    """
    text = discipline_text("z_image")

    assert "本仓经验值" in text, "Z-Image 纪律仍把 600 字写成硬口径"
    assert "不是模型上限" in text, "600 字没标清它只是本仓经验值"


# ---------------------------------------------------------------------------
# 5. 图生图编辑纪律：同一模型、不同任务，两份不许混用（2026-10-31 尾帧事故）
# ---------------------------------------------------------------------------
#
# 文生图那份要求「写完整画面、约 20 句」；编辑请求要的是「绑定输入图、写终点机位、
# 只写变化」。混用不报错，只会让模型把参考图当草稿重画一遍——所以两份必须各住一处，
# 且装配时取对的那一份。

def test_every_model_has_a_distinct_edit_discipline():
    texts = {model: discipline.edit_discipline_text(model) for model in DISCIPLINE_MODELS}

    assert len(set(texts.values())) == 3, "有两档编辑纪律完全相同（分支没生效）"
    for model, text in texts.items():
        assert text.startswith(discipline.EDIT_DISCIPLINE_HEADING_PREFIX), \
            f"{model} 档的编辑纪律不是以标题起头的完整小节"
        assert discipline.discipline_text(model) != text, \
            f"{model} 档的编辑纪律与文生图那份是同一份（任务没分开）"


def test_edit_disciplines_carry_the_three_edit_anchors():
    """三档都必须点名：绑定输入图、写终点机位、保留写成不变句。"""
    for model in DISCIPLINE_MODELS:
        text = discipline.edit_discipline_text(model)
        assert "输入图" in text or "input image" in text.lower(), \
            f"{model} 档没要求绑定输入图"
        assert "机位" in text or "camera" in text.lower(), \
            f"{model} 档没要求写终点机位"
        assert "保持" in text or "unchanged" in text.lower(), \
            f"{model} 档没要求写保留句"


def test_missing_or_broken_edit_discipline_is_loud(tmp_path, monkeypatch):
    """缺文件、文件被覆写成别的东西，都要响亮报错——静默退回文生图那份就是事故。"""
    monkeypatch.setattr(discipline, "DISCIPLINES_DIR", tmp_path)
    with pytest.raises(RuntimeError, match="缺少图生图编辑"):
        discipline.edit_discipline_text("qwen")

    (tmp_path / "qwen_edit.md").write_text("这不是一节纪律。\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="不是一节图生图编辑纪律"):
        discipline.edit_discipline_text("qwen")


# ---------------------------------------------------------------------------
# 6. 尾帧编辑链（#131）：角色声明按档 + 背景保留句按机位分叉
# ---------------------------------------------------------------------------

def test_edit_disciplines_carry_the_role_declaration_by_tier():
    """每份编辑纪律都要交代「这是编辑，不是重新生成」，措辞按档（L4-01）。

    措辞不是修辞：官方把「永远是编辑」写成任务定性（Qwen-Image 2.1 编辑改写系统
    提示词 :18），Qwen 档跟指令语言写中文，Z-Image / MOSS 档写英文原句。
    """
    qwen = discipline.edit_discipline_text("qwen")
    assert "这是对输入图的编辑，不是重新生成" in qwen, "Qwen 编辑档没要求中文角色声明"
    for model in ("z_image", "moss"):
        text = discipline.edit_discipline_text(model)
        assert "This is an IMAGE EDIT, NOT A REGENERATION" in text, \
            f"{model} 编辑档没要求英文角色声明"


def test_edit_body_language_follows_the_task_not_the_model():
    """正文语言按**任务**分：编辑档跟指令语言（Qwen 中文），另两档语言不变（D3/L4-12）。

    同一条 Qwen 在两个任务上两种语言是有意的：文生图描述画面（英文长文），
    图生图执行指令（中文）。
    """
    assert "正文用中文" in discipline.edit_discipline_text("qwen"), \
        "Qwen 编辑档仍要求英文正文——这是错的那一半（官方 :5-9）"
    assert "正文用英文" not in discipline.edit_discipline_text("qwen")
    assert "正文用中文" in discipline.edit_discipline_text("z_image"), \
        "Z-Image 编辑档的语言不该被这条改到"
    assert "正文用英文" in discipline.edit_discipline_text("moss"), \
        "MOSS 编辑档的语言不该被这条改到"


def test_edit_disciplines_scope_background_continuity_by_camera_move():
    """背景连续改成**条件句**：机位不动才允许把背景列进保留项（L4-05 / L4-06）。

    机位在动时要求「背景相对位置不变」等于给模型两个互斥目标（2026-10-31 实跑：
    段词是 tracks backward，保留项却写 unchanged in their established spatial relationships）。
    """
    for model in DISCIPLINE_MODELS:
        text = discipline.edit_discipline_text(model)
        assert ("机位不动" in text) or ("camera does not move" in text.lower()), \
            f"{model} 编辑档的背景连续仍是无条件句"


# ---------------------------------------------------------------------------
# 7. 关键帧语言与资产图专属规则（issue #148）
# ---------------------------------------------------------------------------

def test_frame_role_prompt_takes_body_language_from_the_model_tier():
    """关键帧角色提示词不再写死中文：正文语言按【出图模型】/生图纪律（issue #148 AC2）。

    角色提示词是一份、三档共用；语言分支只能由请求里的【出图模型】【生图纪律】块给。
    写死「中文」时，Qwen／MOSS 的关键帧词会被角色把纪律块盖掉（同 #139 的现场形态）。
    """
    text = load_role_prompt("frame_prompt_engineer")

    assert "【出图模型】" in text, "角色提示词没把语言指给【出图模型】块"
    for name in ("Qwen", "MOSS", "Z-Image"):
        assert name in text, f"角色提示词没点出 {name} 档的语言差别"
    assert "英文" in text
    assert "必须是完整、可直接复制的中文自然语言生图提示词" not in text, \
        "关键帧角色提示词还写着无条件的「正文中文」"


def test_qwen_gait_order_is_scoped_to_asset_images():
    """Qwen 的「约 20 句」走帧顺序只管资产图；关键帧按角色八步骨架（issue #148 AC3）。

    两者顺序不同（资产：主体→外观→姿态→构图…／关键帧：背景→主体→头脸→衣物…），
    同一条纪律块同时喂着资产图与关键帧，不带范围就是在两处里压掉一处。
    """
    line = next(line for line in discipline_text("qwen").splitlines() if "约 20 句" in line)

    assert "资产图" in line, "Qwen 的走帧顺序没标适用范围"
    assert "关键帧" in line and "八步" in line, \
        "Qwen 的走帧顺序没给关键帧让路（角色八步骨架）"


def test_moss_text_and_length_rules_are_scoped_to_asset_images():
    """MOSS 的「文字与版式」「长度不设上限」只在资产图生效（issue #148 AC3）。

    关键帧有角色八步的两条相反上限（可读文字 ≤2 条、约 20 句 / 400–500 词），
    不带范围时纪律块（「以本节为准」）会把它们压掉。
    """
    lines = discipline_text("moss").splitlines()
    for keyword in ("文字与版式", "长度不设上限"):
        line = next(line for line in lines if keyword in line)
        assert "资产图" in line, f"MOSS 的「{keyword}」没标适用范围"
        assert "关键帧" in line, f"MOSS 的「{keyword}」没给关键帧让路"

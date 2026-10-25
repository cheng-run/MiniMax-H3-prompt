"""生图模型路由：文字类关键词命中 → MOSS，手动选择优先（issue #88 / 父 spec #84 D8）。

**接缝**：``image_routing`` 的纯函数（关键词命中、路由判定、条目字段的读写与描述）——
它不认识 LLM、不认识终端，因此可以把「命中哪个词」「谁覆盖谁」钉成可读的规格。
真正把它接进节点与素材循环的那两处接线由 ``tests/test_image_routing_wiring.py``（节点）
与 ``tests/test_wizard_asset_loop.py`` 末尾的 issue-#88 用例（素材循环菜单与横幅）钉。

为什么要双语的词表：设计正文与 MOSS/Qwen 出的提示词**语言不同**（正文中文、词是英文），
只写中文针在英文提示词上恒不命中＝这条路由对自动切过去的那一类资产结构性失效；
反过来只写英文针，中文设计正文里的「包装」就永远看不见（本仓 2026-10-22 在
地点词表上踩过同形：要求词侧必须双语）。
"""
from __future__ import annotations

import pytest

from minimax_h3_prompt import image_config, image_routing


# ---------------------------------------------------------------------------
# 关键词命中：中文正文与英文提示词都要咬得住
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text, expected", [
    ("瓶身的包装上印着一只橘猫", "包装"),
    ("门头挂着一块褪色的招牌", "招牌"),
    ("瓶身侧面贴着一张小标签", "标签"),
    ("瓶身包装的下方印着一行文字", "包装"),
    ("墙上贴着一张复古海报", "海报"),
    ("胸前有一个圆形 logo", "logo"),
])
def test_chinese_keywords_hit(text, expected):
    """六类中文关键词各自命中，并回**那一个词**——终端要念的就是它。"""
    assert image_routing.text_capability_hit(text) == expected


@pytest.mark.parametrize("text, expected", [
    ("A glass bottle with a printed LABEL on the body.", "标签"),
    ("A shop front with a faded neon sign above the door.", "招牌"),
    ("A cardboard packaging box on a wooden table.", "包装"),
    ("A round logo stitched on the chest.", "logo"),
    ("Large lettering across the top of the wall.", "文字"),
    ("A vintage poster taped to the wall.", "海报"),
])
def test_english_keywords_hit(text, expected):
    """英文提示词同样命中（大小写不敏感）：MOSS/Qwen 出的词是英文，只认中文＝漏判。"""
    assert image_routing.text_capability_hit(text) == expected


def test_english_needles_do_not_match_inside_longer_words():
    """英文针按**整词**匹配：``design``／``texture`` 不是文字能力需求。

    子串匹配会把「character design」认成招牌、「skin texture」认成文字，于是每次出词
    都静默切到 MOSS——命中错了不报错，只是每一张都按 MOSS 的纪律写。
    """
    assert image_routing.text_capability_hit("character design sheet, smooth texture") is None


def test_no_keyword_means_no_hit():
    """普通资产提示词不命中：未命中要回 None，路由据此走配置默认。"""
    assert image_routing.text_capability_hit("一位红裙女孩站在秋日庭院里") is None
    assert image_routing.text_capability_hit("", " ") is None


def test_multiple_texts_are_scanned_in_order():
    """多段文本（设计正文 + 该资产提示词）合并判断：任一段命中即算命中。"""
    assert image_routing.text_capability_hit("设计正文没提", "瓶身贴着标签") == "标签"


# ---------------------------------------------------------------------------
# 路由判定：手动 > 自动 > 配置默认
# ---------------------------------------------------------------------------

def test_auto_hit_routes_to_moss():
    """命中关键词 → MOSS（spec #84 D8：文字/版式 MOSS 最强）。"""
    result = image_routing.assignment("prop", texts=["瓶身的包装上印着一只橘猫"])

    assert result == {"model": "moss", "source": "auto", "keyword": "包装"}


def test_miss_keeps_the_configured_kind_model(tmp_path, monkeypatch):
    """未命中 → 该档配置的模型（不是写死的某一档）。"""
    path = tmp_path / "image.yaml"
    path.write_text("prop: qwen\n", encoding="utf-8")
    monkeypatch.setattr(image_config, "IMAGE_CONFIG_PATH", path)

    result = image_routing.assignment("prop", texts=["一把青铜钥匙"])

    assert result == {"model": "qwen", "source": "config", "keyword": ""}


def test_manual_choice_beats_auto_hit():
    """手动选择优先于自动规则：命中「包装」也按人选的 Z-Image 出词。"""
    result = image_routing.assignment(
        "prop", texts=["瓶身的包装上印着一只橘猫"], manual="z_image")

    assert result == {"model": "z_image", "source": "manual", "keyword": ""}


def test_manual_choice_is_checked_against_the_model_vocabulary():
    """手动值写错要响：静默回落等于「人以为改了、实际还是旧的」。"""
    with pytest.raises(ValueError, match="sd3"):
        image_routing.assignment("prop", texts=["一把青铜钥匙"], manual="sd3")


# ---------------------------------------------------------------------------
# 条目字段（state 里那一份记录）：读、判手动、描述
# ---------------------------------------------------------------------------

def _entry(**extra) -> dict:
    return {"name": "橘子汽水", "positive_prompt": "…", **extra}


def test_entry_round_trips_the_assignment():
    """写进条目、再读回来是同一份——节点与素材循环之间只走这一种形状。"""
    entry = _entry()
    image_routing.put_assignment(
        entry, {"model": "moss", "source": "auto", "keyword": "包装"})

    assert image_routing.assignment_of(entry) == {
        "model": "moss", "source": "auto", "keyword": "包装"}
    assert image_routing.model_of(entry) == "moss"


def test_old_entries_without_the_field_read_as_unknown():
    """旧会话的条目没有这个字段：读回 None，调用方回落到配置默认（不是报错）。"""
    assert image_routing.assignment_of(_entry()) is None
    assert image_routing.model_of(_entry()) is None


def test_manual_entries_are_told_apart_from_auto_ones():
    """节点重跑时要认得出「这是人选的」，否则一次阶段 1 重跑就把人的选择冲掉。"""
    auto, manual = _entry(), _entry()
    image_routing.put_assignment(auto, {"model": "moss", "source": "auto", "keyword": "包装"})
    image_routing.put_assignment(manual, {"model": "z_image", "source": "manual", "keyword": ""})

    assert image_routing.manual_model_of(auto) is None
    assert image_routing.manual_model_of(manual) == "z_image"


def test_malformed_entry_field_is_loud():
    """字段坏了要响：认不出模型就意味着后续改词会按别家的纪律改，静默不许。"""
    with pytest.raises(ValueError, match="image_model"):
        image_routing.assignment_of(_entry(image_model="moss"))
    with pytest.raises(ValueError, match="sd3"):
        image_routing.assignment_of(_entry(image_model={"model": "sd3", "source": "auto"}))


def test_describe_names_the_hit_word_and_the_model():
    """终端那一行：命中词 + 落到哪个模型，用户据此复核自动判断。"""
    assert image_routing.described(
        {"model": "moss", "source": "auto", "keyword": "包装"}) == "命中『包装』→ MOSS-Image"
    assert image_routing.described(
        {"model": "z_image", "source": "manual", "keyword": ""}) == "手动指定 → Z-Image Turbo"
    assert image_routing.described(
        {"model": "qwen", "source": "config", "keyword": ""}) == "按配置默认 → Qwen-Image 2.1"


def test_write_port_rejects_bad_values():
    """写口与读口同一套校验：存进去一个不存在的模型/来源，读的时候才炸就已经晚了。"""
    entry = _entry()
    with pytest.raises(ValueError, match="sd3"):
        image_routing.put_assignment(entry, {"model": "sd3", "source": "auto"})
    with pytest.raises(ValueError, match="bogus"):
        image_routing.put_assignment(entry, {"model": "moss", "source": "bogus"})
    assert image_routing.ENTRY_KEY not in entry, "坏值不许落进条目"


def test_described_refuses_to_guess_a_bad_source():
    """描述口静默兜底会把坏来源伪装成「按配置默认」——终端会理直气壮地报错理由。"""
    with pytest.raises(ValueError, match="bogus"):
        image_routing.described({"model": "moss", "source": "bogus"})


def test_keyword_list_is_the_spec_list():
    """词表就是票面那六类（issue #88）：多一类或少一类都会改变哪些资产被切到 MOSS。"""
    assert image_routing.TEXT_CAPABILITY_KEYWORDS == ("标签", "招牌", "包装", "logo", "文字", "海报")

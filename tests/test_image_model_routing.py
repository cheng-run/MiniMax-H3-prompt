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
    assert image_routing.text_capability_hit(text).word == expected


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
    assert image_routing.text_capability_hit(text).word == expected


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
    assert image_routing.text_capability_hit("设计正文没提", "瓶身贴着标签").word == "标签"


# ---------------------------------------------------------------------------
# 路由判定：手动 > 自动 > 配置默认
# ---------------------------------------------------------------------------

def test_auto_hit_routes_to_moss():
    """命中关键词 → MOSS（spec #84 D8：文字/版式 MOSS 最强）。"""
    result = image_routing.assignment("prop", texts=["瓶身的包装上印着一只橘猫"])

    assert result == {
        "model": "moss", "source": "auto", "keyword": "包装",
        "evidence": "瓶身的包装上印着一只橘猫",
    }


def test_miss_keeps_the_configured_kind_model(tmp_path, monkeypatch):
    """未命中 → 该档配置的模型（不是写死的某一档）。"""
    path = tmp_path / "image.yaml"
    path.write_text("prop: qwen\n", encoding="utf-8")
    monkeypatch.setattr(image_config, "IMAGE_CONFIG_PATH", path)

    result = image_routing.assignment("prop", texts=["一把青铜钥匙"])

    assert result == {"model": "qwen", "source": "config", "keyword": "", "evidence": ""}


def test_manual_choice_beats_auto_hit():
    """手动选择优先于自动规则：命中「包装」也按人选的 Z-Image 出词。"""
    result = image_routing.assignment(
        "prop", texts=["瓶身的包装上印着一只橘猫"], manual="z_image")

    assert result == {"model": "z_image", "source": "manual", "keyword": "", "evidence": ""}


def test_manual_choice_is_checked_against_the_model_vocabulary():
    """手动值写错要响：静默回落等于「人以为改了、实际还是旧的」。"""
    with pytest.raises(ValueError, match="sd3"):
        image_routing.assignment("prop", texts=["一把青铜钥匙"], manual="sd3")


def test_character_skips_the_keyword_rule(tmp_path, monkeypatch):
    """人物类不参与关键词自动切档（L6-12 / 2026-11-01 裁定）：仍按配置默认。

    身份板上常有校名、臂章上的字——自动规则会因此把整张板切到 MOSS，而
    ``moss.md:11`` 明写四格身份板不是本档强项。这条只挡**自动**规则。
    """
    path = tmp_path / "image.yaml"
    path.write_text("character: qwen\n", encoding="utf-8")
    monkeypatch.setattr(image_config, "IMAGE_CONFIG_PATH", path)

    result = image_routing.assignment(
        "character", texts=["身份板：校服胸前绣着校名，臂章上有文字与 logo"])

    assert result == {"model": "qwen", "source": "config", "keyword": "", "evidence": ""}


def test_manual_moss_still_works_for_character():
    """手动指定对人物仍然生效（含 MOSS）：人看图选档是人的判断，不受自动规则豁免波及。"""
    result = image_routing.assignment(
        "character", texts=["身份板：校服胸前绣着校名"], manual="moss")

    assert result == {"model": "moss", "source": "manual", "keyword": "", "evidence": ""}


def test_exempt_kinds_are_real_asset_kinds():
    """豁免表里的类别必须是真实存在的档名：拼错/改名会让豁免静默失效（永不命中的守卫）。

    判据与 ``image_config.IMAGE_KINDS`` 对齐而不是抄一份字面量——两处各写一遍，改档名时
    就会只改到一边。
    """
    assert image_routing.AUTO_ROUTING_EXEMPT_KINDS <= set(image_config.IMAGE_KINDS)


# ---------------------------------------------------------------------------
# 条目字段（state 里那一份记录）：读、判手动、描述
# ---------------------------------------------------------------------------

def _entry(**extra) -> dict:
    return {"name": "橘子汽水", "positive_prompt": "…", **extra}


def test_entry_round_trips_the_assignment():
    """写进条目、再读回来是同一份——节点与素材循环之间只走这一种形状。"""
    entry = _entry()
    image_routing.put_assignment(entry, {
        "model": "moss", "source": "auto", "keyword": "包装",
        "evidence": "瓶身的包装印着品牌色块"})

    assert image_routing.assignment_of(entry) == {
        "model": "moss", "source": "auto", "keyword": "包装", "evidence": "瓶身的包装印着品牌色块"}
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


# ---------------------------------------------------------------------------
# 否定句不误切档与命中证据（issue #167 / spec #169 C）
#
# 现场：设计正文里一句「不出现现代塑料包装」把 12 条道具全部切到 MOSS（2026-11-04
# r2v 实跑）。切档错了不报错，只是整类的词都按另一套纪律写；所以判定必须看**命中词
# 所在的那一个分句**是不是禁令。取舍写明：宁可漏切、不整类误切——人还能在素材循环里
# 手动改档，而自动规则误切的整类不会自己浮出来。
# ---------------------------------------------------------------------------

def test_hit_carries_the_clause_it_came_from():
    """命中不只回那个词，还回它所在的分句——终端要念「因为哪一句」。"""
    hit = image_routing.text_capability_hit("道具设计：瓶身贴着标签，颜色偏暖")

    assert hit is not None
    assert hit.word == "标签"
    assert hit.clause == "瓶身贴着标签", "分句按中文标点切开，不带前后那几段"


def test_negated_clause_does_not_hit():
    """「不得出现现代塑料包装」是禁令不是文字需求：整类道具不许被切到 MOSS。"""
    assert image_routing.text_capability_hit("道具设计：瓶身不得出现现代塑料包装") is None


def test_affirmative_clause_beside_a_negated_one_still_hits():
    """同一段里否定分句与肯定分句并存：只有肯定那一句生效（否定不污染整段）。"""
    hit = image_routing.text_capability_hit(
        "不得出现现代塑料包装；瓶身要贴一张手写标签")

    assert hit is not None
    assert (hit.word, hit.clause) == ("标签", "瓶身要贴一张手写标签")


def test_a_negation_after_the_keyword_still_covers_the_whole_clause():
    """否定词出现在命中词之后也算这一句的禁令：分句是判定的粒度，不是位置先后。"""
    assert image_routing.text_capability_hit("道具设计：包装不得使用现代塑料") is None


@pytest.mark.parametrize("clause", [
    "不得出现现代塑料包装",
    "不出现任何标签",
    "不需要包装",
    "不要文字",
    "不使用海报",
    "不能有招牌",
    "禁止添加 logo",
    "避免出现文字",
    "没有标签",
    "无包装",
])
def test_chinese_negations_suppress_the_hit(clause):
    """中文否定词逐条咬住（票面点名的九个词 ＋ 分句首字「无」）。"""
    assert image_routing.text_capability_hit(f"道具设计：{clause}") is None


@pytest.mark.parametrize("clause", [
    "no labels or packaging",
    "without any text",
    "avoid lettering",
    "never show a logo",
    "not a poster in sight",
])
def test_english_negations_suppress_the_hit(clause):
    """英文否定词逐条咬住：MOSS/Qwen 档的正文是英文，只写中文否定词等于对那一半文本失效。"""
    assert image_routing.text_capability_hit(f"prop design: a bottle, {clause}") is None


def test_english_negation_needs_a_word_boundary():
    """英文否定词按整词匹配：``anodized`` 里的 ``no`` 不是否定（否则每张金属道具都漏切）。"""
    hit = image_routing.text_capability_hit(
        "An anodized bottle with a printed label on the body.")

    assert hit is not None and hit.word == "标签"


def test_negation_is_per_clause_not_per_paragraph():
    """否定只吃它所在的分句：英文同段里另一个肯定分句照旧命中。"""
    hit = image_routing.text_capability_hit(
        "prop design: no labels on the cap, but a printed poster on the wall")

    assert hit is not None and hit.word == "海报"


def test_the_wu_negation_only_counts_at_the_clause_start():
    """「无」只按**分句首字**算否定（票面口径）。

    句中出现的「无」不算：正文里的「无锡」「无需」既可能是地名也可能是半句，放宽到
    子串会把肯定诉求也吞掉。要放宽得先改规格——所以这条钉住当前的边界。
    """
    assert image_routing.text_capability_hit("道具设计：无包装") is None
    assert image_routing.text_capability_hit("道具设计：瓶身无文字").word == "文字"


def test_assignment_records_the_source_clause():
    """切档记录里存来源分句：事后要能回答「当初凭什么切的这一档」。"""
    result = image_routing.assignment("prop", texts=["道具设计：瓶身包装印着品牌色块"])

    assert result == {
        "model": "moss", "source": "auto", "keyword": "包装",
        "evidence": "瓶身包装印着品牌色块",
    }


def test_assignment_leaves_evidence_empty_when_no_clause_decided_it():
    """手动与配置默认没有来源分句：证据字段留空，不许拿提示词正文凑数。"""
    manual = image_routing.assignment(
        "prop", texts=["瓶身包装印着品牌色块"], manual="z_image")
    configured = image_routing.assignment("prop", texts=["一把青铜钥匙"])

    assert manual["evidence"] == "" and configured["evidence"] == ""


def test_described_appends_the_source_clause():
    """终端那一行带上来源：「因为哪一句」决定用户要不要当场手改。"""
    text = image_routing.described({
        "model": "moss", "source": "auto", "keyword": "包装",
        "evidence": "瓶身包装印着品牌色块",
    })

    assert text == "命中『包装』→ MOSS-Image（来源：瓶身包装印着品牌色块）"


def test_described_says_nothing_about_sources_when_there_is_no_evidence():
    """旧记录没有证据字段：照常展示，不印一个空的「（来源：）」。"""
    text = image_routing.described({"model": "moss", "source": "auto", "keyword": "包装"})

    assert text == "命中『包装』→ MOSS-Image"


def test_old_records_without_evidence_still_load():
    """issue #167 之前写进 state 的记录没有 evidence：读回空串，不报错（旧会话照常续跑）。"""
    entry = _entry(image_model={"model": "moss", "source": "auto", "keyword": "包装"})

    assert image_routing.assignment_of(entry) == {
        "model": "moss", "source": "auto", "keyword": "包装", "evidence": ""}
    assert image_routing.manual_model_of(_entry(
        image_model={"model": "moss", "source": "manual", "keyword": ""})) == "moss"

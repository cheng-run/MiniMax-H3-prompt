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
# 否定句不误切档与命中证据（issue #167 / spec #169 C；粒度 #177 定为「句」）
#
# 现场：设计正文里一句「不出现现代塑料包装」把 12 条道具全部切到 MOSS（2026-11-04
# r2v 实跑）。切档错了不报错，只是整类的词都按另一套纪律写；所以判定必须看**命中词
# 所在的那一句**是不是禁令。取舍写明：宁可漏切、不整类误切——人还能在素材循环里
# 手动改档，而自动规则误切的整类不会自己浮出来。句边界只认句末标点与换行（#177）。
# ---------------------------------------------------------------------------

def test_hit_carries_the_sentence_it_came_from():
    """命中不只回那个词，还回它所在的句——终端要念「因为哪一句」。

    句边界只认句末标点（#177）：逗号不切，所以「瓶身贴着标签，颜色偏暖」是同一句，
    证据里带上它自己的开头，用户才对得上正文。
    """
    hit = image_routing.text_capability_hit("道具设计：瓶身贴着标签，颜色偏暖。背景是木桌")

    assert hit is not None
    assert hit.word == "标签"
    assert hit.sentence == "道具设计：瓶身贴着标签，颜色偏暖", "句按句末标点切开，逗号不切"


def test_negated_sentence_does_not_hit():
    """「不得出现现代塑料包装」是禁令不是文字需求：整类道具不许被切到 MOSS。"""
    assert image_routing.text_capability_hit("道具设计：瓶身不得出现现代塑料包装") is None


def test_affirmative_sentence_beside_a_negated_one_still_hits():
    """同一段里否定句与肯定句并存：只有肯定那一句生效（否定不污染整段）。"""
    hit = image_routing.text_capability_hit(
        "不得出现现代塑料包装。瓶身要贴一张手写标签")

    assert hit is not None
    assert (hit.word, hit.sentence) == ("标签", "瓶身要贴一张手写标签")


def test_a_negation_after_the_keyword_still_covers_the_whole_sentence():
    """否定词出现在命中词之后也算这一句的禁令：句是判定的粒度，不是位置先后。"""
    assert image_routing.text_capability_hit("道具设计：包装不得使用现代塑料") is None


@pytest.mark.parametrize("text", [
    "道具设计：不得出现现代塑料包装",
    "道具设计：不出现任何标签",
    "道具设计：不需要包装",
    "道具设计：不要文字",
    "道具设计：不使用海报",
    "道具设计：不能有招牌",
    "道具设计：禁止添加 logo",
    "道具设计：避免出现文字",
    "道具设计：没有标签",
    # 「无」只在**句首**算否定：句中的它可能是地名或半个词，所以这里另起一句来验。
    "道具设计：玻璃汽水瓶。无包装",
])
def test_chinese_negations_suppress_the_hit(text):
    """中文否定词逐条咬住（票面点名的九个词 ＋ 句首字「无」）。"""
    assert image_routing.text_capability_hit(text) is None


@pytest.mark.parametrize("sentence", [
    "no labels or packaging",
    "without any text",
    "avoid lettering",
    "never show a logo",
    "not a poster in sight",
])
def test_english_negations_suppress_the_hit(sentence):
    """英文否定词逐条咬住：MOSS/Qwen 档的正文是英文，只写中文否定词等于对那一半文本失效。"""
    assert image_routing.text_capability_hit(f"prop design: a bottle, {sentence}") is None


def test_english_negation_needs_a_word_boundary():
    """英文否定词按整词匹配：``anodized`` 里的 ``no`` 不是否定（否则每张金属道具都漏切）。"""
    hit = image_routing.text_capability_hit(
        "An anodized bottle with a printed label on the body.")

    assert hit is not None and hit.word == "标签"


def test_negation_is_per_sentence_not_per_paragraph():
    """否定只吃它所在的句：英文同段里另一个肯定句照旧命中。

    逗号不再切句（#177），所以两句之间用句号——这正是新语义：同一句里「不要 X，但要 Y」
    会一起漏切，跨句则各自判定。
    """
    hit = image_routing.text_capability_hit(
        "prop design: no labels on the cap. But a printed poster on the wall")

    assert hit is not None and hit.word == "海报"


def test_the_wu_negation_has_two_narrow_shapes():
    """「无」算否定的两种窄形态：**句首**（#167）与**紧邻命中词之前**（#177 补充裁定）。

    别的形态一律不算：放宽到「句里有『无』」会把地名（无锡）、半词与「无数标签」这类
    肯定诉求一起吞掉。要放宽得先改规格——这条钉住当前边界。
    #177 的后果：句边界变大后，冒号后的「无包装」不再自成一句；它能被否定靠的是
    「紧邻」这条，不是句首那条。
    """
    assert image_routing.text_capability_hit("道具设计：玻璃汽水瓶。无包装") is None
    assert image_routing.text_capability_hit("道具设计：瓶身无文字") is None


def test_assignment_records_the_source_sentence():
    """切档记录里存来源句：事后要能回答「当初凭什么切的这一档」。"""
    result = image_routing.assignment("prop", texts=["道具设计：瓶身包装印着品牌色块"])

    assert result == {
        "model": "moss", "source": "auto", "keyword": "包装",
        "evidence": "道具设计：瓶身包装印着品牌色块",
    }


def test_assignment_leaves_evidence_empty_when_no_sentence_decided_it():
    """手动与配置默认没有来源句：证据字段留空，不许拿提示词正文凑数。"""
    manual = image_routing.assignment(
        "prop", texts=["瓶身包装印着品牌色块"], manual="z_image")
    configured = image_routing.assignment("prop", texts=["一把青铜钥匙"])

    assert manual["evidence"] == "" and configured["evidence"] == ""


def test_described_appends_the_source_sentence():
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


# ---------------------------------------------------------------------------
# 否定按「句」判定，不按「分句」（issue #177，2026-11-05 裁定）
#
# #167/#172 按「，、；：」切分句，于是顿号并列的枚举不继承前面的否定：设计正文里
# 「不得出现现代塑料包装、标签」切完只剩「标签」自成一段，句内没有否定词，整类道具
# 又被切到 MOSS。主树 15 个会话状态里「否定词后 40 字内出现顿号」共 689 处（占多数
# 的真实形态），不是边角案例。现裁定：**整句**含否定词即整句跳过，句边界只认句末
# 标点（。！？.!?）与换行；同句里「不要 X，但要 Y」会一起漏切——沿用 #172 的
# 「宁可漏切、不整类误切」。
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("sentence", [
    # 样本全部取自主树 output/sessions/*/GEN*/session-state.json 的真实设计正文
    "不出现与回忆有关的标签、照片或信物",
    "不出现地域性糕点、馅料或商业包装",
    "不使用姓名、记号或家庭标签建立主角",
    "不得出现中文文字、中文车牌或中国城市设施",
    "不能出现发光书页、魔法符文或清晰可读文字",
    "不出现真实品牌、logo、夸张霓虹或童话化装饰",
    "不出现第二个人、车流或夸张招牌",
    "没有人物、没有文字、标识或品牌痕迹",
])
def test_an_enumeration_after_a_negation_inherits_it(sentence):
    """顿号并列项整体受否定约束：命中词在顿号之后也算这一句的禁令（#177 主诉）。"""
    assert image_routing.text_capability_hit(f"道具设计：{sentence}") is None


def test_a_comma_separated_english_enumeration_inherits_the_negation():
    """英文 ``without labels, text, or logos`` 同样整句受否定——两套规则不分叉。"""
    assert image_routing.text_capability_hit(
        "prop design: a plain bottle without labels, text, or logos") is None


def test_the_ticket_acceptance_samples():
    """票面「验收草案」原样钉住：禁令枚举不切档，另一句肯定诉求照旧切（#177）。"""
    assert image_routing.text_capability_hit(
        "道具设计：不得出现现代塑料包装、标签") is None

    hit = image_routing.text_capability_hit(
        "道具设计：不得出现现代塑料包装、标签。需要出现年代感的招牌")

    assert hit is not None
    assert (hit.word, hit.sentence) == ("招牌", "需要出现年代感的招牌")


# ---------------------------------------------------------------------------
# 「无X」否定：命中词紧邻前一个字是「无」（#177 补充裁定，2026-11-05）
#
# 冒号不再是句边界之后，真实写法「**行动事实**：无对白、无字幕」这样的行整句没有
# 否定词表里的词、也不以「无」开头，命中词却紧跟在「无」后面——补这一条窄规则：
# **只认紧邻**，不新增切句边界。主树真实形态取样如下。
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "**行动事实**：无对白、无文字",              # 票面形态 ＋ 真实命中词
    "环境：无飞虫、无鸟影、无文字标识",          # 票面形态 ＋ 真实命中词
    "暗环境下唯一光源；屏幕无文字、无图标、无通知",
    "热饮使用无标识、无文字、无图案的简洁纸杯",
    "机身无品牌、无logo、无系统界面元素",
    "**可识别细节**：**无包装、裸露单条**",
    "全门无贴纸，无文字",
    "街角无招牌门面",
])
def test_wu_immediately_before_the_hit_suppresses_it(text):
    """「无文字」「无包装」「无logo」「无招牌」都算否定：那一处命中跳过（#177 补充）。"""
    assert image_routing.text_capability_hit(text) is None


def test_the_wu_rule_needs_adjacency_not_just_presence():
    """「无」必须在**紧邻前一字**：``无数标签`` 是肯定（数不清的标签），照旧命中。

    放宽成「句子里有『无』就否定」会把这类肯定诉求一起吞掉——窄口径是刻意的。
    """
    hit = image_routing.text_capability_hit("画面里有无数标签贴满墙面")

    assert hit is not None and hit.word == "标签"


def test_the_wu_rule_does_not_reach_across_an_intervening_word():
    """边界：「无任何标签」不算否定（规则只认紧邻，不认「无…X」的中间字）。

    这是补充裁定的窄口径：要覆盖「无任何」「无其它」得先改规格。用例钉住当前边界，
    免得将来有人顺手把它放宽又没人说得清是哪次改的。
    """
    assert image_routing.text_capability_hit("房间无任何标签").word == "标签"

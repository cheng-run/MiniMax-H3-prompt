"""生图提示词纪律与警告级检查（issue #68 / ADR 0018）。

两层，都只断言**外部行为**：

1. **规则文本**：唯一可写来源是 ``prompts/image_prompt_engineer.md`` 末尾那一节。
   加载口把整份 .md 读给模型，所以纪律必须原样到达 ``load_role_prompt`` 的返回值；
   AI 改词（#72）从同一个读取口取，不许再抄一份。守卫用**变异检验**：
   把来源里的一条删掉，判据必须报红（永不为红＝没有守卫）。
2. **检查器**：五类违规（负向表述、权重语法、质量套话、超长、方位缺锚点）各报一条
   警告；合法文本零警告；中英文样本都要咬得住——本仓有过「单测样本全英文、中文路径
   没覆盖」的结构性漏测。检查只警告，不阻断出词，也不改写产物。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from minimax_h3_prompt import image_config
from minimax_h3_prompt.agents import image_prompt_agent_key, load_role_prompt
from minimax_h3_prompt.brief_parser import Brief
from minimax_h3_prompt.graph import nodes
from minimax_h3_prompt.tools import image_prompt_discipline as discipline
from minimax_h3_prompt.tools.image_prompt_discipline import (
    check_image_prompt,
    discipline_text,
)

PROMPTS_DIR = Path(__file__).parents[1] / "src" / "minimax_h3_prompt" / "prompts"
ROLE_MD = PROMPTS_DIR / "image_prompt_engineer.md"
# issue #87 起纪律按模型分文件：本文件考的八条属于 **Z-Image 档**（其余两档由
# tests/test_image_model_discipline.py 钉）。角色 .md 里只剩一条指针。
ZIMAGE_DISCIPLINE_MD = discipline.discipline_path("z_image")

# 五类违规的稳定代号。测试认代号而不是完整文案：文案会为可读性微调，
# 代号是下游（#72 与将来的展示层）真正依赖的契约。
NEGATIVE = "NEGATIVE_STATEMENT"
WEIGHT = "WEIGHT_SYNTAX"
CLICHE = "QUALITY_CLICHE"
TOO_LONG = "PROMPT_TOO_LONG"
NO_ANCHOR = "SPATIAL_NO_ANCHOR"


def _codes(prompt: str) -> list[str]:
    return [warning.code for warning in check_image_prompt(prompt)]


# ---------------------------------------------------------------------------
# 1. 检查器：五类各报一条，合法文本不报（AC2）
# ---------------------------------------------------------------------------

def test_five_violation_kinds_each_report_exactly_one_warning():
    """一条同时踩五类的提示词：五类都报，且**每类只报一条**（不是按出现次数刷屏）。

    负向词故意出现两次、方位词也出现两次：检查器按类别收口，用户要的是
    「这条提示词犯了哪几类」，不是逐词清单。
    """
    prompt = (
        "左侧有一个女孩，右边有一扇门，不要有阴影，别让她看镜头。"
        "((红裙:1.3))，masterpiece, best quality, 8K。"
        + "细致的庭院石阶与木门，墙面有斑驳的涂料。" * 60
    )

    codes = _codes(prompt)

    assert sorted(codes) == sorted([NEGATIVE, WEIGHT, CLICHE, TOO_LONG, NO_ANCHOR])
    assert len(codes) == len(set(codes)), "同一类违规只该报一条警告"


def test_legal_chinese_prompt_reports_nothing():
    """合法中文样本：锚点＋方位＋距离写全了就不该误报（误报会训练用户忽略警告）。"""
    prompt = (
        "浅木色床头柜上放着一盏暖黄夜灯，灯罩是磨砂玻璃。"
        "穿红裙的女孩站在床头柜的右侧，距离柜沿约一掌宽。"
        "她的左手握着一只白色搪瓷杯，杯口冒着热气。"
        "桌面上有三片不同深浅的绿叶。"
    )

    assert check_image_prompt(prompt) == []


def test_legal_english_prompt_reports_nothing():
    """合法英文样本：``to the right of`` 有锚点，不能被当成裸方位词。"""
    prompt = (
        "A red-dress girl stands to the right of the nightstand, one palm away "
        "from its edge. She holds a white enamel cup in her left hand."
    )

    assert check_image_prompt(prompt) == []


def test_english_violations_are_caught():
    """英文同样要咬得住：负向、权重、套话、裸方位各一条。"""
    prompt = "A girl on the left, no shadows, masterpiece, (red dress:1.2)."

    codes = _codes(prompt)

    assert NEGATIVE in codes
    assert WEIGHT in codes
    assert CLICHE in codes
    assert NO_ANCHOR in codes


def test_bare_bu_is_not_treated_as_negation():
    """「不」单独一个字不当负向词：``不同深浅`` / ``不断`` 是合法描述。

    过宽的 ``不`` 会让几乎每条中文提示词都报警，警告就失去意义。
    """
    assert check_image_prompt("桌面上有三片不同深浅的绿叶。") == []
    assert check_image_prompt("窗外的雨在不断落下。") == []


def test_length_is_counted_in_visible_characters():
    """长度按**非空白字符**数：中文一字算一字，空格与换行不计。

    600 字整不报，601 字报——边界就是纪律写的「约 600 字内」。
    """
    assert _codes("甲" * discipline.MAX_PROMPT_CHARS) == []
    assert _codes("甲" * (discipline.MAX_PROMPT_CHARS + 1)) == [TOO_LONG]
    assert _codes("甲 " * discipline.MAX_PROMPT_CHARS) == []


def test_anchored_direction_does_not_warn_but_bare_direction_does():
    """方位缺锚点：``桌子的左侧`` 合格；``左侧有一把椅子``（句首裸方位）报警。"""
    assert NO_ANCHOR not in _codes("椅子在桌子的左侧。")
    assert NO_ANCHOR not in _codes("画面中央有一座石塔。")
    assert NO_ANCHOR in _codes("左侧有一把椅子。")
    assert NO_ANCHOR in _codes("角落里有三只木箱。")
    assert NO_ANCHOR in _codes("左上角有一盏吊灯。")
    assert NO_ANCHOR not in _codes("画面左上角有一盏吊灯。")


def test_warning_messages_name_the_offending_text():
    """警告要能被用户直接行动：文案里带命中的原词，而不是只说「违反纪律」。"""
    warnings = check_image_prompt("不要有阴影。")
    assert warnings and "不要" in warnings[0].message


# ---------------------------------------------------------------------------
# 2. 规则文本单一来源 + 守卫的变异检验（AC1/AC4）
# ---------------------------------------------------------------------------

def _rule_lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if re.match(r"^\d+\.\s", line)]


def _discipline_is_complete(text: str) -> bool:
    """守卫判据：八条编号纪律齐全，且每条的关键概念都在。

    这条谓词本身要被变异检验（见下一条用例）：来源删掉一条时它必须为 False。
    概念词是**独立于来源的期望值**——不能从 discipline_text() 现算，否则来源改坏
    它跟着一起变，永远绿。
    """
    rules = _rule_lines(text)
    if len(rules) != 8:
        return False
    # 每条纪律的关键概念（ADR 0018 的八条顺序）；只认概念，不逐字抄全文——
    # 逐字抄一份就是第二份可写来源，来源微调时测试会无意义地红。
    expected = (
        ("看得见",),
        ("证据",),
        ("锚点", "方位"),
        ("可数",),
        ("正向",),
        ("前置", "复述"),
        ("短",),
        ("600", "权重", "masterpiece"),
    )
    for rule, concepts in zip(rules, expected):
        if not all(concept.lower() in rule.lower() for concept in concepts):
            return False
    return True


def test_the_eight_rules_are_the_whole_zimage_discipline_file():
    """八条纪律是 Z-Image 档纪律文件的**全部内容**（issue #87 起按模型分文件）。"""
    text = ZIMAGE_DISCIPLINE_MD.read_text(encoding="utf-8").rstrip()

    assert text == discipline_text(), "纪律文件正文与读取口返回的不一致"
    assert _discipline_is_complete(discipline_text()), "Z-Image 档不是完整的八条纪律"
    # 角色 .md 里只剩一条指针：纪律正文留在那里就会成为第二份可写来源（三档并存后
    # 更糟——公共段出现一套写法，模型不知道听哪份）。
    assert ROLE_MD.read_text(encoding="utf-8").count(discipline.DISCIPLINE_HEADING_PREFIX) == 1, \
        "角色提示词里仍有纪律正文（或指针重复）"


def test_discipline_reaches_the_role_prompt():
    """规则文本必须到达加载口返回值（模型真正读到的 system prompt）。

    这是「规则在、接线不在」的守卫：只写在文件里但加载口没读它，模型永远看不到。
    """
    assert discipline_text() in load_role_prompt("image_prompt_engineer", image_model="z_image")


def test_guard_bites_when_the_source_loses_a_rule():
    """变异检验：把来源里第 8 条删掉，守卫判据必须报红。

    永不为红的守卫等于没有守卫。这里直接对谓词喂坏文本，证明它咬得住；
    若有人真的删掉 .md 里的一条，上面的用例会红。
    """
    healthy = discipline_text()
    assert _discipline_is_complete(healthy)

    broken = re.sub(r"^8\.\s.*$", "", healthy, flags=re.MULTILINE)
    assert not _discipline_is_complete(broken), "守卫对缺一条的来源仍然放行（永不为红）"


def test_discipline_text_is_not_copied_into_other_role_prompts():
    """纪律正文只准有一份可写来源：别的角色 .md 不许手抄一段（抄了就会各自漂移）。"""
    needle = _rule_lines(discipline_text())[0]
    for path in sorted(PROMPTS_DIR.glob("*.md")):
        if path == ROLE_MD:
            continue
        assert needle not in path.read_text(encoding="utf-8"), \
            f"{path.name} 手抄了生图提示词纪律（应只留指针或完全不提）"


# ---------------------------------------------------------------------------
# 3. 接线：出词后打印警告，但不阻断、不改写产物（AC3/AC5）
# ---------------------------------------------------------------------------

def _brief() -> Brief:
    # 资产图节点只在多参路线出词（issue #78 源头守卫）：本文件考的是节点出词后的
    # 纪律警告，走 base 的话节点直接早退，一次请求都不会发。
    return Brief(mode="ref", variant="T2VA", duration=5.0, style="写实", plot="秋日庭院")


def _assets_json(name: str, prompt: str) -> str:
    # 锁死清单必须是正文原文片段（issue #53 的不变式），所以取提示词开头那三个字，
    # 不另写一项「名字」——夹具不合规时节点会在检查器之前先停下来，测不到接线。
    return json.dumps([
        {"name": name, "positive_prompt": prompt, "locked_features": [prompt[:3]]},
    ])


def _run_node(tmp_path, monkeypatch, prompt: str) -> dict:
    """跑一次人物出词节点，**钉在 Z-Image 档**：本文件的五类警告是那一档的口径。"""
    config_path = tmp_path / "image.yaml"
    config_path.write_text("character: z_image\n", encoding="utf-8")
    monkeypatch.setattr(image_config, "IMAGE_CONFIG_PATH", config_path)
    monkeypatch.setattr(nodes, "run_agent", lambda agent, message: _assets_json("甲", prompt))
    node = nodes._make_image_prompt_node(
        {image_prompt_agent_key(m): object() for m in image_config.IMAGE_MODEL_KEYS},
        "人物", "character_design", "character_image_prompts",
    )
    return node({"brief": _brief(), "character_design": "设计正文"})


def test_node_prints_warnings_but_still_returns_the_prompt(tmp_path, monkeypatch, capsys):
    """违规照常出词：警告只打印，节点仍返回原提示词（只警告不阻断）。"""
    bad = "左上角有一盏灯，不要有阴影，masterpiece。"
    out = _run_node(tmp_path, monkeypatch, bad)

    printed = capsys.readouterr().out
    assert "[警告]" in printed, "节点没有把纪律警告打到用户面前"
    assert "负向表述" in printed and "质量套话" in printed, \
        f"警告缺类别：{printed!r}"

    entries = out["character_image_prompts"]
    assert entries[0]["positive_prompt"] == bad, "检查器改写了提示词（只该警告）"


def test_node_is_quiet_on_a_legal_prompt(tmp_path, monkeypatch, capsys):
    """合法提示词不许刷警告：警告要有信息量，不能每条都报。"""
    _run_node(tmp_path, monkeypatch, "穿红裙的女孩站在床头柜的右侧，距离柜沿约一掌宽。")

    assert "[警告]" not in capsys.readouterr().out


def test_checker_does_not_mutate_the_prompt_string():
    """纯函数：检查前后文本逐字相同（既有产物不被自动改写）。"""
    prompt = "左上角有一盏灯，不要有阴影。"
    before = prompt
    check_image_prompt(prompt)
    assert prompt == before


# ---------------------------------------------------------------------------
# 4. 检查按模型分口径（issue #87）
# ---------------------------------------------------------------------------

def test_non_zimage_models_only_get_the_generic_cliche_check():
    """Qwen／MOSS 档只跑「质量套话」：其余四类的依据都是 Turbo 自己的事实。

    负向、权重、超长、方位四类都建立在 ADR 0018 的实测上（CFG=1、编码器不认权重、
    长文稀释注意力、空间关系是 Turbo 弱项）。拿它们判 Qwen 要的英文长文，会刷出一片
    假警告——真问题被淹掉，警告失去信息量（本仓对警告的唯一要求就是有信息量）。
    """
    long_english = (
        "A girl in a red dress stands beside a wooden cabinet in a quiet room. "
        * 20
    ) + "8K"

    zimage_codes = _codes(long_english)
    assert TOO_LONG in zimage_codes, "Z-Image 档不再报超长——判据被削弱了"
    assert CLICHE in zimage_codes

    assert [w.code for w in check_image_prompt(long_english, model="qwen")] == [CLICHE]
    assert [w.code for w in check_image_prompt(long_english, model="moss")] == [CLICHE]


def test_checker_rejects_an_unknown_model():
    """未知模型不许静默按默认档判：那会让「按档检查」变成一句空话。"""
    with pytest.raises(ValueError, match="flux"):
        check_image_prompt("随便一条", model="flux")

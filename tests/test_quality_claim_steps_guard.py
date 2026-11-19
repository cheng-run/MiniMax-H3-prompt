"""画质／听感结论必须带采样档位与分辨率（#336，父 spec #332）。

**为什么需要它**：采样配置是画质对比里的**独立变量**——2026-11-18 验货跑用 25 步
（包内控件默认值）、成片 864×480，而同期的 A/B 与真实产物是别的档；只写「画质还行」
的结论，隔一轮就没人分得清两次数字是不是同一套条件（#296 的 F3「采样两套口径」）。
规则本身只有一条，写在 ``CODING_STANDARDS.md`` 第三节（证据与断言）——它是给人和
agent 读的判据，机器能守的部分只有「这条规则还在、数字还对得上真源」。

**为什么数字要现算而不写死**：导演包那一档的步数从 ``director.pack.PACK_WIDGETS``
派生。常量一改而规则行没跟着改，规则就从「判据」静默变成「假陈述」——记忆
``guard-shape-must-derive-from-constants`` 的同族形态。

**实测（2026-11-19，本机）**：``<ComfyUI>/output/视频/MiniMax-H3`` 下 72 份真实产物
按 PNG 元数据统计为 4/8/14/24/32/33 步都有。所以「仓内真实产物」这一档**不是一个
固定数字**：票面与 G6 点名的 24 步只是其中一对样本，规则行必须写成「以元数据为准」。

变异检验：把规则行里的「与分辨率」删掉，本文件的用例必须变红（``tests/test_mutation_anchors``
保证锚点唯一、目标用例存在）。
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STANDARDS = ROOT / "CODING_STANDARDS.md"
ADR_0003 = ROOT / "docs" / "adr" / "0003-flicker-threshold-is-config-dependent.md"


def _rule_rows() -> list[str]:
    """``CODING_STANDARDS.md`` 表格里讲「画质」的行（规则行的唯一锚点）。"""
    text = STANDARDS.read_text(encoding="utf-8")
    return [line for line in text.splitlines() if line.startswith("|") and "画质" in line]


def _rule_text() -> str:
    rows = _rule_rows()
    assert rows, (
        "CODING_STANDARDS.md 里找不到「画质」规则行。这条规则的整行被删掉了——"
        "两次画质结论从此不可比，而没有任何检查会报（#336）")
    return "\n".join(rows)


def test_quality_claims_rule_requires_steps_and_resolution() -> None:
    """判据本体：一档采样档位、一档分辨率，两句都在。"""
    rule = _rule_text()
    assert "steps" in rule, f"规则行没点名采样档位（steps）：{rule}"
    assert "分辨率" in rule, f"规则行没点名分辨率：{rule}"
    assert "听感" in rule, f"规则行的适用范围缺「听感」：{rule}"


def test_rule_names_each_tier_with_its_source() -> None:
    """三套口径各自给出处，读者才判得出「这次是哪一档」。"""
    rule = _rule_text()
    # 导演包那一档的数字从常量现算：常量改了而规则没改，这条当场红。
    from minimax_h3_prompt.director.pack import PACK_WIDGETS

    assert f"{PACK_WIDGETS['steps']} 步" in rule, (
        f"规则行里导演包那一档的步数与 ``PACK_WIDGETS['steps']``"
        f"（{PACK_WIDGETS['steps']}）对不上：{rule}")
    # ADR 0003 的 A/B 是 8 步：规则行写了 8 步，ADR 自己也得仍写着 8 steps。
    assert "ADR 0003" in rule, f"规则行没点名 8 步 A/B 的 ADR：{rule}"
    assert "8 步" in rule, f"规则行没写 ADR 0003 那一档是 8 步：{rule}"
    adr_text = ADR_0003.read_text(encoding="utf-8")
    assert "八步样本" in adr_text or "8 steps" in adr_text, (
        "ADR 0003 里已经读不到八步／8 steps——规则行引用的那一档出处变了，先核实再改规则")
    # 真实产物那一档：以产物元数据为准，不是固定数字。
    assert "loadout_manifest" in rule, f"规则行没给真实产物档的读口：{rule}"
    assert "不是一个固定数字" in rule or "不固定" in rule, (
        f"规则行把真实产物档写成了单一数字，而本机实测 4/8/14/24/32/33 都有：{rule}")


def test_rule_cross_references_the_spec_and_acceptance_ticket() -> None:
    """与 #332 的「验收物证必须带 steps」互指：出处列必须点名那张验收票。"""
    rule = _rule_text()
    assert "#332" in rule, f"规则行没指回父 spec #332：{rule}"
    assert "#341" in rule, f"规则行没指向它的第一处消费者（验收票 #341）：{rule}"

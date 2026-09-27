"""Agent 角色提示词（``src/minimax_h3_prompt/prompts/*.md``）与官方规范的一致性。

这些 .md 是 LLM 直接读到的文本——它们说错话，产出就跟着错，而链条上没有别的东西会报红。

2026-09-23（issue #8）的教训：`prompt_engineer.md` 把官方**明令要求**的 edge-stability 句
列为「自创结构」禁令，而同一批改动里分段模板又必须要求它——同一句话，一处禁一处要。
本文件钉住两条：

1. 「角色提示词不得与官方规范相反」（issue #8）；
2. 「官方原文只准有一处可写来源，角色 .md 只留指针、原文由加载处注入」（issue #35）——
   第 2 条原先靠人工抄写维持，而抄写会漂移：本文件因此**同时**钉住「原文到得了模型眼前」
   （查加载口返回值）与「.md 里没有第二份手抄」（查文件本身）。
"""
from pathlib import Path

from minimax_h3_prompt.agents import (
    OFFICIAL_VERBATIM_MARKER,
    OFFICIAL_VERBATIM_ROLES,
    load_role_prompt,
)
from minimax_h3_prompt.tools.h3_validator import ALIGN_TEMPLATES, EDGE_STABILITY_SENTENCE

# 与加载口各自独立地定位 .md：刻意不用 agents.PROMPTS_DIR，免得「目录常量写错」时两边一起错
PROMPTS_DIR = Path(__file__).parents[1] / "src" / "minimax_h3_prompt" / "prompts"


def test_prompt_engineer_requires_edge_stability_sentence():
    """整片提示词工程师必须要求块尾 edge-stability 句，且不得再禁它。"""
    prompt = load_role_prompt("prompt_engineer")

    assert EDGE_STABILITY_SENTENCE in prompt, \
        "prompt_engineer.md 未要求官方 edge-stability 原句（每个镜头块都要以此收尾）"
    # 这份 .md 不该再用「防波纹咒语」框架提它——那正是 2026-09-22 误判的原话，
    # 留着它会把这个要求重新读成自创结构
    assert "防波纹" not in prompt, \
        "prompt_engineer.md 仍以「防波纹咒语」框架描述官方要求（那不是自创结构）"


def test_prompt_engineer_carries_every_official_verbatim_line():
    """官方原文必须**到达**加载口返回值：三行对齐指令 + edge-stability 原句，逐字符在场。

    缝在加载口：`load_role_prompt` 的返回值就是进 system prompt 的那份文本（#30 Testing
    Decisions 缝 1）。四条都要：少一条就有一种变体的首行、或每个镜头块的收尾句没依据。
    """
    prompt = load_role_prompt("prompt_engineer")

    for variant, line in ALIGN_TEMPLATES.items():
        assert line in prompt, f"prompt_engineer 拿不到 {variant} 首行对齐指令原文"
    assert EDGE_STABILITY_SENTENCE in prompt, "prompt_engineer 拿不到 edge-stability 原句"


def test_official_verbatim_block_stays_off_other_roles():
    """没拿原文块的角色就是拿不到——与词表白名单同一纪律：不声明即拿不到。

    甄别判据是「这个角色会不会自己逐字符写这段原文」：审查 / 质检角色是拿校验器工具去判，
    写段那两条路径是代码拼的请求（`segment_prompts` 直接插值常量），都不需要这条注入。
    全线加白名单会让每个角色的上下文都背上四段与自己无关的英文原文。
    """
    for role in ("qa", "storyboard", "cinematographer"):
        assert EDGE_STABILITY_SENTENCE not in load_role_prompt(role), \
            f"{role} 不该拿到官方原文块"


def test_every_declared_role_leaves_a_pointer_to_the_injected_block():
    """白名单里**每个**角色的 .md 都要留下指向注入块的指针。

    只钉 prompt_engineer 一份的写法有个口子：将来往 ``OFFICIAL_VERBATIM_ROLES`` 里加角色
    却忘了在它的 .md 里写指针，原文虽然送出去了、模型却不知道文末那块是干什么用的，
    而没有任何东西会报红。判据取 ``OFFICIAL_VERBATIM_MARKER``——改块标题而不改 .md 指针、
    或反之，同样报红。
    """
    for role in sorted(OFFICIAL_VERBATIM_ROLES):
        text = (PROMPTS_DIR / f"{role}.md").read_text(encoding="utf-8")
        assert OFFICIAL_VERBATIM_MARKER in text, \
            f"{role}.md 没有指向加载处注入块（{OFFICIAL_VERBATIM_MARKER}）的指针"


def test_no_role_prompt_md_hardcodes_official_text():
    """角色 .md 一律不得再手抄官方原文（issue #35）——原文只留指针，由加载处注入。

    为什么要查**文件**而不是加载口：注入块本来就该出现在返回值里（上一条用例钉它），
    这条钉的是「第 2 份可写来源已经消失」。判据只能是文件本身——手抄的那份若留着，
    改了常量它不会跟着动，而返回值里两份都在场，谁也看不出哪份是旧的。
    """
    hardcopies = (EDGE_STABILITY_SENTENCE, *ALIGN_TEMPLATES.values())

    for path in sorted(PROMPTS_DIR.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        for copied in hardcopies:
            assert copied not in text, \
                f"{path.name} 仍手抄着官方原文：{copied[:48]}…（应改为指针，原文由加载处注入）"

    # 指针必须留下——「见官方规范」这种含糊说法指不到注入块，模型不知道文末那块是干嘛的。
    # 逐角色的指针由 test_every_declared_role_leaves_a_pointer_to_the_injected_block 钉住。

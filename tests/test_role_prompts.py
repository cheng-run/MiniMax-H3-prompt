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
import re
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
        "prompt_engineer.md 未要求 edge-stability 原句（每个镜头块都要以此收尾）"
    # 这份 .md 不该再用「防波纹咒语」框架提它——那正是 2026-09-22 误判的原话，
    # 留着它会把这个要求重新读成自创结构
    assert "防波纹" not in prompt, \
        "prompt_engineer.md 仍以「防波纹咒语」框架描述官方要求（那不是自创结构）"


def test_prompt_engineer_carries_every_official_verbatim_line():
    """逐字符原文必须**到达**加载口返回值：三行对齐指令 + edge-stability 原句，逐字符在场。

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


# .md 里「见文末【…原文】」形式的指针。只认方括号里以「原文」收尾的写法——那是
# 指针正在点名注入块的唯一形状，比较目标取自 ``OFFICIAL_VERBATIM_MARKER``。
_POINTER = re.compile(r"【[^】]*原文】")


def test_pointer_sentences_name_the_injected_block_verbatim():
    """指针必须逐字等于注入块标题：指向一半在文本上完全合法，模型却找不到那一块。

    2026-11-01 复审实测：``prompt_engineer.md`` 写着「见文末【逐字符原文】」，而注入块
    标题是「【官方逐字符原文】」。旧用例只断言标记串**出现过**（:14 的指针就满足），
    于是这条断链没有东西会报红——指针等于没写。
    """
    for path in sorted(PROMPTS_DIR.glob("*.md")):
        pointers = set(_POINTER.findall(path.read_text(encoding="utf-8")))
        wrong = pointers - {OFFICIAL_VERBATIM_MARKER}
        assert not wrong, (
            f"{path.name} 的指针没有逐字指向注入块：{sorted(wrong)}"
            f"（应为 {OFFICIAL_VERBATIM_MARKER!r}）")


# ── 剧情层体裁声明「三跳」（issue #129 / L1-01、L1-10、L1-11） ──
# 一行文本跨三个角色传递：编剧写 → 分镜师原样抄 → 规划师读。三处任何一处丢掉行前缀，
# 链路就断，而链条上没有别的东西会报红（没有人核对分镜表首行是不是编剧原话）。
# 判据只取一个常量：提示词里「形式：」的写法三处必须一致；档名与段数带也只在
# 编剧/规划师两端各出现一次，改一端不改另一端必须报红。
FORM_LINE_PREFIX = "形式："
GENRE_TIERS = ("单段", "短叙事", "长叙事")


def _prompt_text(role: str) -> str:
    return (PROMPTS_DIR / f"{role}.md").read_text(encoding="utf-8")


def test_genre_form_line_chain_closes_across_three_roles():
    """编剧写、分镜师原样抄、规划师读——断在哪一跳这里就红在哪一跳。"""
    writer = _prompt_text("screenwriter")
    board = _prompt_text("storyboard")
    planner = _prompt_text("segment_planner_director")

    assert FORM_LINE_PREFIX in writer, "编剧提示词没有要求写体裁声明行"
    for tier in GENRE_TIERS:
        assert tier in writer, f"编剧提示词没列出体裁档「{tier}」"
    assert FORM_LINE_PREFIX in board, "分镜师提示词没有要求抄写体裁声明行"
    assert "原样" in board, "分镜师提示词没写「原样抄写」（抄写而非重写是 #129 的判据）"
    assert FORM_LINE_PREFIX in planner, "规划师提示词没有读取体裁声明行"
    for tier in GENRE_TIERS:
        assert tier in planner, f"规划师提示词没给出体裁档「{tier}」的段数带"
    # 缺声明时的兜底必须写死在规划师里；「短叙事」这一档名也承自同一条链路
    assert "兜底" in planner and "短叙事" in planner and "2–6 段" in planner, \
        "规划师缺「无声明按短叙事（2–6 段）兜底」的口径"


def test_structural_and_shot_layer_rules_land_in_their_roles():
    """结构层落导演 / 镜头层落分镜师 / 段层落规划师——错位或漏条都报红。"""
    vid = _prompt_text("director")
    board = _prompt_text("storyboard")
    planner = _prompt_text("segment_planner_director")

    # 结构层四条（L1-02…L1-05）：场景转向 / 价值反转次数 / 动机可回溯 / 位置变化前先交代
    for phrase in ("每一场", "价值反转", "触发点", "先交代"):
        assert phrase in vid, f"导演提示词缺结构层条目关键词「{phrase}」"

    # 镜头层三条（L1-07…L1-09）：一镜一维度 / 只变距离角度改用运镜 / 3–8 秒且不得均分
    assert "一个维度" in board, "分镜师提示词缺「一镜只承载一个维度」"
    assert "改用机位运动" in board, "分镜师提示词缺「只变距离或轻微角度就改用运镜」"
    assert "3–8 秒" in board and "均分" in board, "分镜师提示词缺镜头长度带或均分禁令"

    # 段层（L1-11…L1-13）：4–15 秒单段带与 end_hook 必须与起点不同
    assert "4–15 秒" in planner, "规划师提示词缺 4–15 秒段长带"
    assert "end_hook" in planner, "规划师提示词缺 end_hook 约束"


def test_planner_forbids_a_fake_camera_cut_at_the_same_location():
    """`camera_cut` 必须换空间/时间，同一地点再切一次＝假切镜（P3，2026-11-02 验收）。

    #137 实跑：段1 停在便利店雨棚，段2 又判 `camera_cut` 切到同一个雨棚——切镜没有
    带来任何新信息，尾帧词也被迫从「站立/手插袋」大改成「骑行离开」。规划器违反的是
    它自己的类型契约，而当时提示词里没有一句拦它。
    """
    planner = _prompt_text("segment_planner_director")

    assert "假切镜" in planner, "规划师提示词没有点名「假切镜」这种错误"
    assert "同一地点" in planner and "camera_cut" in planner, \
        "规划师提示词没写「同一地点不许用 camera_cut」"
    assert "换空间" in planner or "换时间" in planner, \
        "规划师提示词没写 camera_cut 的判据（换空间或换时间）"


def test_storyboard_forbids_pasting_wordlist_examples_with_picture_refs():
    """分镜表不得照抄词表例句，也不得写 `Picture N` 落点句（P9，2026-11-02 验收）。

    #138 实跑：分镜表末行多出一句「The push-in settles into the pose … established by
    Picture 2 …」——来源是 `knowledge/film-language-video.md:57` 的官方例句被原样抄走。
    剧情链阶段没有 Picture 编号，这句会随分镜表进后续请求。
    """
    board = _prompt_text("storyboard")

    assert "照抄" in board, "分镜师提示词没禁止照抄词表例句"
    assert "Picture N" in board, "分镜师提示词没点名不许写 Picture N 落点句"


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

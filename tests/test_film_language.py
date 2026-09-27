"""电影语言真源（视频半）的接线与内容纪律（issue #31 选词侧 / #32 落笔侧）。

三个断言轴，都只断言**外部行为**（模型真的收到了什么）。**例外只有两处**——
票面明文要求的中英对照与「按消费者取切片」，那是产物规格而不是内部措辞；
除此之外不断言真源的遣词造句，真源会持续微调，测试钉的是「切片到了没有」。先例：``tests/test_role_prompts.py``
（角色提示词与官方规范一致）、``tests/test_segment_prompts.py`` 的
``test_edge_stability_reaches_both_segment_requests``（本仓到达性测试的祖宗：
只断言模块常量是「被测物自身的单测」——常量对了、请求组装时漏掉，照样不报红）。

1. **到达性**：切片真进了该拿的角色（选词侧：分镜师、摄影指导；落笔侧：整片提示词工程师）。
2. **不外溢**：不该拿到的角色拿不到。分段规划师的段尾钩子是锚帧语义校验的对照物，
   掺审美词就没法人工判定了；美术统筹的风格基调已由 brief 给出；制作人不选词。
3. **不复述**：要逐字符照抄的官方原文（edge-stability 句、帧变体锚定行）在真源里
   只留指针。复述一份就是又一份硬拷贝——本仓已经吃过「同一句官方原文 3 份硬拷贝」的亏。
"""
from __future__ import annotations

from minimax_h3_prompt import film_language
from minimax_h3_prompt.agents import ROLE_KEYS, load_role_prompt

# 选词侧（issue #31）：选词真正发生的那两个角色
CHOOSING_ROLES = ("storyboard", "cinematographer")
# 落笔侧（issue #32）：写最终正文的角色。两条写段路径不是角色（请求由
# ``segment_prompts`` 拼），它们的到达性在 ``tests/test_segment_prompts.py`` 里钉，
# 那里能断言真实请求字符串；此处只管角色加载口这一根。
WRITING_ROLES = ("prompt_engineer",)


def _marker() -> str:
    """真源标记串 = 真源自己的标题行。

    从真源文件派生、不另抄一份常量：手抄的常量会在真源改名时静默失配，
    于是「到达性」断言变成永远为真的空断言。
    """
    marker = film_language.video_source_headline().strip()
    assert marker, "真源没有标题行——标记串取空会让所有到达性断言变成永真"
    return marker


def test_wordlist_reaches_the_two_choosing_roles():
    """分镜师与摄影指导的系统提示词里必须有真源切片（缝 1：角色提示词加载口）。"""
    marker = _marker()
    for role in CHOOSING_ROLES:
        assert marker in load_role_prompt(role), \
            f"{role}.md 的系统提示词没带上电影语言词表——选词发生的角色手里没词"


def test_wordlist_stays_out_of_roles_that_must_not_have_it():
    """不该拿到的角色一律不带切片（票面点名三个，其余白名单外的角色一并覆盖）。"""
    marker = _marker()
    allowed = CHOOSING_ROLES + WRITING_ROLES
    for role in ("segment_planner", "art_director", "producer"):
        assert marker not in load_role_prompt(role), \
            f"{role} 不该拿到词表切片（分段规划师的段尾钩子要留作可判定的末态声明）"
    for role in ROLE_KEYS:
        if role in allowed:
            continue
        assert marker not in load_role_prompt(role), f"{role} 不在消费者白名单里"


def test_wordlist_reaches_the_whole_video_prompt_engineer():
    """落笔侧·整片（issue #32）：整片提示词工程师的系统提示词里必须有真源切片。

    它是短视频唯一的落笔者，也是长视频整片提示词（阶段 2 的组装输入）的作者；
    词表不到它手上，前面分镜师与摄影指导挑好的词就落不了地。
    """
    for role in WRITING_ROLES:
        assert _marker() in load_role_prompt(role), \
            f"{role}.md 的系统提示词没带上电影语言词表——落笔的角色手里没词"


def test_writing_side_declares_one_shared_slice():
    """落笔侧三条路径声明的是**同一个**元组（issue #32）。

    整片（prompt_engineer）与两条写段路径（segment_prompts 经 "segment_writer" 取）
    各声明各的会在改动时漂移，而漂移的后果正是本票要防的「一套有、一套没有」。
    """
    assert film_language.VIDEO_SLICES["prompt_engineer"] == \
        film_language.VIDEO_SLICES["segment_writer"]


def test_writing_side_slice_carries_the_sections_reserved_for_it():
    """落笔侧拿到的是「落笔要用的那几张表」（含真源里点名留给落笔侧的两节）。

    探针取**小节标题**（它同时是 ``VIDEO_SLICES`` 的声明键），不取词表里的词：
    钉具体的词会在真源换一个更准的词时误报，而「哪一节到了谁手里」才是这里要钉的行为。
    """
    slice_text = film_language.video_slice("segment_writer")
    for head in (
        "## 运镜怎么写",
        "## 每镜六要素",
        "## 七格骨架",
        "## 词表：景别",
        "## 词表：机位角度",
        "## 词表：构图与焦点",
        "## 词表：光线",
        "## 词表：风格",   # 真源里点名「留给落笔侧」，此前无人取
        "## 写作纪律",     # 真源里点名「落笔侧一处都没提」
    ):
        assert head in slice_text, f"落笔侧切片里没有 {head}"


def test_segment_planner_instruction_has_no_wordlist():
    """分段规划师走的是**自己**的加载口，不是 load_role_prompt——两根都要钉。

    只钉 load_role_prompt 是钉了个没人走的路：真正的出站请求由 ``_load_instruction``
    组装，它变脏了测试也看不见（本仓「规则在、接线不在」的老毛病）。
    """
    from minimax_h3_prompt.segment_planner import _load_instruction

    assert _marker() not in _load_instruction()


def test_wordlist_is_bilingual():
    """视频半必须中英对照：中文输出的角色要选得到词，正文侧要拿到英文原文。

    本仓已有先例：中文输出下只给英文词表＝结构性必挂。三处抽查分别落在
    景别、机位角度、光线三个词表上。

    为什么这里**可以**钉具体的词（与「不断言真源内部措辞」看似冲突）：中英对照本身就是
    票面的验收标准（AC-3），它是**产物规格**不是内部措辞——词表里没有可断的「外部行为」，
    只能抽样断内容；换成断小节标题则等于什么都没断（标题在、词没了照样绿）。
    """
    src = film_language.video_source_text()
    for zh, en in (("特写", "close-up"), ("仰拍", "low angle"), ("逆光", "backlight")):
        assert zh in src and en in src, f"词表缺中英对照：{zh} / {en}"


def test_slice_is_per_consumer_not_the_whole_source():
    """切片按消费者取，不是「整份真源灌给所有角色」。

    光线词表只给摄影指导（画面细化要写光线方向与强度／质感）；分镜师定的是景别与构图，
    拿到一整份是噪声。未知消费者返回空串——不加白名单就等于悄悄扩权。

    探针取**小节标题**（它同时是 `VIDEO_SLICES` 的声明键），不取词表里的词：
    钉具体的词会在真源换一个更准的词时误报，而「哪一节到了谁手里」才是这里要钉的行为。
    """
    assert "## 词表：光线" in film_language.video_slice("cinematographer")
    assert "## 词表：光线" not in film_language.video_slice("storyboard")
    # 景别两边都要（分镜师定景别，摄影指导继承它）——反过来也证明切片不是按「分镜师拿全部」切
    for role in CHOOSING_ROLES:
        assert "## 词表：景别" in film_language.video_slice(role)
    assert film_language.video_slice("segment_planner") == ""
    assert film_language.video_slice("不存在的角色") == ""


def test_missing_section_is_loud(monkeypatch):
    """小节标题改了、切片声明没跟着改 → 报错，不许静默少送一节。

    本仓明确违规之一是静默失败（设计决定 #8）：词表少送一节，模型照旧产出，
    链条上没有任何东西会报红——只能靠这里大声炸掉。
    """
    import pytest

    monkeypatch.setitem(film_language.VIDEO_SLICES, "storyboard", ("这一节不存在",))
    with pytest.raises(ValueError, match="找不到小节"):
        film_language.video_slice("storyboard")


def test_source_never_quotes_the_verbatim_official_lines():
    """要逐字符照抄的官方原文只留指针，不复述（issue #30 的复述纪律）。

    甄别判据是「该处提示词现在有没有」，不是「校验器管不管」：这两处的原文已有唯一来源，
    在真源里再抄一份就是第 N 份硬拷贝（本仓吃过「同一句官方原文 3 份硬拷贝」的亏）。
    """
    from minimax_h3_prompt.tools.h3_validator import ALIGN_TEMPLATES, EDGE_STABILITY_SENTENCE

    src = film_language.video_source_text()
    assert EDGE_STABILITY_SENTENCE not in src, "真源复述了 edge-stability 原文（应只留指针）"
    for variant, line in ALIGN_TEMPLATES.items():
        assert line not in src, f"真源复述了 {variant} 锚定行原文（应只留指针）"
    # 「只留指针」得真的指出去哪取，否则删掉原文等于把这处知识一起删了
    assert "EDGE_STABILITY_SENTENCE" in src
    assert "ALIGN_TEMPLATES" in src


def test_source_is_plain_text_without_placeholders():
    """真源是逐字读入的纯文本：加载链上没有模板与插值，正文里不许出现花括号。"""
    src = film_language.video_source_text()
    assert "{" not in src and "}" not in src, "真源里出现花括号——它会与花括号插值打架"


def test_cinematographer_reason_no_longer_contradicts_the_english_body_rule():
    """摄影指导那句说反话的理由要改对，规则本身（中间产物中文）不变（issue #31）。

    原话声称「这些内容将进入 H3 正文，正文必须是中文」，与「H3 正文必须英文」
    （官方 Output Rules）直接矛盾——同一份系统提示词里两句互相打架。
    """
    prompt = load_role_prompt("cinematographer")
    assert "正文必须是中文" not in prompt, \
        "摄影指导提示词仍在声称 H3 正文必须中文（与官方 Output Rules 相反）"
    assert "H3 正文" in prompt and "英文" in prompt, \
        "修正后的理由句不见了——理由必须与「H3 正文必须英文」一致"
    assert "用中文写" in prompt, "规则本身不该变：画面细化是中间产物，仍然用中文写"

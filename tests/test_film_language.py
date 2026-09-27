"""电影语言真源（视频半 / 生图半）的接线与内容纪律（#31 选词侧 / #32 落笔侧 / #33 生图侧）。

三个断言轴，都只断言**外部行为**（模型真的收到了什么）。**例外只有两处**——
票面明文要求的中英对照与「按消费者取切片」，那是产物规格而不是内部措辞；
除此之外不断言真源的遣词造句，真源会持续微调，测试钉的是「切片到了没有」。先例：``tests/test_role_prompts.py``
（角色提示词与官方规范一致）、``tests/test_segment_prompts.py`` 的
``test_edge_stability_reaches_both_segment_requests``（本仓到达性测试的祖宗：
只断言模块常量是「被测物自身的单测」——常量对了、请求组装时漏掉，照样不报红）。

1. **到达性**：切片真进了该拿的角色（选词侧：分镜师、摄影指导；落笔侧：整片提示词工程师；
   生图侧：关键帧生图提示词工程师）。
2. **不外溢**：不该拿到的角色拿不到。分段规划师的段尾钩子是锚帧语义校验的对照物，
   掺审美词就没法人工判定了；美术统筹的风格基调已由 brief 给出；制作人不选词。
   **两份真源也不许互相串门**（issue #33）：运镜词表进了静态图就是直接违规。
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
# 生图侧（issue #33）：静态画面是**另一份真源**，目前只有一条出站路径读它。
# `image_prompt_engineer`（资产图那条路）**不在**里面：它的三个节点在
# `pipeline._FOLDED_NODES` 里从没接进链，接它属编排改动，见另一张票。
IMAGE_ROLES = ("frame_prompt_engineer",)
# 视频侧的消费者（选词侧 + 落笔侧的角色；两条写段路径不是角色，不在这里）。
VIDEO_ROLES = CHOOSING_ROLES + WRITING_ROLES


def _marker() -> str:
    """真源标记串 = 真源自己的标题行。

    从真源文件派生、不另抄一份常量：手抄的常量会在真源改名时静默失配，
    于是「到达性」断言变成永远为真的空断言。
    """
    marker = film_language.video_source_headline().strip()
    assert marker, "真源没有标题行——标记串取空会让所有到达性断言变成永真"
    return marker


def _image_marker() -> str:
    """生图半真源标记串 = 它自己的标题行（与视频侧同理：手抄常量会静默失配）。"""
    marker = film_language.image_source_headline().strip()
    assert marker, "生图半真源没有标题行——标记串取空会让到达性断言变成永真"
    return marker


def test_wordlist_reaches_every_declared_consumer_role():
    """拿到切片的角色必须真的拿到（缝 1：角色提示词加载口）。

    两轴一起钉（断言同形，分两条写只是两份要同步维护的副本）：选词侧的分镜师与
    摄影指导——选词真正发生在它们手里；落笔侧的整片提示词工程师——短视频唯一的
    落笔者、长视频整片提示词的作者，词表不到它手上，前面挑好的词就落不了地。
    两条写段路径不是角色（请求由 ``segment_prompts`` 拼字符串），它们的到达性在
    ``tests/test_segment_prompts.py`` 里钉，那里断言的是真实请求字符串。
    """
    marker = _marker()
    for role in VIDEO_ROLES:
        assert marker in load_role_prompt(role), \
            f"{role}.md 的系统提示词没带上电影语言词表切片——这个消费者手里没词"


def test_video_wordlist_stays_out_of_roles_that_must_not_have_it():
    """**视频侧**切片不该外溢（票面点名三个，其余白名单外的角色一并覆盖）。

    名字里的限定词是必要的：#33 之后 `frame_prompt_engineer` **也拿到切片**了，
    只是那份来自生图半。这里钉的是**视频侧标记串**不出现在非消费者的提示词里
    （生图角色拿到运镜词表是直接违规）；生图半的外溢由 `test_the_two_wordlists_never_cross` 钉。
    """
    marker = _marker()
    allowed = VIDEO_ROLES
    for role in ("segment_planner", "art_director", "producer"):
        assert marker not in load_role_prompt(role), \
            f"{role} 不该拿到词表切片（分段规划师的段尾钩子要留作可判定的末态声明）"
    for role in ROLE_KEYS:
        if role in allowed:
            continue
        assert marker not in load_role_prompt(role), f"{role} 不在消费者白名单里"


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


def test_writing_side_takes_every_section_of_the_source():
    """落笔侧取**全份**：真源将来新增一节时它不能静默漏掉（issue #32 裁定二）。

    ``video_slice`` 的静默失败方向是「**少给**」——它只在**已声明**的小节缺失时抛错，
    声明里没写的小节会被安静地略过。落笔侧要的是整份真源（ADR 0007 裁定二），
    所以「真源有哪些小节」与「声明了哪些小节」必须**集合相等**：将来给真源加一节，
    这条立刻报红，逼出一个显式决定（收进落笔侧，还是明确不给），而不是让新纪律
    悄悄到不了任何一个落笔者手上——本仓「规则在、接线不在」正是这类静默缺陷。

    小节头的识别在测试里**另写一遍**（不调 film_language 的私有解析）：同一约定
    的两份独立实现互相对照，比拿被测物自己的解析结果断言它自己更有意义。
    """
    heads = [
        line[len("## "):].strip()
        for line in film_language.video_source_text().splitlines()
        if line.startswith("## ")
    ]
    declared = list(film_language.VIDEO_SLICES["segment_writer"])
    assert sorted(heads) == sorted(declared), (
        "落笔侧的切片声明与真源的小节不一致——真源新增的小节不会自己进落笔侧"
        f"（真源 {sorted(heads)}／声明 {sorted(declared)}）"
    )


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


def test_choosing_slices_are_per_consumer_not_the_whole_source():
    """**选词侧**的切片按消费者取，不是「整份真源灌给所有角色」。

    光线词表只给摄影指导（画面细化要写光线方向与强度／质感）；分镜师定的是景别与构图，
    拿到一整份是噪声。未知消费者返回空串——不加白名单就等于悄悄扩权。

    名字里的限定词是必要的：#32 之后**落笔侧**恰恰相反（整份真源全取，见下一条），
    老名字「切片按消费者取、不是整份」会被读成对全体消费者的断言。

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


# ---------------------------------------------------------------------------
# 生图侧（issue #33）：另一份真源、另一个读取口
#
# 静态画面与视频是**两套语言**：运镜、剪辑、时间码、H3 三段式一律不进生图提示词。
# 所以这里钉的不只是「到了没有」，还有「有没有串门」——把视频侧切片送给生图角色，
# 或把生图半送给视频角色，都是结构性错误，而两者都不会让别的测试报红。
# ---------------------------------------------------------------------------

def test_image_wordlist_reaches_the_frame_prompt_engineer():
    """关键帧生图提示词工程师拿到的是**生图半**切片（缝 1：角色提示词加载口）。

    它是链上唯一真跑的生图角色（`_LINEAR_CHAIN` 里只有 `fl2va_frame_prompts` 一个
    生图节点）；票面明说资产图那条路不做——`image_prompt_engineer` 的三个节点在
    `_FOLDED_NODES` 里从没接进链，喂进 ComfyUI 的实际是设计正文，接它属编排改动。
    """
    marker = _image_marker()
    for role in IMAGE_ROLES:
        assert marker in load_role_prompt(role), \
            f"{role} 没拿到生图半词表——它手里没有选词依据"


def test_the_two_wordlists_never_cross():
    """两份真源不许互相串门（票面 AC-1 的「结构性防错：两份真源，不是一份切片」）。

    三条一起钉：
    1. 生图角色不许拿到**视频侧**切片——运镜词表进了静态图就是直接违规；
    2. 视频侧角色（选词侧 + 落笔侧）不许拿到生图半；
    3. 两张白名单**不相交**。这一条是给将来加消费者的人看的：加载口按
       `video_slice(role) or image_slice(role)` 取，同一个键同时出现在两边时
       视频侧会**静默**赢，生图半那份永远送不出去。
    """
    image_marker, video_marker = _image_marker(), _marker()
    for role in IMAGE_ROLES:
        assert video_marker not in load_role_prompt(role), \
            f"{role} 拿到了视频侧词表——静态图无从执行机位运动"
    for role in VIDEO_ROLES:
        assert image_marker not in load_role_prompt(role), f"{role} 拿到了生图半真源"
    both = set(film_language.VIDEO_SLICES) & set(film_language.IMAGE_SLICES)
    assert not both, (
        f"{sorted(both)} 同时出现在两份词表白名单里：加载口按 or 取第一个，"
        "视频侧会静默赢、生图半那份永远送不出去"
    )


def test_image_slice_takes_every_section_of_the_source():
    """生图侧取**全份**：真源新增一节时不能静默漏掉（同 #32 那条集合相等断言）。

    `image_slice` 的静默失败方向与 `video_slice` 一样是「少给」——它只在**已声明**的
    小节缺失时抛错。生图半目前只有一个消费者（它一个人要从景别写到风格），按节裁剪
    没有意义；但「声明 == 真源小节」仍要钉住，将来给真源加一节必须是个显式决定。

    小节头的识别在测试里**另写一遍**（不调 `film_language` 的私有解析）。
    """
    heads = [
        line[len("## "):].strip()
        for line in film_language.image_source_text().splitlines()
        if line.startswith("## ")
    ]
    declared = list(film_language.IMAGE_SLICES["frame_prompt_engineer"])
    assert heads, "生图半真源一个小节都没有——切片会退化成只剩前言"
    assert sorted(heads) == sorted(declared), (
        "生图侧的切片声明与真源的小节不一致——真源新增的小节不会自己进生图角色"
        f"（真源 {sorted(heads)}／声明 {sorted(declared)}）"
    )


def test_image_slice_needs_an_explicit_declaration():
    """不声明即拿不到：资产图那条路的提示词工程师**故意不在**生图白名单里。

    它的三个节点在 ``graph.pipeline._FOLDED_NODES`` 里从没接进链（喂进 ComfyUI 的实际是
    设计正文），接它属编排改动、另开票——所以它现在拿到空串是**结论**不是遗漏。
    视频侧的角色也不能从这个口子拿到生图半。
    """
    for consumer in ("image_prompt_engineer", "storyboard", "不存在的角色"):
        assert film_language.image_slice(consumer) == "", \
            f"{consumer} 没被声明，不该拿到生图半切片（不声明即拿不到）"


def test_image_source_keeps_the_static_hard_constraints():
    """生图侧既有硬约束必须留在真源里，且**不许把视频侧运镜词表带进来**（AC-4 / AC-1）。

    两半各一条：
    - 硬约束探头（禁运镜/剪辑/时间码/三段式、禁堆质量标签、正文中文）——词表接了、
      纪律丢了，模型就会开始写「高速推镜」；
    - 视频侧**独有的运镜白名单词**不能出现在生图半里。这些词只会因为「把视频词表
      整段抄过来」而出现，是串门的唯一泄漏口（`low angle` 这类静态同样合法的机位词
      不算——两份真源各自列各自的机位表）。

    为什么这里**可以**钉具体措辞（与「不断言真源内部遣词造句」看似冲突）：票面 AC-4
    要的就是「这几条硬约束没被破坏」，它是**产物规格**；换成断小节标题则等于什么都没断
    （标题在、约束没了照样绿）。同 `test_wordlist_is_bilingual`。
    """
    src = film_language.image_source_text()
    for rule in ("运镜", "剪辑", "时间码", "三段式", "质量标签", "中文"):
        assert rule in src, f"生图半真源丢了硬约束：{rule}"
    for leaked in ("Zoom In", "Truck Left", "Pedestal Up", "Arc Shot", "Roll Clockwise"):
        assert leaked not in src, f"生图半真源里出现了视频侧运镜白名单词 {leaked}"


def test_image_source_is_plain_text_without_placeholders():
    """生图半同样是逐字读入的纯文本：正文里不许出现花括号（同 ADR 0006 的纪律）。

    BFL 官方的 JSON 示例尤其容易诱人直接抄进来——本份改成字段表（见「结构化字段」一节）。
    """
    src = film_language.image_source_text()
    assert "{" not in src and "}" not in src, "生图半真源里出现花括号——它会与花括号插值打架"


def test_image_wordlist_is_bilingual():
    """生图半也必须中英对照：正文要求中文，专有名词保留原文。

    与视频半同一条理由（中文输出下只给英文词表＝结构性必挂）。三处抽查分别落在
    景别、构图、光线三个词表上。
    """
    src = film_language.image_source_text()
    for zh, en in (("特写", "close-up"), ("负空间", "negative space"), ("柔光", "soft lighting")):
        assert zh in src and en in src, f"生图词表缺中英对照：{zh} / {en}"


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

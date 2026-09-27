"""电影语言真源的读取口：按消费者取切片（#31 选词侧 / #32 落笔侧 / #33 生图侧）。

**为什么要有这一层**：真源（``knowledge/film-language-*.md``）是一份给人读的连续文本，
而到达模型的应该是**按角色裁剪过的片段**——分镜师定的是景别与构图，摄影指导才写光线与机位角度。
整份灌给所有角色既浪费上下文，也会让不写光线的角色去挑光线词。

**两份真源，两个读取口**：视频半（H3 正文）与生图半（静态画面）是**两套语言**，
各自一份文件、各自一张白名单（``VIDEO_SLICES`` / ``IMAGE_SLICES``）。两张白名单**不许相交**——
``agents.load_role_prompt`` 按「先视频后生图」取第一份非空的，同一个键同时出现在两边时
视频侧会静默赢。运镜词表进了静态图就是直接违规，所以这条由测试钉住。

**为什么不做 skill 运行时**：流水线角色是一次纯 API 调用，没有工具、没有按需加载、没有
「先看目录再决定读哪个」的机会。全仓只有三处会把**文件内容读进模型请求**（角色提示词目录、
分段规划师提示词、澄清器提示词），所以「把知识接进那几个真被读到的文本」才是本仓唯一有意义
的形态。裁定见 ``docs/adr/0006``。

**切片只做搬运，不改写**：每一节都是源文本的逐字原文（不插值、不套模板——加载链上没有模板
引擎，真源正文里也因此不许出现花括号），节与节之间**不插入任何分隔文字**，按**源文件顺序**
拼接（不是按声明顺序），所以「送到模型手里的那一段」可以逐字去源文件里比对。
（选中的小节未必在源文件里相邻，故整段切片未必是源文件的连续子串。）

**没有校验闸门**：本模块只负责「把词送到」，不新增任何闸门。词表到了请求里，产出好不好
仍由人工读（本仓纪律：闸门收紧要先有实测样本）。
"""
from __future__ import annotations

from pathlib import Path

KNOWLEDGE_DIR = Path(__file__).resolve().parent / "knowledge"
VIDEO_SOURCE_PATH = KNOWLEDGE_DIR / "film-language-video.md"
IMAGE_SOURCE_PATH = KNOWLEDGE_DIR / "film-language-image.md"

# 小节以二级标题起行；key 与小节标题**全等**才算命中。
# 为什么不用「标题含 key」的模糊匹配：模糊匹配下两个 key 可能落到同一节、一个 key 可能落到
# 两节，而按标题取切片只能挑一个——静默取错比大声报错难查得多。全等的代价是标题改了字就得
# 同步改声明，但那会立刻报「找不到小节」，不是静默失效。
_SECTION_PREFIX = "## "

# 落笔侧（issue #32）的切片：**写最终正文的那三条路径共用同一份**——整片提示词工程师
# （角色键 ``prompt_engineer``），以及长视频的两条写段路径（共享键 ``segment_writer``，
# 由 ``segment_prompts`` 取用；它们不是角色，请求是拼出来的字符串）。
#
# 为什么共用一份、而不按路径各取所需：三条路径写的是**同一件事**（最终 H3 正文），
# 一份词表两档写法就会「规划失败掉一个档次」——那正是本仓犯过的错（issue #26 的锚定与
# 修订只改了主路径，回退路径的缺陷原样复发）。所以两个键**共用同一个元组**，
# 并有测试钉住两者相等（钉的是「送给模型的切片内容一样」这个外部行为；是不是同一个
# 对象是这里的实现细节，测试刻意不钉）。
#
# 为什么落笔侧取**全部九节**（因而它的切片就等于整份真源）：选词侧只定「用哪个词」，
# 各取所需即可；落笔侧要把词**落成句子**——景别、机位角度、构图、光线、风格它都现写，
# 运镜白名单与写作纪律是落笔纪律（真源自己写着「写作纪律……落笔侧一处都没提」，
# 「词表：风格」也是留给落笔侧的），每镜六要素与七格骨架则定义了它要写的那个镜头块
# 必须确立什么、按什么排序。少给一节就是少一条落笔纪律，而切片声明是显式的：
# 将来要收窄，改这里一处即可（两个键共用同一个元组，不会只改到一半）。
_WRITING_SIDE_SLICES: tuple[str, ...] = (
    "运镜怎么写",
    "每镜六要素",
    "七格骨架",
    "词表：景别",
    "词表：机位角度",
    "词表：构图与焦点",
    "词表：光线",
    "词表：风格",
    "写作纪律",
)

# 消费者 → 要哪几节。键**大多是角色名**（角色提示词的加载处按角色名取），
# 但也有不是角色的（`segment_writer` 是两条写段路径的共享键，那两条路径的请求是
# `segment_prompts` 拼出来的字符串），故这里不写成「角色键」。
#
# 选词侧（issue #31）只接**选词真正发生的那两个角色**：
# - 分镜师定景别与构图要点，故取景别表与构图表；
# - 摄影指导把分镜细化成画面，多取**机位角度**与**光线**两张表（画面细化要写光的方向、
#   强度与质感，而分镜表里没有光线这一栏）。
# 两边都取运镜写法与写作纪律——两者都要落笔写机位运动与切点。
#
# **不进白名单的角色**（票面明列）：分段规划师（它的段尾钩子是锚帧语义校验的对照物，
# 掺审美词会让人工判定不可判）、美术统筹（风格基调已由 brief 给出）、制作人。其余角色同理：
# 不声明即拿不到，加白名单是显式动作，不会悄悄扩权。
VIDEO_SLICES: dict[str, tuple[str, ...]] = {
    "storyboard": (
        "运镜怎么写",
        "每镜六要素",
        "七格骨架",
        "词表：景别",
        "词表：构图与焦点",
        "写作纪律",
    ),
    "cinematographer": (
        "运镜怎么写",
        "每镜六要素",
        "七格骨架",
        "词表：景别",
        "词表：机位角度",
        "词表：构图与焦点",
        "词表：光线",
        "写作纪律",
    ),
    # 落笔侧（issue #32）：整片提示词工程师。
    "prompt_engineer": _WRITING_SIDE_SLICES,
    # 落笔侧（issue #32）：长视频的两条写段路径（v2 规划式、规划失败回退式）。
    # 键不是角色名——这两条路径的请求由 ``segment_prompts`` 拼字符串，没有角色 agent。
    "segment_writer": _WRITING_SIDE_SLICES,
}
# 「词表：风格」此前无人取（风格基调由 brief 给定，选词侧的角色都不挑风格词），
# 现已随 _WRITING_SIDE_SLICES 归落笔侧——真源是视频半的单一来源，
# 落笔侧要用到的词表不该另开一份。

# 生图侧（issue #33）的切片。**生图半是另一份真源**，运镜词表一个字都不进这里。
#
# 目前只有一个消费者：关键帧生图提示词工程师（角色键 ``frame_prompt_engineer``）——
# 它是链上唯一真跑的生图角色。资产图那条路的 ``image_prompt_engineer`` **故意不在**里面：
# 它的三个节点在 ``graph.pipeline._FOLDED_NODES`` 里从没接进链，喂进 ComfyUI 的实际是
# **设计正文**，接它属编排改动（另开票）。
#
# 取**全部小节**：这个消费者一个人要从景别、视角、构图、光线写到风格与材质，按节裁剪
# 没有意义。声明仍然显式（收窄改这里一处），并有「声明 == 真源小节」的集合相等断言守着
# —— ``image_slice`` 的静默失败方向同样是「少给」，真源将来新增一节不会自己送到。
_IMAGE_SLICES: tuple[str, ...] = (
    "四条硬约束",
    "画布决定构图：两套",
    "结构化字段",
    "词表：景别",
    "词表：视角与机位",
    "词表：构图",
    "词表：光线",
    "词表：镜头与焦段",
    "词表：风格与材质",
    "hex 锁色",
    "落笔前自查",
)

IMAGE_SLICES: dict[str, tuple[str, ...]] = {
    "frame_prompt_engineer": _IMAGE_SLICES,
}


def video_source_text() -> str:
    """整份视频半真源（纯文本，逐字）。"""
    return VIDEO_SOURCE_PATH.read_text(encoding="utf-8")


def image_source_text() -> str:
    """整份生图半真源（纯文本，逐字）。"""
    return IMAGE_SOURCE_PATH.read_text(encoding="utf-8")


def video_source_headline() -> str:
    """真源第一行（标题）。

    到达性测试的标记串从它派生——手抄一份常量会在真源改名时静默失配，
    于是「词表到了没有」的断言变成永远为真的空断言。
    """
    lines = video_source_text().lstrip().splitlines()
    return lines[0] if lines else ""


def image_source_headline() -> str:
    """生图半真源第一行（标题）。标记串的取法同 ``video_source_headline``。"""
    lines = image_source_text().lstrip().splitlines()
    return lines[0] if lines else ""


def _split_sections(text: str) -> tuple[str, list[tuple[str, str]]]:
    """切成（前言, [(小节标题, 小节原文), ...]），顺序即源文件顺序。"""
    preamble: list[str] = []
    sections: list[tuple[str, str]] = []
    head: str | None = None
    body: list[str] = []
    for line in text.splitlines(keepends=True):
        if line.startswith(_SECTION_PREFIX):
            if head is not None:
                sections.append((head, "".join(body)))
            head = line[len(_SECTION_PREFIX):].strip()
            body = [line]
        elif head is None:
            preamble.append(line)
        else:
            body.append(line)
    if head is not None:
        sections.append((head, "".join(body)))
    return "".join(preamble), sections


def _slice(source_path: Path, declarations: dict[str, tuple[str, ...]], consumer: str) -> str:
    """按声明的键取某份真源里的小节，逐字拼成切片；消费者不在白名单里返回空串。

    找不到声明的小节（标题被改过）、或真源里出现重名小节（取哪一节不唯一）都**报错**，
    而不是静默少给一节：「词表悄悄没送到」正是本仓「规则在、接线不在」那一类缺陷，
    不能靠人发现。
    """
    keys = declarations.get(consumer)
    if not keys:
        return ""

    preamble, sections = _split_sections(source_path.read_text(encoding="utf-8"))
    heads = [head for head, _body in sections]
    duplicates = sorted({head for head in heads if heads.count(head) > 1})
    if duplicates:
        raise ValueError(f"{source_path.name} 里有重名小节 {duplicates}——按标题取切片会取错")
    missing = [key for key in keys if key not in heads]
    if missing:
        raise ValueError(
            f"{source_path.name} 里找不到小节 {missing}（消费者 {consumer}）——"
            "真源的小节标题改了，切片声明要跟着改"
        )
    return preamble + "".join(body for head, body in sections if head in keys)


def video_slice(consumer: str) -> str:
    """取视频半真源里给该消费者的切片；消费者不在白名单里返回空串。"""
    return _slice(VIDEO_SOURCE_PATH, VIDEO_SLICES, consumer)


def image_slice(consumer: str) -> str:
    """取生图半真源里给该消费者的切片；消费者不在白名单里返回空串。

    与 ``video_slice`` 是**两个读取口**，不是同一个函数换参数：两份真源的消费者不同、
    语言不同，谁都不该误用另一个的口子（``agents.load_role_prompt`` 也只按角色各取一次）。
    """
    return _slice(IMAGE_SOURCE_PATH, IMAGE_SLICES, consumer)

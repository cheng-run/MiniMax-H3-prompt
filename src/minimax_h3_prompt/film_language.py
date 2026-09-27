"""电影语言真源的读取口：按消费者取切片（issue #31）。

**为什么要有这一层**：真源（``knowledge/film-language-video.md``）是一份给人读的连续文本，
而到达模型的应该是**按角色裁剪过的片段**——分镜师定的是景别与构图，摄影指导才写光线与机位角度。
整份灌给所有角色既浪费上下文，也会让不写光线的角色去挑光线词。

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

# 小节以二级标题起行；key 与小节标题**全等**才算命中。
# 为什么不用「标题含 key」的模糊匹配：模糊匹配下两个 key 可能落到同一节、一个 key 可能落到
# 两节，而按标题取切片只能挑一个——静默取错比大声报错难查得多。全等的代价是标题改了字就得
# 同步改声明，但那会立刻报「找不到小节」，不是静默失效。
_SECTION_PREFIX = "## "

# 消费者（角色键）→ 要哪几节。
#
# 本票只接**选词真正发生的那两个角色**：
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
}
# 「词表：风格」暂时无人取：风格基调由 brief 给定，选词侧的角色都不挑风格词。
# 它留在真源里供**落笔侧**（整片提示词工程师与两条写段路径，issue #32）取用，
# 不是无主内容——真源是视频半的单一来源，落笔侧要用到的词表不该另开一份。


def video_source_text() -> str:
    """整份视频半真源（纯文本，逐字）。"""
    return VIDEO_SOURCE_PATH.read_text(encoding="utf-8")


def video_source_headline() -> str:
    """真源第一行（标题）。

    到达性测试的标记串从它派生——手抄一份常量会在真源改名时静默失配，
    于是「词表到了没有」的断言变成永远为真的空断言。
    """
    lines = video_source_text().lstrip().splitlines()
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


def video_slice(consumer: str) -> str:
    """取视频半真源里给该消费者的切片；消费者不在白名单里返回空串。

    找不到声明的小节（标题被改过）、或真源里出现重名小节（取哪一节不唯一）都**报错**，
    而不是静默少给一节：「词表悄悄没送到」正是本仓「规则在、接线不在」那一类缺陷，
    不能靠人发现。
    """
    keys = VIDEO_SLICES.get(consumer)
    if not keys:
        return ""

    preamble, sections = _split_sections(video_source_text())
    heads = [head for head, _body in sections]
    duplicates = sorted({head for head in heads if heads.count(head) > 1})
    if duplicates:
        raise ValueError(f"{VIDEO_SOURCE_PATH.name} 里有重名小节 {duplicates}——按标题取切片会取错")
    missing = [key for key in keys if key not in heads]
    if missing:
        raise ValueError(
            f"{VIDEO_SOURCE_PATH.name} 里找不到小节 {missing}（消费者 {consumer}）——"
            "真源的小节标题改了，切片声明要跟着改"
        )
    return preamble + "".join(body for head, body in sections if head in keys)

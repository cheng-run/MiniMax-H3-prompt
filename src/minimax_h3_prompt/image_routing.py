"""生图模型路由：文字类关键词 → MOSS，手动选择优先（issue #88 / 父 spec #84 D8）。

一张资产图用哪个生图模型，由三档优先级决定（高到低）：

1. **手动选择**——素材循环里人给这张改过模型（``source=manual``）；
2. **自动路由**——设计正文或该资产提示词命中文字类关键词（标签/招牌/包装/logo/文字/海报）
   时切到 MOSS（D8 的外部调研结论：文字与版式 MOSS 最强）；
3. **配置默认**——都没有就按 ``config/image.yaml`` 里那一档的模型（``source=config``）。

**判定结果跟着条目走**（``entry["image_model"]``），不另建一张「语义名 → 模型」的表。
理由是并行：三个资产图节点在同一个组合节点里并发跑，它们各自只返回自己那一类的产物键；
再合写一张共享的表时，``make_parallel`` 的 ``merged.update`` 会让最后返回的那一类覆盖掉
前两类的记录（静默丢一半，无任何报错）。判定是**这一条资产**的属性，放在这一条上就不会
有第二个写者。

**为什么要把判定记下来、而不是每次现算**：现算的输入里包含提示词正文，而正文会被改写
（改写提示词、AI 改词）。改写后关键词可能不再出现，现算就会把一张本来由 MOSS 写的词
认成默认档——接着 AI 改词会按别家的纪律改它（三档的写法相反），产物照样生成、不报错。
记下来的那一份是「这条词当年是按哪一档的纪律写的」，与正文后来改成什么样无关。

**双语词表是硬要求**：设计正文是中文、而提示词是英文（MOSS/Qwen 档），只写中文针在
英文提示词上恒不命中，等于这条路由对「已经被切过去的那一类资产」结构性失效；只写英文针
则中文正文里的「包装」永远看不见。英文针按**整词**匹配（``design`` 里有 ``sign``、
``texture`` 里有 ``text``），子串匹配会让每一张都误切到 MOSS，而误切不报错。
"""
from __future__ import annotations

import re
from typing import Any, Mapping, Sequence

from .image_config import IMAGE_MODEL_KEYS, image_model_for, image_model_label

# 判定来源（写进条目，决定节点重跑时谁覆盖谁）。
SOURCE_CONFIG = "config"
SOURCE_AUTO = "auto"
SOURCE_MANUAL = "manual"
SOURCES = (SOURCE_CONFIG, SOURCE_AUTO, SOURCE_MANUAL)

# 条目里承载判定的字段名。**只有这一处定义**：节点写、素材循环读、AI 改词取模型。
ENTRY_KEY = "image_model"

# 展示词 → (中文针, 英文针)。中文针按子串匹配（中文不需词边界），英文针在下面补上
# 「前后不是字母数字下划线」的边界——``\b`` 在这里不够用：``sign`` 与 ``signage`` 的首尾
# 都要能被同一个正则分开处理。
_TEXT_KEYWORD_PATTERNS: tuple[tuple[str, str, str], ...] = (
    ("标签", "标签", r"labels?"),
    ("招牌", "招牌", r"signboards?|signages?|signs?"),
    ("包装", "包装", r"packag(?:es?|ing)"),
    ("logo", "logo", r"logos?|logotypes?"),
    ("文字", "文字", r"texts?|lettering"),
    ("海报", "海报", r"posters?"),
)

# 票面（issue #88）点名的六类文字能力关键词，**从上面那张针表派生**：终端念的词、测试
# 断言的词表、英文针的归类必须同源——另写一份字面清单，加一类词时就会只改到一边
# （要么永远不命中、要么念出一个没有针的词），而这不会报错。
TEXT_CAPABILITY_KEYWORDS = tuple(word for word, _zh, _latin in _TEXT_KEYWORD_PATTERNS)

# 命中后切到哪一个模型。是**规则的结果**而不是「默认档」：默认档住在 image_config。
TEXT_CAPABILITY_MODEL = "moss"

_LATIN_PATTERNS: dict[str, re.Pattern[str]] = {
    # 边界用「前后不是字母数字下划线」而不是 ``\b``：``sign`` 与 ``signage`` 这类长短词
    # 要在同一个正则里分开处理，``\b`` 只认「词与非词之间」，管不住「sign 是 signage 的
    # 前缀」这一层。
    word: re.compile(rf"(?<![0-9A-Za-z_])(?:{latin})(?![0-9A-Za-z_])", re.IGNORECASE)
    for word, _zh, latin in _TEXT_KEYWORD_PATTERNS
}
_ZH_NEEDLES: dict[str, str] = {
    word: zh for word, zh, _latin in _TEXT_KEYWORD_PATTERNS
}


def _first_position(text: str, word: str) -> int | None:
    """该词在文本里第一次出现的位置；没有 → None。"""
    positions: list[int] = []
    zh_index = text.find(_ZH_NEEDLES[word])
    if zh_index >= 0:
        positions.append(zh_index)
    match = _LATIN_PATTERNS[word].search(text)
    if match is not None:
        positions.append(match.start())
    return min(positions) if positions else None


def text_capability_hit(*texts: str) -> str | None:
    """任一文本命中文字类关键词时返回**命中的那个词**（终端要念它），否则 None。

    多段文本（设计正文 ＋ 该资产提示词）按顺序扫，取**最先出现**的那一个词：一段文本
    同时提到「包装」与「文字」时，念最靠前的那处——用户看到的是「让系统做决定的那个
    地方」，而不是词表里排得最靠前的那个（那会念出一处已经过去的词）。
    """
    for text in texts:
        body = str(text or "")
        candidates = [
            (position, order, word)
            for order, word in enumerate(TEXT_CAPABILITY_KEYWORDS)
            if (position := _first_position(body, word)) is not None
        ]
        if candidates:
            return min(candidates)[2]
    return None


def assignment(kind: str, *, texts: Sequence[str], manual: str | None = None) -> dict[str, str]:
    """判定这张资产用哪个模型（手动 > 自动 > 配置默认），返回可写进条目的记录。

    ``manual`` 是人在素材循环里选过的模型；写错值要响（``sd3`` 这类拼错静默回落，
    等于「人以为改了、实际一直按旧模型出词」，且产物不报错）。
    """
    if manual is not None:
        if manual not in IMAGE_MODEL_KEYS:
            raise ValueError(
                f"生图模型无效：{manual!r}（只认 {', '.join(IMAGE_MODEL_KEYS)}）")
        return {"model": manual, "source": SOURCE_MANUAL, "keyword": ""}
    hit = text_capability_hit(*texts)
    if hit is not None:
        return {"model": TEXT_CAPABILITY_MODEL, "source": SOURCE_AUTO, "keyword": hit}
    return {"model": image_model_for(kind), "source": SOURCE_CONFIG, "keyword": ""}


def _validated(raw: Mapping[str, Any]) -> dict[str, str]:
    """校验并规范化一份判定（``{model, source, keyword}``）。

    **读写同一套校验**：写口不收坏值、读口不吞坏值。两边一紧一松就是同一份形状的两处
    口径——写进去一个不存在的来源，读的时候还会被伪装成「按配置默认」（静默，且看起来
    完全正常）。认不出的模型更坏：后续 AI 改词会按别家的纪律改它。
    """
    model = str(raw.get("model") or "")
    if model not in IMAGE_MODEL_KEYS:
        raise ValueError(
            f"资产条目的生图模型无效：{model!r}"
            f"（只认 {', '.join(IMAGE_MODEL_KEYS)}）")
    source = str(raw.get("source") or "")
    if source not in SOURCES:
        raise ValueError(
            f"资产条目的模型来源无效：{source!r}（只认 {', '.join(SOURCES)}）")
    return {"model": model, "source": source, "keyword": str(raw.get("keyword") or "")}


def put_assignment(entry: dict[str, Any], value: Mapping[str, str]) -> None:
    """把判定写进资产条目（形状规范化成三键；坏值当场响，不留坏形状给读口）。"""
    entry[ENTRY_KEY] = _validated(value)


def assignment_of(entry: Mapping[str, Any]) -> dict[str, str] | None:
    """读条目里的判定；旧条目没有这个字段 → None（调用方回落到配置默认）。"""
    raw = entry.get(ENTRY_KEY)
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise ValueError(
            f"资产条目的 {ENTRY_KEY} 字段错形：{raw!r}"
            "（应为 {model, source, keyword} 对象）")
    return _validated(raw)


def model_of(entry: Mapping[str, Any]) -> str | None:
    """条目记录的模型键；旧条目没有记录 → None。"""
    value = assignment_of(entry)
    return value["model"] if value is not None else None


def manual_model_of(entry: Mapping[str, Any]) -> str | None:
    """条目里**人选的**模型；自动判定与配置默认都回 None（节点重跑据此保留人的选择）。"""
    value = assignment_of(entry)
    if value is not None and value["source"] == SOURCE_MANUAL:
        return value["model"]
    return None


def described(value: Mapping[str, str]) -> str:
    """终端那一行的人话：「命中『包装』→ MOSS-Image」／「手动指定 → …」／「按配置默认 → …」。

    **唯一写源**：节点出词时打的那一行与素材循环菜单展示的是同一件事，两处各拼一遍
    就会各自漂（一处说命中、一处说手动）。坏形状越过 ``_validated`` 就响——不认识的来源
    若被伪装成「按配置默认」，终端会理直气壮地报一个错的理由。
    """
    checked = _validated(value)
    label = image_model_label(checked["model"])
    if checked["source"] == SOURCE_AUTO:
        return f"命中『{checked['keyword']}』→ {label}"
    if checked["source"] == SOURCE_MANUAL:
        return f"手动指定 → {label}"
    return f"按配置默认 → {label}"


__all__ = [
    "ENTRY_KEY", "SOURCES", "SOURCE_AUTO", "SOURCE_CONFIG", "SOURCE_MANUAL",
    "TEXT_CAPABILITY_KEYWORDS", "TEXT_CAPABILITY_MODEL",
    "assignment", "assignment_of", "described", "manual_model_of", "model_of",
    "put_assignment", "text_capability_hit",
]

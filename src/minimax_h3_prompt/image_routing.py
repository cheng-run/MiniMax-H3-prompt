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

**否定句不切档**（issue #167 / 父 spec #169 C；粒度由 #177 定为「句」）：2026-11-04 的
r2v 实跑里，设计正文一句「不出现现代塑料包装」把 12 条道具全部切到 MOSS——而那句话是
**禁令**，恰恰说明这一类不需要文字能力。判定的粒度是**命中词所在的句**，不是整段：句里
出现否定词就跳过这一处、继续找下一处肯定命中（同段里「不得塑料包装。瓶身贴一张手写标签」
只认后半句）。

**为什么是「句」而不是「分句」**（issue #177）：真实设计正文里的禁令多是顿号枚举——
「不出现与回忆有关的标签、照片或信物」「不得出现中文文字、中文车牌或中国城市设施」。
按逗号/顿号/分号切分句时，枚举里第二项起的命中词自成一段、段内看不到否定词，于是整类
资产又被切到 MOSS（主树 15 个会话状态里「否定词后 40 字内出现顿号」共 689 处，是主流
形态而非边角）。现在整句含否定词即整句跳过，代价是同句里的「不要 X，但要 Y」会一起
漏切——沿用 #172 的取舍：**宁可漏切、不整类误切**（漏切还能在素材循环里手动改档，
而整类误切不报错，产出的每一条词都按另一套纪律写）。

**「无X」另算一条窄规则**（#177 补充裁定，2026-11-05）：句首为「无」之外，命中词**紧邻
前一个字**是「无」也算否定（``无文字``／``无包装``／``无logo``／``无招牌``）。真实正文
写成 ``**行动事实**：无对白、无字幕`` 时整句既没有否定词表里的词、也不以「无」开头
（冒号已不再是句边界），缺这条会整类误切。**只认紧邻**——``无数标签`` 是肯定，
``无任何标签`` 也不在规则里；放宽成「句里有『无』」会把肯定诉求一起吞掉。

命中记录连同来源句一起存进条目（``evidence``），终端因此能回答「凭什么切的这一档」。
"""
from __future__ import annotations

import re
from typing import Any, Mapping, NamedTuple, Sequence

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

# 不参与「按文字类关键词自动切档」的资产类别（L6-12 / 2026-11-01 裁定）：人物身份板上
# 的校名、臂章上的字会把整张板切到 MOSS，而 ``prompts/image_prompt_disciplines/moss.md``
# 的「人物身份板不是本档强项」一条明写四格身份板不是本档强项（指针不带行号：本仓行号会漂）。
# 只管自动规则——手动指定仍可给人物选 MOSS（人看图选档是人的判断）。
AUTO_ROUTING_EXEMPT_KINDS = frozenset({"character"})

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


class TextCapabilityHit(NamedTuple):
    """一次关键词命中：命中的词 ＋ 它所在的句。

    句是**判定**的粒度（否定只管自己那一句，但顿号/逗号并列项不另起一句），也是终端
    要念出来的证据。
    """

    word: str
    sentence: str


# 句边界：**只认句末标点与换行**（#177）。逗号、顿号、分号**不算边界**——中文设计正文
# 的禁令多是顿号枚举（「不出现…标签、照片或信物」），把它们当边界会让枚举里第二项起的
# 命中词脱离否定词所在的那一段，于是整类误切到 MOSS（#172 的残差）。英文同理：
# ``without labels, text, or logos`` 也是整句受否定，两套规则不必分叉。
_SENTENCE_BREAKS = re.compile(r"[。！？.!?\n\r]+")

# 否定词（票面点名的清单）。中文按子串判：中文没有词边界，也不像英文那样有
# 大小写与复数形态。英文按**整词**判，理由同上面英文针——``anodized`` 里有 ``no``，
# 子串匹配会让每一张金属质感的道具都被当成禁令而漏切。
_NEGATION_ZH: tuple[str, ...] = (
    "不得", "不出现", "不需要", "不要", "不使用", "不能", "禁止", "避免", "没有",
)
_NEGATION_LATIN = re.compile(
    r"(?<![0-9A-Za-z_])(?:no|without|avoid|never|not)(?![0-9A-Za-z_])", re.IGNORECASE)


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


def _sentence_hit(sentence: str) -> tuple[str, int] | None:
    """这一句里最先出现的那个命中词与它的**起始位置**；没有 → None。

    取最靠前的那一处而不是词表顺序：用户看到的是「让系统做决定的那个地方」，
    念一处已经过去的位置会让他对不上正文。位置要一并回给否定判定——「无X」那条
    规则看的是命中词紧邻前一个字（#177 补充裁定）。
    """
    candidates = [
        (position, order, word)
        for order, word in enumerate(TEXT_CAPABILITY_KEYWORDS)
        if (position := _first_position(sentence, word)) is not None
    ]
    if not candidates:
        return None
    position, _order, word = min(candidates)
    return word, position


def _is_negated(sentence: str, position: int) -> bool:
    """这一句是不是禁令（含否定词；首字或命中词紧邻前一字是「无」）。

    「无」有两条口径，二者并存、不冲突：

    - **句首为无**（#167）：整句是禁令；
    - **紧邻命中词为无**（#177 补充裁定，2026-11-05）：``无文字``／``无包装``／
      ``无logo``／``无招牌`` 这一处命中跳过。真实设计正文写成
      ``**行动事实**：无对白、无字幕`` 时整句没有任何否定词表里的词、也不以「无」开头，
      冒号又不再是句边界，缺这条规则会整类误切。

    **只认紧邻**：``无数标签``（数不清的标签）是肯定，``无任何标签`` 也不在这一条里—
    —放宽成「句里有『无』就否定」会把肯定诉求一起吞掉；要覆盖中间夹字的形态得先改规格。

    否定词出现在命中词**之后**也算——判的是整句的极性，不是词的位置（#167）。
    """
    body = sentence.strip()
    if body.startswith("无"):
        return True
    if position > 0 and body[position - 1] == "无":
        return True
    if any(word in body for word in _NEGATION_ZH):
        return True
    return _NEGATION_LATIN.search(body) is not None


def text_capability_hit(*texts: str) -> TextCapabilityHit | None:
    """任一文本命中文字类关键词时返回**命中的词与所在句**（终端要念它们），否则 None。

    多段文本（设计正文 ＋ 该资产提示词）按顺序扫，取**最先出现的那个肯定命中**；文本内部
    再按句切分，跳过含否定词的句。句顺序即文本顺序，所以「最先出现」与「先命中的句」是
    同一件事——不会因为否定跳过一处就跳到词表里排得靠前的词上。
    """
    for text in texts:
        for sentence in _SENTENCE_BREAKS.split(str(text or "")):
            body = sentence.strip()
            if not body:
                continue
            hit = _sentence_hit(body)
            if hit is None:
                continue
            word, position = hit
            if _is_negated(body, position):
                continue
            return TextCapabilityHit(word=word, sentence=body)
    return None


def assignment(kind: str, *, texts: Sequence[str], manual: str | None = None) -> dict[str, str]:
    """判定这张资产用哪个模型（手动 > 自动 > 配置默认），返回可写进条目的记录。

    ``manual`` 是人在素材循环里选过的模型；写错值要响（``sd3`` 这类拼错静默回落，
    等于「人以为改了、实际一直按旧模型出词」，且产物不报错）。

    人物类**跳过自动关键词规则**：身份板上的校名与臂章文字会命中「文字」，把整张板
    切到不擅长四格身份板的 MOSS。跳过规则后仍走配置默认档，手动指定照旧优先。
    """
    if manual is not None:
        if manual not in IMAGE_MODEL_KEYS:
            raise ValueError(
                f"生图模型无效：{manual!r}（只认 {', '.join(IMAGE_MODEL_KEYS)}）")
        return {"model": manual, "source": SOURCE_MANUAL, "keyword": "", "evidence": ""}
    if kind not in AUTO_ROUTING_EXEMPT_KINDS:
        hit = text_capability_hit(*texts)
        if hit is not None:
            # evidence 存**来源句**而不只是那个词：词单独一行时「包装」既可能是需求
            # 也可能是禁令，只有整句能让用户当场判断这次切档有没有道理（#167）。
            return {"model": TEXT_CAPABILITY_MODEL, "source": SOURCE_AUTO,
                    "keyword": hit.word, "evidence": hit.sentence}
    return {"model": image_model_for(kind), "source": SOURCE_CONFIG, "keyword": "", "evidence": ""}


def _validated(raw: Mapping[str, Any]) -> dict[str, str]:
    """校验并规范化一份判定（``{model, source, keyword, evidence}``）。

    **读写同一套校验**：写口不收坏值、读口不吞坏值。两边一紧一松就是同一份形状的两处
    口径——写进去一个不存在的来源，读的时候还会被伪装成「按配置默认」（静默，且看起来
    完全正常）。认不出的模型更坏：后续 AI 改词会按别家的纪律改它。

    ``evidence`` 缺省空串：issue #167 之前写进 state 的记录没有这个键，旧会话要照常续跑。
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
    return {
        "model": model, "source": source,
        "keyword": str(raw.get("keyword") or ""),
        "evidence": str(raw.get("evidence") or ""),
    }


def put_assignment(entry: dict[str, Any], value: Mapping[str, str]) -> None:
    """把判定写进资产条目（形状规范化成四键；坏值当场响，不留坏形状给读口）。"""
    entry[ENTRY_KEY] = _validated(value)


def assignment_of(entry: Mapping[str, Any]) -> dict[str, str] | None:
    """读条目里的判定；旧条目没有这个字段 → None（调用方回落到配置默认）。"""
    raw = entry.get(ENTRY_KEY)
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise ValueError(
            f"资产条目的 {ENTRY_KEY} 字段错形：{raw!r}"
            "（应为 {model, source, keyword, evidence} 对象）")
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
    """终端那一行的人话：「命中『包装』→ MOSS-Image（来源：…）」／「手动指定 → …」／
    「按配置默认 → …」。

    **唯一写源**：节点出词时打的那一行与素材循环菜单展示的是同一件事，两处各拼一遍
    就会各自漂（一处说命中、一处说手动）。坏形状越过 ``_validated`` 就响——不认识的来源
    若被伪装成「按配置默认」，终端会理直气壮地报一个错的理由。

    来源句只在**有证据**时追加：旧记录（issue #167 之前）没有这个字段，印一个空的
    「（来源：）」比不印更像 bug。
    """
    checked = _validated(value)
    label = image_model_label(checked["model"])
    if checked["source"] == SOURCE_AUTO:
        text = f"命中『{checked['keyword']}』→ {label}"
    elif checked["source"] == SOURCE_MANUAL:
        text = f"手动指定 → {label}"
    else:
        text = f"按配置默认 → {label}"
    if checked["evidence"]:
        text += f"（来源：{checked['evidence']}）"
    return text


__all__ = [
    "AUTO_ROUTING_EXEMPT_KINDS",
    "ENTRY_KEY", "SOURCES", "SOURCE_AUTO", "SOURCE_CONFIG", "SOURCE_MANUAL",
    "TEXT_CAPABILITY_KEYWORDS", "TEXT_CAPABILITY_MODEL", "TextCapabilityHit",
    "assignment", "assignment_of", "described", "manual_model_of", "model_of",
    "put_assignment", "text_capability_hit",
]

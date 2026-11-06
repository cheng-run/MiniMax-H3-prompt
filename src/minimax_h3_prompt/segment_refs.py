"""段内参考素材子集与重编号（issue #75 / ADR 0017「代价」）。

多参（ref2va）整片提示词里，``subject_definitions`` 定义的是**全片**素材（一次真实
运行有 24 张图、25 个 Subject）。整批喂给 H3 会把 Qwen3-VL 的文本编码序列撑到约
5.4 万 token；本机 flash/efficient SDPA 都缺 kernel，只剩朴素注意力，``einsum`` 当场
要 232 GiB（2026-10-22 的 ``torch.OutOfMemoryError``）。所以**每段只喂它用到的素材**
（ADR 0017：9 是每段默认值仍然合理，作为全局容量才是错的）。

本模块是那一步的纯函数层：不读盘、不调模型、不打印——输入本段 Shot 原文与整片定义，
输出「保留哪些素材、图号怎么改」。下面四件事分开做，是因为它们各有各的失败形态：

0. **闭包**（``compute_asset_closure``，#181）：沿定义块做传递闭包，并且是全仓**唯一**
   的一份实现——规划期算段内素材预算与写段期算段内素材子集都调它。两份实现迟早会对
   同一段给出两个答案，后果是段词与挂图不同源、而且不报错。它同时交回**每条边的
   来源**（被哪条定义、哪句话拉进来，是否命中一致性锚句式），锚的降级判据才有据可查。

1. **子集**（``compute_segment_scope``）：从本段 Shot 原文收集 ``<Subject N>`` /
   ``<Picture N>``，再沿定义做**闭包**——Subject 的定义里常常引用它的来源图
   （``<Subject 1> is the woman in <Picture 1>``），只按 Shot 原文收集会漏掉它们，
   段内正文就引用了没定义、也没接线的图。漏收的后果不是「少一张图」而是**标签悬空**。
2. **重编号**（``rewrite_picture_labels``）：官方标签一经定义就在全篇保持同一含义
   （ref-en.txt:33 附近的规则），段内编号必须自洽且从 1 连续——接线按新号落槽
   （新 ``<Picture N>`` → ``ref_image_{N-1}``），有缺口就会接错槽。
   **Subject 原号不动**：模型对 Subject 的连续性没有要求，只有 Picture 跟接线有关。
3. **上限**（``refs_per_segment``，默认 9，且取它与官方单次上限 9 图的较小值 ——
   issue #86）：本段用到的图超过上限时**报错并点名**，绝不静默截断——截断等于让
   暂停在一段的提示词里引用没接线的图。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from .ref_capacity import MAX_PICTURES
from .text_patterns import (
    PICTURE_TAG as _PICTURE_RE,
    SHOT_TAG as _SHOT_TAG_RE,
    SUBJECT_TAG as _SUBJECT_RE,
)
from .workflow_config import DEFAULT_REFS_PER_SEGMENT, workflow_settings

# 官方标签（ref-en.txt §2）：<Subject N> / <Picture N>。与校验器同一套形态，
# 但这里只做「收集与改写」，判据（未定义/未使用/跳号）仍住在 ``tools.h3_validator``。

# 定义条目行首标签：官方要求「Give each item its own line」，所以一行起头是标签的
# 就是一个新条目；续行归上一个条目（历史产物里偶有折行，不能把续行当新条目丢掉）。
_ENTRY_HEAD_RE = re.compile(r"^\s*<(Subject|Picture)\s+(\d+)>")

# 本段接线卡里「台账读不到正本」的占位：绝不编一个像真的路径（照卡接线会接到空气）。
_MISSING_PATH = "（台账未登记正本）"
_MISSING_NAME = "（未登记素材）"


class SegmentAssetOverflowError(ValueError):
    """本段用到的参考图超过 ``refs_per_segment``（issue #75 AC3）。

    单独立一个类型是为了让调用方能把它与「数据坏了」的 ValueError 分开处理：这条
    是**设计边界**（本段素材太多，得拆段或精简），不是解析失败。
    """


@dataclass(frozen=True)
class SegmentAsset:
    """本段保留的一张参考图：段内新号 → 原号 → 语义名 → 正本路径（新台账为库内绝对路径）。"""

    new_picture: int
    original_picture: int
    name: str
    path: str = ""


@dataclass(frozen=True)
class SegmentAnchor:
    """本段**不占槽**的一致性锚（#183）：图被撤下，只留语义名与溯源。

    它存在的意义是「和某张图保持一致」，不是「要被画出来」——所以不挂图、不进接线槽、
    不参与 ``refs_per_segment`` 计数。``via`` 记「是哪个 Subject 的哪句话把它勾进来的」
    （锚链上第一个锚边的定义，真实形态里是 ``Subject 21`` 而不是它直属的 ``Subject 22``），
    ``evidence`` 是那句话的原文：将来问「这张为什么被降级」时，两者就是答案。
    """

    original_picture: int
    name: str = ""
    via: str = ""
    evidence: str = ""


@dataclass(frozen=True)
class SegmentAssetScope:
    """一段的参考素材子集：保留的图（新号↔原号）与保留的 Subject 原号。

    ``pictures`` 只含**出镜**的图；只作一致性锚的图在 ``anchors`` 里，不占槽但保留溯源
    （#183）。``subject_numbers`` 含锚链上的 Subject——它们的定义条目要留在段内，否则
    段词里 ``<Subject N>`` 会悬空。
    """

    pictures: tuple[SegmentAsset, ...]
    subject_numbers: tuple[int, ...] = ()
    anchors: tuple[SegmentAnchor, ...] = ()

    @property
    def picture_map(self) -> dict[int, int]:
        """原号 → 段内新号。查表而不是重算，避免两处各推一遍顺序。"""
        return {item.original_picture: item.new_picture for item in self.pictures}

    @property
    def ref_meta(self) -> list[tuple[int, str, str]]:
        """喂给 ``validate_ref_segment`` 的登记表（新号口径）。

        描述留空：校验器只用编号与语义名（``PICTURE_UNUSED`` 的消息里点名），
        把整条生图提示词塞进 plan.json 既没必要也不该进续跑快照。
        """
        return [(item.new_picture, item.name, "") for item in self.pictures]

    def to_dict(self) -> dict[str, Any]:
        """落 ``plan.json`` 的 ``assets_in_segment``：续跑照这张表改写，不重算。

        ``anchors`` 只在非空时出现：没有锚的段（绝大多数）落盘形态与改动前逐字节相同，
        旧读者也不会因为多一个空键而认不出来。
        """
        data: dict[str, Any] = {
            "pictures": [
                {"new": item.new_picture, "original": item.original_picture,
                 "name": item.name, "path": item.path}
                for item in self.pictures
            ],
            "subjects": list(self.subject_numbers),
        }
        if self.anchors:
            data["anchors"] = [
                {"number": item.original_picture, "name": item.name,
                 "via": item.via, "evidence": item.evidence}
                for item in self.anchors
            ]
        return data

    @classmethod
    def from_dict(cls, raw: Any) -> "SegmentAssetScope":
        """读回 ``plan.json`` 里的映射。形态坏了要响——续跑照错表改写就是接错线。"""
        if not isinstance(raw, Mapping):
            raise ValueError(f"assets_in_segment 必须是对象：{raw!r}")
        pictures_raw = raw.get("pictures", [])
        if not isinstance(pictures_raw, list):
            raise ValueError(f"assets_in_segment.pictures 必须是数组：{pictures_raw!r}")
        pictures: list[SegmentAsset] = []
        for item in pictures_raw:
            if not isinstance(item, Mapping):
                raise ValueError(f"assets_in_segment.pictures 的条目必须是对象：{item!r}")
            try:
                pictures.append(SegmentAsset(
                    new_picture=int(item["new"]),
                    original_picture=int(item["original"]),
                    name=str(item.get("name", "")),
                    path=str(item.get("path", "")),
                ))
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(f"assets_in_segment.pictures 的条目读不出：{item!r}") from exc
        subjects_raw = raw.get("subjects", [])
        if not isinstance(subjects_raw, list):
            raise ValueError(f"assets_in_segment.subjects 必须是数组：{subjects_raw!r}")
        # 旧 plan.json 没有这个键（锚降级是 #183 才有的）：缺省空表，不把用户进度读废。
        anchors_raw = raw.get("anchors", [])
        if not isinstance(anchors_raw, list):
            raise ValueError(f"assets_in_segment.anchors 必须是数组：{anchors_raw!r}")
        anchors: list[SegmentAnchor] = []
        for item in anchors_raw:
            if not isinstance(item, Mapping):
                raise ValueError(f"assets_in_segment.anchors 的条目必须是对象：{item!r}")
            try:
                anchors.append(SegmentAnchor(
                    original_picture=int(item["number"]),
                    name=str(item.get("name", "")),
                    via=str(item.get("via", "")),
                    evidence=str(item.get("evidence", "")),
                ))
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(f"assets_in_segment.anchors 的条目读不出：{item!r}") from exc
        return cls(pictures=tuple(pictures),
                   subject_numbers=tuple(int(n) for n in subjects_raw),
                   anchors=tuple(anchors))


def parse_definition_entries(subject_definitions: str) -> list[tuple[str, int, str]]:
    """``subject_definitions`` → ``[(kind, number, entry_text), ...]``（保持原序）。

    ``kind`` ∈ ``{"subject", "picture"}``；行首不是标签的段落（前言/无标签条目）用
    ``kind=""``、``number=0`` 表示——它们是**不可判定归属**的文本，调用方按「保留但
    改写图号」处理（丢掉会静默删掉定义块里的内容）。
    """
    entries: list[tuple[str, int, str]] = []
    for line in str(subject_definitions or "").splitlines():
        match = _ENTRY_HEAD_RE.match(line)
        if match:
            entries.append((match.group(1).lower(), int(match.group(2)), line))
        elif entries and line.strip():
            kind, number, text = entries[-1]
            entries[-1] = (kind, number, f"{text}\n{line}")
        elif line.strip():
            entries.append(("", 0, line))
        else:
            # 空行：条目之间的间隔，不单独成条；续行语义不受影响。
            continue
    return entries


def _labels_in(text: str) -> tuple[set[int], set[int]]:
    """文本里出现的 ``(Subject 号集合, Picture 号集合)``。"""
    return ({int(m.group(1)) for m in _SUBJECT_RE.finditer(text)},
            {int(m.group(1)) for m in _PICTURE_RE.finditer(text)})


# 句子边界：闭包溯源要交回「拉它进来的**那句**文本」，不是整条定义（一条定义可以是
# 好几个句子，锚句式只看它自己那句才判得准）。中文标点与换行都算边界。
_SENTENCE_END_RE = re.compile(r"[.!?。！？\n]")


def _sentence_at(text: str, index: int) -> str:
    """``text[index]`` 落在的那一句（含句末标点，首尾去空白）。

    认不出边界时退化成整段：溯源少一点精度可以接受，**指错句子**不行——所以宁可
    把范围放大到整条定义，也不猜。
    """
    start = 0
    for match in _SENTENCE_END_RE.finditer(text, 0, index):
        start = match.end()
    end_match = _SENTENCE_END_RE.search(text, index)
    end = end_match.end() if end_match else len(text)
    return text[start:end].strip()


# 一致性锚句式（父 spec #180 用户故事 9）：白名单式判据——默认算出镜，只有定义原文
# 明写「与…一致」才算锚。全仓**只此一处**：降级逻辑（#183）也读它，两处各写一份，
# 同一张图在规划期与写段期就会被分成两类，而且不报错。
CONSISTENCY_ANCHOR_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"must\s+agree\s+with", re.IGNORECASE),
    re.compile(r"consistent\s+with", re.IGNORECASE),
    # 「与…一致」中间只许到句读为止：「与」和「一致」跨句时说的是两件事。
    re.compile(r"与[^，。；！？,;!?\n]{1,40}一致"),
)


def is_consistency_anchor(text: str) -> bool:
    """这句是不是一致性锚句式（只判传来的一句，不判整条定义）。"""
    return any(pattern.search(str(text or ""))
               for pattern in CONSISTENCY_ANCHOR_PATTERNS)


@dataclass(frozen=True)
class ClosureEdge:
    """一条「因为某条定义点了它，所以它进入本段」的溯源边（#181）。

    ``via`` 是触发它的**已保留节点**，``definition`` 是行文里点了它的**那条定义**：
    正向闭包里两者相同（Subject 1 的定义点了 Picture 2）；反向拉取时不同（图没有
    独立定义行，是 Subject 3 的定义点了这张图，才把 Subject 3 拉进来）。将来要问
    「这张图为什么会挂在只拍室内的一段里」，靠的就是这两个字段加上 ``evidence``。
    """

    target: tuple[str, int]
    via: tuple[str, int]
    definition: tuple[str, int]
    evidence: str
    anchor: bool


@dataclass(frozen=True)
class AssetClosure:
    """沿定义块做传递闭包的结果：保留的图号、Subject 号，以及每条边的来源。"""

    pictures: tuple[int, ...]
    subjects: tuple[int, ...]
    edges: tuple[ClosureEdge, ...] = ()
    # 本次闭包**实际**用的种子（``(kind, number)``，已含「种子为空→全量」那一支的
    # 展开）。降级要判「这张是不是本段自己点名的」，判据必须与闭包同源：让调用方
    # 再传一遍种子，两处一旦不一致就会把种子降成锚，而且不报错。
    seeds: tuple[tuple[str, int], ...] = ()

    def edges_into(self, kind: str, number: int) -> tuple[ClosureEdge, ...]:
        """指向某个节点的全部边（查「它为什么进来」用，按记录顺序）。"""
        target = (kind, int(number))
        return tuple(edge for edge in self.edges if edge.target == target)


def _citations(entry: str) -> list[tuple[str, int, str]]:
    """一条定义点了谁：``[(kind, number, 它所在的那句), ...]``，按出现位置排序。

    按位置排序而不是先 Subject 后 Picture，是为了让溯源边的顺序与原文一致——两条
    边来自同一句话时，读起来才是「先读到哪张就记哪张」。
    """
    hits: list[tuple[int, str, int]] = []
    for kind, pattern in (("subject", _SUBJECT_RE), ("picture", _PICTURE_RE)):
        for match in pattern.finditer(entry):
            hits.append((match.start(), kind, int(match.group(1))))
    hits.sort()
    return [(kind, number, _sentence_at(entry, at)) for at, kind, number in hits]


def compute_asset_closure(
    *,
    seed_subjects: Iterable[int] = (),
    seed_pictures: Iterable[int] = (),
    subject_definitions: str = "",
    registry: Iterable[Sequence] = (),
) -> AssetClosure:
    """沿 ``subject_definitions`` 做传递闭包（#181：规划期与写段期共用的唯一实现）。

    纯函数：不读盘、不认识路线。输入只有种子（本段点名的 Subject/Picture 号）、
    全片定义块与素材登记表，输出保留的号与**每条边的来源**——被哪条定义拉进来、
    拉它进来的那句文本、是否命中一致性锚句式。

    三条既有规则原样保留（``compute_segment_scope`` 的外部行为全靠它们）：

    1. **有独立定义行的 Picture 不反向拉引用它的 Subject**：它自己把图讲清楚了，
       反向拉取会把「全片空间系统」这类大 Subject 拖进只拍一间的段（2026-10-22 实测）。
    2. **没有独立定义行的 Picture 反向拉引用它的 Subject**：官方写法就是「图只作为
       来源内联在 Subject 定义里」，这时不反向拉，段内标签会悬空。
    3. **种子为空时保留全部登记图与全部 Subject**：分不清本段用什么时宁可超限报错，
       也不静默让段内无参照。

    锚句式在这里**只做分类标记、不做降级**：本函数不改变任何调用方的可观察行为，只把
    判据的依据（边 ∧ 实际用的种子）交出来。降级是 ``demote_consistency_anchors`` 的事
    （#183），规划期与写段期都调那一份——两处各写一遍，同一张图会在两个阶段被分成两类。
    """
    entries = {(kind, number): text
               for kind, number, text in parse_definition_entries(subject_definitions)
               if kind}
    subject_entries = [(number, text) for (kind, number), text in entries.items()
                       if kind == "subject"]
    seeds = ([("subject", int(n)) for n in seed_subjects]
             + [("picture", int(n)) for n in seed_pictures])
    if not seeds:
        # 无从判定本段素材：保留全部登记图与全部 Subject。只留图不留 Subject 会让
        # 段内定义块半缺——正文引用的实体没有定义，比多喂几张图坏得多。
        seeds = [("picture", int(item[0])) for item in registry]
        seeds += [("subject", number) for kind, number in entries if kind == "subject"]

    kept_subjects: set[int] = set()
    kept_pictures: set[int] = set()
    edges: list[ClosureEdge] = []
    seen: set[tuple[str, int]] = set()
    pending = list(seeds)
    while pending:
        key = pending.pop()
        if key in seen:
            continue
        seen.add(key)
        kind, number = key
        if kind == "subject":
            kept_subjects.add(number)
        else:
            kept_pictures.add(number)
        entry = entries.get(key)
        if not entry:
            if kind == "picture":
                # 反向：图没有独立定义行（官方写法：只作为 Subject 的来源内联），
                # 就以这张图为来源的 Subject 定义也要保留——不拉，段内标签会悬空。
                # 有独立定义行的图**不**走这一支（见 ``compute_segment_scope`` 的注释：
                # 无条件反向拉取会把「全片空间系统」拖进只拍一间的段）。
                for subject_number, subject_text in subject_entries:
                    target = ("subject", subject_number)
                    cited = re.search(rf"<Picture\s+{number}>", subject_text)
                    if not cited:
                        continue
                    evidence = _sentence_at(subject_text, cited.start())
                    # 已经进来的 Subject 也要记边：将来问「这张图为什么被降级」时，
                    # 少一条边就等于少一条解释（kept 集合本来就与是否记账无关）。
                    edges.append(ClosureEdge(
                        target=target, via=key, definition=target,
                        evidence=evidence,
                        anchor=is_consistency_anchor(evidence)))
                    if target not in seen:
                        pending.append(target)
            continue
        for target_kind, target_number, evidence in _citations(entry):
            target = (target_kind, target_number)
            if target == key:
                # 定义行行首的标签是它自己的名字，不是一条引用。留着会让溯源表里
                # 每个节点都多一条「自己拉自己」，查「为什么进来」时反而被淹没。
                continue
            edges.append(ClosureEdge(target=target, via=key, definition=key,
                                    evidence=evidence,
                                    anchor=is_consistency_anchor(evidence)))
            if target not in seen:
                pending.append(target)

    # 同一条边可能被同一句话点两次（``<Picture 1> … <Picture 1>``）：留一份就够，
    # 重复只会在溯源表里制造噪声。保持首次出现的顺序。
    unique_edges: list[ClosureEdge] = []
    for edge in edges:
        if edge not in unique_edges:
            unique_edges.append(edge)

    return AssetClosure(pictures=tuple(sorted(kept_pictures)),
                        subjects=tuple(sorted(kept_subjects)),
                        edges=tuple(unique_edges),
                        seeds=tuple(sorted(seeds)))


def _node_label(node: tuple[str, int]) -> str:
    """溯源里给节点起名：``("subject", 21)`` → ``"Subject 21"``（与段词里的标签同形）。"""
    kind, number = node
    return f"{'Subject' if kind == 'subject' else 'Picture'} {number}"


def demote_consistency_anchors(
    closure: AssetClosure, *, names: Mapping[int, str] | None = None,
) -> tuple[tuple[int, ...], tuple[SegmentAnchor, ...]]:
    """把只靠一致性锚句式勾进来的图从出镜集合里撤下来（#183）。

    判据是**白名单＋传播**，不是黑名单：

    - **种子永远算出镜**（本段正文/规划师点名要拍的图）；
    - 一条**非锚**边（如 ``comes from <Picture N>``）从出镜节点出发，目标也出镜——
      「同一张图若另有出镜边，仍算出镜」；
    - 其余节点算锚：只能经锚边到达的，以及挂在它们下游的（``Subject 21`` 锚到
      ``Subject 22``，``Subject 22`` 自己的定义再落到 ``Picture 22``——图本身没有锚句，
      但它整条来路都是锚）。

    返回 ``(出镜图号, 锚列表)``。``via`` 取**锚链上第一个锚边的定义**，不是图直属的
    那条定义：真实现场问「这张为什么被降级」，答案是 ``Subject 21`` 那句
    ``must agree with <Subject 22>``，而不是 ``Subject 22`` 的 ``comes from``。

    种子为空（``compute_asset_closure`` 退回「保留全部登记图」那一支）时全部算出镜：
    那一路本来就分不清本段用什么，此时降级只会把参考素材悄悄删光。
    """
    seeds = set(closure.seeds)
    if not seeds:
        # 防御性：闭包一定会填 seeds（含全量展开），走到这里说明有人手工拼了一个
        # AssetClosure。宁可一张不降，也不凭一个空种子集把图全撤掉。
        return closure.pictures, ()

    depicted: set[tuple[str, int]] = set(seeds)
    changed = True
    while changed:
        changed = False
        for edge in closure.edges:
            if edge.anchor or edge.target in depicted or edge.via not in depicted:
                continue
            depicted.add(edge.target)
            changed = True

    # 锚的来路：先认「从出镜节点直接跨出去的那条锚边」，再沿闭包往下游继承——
    # 下游节点自己是靠什么进来的，对「为什么被撤」没有解释力。
    causes: dict[tuple[str, int], ClosureEdge] = {}
    changed = True
    while changed:
        changed = False
        for edge in closure.edges:
            if edge.target in depicted or edge.target in causes:
                continue
            if edge.via in causes:
                causes[edge.target] = causes[edge.via]
                changed = True
            elif edge.anchor and edge.via in depicted:
                causes[edge.target] = edge
                changed = True

    name_map = dict(names or {})
    anchors: list[SegmentAnchor] = []
    for number in closure.pictures:
        node = ("picture", number)
        if node in depicted:
            continue
        cause = causes.get(node)
        anchors.append(SegmentAnchor(
            original_picture=number,
            name=name_map.get(number, ""),
            via=_node_label(cause.definition) if cause else "",
            evidence=cause.evidence if cause else "",
        ))
    pictures = tuple(n for n in closure.pictures if ("picture", n) in depicted)
    return pictures, tuple(anchors)


def _mentioned_names(shot_text: str, registry: Sequence[tuple[int, str, str]]
                     ) -> tuple[set[int], set[str]]:
    """Shot 原文里出现的**语义名** → (图号集合, 命中的名字集合)。

    为什么必须按语义名收：官方写法里 ``<Picture N>`` 只在 ``subject_definitions``
    被引（作为 Subject 的来源），**正文走语义名**（CONTEXT.md「参考素材」的指称体系）。
    只收标签的话，真实产物（段正文写「红裙女孩」而不是 ``<Subject 1>``）会一张图都
    不保留——段内正文就成了无参照描述，比多喂几张图坏得多。

    匹配按**最长名字优先**并吃掉已匹配片段：「素材1」是「素材10」的前缀，朴素
    ``in`` 会把长名命中的短名前缀也算一次，凭空多留一张图。名字来自素材登记，
    「无出场」时可能为空串——空名跳过，否则 ``"" in text`` 恒真。
    """
    numbers: set[int] = set()
    found: set[str] = set()
    remaining = str(shot_text or "")
    for picture, name, _desc in sorted(registry, key=lambda item: -len(item[1] or "")):
        if not name:
            continue
        if name in remaining:
            numbers.add(picture)
            found.add(name)
            remaining = remaining.replace(name, " ")
    return numbers, found


def compute_segment_scope(
    *,
    shot_text: str,
    subject_definitions: str,
    registry: Sequence[tuple[int, str, str]],
    refs_per_segment: int = DEFAULT_REFS_PER_SEGMENT,
    paths: Mapping[int, str] | None = None,
    segment_label: str = "",
) -> SegmentAssetScope:
    """算本段要用的参考素材子集（issue #75 AC1/AC3）。

    收料有三条口，缺一条都会让段内正文引用没接线的素材：

    1. Shot 原文里的**标签**（``<Subject N>`` / ``<Picture N>``）；
    2. Shot 原文里的**语义名**（正文走语义名，见 ``_mentioned_names``）；
    3. **沿定义做闭包**——被保留的 Subject 定义里引用的 Subject/Picture、被保留的
       Picture 定义里**反向**被哪些 Subject 引用。闭包必须做，是因为官方写法就是
       「只作为来源的图内联在 Subject 定义里」（ref-en.txt:37）。

    第 3 条本身不由这里实现：本函数只负责收种子（1、2）与判上限，闭包调
    ``compute_asset_closure``（#181 的唯一实现，规划期的素材预算也调它）。

    分不清（Shot 原文既没有标签也没有语义名）时**保留全部**登记图：静默让段内无
    参照比超限报错坏得多，而超限会在这里点名报错，用户还能改。

    ``registry`` 是整片登记 ``(picture, name, description)``（``ref_asset_registry``
    的形态）：编号即原号，名称进接线卡。``paths`` 是原号 → 正本路径（台账口径；
    新台账经统一解析口给出库内绝对路径），缺了就在卡上写「台账未登记正本」，不编路径。

    超过 ``refs_per_segment`` 抛 ``SegmentAssetOverflowError``（点名超限素材）。
    """
    entries = {(kind, number): text for kind, number, text in parse_definition_entries(
        subject_definitions) if kind}
    subject_entries = [(number, text) for (kind, number), text in entries.items()
                       if kind == "subject"]
    seed_subjects, seed_pictures = _labels_in(shot_text)
    by_name, mentioned = _mentioned_names(shot_text, registry)
    seed_pictures |= by_name
    for number, text in subject_entries:
        if any(name in text for name in mentioned):
            seed_subjects.add(number)

    # 闭包只此一份（#181）：规划期算预算（#186）与写段期算子集必须调它，否则同一段
    # 会被算出两个答案，段词与挂图静默不同源。这里只负责「收种子」与「判上限」。
    closure = compute_asset_closure(
        seed_subjects=seed_subjects, seed_pictures=seed_pictures,
        subject_definitions=subject_definitions, registry=registry)

    names = {picture: name for picture, name, _desc in registry}
    path_map = dict(paths or {})
    # 锚降级（#183）：只被「must agree with / consistent with / 与…一致」这类句式勾进来
    # 的图不占槽——它不挂图，只把定义里那句改写成不含标签的描述（见
    # ``rewrite_anchor_picture_labels``）。出镜的判定由闭包的边说话，本函数不另立判据。
    ordered, anchors = demote_consistency_anchors(closure, names=names)
    # 每段张数取「配置值」与「官方单次上限」的较小者（issue #86 / spec #84 D4）：
    # `refs_per_segment` 是本机想喂几张，官方上限是一次请求收得下几张——后者更硬。
    # 不取小值的话，把配置调大就会让卡与工作流对不上，而请求照样非法。
    configured = int(refs_per_segment)
    cap = min(configured, MAX_PICTURES)
    if len(ordered) > cap:
        listed = "、".join(
            f"<Picture {n}> {names.get(n, _MISSING_NAME)}" for n in ordered)
        where = f"{segment_label}：" if segment_label else ""
        # 两个上限是谁卡住的要说破：配置 9 与官方 9 撞车时用户改配置也没用，
        # 只报「超过每段上限 9」会给人「调大配置就能过」的错觉。
        limit_text = (f"官方单次请求上限 {MAX_PICTURES} 图"
                      if configured > MAX_PICTURES
                      else f"每段上限 {configured} 张")
        raise SegmentAssetOverflowError(
            f"{where}本段要用 {len(ordered)} 张参考图，超过{limit_text}"
            f"（{listed}）。超限不截断——截掉的图在正文里就没有接线可落。"
            "出路：把这一段再拆短（素材随镜头走），或精简本段真正需要的素材。")
    return SegmentAssetScope(
        pictures=tuple(
            SegmentAsset(new_picture=new, original_picture=original,
                         name=names.get(original, _MISSING_NAME),
                         path=path_map.get(original, ""))
            for new, original in enumerate(ordered, 1)),
        # Subject 保留闭包全部：锚链上的 Subject 定义也要留在段内，只撤图不撤定义
        # （删掉定义，段词里的 <Subject N> 就悬空了）。
        subject_numbers=closure.subjects,
        anchors=anchors,
    )


def rewrite_picture_labels(text: str, picture_map: Mapping[int, int]) -> str:
    """把文本里的 ``<Picture 原号>`` 全部改成 ``<Picture 新号>``。

    映射里没有的号**原样保留**：那是「引用了未定义图」的既有问题，由校验器当面报，
    不在这里悄悄改名或删除（改名会掩盖问题，删除会让正文少一句）。
    """
    def _replace(match: re.Match) -> str:
        original = int(match.group(1))
        return f"<Picture {picture_map[original]}>" if original in picture_map else match.group(0)

    return _PICTURE_RE.sub(_replace, str(text or ""))


# 被撤下的锚图在定义句里的替身：不含任何标签的名词短语（#183）。按句子语言挑一个，
# 是用一个常量表而不是调模型——改写必须确定性、可复现。
_ANCHOR_PLACEHOLDER_LATIN = "the corresponding reference material"
_ANCHOR_PLACEHOLDER_CJK = "对应参考素材"
_CJK_RE = re.compile(r"[㐀-䶿一-鿿]")


def rewrite_anchor_picture_labels(text: str, anchor_numbers: Iterable[int]) -> str:
    """把被降级为锚的 ``<Picture N>`` 换成不含标签的描述（#183，确定性，不调模型）。

    为什么必须改写：锚的 Subject 定义要留着（否则段词里 ``<Subject N>`` 悬空），可那条
    定义里的「来源图」半句还指着被撤下的图——段内既没有这个号的定义、也没有接线可落，
    模型会照着写一个不存在的槽位。只换标签本身、其余字句原样，是因为模型本来就是按
    「原样复制定义块」被要求的：改写范围越小，段词越接近它写出来的那一版。
    """
    numbers = {int(number) for number in anchor_numbers}
    body = str(text or "")
    if not numbers:
        return body
    placeholder = (_ANCHOR_PLACEHOLDER_CJK if _CJK_RE.search(body)
                   else _ANCHOR_PLACEHOLDER_LATIN)

    def _replace(match: re.Match) -> str:
        return placeholder if int(match.group(1)) in numbers else match.group(0)

    return _PICTURE_RE.sub(_replace, body)


def scope_ref_sections(sections: Mapping[str, str],
                       scope: SegmentAssetScope) -> dict[str, str]:
    """整片六段式 → 只含本段素材的版本（issue #75 AC2）。

    - ``subject_definitions``：裁掉本段用不到的 Subject/Picture 条目，图号换新号；
      **Subject 原号不动**（模型对 Subject 连续性没有要求，只有 Picture 跟接线有关）。
      **锚的 Subject 定义留着**（``scope.subject_numbers`` 含锚链上的 Subject），
      但里面指向被撤锚图的 ``<Picture N>`` 改写成不含标签的描述——删定义会让
      ``<Subject N>`` 悬空，留标签会让段内指着一个没挂的槽位（#183）。
    - ``retention_analysis``：同样按条目裁剪并改写图号；[Shot N] 的出现记录**原样
      保留**——那是整片的出现事实，改成段内编号反而是编造。按**本段镜头**再裁一次
      是另一件事，由 ``scope_retention_to_shots`` 在写段前做（这里只管素材范围：
      镜头范围与素材范围不是一回事，段 2 用的图可以出现在段 5 的镜头里）。
    - ``summary`` / 音轨两段：只改写图号（它们本来就该是段内视角的文本）。
    - ``detailed_description``：原样返回——正文的段内化由写段器负责（机械路径由
      ``rebase_ref_shot_blocks`` 重编号镜头），这里替它改就成第二份写段逻辑。
    """
    picture_map = scope.picture_map
    kept = {("subject", n) for n in scope.subject_numbers}
    kept |= {("picture", item.original_picture) for item in scope.pictures}
    # 锚的**图**条目不留（那张图不挂、也不该在段内被引用），只留引用它的 Subject
    # 定义；定义里指向锚图的那半句改写成不含标签的描述（#183）。
    anchor_numbers = {item.original_picture for item in scope.anchors}
    out = dict(sections)

    def _filter_block(text: str) -> str:
        kept_lines: list[str] = []
        for kind, number, entry in parse_definition_entries(text):
            if kind and (kind, number) not in kept:
                continue
            line = rewrite_picture_labels(entry, picture_map)
            kept_lines.append(rewrite_anchor_picture_labels(line, anchor_numbers))
        return "\n".join(kept_lines)

    if "subject_definitions" in out:
        out["subject_definitions"] = _filter_block(out["subject_definitions"])
    if "retention_analysis" in out:
        out["retention_analysis"] = _filter_block(out["retention_analysis"])
    for name in ("summary", "overall_soundscape", "non_diegetic_music"):
        if name in out:
            out[name] = rewrite_picture_labels(out[name], picture_map)
    return out


# retention_analysis 的两种条目形态（官方示例与模型实际产出各一种）：
#   <Subject 1> (appears in [Shot 1], [Shot 3]): fully_preserved - ...
#   Shot 2, 00:04-00:10: <Subject 1> partially_preserved, ...
# 前者一行跨多个镜头，后者一行只讲一个镜头——两种都要按本段镜头裁。
_EDGE_SHOT_RE = re.compile(r"^\s*Shot\s+(\d+)\s*[,:]\s*", re.IGNORECASE)
_APPEARS_RE = re.compile(r"\(\s*appears in\s+([^)]*)\)", re.IGNORECASE)


def scope_retention_to_shots(text: str, shots: Iterable[int], *,
                             defined_subjects: Iterable[int] | None = None) -> str:
    """retention_analysis 只留本段镜头涉及的条目（2026-10-26 实测）。

    为什么：这份块整片重复时，会把**其它段**的动作清单一起喂给写段器。实测段 2 的
    retention 里还写着段 1 的 ``a backward glance and a lowered gaze``，同一 seed 下
    产出的首帧与段 1 首帧 MAD 只有 27.68（几乎同一构图）——模型把上一段重演了一遍。
    裁到只剩本段镜头后 MAD 升到 46.59，重复消失。

    两种条目形态都要认：``<Subject N> (appears in [Shot 1], [Shot 3])`` 一行跨多镜，
    裁的是**括号里的镜头列表**（保留原文顺序）；``Shot 2, ...`` 一行一镜，镜头号不在
    本段就整行删。不带镜头号的条目（``<Subject 6> stays fully_preserved in every
    shot``、音频标记说明）是全局说明，原样保留。

    ``defined_subjects`` 给了就再按**本段定义块**过滤一层：素材子集（``compute_segment_
    scope``）按图裁，留存分析按镜头裁，两者不同源——段 4 只剩 A 时，缩到 Shot 4 的
    那条仍写着 ``<Subject 1>``/``<Subject 4>``/``<Subject 5>``，全是本段没有定义的
    悬空标签（2026-10-26 实测，校验器不查这一层，静默放过）。按标签切段、整段丢弃
    未定义的那个，同一片段里并存的已定义标签照留。

    一条都没认出来时**原样返回**（不是「没删行就返回」——只改括号里镜头列表也算认出来），
    一条都没留下时也原样返回：格式变了就整块清空，比重复更坏——那等于本段没有留存
    分析，模型失去全部素材约束。
    """
    keep = {int(n) for n in shots}
    defined = {int(n) for n in defined_subjects} if defined_subjects is not None else None
    original = str(text or "").strip()
    if not keep or not original:
        return original

    kept_lines: list[str] = []
    recognised = False
    for line in original.splitlines():
        appears = _APPEARS_RE.search(line)
        if appears:
            recognised = True
            picked: list[int] = []
            for raw in _SHOT_TAG_RE.findall(appears.group(1)):
                number = int(raw)
                if number in keep and number not in picked:
                    picked.append(number)
            if not picked:
                continue
            line = _APPEARS_RE.sub(
                "(appears in " + ", ".join(f"[Shot {n}]" for n in picked) + ")",
                line, count=1)
        else:
            edge = _EDGE_SHOT_RE.match(line)
            if edge:
                recognised = True
                if int(edge.group(1)) not in keep:
                    continue
        if defined is not None:
            line = _drop_undefined_subject_fragments(line, defined)
            if not line.strip():
                continue
        kept_lines.append(line)

    # 一条都没认出来＝格式变了，原样交回给校验器报，别在这里悄悄清空
    if not recognised:
        return original
    body = "\n".join(kept_lines).strip()
    return body or original


# 按标签切「片段」：一个片段 = 一个标签 + 它后面的描述，直到下一个标签。这样
# ``<Subject 1> partially_preserved ..., <Subject 3> attribute_transfer ...`` 能按
# 实体分别取舍，而不是把整行一起判死。
_TAG_SPLIT_RE = re.compile(r"(?=<(?:Subject|Picture)\s+\d+>)")


def _drop_undefined_subject_fragments(line: str, defined: set[int]) -> str:
    """丢掉只提未定义 Subject 的片段；不带标签的碎片跟着前一个片段走。"""
    chunks = _TAG_SPLIT_RE.split(line)
    kept: list[str] = []
    for chunk in chunks:
        tags = [int(n) for n in _SUBJECT_RE.findall(chunk)]
        if not tags:
            # 前缀（``Shot 4, 00:16-00:22: ``）或无标签的延续（``a lowered look and a
            # lifted gaze``）：延续要紧跟它前面被保留的那个片段，所以照留。
            kept.append(chunk)
            continue
        if any(n in defined for n in tags):
            kept.append(chunk)
    # 被丢片段留下的分隔逗号会挂在句尾（``... a lifted gaze,``）：它不是原文语义，
    # 只是切分残留，收掉。
    return "".join(kept).strip().rstrip(",").strip()


def render_segment_injection_card(scope: SegmentAssetScope, *, position: int,
                                  total: int) -> str:
    """本段接线卡（issue #75 AC4）：新号 ↔ 原号 ↔ 素材名 ↔ 正本文件 ↔ 工作流槽位。

    终端打印与 ``segments/injection-card-NN.md`` 落盘**同一份文本**（单一来源）：
    两处各拼一遍，用户看到的与卡上写的就会各漂各的。
    槽位名从 ``injection_card.slot_name`` 取（端口命名唯一来源），不在这里手写前缀。
    """
    from .injection_card import slot_name  # 延迟导入：本模块被写段层 import，别拖配置栈
    from .kinds import KIND_PICTURE

    # 端口数与名字都从生效配置取一次（读盘一次，两处共用）。**端口数**是「工作流有几个
    # 图槽」，未用槽要点到真实端口——用官方上限 9 去截会把它算成假的（配置 24 槽时
    # 「未用槽 ref_image_9…8」是个倒着的空区间）。官方上限另行写在说明里（issue #86）。
    active = workflow_settings()
    ports = active.slot_count(KIND_PICTURE)
    used = len(scope.pictures)
    if used == 0:
        unused = (f"未用槽：图槽全部（ref_image_0 … ref_image_{ports - 1}）留空或 bypass"
                  "——本段不引用任何参考图。")
    elif used >= ports:
        unused = (f"未用槽：无（本段用满 {ports} 个图槽；官方单次请求上限 {MAX_PICTURES} 图）。")
    else:
        unused = (f"未用槽：ref_image_{used} … ref_image_{ports - 1} 留空或 bypass"
                  f"（本段只喂 {used} 张，官方单次请求上限 {MAX_PICTURES} 图）——"
                  "空槽是正常的，别把上一段的图接进来。")
    lines = [
        f"## 本段参考素材接线（段 {position}/{total}）",
        "",
        # 段卡也点名登记工作流（issue #86 AC1）：整片卡与段卡都只许指向 9 槽 V2，
        # 用户照卡进 ComfyUI 时不必再回头翻菜单确认打开的是哪一张。
        f"登记工作流：{active.ref_workflow_name}（图槽 {ports} 个）",
        "",
        "| 新号 | 原号 | 素材名 | 正本文件 | 工作流槽位 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for item in scope.pictures:
        slot = slot_name(KIND_PICTURE, item.new_picture)
        lines.append(
            f"| <Picture {item.new_picture}> | <Picture {item.original_picture}> "
            f"| {item.name} | {item.path or _MISSING_PATH} | {slot} |")
    if not scope.pictures:
        lines.append("| （本段不引用任何参考图） | | | | |")
    lines += [
        "",
        "接线：按上表**新号**升序把「正本文件」接到「工作流槽位」；段内提示词里的"
        "`<Picture N>` 就是**新号**，不是整片注入卡上的号。",
        unused,
    ]
    return "\n".join(lines) + "\n"


def total_pictures(scope: SegmentAssetScope) -> int:
    """本段喂几张图（给调用方判上限/打印用，读的是同一份 scope）。"""
    return len(scope.pictures)


__all__ = [
    "AssetClosure",
    "CONSISTENCY_ANCHOR_PATTERNS",
    "ClosureEdge",
    "SegmentAnchor",
    "SegmentAsset",
    "SegmentAssetOverflowError",
    "SegmentAssetScope",
    "compute_asset_closure",
    "compute_segment_scope",
    "demote_consistency_anchors",
    "is_consistency_anchor",
    "parse_definition_entries",
    "render_segment_injection_card",
    "rewrite_anchor_picture_labels",
    "rewrite_picture_labels",
    "scope_ref_sections",
    "scope_retention_to_shots",
    "total_pictures",
]

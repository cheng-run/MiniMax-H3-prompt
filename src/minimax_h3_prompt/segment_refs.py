"""段内参考素材子集与重编号（issue #75 / ADR 0017「代价」）。

多参（ref2va）整片提示词里，``subject_definitions`` 定义的是**全片**素材（一次真实
运行有 24 张图、25 个 Subject）。整批喂给 H3 会把 Qwen3-VL 的文本编码序列撑到约
5.4 万 token；本机 flash/efficient SDPA 都缺 kernel，只剩朴素注意力，``einsum`` 当场
要 232 GiB（2026-10-22 的 ``torch.OutOfMemoryError``）。所以**每段只喂它用到的素材**
（ADR 0017：9 是每段默认值仍然合理，作为全局容量才是错的）。

本模块是那一步的纯函数层：不读盘、不调模型、不打印——输入本段 Shot 原文与整片定义，
输出「保留哪些素材、图号怎么改」。三件事分开做，是因为它们各有各的失败形态：

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
from .workflow_config import DEFAULT_REFS_PER_SEGMENT, workflow_settings

# 官方标签（ref-en.txt §2）：<Subject N> / <Picture N>。与校验器同一套形态，
# 但这里只做「收集与改写」，判据（未定义/未使用/跳号）仍住在 ``tools.h3_validator``。
_PICTURE_RE = re.compile(r"<Picture\s+(\d+)>")
_SUBJECT_RE = re.compile(r"<Subject\s+(\d+)>")

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
class SegmentAssetScope:
    """一段的参考素材子集：保留的图（新号↔原号）与保留的 Subject 原号。"""

    pictures: tuple[SegmentAsset, ...]
    subject_numbers: tuple[int, ...] = ()

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
        """落 ``plan.json`` 的 ``assets_in_segment``：续跑照这张表改写，不重算。"""
        return {
            "pictures": [
                {"new": item.new_picture, "original": item.original_picture,
                 "name": item.name, "path": item.path}
                for item in self.pictures
            ],
            "subjects": list(self.subject_numbers),
        }

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
        return cls(pictures=tuple(pictures),
                   subject_numbers=tuple(int(n) for n in subjects_raw))


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

    分不清（Shot 原文既没有标签也没有语义名）时**保留全部**登记图：静默让段内无
    参照比超限报错坏得多，而超限会在这里点名报错，用户还能改。

    ``registry`` 是整片登记 ``(picture, name, description)``（``ref_asset_registry``
    的形态）：编号即原号，名称进接线卡。``paths`` 是原号 → 正本路径（台账口径；
    新台账经统一解析口给出库内绝对路径），缺了就在卡上写「台账未登记正本」，不编路径。

    超过 ``refs_per_segment`` 抛 ``SegmentAssetOverflowError``（点名超限素材）。
    """
    kept_subjects: set[int] = set()
    kept_pictures: set[int] = set()
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
    if not seed_subjects and not seed_pictures:
        # 无从判定本段素材：保留全部（超限照报，不静默饿死本段）。Subject 也一并保留
        # ——只留图不留 Subject 会让段内定义块半缺，正文引用的实体没有定义。
        seed_pictures = {picture for picture, _name, _desc in registry}
        seed_subjects = {number for (kind, number) in entries if kind == "subject"}

    pending: list[tuple[str, int]] = [("subject", n) for n in seed_subjects]
    pending += [("picture", n) for n in seed_pictures]
    seen: set[tuple[str, int]] = set()
    while pending:
        key = pending.pop()
        if key in seen:
            continue
        seen.add(key)
        kind, number = key
        if kind == "subject":
            kept_subjects.add(number)
            entry = entries.get(key)
            if entry:
                subjects, pictures = _labels_in(entry)
                pending += [("subject", n) for n in subjects - kept_subjects]
                pending += [("picture", n) for n in pictures - kept_pictures]
            continue
        kept_pictures.add(number)
        entry = entries.get(key)
        if entry:
            # 独立 Picture 定义：它自己讲清楚了这张图，所以**不**反向拉「所有引用这张图
            # 的 Subject」——2026-10-22 实测：段 1 只用到 <Picture 1>，反向闭包把「公寓
            # 材质与空间系统」Subject 6 拉进来（它引用 Picture 1/4/9/10/24），于是夜间
            # 卧室、卸妆棉、窗边起居区全被拖进本段，8 张里 4 张本段根本拍不到。
            # 反向拉取只服务「图没有独立定义行、只内联在 Subject 里」的官方写法（见
            # else 分支）。
            subjects, pictures = _labels_in(entry)
            pending += [("subject", n) for n in subjects - kept_subjects]
            # Picture→Picture：定义里引用的其他参考图（如夜间卧室引用夜灯/充电器）
            # 也是本段的依赖。漏掉它们，段内会留下 `<Picture N>` 悬空引用：模型看到
            # 标签却拿不到图，校验器也会报 PICTURE_UNDEFINED。
            pending += [("picture", n) for n in pictures - kept_pictures]
        else:
            # 反向：以这张图为来源的 Subject 定义也要保留（「只作为来源的图内联在
            # Subject 定义里」是官方推荐写法，图自己没有独立定义行）。
            for subject_number, subject_text in subject_entries:
                if subject_number in kept_subjects:
                    continue
                if re.search(rf"<Picture\s+{number}>", subject_text):
                    pending.append(("subject", subject_number))

    names = {picture: name for picture, name, _desc in registry}
    path_map = dict(paths or {})
    ordered = sorted(kept_pictures)
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
        subject_numbers=tuple(sorted(kept_subjects)),
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


def scope_ref_sections(sections: Mapping[str, str],
                       scope: SegmentAssetScope) -> dict[str, str]:
    """整片六段式 → 只含本段素材的版本（issue #75 AC2）。

    - ``subject_definitions``：裁掉本段用不到的 Subject/Picture 条目，图号换新号；
      **Subject 原号不动**（模型对 Subject 连续性没有要求，只有 Picture 跟接线有关）。
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
    out = dict(sections)

    def _filter_block(text: str) -> str:
        kept_lines: list[str] = []
        for kind, number, entry in parse_definition_entries(text):
            if kind and (kind, number) not in kept:
                continue
            kept_lines.append(rewrite_picture_labels(entry, picture_map))
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
_SHOT_TAG_RE = re.compile(r"\[Shot\s+(\d+)\]")
_APPEARS_RE = re.compile(r"\(\s*appears in\s+([^)]*)\)", re.IGNORECASE)
_EDGE_SHOT_RE = re.compile(r"^\s*Shot\s+(\d+)\s*[,:]\s*", re.IGNORECASE)


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
    from .asset_ledger import KIND_PICTURE

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
    "SegmentAsset",
    "SegmentAssetOverflowError",
    "SegmentAssetScope",
    "compute_segment_scope",
    "parse_definition_entries",
    "render_segment_injection_card",
    "rewrite_picture_labels",
    "scope_ref_sections",
    "scope_retention_to_shots",
    "total_pictures",
]

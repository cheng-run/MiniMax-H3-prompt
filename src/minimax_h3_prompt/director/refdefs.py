"""导演台 r2v 的定义块：阶段 1 的中文素材 → 官方英文六节外壳的第 1、3 节。

**为什么要这一步（2026-11-01 真实验收现场）**：r2v 段提示词是 ref 六节外壳，
第 1、3 节（``subject_definitions`` / ``retention_analysis``）由组装器**逐字**取用。
取用的源是阶段 1 ``reference_consistency`` 节点产出的 ``subject_defs``，而那份素材：

1. 是**中文 markdown**——角色提示词第一句就是「你的产出（markdown）」，模型于是写
   ``## subject_definitions``；``split_ref_sections`` 只认行首裸标签
   ``subject_definitions:``，解析结果是**空字典**，写段当场炸在「定义块缺失」上。
2. 语言也不对——r2v 段要求全英文（``segment_writer_director.md`` §r2v、issue #75 的
   语言闸），中文定义原样复制进去就是「英文正文 + 中文定义块」。

所以导演台 r2v 在写段之前**必须**把中文素材再走一次官方口径，产出可解析、全英文、
每张登记图都被引用的 1、3 节（引用 = 作为某个 Subject 的来源图，或独立的 Picture 行；
官方 ref-en.txt §2.2 只给帧／构图锚单开行）。这一步只做一次（不是每段一次）：定义块
逐段重复，逐段重写就是让同一批素材每段漂一个版本。

产物落 ``GENxxx/director/ref-sections.json``：续跑、改词、打包都从它读，不重付、也不
让第二处再推一遍「哪张图叫什么」。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

# 定义块产物的文件名与角色名。角色名必须与写段/改词分开（ADR 0019 的成本账）：
# 混在一处就分不清钱花在「定义块」还是「逐段写」上。
REF_SECTIONS_FILENAME = "ref-sections.json"
DIRECTOR_REF_SECTIONS_ROLE = "role_director_ref_sections"

# 只有这四个判据能拦住「写进去也白写」的定义块，其余（六节齐全、镜头、时间戳）由写段
# 那一侧管：这里只有两节，拿整套 ref 校验去判会报一堆与定义块无关的假错。
_GUARDED_CODES = frozenset({
    "PICTURE_NO_DEFINITION",   # 引用了没定义的图
    "PICTURE_UNUSED",          # 登记了却没被定义——段词里那句 <Picture N> 会悬空
    "PICTURE_UNDEFINED",       # 引用了登记表里没有的号
    "BODY_NOT_ENGLISH",        # 中文散文（issue #75）
})

# 合并不了是**写法偏好**不是缺陷（L6-09 / 票 #134）：逐图各建一个 Subject 照样能给
# 模型用，只是同一个人被拆成几份。所以这条只出站、不进 ``_GUARDED_CODES``、不占重写
# 预算——把能跑的产物打回重写，就等于让一条建议挡住交付。
SUBJECT_MERGE_CANDIDATE = "SUBJECT_MERGE_CANDIDATE"


@dataclass(frozen=True)
class RefSections:
    """全局定义块：两节正文 + 生成时用的登记表（编号 ↔ 语义名）。

    ``registry`` 必须跟着产物一起存：段内素材子集按**语义名**认图，打包按**语义名**
    找正本；只存正文的话，两处都得再猜一次「<Picture 3> 是哪件素材」。
    """

    subject_definitions: str
    retention_analysis: str
    registry: tuple[tuple[int, str], ...] = ()

    def as_mapping(self) -> dict[str, str]:
        return {
            "subject_definitions": self.subject_definitions,
            "retention_analysis": self.retention_analysis,
        }

    def to_dict(self) -> dict:
        return {
            "subject_definitions": self.subject_definitions,
            "retention_analysis": self.retention_analysis,
            "registry": [list(item) for item in self.registry],
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "RefSections":
        registry = tuple(
            (int(pair[0]), str(pair[1])) for pair in raw.get("registry") or ())
        return cls(
            subject_definitions=str(raw.get("subject_definitions", "")),
            retention_analysis=str(raw.get("retention_analysis", "")),
            registry=registry,
        )


def render_sections(sections: RefSections | dict) -> str:
    """两节 → 可被 ``split_ref_sections`` 解析的正文（行首裸标签，不加 markdown 装饰）。

    解析与校验都吃这个形态：定义块最终要进官方六节外壳，段头写法只有一种。
    """
    body = sections if isinstance(sections, dict) else sections.as_mapping()
    return (
        "subject_definitions:\n"
        f"{str(body.get('subject_definitions', '')).strip()}\n\n"
        "retention_analysis:\n"
        f"{str(body.get('retention_analysis', '')).strip()}"
    )


def _registry_rows(registry: Iterable[Sequence]) -> list[tuple[int, str]]:
    rows: list[tuple[int, str]] = []
    for item in registry:
        number, name = int(item[0]), str(item[1])
        rows.append((number, name))
    return rows


def _build_request(material: str, registry: Sequence[Sequence]) -> str:
    rows = _registry_rows(registry)
    listed = "\n".join(f"<Picture {number}> = {name}" for number, name in rows)
    return (
        "【任务】把下面这份中文定义素材改写成 MiniMax H3 参考生视频提示词的两节：\n"
        "`subject_definitions 与 retention_analysis`（它们会逐字进入每一段提示词）。\n\n"
        "【硬性格式】\n"
        "1. 只输出这两节，段头**逐字**是行首的 `subject_definitions:` 与 "
        "`retention_analysis:`——不要 `##`、不要粗体、不要编号、不要 markdown 标题。\n"
        "2. 两节全部英文：中文语义名要**翻成英文**再写（人物姓名等专名可保留原文，"
        "但描述词必须是英文）——校验器按「中文占比 + 绝对字数」判语言，"
        "十三个中文素材名原样堆在定义里就会被判成中文散文。\n"
        f"3. 下面 {len(rows)} 张登记图**每一张**都必须被**引用**——不是各占一个定义行："
        "要么作为某个 `<Subject N>` 的来源图写进它的定义，要么作为独立的 `<Picture N>` "
        "行；编号逐字沿用、不得改号、不得漏。\n"
        "4. retention_analysis 里每张图一行，关系标记只许用官方取值：\n"
        "   fully_preserved / partially_preserved / attribute_transfer / weak_reference。\n"
        "5. 定义句照官方写法：`<Subject N> is the …` 描述可复用的可见内容，"
        "`<Picture N> is …` 指向具体的那张图。\n"
        "   同一角色的多张母版（身份／表情／服装……）**合成一个 Subject**，逐图说明各提供"
        "什么，例如：`<Subject N> is … whose appearance comes from <Picture 1>, whose "
        "expressions come from <Picture 2>, and whose outfit comes from <Picture 3>.`\n"
        "   图只作为某个 Subject 的来源时，**不单开** `<Picture N>` 行；只有图本身就是某一"
        "镜的首帧／关键帧／尾帧／构图锚时，才单开 `<Picture N>` 行。\n\n"
        "【参考登记（编号 = 语义名，不得改号）】\n"
        f"{listed}\n\n"
        "【中文定义素材】\n"
        f"{str(material or '').strip()}\n"
    )


@dataclass(frozen=True)
class DefinitionCheck:
    """定义块的检查结论。两类结论**分开承载**，因为处置方式相反：

    - ``problems``（硬错）：进重写预算——不合格就退回重写，预算用尽即抛。
    - ``warnings``（建议）：只出站不判死。合并主体是写法偏好，现行逐图各建 Subject
      的定义块仍然可用；混进 problems 会把一份能跑的产物打回重写。
    """

    problems: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


# 定义条目的行首标签与正文里的任意标签。分开两个正则：行首那个决定「一条定义从哪开始」，
# 正文那个用来找它引用了谁（官方合并句式正是一条 Subject 行里点名多张 Picture）。
_DEF_LABEL_RE = re.compile(r"^<(Subject|Picture|Video|Audio)\s+(\d+)>")
_ANY_LABEL_RE = re.compile(r"<(Subject|Picture|Video|Audio)\s+(\d+)>")


def _definition_entries(text: str) -> list[tuple[str, int, str]]:
    """把 ``subject_definitions`` 切成一条条定义：行首带标签的行开新条，续行归上一条。

    续行必须跟着走：合并句式是一条长句，折行之后图片引用会掉到下一行去，只看行首
    就会把「同一个主体的三张来源图」读成三张没人认领的图。
    """
    entries: list[list] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        head = _DEF_LABEL_RE.match(stripped)
        if head:
            entries.append([head.group(1), int(head.group(2)), stripped])
        elif entries:
            entries[-1][2] = f"{entries[-1][2]} {stripped}"
    return [(kind, number, body) for kind, number, body in entries]


def _semantic_prefix(name: str) -> str:
    """语义名里「同一个角色」的那一段：**去掉最后一段主责**后的前缀。

    母版清单（#133）把同一个人的多张母版写成 ``CHAR_01_身份母版`` / ``CHAR_01_表情母版``
    / ``CHAR_01_服装母版``——变化只在最后那一段（主责），前面是角色自己。所以前缀是
    「除最后一段以外的全部」，而不是第一段：``CHAR_01`` 与 ``CHAR_02`` 的第一段同为
    ``CHAR``，按第一段分组会把两个不同角色判成同一个（假建议）。
    不带 ``_`` 的语义名（道具、场景、只有一张母版的角色）不分组：没有主责段就分不清
    「同一个角色的另一张」与「另一件东西」，凭名字猜只会造一批假建议。
    """
    head, sep, _tail = str(name or "").rpartition("_")
    return head if sep and head.strip() else ""


def _merge_warnings(text: str, registry: Sequence[Sequence]) -> tuple[str, ...]:
    """同一语义名前缀下出现多个 ``<Subject N>`` → 提示可合并（L6-09，票 #134）。

    关联方向两条都认，因为官方两种写法都合法：

    - ``<Picture N>`` 行认领来源主体（存量实跑的形状：``… image for <Subject 1>``）；
    - ``<Subject N>`` 行内联来源图（官方合并句式：``whose appearance comes from
      <Picture 1>``）。

    只认显式点名：一条 Picture 行既没指名主体、也没被任何主体指名时，宁可漏报——这是
    建议，不是判据，猜错只会让用户去合并不该合的东西。
    """
    from ..tools.h3_validator import split_ref_sections

    names = {int(item[0]): str(item[1]) for item in registry}
    pictures = split_ref_sections(text).get("subject_definitions", "")
    prefix_of = {number: _semantic_prefix(names.get(number, "")) for number in names}

    grouped: dict[str, set[int]] = {}
    for kind, number, body in _definition_entries(str(pictures)):
        labels = _ANY_LABEL_RE.findall(body)
        links: list[tuple[int, int]] = []   # (登记图号, 主体号)
        if kind == "Subject":
            links = [(int(pic), number) for label, pic in labels if label == "Picture"]
        elif kind == "Picture":
            links = [(number, int(subject)) for label, subject in labels
                     if label == "Subject"]
        for picture, subject in links:
            prefix = prefix_of.get(picture, "")
            if prefix:
                grouped.setdefault(prefix, set()).add(subject)

    return tuple(
        f"{SUBJECT_MERGE_CANDIDATE}: {'、'.join(f'<Subject {n}>' for n in sorted(subjects))}"
        f" 的来源图共享语义名前缀「{prefix}」——看起来是同一角色的多张母版，"
        "可合并为一个 Subject，逐图说明各提供什么（身份／表情／服装）"
        for prefix, subjects in sorted(grouped.items()) if len(subjects) >= 2)


def _guard_issues(text: str, registry: Sequence[Sequence]) -> DefinitionCheck:
    """定义块的判据：四类硬错（进重写预算）＋ 一条合并建议（只出站）。"""
    from ..tools.h3_validator import split_ref_sections, validate_ref

    sections = split_ref_sections(text)
    problems: list[str] = []
    for key in ("subject_definitions", "retention_analysis"):
        if not str(sections.get(key, "")).strip():
            problems.append(
                f"MISSING_SECTION: 解析不出 `{key}:` 这一节（段头必须行首裸标签）")
    ref_meta = [(int(item[0]), str(item[1]), "") for item in registry]
    for issue in validate_ref(text, ref_meta=ref_meta):
        if issue.severity == "error" and issue.code in _GUARDED_CODES:
            problems.append(f"{issue.code}: {issue.message}")
    return DefinitionCheck(
        problems=tuple(problems),
        warnings=_merge_warnings(text, registry),
    )


def build_ref_sections(*, material: str, registry: Sequence[Sequence], llm,
                       attempts: int | None = None) -> RefSections:
    """中文素材 → 英文两节（有界重试）。

    调用失败（返回 None）当场抛 ``RuntimeError``：那是网关/卡住，重试同一段请求毫无
    意义，与「内容不合格再写一遍」必须分开报（ADR 0019）。
    """
    from ..segment_prompts import MAX_SEGMENT_ATTEMPTS, _direct_write

    budget = attempts or MAX_SEGMENT_ATTEMPTS
    base = _build_request(material, registry)
    request = base
    problems: tuple[str, ...] = ()
    for attempt in range(budget):
        raw = _direct_write(llm, request, role=DIRECTOR_REF_SECTIONS_ROLE,
                            label="导演台 r2v 定义块")
        if raw is None:
            raise RuntimeError(
                "导演台 r2v 定义块调用失败（网关/卡住/空回复，见上一行警告）："
                "定义块是六节外壳的第 1、3 节，没有它写出来的段就是无参照描述")
        text = str(raw)
        check = _guard_issues(text, registry)
        problems = check.problems
        if not problems:
            from ..tools.h3_validator import split_ref_sections

            # 建议只在**定稿**这一版出站：退回重写时也念一遍，用户会以为模型漏改了。
            for warning in check.warnings:
                print(f"[警告] 定义块：{warning}")
            sections = split_ref_sections(text)
            return RefSections(
                subject_definitions=str(sections["subject_definitions"]).strip(),
                retention_analysis=str(sections["retention_analysis"]).strip(),
                registry=tuple(_registry_rows(registry)),
            )
        if attempt + 1 == budget:
            break
        request = (
            f"{base}\n\n"
            "【上一次产出被校验器判为不合格，请按下列问题修正后重写】\n"
            + "\n".join(f"- {line}" for line in problems)
        )
    raise ValueError(
        f"导演台 r2v 定义块不合格（重写预算 {budget} 次已用尽）："
        + "；".join(problems))


def sections_path(session_dir: str | Path) -> Path:
    """产物落点：``GENxxx/director/ref-sections.json``（与 plan.json 同目录）。"""
    from .planning import DIRECTOR_DIRNAME

    return Path(session_dir) / DIRECTOR_DIRNAME / REF_SECTIONS_FILENAME


def save_ref_sections(session_dir: str | Path, sections: RefSections) -> Path:
    path = sections_path(session_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(sections.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return path


def load_ref_sections(session_dir: str | Path) -> RefSections | None:
    path = sections_path(session_dir)
    if not path.is_file():
        return None
    raw = json.loads(path.read_text(encoding="utf-8"))
    return RefSections.from_dict(raw)


def has_sections(sections: RefSections | None) -> bool:
    """两节都非空才算有定义块（只有一节等于半份空定义，组装器照样拼不出来）。"""
    if sections is None:
        return False
    return all(
        str(getattr(sections, key, "")).strip()
        for key in ("subject_definitions", "retention_analysis"))


_CJK_RE = re.compile(r"[一-鿿]")


def cjk_chars(text: str) -> int:
    """中文汉字个数：测试与报错措辞共用，不各写一份正则。"""
    return len(_CJK_RE.findall(str(text or "")))

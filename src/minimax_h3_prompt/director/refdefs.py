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
每张登记图都有定义的 1、3 节。这一步只做一次（不是每段一次）：定义块逐段重复，逐段
重写就是让同一批素材每段漂一个版本。

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
        f"3. 下面 {len(rows)} 张登记图**每一张**都要在 subject_definitions 里有定义，"
        "编号逐字沿用、不得改号、不得漏。\n"
        "4. retention_analysis 里每张图一行，关系标记只许用官方取值：\n"
        "   fully_preserved / partially_preserved / attribute_transfer / weak_reference。\n"
        "5. 定义句照官方写法：`<Subject N> is the …` 描述可复用的可见内容，"
        "`<Picture N> is …` 指向具体的那张图（可作为某个 Subject 的来源）。\n\n"
        "【参考登记（编号 = 语义名，不得改号）】\n"
        f"{listed}\n\n"
        "【中文定义素材】\n"
        f"{str(material or '').strip()}\n"
    )


def _guard_issues(text: str, registry: Sequence[Sequence]) -> list[str]:
    """定义块的四类硬判据；返回可读的问题清单（空＝通过）。"""
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
    return problems


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
    problems: list[str] = []
    for attempt in range(budget):
        raw = _direct_write(llm, request, role=DIRECTOR_REF_SECTIONS_ROLE,
                            label="导演台 r2v 定义块")
        if raw is None:
            raise RuntimeError(
                "导演台 r2v 定义块调用失败（网关/卡住/空回复，见上一行警告）："
                "定义块是六节外壳的第 1、3 节，没有它写出来的段就是无参照描述")
        text = str(raw)
        problems = _guard_issues(text, registry)
        if not problems:
            from ..tools.h3_validator import split_ref_sections

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

"""导演台尾帧变换提示词（issue #107）：三段式图生图提示词、触发集合、确定性检查。

**为什么它是第三种提示词产物**（CONTEXT.md「尾帧变换提示词」）：段提示词写的是**视频**
（壁纸一样从这一帧演到下一帧），首帧提示词是**文生图**，而这一份是**图生图**——输入是
本段起点画面（上一段剥出的末帧），输出是「怎样把它变成本段终点画面」。终点画面不在
包里（设计 §3.1）：打包时上一段还没跑，图不存在，所以它只能由用户拿这份词现生成、
在导演台 UI 里逐段挂。混成文生图提示词，生成出来的图与成片不同源，接缝跳变会被下一段
的段间引导直接继承下去。

**触发集合从给图契约现算，不手抄字面量**：需要终点画面的段，正是「必填给图清单在
**任意模式**下都含终点画面」的那几类——``camera_same`` / ``camera_cut`` / ``ending``。
``opening`` 在 ``fl2v`` 下终点画面只是**可选**、``continue`` 一个图都不给，故都不触发。
另抄一份 ``("camera_same", "camera_cut", "ending")`` 会与 ``frames.give_images_for``
各自漂：段类型规则一改，这里就静默失效（本仓「规则在、接线不在」的老病）。

**三段式由代码拼、正文由 LLM 写**：段头是结构契约，模型的服从性不参与——它只写三段
正文，段头由 ``normalize_tailframe_output`` 按常量重排重贴；缺段由校验器点名。这与
``director/prompts.py`` 的「注记由组装器注入」是同一条纪律。

**纪律只在警告级**：裁定见 ADR 0018——把生图纪律做成 error 闸门会被合法写法误伤。
结构性缺陷（缺段、清单转述正文）才是 error，它触发**有界重写**（同段提示词），
重写预算用尽仍**照常落盘**并带着问题清单交回——票面 AC3「违规有告警，不阻断产出」。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

# 跨模块取这几个名字都是**刻意的复用**，不是抄近路：
# - ``_locked_hit`` 是「条目是不是正文原文片段」的唯一谓词（字面互为子串）。这里
#   重写一份判据，两处就会对「转述算不算命中」各执一词（本仓「同一契约不许两处口径」）。
# - ``_direct_write`` 是直调的唯一出口（看门狗 + 失败当场打印，ADR 0019 / issue #70）；
#   另写一遍就是第二套等待体验。
# - ``check_image_prompt`` / ``discipline_text`` 是生图纪律的唯一读取口与检查器
#   （ADR 0018）；尾帧变换提示词与资产图写的是同一件事的两种产物，纪律不能各有一套。
from ..asset_proofread import _locked_hit
from ..image_config import DEFAULT_IMAGE_MODEL
from ..segment_prompts import MAX_SEGMENT_ATTEMPTS, _direct_write
from ..tools.h3_validator import ValidationIssue
from ..tools.image_prompt_discipline import check_image_prompt, discipline_text
from ..tools.reference_auditor import describe_image
from .frames import (
    IMAGE_END,
    PLAN_MODES,
    SEGMENT_TYPES,
    DirectorPlan,
    DirectorSegment,
    give_images_for,
    is_guided,
    segment_type_label,
)

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"
_INSTRUCTION_NAME = "tailframe_writer_director.md"

# 直调路径在事件与成本表里的角色名：**必须**与写段角色分开（ADR 0019）——尾帧提示词
# 是图生图那条钱的出口，混进写段就分不清钱花在「写视频词」还是「生成终点画面」上。
DIRECTOR_TAILFRAME_WRITER_ROLE = "role_director_tailframe_writer"

# 三段式的段头。``### `` 前缀跟的是**产品**里的既有形态：资产提示词产物的锁死清单就是
# 三级标题（``generation.py:1109``），尾帧文档是同一类给人复制粘贴的产物，不该另造格式。
# 段头名只住这里：请求里下发、归一里重贴、校验里比对，三处全部现取（``tailframe_sections``），
# 重抄一份字面量就会在改名时静默失效（「永不为红＝没有」）。
SECTION_PREFIX = "### "
TRANSFORM_HEAD = f"{SECTION_PREFIX}变换指令"
PRESERVE_HEAD = f"{SECTION_PREFIX}保留项声明"
LOCKED_HEAD = f"{SECTION_PREFIX}锁死清单"

# 请求里的两块注记（只给模型看，不进产物）。读者不同，故与产物段头分开。
_START_READING_HEAD = "【起点画面读图结果】"
_SECTION_REQUIREMENT_HEAD = "【三段式段头】"

# 读图指令：保留项声明要逐项写「背景 / 光照方向 / 其余人物 / 服装 / 道具」，读图就必须
# 按这几类给事实；默认读图指令面向角色一致性，问不到光线方向与陈设。
_START_READING_INSTRUCTION = (
    "这张图是一段 AI 视频某一段的第一帧，之后要由视觉模型改写成该段的终点画面。\n"
    "请只描述图里真实可见的内容，逐项列出：主体（人数、朝向、姿态、服装颜色与款式、标志物）、\n"
    "背景陈设与它们的相对位置、光源方向与色温、地面积水/反射等可复述的细节。\n"
    "用中文输出 4-6 句，不写心理活动与剧情。"
)

# 尾帧文档的落点：``GENxxx/director/tailframe-NN.md``（1 起，与 ``segment-NN.md`` 同目录）。
# 编号跟**段号**而不是第几个产物：用户是拿「第 3 段的终点画面」去挂导演台的，重编号会挂错槽。
TAILFRAME_FILENAME = "tailframe-{position:02d}.md"


def tailframe_segment_types() -> tuple[str, ...]:
    """需要终点画面的段类型：必填给图清单在**所有**可规划模式下都含终点画面。

    现算而不是常量快照：给图契约（``frames.give_images_for``）是唯一真源，这里只在它上面
    加一层「恒含终点画面」的筛选。调用时求值，改契约的用例才咬得住（测试用 monkeypatch
    把 ``continue`` 改成要终点画面，断言触发集合跟着变）。
    """
    return tuple(
        segment_type for segment_type in SEGMENT_TYPES
        if all(IMAGE_END in give_images_for(segment_type, mode) for mode in PLAN_MODES)
    )


def needs_tailframe(segment_type: str) -> bool:
    """该段类型要不要终点画面；未知类型**报错**（同 ``frames.is_guided`` 的口径，不回落）。"""
    is_guided(segment_type)
    return segment_type in tailframe_segment_types()


def tailframe_sections() -> tuple[str, str, str]:
    """三段式的段头与**出站顺序**（现取常量，便于变异检验改头即改口）。"""
    return (TRANSFORM_HEAD, PRESERVE_HEAD, LOCKED_HEAD)


def load_instruction() -> str:
    """尾帧变换的写作指令（角色提示词唯一一份，不按模式分节——这是图生图，与模式无关）。"""
    return (PROMPTS_DIR / _INSTRUCTION_NAME).read_text(encoding="utf-8")


def tailframe_path(session_dir: str | Path, position: int) -> Path:
    """第 ``position`` 段（1 起）的尾帧文档路径。"""
    return Path(session_dir) / "director" / TAILFRAME_FILENAME.format(position=position)


# ---------------------------------------------------------------------------
# 请求 → 归一 → 校验（纯函数）
# ---------------------------------------------------------------------------

def build_tailframe_request(segment: DirectorSegment, *, start_image: str | Path,
                            start_frame_description: str,
                            model: str = DEFAULT_IMAGE_MODEL) -> str:
    """组装一次图生图写作请求（入参全部显式，不依赖 state——issue #26 的教训）。

    起点画面的内容由**视觉模型读图**给出（``describe_image`` 是读图唯一口），写词模型
    拿到的是一段可复述的事实文本：让它凭空想「上一段结束时画面里有什么」，保留项声明
    就会写成设计稿而不是这张图上真有的东西。
    """
    if not needs_tailframe(segment.type):
        raise ValueError(
            f"第 {segment.index} 段类型 {segment.type!r}（{segment_type_label(segment.type)}）"
            "不需要终点画面：尾帧变换只对运镜段与收尾定格段产出"
            f"（触发集合：{'/'.join(tailframe_segment_types())}）")
    heads = "\n".join(tailframe_sections())
    return (
        f"{load_instruction()}\n\n"
        "---\n"
        f"本段编号：Segment {segment.index}\n"
        f"段类型：{segment_type_label(segment.type)}\n"
        f"本段剧情概述（中文，仅供你理解剧情，不得写进提示词）：{segment.summary}\n"
        f"本段落定的画面（终点画面必须落到这个状态）：{segment.end_hook}\n"
        f"起点画面（参考图，本段第一帧；终点画面由它变换而来）：{start_image}\n"
        f"\n{_START_READING_HEAD}\n{str(start_frame_description).strip()}\n"
        f"\n{_SECTION_REQUIREMENT_HEAD}（逐字照抄、顺序不变）：\n{heads}\n"
        f"\n{discipline_text(model)}\n"
    )


def split_tailframe_sections(text: str) -> dict[str, str]:
    """按段头切三段；只收**行首整行**等于段头的那几行（产物形态，不做模糊归一）。

    段头缺失时不编内容：返回的字典就少那几个键，由 ``validate_tailframe_prompt`` 点名。
    """
    raw = str(text or "")
    found: list[tuple[int, int, str]] = []
    for head in tailframe_sections():
        match = re.search(rf"^{re.escape(head)}\s*$", raw, re.MULTILINE)
        if match:
            found.append((match.start(), match.end(), head))
    found.sort()
    sections: dict[str, str] = {}
    for position, (_, end, head) in enumerate(found):
        stop = found[position + 1][0] if position + 1 < len(found) else len(raw)
        sections[head] = raw[end:stop].strip()
    return sections


def normalize_tailframe_output(text: str) -> str:
    """LLM 产出 → 自包含的三段式文档：按常量重排、重贴段头、丢掉段外杂话。

    与 ``director/prompts.normalize_segment_output`` 同一条纪律：模型只提供正文，段头与
    顺序由代码保证。一段都没解析出来时**原样交回**（不拼假段），让校验器把缺段点名。
    """
    raw = str(text or "").strip()
    sections = split_tailframe_sections(raw)
    if not sections:
        return raw
    blocks = [f"{head}\n{sections[head]}".rstrip()
              for head in tailframe_sections() if head in sections]
    return "\n\n".join(blocks).strip()


def _locked_entries(section: str) -> tuple[str, ...]:
    """锁死清单小节 → 条目元组。只认列表行（``- x`` / ``* x`` / ``• x``）。"""
    entries: list[str] = []
    for line in str(section).splitlines():
        matched = re.match(r"^[-*•]\s*(.+)$", line.strip())
        if matched and matched.group(1).strip():
            entries.append(matched.group(1).strip())
    return tuple(entries)


def validate_tailframe_prompt(text: str, *,
                              model: str = DEFAULT_IMAGE_MODEL) -> list[ValidationIssue]:
    """三段式尾帧提示词的确定性检查（票面 AC1/AC3）。

    两类结论分开定级：

    1. **结构性**（error）：缺段 / 空段 / 清单为空 / 清单条目不是正文原文片段。
       最后一条是 ADR 0016 的不变式——条目是**判据**，转述了就读图校对永远不命中，
       报告照写「干净」（静默失效），所以它必须能触发重写。
    2. **生图纪律**（warning，ADR 0018）：只写看得见的事实、一律正向陈述等，由
       ``check_image_prompt`` 按档现判；违规不阻断产出，用户看得到即可。

    判据方向与 ``asset_proofread._locked_hit`` 同一口径：条目与正文**字面互为子串**即算
    命中（同一谓词，不写第二套）；正文取变换指令与保留项声明两节，**不含清单自己**——
    拿整篇去比，条目当然在自己里，判据恒真。
    """
    body = str(text or "").strip()
    if not body:
        return [ValidationIssue(
            "error", "TAILFRAME_EMPTY",
            "尾帧变换提示词为空：用户拿不到任何可执行的图生图指令")]

    sections = split_tailframe_sections(body)
    issues: list[ValidationIssue] = []

    missing = [head for head in tailframe_sections() if head not in sections]
    if missing:
        issues.append(ValidationIssue(
            "error", "TAILFRAME_SECTION_MISSING",
            f"缺三段式段头：{'、'.join(missing)}"
            f"（应为 {' → '.join(tailframe_sections())}，缺一段这份文档就不完整）"))

    empty = [head for head in tailframe_sections()
             if head in sections and not sections[head].strip()]
    if empty:
        issues.append(ValidationIssue(
            "error", "TAILFRAME_SECTION_EMPTY",
            f"三段式小节为空：{'、'.join(empty)}——段头在、内容没写"))

    locked = _locked_entries(sections.get(LOCKED_HEAD, ""))
    if LOCKED_HEAD in sections and not locked:
        issues.append(ValidationIssue(
            "error", "TAILFRAME_LOCKED_EMPTY",
            "锁死清单没有条目：清单是之后核对终点画面有没有跑偏的唯一判据，"
            "空清单等于放弃这道核对"))
    source = "\n".join(
        sections.get(head, "") for head in (TRANSFORM_HEAD, PRESERVE_HEAD)).strip()
    # 正文两节都空时不再报「条目不是原文片段」：缺段／空段那两条已经点名，
    # 同一个毛病报三次只会把重写请求刷成噪声（用户要的是该改哪一处）。
    if source:
        paraphrased = [entry for entry in locked if not _locked_hit(entry, (source,))]
        if paraphrased:
            issues.append(ValidationIssue(
                "error", "TAILFRAME_LOCKED_NOT_VERBATIM",
                f"锁死清单条目不是正文原文片段：{'、'.join(paraphrased)}"
                "——条目要逐字摘自变换指令或保留项声明，转述会让判据永远不命中"
                "（ADR 0016）"))

    for warning in check_image_prompt(body, model=model):
        issues.append(ValidationIssue("warning", warning.code, warning.message))
    return issues


# ---------------------------------------------------------------------------
# 写与落盘（直调唯一出口 + 有界重写）
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class WrittenTailframe:
    """一段尾帧文档的落盘结果：路径、正文、三段内容与**定稿时**的检查结论。"""

    index: int
    type: str
    path: Path
    text: str
    transform: str
    preserved: str
    locked: tuple[str, ...]
    issues: tuple[ValidationIssue, ...]

    @property
    def errors(self) -> tuple[ValidationIssue, ...]:
        return tuple(issue for issue in self.issues if issue.severity == "error")


def write_tailframe_prompt(segment: DirectorSegment, *, llm,
                           start_image: str | Path,
                           start_frame_description: str,
                           model: str = DEFAULT_IMAGE_MODEL) -> str | None:
    """写一段尾帧文档（直调看门狗 + 有界重写）。调用失败返回 ``None``（调用方据此大声报错）。

    契约与 ``write_director_segment`` 同形：内容不合格带着问题清单重写到
    ``MAX_SEGMENT_ATTEMPTS`` 次，仍不合格**交回原文**——告警不拦产出，但问题必须当着
    用户的面摆出来，不返回 ``None`` 装死（那会把「模型写歪了」与「网关卡住了」混成一件）。
    """
    base_request = build_tailframe_request(
        segment, start_image=start_image,
        start_frame_description=start_frame_description, model=model)
    request = base_request
    text: str | None = None
    for attempt in range(MAX_SEGMENT_ATTEMPTS):
        raw = _direct_write(
            llm, request, role=DIRECTOR_TAILFRAME_WRITER_ROLE,
            label=f"第 {segment.index} 段尾帧变换提示词")
        if raw is None:
            return None
        text = normalize_tailframe_output(raw)
        errors = [issue for issue in validate_tailframe_prompt(text, model=model)
                  if issue.severity == "error"]
        if not errors:
            return text
        if attempt + 1 == MAX_SEGMENT_ATTEMPTS:
            break  # 重试预算用尽：交回原文，由出站把问题当面打给用户
        request = (
            f"{base_request}\n\n"
            "【上一次产出被校验器判为不合格，请按下列问题修正后重写】\n"
            + "\n".join(f"- {issue.code}: {issue.message}" for issue in errors)
        )
    return text


def write_tailframe_prompts(plan: DirectorPlan, *, llm, session_dir: str | Path,
                            start_image: str | Path,
                            model: str = DEFAULT_IMAGE_MODEL) -> list[WrittenTailframe]:
    """逐段产出 ``GENxxx/director/tailframe-NN.md``（只给触发的段写）。

    ``start_image`` 是**一张**起点画面（本票口径：演示用固定帧图，不接 #105 的缓存读取口）。
    同一张图只读一次——读图是真实计费调用，逐段重读同一张图只是在烧配额；生产里每段
    起点画面不同，缓存自然各读一次。

    出错就**停下并点名段号**：读图失败、调用失败都不写半份文档（半份尾帧比没有更糟——
    用户会拿它去生成终点画面）。
    """
    image = Path(start_image)
    if not image.is_file():
        raise ValueError(
            f"起点画面不存在：{image}——尾帧变换要以该段第一帧为参考图，"
            "没有它写出来的就是文生图提示词")

    descriptions: dict[Path, str] = {}
    written: list[WrittenTailframe] = []
    for segment in plan.segments:
        if not needs_tailframe(segment.type):
            continue
        if image not in descriptions:
            try:
                descriptions[image] = describe_image(
                    image, instruction=_START_READING_INSTRUCTION)
            except Exception as exc:  # noqa: BLE001 - 病因原样交回，段号在这里补
                raise RuntimeError(
                    f"第 {segment.index} 段起点画面读图失败：{exc}") from exc
        text = write_tailframe_prompt(
            segment, llm=llm, start_image=image,
            start_frame_description=descriptions[image], model=model)
        if text is None:
            raise RuntimeError(
                f"第 {segment.index} 段尾帧变换提示词调用失败（已落盘 {len(written)} 段）："
                "失败原因见上一行警告；已落盘的段可续跑复用")
        path = tailframe_path(session_dir, segment.index)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text.rstrip() + "\n", encoding="utf-8")

        sections = split_tailframe_sections(text)
        issues = tuple(validate_tailframe_prompt(text, model=model))
        for issue in issues:
            # 告警要在**产出那一刻**打在终端上：这份产物不进后续自动流程，
            # 只在 #105 的陪跑出站里被列出——那时用户已经在忙着生成图了。
            print(f"[警告] 第 {segment.index} 段尾帧变换提示词 "
                  f"{issue.severity.upper()} {issue.code}: {issue.message}", flush=True)
        written.append(WrittenTailframe(
            index=segment.index,
            type=segment.type,
            path=path,
            text=text,
            transform=sections.get(TRANSFORM_HEAD, ""),
            preserved=sections.get(PRESERVE_HEAD, ""),
            locked=_locked_entries(sections.get(LOCKED_HEAD, "")),
            issues=issues,
        ))
    return written

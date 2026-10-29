"""导演台逐段提示词（issue #103）：七模式里一期四种的方言、绝对时间轴、确定性校验。

**为什么按模式分派而不是一份通用口径**：导演台的七个生成模式对提示词的要求不同
（`docs/research/2026-10-29-director-插件原生格式.md` §7）。插件侧 `TASK_PROMPT_SPECS`
的 system_prompt 全是空串——它**不校验、只会在执行期改写**（补 `<Picture N>`、删无效
`<Audio N>`，`plan.py:554-624`）。所以方言只能由项目侧保证，且**每种模式一份**：
- `r2v` 保留官方 ref 六节外壳（唯一含音频段的模式），`detailed_description` 改时序写法、
  去掉 `start_state`（导演台没有桥接帧概念）、主体用 `<Picture N>`；
- `t2v` / `i2v` / `fl2v` 是单段正文（**无 section 头**），音频描述并入正文——插件里
  这三种模式看到的就是一段自由文本；
- `v2v` / `rv2v` / `mixed` 是二期（#109 / #110），一期**明确报错**，不许被当成四种之一
  静默跑一遍。

**时间轴按帧重算成成片坐标**：段表以帧为真源（#102），提示词里「第几秒」一律从
`start_frame / 24` 现算，不沿用「相对本段起点」的旧口径（那是 base/ref 的口径，
`CONTEXT.md`「细粒度时间戳」）。注记形态只住 ``_DIRECTOR_TIMELINE_NOTE`` 一处，
守卫从它派生——重抄一份字面就会在常量一改时静默失效（「永不为红＝没有」）。

**不碰 base/ref 的任何产物形态**：本模块只被导演台流程调用；六节外壳的拼装复用
``segment_prompts.assemble_ref_segment``（纯字符串拼装，没有多参的段序/时间轴判据），
校验则**另写**——``validate_ref_segment`` 那套按多参段序与相对时间轴判，套到导演台
上会把每一段都判红（设计 §3.3）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .. import film_language
# 跨模块取那两个下划线常量与直调出口，是**刻意的复用**而不是抄近路：
# - ``_REF_START/END_STATE_PREFIX`` 是「起止状态句」的唯一可写来源。导演台**禁止**这两句，
#   但「怎么认出来」必须跟着那边走——重抄一份字面，ref 的措辞一改，这里的禁令就再也咬不住
#   （「永不为红＝没有」，本仓吃过多次）。
# - ``_direct_write`` 是写段直调的唯一出口（看门狗 + 失败当场打印，ADR 0019/issue #70）；
#   在导演台另写一遍就是把重试与失败措辞分成两套会各自漂的口径。
from ..segment_prompts import (
    _REF_END_STATE_PREFIX,
    _REF_START_STATE_PREFIX,
    _direct_write,
    MAX_SEGMENT_ATTEMPTS,
    assemble_ref_segment,
    split_task_type,
)
from ..tools.h3_validator import (
    BASE_SECTIONS,
    REF_SECTIONS,
    ValidationIssue,
    has_align_instruction,
    split_ref_sections,
)
from .frames import FPS, PLAN_MODES, DirectorPlan, DirectorSegment

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"
_INSTRUCTION_NAME = "segment_writer_director.md"

# 一期可写段的模式 = 一期可叙事规划的模式（``frames.PLAN_MODES``）。同一份元组：
# 「能不能规划」与「能不能按它的方言写段」是同一件事，分头写两张表迟早一边漏改。
WRITE_MODES = PLAN_MODES

# 直调路径在事件与成本表里的角色名：**必须**与多参/首尾帧的写段角色分开（ADR 0019）——
# 混在一处就分不清钱花在哪条路线上。
DIRECTOR_SEGMENT_WRITER_ROLE = "role_director_segment_writer"
DIRECTOR_SEGMENT_REWRITER_ROLE = "role_director_segment_rewriter"

# 绝对时间轴注记的唯一模板。占位符全部由**帧**现算（成片坐标，ADR 0025 的两套坐标
# 之「成片坐标」）：秒不是第二份真源，`plan.json` 里的 start_s/end_s 只是派生显示值。
_DIRECTOR_TIMELINE_NOTE = (
    "Segment {segment_number} of {total_segments}, covering "
    "{start_s:.3f}s-{end_s:.3f}s of the full {total_s:.3f}s video."
)

# r2v 六节里 summary 的任务类型前缀（官方 ref-en.txt §3）。导演台 r2v 的任务关系与
# 多参同源，故沿用同一个取值。
_REF_TASK_TYPE = "reference generation"

# 行内时间戳（`At MM:SS.mmm`）：导演台正文里最终是**成片坐标**，由组装器从模型写的
# 相对本段起点的时间戳加上 `start_frame / 24` 得到。格式只认两位分、两位秒、三位毫秒
# ——与官方 base/ref 的时间戳形态同一份，模型不会为导演台另造一种写法。
_AT_TIME_RE = re.compile(r"At\s+(\d{2}):(\d{2})\.(\d{3})")

# 段文件落点：``GENxxx/director/segment-NN.md``（1 起）。与 ``plan.json`` 同目录——
# ``segments/`` 是 base/ref 的地盘，两套产物混在一处会让按目录扫段的续跑读错路线。
SEGMENT_FILENAME = "segment-{position:02d}.md"


def _require_write_mode(mode: str) -> str:
    """一期可写段的模式校验；二期模式**明确报错**（票面硬条）。"""
    if mode in WRITE_MODES:
        return mode
    raise ValueError(
        f"导演台模式 {mode!r} 一期不写段（v2v/rv2v 的编辑型方言见 #109、"
        f"mixed 的逐段分派见 #110）；一期只支持 {'/'.join(WRITE_MODES)}")


def segment_path(session_dir: str | Path, position: int) -> Path:
    """第 ``position`` 段（1 起）的提示词文件路径：``GENxxx/director/segment-NN.md``。"""
    return Path(session_dir) / "director" / SEGMENT_FILENAME.format(position=position)


def load_instruction(mode: str) -> str:
    """取该模式的写作指令小节（一个文件按模式分节，加载口按模式取节）。

    为什么一个文件而不是四个：四份文件里相同的纪律（时间窗、禁状态句、机位速度）
    会各自漂；分节后共同纪律只有一份，改一处四条模式同时生效。缺小节**报错**——
    静默取到空指令等于让模型自由发挥。
    """
    _require_write_mode(mode)
    text = (PROMPTS_DIR / _INSTRUCTION_NAME).read_text(encoding="utf-8")
    sections = _split_instruction_file(text)
    missing = [key for key in ("common", mode) if key not in sections]
    if missing:
        raise ValueError(
            f"导演台写段指令缺小节：{'、'.join(missing)}（{_INSTRUCTION_NAME} 必须按模式分节）")
    return f"{sections['common']}\n\n{sections[mode]}"


def _split_instruction_file(text: str) -> dict[str, str]:
    """按 ``## <键>`` 分节；键取小节标题原文（全等匹配，不做模糊归一）。"""
    parts: dict[str, str] = {}
    current: str | None = None
    buffer: list[str] = []
    for line in str(text).splitlines():
        heading = re.match(r"^##\s+(.+?)\s*$", line)
        if heading:
            if current is not None:
                parts[current] = "\n".join(buffer).strip()
            current = heading.group(1).strip()
            buffer = []
            continue
        if current is not None:
            buffer.append(line)
    if current is not None:
        parts[current] = "\n".join(buffer).strip()
    return parts


def render_timeline_note(plan: DirectorPlan, segment: DirectorSegment) -> str:
    """绝对时间轴注记：段序 + 本段在全片里的起止秒 + 全片长度，秒**从帧现算**。

    与 base/ref 的 ``render_ref_segment_timeline_note`` 不是同一个契约：那边按整秒、
    这段按 24fps 的三位小数（107 帧 → 4.458s）。导演台的段边界就是帧，显示成整秒
    会让用户拿 4 去核 4.458 的段，核不上。
    """
    return _DIRECTOR_TIMELINE_NOTE.format(
        segment_number=segment.index,
        total_segments=len(plan.segments),
        start_s=segment.start_frame / FPS,
        end_s=segment.end_frame / FPS,
        total_s=plan.total_frames / FPS,
    )


def _timeline_note_shape() -> str:
    """``_DIRECTOR_TIMELINE_NOTE`` → 形态正则（占位符换通配），由模板**派生**。

    派生而不是字面重抄：模板一改，重抄的那份就再也匹配不上，拿它做的守卫成了永远
    不响的守卫（ADR 0008 裁定二「只留一处可写来源，其余派生或被钉住」）。

    占位符一律放宽成 ``[^\\n]*`` 而不是多参 ``_timeline_note_shape`` 的 ``[^.\\n]*``：
    导演台的秒是三位小数（``4.458s-10.833s``），按「遇到句点就断」放宽会把合法注记
    判成不匹配——守卫于是永不为红（本次实测）。
    """
    return re.sub(r"\\\{[^}]*\\\}", lambda _match: r"[^\n]*",
                  re.escape(_DIRECTOR_TIMELINE_NOTE))


def _strip_self_written_notes(text: str) -> str:
    """去掉模型自写的注记行：注记以组装器为准，留两份只会挤掉正文。"""
    shape = _timeline_note_shape()
    kept = [line for line in str(text or "").splitlines()
            if not re.match(shape, line.strip())]
    return "\n".join(kept).strip()


def _format_absolute_timecode(total_s: float) -> str:
    """秒 → ``MM:SS.mmm``（成片坐标；30 秒片分恒为 00，仍写两位以对齐官方形态）。"""
    total_ms = int(round(float(total_s) * 1000))
    minutes, remainder = divmod(max(total_ms, 0), 60_000)
    seconds, millis = divmod(remainder, 1000)
    return f"{minutes:02d}:{seconds:02d}.{millis:03d}"


def absolutize_timestamps(text: str, start_frame: int) -> str:
    """正文里的相对本段起点时间戳 → **成片坐标**：``start_frame / 24 + 本段内相对秒``。

    为什么由组装器算、不让模型直接写绝对秒：模型算 107/24 = 4.458 会算错，而错一帧
    在导演台里就是段边界对不上（票面 AC「时间轴注记按帧网格重算，与段表一致」）。
    """
    offset_s = start_frame / FPS

    def _rebase(match: re.Match[str]) -> str:
        relative = (int(match.group(1)) * 60 + int(match.group(2))
                    + int(match.group(3)) / 1000.0)
        return f"At {_format_absolute_timecode(offset_s + relative)}"

    return _AT_TIME_RE.sub(_rebase, str(text or ""))


def _section_headers_in_order(text: str) -> list[str]:
    """正文里出现的 ref 段头，按出现位置排序（只认行首 ``header:``）。"""
    positions: list[tuple[int, str]] = []
    for header in REF_SECTIONS:
        match = re.search(rf"^{re.escape(header)}\s*[:：]", text, re.MULTILINE)
        if match:
            positions.append((match.start(), header))
    positions.sort()
    return [header for _, header in positions]


def _at_timestamps(text: str) -> list[float]:
    return [int(m.group(1)) * 60 + int(m.group(2)) + int(m.group(3)) / 1000.0
            for m in _AT_TIME_RE.finditer(text)]


def validate_director_segment(text: str, mode: str, *, plan: DirectorPlan,
                              segment: DirectorSegment) -> list[ValidationIssue]:
    """导演台段提示词的确定性校验（票面 AC：每种模式都有对应测试）。

    与 ``validate_ref_segment`` 的差别是**判据的来源不同**：多参那套按「段序 + 相对
    时间轴 + 起止状态声明」判，导演台没有桥接帧、时间轴是绝对坐标，照搬会把每一段
    都判红（设计 §3.3 点名「不得复用多参段序/时间轴相关的假错判据」）。这里只判本路线
    的五条契约：

    1. ``DIRECTOR_EMPTY``：空段；
    2. ``DIRECTOR_SECTION_*``：r2v 六节齐全且有序；其余模式**不许**有段头；
    3. ``DIRECTOR_STATE_ANNOTATION``：出现起止状态句（导演台没有桥接帧，这两句是
       base/ref 的产物，写进来就是错口径）；
    4. ``DIRECTOR_ALIGN_LINE``：出现 base 的图片对齐指令行（帧槽属于首尾帧链）；
    5. ``DIRECTOR_TIMELINE_*`` / ``DIRECTOR_TIMESTAMP_OUT_OF_RANGE``：注记与段表一致、
       所有 ``At`` 时间戳落在本段帧窗口内（相对口径漏改的信号）。
    """
    _require_write_mode(mode)
    body = str(text or "").strip()
    if not body:
        return [ValidationIssue("error", "DIRECTOR_EMPTY",
                                "段提示词为空：导演台会把空词回落到全局提示词，段内容就此丢失")]

    issues: list[ValidationIssue] = []
    if mode == "r2v":
        present = _section_headers_in_order(body)
        missing = [header for header in REF_SECTIONS if header not in present]
        if missing:
            issues.append(ValidationIssue(
                "error", "DIRECTOR_SECTION_MISSING",
                f"r2v 段缺六节段头：{', '.join(missing)}"
                "（ref 六节外壳是唯一含音频段的官方格式，缺一节就是错格式）"))
        elif present != list(REF_SECTIONS):
            issues.append(ValidationIssue(
                "error", "DIRECTOR_SECTION_ORDER",
                f"六节顺序错误：应为 {' → '.join(REF_SECTIONS)}，实际 {' → '.join(present)}"))
        if not re.search(r"<Picture\s+\d+>", body):
            issues.append(ValidationIssue(
                "error", "DIRECTOR_PICTURE_MISSING",
                "r2v 段正文没有 <Picture N>：参考主体必须按槽号指称，"
                "否则插件会按槽位顺序自行补前缀，指称与挂图对不上"))
    else:
        headers = list(REF_SECTIONS) + list(BASE_SECTIONS)
        found = [header for header in headers
                 if re.search(rf"^{re.escape(header)}\s*[:：]", body, re.MULTILINE)]
        if found:
            issues.append(ValidationIssue(
                "error", "DIRECTOR_SECTION_PRESENT",
                f"{mode} 是单段正文（音频并入正文），不该出现段头：{', '.join(found)}"
                "（插件对这几种模式只读一段自由文本，段头会被当成正文照读）"))

    state_hits = [prefix for prefix in (_REF_START_STATE_PREFIX, _REF_END_STATE_PREFIX)
                  if prefix in body]
    if state_hits:
        issues.append(ValidationIssue(
            "error", "DIRECTOR_STATE_ANNOTATION",
            f"出现起止状态句：{'、'.join(state_hits)}——导演台没有桥接帧，"
            "段间衔接靠潜空间引导，状态句不属于本路线的输出契约"))

    align_lines = [line for line in body.splitlines() if has_align_instruction(line)]
    if align_lines:
        issues.append(ValidationIssue(
            "error", "DIRECTOR_ALIGN_LINE",
            f"{len(align_lines)} 行图片对齐指令（I2VA/FL2VA/L2VA 锚定行）混入导演台段"
            "——帧槽属于首尾帧链，导演台的给图由包结构承担"))

    expected_note = render_timeline_note(plan, segment)
    if expected_note not in body:
        issues.append(ValidationIssue(
            "error", "DIRECTOR_TIMELINE_MISSING",
            f"时间轴注记与段表不符（应含：{expected_note}）"
            "——注记由组装器按帧注入，秒从 start_frame/24 现算"))

    start_s = segment.start_frame / FPS
    end_s = segment.end_frame / FPS
    # 一个帧周期的容差：时间戳按毫秒写，帧边界落在毫秒之外（4.458 vs 107/24=4.4583…）。
    tolerance = 1.0 / FPS
    outside = [stamp for stamp in _at_timestamps(body)
               if not (start_s - tolerance <= stamp <= end_s + tolerance)]
    if outside:
        issues.append(ValidationIssue(
            "error", "DIRECTOR_TIMESTAMP_OUT_OF_RANGE",
            f"时间戳落在本段帧窗口 {start_s:.3f}s-{end_s:.3f}s 之外："
            f"{', '.join(f'{stamp:.3f}s' for stamp in outside)}"
            "——多半是还按「相对本段起点」写的，组装器应已加过 start_frame/24"))
    return issues


# ---------------------------------------------------------------------------
# 写段：请求 → 直调 → 归一 → 校验 → 有界重写 → 落盘
# ---------------------------------------------------------------------------

# 落笔侧词表切片（issue #32 的 ``segment_writer`` 键，与首尾帧/多参两条写段路径**同一份**）。
# 不为导演台新增白名单键：三条写段路径写的是同一件事（最终 H3 正文），多一个键就多一份
# 会各自漂的词表。取不到会报错（真源小节标题改了就是改了），不静默少送。
_WRITING_WORDLIST = film_language.video_slice("segment_writer")


@dataclass(frozen=True)
class WrittenSegment:
    """一段的落盘结果：路径、最终文本与**定稿时**的校验结论（出站清单上逐段展示）。"""

    index: int
    type: str
    path: Path
    text: str
    issues: tuple[ValidationIssue, ...]

    @property
    def errors(self) -> tuple[ValidationIssue, ...]:
        return tuple(issue for issue in self.issues if issue.severity == "error")


def _require_r2v_sections(sections: dict[str, str] | None) -> dict[str, str]:
    """r2v 的定义块必须有：六节的第 1、3 段内容全部来自它，缺了就是半份空定义。"""
    if not sections:
        raise ValueError(
            "r2v 段需要 subject_defs 定义块（subject_definitions / retention_analysis）："
            "六节外壳的第 1、3 节逐字取它，缺了写出来的就是无参照描述")
    missing = [key for key in ("subject_definitions", "retention_analysis")
               if not str(sections.get(key, "")).strip()]
    if missing:
        raise ValueError(
            f"subject_defs 定义块缺 {'、'.join(missing)}：r2v 六节外壳无法拼装")
    return sections


def _future_events_block(plan: DirectorPlan, segment: DirectorSegment) -> str:
    """时间窗外剧情禁区：其他段的概述逐条列出（与多参 v2 请求同形）。"""
    others = [f"- 段 {item.index}（{item.label}）：{item.summary}"
              for item in plan.segments if item.index != segment.index]
    if not others:
        return ""
    return ("\n【时间窗外剧情禁区】以下事件属于其他段，本段一个字都不能出现：\n"
            + "\n".join(others) + "\n")


def _definitions_block(sections: dict[str, str]) -> str:
    return (
        "\nsubject_definitions 定义块（**原样复制**进你的输出，一字不改）：\n"
        f"{str(sections.get('subject_definitions', '')).strip()}\n"
        "\nretention_analysis 定义块（**原样复制**进你的输出，一字不改）：\n"
        f"{str(sections.get('retention_analysis', '')).strip()}\n"
    )


def build_segment_request(plan: DirectorPlan, segment: DirectorSegment, *,
                          shot_table: str = "",
                          sections: dict[str, str] | None = None) -> str:
    """组装一段的写段请求（入参全部显式，不依赖 state——issue #26 的教训）。

    请求里的时间轴提示句取 ``render_timeline_note`` 与 ``absolutize_timestamps`` 的
    同一份口径：请求里不手抄第二份注记形态，也不让模型自己算绝对秒。
    """
    _require_write_mode(plan.mode)
    note = render_timeline_note(plan, segment)
    start_s = segment.start_frame / FPS
    duration_s = segment.export_frames / FPS
    shots_block = (
        f"分镜表（**只取本段 {start_s:.3f}s 起的 {duration_s:.3f}s 窗口内**的镜头；"
        f"其他镜头一个字都不能写进正文）：\n{shot_table}"
        if str(shot_table).strip() else
        "（缺分镜表原文：按本段剧情概述与落定画面写，不得虚构窗外事件）"
    )
    definitions = (
        _definitions_block(_require_r2v_sections(sections)) if plan.mode == "r2v" else "")
    return (
        f"{load_instruction(plan.mode)}\n\n"
        "---\n"
        f"本段编号：Segment {segment.index}/{len(plan.segments)}\n"
        f"段类型：{segment.label}\n"
        f"本段时长：{duration_s:.3f}s（声明 {segment.declared_frames} 帧 / "
        f"导出 {segment.export_frames} 帧）\n"
        f"本段在成片中的起点（成片坐标，供你理解；**不要写进正文**）：{start_s:.3f}s\n"
        "时间轴注记（组装器注入，你不要写；正文叙事须与它一致）：\n"
        f"{note}\n"
        f"本段剧情概述（中文，仅供你理解剧情，不得写进提示词）：{segment.summary}\n"
        f"本段落定的画面（end_hook，正文必须落到这个状态）：{segment.end_hook}\n"
        f"{_future_events_block(plan, segment)}"
        f"\n{shots_block}\n"
        f"{definitions}"
        "\n## 词表切片（ADR 0007 落笔侧同一份）\n"
        "下面词表**前言**里「edge-stability 句」「分段首行的锚定行」两处指针是首尾帧链的"
        "义务，导演台分段**一概不用**（见上方禁止清单）。词表正文的选词与写法照用。\n\n"
        f"{_WRITING_WORDLIST}\n"
    )


def normalize_segment_output(text: str, *, plan: DirectorPlan,
                             segment: DirectorSegment,
                             sections: dict[str, str] | None = None) -> str:
    """LLM 产出 → 自包含的段提示词：定义块取请求给定、注记按帧注入、时间戳换算成成片坐标。

    r2v 复用 ``assemble_ref_segment``（纯字符串拼装，没有多参的段序/时间轴判据）；
    单段模式直接把注记置顶。模型的服从性不参与这两件事——它们是代码性质，
    同 ``normalize_ref_segment_output`` 的理由。
    """
    _require_write_mode(plan.mode)
    raw = str(text or "").strip()
    note = render_timeline_note(plan, segment)
    if plan.mode == "r2v":
        sections = _require_r2v_sections(sections)
        parts = split_ref_sections(raw)
        if not parts:
            # 段落都没解析出来：原样交回，由校验器报缺段（不拼假内容）。
            return raw
        task_type, summary_body = split_task_type(parts.get("summary", ""))
        detailed = absolutize_timestamps(
            _strip_self_written_notes(parts.get("detailed_description", "")),
            segment.start_frame)
        return assemble_ref_segment(
            subject_definitions=sections["subject_definitions"],
            retention_analysis=sections["retention_analysis"],
            task_type=task_type or _REF_TASK_TYPE,
            summary_body=summary_body,
            detailed_body=detailed,
            overall_soundscape=parts.get("overall_soundscape", ""),
            non_diegetic_music=parts.get("non_diegetic_music", ""),
            annotations=note,
        )
    body = absolutize_timestamps(_strip_self_written_notes(raw), segment.start_frame)
    return f"{note}\n{body}".strip()


def write_director_segment(plan: DirectorPlan, segment: DirectorSegment, *, llm,
                           shot_table: str = "",
                           sections: dict[str, str] | None = None) -> str | None:
    """写一段（直调看门狗 + 有界重写）。调用失败返回 None（调用方据此大声报错）。

    契约与 ``write_ref_segment_v2`` 同形：**调用失败**返回 None；**内容不合格**带着
    问题清单重写 ``MAX_SEGMENT_ATTEMPTS`` 次，仍不合格交回原文由出站清单当面展示
    ——不返回 None 装死（那会把「模型写坏了」和「网关卡住了」混成一件事）。
    """
    base_request = build_segment_request(plan, segment, shot_table=shot_table,
                                         sections=sections)
    request = base_request
    text: str | None = None
    for attempt in range(MAX_SEGMENT_ATTEMPTS):
        raw = _direct_write(
            llm, request, role=DIRECTOR_SEGMENT_WRITER_ROLE,
            label=f"第 {segment.index} 段导演台写段")
        if raw is None:
            return None
        text = normalize_segment_output(raw, plan=plan, segment=segment, sections=sections)
        errors = [issue for issue in validate_director_segment(
            text, plan.mode, plan=plan, segment=segment) if issue.severity == "error"]
        if not errors:
            return text
        if attempt + 1 == MAX_SEGMENT_ATTEMPTS:
            break  # 重试预算用尽：交回原文，由出站清单把问题当面打给用户
        request = (
            f"{base_request}\n\n"
            "【上一次产出被校验器判为不合格，请按下列问题修正后重写】\n"
            + "\n".join(f"- {issue.code}: {issue.message}" for issue in errors)
        )
    return text


def write_director_segments(plan: DirectorPlan, *, llm, session_dir: str | Path,
                            shot_table: str = "",
                            subject_defs: str = "") -> list[WrittenSegment]:
    """逐段写并落盘 ``GENxxx/director/segment-NN.md``（1 起）。

    出错就**停下并点名段号**：调用失败（``RuntimeError``）与模式不可写（``ValueError``）
    都不许写成半段假内容——半份段表比没写更糟，用户会拿它去打包（#104）。
    已落盘的前几段保留：它们是真实产物，重跑会覆盖同名文件。
    """
    _require_write_mode(plan.mode)
    sections = (_require_r2v_sections(split_ref_sections(subject_defs))
                if plan.mode == "r2v" else None)
    written: list[WrittenSegment] = []
    for segment in plan.segments:
        text = write_director_segment(plan, segment, llm=llm, shot_table=shot_table,
                                      sections=sections)
        if text is None:
            raise RuntimeError(
                f"第 {segment.index} 段导演台写段调用失败（已落盘 {len(written)} 段）："
                "失败原因见上一行警告；已落盘的段可续跑复用")
        path = segment_path(session_dir, segment.index)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text.rstrip() + "\n", encoding="utf-8")
        written.append(WrittenSegment(
            index=segment.index,
            type=segment.type,
            path=path,
            text=text,
            issues=tuple(validate_director_segment(
                text, plan.mode, plan=plan, segment=segment)),
        ))
    return written


def render_written_listing(plan: DirectorPlan,
                           written: list[WrittenSegment]) -> str:
    """出站：逐段给段号 / 类型 / 给图清单 / 文件路径，并把校验问题贴在该段下面。

    给图清单与段类型必须并排展示：段类型误判会让该段**静默丢掉首帧**（spec 风险清单），
    用户只有在这一步能核。
    """
    from .planning import render_give_images  # 与段表清单同一份渲染（不再抄一份 token 翻译）

    lines = [f"导演台段提示词（{plan.mode} · {len(written)} 段）："]
    for item in written:
        segment = plan.segments[item.index - 1]
        lines.append(
            f"  段{item.index} {segment.label}  给图：{render_give_images(segment, plan.mode)}"
            f"  → {item.path}")
        for issue in item.errors:
            lines.append(f"     [{issue.severity.upper()}] {issue.code}: {issue.message}")
    return "\n".join(lines)


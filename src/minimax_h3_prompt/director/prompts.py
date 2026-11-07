"""导演台逐段提示词（issue #103）：四种模式的方言、绝对时间轴、确定性校验。

**为什么按模式分派而不是一份通用口径**：导演台的四种生成模式对提示词的要求不同
（`docs/research/2026-10-29-director-插件原生格式.md` §7）。插件侧 `TASK_PROMPT_SPECS`
的 system_prompt 全是空串——它**不校验、只会在执行期改写**（补 `<Picture N>`、删无效
`<Audio N>`，`plan.py:554-624`）。所以方言只能由项目侧保证，且**每种模式一份**：
- `r2v` 保留官方 ref 六节外壳（唯一含音频段的模式），`detailed_description` 改时序写法、
  去掉 `start_state`（导演台没有桥接帧概念）、主体用 `<Picture N>`；
- `t2v` / `i2v` / `fl2v` 是官方**三字段外壳**（`integrated_multimodal_description` →
  `overall_soundscape` → `non_diegetic_music`，字段名逐字、顺序固定）。#130 之前这里是
  「单段自由文本、音频并入正文」——那是按插件视图推出来的口径，与官方 base 格式相反；
  插件对这三种模式读的是一整段文本，三字段本来就在那段文本里（#118 只证到原样透传）。
  已取消的 `v2v` / `rv2v` / `mixed` 不再有方言，#170 起由「模式无效」兜底。

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

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

from .. import film_language
from ..ref_capacity import MAX_PICTURES
from ..text_patterns import AT_TIMESTAMP as _AT_TIME_RE
from .film_end import render_film_end_block
from ..segment_refs import (
    SegmentAssetOverflowError,
    compute_segment_scope,
    rewrite_picture_labels,
    scope_ref_sections,
)
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
    # ``_find_sections`` 是「三字段／六节怎么定位」的唯一实现（#130）。形状闸只借它
    # 与本模块的 ``BASE_SECTIONS`` 判「缺节／乱序」，**不整体调 ``validate_base``**：
    # 后者顺带查切点、说话人、edge-stability，全是导演台不用的东西，整调会造一批假错。
    # 在这里另写一份定位正则，节常量或标题写法一改，两处就会各自漂。
    _find_sections,
    has_align_instruction,
    split_ref_sections,
)
from .frames import FPS, PLAN_MODES, DirectorPlan, DirectorSegment
# 产物目录名只有 ``planning.DIRECTOR_DIRNAME`` 一处声明（复审 H1）：段文件、段表、
# 尾帧与导演包必须落在**同一个** ``GENxxx/director/`` 下，各写一份字面量就是几条
# 会各自漂的路径口径（某一条改了，另一条只会表现为「读不到/缺文件」）。
# ``render_give_images`` 同理（#130）：写段请求里的「本段给图」与段表清单、出站清单
# **同一份**渲染，不在这里另写一份 token 翻译。
# 预算的三个类型也从规划层取（#188）：写段只在**规划期定下的那份预算**里选图，两处
# 各定义一套预算形态就会各推一套口径。
from .planning import (
    DIRECTOR_DIRNAME,
    AssetBudget,
    SegmentBudget,
    load_asset_budget,
    render_give_images,
)

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"
_INSTRUCTION_NAME = "segment_writer_director.md"

# 可写段的模式 = 可叙事规划的模式（``frames.PLAN_MODES``）。同一份元组：
# 「能不能规划」与「能不能按它的方言写段」是同一件事，分头写两张表迟早一边漏改。
WRITE_MODES = PLAN_MODES

# 直调路径在事件与成本表里的角色名：**必须**与多参/首尾帧的写段角色分开（ADR 0019）——
# 混在一处就分不清钱花在哪条路线上。
DIRECTOR_SEGMENT_WRITER_ROLE = "role_director_segment_writer"
# 写段的「改写轮」没有独立角色名：改写与首写走同一个 writer 出口（同一份看门狗、
# 同一套失败措辞）。此前这里备了一个 ``..._REWRITER_ROLE`` 的常量，全仓无人引用
# ——死常量会让人以为存在一条独立的改写记账口径（复审 H2，已删）。
#
# #113 的「陪跑里改词」是**用户按键才发生**的另一类调用（与首写分开记账）：
# 成本表上要能一眼看出哪笔钱花在「看过成片之后的重写」上（同
# ``ref_segments.REF_SEGMENT_REVISER_ROLE`` 的理由，ADR 0019）。
DIRECTOR_SEGMENT_REVISER_ROLE = "role_director_segment_reviser"

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
# 判据本体在 ``text_patterns.AT_TIMESTAMP``（2026-11-02 体检 P0-4 单一来源）。

# 段文件落点：``GENxxx/director/segment-NN.md``（1 起）。与 ``plan.json`` 同目录——
# ``segments/`` 是 base/ref 的地盘，两套产物混在一处会让按目录扫段的续跑读错路线。
SEGMENT_FILENAME = "segment-{position:02d}.md"


def _require_write_mode(mode: str) -> str:
    """可写段的模式校验；已取消的旧值**明确报错**（#170）。"""
    if mode in WRITE_MODES:
        return mode
    raise ValueError(
        f"导演台模式无效：{mode!r}（应为 {'/'.join(WRITE_MODES)} 之一，精确小写）")


def segment_path(session_dir: str | Path, position: int) -> Path:
    """第 ``position`` 段（1 起）的提示词文件路径：``GENxxx/director/segment-NN.md``。

    目录名取 ``planning.DIRECTOR_DIRNAME``（#104 复审 H1）：本模块原先硬写 ``"director"``，
    常量一改就会写到别处而不报错——段文件与段表分家，打包时表现为「缺提示词」。
    """
    return Path(session_dir) / DIRECTOR_DIRNAME / SEGMENT_FILENAME.format(position=position)


# 坏段（内容不合格、有界重写用尽）的扩展名。名字**从 ``segment_path`` 派生**而不是再写
# 一份 ``segment-{position:02d}.rejected.md`` 模板：两处各抄一份，段号位数或目录名一改
# 就会分家——坏段写到别处、正式文件照写，用户失去左右对照的凭据（#185）。
REJECTED_SEGMENT_SUFFIX = ".rejected.md"


def rejected_segment_path(session_dir: str | Path, position: int) -> Path:
    """第 ``position`` 段坏段的落点：与正式段文件**同目录、同段号**，多一段后缀。"""
    path = segment_path(session_dir, position)
    return path.with_name(path.stem + REJECTED_SEGMENT_SUFFIX)


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


def relativize_timestamps(text: str, start_frame: int) -> str:
    """``absolutize_timestamps`` 的逆：成片坐标 → **本段相对坐标**（#113 复审采纳）。

    为什么改词必须先逆一次：盘上的段文件是组装器产物，``At`` 已是成片坐标；把它原样
    贴进改词请求，模型会照抄，组装器再 ``absolutize_timestamps`` 加一次本段起点——时间
    戳翻倍并落到本段窗口之外（2026-10-31 复审实测：107 帧的段里 00:06.958 变成
    00:11.416，校验器判 ``DIRECTOR_TIMESTAMP_OUT_OF_RANGE``，重写预算被白白耗光）。

    负秒夹到 0：早于本段起点的坐标说明这段原文本身有问题，夹到段首至少不会写出一句
    不可能的时间戳；真正的问题由校验器在本轮产出上判。
    """
    offset_s = start_frame / FPS

    def _rebase(match: re.Match[str]) -> str:
        absolute = (int(match.group(1)) * 60 + int(match.group(2))
                    + int(match.group(3)) / 1000.0)
        return f"At {_format_absolute_timecode(max(absolute - offset_s, 0.0))}"

    return _AT_TIME_RE.sub(_rebase, str(text or ""))


def _section_headers_in_order(text: str) -> list[str]:
    """正文里出现的 ref 段头，按出现位置排序（只认行首 ``header:``）。

    复用 ``_find_sections``：段头怎么认只有那一处实现（#130）。在这里另跑一份正则，
    节常量或标题写法一改，两处就会各自漂。
    """
    found = _find_sections(text, list(REF_SECTIONS))
    return [header for header, _ in sorted(found.items(), key=lambda item: item[1][1])]


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
    2. ``DIRECTOR_SECTION_*``：r2v 六节齐全且有序；t2v/i2v/fl2v 三字段齐全且有序
       （#130 起闸是**正向**的：段头必需，缺节／乱序才报错）；
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
        # #130：t2v/i2v/fl2v 恢复官方三字段外壳（`integrated_multimodal_description` →
        # `overall_soundscape` → `non_diegetic_music`）。闸的判据**翻了正向**：从前
        # 「出现段头即报错」，现在段头是必需的格式，缺节／乱序才报错。
        # 判据只用节常量与节查找（都不是本模块的私有实现），不整体调 `validate_base`。
        sections = _find_sections(body, BASE_SECTIONS)
        missing = [header for header in BASE_SECTIONS if header not in sections]
        if missing:
            issues.append(ValidationIssue(
                "error", "DIRECTOR_SECTION_MISSING",
                f"{mode} 段缺三字段段头：{', '.join(missing)}"
                "（官方 base 的三字段外壳：画面描述 → 环境声 → 配乐，缺一节就是错格式）"))
        else:
            order = [header for header, _ in sorted(sections.items(),
                                                    key=lambda item: item[1][1])]
            if order != list(BASE_SECTIONS):
                issues.append(ValidationIssue(
                    "error", "DIRECTOR_SECTION_ORDER",
                    f"三字段顺序错误：应为 {' → '.join(BASE_SECTIONS)}，"
                    f"实际 {' → '.join(order)}"))

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
    # 实际挂上去的参考图张数（子集记录的长度，锚不算）。出站清单的「实用 M」取它，
    # 不在展示层另数一遍正文标签——那会把定义块里的标签也算进去（#188）。
    used_pictures: int | None = None

    @property
    def errors(self) -> tuple[ValidationIssue, ...]:
        return tuple(issue for issue in self.issues if issue.severity == "error")


class DirectorSegmentRejectedError(ValueError):
    """写段**内容不合格**（有界重写用尽）：坏段已存 ``.rejected.md``，正式段文件未写。

    为什么单独一个类型而不是复用 ``RuntimeError``：调用失败（网关/卡住/空回复）是
    「这次没写成」，重跑可能就好；内容不合格是「模型按清单改过仍未改好」，用户要去看
    坏段或改素材。两类处置不同，混成一个类型就只能靠读消息分辨（ADR 0019 的同一条纪律）。
    """

    def __init__(self, index: int, issues: Sequence[ValidationIssue],
                 rejected_path: Path) -> None:
        self.index = index
        self.issues = tuple(issues)
        self.rejected_path = Path(rejected_path)
        detail = "；".join(f"{issue.code}: {issue.message}" for issue in self.issues)
        # 点名段号与问题：用户不该为了定位去翻完整份日志（#185）。
        super().__init__(
            f"第 {index} 段导演台写段内容不合格（有界重写用尽），正式段文件未写："
            f"{detail}；坏段已存 {self.rejected_path}")


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


def _fl2v_start_block(plan: DirectorPlan, segment: DirectorSegment) -> str:
    """「本段起点」事实块（#147 fl2v / #192 r2v）：引导段接上一段落定画面，切镜段硬切新场景。

    为什么必须有这一块：段类型只决定给哪几张图，「第一拍从哪接」既不是图也不是
    ``end_hook``（那是终点）。不写进请求，模型只能按类型名猜——``continue`` 会被写成
    从一张不存在的首帧图开始，``camera_cut`` 会被写成与上一段同景的软过渡。

    r2v 与 fl2v 共用同一份事实，但切镜段措辞必须分叉：r2v 没有帧槽
    （``frames.give_images_for`` 对 r2v 恒给参考素材），照抄 fl2v 的「本段第一帧
    来自本段首帧图」会把模型引向一张不存在的图。函数名保留 fl2v 是因为改名只会
    波及既有用例、不改变行为。
    """
    if plan.mode not in ("fl2v", "r2v"):
        return ""
    if segment.type == "opening":
        return "\n【本段起点】全片开场：本段第一帧就是全片第一帧，没有上一段可承接。\n"
    previous = next(
        (item for item in plan.segments if item.index == segment.index - 1), None)
    if previous is None:
        return ""
    if segment.type == "camera_cut":
        if plan.mode == "r2v":
            return (
                "\n【本段起点】切镜：**硬切**的新场景——**不承接**上一段的画面、"
                "构图与人物位置。\n")
        return (
            "\n【本段起点】切镜：本段第一帧来自**本段首帧图**，是**硬切**的新场景——"
            "**不承接**上一段的画面、构图与人物位置；上一段落定画面"
            f"（{previous.end_hook}）只作剧情先后参考，不要写成同一空间。\n")

    # continue / camera_same / ending：都是引导段，第一拍从上一段终点接着写。
    return (
        "\n【本段起点】上一段落定画面："
        f"{previous.end_hook}\n"
        "（本段第一拍从这里接着写；本段没有首帧图。）\n")


def _film_end_block(plan: DirectorPlan, segment: DirectorSegment, *,
                    film_end: bool, description: str) -> str:
    """全片尾帧的终点约束（#160）：只进**终点落在该图上**的那一段的请求。

    两类段有这张图：多段的 ``ending``；单段 fl2v 的 ``opening``（整片只有这一段，
    用户给的 film-end 就是它的终点，见 ``frames.optional_images_for``）。判据里带
    ``film_end`` 是为了与给图行同源：盘上没这张图时 ``film_end=False``，不该凭
    描述字符串「碰巧」注入。
    """
    if plan.mode != "fl2v" or not str(description).strip():
        return ""
    is_endpoint = segment.type == "ending" or (
        film_end and len(plan.segments) == 1 and segment.type == "opening")
    return render_film_end_block(str(description)) if is_endpoint else ""


def _segment_budget_block(entry: SegmentBudget | None) -> str:
    """写段请求里的预算清单（#188）：``<Picture 原号> = 语义名`` 是可执行的那一份。

    抽象约束（「别超过 9 张」）模型只会当成一句提醒；把本段预算的**原号与语义名**逐行
    写出来，它才有据可依地在第一版就按剧情取舍。清单外的图会被退回重写，不是截断。
    """
    if entry is None:
        return ""
    if not entry.pictures:
        # 规划师没挑图（#186 的「空预算」）：写段照常按正文需要挑，仍受每段 9 张上限。
        # 这条"不约束"是刻意的——空预算在这里若当"零张"用，写段永远满足不了
        # （``compute_segment_scope`` 认不出素材时会保留全部登记图）。
        return (
            "\n【本段参考图预算清单】规划师没有为本段挑图（预算为空）："
            f"按正文实际需要引用定义块里的素材，仍受每段 {MAX_PICTURES} 张上限。\n")
    rows = "\n".join(f"<Picture {item.number}> = {item.name}"
                     for item in entry.pictures)
    return (
        "\n【本段参考图预算清单】本段预算清单就是上限：最多挂 "
        f"{entry.slot_count} 张，且只能从下列清单里挑（清单外的图一律不许挂）：\n"
        f"{rows}\n"
        "挂了清单外的图会被退回重写；少用几张不算错——预算只是上限，不是承诺。\n")


def _budget_violation(segment: DirectorSegment, record: Sequence[dict] | None,
                      entry: SegmentBudget | None) -> ValidationIssue | None:
    """本段实际子集 vs 预算：多出来的图做成一条校验错误（点名段号与图）。

    空预算**不**触发（见 ``_segment_budget_block`` 的理由）；``entry is None`` 是旧会话
    没有预算文件，同样不校验（打包期由 #189 出声）。少于预算是合法的：预算本是上限。
    """
    if entry is None or not entry.pictures or record is None:
        return None
    permitted = {item.number for item in entry.pictures}
    extra = [row for row in record if int(row["original"]) not in permitted]
    if not extra:
        return None
    listed = "、".join(f"<Picture {row['original']}> {row['name']}" for row in extra)
    allowed = "、".join(f"<Picture {item.number}> {item.name}" for item in entry.pictures)
    return ValidationIssue(
        "error", "DIRECTOR_BUDGET_VIOLATION",
        f"第 {segment.index} 段挂了本段预算之外的参考图：{listed}。"
        f"本段预算清单就是上限（{allowed}）；请删掉预算外的引用，"
        "或把它们改写成不含 <Picture N> 的纯文字描述。")


def build_segment_request(plan: DirectorPlan, segment: DirectorSegment, *,
                          shot_table: str = "",
                          sections: dict[str, str] | None = None,
                          film_end: bool = False,
                          film_end_description: str = "",
                          budget: SegmentBudget | None = None) -> str:
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
    # 参考图预算是**事前的**，不是事后截断：插件按「每次请求」限 9 张（
    # ``lib/ref_images.py`` 的 MAX_REFERENCE_IMAGES），而本段最终只会挂正文真正点名
    # 的那几张。先写清上限，模型才在第一版就按剧情取舍，而不是写满 13 张再被退回。
    budget_note = (
        f"\n【本段参考图预算】最多引用 {MAX_PICTURES} 张不同的 <Picture N>："
        "插件每次生成（＝每段）只接受这么多个参考图槽位。请按本段剧情挑选**真正出场**"
        f"的素材，超过 {MAX_PICTURES} 张会被退回重写（不会替你删图——删掉的图在正文里"
        "没有接线可落）。\n"
        if plan.mode == "r2v" else "")
    # 本段的具体预算清单（#188）：上限之外还要说清**哪几张**能挂，否则规划期挑的图
    # 与写段实际挂的图会各推一套（#180 事故的第 10 张就是这么来的）。
    budget_list = _segment_budget_block(budget) if plan.mode == "r2v" else ""
    # 本段输出外壳（#130）：r2v 的六节外壳已在指令里写死，这里只服务三条单段模式。
    # 为什么要在**请求里**再写一遍：指令是通用纪律，请求是这一次的执行前提；三字段是
    # 逐字要照抄的格式，写在离产出最近的地方，模型才不会按自己习惯改字段名。
    shell = (
        "\n【本段输出外壳】\n"
        "本段正文必须是三个字段，字段名逐字、顺序固定、节间空一行：\n"
        "`integrated_multimodal_description:` → `overall_soundscape:` → `non_diegetic_music:`。\n"
        "全英文；`<d>` 对白、歌词与画面可见文字保留原语言。不要输出这三节之外的任何段落。\n"
        # L2-02（#144 A）：声场与正文的分工写在**离产出最近**的地方。只写在角色提示词里
        # 时，模型仍会把对白与唱歌抄进 soundscape——两处写的是同一件事，缺一处就会漂。
        "`overall_soundscape:` 只写环境声、动作声与非语言人声，"
        "不重复对白、唱歌与角色能听到的音乐（它们只写在正文里）。\n"
        if plan.mode != "r2v" else "")
    # 本段给图（#130）：改动前请求里**没有任何**「本段有没有首尾帧图」的信息，而
    # 「有尾帧才写终点构图句」等规则全依赖它。这条事实段表里现成、也有中文渲染，
    # 直接调 ``render_give_images``——与段表清单、出站清单同一份，不另写 token 翻译。
    # ``film_end`` 只对**单段** fl2v 的开场有效：全片尾帧在单段片子里就是这一段的
    # 终点画面；多段时它属于最后一段（见 ``frames.optional_images_for``，#147）。
    give_images = render_give_images(segment, plan.mode,
                                     single_segment=len(plan.segments) == 1,
                                     film_end=film_end)
    give = f"【本段给图】{give_images}\n"
    film_end_block = _film_end_block(
        plan, segment, film_end=film_end, description=film_end_description)
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
        f"{give}"
        f"{_fl2v_start_block(plan, segment)}"
        f"{film_end_block}"
        f"本段剧情概述（中文，仅供你理解剧情，不得写进提示词）：{segment.summary}\n"
        f"本段落定的画面（end_hook，正文必须落到这个状态）：{segment.end_hook}\n"
        f"{shell}"
        f"{_future_events_block(plan, segment)}"
        f"\n{shots_block}\n"
        f"{definitions}"
        f"{budget_note}"
        f"{budget_list}"
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


def _write_validated(plan: DirectorPlan, segment: DirectorSegment, *, llm,
                     base_request: str, sections: dict[str, str] | None,
                     role: str, label: str,
                     registry: Sequence[Sequence] = (),
                     budget: SegmentBudget | None = None
                     ) -> tuple[str, tuple[ValidationIssue, ...], list[dict] | None] | None:
    """首写与改词共用的循环：请求 → 直调 → 归一 → 校验 → 有界重写。

    为什么抽成一处：两条路径的落笔纪律必须逐条相同（同一份请求上下文、同一套
    ``normalize_segment_output`` 时间戳换算、同一个 ``validate_director_segment``、
    同一份 ``MAX_SEGMENT_ATTEMPTS`` 预算与问题清单措辞）。此前两份循环各写一遍，
    改一处（比如重试探针或问题清单标题）另一处会静默漂——「同一语义只许一处口径」。

    返回 ``None`` ＝ **调用失败**（网关/卡住/空回复）；否则返回 ``(最后一版文本,
    问题清单, 段内素材子集记录)``。预算用尽后仍不合格时也照常返回文本与清单：改词由
    调用方「不落盘」地报给用户；批量写段据清单里的 error 硬失败，把坏文本存成
    ``.rejected.md``（#185）——处置不同，判定内容却同源。

    ``registry`` 非空时（r2v）每轮合格后还要**裁段内素材子集**；超过每段上限会作为
    一条校验错误回灌给模型重写（2026-11-01 真实现场：第 2 段引用了 10 张、上限 9）。
    「超了就地截断」不是选项——截掉的图在正文里没有接线可落，写出来的段会指着空气。
    给了 ``budget`` 时再核一道「子集 ⊆ 预算」（#188）：预算外的图同样回灌重写。
    """
    request = base_request
    # 初值给空串而不是 None：循环体保证第一次走到 return 前一定赋过值，
    # 用 None 只会让每个 return 站点都要多一次无意义的判空。
    text = ""
    issues: tuple[ValidationIssue, ...] = ()
    for attempt in range(MAX_SEGMENT_ATTEMPTS):
        # 每一轮都从「没有子集记录」开始：``record`` 只由本轮的裁剪产生。放在循环外
        # 初始化时，上一轮裁好子集、这一轮直接写坏的情况会把旧 record 配着新 text
        # 一起返回——调用方照它落 ``assets-in-segment.json``，段词与挂图就不同源了。
        record: list[dict] | None = None
        raw = _direct_write(llm, request, role=role, label=label)
        if raw is None:
            return None
        text = normalize_segment_output(raw, plan=plan, segment=segment, sections=sections)
        issues = tuple(validate_director_segment(
            text, plan.mode, plan=plan, segment=segment))
        errors = [issue for issue in issues if issue.severity == "error"]
        if not errors and registry and sections is not None:
            try:
                text, record = _scope_segment_text(
                    text, plan=plan, segment=segment,
                    sections=sections, registry=registry)
            except SegmentAssetOverflowError as exc:
                # 只接「本段素材太多」这一类（``compute_segment_scope`` 的设计边界）：
                # 它是**可以被模型自己修好的**——少引用一张、把两张合并成一句描述。
                # 同时并进 ``issues``：预算用尽时调用方要能在出站清单上看到这条，
                # 而不是只有「六节齐全」的一串绿。
                overflow = ValidationIssue(
                    "error", "DIRECTOR_PICTURE_OVERFLOW", str(exc))
                issues = (*issues, overflow)
                errors = [overflow]
            else:
                # 裁完的正文换了图号，再走一遍同一条校验器（标号改了，悬空与否重算）。
                issues = tuple(validate_director_segment(
                    text, plan.mode, plan=plan, segment=segment))
                errors = [issue for issue in issues if issue.severity == "error"]
                if not errors:
                    # 子集 ⊆ 预算才算合格（#188）：超出去的图是规划与写段各推一套的
                    # 证据，回灌给模型自己取舍——用尽后由调用方的硬失败接住。
                    violation = _budget_violation(segment, record, budget)
                    if violation is None:
                        return text, issues, record
                    issues = (*issues, violation)
                    errors = [violation]
        elif not errors:
            return text, issues, record
        if attempt + 1 == MAX_SEGMENT_ATTEMPTS:
            # 重试预算用尽：交回最后一版与问题清单，由调用方决定怎么展示/是否落盘。
            return text, issues, record
        request = (
            f"{base_request}\n\n"
            "【上一次产出被校验器判为不合格，请按下列问题修正后重写】\n"
            + "\n".join(f"- {issue.code}: {issue.message}" for issue in errors)
        )
    return text, issues, record


def write_director_segment(plan: DirectorPlan, segment: DirectorSegment, *, llm,
                           shot_table: str = "",
                           sections: dict[str, str] | None = None,
                           registry: Sequence[Sequence] = (),
                           film_end: bool = False,
                           film_end_description: str = "") -> str | None:
    """写一段（直调看门狗 + 有界重写）。调用失败返回 None（调用方据此大声报错）。

    契约与 ``write_ref_segment_v2`` 同形：**调用失败**返回 None；**内容不合格**带着
    问题清单重写 ``MAX_SEGMENT_ATTEMPTS`` 次，仍不合格交回原文由出站清单当面展示
    ——不返回 None 装死（那会把「模型写坏了」和「网关卡住了」混成一件事）。
    """
    base_request = build_segment_request(plan, segment, shot_table=shot_table,
                                         sections=sections, film_end=film_end,
                                         film_end_description=film_end_description)
    result = _write_validated(
        plan, segment, llm=llm, base_request=base_request, sections=sections,
        role=DIRECTOR_SEGMENT_WRITER_ROLE,
        label=f"第 {segment.index} 段导演台写段", registry=registry)
    return None if result is None else result[0]


# 改词请求的块标题：请求文本、测试里的假模型（按 ``_REWRITE_CURRENT_HEAD`` 切出当前段）
# 与断言三处共用这一份（各写一遍就是又一个会各自漂的字面量，同
# ``ref_segments.REF_SEGMENT_BODY_HEAD`` 的纪律）。
_REWRITE_REQUIREMENT_HEAD = "【用户看过成片后不满意：只改画面正文】"
_REWRITE_CURRENT_HEAD = "【当前段提示词（只改其中的画面正文）】"


@dataclass(frozen=True)
class RewriteOutcome:
    """一轮改词的结果（#113）。

    ``text is None`` ＝ **调用失败**（网关/卡住/空回复）；``errors`` 非空 ＝ **产出不合格**
    （重写预算已用尽）。两类都必须让调用方「原词不动、不落账、留在等待里」，所以不能像
    首写那样把两种失败压成同一个 ``str | None``——首写的失败由出站清单逐段展示，
    改词的失败则要当场说清是哪一类，用户才知道该重按 e 还是先查网关。
    """

    text: str | None
    issues: tuple[ValidationIssue, ...] = ()

    @property
    def errors(self) -> tuple[ValidationIssue, ...]:
        return tuple(issue for issue in self.issues if issue.severity == "error")


def build_rewrite_request(plan: DirectorPlan, segment: DirectorSegment, *,
                          current_text: str, shot_table: str = "",
                          sections: dict[str, str] | None = None,
                          film_end: bool = False,
                          film_end_description: str = "") -> str:
    """一轮改词的请求（#113）：**同一份写段上下文** ＋ 当前段提示词 ＋ 只改正文的要求。

    为什么直接复用 ``build_segment_request`` 而不是另写一份精简请求：改词的落笔纪律与
    首写必须逐条相同（时间窗外禁区、分镜窗口、词表切片、r2v 定义块），少一处就会
    改出「首写合格、改词走样」的两套方言（「同一语义只许一处口径」）。

    时间基那句是本请求最要紧的提示：``current_text`` 是盘上的段文件，里面的 ``At``
    时间戳已是**成片坐标**（组装器加过本段起点）；模型照抄它会被组装器再加一次、
    落出本段窗口。所以下面先把当前段原文**转回本段相对坐标**再交给模型，并在要求里
    写清「00:00.000 = 本段第一帧」与组装器会补上的起点秒——实现与提示两边同一条口径，
    不能只靠模型自觉（2026-10-31 复审实测：只写提示时模型照抄，时间戳翻倍）。
    """
    base = build_segment_request(plan, segment, shot_table=shot_table,
                                 sections=sections, film_end=film_end,
                                 film_end_description=film_end_description)
    start_s = segment.start_frame / FPS
    relative_current = relativize_timestamps(current_text, segment.start_frame)
    return (
        f"{base}\n\n"
        "---\n"
        f"{_REWRITE_REQUIREMENT_HEAD}\n"
        "用户已经在导演台里看过这一段的成片，画面不满意。请按同样的写作纪律重写本段，"
        "让画面更贴合本段剧情概述、落定画面与分镜表。\n\n"
        "纪律：\n"
        "1. **只改画面正文**：不写时间轴注记、不写起止状态句、不写图片对齐指令行"
        "（上文禁止清单照旧生效）——注记由组装器按帧注入。\n"
        "2. 没被点名的问题不要顺手改：提示词是逐轮小步改的，全篇重写会把没提到的细节"
        "一并漂走。\n"
        f"3. 正文时间基**相对本段起点**（00:00.000 = 本段第一帧）；组装器会再加上本段在"
        f"成片里的起点 {start_s:.3f}s——**不要照抄**下面当前段提示词里的绝对时间戳。\n"
        "4. 只输出提示词纯正文：不要解释、不要 markdown 围栏、不要前言或结语。\n\n"
        f"{_REWRITE_CURRENT_HEAD}\n{str(relative_current).strip()}\n"
    )


def rewrite_director_segment(plan: DirectorPlan, segment: DirectorSegment, *, llm,
                             current_text: str, shot_table: str = "",
                             sections=None, film_end: bool = False,
                             film_end_description: str = "") -> RewriteOutcome:
    """按「看过成片、不满意」重写一段（#113）：与 ``write_director_segment`` 同形。

    同形之处就是票面要的「同一份写段指令与同一个校验器」：请求从
    ``build_segment_request`` 的同一份上下文长出来，产出走
    ``normalize_segment_output``（注记代码注入、时间戳合成片坐标）＋
    ``validate_director_segment``（禁起止状态句、禁对齐行、时间戳落本段窗口），
    不合格按 ``MAX_SEGMENT_ATTEMPTS`` 有界重写。

    与首写的差别只有一个返回值形态：首写把「调用失败」与「内容坏」都交给出站清单逐段
    展示；改词发生在等待循环里，必须当场分开报（见 ``RewriteOutcome``），且**任何一类
    都不许覆盖原段提示词**（写坏词比不改更糟：用户会拿它去重跑）。

    ``sections`` 只在 r2v 用到：六节外壳的第 1、3 节逐字取它（``_require_r2v_sections``）。
    调用方从**盘上的段文件**取（``split_ref_sections(current_text)``），不从阶段 1 的
    中文素材取——那份素材已经过 ``refdefs`` 变成英文、又按段裁成了子集；改词若退回
    全局中文块，改出来的段会和同目录其它段用两套定义（2026-11-01 真实现场）。
    """
    parts = _sections_mapping(sections)
    sections = _require_r2v_sections(parts) if plan.mode == "r2v" else None
    base_request = build_rewrite_request(
        plan, segment, current_text=current_text, shot_table=shot_table,
        sections=sections, film_end=film_end,
        film_end_description=film_end_description)
    result = _write_validated(
        plan, segment, llm=llm, base_request=base_request, sections=sections,
        role=DIRECTOR_SEGMENT_REVISER_ROLE,
        label=f"第 {segment.index} 段导演台改词")
    if result is None:
        return RewriteOutcome(text=None)
    text, issues, _record = result
    return RewriteOutcome(text=text, issues=issues)


# 段内素材表（r2v）：``director/assets-in-segment.json``，段号 → 该段挂的图
# （``new`` 是段内槽号，``original`` 是全局登记号，``name`` 是台账语义名）。
# 打包**照它挂图**、不自己再推一遍子集：写段与打包各推一次就是同一契约两处口径，
# 哪边先改都会让「段词里的 <Picture 3>」和「包里的第 3 张」变成两张不同的图。
SEGMENT_ASSETS_FILENAME = "assets-in-segment.json"


def segment_assets_path(session_dir: str | Path) -> Path:
    return Path(session_dir) / DIRECTOR_DIRNAME / SEGMENT_ASSETS_FILENAME


def _sections_mapping(sections) -> dict[str, str] | None:
    """``RefSections`` / 字典 / None → 两节字典（None 原样返回，非 r2v 不用定义块）。"""
    if sections is None:
        return None
    if hasattr(sections, "as_mapping"):
        return sections.as_mapping()
    return dict(sections)


def _scope_segment_text(text: str, *, plan: DirectorPlan, segment: DirectorSegment,
                        sections: dict[str, str], registry: Sequence[Sequence]
                        ) -> tuple[str, list[dict] | None]:
    """段正文 → (按本段素材子集重写后的正文, 子集记录)。

    子集按**已写出的正文**算，不按规划期的梗概猜：正文才是这一段真正点名了谁的证据
    （``compute_segment_scope`` 认 ``<Picture N>`` 标签与语义名两条口）。三段之后的重编号
    全部交给 ``scope_ref_sections`` / ``rewrite_picture_labels``——那是多参路线已经用
    过的同一套改写，导演台不另写一份。

    ``registry`` 为空时**不裁**（返回原文与 None）：只有测试与旧调用会走这一支，
    真实流程必须给登记表，否则超过 9 张会在打包期点名报错（不静默丢图）。
    """
    if not registry:
        return text, None
    parts = split_ref_sections(text)
    if not parts:
        return text, None
    shot_text = "\n".join(filter(None, (
        parts.get("summary", ""), parts.get("detailed_description", ""))))
    scope = compute_segment_scope(
        shot_text=shot_text,
        subject_definitions=str(sections.get("subject_definitions", "")),
        registry=[(int(item[0]), str(item[1]), "") for item in registry],
        refs_per_segment=MAX_PICTURES,
        segment_label=f"段 {segment.index}")
    record = [{"new": item.new_picture, "original": item.original_picture,
               "name": item.name} for item in scope.pictures]
    full = {int(item[0]): int(item[0]) for item in registry}
    if len(scope.pictures) == len(registry) and scope.picture_map == full:
        return text, record  # 全量即为子集：段词一字不改（与改动前行为逐字一致）

    scoped = scope_ref_sections(parts, scope)
    note = render_timeline_note(plan, segment)
    detailed = rewrite_picture_labels(
        parts.get("detailed_description", ""), scope.picture_map)
    if detailed.startswith(note):
        # 组装器已注入的注记：重建时由 assemble_ref_segment 再加一次，先摘掉。
        detailed = detailed[len(note):].lstrip("\n")
    # 摘要与音轨两节也要取**裁剪后**的版本（票 #184）：它们本来就是段内视角的文本，
    # 模型却按整片编号写（真实现场：第 1 段只挂 2 张，摘要却写 <Picture 18>）。
    # 用未裁剪的 ``parts`` 重建，正文换了号、摘要还指着旧号，段内标签于是悬空。
    task_type, summary_body = split_task_type(scoped.get("summary", ""))
    rebuilt = assemble_ref_segment(
        subject_definitions=scoped.get("subject_definitions", ""),
        retention_analysis=scoped.get("retention_analysis", ""),
        task_type=task_type or _REF_TASK_TYPE,
        summary_body=summary_body,
        detailed_body=detailed,
        overall_soundscape=scoped.get("overall_soundscape", ""),
        non_diegetic_music=scoped.get("non_diegetic_music", ""),
        annotations=note,
    )
    return rebuilt, record


def write_segment_assets(session_dir: str | Path,
                         scopes: dict[int, list[dict]]) -> Path | None:
    """段内素材表**合并写**；没有 r2v 段（空表）时**不写文件**（别的模式没有素材子集）。

    为什么先读盘再并（票 #184）：重跑单段时调用方只带本轮那一段的记录，整份覆盖写会把
    其它段的子集一起抹掉，而且不报错——打包期就照空表挂图、或退回旧口径整份挂，
    「修好一段、弄坏九段」。所以盘上已有的段原样保留，只有本轮出现的段号被替换；
    条目仍由同一份 ``json.dumps`` 序列化，其它段逐字节不变。
    """
    if not scopes:
        return None
    path = segment_assets_path(session_dir)
    merged = load_segment_assets(session_dir)
    for index, entries in scopes.items():
        merged[int(index)] = list(entries)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {str(index): entries for index, entries in sorted(merged.items())}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def load_segment_assets(session_dir: str | Path) -> dict[int, list[dict]]:
    """读段内素材表；文件不在＝**旧会话**（全量挂图口径），返回空表。

    空表不是错误：改动前的包就是「每段挂全量」，旧会话续跑必须照旧打得出来。
    """
    path = segment_assets_path(session_dir)
    if not path.is_file():
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    return {int(index): list(entries) for index, entries in raw.items()}


def write_director_segments(plan: DirectorPlan, *, llm, session_dir: str | Path,
                            shot_table: str = "",
                            sections=None,
                            registry: Iterable[Sequence] = (),
                            film_end: bool = False,
                            film_end_description: str = "") -> list[WrittenSegment]:
    """逐段写并落盘 ``GENxxx/director/segment-NN.md``（1 起）。

    出错就**停下并点名段号**：调用失败（``RuntimeError``）与模式不可写（``ValueError``）
    都不许写成半段假内容——半份段表比没写更糟，用户会拿它去打包（#104）。
    已落盘的前几段保留：它们是真实产物，重跑会覆盖同名文件。

    **内容不合格同样硬失败（#185）**：有界重写用尽仍有 error（含
    ``DIRECTOR_PICTURE_OVERFLOW``）时不写正式段文件，坏文本改存同目录同段号的
    ``segment-NN.rejected.md``，抛 ``DirectorSegmentRejectedError``。以前这里照写正式
    文件，用户要等到打包才收到一句不点名的话——「超了也照写」让坏段看起来是成品。
    每次写成功后只清掉**该段自己**那份 ``.rejected.md``，绝不整批清空：别的段的坏段
    是用户排查到一半的证据，跑一次第 1 段就抹掉它们等于毁证。

    ``sections`` 是**全片**定义块（r2v 必填；来自 ``refdefs.build_ref_sections`` 的英文
    两节）。每段写完后按本段正文裁一份子集（≤9 张，插件单请求上限），裁完的子集落
    ``assets-in-segment.json`` 给打包期照挂——段词与挂图同源，不许各推一遍。

    段内素材预算（``asset-budget.json``，#186 落盘）从盘上读：本段实际子集必须是它的
    **子集**（#188）。超出的图作为一条 ``DIRECTOR_BUDGET_VIOLATION`` 进有界重写，用尽
    仍超就走下面的硬失败。少于预算不阻断；没有预算文件的旧会话不校验（#189 出声）。
    """
    _require_write_mode(plan.mode)
    global_sections = _sections_mapping(sections)
    if plan.mode == "r2v":
        global_sections = _require_r2v_sections(global_sections)
    # 预算从**盘上**读：向导在写段之前已由 #186 落盘（``persist_director_plan``）。
    # 文件不在＝旧会话，空表 → 每段 ``for_segment`` 返回 None → 不做预算校验（#189
    # 在打包期出声）；r2v 的「规划师没挑图」是**有**条目的空 ``pictures``，两种情形
    # 不混（见 ``_budget_violation``）。
    budget = (load_asset_budget(Path(session_dir))
              if plan.mode == "r2v" else None)
    registry_rows = tuple(registry)
    written: list[WrittenSegment] = []
    for segment in plan.segments:
        # 子集裁剪在**有界重写循环里**（``_write_validated``）：超过每段 9 张时把
        # 「本段素材太多」当一条校验错误回灌，让模型自己取舍后再写一遍；三次仍超才停。
        # 裁完再校验、再落盘——盘上的段文件与段内素材表永远同一版。
        segment_budget = budget.for_segment(segment.index) if budget is not None else None
        base_request = build_segment_request(
            plan, segment, shot_table=shot_table, sections=global_sections,
            film_end=film_end, film_end_description=film_end_description,
            budget=segment_budget)
        result = _write_validated(
            plan, segment, llm=llm, base_request=base_request,
            sections=global_sections, role=DIRECTOR_SEGMENT_WRITER_ROLE,
            label=f"第 {segment.index} 段导演台写段", registry=registry_rows,
            budget=segment_budget)
        if result is None:
            raise RuntimeError(
                f"第 {segment.index} 段导演台写段调用失败（已落盘 {len(written)} 段）："
                "失败原因见上一行警告；已落盘的段可续跑复用")
        text, issues, record = result
        errors = tuple(issue for issue in issues if issue.severity == "error")
        if errors:
            # 有界重写用尽仍不合格：坏文本**只**存成 ``.rejected.md``，正式段文件一个
            # 字节都不写。继续往下走会让一份注定打不出包的段表看起来像成品（#185）。
            rejected = rejected_segment_path(session_dir, segment.index)
            rejected.parent.mkdir(parents=True, exist_ok=True)
            rejected.write_text(text.rstrip() + "\n", encoding="utf-8")
            raise DirectorSegmentRejectedError(segment.index, errors, rejected)
        path = segment_path(session_dir, segment.index)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text.rstrip() + "\n", encoding="utf-8")
        if record is not None:
            # 每段写完就**合并落盘**（#190 复审）：攒到循环结束再整份写的话，第 K 段
            # 硬失败会把 1..K-1 段已经写出的记录一起丢掉——段文件在、挂图表不在，
            # 打包期照空表挂图，而且不报错。失败段走不到这里，所以失败段不留记录。
            write_segment_assets(session_dir, {segment.index: record})
        # 该段这次合格了：它自己那份坏段证据已无对照价值，清掉。只删这一段——别的段的
        # ``.rejected.md`` 归它们自己，批量 glob 会把用户还没看的排查证据一起抹掉。
        rejected = rejected_segment_path(session_dir, segment.index)
        if rejected.is_file():
            rejected.unlink()
        written.append(WrittenSegment(
            index=segment.index,
            type=segment.type,
            path=path,
            text=text,
            # 用**重写循环交回的那一份**，不在调用处复跑校验器：复跑算不出循环里合成的
            # ``DIRECTOR_PICTURE_OVERFLOW``（它只活在 ``_write_validated`` 里），
            # 出站清单会漏报真正的问题（#185）。
            issues=issues,
            used_pictures=len(record) if record is not None else None,
        ))
    return written


def render_written_listing(plan: DirectorPlan,
                           written: list[WrittenSegment],
                           *, film_end: bool = False,
                           budget: AssetBudget | None = None) -> str:
    """出站：逐段给段号 / 类型 / 给图清单 / 文件路径，并把校验问题贴在该段下面。

    给图清单与段类型必须并排展示：段类型误判会让该段**静默丢掉首帧**（spec 风险清单），
    用户只有在这一步能核。

    ``budget`` 给了就在该段下补一句「本段预算 N、实用 M」（#188）：预算估多了、写段
    用少了不阻断，但用户得看得见差在哪。M 取写段期的子集记录（``used_pictures``），
    没有记录（非 r2v / 无素材登记）时不显示，不另造一份「数正文标签」的口径。
    """
    from .planning import render_give_images  # 与段表清单同一份渲染（不再抄一份 token 翻译）

    lines = [f"导演台段提示词（{plan.mode} · {len(written)} 段）："]
    single_segment = len(plan.segments) == 1
    for item in written:
        segment = plan.segments[item.index - 1]
        images = render_give_images(segment, plan.mode,
                                    single_segment=single_segment, film_end=film_end)
        lines.append(
            f"  段{item.index} {segment.label}  给图：{images}  → {item.path}")
        entry = budget.for_segment(item.index) if budget is not None else None
        if entry is not None and item.used_pictures is not None:
            lines.append(f"    本段预算 {entry.slot_count}、实用 {item.used_pictures}")
        for issue in item.errors:
            lines.append(f"     [{issue.severity.upper()}] {issue.code}: {issue.message}")
    return "\n".join(lines)


"""按镜头拆分并按段重写 H3 视频提示词（长视频分段流水线的提示词侧）。

H3 最终提示词结构（官方 base-en.txt，2026-09-22 迁移）：帧变体对齐指令行 →
integrated_multimodal_description（含 [Shot N] 块，英文）→ overall_soundscape →
non_diegetic_music。soundscape/music 按段重写为英文摘要句（官方 §4.6/§4.7）。

本模块提供两级拆分：
- ``split_shots_from_prompt``：机械拆分，每段 = 前缀 + 单镜头块（时间戳归零）+ 全局声音后缀。
- ``rewrite_segment_prompt``：每段一次轻量 LLM 重写，soundscape/music 为本段重写
  英文摘要句，生成符合官方规范的独立单镜头提示词；失败自动回退机械拆分。

另外提供 ``shots_from_prompt`` 与 ``_shot_blocks``：向导路径下 ShotPlan 为空时，
从提示词的 [Shot N] 块与 At MM:SS.mmm 时间戳反推镜头表（时长 = 下一镜时间戳差值）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .tools.h3_validator import (
    ALIGN_TEMPLATES,
    EDGE_STABILITY_SENTENCE,
    ValidationIssue,
    has_align_instruction,
    only_errors,
    timestamps_seconds,
    validate_base,
)
from .user_revisions import render_revision_block

# 镜头块标记：[Shot N]（允许中括号与数字间任意空白）
_SHOT_BLOCK = re.compile(r"\[Shot\s+(\d+)\]")
# 「快机位」措辞（issue #11：致闪因素之一）。覆盖实测物证形态
# 「a fast low tracking shot」「at fast speed」，兼顾 rapid/whip 等同义。
_FAST_CAMERA_RE = re.compile(
    r"\b(?:a\s+)?(?:fast|rapid|swift|whip)[\w-]*\s+"
    r"(?:low\s+|high\s+|wide\s+|tight\s+|close\s+|handheld\s+|tracking\s+|pan\s+"
    r"|dolly\s+|zoom\s+|push-in\s+|crane\s+|aerial\s+|overhead\s+)*"
    r"\w*(?:shot|tracking|pan|dolly|zoom|camera move|push-in|crane|handheld)\b"
    r"|at fast speed|rapid(?:ly)?\s+(?:tracking|pan|dolly|zoom|camera)|whip pan"
    r"|fast-paced camera",
    re.IGNORECASE,
)
# 镜头块首的绝对时间戳：[Shot 2] At 00:03.500, the camera ...
_TIMESTAMP_AFTER_TAG = re.compile(r"(\[Shot\s+\d+\])\s*At\s+(\d{2}):(\d{2})\.(\d{3}),?\s*", re.IGNORECASE)
# 镜头区之后属于全局声音/配乐段落的标记（用于界定镜头区结尾）
_SUFFIX_MARKERS = ("overall_soundscape:", "non_diegetic_music:")


# 行首裸时间戳（无 [Shot N] 标记的镜头边界），用于"模型漏标镜头号"的兼容降级
_BARE_TIMESTAMP_LINE = re.compile(r"^At\s+(\d{2}):(\d{2})\.(\d{3}),?\s*", re.IGNORECASE)


def _mark_bare_timestamp_shots(prompt: str) -> str:
    """兜底：LLM 漏标 [Shot N] 时，把行首 At MM:SS.mmm 行补上 [Shot N] 再拆分。

    触发条件：只有 1 个真实镜头块（_shot_starts 排除 `(from [Shot N])` 交叉引用），
    但行首存在 ≥1 个裸时间戳；每个裸时间戳行前补 `[Shot N]`，N 递增。
    """
    if len(_shot_starts(prompt)) != 1:
        return prompt  # 0 个（真单段）或多于 1 个（已标好）都不用兜底
    lines = prompt.splitlines(keepends=True)
    first_shot_seen = False
    shot_n = 1
    out: list[str] = []
    changed = False
    for line in lines:
        if _SHOT_BLOCK.search(line):
            first_shot_seen = True
        if (
            first_shot_seen
            and _BARE_TIMESTAMP_LINE.match(line)
            and not _SHOT_BLOCK.search(line)
        ):
            shot_n += 1
            line = f"[Shot {shot_n}] " + line  # 在原行首加镜头标记
            changed = True
        out.append(line)
    return "".join(out) if changed else prompt


@dataclass(frozen=True)
class ShotPrompt:
    """一段可单独执行的镜头提示词。

    - ``text``：提示词正文；
    - ``start_seconds`` / ``duration_seconds``：本段在整条视频里的时间窗，
      供信息卡展示与声音段按窗裁剪；未知为 None。
    """

    shot_number: int
    text: str
    start_seconds: float | None = None
    duration_seconds: float | None = None

    def to_dict(self) -> dict:
        return {
            "shot_number": self.shot_number,
            "text": self.text,
            "start_seconds": self.start_seconds,
            "duration_seconds": self.duration_seconds,
        }


def _shot_starts(prompt: str) -> list[re.Match]:
    """定位真正的镜头块起点，排除 `(from [Shot N])` 之类的交叉引用。

    引用特征：`[Shot N]` 紧跟 `)`（如 `(from [Shot 1]) is fully referenced.`）；
    真实镜头块开头是 `[Shot N] ` + 正文 / `At MM:SS.mmm`。
    """
    starts: list[re.Match] = []
    for match in _SHOT_BLOCK.finditer(prompt):
        rest = prompt[match.end():match.end() + 1]
        if rest == ")":
            continue  # 交叉引用，非镜头块
        starts.append(match)
    return starts


def _end_seconds_for(blocks: list[tuple[int, float, str]], index: int, total_duration: float | None) -> float | None:
    """第 index 段的结束时刻：下一段起点；末段用 total_duration 兜底。"""
    if index + 1 < len(blocks):
        return blocks[index + 1][1]
    return total_duration


def _to_segment_header(header: str) -> str:
    """整片头 → 分段头：把整片锚定行**原位换成**规范的单图开场锚。

    每段只喂一张图，FL2VA/L2VA 的整片锚定行不能留（理由见 ``SEGMENT_ANCHOR_VARIANT``）。
    反过来用「含 Picture 2 就剔」过滤会让整片 FL2VA/L2VA 的锚定行整行消失，段 ≥2
    变成无锚定行——交付闸门随即用 I2VA 措辞报红（issue #9 实测）。
    源提示词不含锚定行时（T2VA / ref 模式）不凭空注入。
    """
    lines = header.splitlines()
    if not any(has_align_instruction(line) for line in lines):
        return header
    replaced = [
        I2VA_ANCHOR_LINE if has_align_instruction(line) else line for line in lines
    ]
    return "\n".join(replaced).rstrip() + "\n\n"


def split_shots_from_prompt(prompt: str, total_duration: float | None = None) -> list[ShotPrompt]:
    """把整条 H3 提示词机械拆成逐镜头提示词。

    - 只有一个（或没有）镜头时原样返回单段（由调用方决定是否回退）。这条路径
      **不**过 ``_to_segment_header``：返回的是整片提示词、锚定行仍是整片形态；
      向导在 ``len(segments) < 2`` 时直接回退为展示整段、不当作执行段交付。
    - 每段 = 全局前缀 + 该镜头块（时间戳归零）+ 全局声音/配乐后缀；
      给出 total_duration 时同时填好每段的时间窗（start/duration）。
    """
    prompt = prompt.strip()
    if not prompt:
        return []
    prompt = _mark_bare_timestamp_shots(prompt)  # 兼容 LLM 漏标 [Shot N] 的裸时间戳镜头
    if total_duration is not None:
        # 总时长同样取整：ComfyUI H3 时长选项只有整数档
        total_duration = float(round(total_duration))
    matches = _shot_starts(prompt)
    if len(matches) < 2:
        return [ShotPrompt(1, prompt)]

    # 镜头区结尾：第一个 [Shot 之后出现的 soundscape/music 标记
    suffix_start = len(prompt)
    for marker in _SUFFIX_MARKERS:
        position = prompt.find(marker, matches[0].start())
        if position != -1:
            suffix_start = min(suffix_start, position)
    header = prompt[: matches[0].start()].rstrip() + "\n\n"
    suffix = prompt[suffix_start:].rstrip() + "\n" if suffix_start < len(prompt) else ""
    body = prompt[matches[0].start():suffix_start]

    blocks = _shot_blocks(prompt)
    inner = _shot_starts(body)
    segments: list[ShotPrompt] = []
    for index, match in enumerate(inner):
        end = inner[index + 1].start() if index + 1 < len(inner) else len(body)
        block = body[match.start():end].strip()
        block = _TIMESTAMP_AFTER_TAG.sub(r"\1 ", block)  # 单镜头视频从 0 秒开始
        segment_header = _to_segment_header(header)
        text = segment_header + block + "\n\n" + suffix

        start_s = blocks[index][1] if index < len(blocks) else None
        end_s = _end_seconds_for(blocks, index, total_duration) if blocks else None
        duration = round(end_s - start_s, 3) if (start_s is not None and end_s is not None) else None
        segments.append(ShotPrompt(
            shot_number=int(match.group(1)), text=text.strip() + "\n",
            start_seconds=start_s, duration_seconds=duration,
        ))
    warn_out_of_range_durations(
        [s.duration_seconds for s in segments if s.duration_seconds is not None]
    )
    return segments


def _shot_blocks(prompt: str) -> list[tuple[int, float, str]]:
    """返回 [(shot_number, start_seconds, block_text)]；[Shot 1] 起点为 0。

    时间戳取整：ComfyUI 的 H3 时长选项只有 4-10 秒整数档，LLM 若生成小数
    时间戳（如 8.50s）会产生小数段；此处统一四舍五入到整数秒兜底。
    """
    matches = _shot_starts(prompt)
    if not matches:
        return []
    suffix_start = len(prompt)
    for marker in _SUFFIX_MARKERS:
        position = prompt.find(marker, matches[0].start())
        if position != -1:
            suffix_start = min(suffix_start, position)
    body = prompt[matches[0].start():suffix_start]
    inner = _shot_starts(body)
    blocks: list[tuple[int, float, str]] = []
    for index, match in enumerate(inner):
        end = inner[index + 1].start() if index + 1 < len(inner) else len(body)
        block = body[match.start():end].strip()
        stamp = _TIMESTAMP_AFTER_TAG.match(block)
        seconds = 0.0
        if stamp:
            minutes, secs, millis = int(stamp.group(2)), int(stamp.group(3)), int(stamp.group(4))
            seconds = float(round(minutes * 60 + secs + millis / 1000.0))
        blocks.append((int(match.group(1)), seconds, _TIMESTAMP_AFTER_TAG.sub(r"\1 ", block).strip()))
    return blocks


# ComfyUI 的 H3 时长选项区间：段时长必须落在此区间（整数秒）
MIN_SEGMENT_SECONDS = 4.0
MAX_SEGMENT_SECONDS = 10.0

def warn_out_of_range_durations(durations: list[float], *, stream_print=print) -> None:
    """对落在 4-10s 区间外的段长打印警告（不阻断流程，由人工调整总时长）。"""
    bad = [d for d in durations if not (MIN_SEGMENT_SECONDS <= d <= MAX_SEGMENT_SECONDS)]
    if bad:
        rendered = "、".join(f"{d:g}s" for d in bad)
        stream_print(
            f"[警告] 段时长 {rendered} 超出 ComfyUI H3 可选区间（4-10s 整数），"
            "请人工调整对应镜头时长或总时长"
        )


def shots_from_prompt(prompt: str, total_duration: float):
    """从提示词的 [Shot N] 块 + At MM:SS.mmm 时间戳反推 Shot 列表。

    时长规则：第 N 镜时长 = 第 N+1 镜起点 − 本镜起点；最后一镜 = total_duration − 起点。
    时间戳与总时长均按整数秒处理（ComfyUI H3 时长选项只有 4-10s 整数档）。
    链式字段（previous_shot_id / start_state_derived_from）按顺序接续，满足 ShotPlan.validate。
    """
    from .project_models import Shot  # 延迟导入，避免循环依赖

    total_duration = float(round(total_duration))
    blocks = _shot_blocks(prompt)
    if not blocks:
        return []
    shots: list[Shot] = []
    for index, (shot_number, start, block) in enumerate(blocks):
        if index + 1 < len(blocks):
            duration = blocks[index + 1][1] - start
        else:
            duration = total_duration - start
        if duration <= 0:
            raise ValueError(
                f"[Shot {shot_number}] 时间戳推断出非正时长（起点 {start:g}s，总长 {total_duration:g}s）；"
                "时间戳按整数秒处理，请检查各镜头时间戳是否严格递增且间隔 ≥1s、不超总时长"
            )
        previous_id = f"SH{shot_number - 1:03d}" if index else ""
        one_line = " ".join(block.split())
        summary = one_line[:160] + ("…" if len(one_line) > 160 else "")
        shots.append(Shot(
            shot_id=f"SH{shot_number:03d}",
            shot_number=shot_number,
            duration_seconds=round(duration, 3),
            start_state=summary,
            action=one_line,
            end_state=summary,
            previous_shot_id=previous_id,
            start_state_derived_from=previous_id,
        ))
    return shots


def is_degenerate_durations(durations: list[float]) -> bool:
    """≥3 个镜头且时长全等 → 判定为"无视情节节拍的均分输出"。"""
    return len(durations) >= 3 and len(set(durations)) == 1


# ---------------------------------------------------------------------------
# 按段重写：soundscape/music 按段重写为英文摘要句，生成合规的独立单镜头提示词
# ---------------------------------------------------------------------------

# 分段的锚定行恒为「单图开场锚」形态（即官方 I2VA 行，逐字符固定，base-en.txt 2.1）。
# 每段只喂一张图——段 1 是用户提交的那张，后续段是上一段剥出的桥接帧——且喂进本段
# first frame 槽（wizard._capture_bridge_frame）。brief.variant 描述的是**整片**
# （阶段 2 提示词 + 用户提交哪些帧），逐段执行时由桥接链取代，故分段不得按
# brief.variant 套 ALIGN_TEMPLATES：FL2VA 行会声明用户不喂的 Picture 2，L2VA 行会
# 把这张开场图声明成对齐 S.SS 的尾帧锚（issue #9 裁定 B，2026-09-23）。
# 取值直接取自 validator 的官方模板：模板侧与校验侧同一来源，结构上不可能各说各话。
SEGMENT_ANCHOR_VARIANT = "I2VA"
I2VA_ANCHOR_LINE = ALIGN_TEMPLATES[SEGMENT_ANCHOR_VARIANT]

# ---------------------------------------------------------------------------
# 用户累积修订进分段请求（issue #26）
# ---------------------------------------------------------------------------

# 「不是只落实最后一条」这句不再写进标题：``render_revision_block`` 的收尾句已经说了
# 同一件事，一个块里说两遍只是噪声。
REVISION_CONTEXT_HEADER = "【用户累积修订（长视频的**每一段**都要落实）】"

# 注入优先序：图 > 修订 > 分镜表。图最高沿用既有教条——图是用户提交的**产物**，
# 分镜表是**计划**，成片必须与图连续；修订压过分镜表，因为分镜表是模型按主题写的计划
# 文本，用户的明确要求就是对它的否决。三者不冲突时都要落实。
# 措辞里「首帧读图结果 > 用户累积修订 > 分镜表」是**判据字符串**：测试按它认这条声明，
# 向导的写段前核对块把本块整块照打（用户看到的与模型看到的是同一块）——改措辞要同时
# 改测试里那几处，grep 它。
REVISION_PRIORITY_NOTE = (
    "【注入优先序】三者冲突时一律按此裁定：首帧读图结果 > 用户累积修订 > 分镜表。"
    "图是用户提交的产物、分镜表只是计划，成片必须与图连续；修订是用户的明确要求，"
    "压过分镜表这类模型生成的计划文本。三者不冲突时都要落实。"
)


def render_revision_context(state: dict) -> str:
    """分段请求共用的「累积用户修订 ＋ 注入优先序」块；没有修订时返回空串。

    为什么做成**一个**函数而不是各拼各的：

    - 分段有两条产出路径（v2 规划式、规划失败回退式），2026-09-22 只改了主路径、回退
      路径的缺陷原样复发过一次（见 ``tests/test_wizard_bridge_anchor.py`` 文件头）；
    - 真源是 ``state["user_revisions"]``（阶段 1 人机循环写入、在持久化白名单里），
      从 state 读意味着**每一段**都拿得到，不必逐段传参。

    分段规划**不依赖 state**（它的入参是显式的），所以那条路径由调用方把本函数的
    返回值经 ``revision_context`` 传进去。

    与 ``user_revisions.render_revision_block`` 的分工：那个只产出**清单正文**，给阶段 1
    的首帧节点用（那里没有「段」这回事）；本函数在它外面加了分段用的标题与优先序声明。
    """
    body = render_revision_block(state.get("user_revisions"))
    if not body:
        return ""
    return f"{REVISION_CONTEXT_HEADER}\n{body}\n{REVISION_PRIORITY_NOTE}"


def _revision_block_for_request(state: dict) -> str:
    """请求模板里用的修订块：没有修订时返回空串，块尾补一个换行。

    补换行这件事**只在这里做**：两条路径的模板都是逐行 f-string 拼接，分头写「有没有
    修订 + 要不要补换行」迟早漏一处——漏了就是把修订块和下一节黏在同一行，而请求文本
    没有校验器看得住这种错。
    """
    block = render_revision_context(state)
    return f"{block}\n" if block else ""


_REWRITE_INSTRUCTION = f"""你是 H3 视频提示词工程师。把整条视频的提示词重写为**只覆盖指定时间窗的一段独立单镜头提示词**。

输入：完整提示词（多镜头）+ 本段时间窗（秒）。
输出格式（严格遵守官方 H3 base-en.txt 规范，只输出提示词本身，不要任何解释）：
- 第一行 I2VA 对齐指令（逐字符）：{I2VA_ANCHOR_LINE}
- 空一行；
- integrated_multimodal_description: 只含本段的一个 [Shot 1] 块，英文；正文以 `[Shot 1] ` 标记紧跟字段名开头
  （官方逐字符格式，如 `integrated_multimodal_description: [Shot 1] Live-action, cinematic, a wide shot frames ...`）
  （沿用原 [Shot N] 的画面/运镜/表演描述，**时间戳归零＝相对本段起点计时，绝不得出现绝对片时**，不得虚构原镜头外的内容）；
  实体外观只写在首次出场的 Shot；不写 GLOBAL_LOCK/BRIDGE_FROM/END_HOOK/时长句；
  **每个 Shot 块必须以官方 edge-stability 原句收尾**（逐字符照抄，不改写、不译文）：
  `{EDGE_STABILITY_SENTENCE}`
  官方要求它的理由就是本流程的桥接帧链——抽出的尾帧要保持轮廓锐利，留给下一段当首帧参考；
  **机位与节拍二选一**：快机位（fast/rapid tracking、whip pan、`at fast speed` 等）与 ≥3 个动作节拍
  （正文 ≥3 个 `At 00:XX.XXX`）不得同段共存。**依据只在 4 步采样档成立**——那档同 seed 单变量对照
  （同图、同节拍数，只差机位措辞）：慢机位 0.78 / 快机位+3 拍 4.865，阈值取在 4.1–4.9；
  8 步档下同一文本实测 0.869，但 **8 步的慢机位对照与独立载荷样本都还没有**，所以「8 步下这条处置
  是否还需要」**未验证**：纪律照旧执行，**不许自行放宽**，要放宽先补那两组样本。
  默认降机位为静态/慢速中景、节拍照写，剧情必须快镜时把本段节拍压到 2 个以内；
- overall_soundscape: **为本段重写**（不是从整条裁切，按段重写）：1-4 句 English 连续段落，
  描述本段时间窗内的环境声与动作声，无时间戳（官方 §4.6）；
- non_diegetic_music: **为本段重写**：1-3 句 English，或无声时只写 N/A，无时间戳（官方 §4.7）。
"""


def rewrite_segment_prompt(
    segment: ShotPrompt,
    full_prompt: str,
    llm,
    *,
    state: dict | None = None,
    is_first: bool = False,
    is_last: bool = False,
    position_index: int | None = None,
) -> str | None:
    """用 LLM 把 segment 重写为本段时间窗内的独立提示词；失败返回 None（调用方回退机械拆分）。

    llm 由调用方注入（测试可传 stub）；应为带 ``invoke(str)`` 接口的对象。

    position_index：本段 0-based 段号，用于从 ``bridge_frame_descriptions`` 按段号取
    桥接帧读图结果（回退路径与 v2 路径同一套锚定）；缺省 None 保持旧行为。

    state / is_first / is_last：首尾帧锚定。分段规划失败时流程会回退到本函数，
    这条路径同样要按「图片实际画面」锚定，否则规划一失败，缺陷就原样复发。
    """
    start = segment.start_seconds or 0.0
    duration = segment.duration_seconds
    # 写段时间基必须是本段 0 起：绝对片时只用于剧情定位，否则 LLM 会把节拍排到片段长度之外
    window = (
        f"0-{duration:g}s（本段时长 {duration:g}s；绝对片时 {start:g}s – {start + duration:g}s "
        "仅作剧情定位，严禁写进提示词的时间戳）"
        if duration is not None
        else f"本段 0 起（时长未知；绝对片时 {start:g}s 起，仅作剧情定位，严禁写进时间戳）"
    )
    anchor = _frame_anchor_note(is_first, is_last, state or {}, segment_index=position_index)
    request = (
        f"{_REWRITE_INSTRUCTION}\n\n本段时间窗：{window}\n"
        f"目标镜头：[Shot {segment.shot_number}]\n"
        f"{anchor}\n"
        f"{_revision_block_for_request(state or {})}"
        f"\n完整提示词：\n{full_prompt}"
    )
    try:
        response = llm.invoke(request)
    except Exception:  # noqa: BLE001 - 任何 LLM 异常都回退机械拆分
        return None
    text = getattr(response, "content", response)
    text = str(text).strip()
    return text or None


# ---------------------------------------------------------------------------
# v2：规划式分段（planner 给整秒边界 + 剧情钩子；每段独立细写）
# ---------------------------------------------------------------------------

_SEGMENT_V2_INSTRUCTION = f"""你是 H3 视频提示词的"分段编剧"。为长视频的其中一个 4-10 秒执行段写**可以直接喂给 MiniMax H3 的完整英文提示词**（严格官方 base-en.txt 格式；对白、歌词、画面可见文字保留原文语言）。

H3 是执行型模型：你写什么它就做什么，含糊等于失控；把**不属于本段时间窗的事件**写进任何字段，H3 就会把它们提前演出来。要求：

## ⚠️ 时间窗纪律（最高优先级）
- 你是给「整片 N 秒中的第 [start-end] 秒」写提示词，但**产出的是这一段自己的独立提示词**
- **时间基（最高优先级）：正文里所有 `At 00:XX.XXX` 一律相对本段起点计时**（`00:00.000`＝本段第一帧＝Picture 1），
  取值必须落在 `[0, 本段时长)` 内。绝对片时严禁出现——若本段从整片第 8 秒开始，正文**不能**写 `At 00:08.200`，
  要写 `At 00:00.200`。喂给 H3 的是本段这一条片段，它的时钟从 0 开始；超出时长的节拍会被挤成一团或凭空乱动
- 你只负责写**本段时间窗发生的事**。分析表/人物设定里出现的、发生在其他时间窗的事件（比如整片第 N1 秒才会破碎的窗户、第 N2 秒的转折动作），**本段一个字都不能出现**
- 违反这条 = 本段作废

## 结构（严格官方 base-en.txt 格式，全部英文）
第一行 I2VA 对齐指令（逐字符）：
`{I2VA_ANCHOR_LINE}`
空一行后：
1. `integrated_multimodal_description:` 本段剧情，**默认只有 [Shot 1] 一个镜头块**（段内切镜是显式例外，仅景别跳变等确有必要时）。
   - 正文**以 `[Shot 1] ` 标记紧跟字段名开头**（官方逐字符格式，如 `integrated_multimodal_description: [Shot 1] Live-action, cinematic, a wide shot frames ...`）——
     首行 `(from [Shot 1])` 的引用靠这个标记成立，漏写即引用悬空；标记后先声明风格与初始构图，描述画面必须与 Picture 1（本段首帧图：段 1 是用户提交的首帧，后续段是桥接帧）一致——图片是唯一事实源，文字只做锚定，不要描述图片里不存在的状态
   - **实体外观只写在它首次出场的 Shot 内**，一次写全；未出场的实体不写外观。
     **只有该实体在本段首帧（Picture 1）里确实不存在时**，才可以用一句否定（如 `No cat is visible in the frame.`）；
     首帧图里已经有的实体**绝不能**否定——那等于让 H3 无视你喂进去的图，本段会当场跑偏
   - 时间戳 `At 00:XX.XXX` 写进句子内，**且相对本段起点**（本段第 2.5 秒 → `At 00:02.500, the glass door is slowly pushed open...`）
   - **最后一个时间戳距段尾必须 ≥1s**——关键出场节拍要留展开空间，不许压在段尾
     （此处的「段尾」＝**本段时长**，不是整片时长）
   - **每个 Shot 块收尾**：先让画面自然落定（不写"静止/定格"），然后以官方 edge-stability 原句收尾——
     它必须是**该块的最后一句**；**多镜段里每个 `[Shot N]` 块各加一次**，不是只在段尾加一次，
     逐字符照抄、不改写不译文（官方 base-en.txt 2.1 硬要求；理由见下）：
     `{EDGE_STABILITY_SENTENCE}`
   - **机位与节拍二选一（实测纪律）**：快机位（fast/rapid tracking、whip pan、`at fast speed` 等）
     与 **≥3 个动作节拍**（正文里 ≥3 个 `At 00:XX.XXX`）**不得同段共存**。**依据只在 4 步采样档
     成立**——那档同 seed 单变量对照（同图、同节拍数，只差机位措辞）：慢机位 0.78 / 快机位+3 拍
     4.865，阈值取在 4.1–4.9；8 步档下同一文本实测 0.869。但 **8 步的慢机位对照与独立载荷样本
     都还没有**，所以「8 步下这条处置是否还需要」**未验证**：纪律照旧执行，**不许自行放宽**，
     要放宽先补那两组样本。默认**降机位**：改静态/慢速中景
     （`a static medium shot with small amplitude at slow speed`），节拍照写；剧情确实必须快镜时，
     把本段节拍压缩到 2 个以内，或把后续节拍拆给相邻段
2. `overall_soundscape:` **为本段重写**英文摘要句（官方 §4.6）：1-4 句 English 连续段落，描述本段的环境声与动作声；**无时间戳**。
3. `non_diegetic_music:` **为本段重写**英文摘要句（官方 §4.7）：1-3 句 English，写观众能听到、角色听不到的配乐（乐器/速度/节奏）；无声时只写 `N/A`；**无时间戳**。

## 禁止（官方 base-en.txt 不存在的自创结构，出现即作废）
- 不写 `GLOBAL_LOCK:` 集中定义区（实体外观只进首次出场的 Shot）
- 不写 `BRIDGE_FROM:` 段首状态字段（段首由 Picture 1 锚定句表达）
- 不写 `END_HOOK:` 段尾状态字段（段尾自然收句）
- 不写 `This is a N-second continuous shot.` 时长句（时长由对齐指令承载）
- 不要漏掉块尾的 edge-stability 原句——它**不是**自创结构：官方 base-en.txt 明令要求
  每个镜头块以此句收尾，理由是抽出的尾帧要留给下一段当首帧参考（正是本流程的桥接帧链）
- 不要把未来段的动作提前写进本段
- 不要输出任何解释/markdown/前言；只输出提示词纯正文
"""


def real_frame_descriptions(state: dict) -> dict[str, str]:
    """从 state 取真实帧图的读图结果 ``{role: description}``（无则空 dict）。

    role 为 ``first`` / ``last``；空白描述视为没有（回退计划锚定）。
    """
    out: dict[str, str] = {}
    for item in state.get("fl2va_frame_descriptions") or []:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role", "")).strip()
        description = str(item.get("description", "")).strip()
        if role and description:
            out[role] = description
    return out


def frame_anchor_context(state: dict) -> str:
    """渲染真实首/尾帧画面描述，供分段规划层（segment_planner）锚定；无读图结果返回 ""。"""
    descriptions = real_frame_descriptions(state)
    labels = {"first": "视频第一帧实际画面", "last": "视频最后一帧实际画面"}
    return "\n".join(
        f"{labels[role]}：{descriptions[role]}" for role in ("first", "last") if role in descriptions
    )


# 双向开场一致性纪律句（issue #12，所有段共用）。
# 正向（issue #6，2026-09-22）：图中已有的事物不得写成不存在/尚未出现；
# 反向（issue #12，GEN004 段 2 坏的正是这一半）：图中没有的事物不得写成已存在/
# 从外部进入——桥接帧是门完整关闭、没有猫，正文却写「门被推开、猫已在店内」。
_FRAME_CONSISTENCY_DISCIPLINE = (
    "本段开场状态必须与这张图逐项一致，双向对齐："
    "**图中已有的事物绝不可写成不存在、尚未出现或「等它登场」**"
    "（例如图中猫已站在店内，就不能写「空店」「无猫」「No cat is visible in the frame」"
    "或让它在本段才推门进来）；"
    "**图中没有的事物也绝不可写成已经存在或正在入场**"
    "（例如图中门是关着的、没有猫，就不能写「门被推开」「猫冲进店内」"
    "或「clerk swings into frame」这类与首帧矛盾的开场）。"
    "分镜表、上文「本段剧情概述」与这张图冲突时，一律以画面为准。"
)


def _bridge_frame_descriptions(state: dict) -> dict[int, str]:
    """从 state 取各段桥接帧的读图结果 ``{0-based 段号: 描述}``。

    元素形如 ``{"segment": <段号>, "description": ...}``，由向导在剥出桥接帧后
    立刻读图写入（``bridge_frame_descriptions``）；缺段/空白描述视为没有（诚实降级）。
    """
    out: dict[int, str] = {}
    for item in state.get("bridge_frame_descriptions") or []:
        if not isinstance(item, dict):
            continue
        try:
            segment = int(item.get("segment"))
        except (TypeError, ValueError):
            continue
        description = str(item.get("description", "")).strip()
        if description:
            out[segment] = description
    return out


def _frame_anchor_note(is_first: bool, is_last: bool, state: dict,
                       segment_index: int | None = None) -> str:
    """本段的首/尾帧锚定说明（两条分段路径共用）。

    第 1 段的首帧 = 用户提交的关键帧图 → 注入读图结果，并明令它压过分镜表。
    中间段的首帧 = 桥接帧（上一段剥出的尾帧）→ 向导剥帧后已读图并写入
    ``bridge_frame_descriptions`` 时注入该段的读图结果（issue #12：写段 LLM
    此前对「本段 0.00s 状态」的全部输入都是动作中段的分镜表/剧情概述，在信息上
    不可能写出与桥接帧一致的开场状态）；没有读图结果时只声明图片是唯一事实源。
    末段的尾帧 = 用户提交的尾帧图 → 正文结尾必须落到该状态。

    双向一致性纪律句（``_FRAME_CONSISTENCY_DISCIPLINE``）对**所有**段发出，
    不只第 1 段——GEN004 段 2 正是在中间段把「图中没有的」写成了「正在入场」。
    """
    descriptions = real_frame_descriptions(state)
    lines: list[str] = []
    # 段 1 的 Picture 1 = 用户提交的那张关键帧图：FL2VA/I2VA 是首帧，仅尾帧模式（L2VA）是尾帧
    anchor_role = "first" if "first" in descriptions else "last"
    bridge = _bridge_frame_descriptions(state)
    if segment_index is not None:
        # 段号是权威事实源：is_first 由它派生，杜绝调用方传出自相矛盾的组合
        is_first = segment_index == 0
    elif is_first:
        segment_index = 0
    if is_first and anchor_role in descriptions:
        slot = "首帧图" if anchor_role == "first" else "尾帧图（仅尾帧模式 L2VA）"
        lines.append(f"Picture 1 是用户提交的{slot}，它的实际画面（读图结果，**唯一事实源**）：\n{descriptions[anchor_role]}")
    elif not is_first and segment_index is not None and segment_index in bridge:
        lines.append(
            f"Picture 1 是上一段剥出的桥接帧（上一段末尾画面），它的实际画面（读图结果，**唯一事实源**）：\n{bridge[segment_index]}"
        )
    elif is_first:
        lines.append("Picture 1 是用户提交的首帧图（图片是唯一事实源）")
    else:
        lines.append("Picture 1 是上一段剥出的桥接帧（上一段末尾画面），图片是唯一事实源")
    lines.append(_FRAME_CONSISTENCY_DISCIPLINE)
    if is_last and "last" in descriptions:
        lines.append(
            f"本段是最后一段，视频结束时必须落到用户提交的尾帧实际画面（读图结果，唯一事实源）：\n{descriptions['last']}"
        )
    return "\n".join(lines)


def _shot_text_map(state: dict) -> dict[int, str]:
    """把 state 里的分镜表按 ``[Shot N]`` 切成 ``{shot 号: 该镜原文}``（无标记则空 dict）。"""
    table = str(state.get("shot_table", "") or "").strip()
    if not table:
        return {}
    return {number: text for number, _start, text in _shot_blocks(table)}


def _segment_shot_texts(plan, state: dict) -> tuple[list[str], bool]:
    """本段各 Shot 的原文。返回 ``(文本列表, 是否精确定位到本段镜头)``。

    从分镜表按 ``[Shot N]`` 抽取本段覆盖的镜头——整条分镜表灌进每一段会把其他时间窗的
    事件一起喂给 H3，破坏「时间窗纪律」。
    同一根因早前已被诊断过并命名为 ``extract_segment_shots``
    （`docs/superpowers/specs/2026-09-17-segment-lock-scoping-design.md` 根因 4：
    「``shot_text_{n}`` 键根本不存在 → 回退整张分镜表」），但那份计划从未实现，
    且其配套结构 GLOBAL_LOCK 已被 2026-09-22 官方格式迁移废弃（同批误删的 edge-stability
    句已于 issue #8 恢复——它是官方硬要求，见 ``EDGE_STABILITY_SENTENCE``）。
    """
    table_map = _shot_text_map(state)
    texts: list[str] = []
    exact = True
    for shot_n in plan.shots_in_segment:
        block = str(table_map.get(int(shot_n), "")).strip()
        if block:
            texts.append(block)
        else:
            exact = False
    return texts, (exact and bool(texts))


def build_segment_v2_request(
    plan,  # SegmentPlan
    all_plans: list,
    state: dict,
) -> str:
    """组装 v2 写段请求（官方英文格式模板）。

    plan：当前段的 SegmentPlan；all_plans：全部规划（用于未来段剧情红线与段落定位）。

    累积修订与帧锚定一样**从 state 读**：state 是本请求的既有入参，每一段都会走这条
    组装，于是修订对每一段都可见。这里原先还收一个 ``brief`` 参数——它从头到尾没有任何
    读者，而修订正是被指望经它传进来的（issue #26 的断链）。死参数已删，不是派新用途。
    """
    shots_text, shots_exact = _segment_shot_texts(plan, state)
    if shots_exact:
        shots_block = "本段 Shot 原始描述（只含本段覆盖的镜头，按时间顺序）：\n" + "\n\n".join(shots_text)
    else:
        shots_block = (
            "本段 Shot 原始描述（⚠️ 未能按 Shot 号定位本段镜头，以下是整条分镜表；"
            "**只准取本段时间窗内的内容**，其他镜头的事件一个字都不能出现）：\n"
            + str(state.get("shot_table", ""))
        )

    # 未来段剧情红线：把后续段的 summary 列出来，明令禁止提前出现
    future_events = [
        f"Segment {p.index + 1} ({p.start_s}-{p.end_s}s): {p.summary}"
        for p in all_plans if p.index > plan.index
    ]
    future_block = (
        "\n【时间窗外剧情禁区】以下事件属于其他视频段，本段绝对不能出现其中任何一个：\n"
        + "\n".join(f"- {s}" for s in future_events)
        if future_events else ""
    )

    return (
        f"{_SEGMENT_V2_INSTRUCTION}\n\n"
        f"--- \n"
        f"本段编号：Segment {plan.index + 1}/{len(all_plans)}\n"
        f"写段时间窗：0-{plan.duration_s}s（本段时长 {plan.duration_s}s；"
        f"绝对片时 {plan.start_s}-{plan.end_s}s 仅作剧情定位，严禁写进提示词的时间戳）\n"
        f"包含 Shot：{plan.shots_in_segment}\n"
        f"本段剧情概述（中文，仅供你理解剧情，不得写进提示词）：{plan.summary}\n"
        f"{_frame_anchor_note(plan.index == 0, plan.index == len(all_plans) - 1, state, segment_index=plan.index)}\n"
        f"{_revision_block_for_request(state)}"
        f"{future_block}\n"
        f"\n{shots_block}"
    )


# 交付前校验：产出不合格时回插 issue 有界重写。2 = 共 2 次 LLM 调用（首次 + 1 次重写）
MAX_SEGMENT_ATTEMPTS = 2


def _check_fast_camera_multi_beat_coexist(text: str, stamps: list[float],
                                          issues: list[ValidationIssue]) -> None:
    """「快机位 + 多拍动作」同段共存 → error（issue #11，钉 seed 2×2 实测）。

    两因素单独出现都不致闪（R1：快机位 + 2 拍 = 0.173 绿；R2：慢机位 + 3 拍 = 0.78 可接受），
    共存才致闪（00016 0.78 → 00017 4.865，同 seed 只差机位措辞，6.2 倍）。
    故两个条件必须同时命中才报。多拍阈值 ≥3 个时间戳：快机位 + ≥3 拍的物证全部
    落在 4.1–4.9（红），快机位 + 2 拍为 0.173（绿）——阈值落在实测分离边界上。

    **上述阈值与「共存才致闪」的结论都是在 4 步采样下测得的（2026-09-23 复测修正）。**
    钉 seed 单变量对照显示**采样总步数才是主因**：同 seed 同文本同 LoRA，
    4 步 2.715 / 8 步 0.869（3.1 倍）；且本检查判 red 的 armA 文本在 8 步下得 0.869，
    与 4 步下的低运动对照 00016（0.78）相当——被判「必闪」的内容在 8 步下并不闪。
    另：闪动与 **LoRA 标称步数是否与采样步数对齐无关**（4step LoRA + 4 步 = 4.420，
    8step LoRA + 4 步 = 4.473，同 seed 同文本，4 步档五发全落在 2.7–5.7）。

    故本规则**判定逻辑保持不变**，但引用它时必须声明采样步数——4 步是其成立的前提。
    ADR 0003 记此裁定。

    无条件运行（不按 duration 门控）：判定只用绝对节拍数，漏传时长不该让检查静默失效
    ——「规则在、接线不在」的同型风险，不留给未来调用方。
    """
    if len(stamps) < 3:
        return
    if not _FAST_CAMERA_RE.search(text):
        return
    issues.append(ValidationIssue(
        "error", "FAST_CAMERA_MULTI_BEAT_COEXIST",
        f"本段同时存在快机位措辞（fast/rapid tracking 等）与 {len(stamps)} 个动作节拍"
        "——「快机位 + 多拍动作」同段共存实测致闪（4 步采样下 flicker_std 4.1–4.9；"
        "单因素均绿）。默认降机位（改静态/慢速中景）；剧情必须快镜时，把动作节拍"
        "压缩到 2 个以内或拆到相邻段。（该阈值量于 4 步采样：同一文本 8 步下实测 "
        "0.869，故本规则以 4 步为前提，改采样配置前先重测）"))


def validate_segment(text: str, duration: float | None = None,
                     start_s: float = 0.0) -> list[ValidationIssue]:
    """按本段时长校验一段分段提示词（返回 issue 列表，error 级需阻塞）。

    分段路径产出的是**本段自己的独立提示词**，它的 target video 就是这一条片段，
    所以时间戳必须落在 ``[0, duration)`` 内；正文缺 ``[Shot 1]`` 标记会让首行
    ``(from [Shot 1])`` 引用悬空（validator 报 NO_SHOT）。

    校验口径与写段模板同源：分段恒为**单图开场锚**，故 variant 恒取
    ``SEGMENT_ANCHOR_VARIANT``（理由见该常量）。跟着 ``brief.variant`` 走会让
    「整片锚定行漏进段」的产出被判合格，用户直接粘进 H3。

    ``start_s`` 补一条 error 级检查**抓不到**的启发式警告：写成绝对片时的正文，只要绝对
    秒没超过片段长度，就与合法写法**完全同形**（``At 00:04.000`` 究竟是「片段第 4 秒」
    还是「整片第 4 秒」，正则无法区分）。故：本段不从 0 起（``start_s > 0``）却**所有**
    时间戳都 ≥ ``start_s`` → 高度可疑，报 warning（不阻塞，避免误伤合法写法）。
    """
    issues = validate_base(text, duration=duration, variant=SEGMENT_ANCHOR_VARIANT)
    stamps = timestamps_seconds(text)
    _check_fast_camera_multi_beat_coexist(text, stamps, issues)
    if start_s > 0 and stamps and min(stamps) >= start_s:
        issues.append(ValidationIssue(
            "warning", "TIMESTAMPS_LOOK_ABSOLUTE",
            f"本段从整片 {start_s:g}s 起，正文所有时间戳都 ≥ {start_s:g}s（最早 {min(stamps):.3f}s）"
            "——疑似写成绝对片时。正文时间戳必须相对本段起点（0.000＝本段第一帧）"))
    return issues


def segment_errors(text: str, duration: float | None = None,
                   start_s: float = 0.0) -> list[ValidationIssue]:
    """只取 error 级 issue（warning 不阻塞交付），供重写循环用。"""
    return only_errors(validate_segment(text, duration, start_s))


def write_segment_v2(
    plan,
    all_plans: list,
    state: dict,
    llm,
) -> str | None:
    """用 LLM 为该段写细颗粒度完整提示词（官方英文格式）。LLM 调用失败返回 None。

    产出先过 ``segment_errors``：有 error 就把 issue 回插请求里有界重写（共
    ``MAX_SEGMENT_ATTEMPTS`` 次 LLM 调用 = 首次 + 1 次重写）。仍不合格时**交回原文**
    而不是 None——返回 None 会被向导当成「生成失败」跳过，用户看不到坏在哪；
    交回原文后由向导把 issue 明细当面打给用户（warning 级不参与重写）。
    """
    duration = float(plan.duration_s)
    start_s = float(plan.start_s)
    base_request = build_segment_v2_request(plan, all_plans, state)
    request = base_request
    text: str | None = None
    for attempt in range(MAX_SEGMENT_ATTEMPTS):
        try:
            response = llm.invoke(request)
        except Exception:  # noqa: BLE001 - LLM 调用异常仍按老契约返回 None
            return None
        text = str(getattr(response, "content", response)).strip()
        if not text:
            return None
        errors = segment_errors(text, duration, start_s)
        if not errors:
            return text
        if attempt + 1 == MAX_SEGMENT_ATTEMPTS:
            break  # 最后一轮：重试预算已用尽，不再构造没人用的请求
        request = (
            f"{base_request}\n\n"
            "【上一次产出被校验器判为不合格，请按下列问题修正后重写】\n"
            + "\n".join(f"- {i.code}: {i.message}" for i in errors)
        )
    return text


def _ctx(**fields: str) -> str:
    """CONTEXT 行内嵌套：key: value，跳过空值。"""
    lines = [f"- {k}: {v}" for k, v in fields.items() if v]
    return "\n".join(lines) if lines else "（空）"

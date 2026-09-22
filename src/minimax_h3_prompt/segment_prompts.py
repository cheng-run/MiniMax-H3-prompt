"""按镜头拆分并按段重写 H3 视频提示词（长视频分段流水线的提示词侧）。

H3 最终提示词结构（base-en.txt 规范）：指令行（时长 + 镜头数）→
integrated_multimodal_description（含 [Shot N] 块）→ overall_soundscape →
non_diegetic_music。soundscape/music 是按整条视频时间轴写的全局段落。

本模块提供两级拆分：
- ``split_shots_from_prompt``：机械拆分，每段 = 前缀 + 单镜头块（时间戳归零）+ 全局声音后缀。
- ``rewrite_segment_prompt``：每段一次轻量 LLM 重写，把 soundscape/music
  裁到本段时间窗，生成符合规范的独立单镜头提示词；失败自动回退机械拆分。

另外提供 ``shots_from_prompt`` 与 ``_shot_blocks``：向导路径下 ShotPlan 为空时，
从提示词的 [Shot N] 块与 At MM:SS.mmm 时间戳反推镜头表（时长 = 下一镜时间戳差值）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# 镜头块标记：[Shot N]（允许中括号与数字间任意空白）
_SHOT_BLOCK = re.compile(r"\[Shot\s+(\d+)\]")
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


def split_shots_from_prompt(prompt: str, total_duration: float | None = None) -> list[ShotPrompt]:
    """把整条 H3 提示词机械拆成逐镜头提示词。

    - 只有一个（或没有）镜头时原样返回单段（由调用方决定是否回退）。
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
        segment_header = header
        if index > 0:
            # `(from [Shot N])` 绑定行指向原整片的 Picture 布局，后续段的头帧由桥接帧替代，剔除避免误导
            kept = [line for line in segment_header.splitlines() if "(from [Shot" not in line]
            segment_header = "\n".join(kept).rstrip() + "\n\n"
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

_REWRITE_INSTRUCTION = f"""你是 H3 视频提示词工程师。把整条视频的提示词重写为**只覆盖指定时间窗的一段独立单镜头提示词**。

输入：完整提示词（多镜头）+ 本段时间窗（秒）。
输出格式（严格遵守官方 H3 base-en.txt 规范，只输出提示词本身，不要任何解释）：
- 第一行 I2VA 对齐指令（逐字符）：For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.
- 空一行；
- integrated_multimodal_description: 只含本段的一个 [Shot 1] 块，英文
  （沿用原 [Shot N] 的画面/运镜/表演描述，时间戳归零，不得虚构原镜头外的内容）；
  实体外观只写在首次出场的 Shot；不写 GLOBAL_LOCK/BRIDGE_FROM/END_HOOK/防波纹咒语/时长句；
- overall_soundscape: **为本段重写**（不是从整条裁切，按段重写）：1-4 句 English 连续段落，
  描述本段时间窗内的环境声与动作声，无时间戳（官方 §4.6）；
- non_diegetic_music: **为本段重写**：1-3 句 English，或无声时只写 N/A，无时间戳（官方 §4.7）。
"""


def rewrite_segment_prompt(
    segment: ShotPrompt,
    full_prompt: str,
    llm,
) -> str | None:
    """用 LLM 把 segment 重写为本段时间窗内的独立提示词；失败返回 None（调用方回退机械拆分）。

    llm 由调用方注入（测试可传 stub）；应为带 ``invoke(str)`` 接口的对象。
    """
    start = segment.start_seconds or 0.0
    duration = segment.duration_seconds
    window = (
        f"{start:g}s – {start + duration:g}s"
        if duration is not None else f"{start:g}s 起（时长未知）"
    )
    request = (
        f"{_REWRITE_INSTRUCTION}\n\n本段时间窗：{window}\n"
        f"目标镜头：[Shot {segment.shot_number}]\n\n完整提示词：\n{full_prompt}"
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

_SEGMENT_V2_INSTRUCTION = """你是 H3 视频提示词的"分段编剧"。为长视频的其中一个 4-10 秒执行段写**可以直接喂给 MiniMax H3 的完整英文提示词**（严格官方 base-en.txt 格式；对白、歌词、画面可见文字保留原文语言）。

H3 是执行型模型：你写什么它就做什么，含糊等于失控；把**不属于本段时间窗的事件**写进任何字段，H3 就会把它们提前演出来。要求：

## ⚠️ 时间窗纪律（最高优先级）
- 你是给「整片 N 秒中的第 [start-end] 秒」写提示词
- 你只负责写**本段时间窗发生的事**。分镜表/人物设定里出现的、发生在其他时间窗的事件（比如整片第 N1 秒才会破碎的窗户、第 N2 秒的转折动作），**本段一个字都不能出现**
- 违反这条 = 本段作废

## 结构（严格官方 base-en.txt 格式，全部英文）
第一行 I2VA 对齐指令（逐字符）：
`For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.`
空一行后：
1. `integrated_multimodal_description:` 本段剧情，**默认只有 [Shot 1] 一个镜头块**（段内切镜是显式例外，仅景别跳变等确有必要时）。
   - 开头先声明风格与初始构图（如 `Live-action, cinematic, a wide shot frames ...`），描述画面必须与 Picture 1（本段首帧参考图/桥接帧）一致——图片是唯一事实源，文字只做锚定，不要描述图片里不存在的状态
   - **实体外观只写在它首次出场的 Shot 内**，一次写全；未出场的实体不写外观，必要时只用一句否定（如 `No cat is visible in the frame.`）
   - 时间戳 `At 00:XX.XXX` 写进句子内（如 `At 00:02.500, the glass door is slowly pushed open...`）
   - **最后一个时间戳距段尾必须 ≥1s**——关键出场节拍要留展开空间，不许压在段尾
   - 段尾状态自然收在最后一个 Shot 的末句（画面停在自然落定的一瞬，不写"静止/定格"）
2. `overall_soundscape:` **为本段重写**英文摘要句（官方 §4.6）：1-4 句 English 连续段落，描述本段的环境声与动作声；**无时间戳**。
3. `non_diegetic_music:` **为本段重写**英文摘要句（官方 §4.7）：1-3 句 English，写观众能听到、角色听不到的配乐（乐器/速度/节奏）；无声时只写 `N/A`；**无时间戳**。

## 禁止（官方 base-en.txt 不存在的自创结构，出现即作废）
- 不写 `GLOBAL_LOCK:` 集中定义区（实体外观只进首次出场的 Shot）
- 不写 `BRIDGE_FROM:` 段首状态字段（段首由 Picture 1 锚定句表达）
- 不写 `END_HOOK:` 段尾状态字段（段尾自然收句）
- 不写 `This is a N-second continuous shot.` 时长句（时长由对齐指令承载）
- 不写防波纹咒语（"保持轮廓…无波纹、扭曲或边缘抖动"之类）
- 不要把未来段的动作提前写进本段
- 不要输出任何解释/markdown/前言；只输出提示词纯正文
"""


def build_segment_v2_request(
    plan,  # SegmentPlan
    all_plans: list,
    state: dict,
    brief,
) -> str:
    """组装 v2 写段请求（官方英文格式模板）。

    plan：当前段的 SegmentPlan；all_plans：全部规划（用于未来段剧情红线与段落定位）。
    """
    # 本段 Shot 块原文：从原 prompt 的该时间段抓（planner 给出 shot 号后从 state 里挑）
    shots_text = []
    for shot_n in plan.shots_in_segment:
        shot_key = f"shot_text_{shot_n}"  # stage 2 每段 prompt 生成时把每个 Shot 单独写进 state
        if shot_key in state:
            shots_text.append(str(state[shot_key]))

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
    # 首段有用户首帧图；后续段首帧 = 上一段桥接帧（图片是唯一事实源）
    anchor_note = (
        "Picture 1 是用户提供的首帧参考图"
        if plan.index == 0
        else "Picture 1 是上一段剥出的桥接帧（上一段末尾画面），图片是唯一事实源"
    )

    return (
        f"{_SEGMENT_V2_INSTRUCTION}\n\n"
        f"--- \n"
        f"本段编号：Segment {plan.index + 1}/{len(all_plans)}\n"
        f"时间窗：{plan.start_s}-{plan.end_s}s（时长 {plan.duration_s}s，对应整段视频的 {plan.start_s}-{plan.end_s}s）\n"
        f"包含 Shot：{plan.shots_in_segment}\n"
        f"本段剧情概述（中文，仅供你理解剧情，不得写进提示词）：{plan.summary}\n"
        f"{anchor_note}\n"
        f"{future_block}\n"
        f"\n本段 Shot 原始描述（如有多条则依次按时间顺序排）：\n" + ("\n\n".join(shots_text) or state.get("shot_table", ""))
    )


def write_segment_v2(
    plan,
    all_plans: list,
    state: dict,
    brief,
    llm,
) -> str | None:
    """用 LLM 为该段写细颗粒度完整提示词（官方英文格式）。失败返回 None。"""
    try:
        response = llm.invoke(build_segment_v2_request(plan, all_plans, state, brief))
    except Exception:  # noqa: BLE001
        return None
    text = getattr(response, "content", response)
    text = str(text).strip()
    return text or None


def _ctx(**fields: str) -> str:
    """CONTEXT 行内嵌套：key: value，跳过空值。"""
    lines = [f"- {k}: {v}" for k, v in fields.items() if v]
    return "\n".join(lines) if lines else "（空）"

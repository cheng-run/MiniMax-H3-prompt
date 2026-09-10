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
    return segments


def _shot_blocks(prompt: str) -> list[tuple[int, float, str]]:
    """返回 [(shot_number, start_seconds, block_text)]；[Shot 1] 起点为 0。"""
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
            seconds = minutes * 60 + secs + millis / 1000.0
        blocks.append((int(match.group(1)), seconds, _TIMESTAMP_AFTER_TAG.sub(r"\1 ", block).strip()))
    return blocks


def shots_from_prompt(prompt: str, total_duration: float):
    """从提示词的 [Shot N] 块 + At MM:SS.mmm 时间戳反推 Shot 列表。

    时长规则：第 N 镜时长 = 第 N+1 镜起点 − 本镜起点；最后一镜 = total_duration − 起点。
    链式字段（previous_shot_id / start_state_derived_from）按顺序接续，满足 ShotPlan.validate。
    """
    from .project_models import Shot  # 延迟导入，避免循环依赖

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
                f"[Shot {shot_number}] 时间戳推断出非正时长（起点 {start:.3f}s，总长 {total_duration:.1f}s）；"
                "请检查提示词中各镜头时间戳是否严格递增且不超总时长"
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
# 按段重写：soundscape/music 裁到本段时间窗，生成合规的独立单镜头提示词
# ---------------------------------------------------------------------------

_REWRITE_INSTRUCTION = """你是 H3 视频提示词工程师。把整条视频的提示词重写为**只覆盖指定时间窗的一段独立单镜头提示词**。

输入：完整提示词（多镜头）+ 本段时间窗（秒）。
输出格式（严格遵守 H3 base 规范，只输出提示词本身，不要任何解释）：
- 第一行指令行：说明本段时长（精确到 0.01 秒）与镜头数（1），英文；
- 空一行；
- integrated_multimodal_description: 只含本段的一个 [Shot 1] 块
  （沿用原 [Shot N] 的画面/运镜/表演描述，时间戳归零，不得虚构原镜头外的内容）；
- overall_soundscape: 只保留本段时间窗内的环境声与动作声（原描述中超出该窗口的内容一律删除）；
- non_diegetic_music: 只保留本段时间窗内的配乐内容与起止（删除其他时间点的渐强/渐弱描述）。
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
        f"{start:.2f}s – {start + duration:.2f}s"
        if duration is not None else f"{start:.2f}s 起（时长未知）"
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

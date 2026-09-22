"""分段规划师（segment planner）：把整条视频拆成可执行的 4-10s 整秒分段。

两步法的第一步：只规划边界与衔接，不写正文。正文由每段独立调
prompt_engineer 产出（见 ``wizard._run_segmented_flow``）。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"
MIN_SEG_S = 4
MAX_SEG_S = 10

# 注入规划请求的「关键帧实际画面」块标题（有读图结果时才出现）
FRAME_CONTEXT_HEADER = "【关键帧实际画面（用户提交的图片读图结果，**唯一事实源**；与分镜表冲突时以画面为准）】"


@dataclass(frozen=True)
class SegmentPlan:
    index: int
    start_s: int
    end_s: int
    shots_in_segment: tuple[int, ...]
    summary: str
    end_hook: str  # 本段结束时画面必须达到的具体状态（下段首帧锚）

    @property
    def duration_s(self) -> int:
        return self.end_s - self.start_s

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "start_s": self.start_s,
            "end_s": self.end_s,
            "shots_in_segment": list(self.shots_in_segment),
            "summary": self.summary,
            "end_hook": self.end_hook,
        }


def _load_instruction() -> str:
    path = PROMPTS_DIR / "segment_planner.md"
    return path.read_text(encoding="utf-8")


def _extract_json(text: str) -> dict | None:
    """从 LLM 输出抓 JSON：允许 ```json 围栏或裸 JSON。"""
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    candidate = m.group(1) if m else text
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        # 尝试截取第一个 { 到最后一个 }
        start, end = candidate.find("{"), candidate.rfind("}")
        if 0 <= start < end:
            try:
                return json.loads(candidate[start:end + 1])
            except json.JSONDecodeError:
                return None
    return None


def parse_segment_plan(text: str, total_s: int) -> list[SegmentPlan] | None:
    """解析并校验 segment planner 输出。失败返回 None。"""
    data = _extract_json(text)
    if not isinstance(data, dict):
        return None
    raw_segments = data.get("segments")
    if not isinstance(raw_segments, list) or not raw_segments:
        return None

    plans: list[SegmentPlan] = []
    for i, item in enumerate(raw_segments):
        try:
            start = int(item["start_s"])
            end = int(item["end_s"])
        except (KeyError, TypeError, ValueError):
            return None
        duration = end - start
        # 硬约束：边界整数（int() 已保证）、时长 4-10 秒、递增连续覆盖
        if duration < MIN_SEG_S or duration > MAX_SEG_S:
            return None
        if i == 0 and start != 0:
            return None
        if plans and plans[-1].end_s != start:
            return None
        shots = item.get("shots_in_segment", [])
        if isinstance(shots, str):
            shots = [shots]  # 容错
        plans.append(SegmentPlan(
            index=i,
            start_s=start,
            end_s=end,
            shots_in_segment=tuple(int(s) for s in shots if isinstance(s, (int, str))),
            summary=str(item.get("summary", "")),
            end_hook=str(item.get("end_hook", "")),
        ))
    # 最后一段 end 可以略超 total（留余量）但不许欠
    if plans[-1].end_s < total_s:
        return None
    return plans


def plan_segments(shot_table: str, total_s: float, llm, *, frame_context: str = "") -> list[SegmentPlan] | None:
    """调 LLM 规划分段；任一失败返回 None（调用方回退机械拆分）。

    shot_table：阶段 1 的「分镜设计」文本（[Shot N] 描述 + 切点）。
    frame_context：真实关键帧图片的读图结果（``segment_prompts.frame_anchor_context``）。
    分镜表只是**计划**，用户可能复用/修改首帧图；两者冲突时以图片为准，否则
    plan 层会把「空店→猫进门」这类已被图片否定的状态固化进每一段。
    """
    total_int = int(round(total_s))
    frame_block = (
        f"\n{FRAME_CONTEXT_HEADER}\n{frame_context}\n"
        if frame_context.strip() else ""
    )
    request = (
        f"{_load_instruction()}\n\n"
        f"视频总时长：{total_int}s\n"
        f"{frame_block}"
        f"\n分镜表：\n{shot_table}"
    )
    try:
        response = llm.invoke(request)
    except Exception:  # noqa: BLE001
        return None
    text = getattr(response, "content", response)
    return parse_segment_plan(str(text), total_int)

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
# 规划验收闸的时长档**分路线**（ADR 0012）：
# - 多参（ref）：4–15 整数秒——59- 工作流时长节点实测契约 min=4/max=15
#   （issue #46；旧「下拉只有 4-10」对该节点已过时）；
# - 首尾帧链（base）：4–10 保守档**硬闸**保持不动——工作流 62 的节点档未复核，
#   解析闸放过去就是让不听话的 LLM 规划出 ComfyUI 可能选不出时长的段（ADR 0012）。
# 注意这不是 preflight 的执行段/交接校验（那两处按 AC6 已扩到 4–15 节点网格），
# 是「规划产物验收」——规划档按路线分，校验档按节点能力走。
MIN_SEG_S = 4
MAX_SEG_S = 15
BASE_MAX_SEG_S = 10

# 注入规划请求的「关键帧实际画面」块标题（有读图结果时才出现）
FRAME_CONTEXT_HEADER = "【关键帧实际画面（用户提交的图片读图结果，**唯一事实源**；与分镜表冲突时以画面为准）】"


@dataclass(frozen=True)
class SegmentPlan:
    index: int
    start_s: int
    end_s: int
    shots_in_segment: tuple[int, ...]
    summary: str
    end_hook: str  # 本段结束时画面必须达到的具体状态（首尾帧链：下段首帧锚）
    # 多参分段（issue #46）：无桥接帧，start_state 与 end_hook 一起作为「起止状态声明」
    # 进每段提示词正文（段间叙事衔接的显式依据）。首尾帧链不用它（默认空）。
    start_state: str = ""

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
            "start_state": self.start_state,
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "SegmentPlan":
        """``to_dict`` 的逆操作，供**续接**读回已落盘的 ``segments/plan.json``。

        为什么需要它：``plan_segments`` 是 LLM 调用，重跑会得到另一套分段边界，
        而 ``progress.json`` 的 ``done=N`` 只对产出它的那套分段有意义。续接必须用
        同一套分段，就得能把 plan.json 读回来——GEN005 当初是手抄一份绕过去的
        （``resume_gen005.py``，issue #17）。
        """
        return cls(
            index=int(raw["index"]),
            start_s=int(raw["start_s"]),
            end_s=int(raw["end_s"]),
            shots_in_segment=tuple(int(s) for s in raw["shots_in_segment"]),
            summary=str(raw["summary"]),
            end_hook=str(raw["end_hook"]),
            # 旧 plan.json 没有这个键：缺省空串（首尾帧链的既有产物）
            start_state=str(raw.get("start_state", "")),
        )


def _normalize_shot_windows(shots, total_s: int) -> list[tuple[int, int]]:
    """校验镜头窗序列：整数、递增、无缝覆盖 [0, total_s]；不合格抛 ValueError。"""
    windows = [(int(s), int(e)) for s, e in shots]
    if not windows:
        raise ValueError("shots 为空：无镜头窗可拆")
    if windows[0][0] != 0 or windows[-1][1] != total_s:
        raise ValueError(f"镜头窗必须覆盖 0–{total_s}，实际 {windows[0][0]}–{windows[-1][1]}")
    for (s0, e0), (s1, e1) in zip(windows, windows[1:]):
        if not s0 < e0:
            raise ValueError(f"镜头窗 {s0}-{e0} 非正时长")
        if e0 != s1:
            raise ValueError(f"镜头窗有缺口/重叠：{s0}-{e0} 之后是 {s1}-{e1}")
    if not windows[-1][0] < windows[-1][1]:
        raise ValueError(f"镜头窗 {windows[-1]} 非正时长")
    return windows


def _split_overlong_blocks(windows: list[tuple[int, int]], min_s: int,
                           max_s: int) -> list[tuple[int, int]]:
    """把超过 max_s 的单镜窗镜内切成 ≤max_s 的整数块（末块吸收余数）。

    镜内切是「边界落在镜头切点」的唯一合法例外——长镜头装不进任何执行窗口
    （CONTEXT.md「Segment」：一个长于执行窗口的 Shot 会被拆成多个 Segment）。
    切块口径复用 project_models._plan_segment_durations（末段吸收余数一处真源）。
    """
    from .project_models import _plan_segment_durations  # noqa: PLC0415 - 避免顶层循环导入

    blocks: list[tuple[int, int]] = []
    for start, end in windows:
        length = end - start
        if length <= max_s:
            blocks.append((start, end))
            continue
        cursor = start
        for duration in _plan_segment_durations(length, min_seconds=min_s, max_seconds=max_s):
            step = int(duration)
            blocks.append((cursor, cursor + step))
            cursor += step
        blocks[-1] = (blocks[-1][0], end)  # 防浮点残差，末块对齐镜尾
    return blocks


def pack_shots_into_segments(shots, total_s: int, *, min_s: int | None = None,
                             max_s: int | None = None) -> list[tuple[int, int]]:
    """把镜头窗「不均匀硬切」成 4–15s 整数执行段：边界尽量落在镜头切点。

    多参分段（issue #46）的确定性底座，规划失败回退也走它。规则：

    - 窗口优先在**镜头切点**收口；时长落在 [min_s, max_s]；
    - 单镜超 max_s：镜内再切（唯一允许的非切点边界）；
    - 碎窗（< min_s）装不下下一块时从下一块借秒数（下块必然较长，否则贪心已合并）；
    - 末窗 < min_s：先并回上一段（≤max_s），并不动就从上段挪秒（上段保住 min_s）；
    - 总长 ≤ max_s：原样单段（打包入口只服务 >15s 的片长，行为照样定死）。
    """
    min_s = int(MIN_SEG_S if min_s is None else min_s)
    max_s = int(MAX_SEG_S if max_s is None else max_s)
    windows = _normalize_shot_windows(shots, int(total_s))
    if int(total_s) <= max_s:
        return [(0, int(total_s))]
    blocks = _split_overlong_blocks(windows, min_s, max_s)

    segments: list[tuple[int, int]] = []
    cur_s, cur_e = blocks[0]
    for b_s, b_e in blocks[1:]:
        if (cur_e - cur_s) + (b_e - b_s) <= max_s:
            cur_e = b_e  # 贪心吞并整块（切点收口）
            continue
        if (cur_e - cur_s) >= min_s:
            segments.append((cur_s, cur_e))
            cur_s, cur_e = b_s, b_e
            continue
        # 当前窗 < min_s 且装不下整块：从 b 借到 min_s（b 的长度 > max_s - min_s ≥ need）
        need = min_s - (cur_e - cur_s)
        cur_e = b_s + need
        segments.append((cur_s, cur_e))
        cur_s, cur_e = cur_e, b_e

    if (cur_e - cur_s) < min_s and segments:
        prev_s, prev_e = segments[-1]
        if (prev_e - prev_s) + (cur_e - cur_s) <= max_s:
            segments[-1] = (prev_s, cur_e)  # 并回上一段
            cur_s = cur_e
        else:
            need = min_s - (cur_e - cur_s)
            move = min(need, (prev_e - prev_s) - min_s)  # 上段保住 min_s（组合 >max 时必够挪）
            segments[-1] = (prev_s, prev_e - move)
            cur_s = prev_e - move
    if cur_e > cur_s:
        segments.append((cur_s, cur_e))
    return segments


def _load_instruction(mode: str = "base") -> str:
    """规划指令真源。ref（多参）与 base（首尾帧）各一份：两者的锚定语义不同
    （多参无桥接帧、起止状态进提示词），塞一个文件里改一处漏半处。"""
    name = "segment_planner_ref.md" if mode == "ref" else "segment_planner.md"
    path = PROMPTS_DIR / name
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


def parse_segment_plan(text: str, total_s: int, *, mode: str = "base",
                       shot_cuts: tuple[int, ...] | None = None) -> list[SegmentPlan] | None:
    """解析并校验 segment planner 输出。失败返回 None。

    时长上限分路线（ADR 0012）：base 4–10（首尾帧链规划保守档，硬闸不动），
    ref 4–15（59- 节点契约）。ref 另外要求：

    - ``start_state`` 与 ``end_hook`` 都非空——多参没有桥接帧，起止状态声明是段间
      叙事衔接的唯一显式依据，缺一个整段产物就不合格（拒收→调用方回退机械拆分）；
    - 给定 ``shot_cuts``（镜头切点表）时，内部段边界必须落在切点上（「不均匀硬切，
      边界落在镜头切点」）；唯一例外是镜头本身超过执行窗口（> MAX_SEG_S）时允许
      镜内切——长镜头拆段本来就没有切点可用。
    """
    limit = MAX_SEG_S if mode == "ref" else BASE_MAX_SEG_S
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
        # 硬约束：边界整数（int() 已保证）、时长在本路线档内、递增连续覆盖
        if duration < MIN_SEG_S or duration > limit:
            return None
        if i == 0 and start != 0:
            return None
        if plans and plans[-1].end_s != start:
            return None
        shots = item.get("shots_in_segment", [])
        if isinstance(shots, str):
            shots = [shots]  # 容错
        start_state = str(item.get("start_state", ""))
        end_hook = str(item.get("end_hook", ""))
        if mode == "ref" and (not start_state.strip() or not end_hook.strip()):
            return None
        plans.append(SegmentPlan(
            index=i,
            start_s=start,
            end_s=end,
            shots_in_segment=tuple(int(s) for s in shots if isinstance(s, (int, str))),
            summary=str(item.get("summary", "")),
            end_hook=end_hook,
            start_state=start_state,
        ))
    # 最后一段 end 可以略超 total（留余量）但不许欠
    if plans[-1].end_s < total_s:
        return None
    if mode == "ref" and shot_cuts is not None and not _boundaries_on_cuts(plans, shot_cuts):
        return None
    return plans


def _boundaries_on_cuts(plans: list[SegmentPlan], shot_cuts: tuple[int, ...]) -> bool:
    """每个内部段边界要么落在镜头切点上，要么落在超长镜头（> MAX_SEG_S）内部。"""
    cuts = set(shot_cuts)
    spans = sorted((shot_cuts[i], shot_cuts[i + 1]) for i in range(len(shot_cuts) - 1))
    for boundary in (p.end_s for p in plans[:-1]):
        if boundary in cuts:
            continue
        inside_overlong = any(s < boundary < e and (e - s) > MAX_SEG_S for s, e in spans)
        if not inside_overlong:
            return False
    return True


def plan_segments(shot_table: str, total_s: float, llm, *, frame_context: str = "",
                  revision_context: str = "", mode: str = "base",
                  shot_cuts: tuple[int, ...] | None = None) -> list[SegmentPlan] | None:
    """调 LLM 规划分段；任一失败返回 None（调用方回退机械拆分）。

    shot_table：阶段 1 的「分镜设计」文本（[Shot N] 描述 + 切点）。
    frame_context：真实关键帧图片的读图结果（``segment_prompts.frame_anchor_context``）。
    分镜表只是**计划**，用户可能复用/修改首帧图；两者冲突时以图片为准，否则
    plan 层会把「空店→猫进门」这类已被图片否定的状态固化进每一段。
    revision_context：用户的累积修订 ＋ 注入优先序（``segment_prompts.render_revision_context``）。
    本函数**不依赖 state**——修订与帧锚一样经显式入参进来，于是「规划层到底看到了什么」
    在调用点一眼可见；issue #26 的断链正是「收了一个没人读的参数」那种形态。

    mode="ref"（多参分段，issue #46）：换 ref 规划指令（镜头切点边界、4–15s、
    起止状态英文进提示词），解析侧同时启用 ref 契约（状态必填 + 切点对齐）。
    shot_cuts：镜头切点表（含 0 与 total），供「边界落在镜头切点」的确定性校验；
    抽不出切点时传 None，只靠指令约束。
    """
    total_int = int(round(total_s))
    frame_block = (
        f"\n{FRAME_CONTEXT_HEADER}\n{frame_context}\n"
        if frame_context.strip() else ""
    )
    revision_block = f"\n{revision_context}\n" if revision_context.strip() else ""
    request = (
        f"{_load_instruction(mode)}\n\n"
        f"视频总时长：{total_int}s\n"
        f"{frame_block}"
        f"{revision_block}"
        f"\n分镜表：\n{shot_table}"
    )
    try:
        response = llm.invoke(request)
    except Exception:  # noqa: BLE001
        return None
    text = getattr(response, "content", response)
    return parse_segment_plan(str(text), total_int, mode=mode, shot_cuts=shot_cuts)

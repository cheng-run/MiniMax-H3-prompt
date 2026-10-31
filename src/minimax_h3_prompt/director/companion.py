"""逐段陪跑的可证层判定（issue #105，设计 §3.7）。

**纯函数、不碰磁盘**：调用方把量好的帧数传进来，这里只回答「这一段过没过」。盘上怎么
数帧住在 ``cache.py``，账怎么写住在 ``ledger.py``——一个契约只住一处（设计 §1 第 3 条）。

两条判定入口，帧数判据同一份（``_judge_frames``）：

- ``judge_video_evidence``：**产线路径**（#112 起）。证据是用户在陪跑里贴回的**本段
  成片视频**，``cache.actual_frames`` 数它；单段与多段共用（2026-10-31 复审更名）。
- ``judge_segment``：缓存侧的纯函数，保留它可以被直接调用（级联与旧测试按段缓存
  清单判「这段在不在」）；产线不再走这条，因为插件默认 raw 编码、缓存常常不存在。

判据**按路线帧形态**，**不复用** ``tools/preflight.H3_TIER_FRAMES``（ADR 0024 明令）：
那张表是 17k+5 档位，有引导段的导出帧恒为 17 的倍数，与它永不相交。抄过来只会让两条
路线的帧口径互相污染。

三种结论（票面三条 AC）：

==========================  ======  ================================
结论                         status  行为
==========================  ======  ================================
合规通过                     pass    写通过行
少几帧告警（容差内）         warn    仍写通过行（可证层放行，只是不可解释的差额要出站）
勾错段 / 该跑未跑 / 超容差   fail    不写行，停下重来
==========================  ======  ================================
"""
from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Sequence

from .frames import DirectorPlan, DirectorSegment

# 有引导段允许的「实际比导出少」的帧数（设计 §3.7，票面「先取 5」）。
# 存在的理由：插件会因下一段的相位对齐裁掉上一段尾部几帧（插件勘察 §4.3）。
# 用户可在 ``config/agent.yaml`` 的 ``director.frame_tolerance`` 覆盖——
# **配置是用户旋钮，常量是代码默认**；两者同值不是两处口径，是默认值的唯一来源。
DIRECTOR_FRAME_TOLERANCE = 5

STATUS_PASS = "pass"
STATUS_WARN = "warn"
STATUS_FAIL = "fail"

CODE_FRAME_OK = "FRAME_OK"                # 帧数合规（通过行的 code 列）
CODE_FRAME_SHORT = "FRAME_SHORT"          # 少几帧（容差内=告警，超容差=报红）
CODE_FRAME_OVERFLOW = "FRAME_OVERFLOW"    # 多于计划帧：缓存不是这一段的产物
CODE_FRAME_MISMATCH = "FRAME_MISMATCH"    # 无引导段的帧数与声明帧不等
CODE_SEGMENT_MISMATCH = "SEGMENT_MISMATCH"  # 勾错段：缓存里有别的段，没有这一段
CODE_SEGMENT_NOT_RUN = "SEGMENT_NOT_RUN"  # 该跑未跑：缓存目录里一段都没有


@dataclass(frozen=True)
class DirectorVerdict:
    """一段的判定结论。``passed`` 才写通过行，``status`` 决定出站用哪种口吻。"""

    status: str
    code: str
    message: str
    actual_frames: int | None
    expected_frames: int

    @property
    def passed(self) -> bool:
        return self.status in (STATUS_PASS, STATUS_WARN)


def judge_segment(segment: DirectorSegment, *,
                  actual_frames: int | None,
                  cached_segments: Sequence[int],
                  tolerance: int | None = None) -> DirectorVerdict:
    """判一段过没过。

    ``actual_frames`` 为 ``None`` 表示**该段号不在缓存清单里**——这正是「勾错段」
    与「该跑未跑」的形态，两者都直接报红（票面 AC3：不许静默通过）。

    ``tolerance`` 缺省时**在调用时**取模块常量：写成参数默认值会在函数定义那一刻
    求值，改常量对它无效（本仓「守卫形态要从常量派生」的教训）。
    """
    if tolerance is None:
        tolerance = DIRECTOR_FRAME_TOLERANCE
    if isinstance(tolerance, bool) or not isinstance(tolerance, int) or tolerance < 0:
        raise ValueError(f"帧容差必须是非负整数，实际 {tolerance!r}")

    present = tuple(sorted(int(item) for item in cached_segments))
    if actual_frames is None or segment.index not in present:
        if not present:
            return DirectorVerdict(
                STATUS_FAIL, CODE_SEGMENT_NOT_RUN,
                f"第 {segment.index} 段还没跑：段缓存目录里一段终稿帧都没有。"
                "请在导演台里用 runSelection 勾上这一段后点运行。",
                None, _expected(segment))
        newest = max(present)
        return DirectorVerdict(
            STATUS_FAIL, CODE_SEGMENT_MISMATCH,
            f"第 {segment.index} 段没跑，但缓存里已有第 "
            f"{'、'.join(str(item) for item in present)} 段"
            f"（最新是第 {newest} 段）——多半是 runSelection 勾错了段。"
            f"请回导演台**只勾第 {segment.index} 段**重跑，别拿别的段的帧顶替。",
            None, _expected(segment))

    return _judge_frames(segment, actual_frames, tolerance)


def _judge_frames(segment: DirectorSegment, actual_frames: int,
                  tolerance: int) -> DirectorVerdict:
    """**帧数**判定（``judge_segment`` 与 ``judge_video_evidence`` 共用的纯函数）。

    2026-10-31 复审采纳：证据来源换了（段缓存 → 用户贴回的成片视频），但「多少帧算
    过」必须只有一份判据。两条路径此前各自调 ``judge_segment`` 绕一圈，等于把
    「缓存清单里有没有这一段」这种证据层判断混进帧数判定；抽出来后谁调它都不再关心
    盘上有没有缓存。

    有引导段：多于导出帧＝不是本段产物；少 1..tolerance 帧＝告警放行；再少＝报红。
    无引导段：必须**等于**声明帧（没有裁尾理由，也没有容差）。
    """
    expected = _expected(segment)
    if segment.guided:
        if actual_frames > segment.export_frames:
            return DirectorVerdict(
                STATUS_FAIL, CODE_FRAME_OVERFLOW,
                f"第 {segment.index} 段实测 {actual_frames} 帧，多于计划导出 "
                f"{segment.export_frames} 帧：导出帧是定案值，多出来的帧说明这条视频"
                "不是这一段的产物，请确认贴的是本段成片。",
                actual_frames, expected)
        gap = segment.export_frames - actual_frames
        if gap == 0:
            return DirectorVerdict(
                STATUS_PASS, CODE_FRAME_OK,
                f"第 {segment.index} 段：实际 {actual_frames} 帧 ＝ 计划导出 "
                f"{segment.export_frames} 帧，通过。",
                actual_frames, expected)
        if gap <= tolerance:
            return DirectorVerdict(
                STATUS_WARN, CODE_FRAME_SHORT,
                f"第 {segment.index} 段：实际 {actual_frames} 帧，比计划导出 "
                f"{segment.export_frames} 帧**少 {gap} 帧**（容差 {tolerance} 内，"
                "是下一段相位对齐裁尾的常见形态）：放行。",
                actual_frames, expected)
        return DirectorVerdict(
            STATUS_FAIL, CODE_FRAME_SHORT,
            f"第 {segment.index} 段：实际 {actual_frames} 帧，比计划导出 "
            f"{segment.export_frames} 帧少 {gap} 帧，超出容差 {tolerance}："
            "这不是相位对齐能解释的差额，请重跑本段。",
            actual_frames, expected)

    # 无引导段：导出帧＝声明帧，没有裁尾理由，也没有容差。
    if actual_frames == segment.declared_frames:
        return DirectorVerdict(
            STATUS_PASS, CODE_FRAME_OK,
            f"第 {segment.index} 段（无引导）：实际 {actual_frames} 帧 ＝ 声明帧，通过。",
            actual_frames, expected)
    return DirectorVerdict(
        STATUS_FAIL, CODE_FRAME_MISMATCH,
        f"第 {segment.index} 段（无引导）：实际 {actual_frames} 帧 ≠ 声明帧 "
        f"{segment.declared_frames} 帧。无引导段没有裁尾理由，请重跑本段；"
        "若反复不等，核一下导演台该段的 durationSec 是否被 UI 归一化改过。",
        actual_frames, expected)


def _export_forms(plan: DirectorPlan,
                  segment: DirectorSegment) -> tuple[tuple[int, int], ...]:
    """该段可能出现在的合并导出形态：``(前 k 段, 累计导出帧)``，k 从本段到末段。

    导演包固定 ``exportMode="all"``（``pack.py`` 的装配约束 7）：「全部导出」拼的是
    时间线上**所有已有帧的段**，未勾的段从缓存填（插件 ``executor_core.py:467-471``、
    ``:1833-1837``）。所以跑第 N 段贴回来的视频，总帧数是 **``end_frame[N]``**（前
    N 段），不是本段的 ``export_frames``——2026-10-31 现场 226 ＝ 39 + 187。
    """
    return tuple(
        (k, plan.segments[k - 1].end_frame)
        for k in range(segment.index, len(plan.segments) + 1))


def _match_cumulative_export(plan: DirectorPlan, segment: DirectorSegment,
                             actual_frames: int,
                             tolerance: int) -> tuple[int, int] | None:
    """视频总帧数落在哪一种合并导出上；都不落则返回 ``None``。

    每个接缝的相位对齐最多裁掉前一段 ``tolerance`` 帧（``_judge_frames`` 的同一
    容差），所以前 k 段的合法区间是 ``[end_frame[k] - tolerance × (k - 1),
    end_frame[k]]``。段长远大于容差×段数，各区间不重叠，取上界最近的那一个即可。
    """
    candidates = []
    for k, total in _export_forms(plan, segment):
        if total - tolerance * (k - 1) <= actual_frames <= total:
            candidates.append((k, total))
    if not candidates:
        return None
    return min(candidates, key=lambda item: abs(item[1] - actual_frames))


def _cumulative_verdict(segment: DirectorSegment, actual_frames: int,
                        k: int, total: int, *, tolerance: int,
                        plan: DirectorPlan) -> DirectorVerdict:
    """合并导出形态的判定：本段在片子里，帧数按导出帧算，直接放行。

    为什么不再拿总帧数去比本段导出帧：两者本来就不该相等（一个是累计、一个是本段）。
    真正的帧数判据由时间线给定；被下一段相位对齐裁掉 ≤ ``tolerance`` 帧属于已知形态，
    仍然算过（与 ``_judge_frames`` 的告警口径一致）。
    """
    if k == segment.index:
        form = f"前 {k} 段合并导出"
        detail = f"本段占最后 {segment.export_frames} 帧"
    else:
        form = "整片合并导出" if k == len(plan.segments) else f"前 {k} 段合并导出"
        detail = (f"本段占第 {segment.start_frame}–{segment.end_frame - 1} 帧"
                  f"（可能被下一段相位对齐裁掉 ≤{tolerance} 帧）")
    return DirectorVerdict(
        STATUS_PASS, CODE_FRAME_OK,
        f"第 {segment.index} 段：这条是{form}（{actual_frames} 帧），{detail}；"
        f"导出帧 {segment.export_frames} 帧，通过。",
        actual_frames, _expected(segment))


def _no_export_match_verdict(plan: DirectorPlan, segment: DirectorSegment,
                             actual_frames: int,
                             tolerance: int) -> DirectorVerdict:
    """总帧数对不上任何导出形态：这条视频不是本段的产物（或不是这一次的）。"""
    forms = "、".join(
        f"前 {k} 段 {total} 帧"
        for k, total in _export_forms(plan, segment))
    return DirectorVerdict(
        STATUS_FAIL, CODE_FRAME_OVERFLOW,
        f"第 {segment.index} 段实测 {actual_frames} 帧：既不是单段导出"
        f"（{segment.export_frames} 帧），也不是本段在时间线上的合并导出"
        f"（{forms}；每个接缝允许少 ≤{tolerance} 帧）。请确认贴的是本段成片。",
        actual_frames, _expected(segment))


def judge_video_evidence(segment: DirectorSegment, *,
                         actual_frames: int | None,
                         plan: DirectorPlan | None = None,
                         tolerance: int | None = None) -> DirectorVerdict:
    """成片视频证据的判定：**单段与多段共用**；证据是用户贴回的成片视频（#112）。

    为什么不能再叫 ``judge_single_shot``：2026-10-31 起多段陪跑的判定证据也统一换成
    用户贴回的成片视频，缓存帧只作定位/现场线索（不再参与判定）。旧名字会让人以为这
    只服务单段，从而在多段路径上另写一份帧数判据——「同一语义只许一处口径」。

    单段计划仍是最典型的入口：插件**不会**为它写段缓存（``executor_core.py:106-125``
    的 ``_segment_disk_cache_needed``，且 ``segment_continuity.py:192-207`` 在
    ``segment_count < 2`` 时关掉段间引导）。2026-10-30 真实跑现场：单段 i2v 跑完
    243 帧，``output/minimax_seg_cache`` 目录不存在，旧陪跑却对着一条已经跑完的片子
    报 `SEGMENT_NOT_RUN`——错的是**证据来源**，不是用户没跑。

    帧数口径**复用** ``_judge_frames``（与 ``judge_segment`` 同一份），不再绕
    ``judge_segment`` 的「段在不在缓存清单里」那一层：视频已经交到手里，那层判断
    既不可达，也只会误报。

    **``plan`` 给了就按「合并导出」认形态**（2026-10-31 现场 226 帧误报后加）：导演包
    固定 ``exportMode="all"``，陪跑贴回来的通常是「前 N 段合并导出」或「整片」，总
    帧数是累计值，不是本段的 ``export_frames``。先认出形态、再按本段帧数放行；认不出
    才报红。``plan=None`` 保留给直接调用的旧入口（按单段导出判）。
    """
    if actual_frames is None:
        raise ValueError(
            "单段判定的实际帧数不能为 None：这一情形的证据是用户交回来的视频文件，"
            "读不出帧数应当由调用方先报错，不能走到判定这一步")
    if tolerance is None:
        tolerance = DIRECTOR_FRAME_TOLERANCE
    if isinstance(tolerance, bool) or not isinstance(tolerance, int) or tolerance < 0:
        raise ValueError(f"帧容差必须是非负整数，实际 {tolerance!r}")
    if plan is None:
        return _judge_frames(segment, actual_frames, tolerance)

    form = _match_cumulative_export(plan, segment, actual_frames, tolerance)
    if form is not None:
        k, total = form
        return _cumulative_verdict(
            segment, actual_frames, k, total, tolerance=tolerance, plan=plan)
    # 比本段导出帧还短，或在单段导出的容差内：仍是「单段视频」的帧数判据
    # （短多少、是否在容差内由 ``_judge_frames`` 说准，不在这里另造一套文案）。
    expected = _expected(segment)
    if actual_frames <= expected:
        return _judge_frames(segment, actual_frames, tolerance)
    return _no_export_match_verdict(plan, segment, actual_frames, tolerance)


def _expected(segment: DirectorSegment) -> int:
    """该段的期望帧数：有引导段看导出帧，无引导段看声明帧（两者在无引导时相等）。"""
    return segment.export_frames if segment.guided else segment.declared_frames

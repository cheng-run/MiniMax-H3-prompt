"""逐段陪跑的可证层判定（issue #105，设计 §3.7）。

**纯函数、不碰磁盘**：调用方拿 ``cache.cached_segments`` 与 ``cache.actual_frames``
的读数进来，这里只回答「这一段过没过」。盘上怎么数帧住在 ``cache.py``，账怎么写住在
``ledger.py``——一个契约只住一处（设计 §1 第 3 条）。

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

from .frames import DirectorSegment

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


def judge_single_shot(segment: DirectorSegment, *,
                      actual_frames: int | None,
                      tolerance: int | None = None) -> DirectorVerdict:
    """单段计划（插件**不会**为它写段缓存）的判定：证据是成片视频，不是段缓存。

    为什么必须单列一条：插件的磁盘缓存是给「分段重跑 / refine / 段间引导」用的
    （``executor_core.py:106-125`` 的 ``_segment_disk_cache_needed``）。单段计划三样
    都不成立——``segment_continuity.py:192-207`` 在 ``segment_count < 2`` 时直接关掉
    段间引导，于是插件**一份缓存都不写**。2026-10-30 真实跑现场：单段 i2v 跑完 243 帧
    （`MiniMax-H3视频_00001_.mp4`，243 帧 ＠24fps，与声明帧一致），
    ``output/minimax_seg_cache`` 目录根本不存在，陪跑却报 `SEGMENT_NOT_RUN`
    「这一段还没跑」——错的是**证据来源**，不是用户没跑。

    帧数口径**复用** ``judge_segment``（无引导段：实际必须等于声明帧），这里只替掉
    「段在不在缓存清单里」这一层：视频已经交到手里，那层判断既不可达，也只会误报。
    """
    if actual_frames is None:
        raise ValueError(
            "单段判定的实际帧数不能为 None：这一情形的证据是用户交回来的视频文件，"
            "读不出帧数应当由调用方先报错，不能走到判定这一步")
    return judge_segment(segment, actual_frames=actual_frames,
                         cached_segments=(segment.index,), tolerance=tolerance)


def _expected(segment: DirectorSegment) -> int:
    """该段的期望帧数：有引导段看导出帧，无引导段看声明帧（两者在无引导时相等）。"""
    return segment.export_frames if segment.guided else segment.declared_frames

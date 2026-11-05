"""画幅核对的流程胶水（issue #158）：把纯判定接到导演台的两处真实读取口。

为什么与纯判定模块（``aspect``）分开：那边只放纯函数，谁也不依赖；这里要读导演包
落点、段首帧 sidecar、关键帧画布与视频元信息，还要生成给用户看的整段文案。混在
一个模块里，纯函数的用例就得为「文件读不到」「配置缺失」造一堆桩。

三处调用（段规划前的预检、逐段贴回成片、最终交付打包前）都从这里取文案与判定，
纯算法仍只住纯判定模块。``i2v``／``fl2v`` 用锚图/关键帧画布；产资产模式（``r2v``，
#163／#176）取向导答的成片画幅，并带线性尺寸判据。
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from ..brief_parser import DIRECTOR_ASSET_MODES, CanvasMap
from .aspect import check_aspect, check_size, image_size
from .firstframe import FIRST_FRAME_IMAGE_FIELD, firstframe_path
from .frame_meta import read_frame_meta
from .frames import FIRST_SEGMENT_TYPE, needs_first_frame_image
from .pack import film_end_still, start_still

PRECHECK_MODES = ("i2v", "fl2v")
"""画幅核对只在 i2v／fl2v 触发：t2v 没有关键帧可裁，r2v 的参考图不是锚点——拿帧生图
画布当期望值会把正常竖片全判成超差。**模式闸只住这里一处**，向导预检从这里派生。"""

FILM_SIZE_CONFIRM_KEY = "film_size_confirm"
"""尺寸差一档的确认记录（#176）：向导侧键、不进图，登记在
``session_store.PERSISTED_OUTSIDE_GRAPH``。形状 ``{mode, declared, actual, at}``——
「任一边数值变化即作废重问」靠这三项逐项比出来，换第三档尺寸会重新确认。"""


def frame_canvas(canvases: CanvasMap | None = None) -> tuple[int, int]:
    """关键帧生图模型在当前会话画布表下的生效画布（宽, 高）。

    预检（``wizard``）与回贴核对（``expected_output_frame``）都拿它当回落值：同一份
    期望画幅只许有一个算法，否则两处会各算一套（#157 复审）。
    """
    from ..generation import frame_canvas_size
    from ..image_config import load_frame_image_model

    return frame_canvas_size(load_frame_image_model(), canvases)


def _anchor_frame(
    mode: str, segment_index: int, segment_type: str, directory: Path,
) -> tuple[tuple[int, int], str] | None:
    """本段盘上真实存在的锚图与它的宽高；没有可用锚图返回 ``None``。

    优先级照票面：开场 ``start`` → 全片 ``film-end`` → 切镜段用户挂过的首帧图。
    读不出尺寸（文件没了 / 损坏）就当这张锚图不存在，由调用方回落关键帧画布——
    帧数校验已经是更硬的证据，一张坏图不该把整段陪跑卡死。
    """
    candidates: list[tuple[Path, str]] = []
    if segment_type == FIRST_SEGMENT_TYPE:
        start = start_still(directory, segment_index)
        if start is not None:
            candidates.append((start, f"第 {segment_index} 段开场首帧"))
    if segment_type == "ending":
        end = film_end_still(directory)
        if end is not None:
            candidates.append((end, "全片尾帧"))
    if needs_first_frame_image(segment_type, mode):
        meta = read_frame_meta(firstframe_path(directory, segment_index)) or {}
        recorded = str(meta.get(FIRST_FRAME_IMAGE_FIELD) or "")
        if recorded:
            candidates.append((Path(recorded), f"第 {segment_index} 段首帧图"))
    for path, label in candidates:
        size = image_size(path)
        if size is not None:
            return size, f"{label} {path}"
    return None


def expected_output_frame(
    *, mode: str, segment_index: int, segment_type: str,
    directory: str | Path, canvases: CanvasMap | None = None,
) -> tuple[tuple[int, int], str]:
    """本段成片的期望宽高与来源：有锚图用锚图，没有锚图回落关键帧生图模型的画布。"""
    anchor = _anchor_frame(mode, segment_index, segment_type, Path(directory))
    if anchor is not None:
        return anchor
    return frame_canvas(canvases), "关键帧生图模型的生效画布"


def precheck_message(
    mode: str, *, frame_canvas: tuple[int, int], declared: tuple[int, int],
) -> str | None:
    """段规划前要打印的预检文案；不触发（模式不符）或比例相符时返回 ``None``。

    文案必须同时给两边数值、两边比例与后果：用户要能判断该改画布还是改节点，
    「比例不一致」一句话帮不了他。
    """
    if mode not in PRECHECK_MODES:
        return None
    check = check_aspect(*frame_canvas, *declared)
    if not check.exceeds:
        return None
    return "\n".join([
        f"\n[画幅预检] 关键帧画布 {check.actual_width}×{check.actual_height}"
        f"（比例 {check.actual_ratio:.3f}）与 director 节声明的成片画幅 "
        f"{check.expected_width}×{check.expected_height}"
        f"（比例 {check.expected_ratio:.3f}）相差 "
        f"{check.relative_delta * 100:.1f}%，超过 {check.tolerance * 100:.0f}% 容差。",
        "  后果：导演台按声明画幅出片时，关键帧锚点会被裁切——比例差得越多，"
        "画面里的主体越可能被裁掉。",
        "  该改哪一边：重跑向导重选**成片画幅**（#161 起关键帧画布与包输出都由它派生），"
        "或确认导演台节点的 width/height 已按向导打印的值设置；"
        "旧会话仍可改 config/agent.yaml 的 director.width/height 兜底。",
    ])


def _video_size(video: Path) -> tuple[int, int] | None:
    """视频的实际宽高；读不出（损坏 / 不是视频 / 元信息缺失）返回 ``None``。"""
    from ..tools.frame_match import video_meta

    meta = video_meta(video)
    try:
        width = int(meta.get("width") or 0)
        height = int(meta.get("height") or 0)
    except (TypeError, ValueError):
        return None
    if width <= 0 or height <= 0:
        return None
    return width, height


def _stored_confirmation(
    stage_state: dict | None, *, mode: str,
    declared: tuple[int, int], actual: tuple[int, int],
) -> bool:
    """这条 (模式, 向导值, 实际值) 是否已在会话里确认过。

    错形按「没有确认」处理并重问：它是一份「别再来烦我」的缓存，不是产物——坏值
    重问一次是安全的，为它抛错反而会把整段陪跑卡死。值变化即不匹配，正好实现票面
    的「任一边数值变化即作废重问」。
    """
    if not isinstance(stage_state, dict):
        return False
    record = stage_state.get(FILM_SIZE_CONFIRM_KEY)
    if not isinstance(record, dict) or record.get("mode") != mode:
        return False

    def _pair(value: object) -> tuple[int, int] | None:
        if not isinstance(value, (list, tuple)) or len(value) != 2:
            return None
        if any(type(item) is not int for item in value):
            return None
        return value[0], value[1]

    return _pair(record.get("declared")) == declared and _pair(record.get("actual")) == actual


def _remember_confirmation(
    stage_state: dict | None, *, mode: str,
    declared: tuple[int, int], actual: tuple[int, int],
) -> None:
    """记下这次尺寸确认；没有会话状态可写时只是不记（调用方仍会放行）。"""
    if not isinstance(stage_state, dict):
        return
    stage_state[FILM_SIZE_CONFIRM_KEY] = {
        "mode": mode,
        "declared": [declared[0], declared[1]],
        "actual": [actual[0], actual[1]],
        "at": datetime.now(timezone.utc).isoformat(),
    }


def film_size_verdict(
    *, mode: str, video: Path, brief, label: str, consequence: str,
    session_created_at: str | None,
    stage_state: dict | None = None, ask: Callable[[str], str] | None = None,
) -> tuple[bool, str | None]:
    """产资产模式（r2v）的成片宽高核对：``(是否放行, 要打印的提示)``（#163／#176）。

    与 i2v／fl2v 的锚图核对**分开**：那两边比的是「关键帧锚点会不会被裁」，期望值
    是锚图或关键帧画布；这里比的是「用户答的成片画幅有没有真的跑出来」，期望值只取
    ``film_canvas.film_size_for``（ADR 0028 的单一真源）。拿两条判据互相代替，
    就会一边把正常取整判成事故、另一边把 2.2 倍的尺寸差放过去。

    ``session_created_at`` 是会话的创建时间（#179）：创建早于成片画幅问答（#161）的
    旧会话直接放行——它 ``canvases`` 里那格是**关键帧画布**，帧模型切回 ``z_image``
    后拿它比就是假硬停。时间**由调用方沿调用链传进来**，判据内部不读盘。

    处置分两档：比例超差 → 停（画面会被裁/拉伸，没有「继续」的出口）；只有尺寸差
    一档 → 打印两边数值、比例、来源，**一次确认**，拒绝则停。确认按
    ``(mode, declared, actual)`` 记住，任一边变化即重问。
    """
    if mode not in DIRECTOR_ASSET_MODES:
        return True, None

    from .film_canvas import (
        FILM_SIZE_LABEL, film_size_for, is_legacy_film_aspect_session,
    )

    if is_legacy_film_aspect_session(session_created_at):
        return True, (
            f"[画幅核对] {label}：这是旧会话（创建于成片画幅问答落地之前，"
            "按创建时间判定），本次放行，不参与比例／尺寸比对；"
            "请自行核对导演台节点的 width/height 与你想出的画幅一致。")

    expected = film_size_for(brief) if brief is not None else None
    if expected is None:
        return True, (
            f"[画幅核对] {label}：会话里没有成片画幅的答案（旧会话），本次放行；"
            "请自行核对导演台节点的 width/height 与你想出的画幅一致。")

    actual = _video_size(video)
    if actual is None:
        return True, (f"[警告] {label}读不出宽高（元信息缺失），画幅核对放行：{video}")

    source = f"{FILM_SIZE_LABEL}（向导唯一的画幅问答）"
    aspect = check_aspect(*actual, *expected)
    size = check_size(*actual, *expected)
    if aspect.exceeds:
        return False, "\n".join([
            f"\n[画幅核对] {label}的画幅与向导答的成片画幅不符，**停止**。",
            f"  向导答案（来源：{source}）：{expected[0]}×{expected[1]}"
            f"（比例 {aspect.expected_ratio:.3f}）",
            f"  实际成片：{actual[0]}×{actual[1]}"
            f"（比例 {aspect.actual_ratio:.3f}，{video}）",
            f"  比例相差 {aspect.relative_delta * 100:.1f}%，"
            f"超过 {aspect.tolerance * 100:.0f}% 容差；"
            f"宽相差 {size.width_delta * 100:.1f}%、"
            f"高相差 {size.height_delta * 100:.1f}%。",
            "  这不是「尺寸差一档」：比例不同，画面会被裁切或拉伸。"
            "请改导演台节点的 width/height 后重跑本段，再把这条视频贴上。",
            f"  {consequence}",
        ])

    if not size.exceeds:
        return True, None
    if _stored_confirmation(stage_state, mode=mode, declared=expected, actual=actual):
        return True, None
    if ask is None:
        # 没有交互入口时不许擅自放行：确认是票面要求的动作，缺入口就只能停。
        return False, (
            f"[画幅核对] {label}的尺寸与向导答案差一档，但没有可用的确认入口，按停止处理。"
            f"\n  {consequence}")

    # 解释先打印再问：用户要看着两边数值、比例与来源决定要不要继续。
    print("\n".join([
        f"\n[画幅核对] {label}的宽高与向导答的成片画幅差了一档（比例一致、尺寸不同）。",
        f"  向导答案（来源：{source}）：{expected[0]}×{expected[1]}"
        f"（比例 {aspect.expected_ratio:.3f}）",
        f"  实际成片：{actual[0]}×{actual[1]}"
        f"（比例 {aspect.actual_ratio:.3f}，{video}）",
        f"  宽相差 {size.width_delta * 100:.1f}%、"
        f"高相差 {size.height_delta * 100:.1f}%，"
        f"超过 {size.tolerance * 100:.0f}% 容差；"
        f"比例只相差 {aspect.relative_delta * 100:.1f}%（在 "
        f"{aspect.tolerance * 100:.0f}% 内）。",
    ]))
    answer = ask(
        "[成片画幅] 尺寸差一档：确认继续使用这条成片吗？"
        "（回车=确认继续；其它输入=停下）：")
    if answer.strip():
        return False, f"[成片画幅] 已停下：{label}的尺寸与向导答案差一档，未获确认。\n  {consequence}"
    _remember_confirmation(stage_state, mode=mode, declared=expected, actual=actual)
    return True, None


def output_aspect_verdict(
    *, mode: str, segment_index: int, segment_type: str, video: Path,
    directory: str | Path, session_created_at: str | None,
    canvases: CanvasMap | None = None,
    brief=None, ask: Callable[[str], str] | None = None,
    stage_state: dict | None = None,
) -> tuple[bool, str | None]:
    """贴回成片的画幅核对：``(是否放行, 要打印的提示)``。

    ``False`` ＝不落账（段账与改写账都不写、进度不前进）。读不出宽高时按
    「元信息缺失」**放行**并给警告——帧数校验已经是更硬的证据。
    ``i2v``／``fl2v`` 走锚图/关键帧画布那条既有判据（行为与文案不变）；产资产模式
    （#170 收口后只剩 ``r2v``）走 ``film_size_verdict``，期望值只取向导答的成片画幅。
    ``session_created_at`` 只对产资产模式有意义（#179）：旧会话直接放行。它是**必填**
    的（不给默认值）：漏传就抛 ``TypeError``，不会静默退回「永远放行」。
    """
    if mode in DIRECTOR_ASSET_MODES:
        return film_size_verdict(
            mode=mode, video=video, brief=brief,
            label=f"第 {segment_index} 段成片",
            consequence="这一段不记入段产出账，进度停在这里；已通过的段留在账上。",
            stage_state=stage_state, ask=ask,
            session_created_at=session_created_at)
    if mode not in PRECHECK_MODES:
        return True, None

    actual = _video_size(video)
    if actual is None:
        return True, (f"[警告] 第 {segment_index} 段成片读不出宽高（元信息缺失），"
                      f"画幅核对放行：{video}")
    width, height = actual

    (expected_width, expected_height), source = expected_output_frame(
        mode=mode, segment_index=segment_index, segment_type=segment_type,
        directory=directory, canvases=canvases)
    check = check_aspect(width, height, expected_width, expected_height)
    if not check.exceeds:
        return True, None
    return False, "\n".join([
        f"\n[画幅核对] 第 {segment_index} 段成片的画幅与本段期望不符，**拒绝落账**。",
        f"  期望：{expected_width}×{expected_height}"
        f"（比例 {check.expected_ratio:.3f}，来源：{source}）",
        f"  实际：{width}×{height}（比例 {check.actual_ratio:.3f}，{video}）",
        f"  相差 {check.relative_delta * 100:.1f}%，"
        f"超过 {check.tolerance * 100:.0f}% 容差。",
        "  下一步：改导演台节点 width/height 后重跑本段，再把这条视频贴上"
        "（输入 e 改本段提示词、q 停下）。",
        "  这一段不记入段产出账，进度停在这里；已通过的段留在账上。",
    ])


def final_film_verdict(
    *, mode: str, video: Path, brief, session_created_at: str | None,
    stage_state: dict | None = None,
    ask: Callable[[str], str] | None = None,
) -> tuple[bool, str | None]:
    """最终交付前的成片宽高核对（#176）：收到成片 mp4 之后、打包之前。

    与逐段贴回共用 ``film_size_verdict`` 这一份判据；只有标签与后果不同——交付停下
    意味着不进入归档，而不是「这一段不落账」。非产资产模式（t2v）直接放行。
    ``session_created_at``（#179）同样沿调用链传入，旧会话放行。
    """
    return film_size_verdict(
        mode=mode, video=video, brief=brief, label="最终成片",
        consequence="交付停在打包之前；段产出账里已通过的段保持不动。",
        stage_state=stage_state, ask=ask,
        session_created_at=session_created_at)

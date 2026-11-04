"""画幅核对的流程胶水（issue #158）：把纯判定接到导演台的两处真实读取口。

为什么与纯判定模块（``aspect``）分开：那边只放纯函数，谁也不依赖；这里要读导演包
落点、段首帧 sidecar、关键帧画布与视频元信息，还要生成给用户看的整段文案。混在
一个模块里，纯函数的用例就得为「文件读不到」「配置缺失」造一堆桩。

两处调用（段规划前的预检、贴回成片时的核对）都从这里取文案，判定仍只住纯判定模块。
"""
from __future__ import annotations

from pathlib import Path

from ..brief_parser import CanvasMap
from .aspect import check_aspect, image_size
from .firstframe import FIRST_FRAME_IMAGE_FIELD, firstframe_path
from .frame_meta import read_frame_meta
from .frames import FIRST_SEGMENT_TYPE, needs_first_frame_image
from .pack import film_end_still, start_still

PRECHECK_MODES = ("i2v", "fl2v")
"""画幅核对只在 i2v／fl2v 触发：t2v 没有关键帧可裁，r2v 的参考图不是锚点——拿帧生图
画布当期望值会把正常竖片全判成超差。**模式闸只住这里一处**，向导预检从这里派生。"""


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
        "  该改哪一边：改 config/image.yaml 的 frame: 档画布，或改 config/agent.yaml "
        "的 director.width/height（并以它设置导演台节点的 width/height）。",
    ])


def output_aspect_verdict(
    *, mode: str, segment_index: int, segment_type: str, video: Path,
    directory: str | Path, canvases: CanvasMap | None = None,
) -> tuple[bool, str | None]:
    """贴回成片的画幅核对：``(是否放行, 要打印的提示)``。

    ``False`` ＝拒绝落账（段账与改写账都不写、进度不前进）。读不出宽高时按
    「元信息缺失」**放行**并给警告——帧数校验已经是更硬的证据。
    """
    if mode not in PRECHECK_MODES:
        return True, None
    from ..tools.frame_match import video_meta

    meta = video_meta(video)
    try:
        width = int(meta.get("width") or 0)
        height = int(meta.get("height") or 0)
    except (TypeError, ValueError):
        width = height = 0
    if width <= 0 or height <= 0:
        return True, (f"[警告] 第 {segment_index} 段成片读不出宽高（元信息缺失），"
                      f"画幅核对放行：{video}")

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

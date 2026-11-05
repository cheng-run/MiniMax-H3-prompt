"""画幅一致性判定（issue #158）：预检与回贴核对共用的唯一一份判据。

为什么单独成模块：导演台路线上的两处核对（段规划前、贴回成片时）必须给同一个答案。
分头写两份比例算法，就会在「按像素差还是按比例差」「谁除以谁」上各说一套——本仓
「同一契约不许两处口径」已经咬过两次（#56/#57）。这里只放纯函数与读图尺寸的小工具，
不读配置、不打印、不提问。

两条判据并列，各管一件事：

- **比例判据**（``ASPECT_TOLERANCE``，5%）判画幅方向对不对——1920×1088 的关键帧与
  864×480 的成片像素差上百万，但比例只差约 2%，那是正常取整、不该报警；同一张关键帧
  对 480×864 的竖片差约 218%，才是 2026-11-04 验收里锚点被裁的那一类事故。相对差的
  分母取两个比例中**较小**的那个，否则 9:16 对 16:9 只会算出 68%，与验收记录里的
  218% 对不上。
- **线性尺寸判据**（``SIZE_TOLERANCE``，20%）判比例对、尺寸却差一档（#163／#176）：
  864×480 对 1920×1088 只差约 2% 的比例，但宽高各差 55%——只看比例就会把 2.2 倍的
  尺寸差放过去。宽、高**各自**判，分母取期望值（向导答案）。
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

ASPECT_TOLERANCE = 0.05
# 线性尺寸判据的容差（#163／#176）：比例判据只看宽高比，864×480 对 1920×1088
# 只差约 2% 的比例，却差了 2.2 倍的面积——「比例对、尺寸差一档」必须并列判出来。
SIZE_TOLERANCE = 0.20

# 浮点边界：1050/1000 的比例差算出来是 0.050000000000000044，直接 `> 0.05` 会把
# 「正好 5%」误判成超差。余量远小于可感知量级，只用来吃掉二进制浮点的尾数。
_TOLERANCE_EPSILON = 1e-9


@dataclass(frozen=True)
class AspectCheck:
    """一次画幅核对的结果：两边宽高、两个比例、相对差与是否超差。

    保留全部输入与中间值，是因为两处调用要把它们**原样打印**给用户
    （「我该改哪一边」靠的就是两边数值与各自的比例），而不是只报一句「不一致」。
    """

    actual_width: int
    actual_height: int
    expected_width: int
    expected_height: int
    actual_ratio: float
    expected_ratio: float
    relative_delta: float
    tolerance: float
    exceeds: bool


def _ratio(width: int, height: int, label: str) -> float:
    if width <= 0 or height <= 0:
        raise ValueError(f"{label}宽高必须为正：{width}×{height}")
    return width / height


def check_aspect(
    actual_width: int,
    actual_height: int,
    expected_width: int,
    expected_height: int,
    tolerance: float = ASPECT_TOLERANCE,
) -> AspectCheck:
    """``(实际宽, 实际高, 期望宽, 期望高, 容差)`` → 是否超差 + 比例数值。

    「实际」是要核对的那一边（预检＝关键帧画布，回贴＝贴回的成片），「期望」是它
    本该匹配的锚（预检＝``director`` 节声明的成片画幅，回贴＝本段锚图）。相对差对
    称，交换两边不改变结论，但打印时要按这个角色说人话。
    """
    if tolerance < 0:
        raise ValueError(f"容差必须非负：{tolerance}")

    actual_ratio = _ratio(actual_width, actual_height, "实际")
    expected_ratio = _ratio(expected_width, expected_height, "期望")
    relative_delta = abs(actual_ratio - expected_ratio) / min(actual_ratio, expected_ratio)
    return AspectCheck(
        actual_width=actual_width,
        actual_height=actual_height,
        expected_width=expected_width,
        expected_height=expected_height,
        actual_ratio=actual_ratio,
        expected_ratio=expected_ratio,
        relative_delta=relative_delta,
        tolerance=tolerance,
        exceeds=relative_delta > tolerance + _TOLERANCE_EPSILON,
    )


@dataclass(frozen=True)
class SizeCheck:
    """一次线性尺寸核对的结果：宽、高各自的相对差与是否超差（#176）。

    与 ``AspectCheck`` 并列而不是塞进同一个结构：两条判据的容差不同、处置也不同
    （比例超差＝停；尺寸超差＝一次确认），合并会让调用方分不清是哪种超差。
    相对差的分母取**期望值**（向导答案）：它是目标尺寸，实际值偏离它多少才是用户
    需要知道的数。
    """

    actual_width: int
    actual_height: int
    expected_width: int
    expected_height: int
    width_delta: float
    height_delta: float
    tolerance: float
    exceeds: bool


def check_size(
    actual_width: int,
    actual_height: int,
    expected_width: int,
    expected_height: int,
    tolerance: float = SIZE_TOLERANCE,
) -> SizeCheck:
    """``(实际宽, 实际高, 期望宽, 期望高, 容差)`` → 宽高各自是否超差。

    宽、高**各自**判：只看面积或只看一边，会把「1920×1080 对 1080×1920」这类
    交换宽高的片子当成尺寸相符。两条都落在容差内才算通过。
    """
    if tolerance < 0:
        raise ValueError(f"容差必须非负：{tolerance}")

    _ratio(actual_width, actual_height, "实际")
    _ratio(expected_width, expected_height, "期望")
    width_delta = abs(actual_width - expected_width) / expected_width
    height_delta = abs(actual_height - expected_height) / expected_height
    return SizeCheck(
        actual_width=actual_width,
        actual_height=actual_height,
        expected_width=expected_width,
        expected_height=expected_height,
        width_delta=width_delta,
        height_delta=height_delta,
        tolerance=tolerance,
        exceeds=(
            width_delta > tolerance + _TOLERANCE_EPSILON
            or height_delta > tolerance + _TOLERANCE_EPSILON
        ),
    )


def image_size(path: str | Path) -> tuple[int, int] | None:
    """图片的 ``(宽, 高)``；读不出（不存在 / 损坏 / 不是图片）返回 ``None``。

    调用方按「元信息缺失」处理并**放行**（票面 #158）：帧数校验已经是更硬的证据，
    一张读不出尺寸的锚图不该把整段陪跑卡死。
    """
    from PIL import Image  # noqa: PLC0415 - 只有画幅核对需要，别在导入期拉起 pillow

    try:
        with Image.open(path) as image:
            width, height = image.size
    except (OSError, ValueError):
        return None
    if width <= 0 or height <= 0:
        return None
    return int(width), int(height)

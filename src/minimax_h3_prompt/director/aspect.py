"""画幅一致性判定（issue #158）：预检与回贴核对共用的唯一一份判据。

为什么单独成模块：导演台路线上的两处核对（段规划前、贴回成片时）必须给同一个答案。
分头写两份比例算法，就会在「按像素差还是按比例差」「谁除以谁」上各说一套——本仓
「同一契约不许两处口径」已经咬过两次（#56/#57）。这里只放纯函数与读图尺寸的小工具，
不读配置、不打印、不提问。

容差按**比例相对差**（默认 5%），不按像素：1920×1088 的关键帧与 864×480 的成片
像素差上百万，但比例只差约 2%——那是正常取整，不该报警；同一张关键帧对 480×864
的竖片差约 218%，才是 2026-11-04 验收里锚点被裁的那一类事故。相对差的分母取两个
比例中**较小**的那个，否则 9:16 对 16:9 只会算出 68%，与验收记录里的 218% 对不上。
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

ASPECT_TOLERANCE = 0.05

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

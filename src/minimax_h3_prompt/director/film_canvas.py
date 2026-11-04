"""导演台成片画幅的单一真源（#161，ADR 0028）。

为什么需要这个模块：2026-11-04 的真实短片里，向导问的是关键帧画布、``config/agent.yaml``
的 ``director.width/height`` 另声明一份成片画幅、导演台节点上又是第三份（插件默认
864×480）。三份口径里没有一份是「用户这一次真的想要的画幅」，实跑因此出现过包写
736×1280、节点跑 864×480（关键帧 1920×1088 被中心裁掉主体）。

ADR 0028 的裁定：**向导只问一次成片画幅**，关键帧画布与导演包 ``output`` 都由它派生；
插件不保证把包的 ``output`` 应用到节点（同一次实跑里未生效），所以向导另行**交代**
节点该设的 width/height。本模块就是这三处唯一的算法所在地——纯函数、不读盘，
路径与配置由调用方取好再传进来（用例因此不用造文件桩）。
"""
from __future__ import annotations

from dataclasses import replace

from .pack import CANVAS_ALIGN, DirectorPackSettings

FILM_SIZE_LABEL = "成片画幅"


def _snap(value: int) -> int:
    """就近对齐到 ``CANVAS_ALIGN``，**四舍五入而不是银行家舍入**。

    ``round(22.5)`` 在 Python 里是 22（偶数优先），720 会被拉去 704；一半的概率
    往小掉会让 9:16 这类常见比例在对齐后系统性偏向一边。这里显式向上补半格。
    """
    snapped = int((int(value) + CANVAS_ALIGN // 2) // CANVAS_ALIGN) * CANVAS_ALIGN
    return max(CANVAS_ALIGN, snapped)


def snap_film_size(width: int, height: int) -> tuple[int, int]:
    """把用户选的画幅尺寸对齐到插件网格（后端会拒绝非 32 倍数的画布）。

    对齐发生在**问完之后**：菜单里的尺寸是生图尺寸（1920×1080 之类），拿它当视频
    输出会被插件拒绝；在这里对齐，用户看到的是「选了 1920×1080，实际 1920×1088」。
    """
    return _snap(width), _snap(height)


def film_frame_canvases(
    film: tuple[int, int], frame_model: str,
) -> dict[str, tuple[int, int]]:
    """关键帧画布＝成片画幅同尺寸，挂在**当前关键帧模型**的家族键上。

    同尺寸而不是「同比例的另一档」：比例差为 0，锚点不存在被中心裁切的余地。
    旧键位（``zimage``）按别名归一，不另立一份键——两套键会让读口各认一套。
    """
    from ..image_config import canonical_image_model

    return {canonical_image_model(frame_model): (int(film[0]), int(film[1]))}


def film_size_for(brief, *, frame_model: str | None = None) -> tuple[int, int] | None:
    """会话里定的成片画幅；旧会话（没有这一问的答案）返回 ``None``。

    读的是 ``Brief.canvases`` 里**当前关键帧模型家族**那一格：``film_frame_canvases``
    写进去的就是它。别的档的选定值不算数（#144 D：不许跨档继承），配置里换了帧模型
    后读不到就读不到——那时回落配置声明，而不是把上一档的尺寸硬套过来。
    """
    from ..image_config import canonical_image_model, load_frame_image_model

    family = canonical_image_model(frame_model or load_frame_image_model())
    canvases = getattr(brief, "canvases", None) or {}
    size = canvases.get(family)
    if not size:
        return None
    width, height = size
    return int(width), int(height)


def film_settings(
    settings: DirectorPackSettings, brief, *, frame_model: str | None = None,
) -> DirectorPackSettings:
    """导演包参数里的 width/height 换成成片画幅；参考图尺寸仍取配置。

    ``ref_max_size`` / ``ref_image_size`` 与画幅无关（它们管参考图清晰度），继续吃
    ``config/agent.yaml`` 的 ``director:`` 节——把它们也搬到这里就是又开一处真源。
    """
    film = film_size_for(brief, frame_model=frame_model)
    if film is None:
        return settings
    return replace(settings, width=film[0], height=film[1])


def node_setup_lines(settings: DirectorPackSettings) -> list[str]:
    """向导交代导演台节点的那几行：与包 ``output`` 同数、可直接照抄。

    为什么是「交代」而不是「依赖导入」：插件导入包时确实有一条把 ``output`` 应用
    到节点的路径（``web/js/minimax_timeline.js`` 的 ``applyImportedTimeline``），但
    2026-11-04 实跑里没生效（包 736×1280、节点 864×480），原因在浏览器层、未证实。
    在确认之前，最可靠的一步是让用户**看一眼节点并照着设**。
    """
    return [
        f"[导演台节点] 导入包后请核对节点的 width / height = "
        f"{settings.width} / {settings.height}，ref_max_size = {settings.ref_max_size}；"
        "包 output 与这三项同源（成片画幅只问这一处）。",
        "  插件不保证把包的 output 应用到节点（2026-11-04 实跑未生效）："
        "节点上的值才是实际执行值，别只信包里的声明。",
    ]

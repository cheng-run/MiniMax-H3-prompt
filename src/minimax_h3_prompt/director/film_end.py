"""全片尾帧（``director/assets/film-end.<ext>``）的画面描述：读一次图，落 sidecar 复用。

为什么要有这个口：用户给的全片尾帧是**终点画面**的唯一机读来源，而读图是要付费的
视觉调用。规划与写段各读一份就会两边各说一套、续跑还会重复付费；收成一个口之后，
「盘上描述还在不在」与「要不要重读」只有一处判据。

读图与缓存的实现住在 ``director/still_description.py``（#318 单源化）：本模块只给
**用途差异**——sidecar 的 ``kind`` 取值与失败消息里那句「全片尾帧读图失败」——以及
全片尾帧自己的请求块措辞。三个用途（全片尾帧 / 开场首帧）共用同一份缓存命中判据，
任何一条不满足就重读并覆盖；**读图失败**必须抛错，让调用方停在收集/规划/写段之前，
绝不带着「终点全靠猜」的段词往下走，且失败时**不落盘**。

为什么 sidecar 与图同目录而不是与正文同名：这张图不属于任何一段（``assets/film-end``
在段目录之外），它的描述也只能挂在图旁边。字段沿用 ``frame_meta`` 的 JSON 形态
（``write_frame_meta`` / ``read_frame_meta`` 按**图片路径**换算成 ``film-end.meta.json``），
不另立一套 sidecar 契约。
"""
from __future__ import annotations

from pathlib import Path

from .still_description import get_still_description

# ``kind`` 的取值：sidecar 里的用途标记，和别的 sidecar（尾帧词等）混在同一目录时
# 仍能认出这是「全片尾帧的读图结果」。
FILM_END_KIND = "film_end"

# 读图失败消息开头那句：共享实现按参数拼进 ``（图：…）：<病因>``，用途各说各的。
FILM_END_FAILURE_LABEL = "全片尾帧读图失败"

# 两个请求（规划 / 写段）共用的块标题：写死在一处，改措辞时两边不会各漂一套。
FILM_END_BLOCK_HEAD = "【全片终点画面（用户提供的尾帧图）】"


def render_film_end_block(description: str, *, for_planning: bool = False) -> str:
    """把描述渲染成请求里的一块；规划与写段共用同一份「以图为准」的措辞。

    ``for_planning`` 只改「拿它来做什么」那半句（规划最后一段的边界与落定画面 /
    写本段正文），判据与文案仍是同一处，避免两个消费者各自写一套终点口径。
    """
    # 不写死 ``ending``：单段 fl2v 的整片只有一段、类型是 ``opening``，它同样是
    # 用户那张尾帧图对应的终点（#157 复审）。两个类型都点名，模型才不会在单段时
    # 找不到「最后一段」。
    target = ("规划最后一段（多段是 ending，单段 fl2v 是 opening）的边界、"
              "summary 与 end_hook 时" if for_planning else "写本段正文时")
    return (
        f"\n{FILM_END_BLOCK_HEAD}\n"
        f"终点以这张图为据：全片最后一帧就是图上的画面。{target}以它为准；"
        "段词与图冲突时，以图为准（按图上的人物朝向、动作方向与物体状态写，"
        "不要照抄分镜表或标题里相反的措辞）。\n"
        f"画面描述：{description}\n"
    )


def get_film_end_description(session_dir: str | Path) -> str | None:
    """取全片尾帧的画面描述；盘上没有这张图返回 ``None``。

    命中条件由共享实现给（``kind`` 对、``description`` 非空、``image_sha256`` 与
    当前图一致——三条全中才复用）；任何一条不满足就重读一次并覆盖 sidecar。
    """
    from .pack import film_end_still

    image = film_end_still(session_dir)
    if image is None:
        return None
    return get_still_description(image, kind=FILM_END_KIND,
                                 failure_label=FILM_END_FAILURE_LABEL)

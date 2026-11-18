"""开场首帧图（``director/assets/01/start.<ext>``）的画面描述：读一次图、落 sidecar 复用。

为什么要有这个口：i2v 与 fl2v 的开场首帧图在阶段 1 之后被收进导流包，此后**再没有**
任何代码读过它——写段模型关于「本段第一帧长什么样」只有分镜表与剧情概述这类**意图**，
图里实际有几扇窗、人在窗的哪一侧全靠推断；推断与图不一致时，段词第一拍就与成片首帧
当场矛盾（spec #317 的 G3）。首尾帧链早已用读图结果当主锚定，本模块把同一件事补给
导演台：图上的画面是事实，剧本是意图，冲突时以图为准。

**与 ``director/firstframe.py`` 严格分开**：那个模块生产逐段首帧的**生图提示词**
（``director/firstframe-NN.md``，由词生图）；本模块读用户**已经交来的图**（由图生描述）。
两者名字里都有「首帧」，但一个是出词的，一个是读图的——混用会推出「切镜段首帧也要在
写段时读」这种做不到的方案（那张图在写第 N 段时盘上还不存在）。

读图与缓存的实现住在 ``director/still_description.py``（#318 单源化）：本模块只给
**用途差异**——sidecar 的 ``kind`` 取值与失败消息里那句「开场首帧图读图失败」——以及
开场首帧自己的请求块措辞。缓存按**图指纹**：图不换就不重读、不重复付费；图换了旧描述
自动作废、重读并覆盖（描述是对「这张图」说的，换了图就不再成立）。sidecar 损坏或字段
不齐按「没有」处理并重读，不把续跑拦死。**读图失败**相反——必须抛错，让调用方停在
规划/写段之前，绝不带着「起点画面全靠猜」的段词往下跑，且失败时**不落盘**。

**为什么不把读图结果混进首尾帧链的帧描述 state 键**（spec §10）：那是帧提交的产物，
塞进来会让「哪条路线看到了什么」重新散落，也会让持久化与续跑判据多一条隐式来源。
这份描述的落点就是图旁边的 sidecar，谁要读谁经这里取。
"""
from __future__ import annotations

from pathlib import Path

from ..writing_disciplines import FRAME_CONSISTENCY_DISCIPLINE
from .still_description import get_still_description

# ``kind`` 的取值：sidecar 里的用途标记，和别的读图结果（全片尾帧）混在同一目录时
# 仍能认出这是「开场首帧的读图结果」。共享用例已按这个字面量钉住用途隔离。
OPENING_STILL_KIND = "opening_still"

# 读图失败消息开头那句：共享实现按参数拼进 ``（图：…）：<病因>``，用途各说各的。
OPENING_STILL_FAILURE_LABEL = "开场首帧图读图失败"

# 两个请求（规划 / 写段）共用的块标题：与 ``FILM_END_BLOCK_HEAD`` 同形（``【…】``
# 独占一行），改措辞时只改这一处。
OPENING_STILL_BLOCK_HEAD = "【本段起点画面（用户提供的开场首帧图）】"


def render_opening_still_block(description: str, *,
                               for_planning: bool = False) -> str:
    """把开场首帧图的描述渲染成请求里的一块；规划与写段共用同一份措辞。

    措辞承担三件事：这是本段第一帧的**唯一画面事实**、以它为准、与分镜表或剧情
    概述冲突时以画面为准。``for_planning`` 只改「拿它来做什么」那半句（规划本段的
    边界与落定画面 / 写本段正文）——与 ``render_film_end_block`` 同形，避免两个
    消费者各自写一套起点口径。

    纪律句取自 ``writing_disciplines.FRAME_CONSISTENCY_DISCIPLINE`` 这一处真源，
    与首尾帧链逐字相同；本模块不复制文本（第三份硬拷贝正是 #319 清掉的东西）。

    空描述返回空串：调用方据此不产生块、也不产生空行。
    """
    if not description or not description.strip():
        return ""
    target = ("规划本段的边界、summary 与 end_hook 时" if for_planning
              else "写本段正文时")
    return (
        f"\n{OPENING_STILL_BLOCK_HEAD}\n"
        f"本段第一帧的唯一画面事实就是这张图：{target}以它为准；"
        "分镜表或剧情概述与它冲突时以画面为准（按图上的人物朝向、站位与物体状态写，"
        "不要照抄或推断图上没有的画面）。\n"
        f"画面描述：{description}\n"
        f"{FRAME_CONSISTENCY_DISCIPLINE}\n"
    )


def get_opening_still_description(session_dir: str | Path) -> str | None:
    """取开场首帧图的画面描述；盘上没有这张图返回 ``None``。

    落点经 ``director.pack.start_still`` 取——扩展名偏好与目录是打包的契约，这里
    不另写一份扩展名列表（换了列表就会「打包说有图、读图说没有」）。命中条件由共享
    实现给：``kind`` 对、``description`` 非空、``image_sha256`` 与当前图一致，三条
    全中才复用；任何一条不满足就重读一次并覆盖 sidecar。
    """
    from .pack import start_still

    image = start_still(session_dir, position=1)
    if image is None:
        return None
    return get_still_description(image, kind=OPENING_STILL_KIND,
                                 failure_label=OPENING_STILL_FAILURE_LABEL)

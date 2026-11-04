"""全片尾帧（``director/assets/film-end.<ext>``）的画面描述：读一次图，落 sidecar 复用。

为什么要有这个口：用户给的全片尾帧是**终点画面**的唯一机读来源，而读图是要付费的
视觉调用。规划与写段各读一份就会两边各说一套、续跑还会重复付费；收成一个口之后，
「盘上描述还在不在」与「要不要重读」只有一处判据。

为什么 sidecar 与图同目录而不是与正文同名：这张图不属于任何一段（``assets/film-end``
在段目录之外），它的描述也只能挂在图旁边。字段沿用 ``frame_meta`` 的 JSON 形态
（``write_frame_meta`` / ``read_frame_meta`` 按**图片路径**换算成 ``film-end.meta.json``），
不另立一套 sidecar 契约。

一致性的判据是**图指纹**：图换了＝描述作废，重读并覆盖；sidecar 损坏＝按没有处理
（重读，不把续跑拦死）。**读图失败**与坏 sidecar 相反——它必须抛错，让调用方停在
收集处，绝不带着「终点全靠猜」的段词往下走。
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from .frame_meta import read_frame_meta, sha256_file, write_frame_meta

# ``kind`` 的取值：sidecar 里的用途标记，和别的 sidecar（尾帧词等）混在同一目录时
# 仍能认出这是「全片尾帧的读图结果」。
FILM_END_KIND = "film_end"

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


def _cached_description(image: Path, fingerprint: str | None) -> str | None:
    """读 sidecar；字段不齐、用途不符或指纹对不上都当没有（返回 ``None``）。"""
    meta = read_frame_meta(image)
    if not isinstance(meta, dict):
        return None
    if meta.get("kind") != FILM_END_KIND:
        return None
    if not fingerprint or meta.get("image_sha256") != fingerprint:
        return None
    description = meta.get("description")
    if not isinstance(description, str) or not description.strip():
        return None
    return description.strip()


def _read_and_cache(image: Path, fingerprint: str | None) -> str:
    """读一次图并落 sidecar；失败抛 ``RuntimeError`` 且**不落盘**。"""
    # 延迟导入：本模块被 planning / prompts 在入口期 import，读图客户端只在真的要
    # 读图时才拉起（配置解析与 langchain 的角色栈都不该为一次纯文本拼装付成本）。
    from ..config import config
    from ..tools.reference_auditor import describe_image

    try:
        description = describe_image(image)
    except Exception as exc:  # noqa: BLE001 - 病因原样交回调用方，路径由这里补上
        raise RuntimeError(f"全片尾帧读图失败（图：{image}）：{exc}") from exc
    description = (description or "").strip()
    if not description:
        raise RuntimeError(
            f"全片尾帧读图失败（图：{image}）：视觉模型没有返回任何内容")
    # 这里**整份重置**而不是合并：图换了＝旧描述作废，sidecar 就该只剩这一张图的
    # 读图结果（与 ``merge_frame_meta`` 的累积语义相反，是显式选择；见 frame_meta）。
    write_frame_meta(image, {
        "kind": FILM_END_KIND,
        "description": description,
        # 模型名从配置现取：事后能追溯这份描述是哪个视觉模型读的（换档后旧描述
        # 仍按图指纹复用，不被模型名悄悄作废）。
        "vision_model": config.vision_model.model,
        "image_sha256": fingerprint,
        "at": datetime.now(timezone.utc).isoformat(),
    })
    return description


def get_film_end_description(session_dir: str | Path) -> str | None:
    """取全片尾帧的画面描述；盘上没有这张图返回 ``None``。

    命中条件：sidecar 存在、``kind`` 对、``description`` 非空、``image_sha256`` 与
    当前图一致——四条全中才复用。任何一条不满足就重读一次并覆盖 sidecar。
    """
    from .pack import film_end_still

    image = film_end_still(session_dir)
    if image is None:
        return None
    fingerprint = sha256_file(image)
    cached = _cached_description(image, fingerprint)
    if cached is not None:
        return cached
    return _read_and_cache(image, fingerprint)

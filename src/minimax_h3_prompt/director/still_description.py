"""读图类产物的唯一实现：读一次图、按图指纹缓存 sidecar、读图失败抛错（#318）。

为什么要有这个模块：全片尾帧（``director/film_end.py``）与开场首帧（#319）都要
「读一次用户交来的图、把描述挂在图旁边的 sidecar 上、图不换就别重读」。六十行的
缓存逻辑各写一份就会各漂一套——本仓刚用六张票清掉的「纪律句硬拷贝」是同一类
问题。收成一个口之后，「盘上描述还在不在」与「要不要重读」只有一处判据。

**差异只走参数**：sidecar 的 ``kind``（用途标记，和别的 sidecar 同目录时仍能认出
这是哪一类读图结果）与失败消息里那句「哪张图的读图失败」（``failure_label``）。
调用方不许各自再写一套「kind 相符吗、要不要重读」的分支——那正是本模块要消灭的。

一致性的判据是**图指纹**：图换了＝描述作废，重读并覆盖；sidecar 损坏＝按没有处理
（重读，不把续跑拦死），由 ``frame_meta.read_frame_meta`` 承担。**读图失败**与坏
sidecar 相反——它必须抛错，让调用方停在收集/规划/写段之前，绝不带着「画面全靠猜」
的段词往下走；而且失败时**不落盘**，不留半份 sidecar。

**图从哪来不归这里管**：落点目录与扩展名偏好是打包的契约（``director.pack`` 的
``film_end_still`` / ``start_still``），各用途自己取好路径再交进来。
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from .frame_meta import read_frame_meta, sha256_file, write_frame_meta


def _match_cached(meta: dict | None, fingerprint: str | None,
                  kind: str) -> str | None:
    """缓存命中契约的唯一实现：用途标记相符 ＋ 描述非空 ＋ 图指纹一致。

    三条是既有全片尾帧判据的原样（#160），换到共享模块只为不另立第二套：任何一条
    不满足就返回 ``None``，调用方据此重读并覆盖。
    """
    if not isinstance(meta, dict):
        return None
    if meta.get("kind") != kind:
        return None
    if not fingerprint or meta.get("image_sha256") != fingerprint:
        return None
    description = meta.get("description")
    if not isinstance(description, str) or not description.strip():
        return None
    return description.strip()


def read_cached_description(image: Path, *, kind: str) -> str | None:
    """只读 sidecar 并判命中；**绝不读图、绝不写盘**。

    离线导出脚本（``scripts/director_request_dump.py``）用它：真实写段会在缓存
    失效时当场调视觉模型并写会话目录，那是导出不该做的事（付费、改现场），所以
    导出只问「盘上那份还在不在」，并复用同一份命中契约。
    """
    return _match_cached(read_frame_meta(image), sha256_file(image), kind)


def _read_and_cache(image: Path, fingerprint: str | None, *,
                    kind: str, failure_label: str) -> str:
    """读一次图并落 sidecar；失败抛 ``RuntimeError`` 且**不落盘**。"""
    # 延迟导入：本模块被 planning / prompts 在入口期 import，读图客户端只在真的要
    # 读图时才拉起（配置解析与 langchain 的角色栈都不该为一次纯文本拼装付成本）。
    from ..config import config
    from ..tools.reference_auditor import describe_image

    try:
        description = describe_image(image)
    except Exception as exc:  # noqa: BLE001 - 病因原样交回调用方，路径由这里补上
        raise RuntimeError(f"{failure_label}（图：{image}）：{exc}") from exc
    description = (description or "").strip()
    if not description:
        raise RuntimeError(
            f"{failure_label}（图：{image}）：视觉模型没有返回任何内容")
    # 这里**整份重置**而不是合并：图换了＝旧描述作废，sidecar 就该只剩这一张图的
    # 读图结果（与 ``merge_frame_meta`` 的累积语义相反，是显式选择；见 frame_meta）。
    write_frame_meta(image, {
        "kind": kind,
        "description": description,
        # 模型名从配置现取：事后能追溯这份描述是哪个视觉模型读的（换档后旧描述
        # 仍按图指纹复用，不被模型名悄悄作废）。
        "vision_model": config.vision_model.model,
        "image_sha256": fingerprint,
        "at": datetime.now(timezone.utc).isoformat(),
    })
    return description


def get_still_description(image: Path, *, kind: str,
                          failure_label: str) -> str:
    """取一张图的画面描述：命中 sidecar 就复用，否则读一次并覆盖。

    ``kind`` 是 sidecar 的用途标记，``failure_label`` 是失败消息开头那句「哪张图」。
    两者都由用途模块给，本函数不认识任何具体用途。
    """
    fingerprint = sha256_file(image)
    cached = _match_cached(read_frame_meta(image), fingerprint, kind)
    if cached is not None:
        return cached
    return _read_and_cache(image, fingerprint, kind=kind,
                           failure_label=failure_label)

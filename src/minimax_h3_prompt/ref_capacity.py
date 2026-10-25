"""参考素材的官方容量上限：9 图 / 3 视频 / 3 音频 / 总文件 12（issue #86 / spec #84 D4）。

**为什么是硬上限**：这组数字取自官方 API 的单次请求规范（spec #84「事实底账」），
不是本仓的保守估计；而本机实测在同一个 9 上独立撞线——24 张参考图把 Qwen3-VL 的
编码序列撑到约 5.4 万 token，16GB 卡上只剩朴素注意力，``einsum`` 当场要 232 GiB
（2026-10-22 的 ``torch.OutOfMemoryError``）。所以「超了会怎样」不是变慢而是跑不出来，
校验必须发生在**提交之前**、在仓库这一侧。

**为什么必须点名**：超限时能做的只有两件事——精简素材，或把这一段再拆短。两条都需要
知道「是哪几张超的」。静默截断会留下正文引用却没接线的素材（`<Picture N>` 悬空），
自动丢弃则是替用户删了他交回的素材；这两条都被 spec #84 明确否掉。故本模块只做一件事：
数出来、点名、抛错。

**四个上限彼此独立**：单型在线上不等于整体合法——9 图 + 3 视频正好用满 12 个文件的
预算，再挂 1 条音频就是 13 个文件。把它们写成四条判据而不是一条「总和」，是因为
用户能采取的动作不同（图多要精简/拆段，视频音频多要减素材），报错要指得准。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from .asset_ledger import (
    KIND_AUDIO,
    KIND_LABELS,
    KIND_PICTURE,
    KIND_VIDEO,
    KINDS,
    AssetLedger,
    format_tag,
)

# 官方单次请求上限（spec #84「事实底账」→ minimax.io v2-create 文档）。改这里的任何
# 一个数都要同时改票面与 ADR：这组数字是外部契约，不是本仓的调参。
MAX_PICTURES = 9
MAX_VIDEOS = 3
MAX_AUDIOS = 3
MAX_FILES = 12

_KIND_CAPS = {
    KIND_PICTURE: MAX_PICTURES,
    KIND_VIDEO: MAX_VIDEOS,
    KIND_AUDIO: MAX_AUDIOS,
}


class ReferenceCapacityError(ValueError):
    """单次请求的参考素材超出官方上限（issue #86 AC3）。

    单独一个类型是为了让调用方把它与「数据坏了」的 ValueError 分开：这条是**容量
    边界**（素材太多，得精简或拆段），不是台账/提示词格式错误。
    """


@dataclass(frozen=True)
class ReferenceItem:
    """一个要被接线的参考素材：类型 + 官方编号 + 语义名。

    形状故意比 ``AssetRecord`` 窄：台账条目带路径与 SHA256（本模块一个都不用），而
    **校验的另一路来源只有编号**——``ref_meta`` 是「剧本声明了哪些 Picture」，不是
    台账记录。收 ``AssetRecord`` 就得为这一路伪造 path/sha256，那是在数据里说谎。
    ``name`` 可以为空（旧台账/纯编号来源），报错时只给标签——绝不编一个名字出来。
    """

    kind: str
    number: int
    name: str = ""

    def label(self) -> str:
        """报错里指代这一条的说法：`<Picture 3>「橘猫」`（没名字就只给标签）。"""
        tag = format_tag(self.kind, self.number)
        return f"{tag}「{self.name}」" if self.name else tag


def items_from_ledger(ledger: AssetLedger) -> list[ReferenceItem]:
    """台账 → 校验条目。台账就是「这次要上传哪些文件」的唯一来源（issue #85 之后
    连路径都统一由它解析），所以容量校验也从它取，不另算一份。"""
    return [ReferenceItem(entry.kind, entry.number, entry.name)
            for entry in ledger.entries]


def check_film_capacity(ledger: AssetLedger, *, single_request: bool,
                        where: str = "") -> None:
    """整片产物口（注入卡）的容量闸，按「这一次请求包含哪些文件」判。

    ``single_request=True``（≤15 秒：整片就是一次请求）→ 图/视频/音频/总文件全判。

    ``single_request=False``（>15 秒：要拆段）→ **只判视频/音频**，图数留给每段判。
    这一支不是放水，而是这条规格的要点：24 张素材的片子，每段只喂 ≤9 张本来就合法
    （spec #84 D5 / ADR 0017 的真实运行），拿整片库存去判图数会把这种片子整片拦死，
    正好废掉「按段取用」。图数在 ``prepare_ref_segmentation``（逐段）与
    ``single_segment_result``（直出）两处按请求判。

    视频/音频在两种路线下都按整片判：它们**没有**段内子集（spec #84「本轮只做上限
    校验与报错」），每一次请求都会带上整片的全部视频与音频。
    """
    items = items_from_ledger(ledger)
    if not single_request:
        items = [item for item in items if item.kind != KIND_PICTURE]
    check_reference_capacity(items, where=where)


def check_reference_capacity(items: Iterable[ReferenceItem], *,
                             where: str = "") -> None:
    """数一遍并按官方上限判；超限抛 ``ReferenceCapacityError``（点名超出的素材）。

    ``where`` 是出处（整片注入卡 / ≤15 秒直出 / 某段接线卡），进报错首行——同一条
    上限在两个环节咬，用户需要知道卡在哪一处。空串则只报上限本身。
    """
    grouped: dict[str, list[ReferenceItem]] = {kind: [] for kind in KINDS}
    for item in items:
        try:
            grouped[item.kind].append(item)
        except KeyError:
            raise ValueError(
                f"参考素材类型无效：{item.kind!r}（只认 {', '.join(KINDS)}）") from None

    problems: list[str] = []
    # 三型各自的上限（顺序固定：图 → 视频 → 音频，报错读起来与卡面同序）
    for kind in (KIND_PICTURE, KIND_VIDEO, KIND_AUDIO):
        bucket = grouped[kind]
        cap = _KIND_CAPS[kind]
        if len(bucket) > cap:
            offenders = "、".join(item.label() for item in sorted(bucket,
                                                                  key=lambda i: i.number))
            problems.append(
                f"- {KIND_LABELS[kind]} {len(bucket)} 个 > {cap} 个：{offenders}")
    # 总文件预算：9 图 + 3 视频已经用满 12，独立音频没有位置（spec #84 Q7 的补充裁定）
    total = sum(len(bucket) for bucket in grouped.values())
    if total > MAX_FILES:
        breakdown = " + ".join(
            f"{KIND_LABELS[kind]} {len(grouped[kind])}"
            for kind in (KIND_PICTURE, KIND_VIDEO, KIND_AUDIO))
        problems.append(f"- 总文件 {total} 个 > {MAX_FILES} 个（{breakdown}）")
    if not problems:
        return

    prefix = f"{where}：" if where else ""
    raise ReferenceCapacityError(
        f"{prefix}参考素材超出官方单次请求上限"
        f"（≤{MAX_PICTURES} 图 / ≤{MAX_VIDEOS} 视频 / ≤{MAX_AUDIOS} 音频 / "
        f"总文件 ≤{MAX_FILES}）：\n"
        + "\n".join(problems)
        + "\n超限不截断、不自动丢弃（截掉的素材会在提示词里变成悬空引用）："
        "请精简素材，或把这一段再拆短后重跑。"
    )


__all__ = [
    "MAX_AUDIOS",
    "MAX_FILES",
    "MAX_PICTURES",
    "MAX_VIDEOS",
    "ReferenceCapacityError",
    "ReferenceItem",
    "check_film_capacity",
    "check_reference_capacity",
    "items_from_ledger",
]

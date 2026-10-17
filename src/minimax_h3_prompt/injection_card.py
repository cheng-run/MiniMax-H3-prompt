"""注入卡（injection card）与视频侧尺寸契约（issue #45）。

注入卡是**纯文本接线清单**：素材台账逐条映射到 59- 工作流参考槽（``ref_image_N``
等，编号按剧本出场顺序），并记录视频输出参数（比例档 + megapixels + 对齐倍数 →
最终 W×H）与登记工作流名；用户照卡进 ComfyUI 手动接线，系统不自动投喂。它是给
人照抄的对照清单，**不是可执行载荷**——槽位文件名只出现在卡上，绝不写进提示词
正文（提示词是喂模型的，模型不接线）。

视频侧尺寸贴合 59- 工作流节点契约（``JZL_HailuoH3VideoParamsPro``，
ComfyUI-JZL-MiniMax-H3/nodes_hailuo_video.py 实测）：

- **10 档预设比例**：标签＝节点 ``ASPECT_RATIOS`` 的 widget 值（可照抄进 widgets），
  **不支持任意比例/宽高直填**——"自定义"只指手输 megapixels/对齐倍数（2026-10-16
  裁定，票面 AC2）；
- **推导公式**＝官方 ResolutionSelector 复刻：``total = megapixels × 1024²``，
  ``scale = √(total / (w_ratio × h_ratio))``，宽高各 ``round(比率 × scale / 倍数)
  × 倍数``。对齐倍数默认 32（节点默认值）；megapixels 默认 0.4（登记工作流 59-
  的存档 widget 现值，2026-10-17 用户拍板）。
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

from .asset_ledger import (
    KIND_AUDIO,
    KIND_LABELS,
    KIND_PICTURE,
    KIND_VIDEO,
    AssetLedger,
    AssetRecord,
    format_tag,
    load_asset_ledger,
)
from .generation import REF_WORKFLOW_NAME

# 注入卡落盘文件名（生成目录根上，与 asset-ledger.json 同级）。
CARD_FILENAME = "injection-card.md"

# 三元组的默认值：比例档与 megapixels 取登记工作流 59- 的存档 widget 现值
# （9:16 / 0.4MP），对齐倍数取节点默认 32。用户不改时注入卡记录的就是这组数。
DEFAULT_RATIO = "9:16 (Portrait Widescreen)"
DEFAULT_MEGAPIXELS = 0.4
DEFAULT_MULTIPLE = 32

# megapixels/对齐倍数的节点契约边界（JZL_HailuoH3VideoParamsPro 的 INPUT_TYPES：
# megapixels min=0.1 max=16.0；multiple min=8 max=128，step=4 只是控件步长、
# 公式对区间内任意整数同义，故只校验区间）。
MEGAPIXELS_MIN, MEGAPIXELS_MAX = 0.1, 16.0
MULTIPLE_MIN, MULTIPLE_MAX = 8, 128


class VideoRatioPreset(NamedTuple):
    """视频输出比例档：``label`` 是节点 widget 值（照抄口径），``w_ratio/h_ratio``
    是比例的宽/高**份数**（不是像素——像素字段全仓叫 width/height，画布比例档
    RatioPreset 同款防撞名）。"""

    label: str
    w_ratio: int
    h_ratio: int


# 10 档预设比例：键序与标签都逐字取自节点 ``ASPECT_RATIOS``（AC2「10 档预设比例，
# 无任意比例」）。标签即 widget 值——显示与照抄只此一份，各拼一遍就会漂。
VIDEO_RATIO_PRESETS: tuple[VideoRatioPreset, ...] = (
    VideoRatioPreset("1:1 (Square)", 1, 1),
    VideoRatioPreset("2:3 (Portrait Photo)", 2, 3),
    VideoRatioPreset("3:2 (Photo)", 3, 2),
    VideoRatioPreset("3:4 (Portrait Standard)", 3, 4),
    VideoRatioPreset("4:5 (Portrait Tall)", 4, 5),
    VideoRatioPreset("4:3 (Standard)", 4, 3),
    VideoRatioPreset("5:4 (Landscape Tall)", 5, 4),
    VideoRatioPreset("9:16 (Portrait Widescreen)", 9, 16),
    VideoRatioPreset("16:9 (Widescreen)", 16, 9),
    VideoRatioPreset("21:9 (Ultrawide)", 21, 9),
)

_SHORTHAND_RE = re.compile(r"^(\d+)\s*:\s*(\d+)$")


def resolve_video_ratio(value: str) -> str:
    """比例档归一到节点 widget 标签：收全标签或 ``9:16`` 简写；档外**大声报**。

    报错里点名 10 档是给「改写重试」留的可赢信息（issue #37 的教训：只说规则
    不给词表，重试在信息上不可赢）。**无任意比例**：``5:6``、``480x864`` 都在这里死。
    """
    text = str(value).strip()
    for preset in VIDEO_RATIO_PRESETS:
        if text == preset.label:
            return preset.label
    m = _SHORTHAND_RE.match(text)
    if m:
        pair = (int(m.group(1)), int(m.group(2)))
        for preset in VIDEO_RATIO_PRESETS:
            if (preset.w_ratio, preset.h_ratio) == pair:
                return preset.label
    labels = "、".join(p.label for p in VIDEO_RATIO_PRESETS)
    raise ValueError(f"比例档无效：{value!r}（只认节点 10 档预设比例，无任意比例：{labels}）")


def derive_video_size(ratio: str, megapixels: float, multiple: int) -> tuple[int, int]:
    """「比例档 + megapixels + 对齐倍数 → 最终 W×H」——官方 ResolutionSelector 公式。

    与 59- 工作流节点 ``JZL_HailuoH3VideoParamsPro.process`` 逐行同口径：宽高各
    对齐倍数取整，故结果可对账（9:16 · 0.4MP · 32 → 480×864，工作流存档值）。
    """
    preset = _preset_by_label(ratio)
    total_pixels = megapixels * 1024 * 1024
    scale = math.sqrt(total_pixels / (preset.w_ratio * preset.h_ratio))
    width = round(preset.w_ratio * scale / multiple) * multiple
    height = round(preset.h_ratio * scale / multiple) * multiple
    return width, height


def _preset_by_label(label: str) -> VideoRatioPreset:
    for preset in VIDEO_RATIO_PRESETS:
        if preset.label == label:
            return preset
    raise ValueError(f"比例档无效：{label!r}")


@dataclass(frozen=True)
class VideoOutputParams:
    """视频输出参数三元组：比例档 + megapixels + 对齐倍数（节点 widgets 口径）。

    W×H **不是输入字段**——由 ``size`` 按节点公式推导（票面「不支持任意宽高直填」
    在类型面上就没有入口）。比例档经 ``resolve_video_ratio`` 归一，简写/全标签同门。
    """

    ratio: str = DEFAULT_RATIO
    megapixels: float = DEFAULT_MEGAPIXELS
    multiple: int = DEFAULT_MULTIPLE

    def __post_init__(self) -> None:
        object.__setattr__(self, "ratio", resolve_video_ratio(self.ratio))
        if isinstance(self.megapixels, bool) or not isinstance(self.megapixels, (int, float)):
            raise ValueError(f"megapixels 必须是数字：{self.megapixels!r}")
        megapixels = float(self.megapixels)
        if not (MEGAPIXELS_MIN <= megapixels <= MEGAPIXELS_MAX):
            raise ValueError(
                f"megapixels 越界：{self.megapixels!r}"
                f"（节点契约 {MEGAPIXELS_MIN}–{MEGAPIXELS_MAX}）")
        object.__setattr__(self, "megapixels", megapixels)
        if isinstance(self.multiple, bool) or not isinstance(self.multiple, int):
            raise ValueError(f"对齐倍数必须是整数：{self.multiple!r}")
        if not (MULTIPLE_MIN <= self.multiple <= MULTIPLE_MAX):
            raise ValueError(
                f"对齐倍数越界：{self.multiple!r}"
                f"（节点契约 {MULTIPLE_MIN}–{MULTIPLE_MAX}）")

    @property
    def size(self) -> tuple[int, int]:
        """最终 W×H（节点公式推导，可照抄进 width/height 旁的对账口径）。"""
        return derive_video_size(self.ratio, self.megapixels, self.multiple)


# 素材类型 → 工作流槽位前缀（0 起编号，节点端口名逐字）：9 图槽 + 3 视频槽 +
# 3 视频音频（随视频走，非独立素材）+ 3 音频槽。
_SLOT_PREFIX = {
    KIND_PICTURE: "ref_image_",
    KIND_VIDEO: "ref_video_",
    KIND_AUDIO: "ref_audio_",
}
# 各型槽位数是**物理端口数**（JZL_MiniMaxH3Ref2vaBus 的 BUS_KEYS 实测：图 0..8、
# 视频 0..2、音频 0..2）：超出就是造出不存在的端口名，照卡接线接进空气——编号
# 越界必须响（绝不静默），见 ``slot_name`` 与 ``render_injection_card``。
_SLOT_COUNTS = {KIND_PICTURE: 9, KIND_VIDEO: 3, KIND_AUDIO: 3}
# 视频音轨的配对槽位前缀（节点 ``ref_video_audio_N``）：它不是独立素材——台账无此
# 型，槽位随同号视频走，故不进 _SLOT_PREFIX（进了就多出一种「可登记类型」的读法）。
_SOUNDTRACK_PREFIX = "ref_video_audio_"


def slot_name(kind: str, number: int) -> str:
    """台账条目 → 工作流参考槽端口名（如 ``<Picture 1>`` → ``ref_image_0``）。

    编号 N 映射到同型第 N 个槽（0 起）：台账编号即剧本出场顺序（label continuity），
    按号落槽后模型的 ``<Picture i>`` 等标签与提示词天然对齐。**缺号有坑**——空槽会让
    模型标签前移错位，渲染层见到缺号要报（见 ``render_injection_card``）。
    **越界不造名**：编号超过该型物理端口数就抛错——照卡接线不能接进不存在的端口。
    """
    if kind not in _SLOT_PREFIX:
        raise ValueError(f"参考素材类型无效：{kind!r}（只认 {', '.join(_SLOT_PREFIX)}）")
    if isinstance(number, bool) or not isinstance(number, int) or number < 1:
        raise ValueError(f"官方编号必须是正整数：{number!r}")
    cap = _SLOT_COUNTS[kind]
    if number > cap:
        raise ValueError(
            f"参考槽编号越界：{kind} {number} 超出节点槽位数 {cap}"
            f"（端口只有 {_SLOT_PREFIX[kind]}0…{_SLOT_PREFIX[kind]}{cap - 1}）——无端口可接")
    return f"{_SLOT_PREFIX[kind]}{number - 1}"


def _missing_slots(entries: tuple[AssetRecord, ...]) -> list[str]:
    """各型「号段有洞」的槽位名（缺号＝模型标签错位的源头，要点名）。

    只扫到**已分配最大号**：尾部空置的端口是正常留白（不移位），号段中间的洞才
    让模型标签前移。越界编号不是「缺号」而是「无处接线」（渲染层另报），也绝不经
    ``slot_name`` 造出假端口名。型全空不算缺（没素材的槽位是正常空置）。
    """
    missing: list[str] = []
    for kind in (KIND_PICTURE, KIND_VIDEO, KIND_AUDIO):
        numbers = {e.number for e in entries if e.kind == kind}
        within = {n for n in numbers if n <= _SLOT_COUNTS[kind]}
        if not within:
            continue
        for number in range(1, max(within) + 1):
            if number not in within:
                missing.append(slot_name(kind, number))
    return missing


def render_injection_card(ledger: AssetLedger, params: VideoOutputParams) -> str:
    """注入卡全文（纯文本，AC1/AC2/AC3）：接线表 + 三元组 + 登记工作流名 + 附注。

    卡是给人照抄的对照清单，**不是可执行载荷**——槽位与正本文件名只住在这里，
    提示词正文一个都不许进（AC4）。缺号、槽位越界、视频音轨配对这三类「接线会
    错位/接空」的坑在附注里说破（绝不静默）。
    """
    width, height = params.size
    lines = [
        "# 注入卡（多参 ref2va 接线清单）",
        "",
        f"登记工作流：{REF_WORKFLOW_NAME}",
        "本卡是给人照抄的对照清单，不是可执行载荷：照卡进 ComfyUI 手动接线，"
        "系统不自动投喂。",
        "",
        "## 参考槽接线（编号按剧本出场顺序）",
        "",
        "| 槽位 | 素材 | 类型 | 官方标签 | 正本文件 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for entry in ledger.entries:
        if entry.number > _SLOT_COUNTS[entry.kind]:
            slot_cell = "（无对应槽位）"  # 越界编号不造假端口名（照卡接线不得接进空气）
        else:
            slot_cell = slot_name(entry.kind, entry.number)
        lines.append(
            f"| {slot_cell} | {entry.name} "
            f"| {KIND_LABELS[entry.kind]} | {format_tag(entry.kind, entry.number)} "
            f"| {entry.path} |")
    if not ledger.entries:
        lines.append("| （无登记素材） | | | | |")
    lines += [
        "",
        "## 视频输出参数（照抄进尺寸节点 widgets）",
        "",
        f"- 比例档：{params.ratio}",
        f"- megapixels：{params.megapixels:g}",
        f"- 对齐倍数：{params.multiple}",
        f"- 最终 W×H：{width} × {height}（由上面三元组按节点公式推导，可对账）",
        "",
        "## 附注",
    ]
    videos = [e for e in ledger.entries if e.kind == KIND_VIDEO]
    audios = [e for e in ledger.entries if e.kind == KIND_AUDIO]
    for entry in videos:
        if entry.number <= _SLOT_COUNTS[KIND_VIDEO]:
            # 配对槽与视频同号（节点 index-paired：ref_video_audio_N 属 ref_video_N）
            lines.append(
                f"- 素材视频「{entry.name}」的音轨接 {_SOUNDTRACK_PREFIX}{entry.number - 1}"
                "（视频加载器的 audio 输出；无音轨则空着）。")
    if any(e.number <= _SLOT_COUNTS[KIND_VIDEO] for e in videos) and audios:
        lines.append(
            "- 素材视频自带音轨时，该音轨在提示词里占一个 <Audio> 号（排在该 <Video> 之前）"
            "——独立音频的 <Audio> 编号可能与台账标签对不上，接线以本卡槽位为准。")
    overflow = [e for e in ledger.entries if e.number > _SLOT_COUNTS[e.kind]]
    for entry in overflow:
        cap = _SLOT_COUNTS[entry.kind]
        lines.append(
            f"- ⚠ 槽位越界：「{entry.name}」{format_tag(entry.kind, entry.number)} 编号 "
            f"{entry.number} 超出节点{KIND_LABELS[entry.kind]}槽位数 {cap}——工作流无对应"
            "端口，照卡接线接不到它；需精简素材（该型只吃 "
            f"{cap} 个）。")
    # 同号重复＝多行抢同一端口，只有一行接得上（台账编号未强制唯一时的防御性告警）
    claimed: dict[tuple[str, int], str] = {}
    for entry in ledger.entries:
        key = (entry.kind, entry.number)
        if key in claimed:
            port = (slot_name(*key) if entry.number <= _SLOT_COUNTS[entry.kind]
                    else "（无对应槽位）")
            lines.append(
                f"- ⚠ 同号重复：「{claimed[key]}」与「{entry.name}」同为 "
                f"{format_tag(entry.kind, entry.number)}（{port}）——一个端口只接一行，"
                "请核对台账编号后重交其一。")
        else:
            claimed[key] = entry.name
    missing = _missing_slots(ledger.entries)
    if missing:
        lines.append(
            f"- ⚠ 缺号：{', '.join(missing)} 无登记素材——空槽会让模型 <Picture N> 等标签"
            "编号前移错位，请补交该素材或接线后逐条核对提示词里的标签。")
    return "\n".join(lines) + "\n"


def write_injection_card(directory: str | Path, params: VideoOutputParams) -> tuple[Path, str]:
    """渲染并落盘注入卡（向导与结构化产物出口共用这一份，issue #45）。

    台账从生成目录读回（``asset-ledger.json``，没有就当空台账——映射区写「无登记
    素材」而参数区照出，视频输出参数本身可照抄）。返回 ``(路径, 全文)``：调用方
    打印全文，排版不再各拼一份。
    """
    base = Path(directory)
    text = render_injection_card(load_asset_ledger(base), params)
    path = base / CARD_FILENAME
    path.write_text(text, encoding="utf-8")
    return path, text

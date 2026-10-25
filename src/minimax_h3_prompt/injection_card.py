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

视频输出参数**不再单独提问**（issue #49，2026-10-18 裁定）：由画布自动推导
（``derive_video_output``）——比例档继承画布比例、megapixels 预算按画布像素面积
反推取档、对齐倍数取默认；brief 显式声明优先于推导；特殊需求在卡上手改，
写卡不覆盖手改（``write_injection_card``）。
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
    resolve_asset_path,
)
from .generation import image_canvas_size
from .workflow_config import WorkflowSettings, workflow_settings

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


# megapixels 反推取档的粒度（issue #49）：画布面积是连续值，取到 0.1 MP 档才成
# 「档」——当日手选 0.9MP 就是 1280×720（0.879MP）取 0.1 档的结果（票面金值）。
MEGAPIXELS_TIER = 0.1


class VideoOutputDerivation(NamedTuple):
    """画布 → 视频输出参数的推导结果（issue #49）。

    ``params`` 是节点 widgets 三元组；``notes`` 是推导过程的说破行（比例档外回退、
    面积夹档），进向导回显与注入卡附注——偏离画布直觉的选择绝不静默。逐项直取时为空。
    ``canvas`` 是推导源画布（回显「由画布 W×H 推导」的出处）；None ＝ 显式声明分支，
    未经推导。
    """

    params: VideoOutputParams
    notes: tuple[str, ...]
    canvas: tuple[int, int] | None = None


def derive_video_output(canvas_width: int, canvas_height: int) -> VideoOutputDerivation:
    """纯函数：画布 → 视频输出参数（issue #49，视频尺寸不再单独提问）。

    三条规则（票面）：比例档继承画布比例、megapixels 预算按画布像素面积反推取档、
    对齐倍数取默认。金值复现当日手选：画布 1280×720 → 16:9 · 0.9MP · 32 → 1280×736。

    - 比例档：画布约分后与 10 档**精确**对上即继承（自定义画布同待遇——继承看比例
      不看菜单）；档外/极端比例**回退最近档**（|宽/高 − 档宽/档高| 最小，同距取表序）
      ——视频节点 10 档没有「通用」可降，回退也得贴着画布形状走，特殊需求走卡上手改。
    - megapixels：面积（w×h/1024²）四舍五入到 0.1 档（半值向上，预算不缩），再夹到
      节点契约区间——极端画布反推值越界时夹档不静默（notes 说破）。
    - 对齐倍数：取默认 32（票面「取默认」，不随画布猜）。

    ``notes`` 只记**偏离画布直觉**的两处（比例档外回退、面积夹档）；逐项直取时为空。
    """
    if (isinstance(canvas_width, bool) or not isinstance(canvas_width, int)
            or isinstance(canvas_height, bool) or not isinstance(canvas_height, int)
            or canvas_width <= 0 or canvas_height <= 0):
        raise ValueError(f"画布尺寸必须为正整数：{canvas_width!r} × {canvas_height!r}")
    notes: list[str] = []
    reduced = _reduce_pair(canvas_width, canvas_height)
    # 精确对档**两边同约分**：21:9 档存的是节点 widget 值的原样份数 (21, 9)（十档里
    # 唯一没约简的一对），只约画布不约档的话，正 21:9 画布（约分成 7:3）会错走回退
    # 分支、打印假说破（issue #49 复审硬伤）。
    preset = next(
        (p for p in VIDEO_RATIO_PRESETS
         if _reduce_pair(p.w_ratio, p.h_ratio) == reduced), None)
    if preset is not None:
        ratio = preset.label
    else:
        # 回退最近档：绝对比例距离（min 对同距按表序取先——结果确定，不掷硬币）。
        target = canvas_width / canvas_height
        preset = min(
            VIDEO_RATIO_PRESETS,
            key=lambda p: abs(target - p.w_ratio / p.h_ratio))
        ratio = preset.label
        notes.append(
            f"画布比例 {reduced[0]}:{reduced[1]} 无对应比例档，回退最近档 {ratio}")
    # 面积反推取档：w×h/1024² MP 四舍五入到 MEGAPIXELS_TIER 档（半值向上，预算不缩）。
    # 落回档值 round 一次杀浮点噪声：乘 0.1 自带噪声（3*0.1 = 0.30000000000000004，
    # 与字面 0.3 不等，复审实测），归零后与手输/声明值才可逐字对账。
    area_mp = canvas_width * canvas_height / (1024 * 1024)
    tiers = math.floor(area_mp / MEGAPIXELS_TIER + 0.5)
    megapixels = round(tiers * MEGAPIXELS_TIER, 10)
    if megapixels < MEGAPIXELS_MIN:
        megapixels = MEGAPIXELS_MIN
        notes.append(f"画布面积 {area_mp:.4g} MP 反推低于节点下限，取档 {megapixels:g} MP")
    elif megapixels > MEGAPIXELS_MAX:
        megapixels = MEGAPIXELS_MAX
        notes.append(f"画布面积 {area_mp:.4g} MP 反推超出节点上限，取档 {megapixels:g} MP")
    return VideoOutputDerivation(
        VideoOutputParams(ratio=ratio, megapixels=megapixels), tuple(notes),
        (canvas_width, canvas_height))


def resolve_video_output(video_output: VideoOutputParams | None,
                         canvases: dict[str, tuple[int, int]] | None,
                         ) -> VideoOutputDerivation:
    """三元组定案（issue #49）：brief 显式声明（含续接已定案）优先于画布推导。

    两条消费路径（向导 ``_deliver_injection_card``、``topic_generation``）共用这一份
    「先声明后推导」的接线——各写一份就是本仓反复出现的「改一处漏一处」形态
    （进度落盘三处同款教训）。推导源取 **zimage 画布**：资产图生图提示词就用它出图
    （``image_prompt_variants``），视频跟自己的参考图对齐尺寸才有意义。返回值
    ``canvas`` 为空 ＝ 声明分支（未经推导）。
    """
    if video_output is not None:
        return VideoOutputDerivation(video_output, (), None)
    canvas = image_canvas_size("zimage", canvases)
    return derive_video_output(*canvas)


def _reduce_pair(w_ratio: int, h_ratio: int) -> tuple[int, int]:
    """比例份数对的约分核（画布与档共用一个口径——两边各约各的就是第二份来源）。"""
    divisor = math.gcd(w_ratio, h_ratio)
    return w_ratio // divisor, h_ratio // divisor


# 素材类型 → 工作流槽位前缀（0 起编号，节点端口名逐字）：图槽 + 视频槽 +
# 视频音频（随视频走，非独立素材）+ 音频槽。
_SLOT_PREFIX = {
    KIND_PICTURE: "ref_image_",
    KIND_VIDEO: "ref_video_",
    KIND_AUDIO: "ref_audio_",
}
# 各型槽位数**不再写死**（issue #65 / ADR 0017）：9 只是旧节点的 UI 上限，不是模型
# 能力。槽位数是工作流属性，从 ``config/workflow.yaml`` 读（缺省图 24 / 视频 3 /
# 音频 3）。超出仍是「造出不存在的端口名」——编号越界必须响（绝不静默），见
# ``slot_name`` 与 ``render_injection_card``。
# 视频音轨的配对槽位前缀（节点 ``ref_video_audio_N``）：它不是独立素材——台账无此
# 型，槽位随同号视频走，故不进 _SLOT_PREFIX（进了就多出一种「可登记类型」的读法）。
_SOUNDTRACK_PREFIX = "ref_video_audio_"


def slot_name(kind: str, number: int,
              settings: WorkflowSettings | None = None) -> str:
    """台账条目 → 工作流参考槽端口名（如 ``<Picture 1>`` → ``ref_image_0``）。

    编号 N 映射到同型第 N 个槽（0 起）：台账编号即剧本出场顺序（label continuity），
    按号落槽后模型的 ``<Picture i>`` 等标签与提示词天然对齐。**缺号有坑**——空槽会让
    模型标签前移错位，渲染层见到缺号要报（见 ``render_injection_card``）。
    **越界不造名**：编号超过该型端口数就抛错——照卡接线不能接进不存在的端口。
    ``settings`` 为空时读生效配置（测试换配置即可换边界，见 ``workflow_config``）。
    """
    if kind not in _SLOT_PREFIX:
        raise ValueError(f"参考素材类型无效：{kind!r}（只认 {', '.join(_SLOT_PREFIX)}）")
    if isinstance(number, bool) or not isinstance(number, int) or number < 1:
        raise ValueError(f"官方编号必须是正整数：{number!r}")
    active = settings or workflow_settings()
    cap = active.slot_count(kind)
    if number > cap:
        raise ValueError(
            f"参考槽编号越界：{kind} {number} 超出节点槽位数 {cap}"
            f"（端口只有 {_SLOT_PREFIX[kind]}0…{_SLOT_PREFIX[kind]}{cap - 1}）——无端口可接")
    return f"{_SLOT_PREFIX[kind]}{number - 1}"


def _missing_slots(entries: tuple[AssetRecord, ...],
                   settings: WorkflowSettings) -> list[str]:
    """各型「号段有洞」的槽位名（缺号＝模型标签错位的源头，要点名）。

    只扫到**已分配最大号**：尾部空置的端口是正常留白（不移位），号段中间的洞才
    让模型标签前移。越界编号不是「缺号」而是「无处接线」（渲染层另报），也绝不经
    ``slot_name`` 造出假端口名。型全空不算缺（没素材的槽位是正常空置）。
    """
    missing: list[str] = []
    for kind in (KIND_PICTURE, KIND_VIDEO, KIND_AUDIO):
        cap = settings.slot_count(kind)
        numbers = {e.number for e in entries if e.kind == kind}
        within = {n for n in numbers if n <= cap}
        if not within:
            continue
        for number in range(1, max(within) + 1):
            if number not in within:
                missing.append(slot_name(kind, number, settings))
    return missing


def render_injection_card(ledger: AssetLedger, params: VideoOutputParams,
                          notes: tuple[str, ...] = (),
                          settings: WorkflowSettings | None = None) -> str:
    """注入卡全文（纯文本，AC1/AC2/AC3）：接线表 + 三元组 + 登记工作流名 + 附注。

    卡是给人照抄的对照清单，**不是可执行载荷**——槽位与正本文件名只住在这里，
    提示词正文一个都不许进（AC4）。缺号、槽位越界、视频音轨配对这三类「接线会
    错位/接空」的坑在附注里说破（绝不静默）。``notes`` 是画布推导的说破行（issue #49
    比例回退/面积夹档），跟在三元组块下——推导替用户做的非常规选择要留底。
    ``settings`` 为空时读生效的工作流配置（登记名与槽位数同出一源，issue #65）。
    """
    active = settings or workflow_settings()
    width, height = params.size
    lines = [
        "# 注入卡（多参 ref2va 接线清单）",
        "",
        f"登记工作流：{active.ref_workflow_name}",
        "本卡是给人照抄的对照清单，不是可执行载荷：照卡进 ComfyUI 手动接线，"
        "系统不自动投喂。",
        "",
        "## 参考槽接线（编号按剧本出场顺序）",
        "",
        "| 槽位 | 素材 | 类型 | 官方标签 | 正本文件 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for entry in ledger.entries:
        if entry.number > active.slot_count(entry.kind):
            slot_cell = "（无对应槽位）"  # 越界编号不造假端口名（照卡接线不得接进空气）
        else:
            slot_cell = slot_name(entry.kind, entry.number, active)
        # 正本位置走统一解析口（issue #85）：新台账给库内绝对路径，用户照卡找得到文件；
        # 旧台账没有库根，解析口原样返回相对路径（旧卡面一字不变）
        lines.append(
            f"| {slot_cell} | {entry.name} "
            f"| {KIND_LABELS[entry.kind]} | {format_tag(entry.kind, entry.number)} "
            f"| {resolve_asset_path(ledger, entry)} |")
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
    ]
    for note in notes:
        lines.append(f"- 推导注记：{note}（要改就在本卡手改三元组，不被系统回写覆盖）")
    lines += [
        "",
        "## 附注",
    ]
    videos = [e for e in ledger.entries if e.kind == KIND_VIDEO]
    audios = [e for e in ledger.entries if e.kind == KIND_AUDIO]
    video_cap = active.slot_count(KIND_VIDEO)
    for entry in videos:
        if entry.number <= video_cap:
            # 配对槽与视频同号（节点 index-paired：ref_video_audio_N 属 ref_video_N）
            lines.append(
                f"- 素材视频「{entry.name}」的音轨接 {_SOUNDTRACK_PREFIX}{entry.number - 1}"
                "（视频加载器的 audio 输出；无音轨则空着）。")
    if any(e.number <= video_cap for e in videos) and audios:
        lines.append(
            "- 素材视频自带音轨时，该音轨在提示词里占一个 <Audio> 号（排在该 <Video> 之前）"
            "——独立音频的 <Audio> 编号可能与台账标签对不上，接线以本卡槽位为准。")
    overflow = [e for e in ledger.entries if e.number > active.slot_count(e.kind)]
    for entry in overflow:
        cap = active.slot_count(entry.kind)
        lines.append(
            f"- ⚠ 槽位越界：「{entry.name}」{format_tag(entry.kind, entry.number)} 编号 "
            f"{entry.number} 超出节点{KIND_LABELS[entry.kind]}槽位数 {cap}——工作流无对应"
            "端口，照卡接线接不到它；可精简素材，或去扩工作流端口"
            "（新建一个该型端口更多的工作流，再把槽位数写进 config/workflow.yaml）。")
    # 同号重复＝多行抢同一端口，只有一行接得上（台账编号未强制唯一时的防御性告警）
    claimed: dict[tuple[str, int], str] = {}
    for entry in ledger.entries:
        key = (entry.kind, entry.number)
        if key in claimed:
            port = (slot_name(*key, active) if entry.number <= active.slot_count(entry.kind)
                    else "（无对应槽位）")
            lines.append(
                f"- ⚠ 同号重复：「{claimed[key]}」与「{entry.name}」同为 "
                f"{format_tag(entry.kind, entry.number)}（{port}）——一个端口只接一行，"
                "请核对台账编号后重交其一。")
        else:
            claimed[key] = entry.name
    missing = _missing_slots(ledger.entries, active)
    if missing:
        lines.append(
            f"- ⚠ 缺号：{', '.join(missing)} 无登记素材——空槽会让模型 <Picture N> 等标签"
            "编号前移错位，请补交该素材或接线后逐条核对提示词里的标签。")
    return "\n".join(lines) + "\n"


class CardWrite(NamedTuple):
    """``write_injection_card`` 的结果：``written=False`` ＝ 卡已存在（可能含手改），
    保留原卡原文——``text`` 永远是**盘上**那份（不是将写未写的渲染稿）。"""

    path: Path
    text: str
    written: bool


def write_injection_card(directory: str | Path, params: VideoOutputParams,
                         notes: tuple[str, ...] = (),
                         settings: WorkflowSettings | None = None) -> CardWrite:
    """渲染并落盘注入卡（向导与结构化产物出口共用这一份，issue #45）。

    台账从生成目录读回（``asset-ledger.json``，没有就当空台账——映射区写「无登记
    素材」而参数区照出，视频输出参数本身可照抄）。``notes`` 是推导说破行，进参数
    块附注（issue #49）；``settings`` 透传给渲染层（issue #65 的槽位口径）。

    **已存在不覆盖**（issue #49 AC4）：卡是手改出口，用户改完系统再渲染回写＝把手改
    抹掉。要重出新卡，用户删除 ``injection-card.md`` 后重跑——删除是显式动作，与
    「不许静默覆盖」同口径。
    """
    base = Path(directory)
    path = base / CARD_FILENAME
    if path.exists():
        return CardWrite(path, path.read_text(encoding="utf-8"), False)
    text = render_injection_card(load_asset_ledger(base), params, notes=notes,
                                 settings=settings)
    path.write_text(text, encoding="utf-8")
    return CardWrite(path, text, True)

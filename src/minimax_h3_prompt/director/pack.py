"""导演包构建与装配校验（issue #104）。

导演包是**给插件吃的数据**，不是我们的中间产物：插件导入时只按字节读它，错了不会
报错，只会「导入成功但图没了」「时间线长度对不上」「mixed 段悄悄变 t2v」。所以本模块
的纪律是**严格失败优先于静默回落**（设计 §1 第 2 条）：八条装配约束全部在**写盘之前**
校验，违规即 ``ValueError`` 并点名字段。

三份口径的住处（设计 §1 第 3 条「一个契约只住一处」）：

- 帧换算只住 ``frames.py``——本模块调 ``duration_sec_for_declared``，不另写换算；
- 包字段与插件常量只住这里——ASCII 正则、前缀、槽位上限、``mixed`` 合法值集合都由
  本模块声明，别的模块从这里取；
- 段提示词文件名只住 ``prompts.segment_path``。

**两套坐标**（设计 §3.2，最容易写错的一处）：``plan.json`` 与导出帧是**成片坐标**
（Σ 导出帧，含「保完整」的 12×有引导段数）；导演台的 ``timeline.totalFrames`` 与
``segments[].start/length/frameCount`` 是**声明坐标**（Σ 声明帧）。包必须写后者
（``gen_timeline.py:102-123`` 按 ``frameCount`` 顺序累加、忽略 ``start``），而用户关心
的成片长度是前者——出站清单因此**必须同时给两个数**。

插件侧依据全部来自 ``docs/research/2026-10-29-director-插件原生格式.md``，行号基准是
插件 HEAD ``4c6245e``；插件升级后本文件的常量与行号都要重核（``PACK_FORMAT_VERSION``
现在是 1）。
"""
from __future__ import annotations

import json
import re
import shutil
import string
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

from ..asset_ledger import KIND_PICTURE, load_asset_ledger, resolve_asset_path
from ..ref_capacity import MAX_PICTURES
from .frames import (
    FPS,
    PLAN_MODES,
    SEGMENT_TYPE_LABELS,
    DirectorPlan,
    duration_sec_for_declared,
    validate_segment_types,
)
from .planning import DIRECTOR_DIRNAME, render_give_images
from .prompts import segment_path

# ---------------------------------------------------------------------------
# 插件契约常量（ComfyUI_MiniMaxH3_Director/director/pack.py，HEAD 4c6245e）
# ---------------------------------------------------------------------------

# ``pack.py:32-33``
PACK_FORMAT = "minimax-h3-director-pack"
PACK_FORMAT_VERSION = 1

# ``pack.py:34``：只有这四个前缀里的媒体值会在**导入时**被改写成「相对 ComfyUI input」
# 的路径（``:899-924``）。写别的前缀等于把图留在包里、运行时找不到（会被静默清空）。
PACK_PREFIXES = ("shared_params/", "asset_groups/", "source_video/", "extra/")

# ``pack.py:35`` 的正则是 ``^[A-Za-z0-9_./]+$``。这里**按字符集声明再派生正则**，
# 而不是把字面抄第二遍：允许字符集一改，派生的正则跟着改，调用点只有一处（本仓
# 「守卫形态要从常量派生」的老教训——字面重抄在常量变化时会静默失效）。
ASCII_PATH_ALLOWED = string.ascii_letters + string.digits + "_./"
_PACK_PATH_RE = re.compile("^[" + re.escape(ASCII_PATH_ALLOWED) + "]+$")

# ``pack.py:44-46``：媒体扩展名（.jpeg→.jpg 等归一不在本票范围内，见 ``_media_ext``）。
IMAGE_EXTS = frozenset({".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".tif", ".tiff"})

# 插件槽位文件名正则（``pack.py:37-41``）：``Picture1-9`` / ``start`` / ``end``。
# 参考图容量沿用 ``ref_capacity.MAX_PICTURES``（=9，官方单次请求上限，spec #84），
# **不在这里另写一个 9**：两个 9 一旦分家，容量守卫就会在一边静默放行。
PICTURE_SLOT_LIMIT = MAX_PICTURES

# 画布必须是 32 的倍数（``lib/image_prep.py:23-33`` 的 ``assert_minimax_canvas``）。
# 那个断言在**执行时**才炸；打包时就拦下，省得用户点运行才发现。
CANVAS_ALIGN = 32

# 官方节点只认 match / max（``plan.py:101-107``）。``"1024"`` 这类导演台内部档在
# 官方节点上会被静默归一成 match——参数看着写了、实际没生效，正是要拦的形态。
REF_IMAGE_SIZES = ("match", "max")

# 段间引导的两个档（``nodes/director_common.py`` / 研究笔记 §6）：guide = 官方基线，
# 本路线默认取它；continue 只给「续写」意图的用户。
CONTINUITY_MODES = ("guide", "continue")
CONTINUITY_DEFAULT = "guide"

# 「保完整」的重叠帧数（插件默认 22，``segment_continuity.py:130-228``）。
CONTINUITY_OVERLAP_FRAMES = 22

# 输出块必填项（设计 §3.4 约束 7）。**字面契约**：值是插件认得的那几个，不是
# 「我们写什么插件就用什么」——写成 {"continuityEnabled": False} 也照样能导入，
# 只是整条片子失去段间引导。
REQUIRED_OUTPUT = {    "continuityEnabled": True,
    "continuityOverlapFrames": CONTINUITY_OVERLAP_FRAMES,
    "continuityKeepTail": True,
    "exportMode": "all",
}

# ``mixed`` 段内合法任务类型（``gen_timeline.py:27`` 的 ``MIXED_SEGMENT_KEYS``）。
# 非法值/空值插件会**静默回落 t2v**（``:156-171``）——这条校验就是要抢在它前面。
# 集合直接由 ``frames.PLAN_MODES`` 派生：一期四个模式与 mixed 段合法值本来就是同一组，
# 分头写两张表迟早一边漏改。
MIXED_SEGMENT_TASKS = frozenset(PLAN_MODES)

# 公共参考的总开关契约值（设计 §3.4 约束 2）。写成常量是为了让「打开它」成为一次
# **可见的**改动：两处写入都取它，校验器按字面 False 判——谁把它改成 True，打包当场
# 报错而不是产出一份「导入成功、槽号全错」的包。
COMMON_ENABLED = False

# 生成类时间线的顶层形态（``gen_timeline.py:37-49``）：``timelineMode`` 不写会让插件
# 按任务类型猜，猜错就走成别的类别（v2v 会因缺源视频报错）。生成类恒 prompt_batch，
# fl2v 有自己的剥离 UI，写 ``fl2v``。
TIMELINE_MODE_PROMPT_BATCH = "prompt_batch"
TIMELINE_MODE_FL2V = "fl2v"
EDIT_MODE_SEGMENT = "segment"
TIMELINE_VERSION = 4  # UI 默认值（``pack.py:849`` 组装时写 5，初始包用 4）

OUTPUT_MODE_FIXED = "fixed"  # 生成类必须 fixed（研究笔记硬约束 6）
AUDIO_MODE_GENERATE = "generate"

# 导演包在会话里的落点（设计 §3.4）：解包目录 + 直接导入的 zip。
PACK_DIRNAME = "pack"
PACK_ZIP_NAME = "initial.mmxpack.zip"
# 包内时间线的文件名。**读口与写口共用这一个常量**：插件按包根 ``timeline.json``
# 读取（插件 ``pack.py:1033``），#108 的最终包比对也从这里取——两处各写一份字面量，
# 改一处漏一处时不会有人报错，只会「包读出来了但段数对不上」。
TIMELINE_FILENAME = "timeline.json"

# ``pack.json.widgets``（``pack.py:589-599``）：插件只搬这 8 个键。取值全部是
# 导演台节点的控件默认值（``nodes/director.py:144-180``：steps 25 / res_multistep /
# simple / cfg 1.0 / shift_video 12.0 / shift_audio 3.0 / seed 0），用户导入后可在
# UI 里改——包不该替用户猜「这次跑几步」。
PACK_WIDGETS = {
    "steps": 25,
    "sampler": "res_multistep",
    "scheduler": "simple",
    "cfg": 1.0,
    "shift_video": 12.0,
    "shift_audio": 3.0,
    "seed": 0,
}

# 段目录里的素材落点（设计 §3.4.1 第 2 条）：``GENxxx/director/assets/NN/start.png``。
ASSETS_DIRNAME = "assets"
START_STEM = "start"
END_STEM = "end"
# 允许的素材扩展名按偏好顺序取第一个存在的：先 png（用户手里最常见的无损格式），
# 其余常见格式兜底。**多张并存取第一张**并出站点名——不静默挑一张不同内容的图。
_ASSET_EXTS = (".png", ".jpg", ".jpeg", ".webp")

# fl2v 段的默认负面词（``fl2v_timeline.py:32`` ``DEFAULT_FL2V_NEGATIVE``）：UI 在
# 段缺负面词时也用它，包里写同一个值可以避免「包里空、UI 里又变回来」的假差异。
FL2V_DEFAULT_NEGATIVE = "bad video"

_SLOT_TOKEN_RE = re.compile(r"<Picture\s+(\d+)>")


# ---------------------------------------------------------------------------
# 数据类型
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DirectorPackSettings:
    """包内固定参数：画布与参考图尺寸。**从配置读**，模块里不给默认值（设计 §4）。

    四个字段都必填：模块内留默认值就是「尺寸写死在代码里」的第三种来源，
    ``Config.director_settings`` 才是唯一的配置读口（YAML 的 ``director:`` 节）。
    显式对象的另一个理由是 ``prepare_director_pack`` 要能在临时目录里被测试——
    配置读取只发生在向导那一层。
    """

    width: int
    height: int
    ref_max_size: int
    ref_image_size: str


@dataclass(frozen=True)
class PackedMedia:
    """一个要进包的媒体：源文件 + 包内相对路径（带 ``PACK_PREFIX`` 前缀）。"""

    source: Path
    pack_rel: str


@dataclass(frozen=True)
class SegmentMedia:
    """一段进包的媒体（按用途分槽；``r2v`` 用 refs，``i2v``/``fl2v`` 用首帧/尾帧）。"""

    refs: tuple[PackedMedia, ...] = ()
    start: PackedMedia | None = None
    end: PackedMedia | None = None


@dataclass(frozen=True)
class PackedSegment:
    """一段打包结果：进包的提示词、帧数与媒体（出站清单按段展示它）。

    媒体按**用途**分三个槽（参考图 / 首帧 / 尾帧），不是混在一个列表里：写包时三者的
    字段形态不同（``refs[]`` / ``genImage`` / ``endImage``），靠文件名猜用途会在
    将来加一种媒体时静默错位。
    """

    index: int
    type: str
    task_type: str
    guided: bool
    declared_frames: int
    duration_sec: float
    prompt: str
    refs: tuple[PackedMedia, ...] = ()
    start_image: PackedMedia | None = None
    end_image: PackedMedia | None = None

    @property
    def label(self) -> str:
        return SEGMENT_TYPE_LABELS[self.type]

    @property
    def media(self) -> tuple[PackedMedia, ...]:
        return tuple(item for item in (self.refs + (self.start_image, self.end_image))
                     if item is not None)


@dataclass(frozen=True)
class DirectorPackJob:
    """可断点续做的打包任务（设计 §3.4 准备口形状）。

    只装**重建包所需的输入与落点**：段表（含成片坐标）、参数、包路径、逐段媒体。
    #105 的陪跑要拿的是段号与缓存，不是这里的字段——别把下游状态塞进 job。
    """

    plan: DirectorPlan
    settings: DirectorPackSettings
    pack_dir: Path
    zip_path: Path
    segments: tuple[PackedSegment, ...]

    @property
    def timeline_total_frames(self) -> int:
        """时间线总帧 = Σ 声明帧（导演台里显示的长度）。"""
        return sum(segment.declared_frames for segment in self.segments)

    @property
    def film_total_frames(self) -> int:
        """成片预计帧 = Σ 导出帧（每段按类型多出「保完整」的 12 帧）。"""
        return self.plan.total_frames


# ---------------------------------------------------------------------------
# 装配约束（八条，逐条拒绝；违反即 ValueError，写盘之前调用）
# ---------------------------------------------------------------------------

def is_ascii_pack_path(rel: str) -> bool:
    """包内相对路径是否合法：ASCII 白名单、不以 ``/`` 开头、无 ``..`` 段。

    与插件 ``_is_ascii_pack_path``（``pack.py:151-155``）同口径：反斜杠先归一成
    斜杠再判。返回 False 的路径**写进 zip 也导不进**（``:673``）。
    """
    text = str(rel or "").replace("\\", "/").strip()
    if not text or text.startswith("/") or ".." in text.split("/"):
        return False
    return bool(_PACK_PATH_RE.fullmatch(text))


def validate_pack_paths(paths: Iterable[str]) -> None:
    """约束 1：包内路径一律 ASCII（中文语义名留在提示词与账本，包内用 ASCII 名）。

    为什么必须自己拦：导入侧对非 ASCII 条目**直接拒包**（``pack.py:673``），而报错
    只有一句英文文件名——用户看不出是哪张素材。这里先点名。
    """
    bad = [path for path in paths if not is_ascii_pack_path(path)]
    if bad:
        raise ValueError(
            "导演包里出现非 ASCII 路径（插件导入会直接拒包）："
            + "、".join(sorted(bad)[:5])
            + ("…" if len(bad) > 5 else "")
            + "——包内媒体名必须是 ASCII（Picture1.png / start.png 这类），"
              "中文语义名只留在提示词与素材台账里")


def validate_common_disabled(shared_params: dict, timeline: dict) -> None:
    """约束 2：``commonEnabled`` 必须是 ``false``，两处都要。

    为真时插件把全局词以 ``{common}\\n\\n{segment}`` 拼到每段前（``plan.py:202-212``），
    还把全局参考图按槽位并进段内（``merge_indexed_refs``）——段词里的 ``<Picture N>``
    与挂图就错位了。这是**契约值**，不是「常量写什么就认什么」：两处都按字面 False 判。
    """
    if shared_params.get("commonEnabled") is not False:
        raise ValueError(
            f"shared_params.json 的 commonEnabled 必须是 false，实际 "
            f"{shared_params.get('commonEnabled')!r}：为真时插件会把全局词拼进每段词、"
            "把全局参考图并进段内，段内槽号与 <Picture N> 对不上")
    if timeline.get("global", {}).get("commonEnabled") is not False:
        raise ValueError(
            f"timeline.global 的 commonEnabled 必须是 false，实际 "
            f"{timeline.get('global', {}).get('commonEnabled')!r}："
            "公共参考打开后参考编号会顺延，与段内挂图对不上")


def validate_canvas(width: int, height: int) -> None:
    """约束 3：画布宽高都是 32 的倍数（``lib/image_prep.py:23-33`` 的断言）。

    后端的 ``assert_minimax_canvas`` 只在**点运行**时才炸；那时用户已经导入、配好了
    段，返工代价最大。打包前拦下。
    """
    for name, value in (("宽", width), ("高", height)):
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f"画布{name}必须是正整数，实际 {value!r}")
        if value % CANVAS_ALIGN:
            raise ValueError(
                f"画布{name} {value} 不是 {CANVAS_ALIGN} 的倍数：插件后端会拒绝执行"
                f"（MiniMax H3 画布按 {CANVAS_ALIGN} 对齐）")


def validate_ref_image_size(value: str) -> None:
    """约束 4：``refImageSize`` 只允许 ``match`` / ``max``（官方节点口径）。

    其它值（含导演台内部档 ``"1024"``）在官方节点上会被静默归一成 ``match``：
    用户以为压到了 1024，实际按原图送——属于「参数写了没生效」。
    """
    if value not in REF_IMAGE_SIZES:
        raise ValueError(
            f"refImageSize 只允许 {' / '.join(REF_IMAGE_SIZES)}，实际 {value!r}："
            "其它档位在官方 MiniMaxH3ReferenceToVideo 上会被静默归一成 match")


def validate_reference_capacity(segments: Sequence[dict]) -> None:
    """约束 5：每段参考图 ≤ 9 张，且 ``refs[].index`` 从 0 连续。

    两条判据必须一起：插件把 ``refs[].index`` 直接当槽号传给官方节点的
    ``reference_image_{index}``（``plan.py:1073-1074``、``lib/ref_images.py:44-46``）。
    超 9 张插件读不到第 10 个槽；跳号则 ``<Picture N>`` 与实际图错位——两种都是
    「能导入、跑出来是别的图」。
    """
    for segment in segments:
        refs = segment.get("refs") or []
        number = segment.get("id") or "?"
        if len(refs) > PICTURE_SLOT_LIMIT:
            raise ValueError(
                f"段 {number} 挂了 {len(refs)} 张参考图，超过插件槽位上限 "
                f"{PICTURE_SLOT_LIMIT}（Picture1–{PICTURE_SLOT_LIMIT}）："
                "超出的图插件读不到，段词里的 <Picture N> 会悬空")
        indexes = [ref.get("index") for ref in refs]
        if indexes != list(range(len(indexes))):
            raise ValueError(
                f"段 {number} 的 refs[].index 不是从 0 连续：{indexes!r}——"
                "插件把 index 直接当槽号（reference_image_{index}），跳号会让 "
                "<Picture N> 与实际挂图错位")


def validate_mixed_fl2v_fields(segments: Sequence[dict], *, task_type: str) -> None:
    """约束 6：``mixed`` 里的 ``fl2v`` 段用首尾帧字段，**不是** ``refs`` 数组。

    只在 ``mixed`` 时间线上生效（独立 fl2v 走的是自己的时间线形态）。插件对 fl2v 段
    用 ``_fl2v_image_raw`` 读 ``startImage``/``endImage``（``gen_timeline.py:177-203``），
    ``refs`` 会被 ``segment_refs_for_context`` 剥掉——写进 refs 等于图静默消失。
    """
    if task_type != "mixed":
        return
    for segment in segments:
        if str(segment.get("taskType") or "") != "fl2v":
            continue
        number = segment.get("id") or "?"
        if segment.get("refs"):
            raise ValueError(
                f"段 {number} 是 mixed 下的 fl2v，却把图挂在 refs 数组里："
                "fl2v 段只认 startImage/endImage（插件会把 refs 剥掉，图静默消失）")
        start = segment.get("startImage") or segment.get("genImage") or {}
        end = segment.get("endImage") or {}
        if not (isinstance(start, dict) and start.get("imageFile")) \
                and not (isinstance(end, dict) and end.get("imageFile")):
            raise ValueError(
                f"段 {number} 是 mixed 下的 fl2v，但首尾帧字段里一张图都没有："
                "至少要有 startImage（或 genImage）或 endImage")


def validate_output_config(output: dict) -> None:
    """约束 7：``output`` 必含五条引导/导出配置，生成类还要 ``mode="fixed"``。

    写成别处不算数：UI 导出载荷会删掉 timeline 根级的同名键
    （``web/js/minimax_timeline.js:271-277``），最终包里就没了。
    """
    missing = {
        key: value for key, value in REQUIRED_OUTPUT.items()
        if output.get(key) != value
    }
    if missing:
        detail = "、".join(
            f"{key} 应为 {value!r}（实际 {output.get(key)!r}）"
            for key, value in missing.items())
        raise ValueError(
            f"timeline.output 缺必备配置或取值不符：{detail}——"
            "引导/导出开关只能挂在 output 下，写根级会被 UI 的导出清洗删掉")
    if not str(output.get("continuityMode") or "").strip():
        raise ValueError(
            "timeline.output 缺 continuityMode（guide / continue）："
            "它是段间引导的模式开关，缺了插件按默认猜")
    if output.get("mode") != OUTPUT_MODE_FIXED:
        raise ValueError(
            f"生成类时间线的 output.mode 必须是 {OUTPUT_MODE_FIXED!r}，"
            f"实际 {output.get('mode')!r}（long_edge 只对编辑型/源视频成立）")


def validate_mixed_segment_tasks(segments: Sequence[dict], *, task_type: str) -> None:
    """约束 8：``mixed`` 段类型只允许 t2v/i2v/fl2v/r2v；空值/其它值报错。

    插件 ``_resolve_segment_task`` 对非法值**静默回落 t2v**（``gen_timeline.py:156-171``）：
    用户选了 i2v 的段会变成纯文生视频，且没有任何日志。项目侧必须抢在它前面。
    """
    if task_type != "mixed":
        return
    for segment in segments:
        number = segment.get("id") or "?"
        value = segment.get("taskType")
        if value not in MIXED_SEGMENT_TASKS:
            raise ValueError(
                f"段 {number} 的任务类型 {value!r} 不是 mixed 的合法值"
                f"（{' / '.join(sorted(MIXED_SEGMENT_TASKS))}）：插件会静默把它改成 t2v，"
                "生成方式与给图清单都会错")


def validate_segment_media_fields(segments: Sequence[dict]) -> None:
    """同一段不许给出内容不同的 ``genImage`` 与 ``startImage``。

    导出时两者都落到 ``asset_groups/NN/start.<ext>``，``_copy_file`` 命中已存在就
    跳过（``pack.py:237-242``、``:454-467``）——第二张图**静默消失**。i2v 用
    ``genImage``+``imageFile``；fl2v 用 ``genImage``（首帧）+ ``endImage``。
    """
    for segment in segments:
        gen = segment.get("genImage") or {}
        start = segment.get("startImage") or {}
        if not isinstance(gen, dict) or not isinstance(start, dict):
            continue
        gen_file = str(gen.get("imageFile") or "")
        start_file = str(start.get("imageFile") or "")
        if gen_file and start_file and gen_file != start_file:
            raise ValueError(
                f"段 {segment.get('id') or '?'} 的 genImage 与 startImage 指向不同文件"
                f"（{gen_file} ≠ {start_file}）：导出时两者都落 start.<ext>，"
                "后一张会被静默跳过（图静默消失）")


# ---------------------------------------------------------------------------
# 构建
# ---------------------------------------------------------------------------

def pack_dir_for(session_dir: str | Path) -> Path:
    """解包目录：``GENxxx/director/pack/``（设计 §3.4）。"""
    return Path(session_dir) / DIRECTOR_DIRNAME / PACK_DIRNAME


def zip_path_for(session_dir: str | Path) -> Path:
    """直接导入的包：``GENxxx/director/initial.mmxpack.zip``。"""
    return Path(session_dir) / DIRECTOR_DIRNAME / PACK_ZIP_NAME


def _require_pack_mode(mode: str) -> str:
    """一期只打 t2v / i2v / fl2v / r2v 的包；二期模式**明确报错**（票面硬条）。"""
    if mode in PLAN_MODES:
        return mode
    raise ValueError(
        f"导演台模式 {mode!r} 一期不打导演包（v2v/rv2v 见 #109、mixed 见 #110）；"
        f"一期只支持 {'/'.join(PLAN_MODES)}")


def validate_continuity_mode(value: str) -> str:
    """校验引导模式并原样返回；坏值报错（会话读回与打包共用这一个口）。

    它的取值就是插件的 ``continuityMode``，写错等于让插件按默认猜；所以读取口
    （``session_store``）与打包口（``prepare_director_pack``）都调这一处，别各写一份。
    """
    if value not in CONTINUITY_MODES:
        raise ValueError(
            f"段间引导模式 {value!r} 无效（应为 {' / '.join(CONTINUITY_MODES)}）："
            "它的取值就是插件的 continuityMode，写错等于让插件猜")
    return value


def _read_segment_prompts(session_dir: Path, plan: DirectorPlan) -> dict[int, str]:
    """逐段读盘上的段提示词；缺文件/空文件**点名报错**（空词会被插件回落全局词）。"""
    texts: dict[int, str] = {}
    for segment in plan.segments:
        path = segment_path(session_dir, segment.index)
        if not path.is_file():
            raise ValueError(
                f"第 {segment.index} 段缺提示词文件 {path}：先跑完写段（#103）再打包，"
                "半份段表打出来的包会缺段内容")
        text = path.read_text(encoding="utf-8").strip()
        if not text:
            raise ValueError(
                f"第 {segment.index} 段提示词文件是空的（{path}）："
                "插件会回落全局提示词，本段内容就此丢失")
        texts[segment.index] = text
    return texts


def _picture_sources(session_dir: Path) -> tuple[tuple[Path, str], ...]:
    """r2v 的参考图正本：台账已登记的图片，按台账顺序（= 官方编号顺序）。

    只走**一个解析口** ``resolve_asset_path``（issue #85 复审：库根/旧布局的分支只住
    那里）。缺正本、类型不认、超过槽位上限全部**点名报错**——静默截断会留下悬空的
    ``<Picture N>``，自动丢图则是替用户删了素材（spec #84）。
    """
    ledger = load_asset_ledger(session_dir)
    pictures = [entry for entry in ledger.entries if entry.kind == KIND_PICTURE]
    if not pictures:
        raise ValueError(
            "r2v 需要素材台账里已登记的图片：台账是空的，段词里的 <Picture N> 会全部"
            "悬空（插件不会报错，只会按槽位顺序补前缀）。先把素材入库再打包")
    if len(pictures) > PICTURE_SLOT_LIMIT:
        raise ValueError(
            f"素材台账登记了 {len(pictures)} 张图片，超过插件槽位上限 "
            f"{PICTURE_SLOT_LIMIT}（Picture1–{PICTURE_SLOT_LIMIT}）："
            "本期不做「超 9 张自动改段内子集」的静默降级（设计 §3.4.1），"
            "请先精简素材或拆段")
    sources: list[tuple[Path, str]] = []
    for entry in pictures:
        path = Path(resolve_asset_path(ledger, entry, session_dir=session_dir))
        if not path.is_file():
            raise ValueError(
                f"素材台账登记的图片 {entry.tag}（{entry.name}）在盘上不存在：{path}"
                "——台账是精确数据，缺正本必须点名，不能打一份少图的包")
        ext = path.suffix.lower()
        if ext not in IMAGE_EXTS:
            raise ValueError(
                f"素材 {entry.tag}（{entry.name}）的扩展名 {ext or '（无）'} 不是图片格式"
                f"（{'/'.join(sorted(IMAGE_EXTS))}）：插件按图片解码，认不出的会被清空")
        sources.append((path, ext))
    return tuple(sources)


def _find_still(session_dir: Path, position: int, stem: str) -> Path | None:
    """段目录里的首/尾帧：``director/assets/NN/<start|end>.<ext>``（按扩展名偏好取第一张）。"""
    folder = session_dir / DIRECTOR_DIRNAME / ASSETS_DIRNAME / f"{position:02d}"
    for ext in _ASSET_EXTS:
        candidate = folder / f"{stem}{ext}"
        if candidate.is_file():
            return candidate
    return None


def opening_start_still_missing(session_dir: str | Path, mode: str) -> Path | None:
    """开场首帧还没就位时，返回它**应该放**的目录；不需要或已就位返回 ``None``。

    i2v/fl2v 的开场段必须有这张图（设计 §3.4.1）。判据住在这里而不是向导里，是因为
    扩展名偏好与落点目录是打包的契约：向导另写一份，换了扩展名列表就会「向导说放好了、
    打包说没有」。调用点＝阶段 1 收尾之前的那道等待（2026-10-30 真实跑现场：用户拿到
    生图提示词后没有机会在图就位前停下来，直接撞上「第 1 段缺开场首帧」）。
    """
    if mode not in ("i2v", "fl2v"):
        return None
    directory = Path(session_dir)
    if _find_still(directory, 1, START_STEM) is not None:
        return None
    return directory / DIRECTOR_DIRNAME / ASSETS_DIRNAME / "01"


def _require_start_still(session_dir: Path, position: int) -> Path:
    """开场首帧缺了**点名段号**：i2v 段没有源图会被插件**直接拒绝**。

    依据是插件的 ``_build_gen_source_clips``（``gen_timeline.py:304-310``）：i2v 属于
    ``GEN_IMAGE_KEYS``，每段都必须有 ``genImage``/段级 ``imageFile``，缺了在建计划阶段
    就抛 ``Segment #N has no source image``——不是「静默变文生视频」（旧注释写错了：
    那个降级只发生在编辑型的其它路径上）。打包前拦下，用户才不会导入后看英文报错。
    """
    found = _find_still(session_dir, position, START_STEM)
    if found is None:
        expected = session_dir / DIRECTOR_DIRNAME / ASSETS_DIRNAME / f"{position:02d}"
        raise ValueError(
            f"第 {position} 段缺开场首帧：应放在 {expected}"
            f"/{START_STEM}.png（接受 {'/'.join(_ASSET_EXTS)}）——"
            "缺首图的 i2v 包导入后会被插件拒绝"
            "（Segment #N has no source image），整段跑不起来")
    return found


def _plan_media(session_dir: Path, plan: DirectorPlan) -> dict[int, SegmentMedia]:
    """按模式决定每段进包的媒体（设计 §3.4.1，2026-10-29 用户裁定）。

    三条来源，**不许互相顶替**：

    - ``r2v``：台账里每张已登记图 × **每一段**（全局登记号 = 段内局部槽号）；
    - ``i2v`` / ``fl2v``：只有**开场段**的 ``assets/NN/start``（fl2v 可选 ``end``）；
    - 运镜/收尾段的终点画面**不进包**：#107 出尾帧变换提示词，用户在导演台里挂。
    """
    empty = {segment.index: SegmentMedia() for segment in plan.segments}
    if plan.mode == "r2v":
        pictures = _picture_sources(session_dir)
        planned: dict[int, SegmentMedia] = {}
        for segment in plan.segments:
            folder = f"asset_groups/{segment.index:02d}"
            planned[segment.index] = SegmentMedia(refs=tuple(
                PackedMedia(source=path, pack_rel=f"{folder}/Picture{slot}{ext}")
                for slot, (path, ext) in enumerate(pictures, 1)))
        return planned
    if plan.mode in ("i2v", "fl2v"):
        opening = plan.segments[0]  # 首段必是开场（frames.validate_segment_types 保证）
        folder = f"asset_groups/{opening.index:02d}"
        start_path = _require_start_still(session_dir, opening.index)
        end_path = (_find_still(session_dir, opening.index, END_STEM)
                    if plan.mode == "fl2v" else None)
        empty[opening.index] = SegmentMedia(
            start=PackedMedia(source=start_path,
                              pack_rel=f"{folder}/{START_STEM}{start_path.suffix.lower()}"),
            end=(PackedMedia(source=end_path,
                             pack_rel=f"{folder}/{END_STEM}{end_path.suffix.lower()}")
                 if end_path is not None else None),
        )
        return empty
    return empty  # t2v：不给图


def _validate_picture_slots(plan: DirectorPlan, texts: dict[int, str],
                            media: dict[int, SegmentMedia]) -> None:
    """段词里的 ``<Picture N>`` 必须落在已挂的参考图里（r2v）。

    挂全量的好处正是「全局号 = 局部号」，所以这条等价于「N ≤ 登记张数」。越界的
    引用插件不报错：它按槽位顺序补前缀，跑出来是**另一张图**当主体。
    """
    if plan.mode != "r2v":
        return
    for segment in plan.segments:
        count = len(media[segment.index].refs)
        for match in _SLOT_TOKEN_RE.finditer(texts[segment.index]):
            slot = int(match.group(1))
            if not 1 <= slot <= count:
                raise ValueError(
                    f"第 {segment.index} 段提示词引用了 <Picture {slot}>，但素材台账只挂了 "
                    f"{count} 张图（Picture1–{count}）：悬空的槽号会让插件按顺序补前缀，"
                    "主体指称与实际挂图对不上")


def _media_ref(media: PackedMedia, *, with_size: bool = False) -> dict[str, Any]:
    """媒体字段的统一写法：**写 ``imageFile``**（只写 ``fileName`` 的导入时不会被改写）。

    插件导入只改这 6 个键名（``pack.py:899-924``）：``imageFile`` 值以
    ``PACK_PREFIXES`` 开头才会被改写成「相对 ComfyUI input」的路径；只写 ``fileName``
    的图像留在包里、运行时找不到（``_collect_missing_media`` 会把字段**清空**但不报错）。
    """
    parent = Path(media.pack_rel).parent.as_posix()
    item: dict[str, Any] = {
        "imageFile": media.pack_rel,
        "fileName": Path(media.pack_rel).name,
        "type": "input",
        "subfolder": "" if parent in ("", ".") else parent,
    }
    if with_size:
        # fl2v 的 startImage/endImage 形状带 width/height（``pack.py:445-451``）；
        # 我们不知道用户图的真实尺寸，写 0 让插件自己量（它按 imageFile 解码）。
        item["width"] = 0
        item["height"] = 0
    return item


def _segment_dict(segment: PackedSegment, *, start: int,
                  settings: DirectorPackSettings) -> dict[str, Any]:
    """一段的 timeline 段对象（**声明坐标**；``length`` 与 ``frameCount`` 互备）。

    ``continuityFromPrev`` 按段类型派生（与 ``frames.is_guided`` 同一份真源）：切镜段
    起点不在上一段终点，写 ``true`` 会让插件拿上一段尾帧当上下文，画面直接接错。
    ``durationSec`` 必须与声明帧自洽（ADR 0025）：插件优先读它并用
    ``align(round(sec×24))`` 覆盖 ``frameCount``（``gen_timeline.py:89-99``）。
    """
    row: dict[str, Any] = {
        "id": f"seg{segment.index:02d}",
        "start": start,
        "length": segment.declared_frames,
        "frameCount": segment.declared_frames,
        "durationSec": segment.duration_sec,
        "prompt": segment.prompt,
        "negativePrompt": (FL2V_DEFAULT_NEGATIVE
                           if segment.task_type == "fl2v" else ""),
        "taskType": segment.task_type,
        # refs[].index 是**槽号**（0 基，从 0 连续）：插件直接把它传给官方节点的
        # reference_image_{index}（``plan.py:1073-1074``），跳号会让 <Picture N>
        # 与实际挂图错位（约束 5 的连续性判据正是钉这条）。
        "refs": [{**_media_ref(media), "index": position}
                 for position, media in enumerate(segment.refs)],
        "refAudios": [],
        "refVideos": [],
        "continuityFromPrev": segment.guided,
        "refImageSize": settings.ref_image_size,
        "loras": [],
        # 三个图像字段先占位再按模式填：字段存在但为空串/None 是插件的正常形态
        # （``pack.py:417-451`` 的 group.json 同款），缺键反而会让 UI 读 undefined。
        "genImage": {"imageFile": "", "fileName": ""},
        "imageFile": "",
        "startImage": None,
        "endImage": None,
    }
    if segment.start_image is not None:
        row["genImage"] = _media_ref(segment.start_image)
        # i2v 的首帧认 ``genImage.imageFile`` 或段级 ``imageFile``
        # （``gen_timeline.py:126-144`` 的 ``_resolve_gen_image_ref``）。
        row["imageFile"] = segment.start_image.pack_rel
    if segment.end_image is not None:
        row["endImage"] = _media_ref(segment.end_image, with_size=True)
    return row


def _shot_dict(segment: PackedSegment, row: dict[str, Any]) -> dict[str, Any]:
    """fl2v 的 ``shots[]``：UI 以它为准、``segments`` 是派生（研究笔记 §3.2）。

    UI 的 ``flattenFl2vShotsToSegments``（``web/js/minimax_fl2v.js:303-347``）把
    ``shots[].startImage/endImage`` 展开成段的 ``genImage/endImage``，所以两处必须
    描述同一件事——这里直接复用段对象已经算好的字段，不重算第二份。
    """
    return {
        "id": row["id"],
        "durationSec": segment.duration_sec,
        "frameCount": segment.declared_frames,
        "prompt": segment.prompt,
        "negativePrompt": row["negativePrompt"],
        "continuityFromPrev": segment.guided,
        "loras": [],
        "startImage": (
            _media_ref(segment.start_image, with_size=True)
            if segment.start_image is not None else None),
        "endImage": row["endImage"],
    }


def _build_output(settings: DirectorPackSettings, continuity_mode: str) -> dict[str, Any]:
    """``timeline.output``（设计 §3.4 约束 7 + 研究笔记 §6 的键表）。"""
    return {
        "mode": OUTPUT_MODE_FIXED,
        "width": settings.width,
        "height": settings.height,
        "longEdge": settings.ref_max_size,
        "maxExportFrames": 0,
        "exportMode": REQUIRED_OUTPUT["exportMode"],
        "audioMode": AUDIO_MODE_GENERATE,
        "refImageSize": settings.ref_image_size,
        "continuityEnabled": REQUIRED_OUTPUT["continuityEnabled"],
        "continuityOverlapFrames": REQUIRED_OUTPUT["continuityOverlapFrames"],
        "continuityMode": continuity_mode,
        # 只有 continue 模式有意义；写插件默认值，不改用户没要求的东西。
        "continuityRedraw": 0.10,
        "continuityKeepTail": REQUIRED_OUTPUT["continuityKeepTail"],
        "exportSourceImages": False,
        "exportPreFaceRefine": False,
    }


def _build_timeline(plan: DirectorPlan, segments: Sequence[PackedSegment], *,
                    settings: DirectorPackSettings, continuity_mode: str) -> dict[str, Any]:
    """整份 ``timeline.json``（v4）：**声明坐标**、生成类显式时间线形态。"""
    rows: list[dict[str, Any]] = []
    cursor = 0
    for segment in segments:
        rows.append(_segment_dict(segment, start=cursor, settings=settings))
        cursor += segment.declared_frames
    timeline: dict[str, Any] = {
        "version": TIMELINE_VERSION,
        "timelineMode": (TIMELINE_MODE_FL2V if plan.mode == "fl2v"
                         else TIMELINE_MODE_PROMPT_BATCH),
        "editMode": EDIT_MODE_SEGMENT,
        "frameRate": FPS,
        # 声明坐标合计：导演台显示的长度就是它（与 plan.total_frames 的成片坐标不同）。
        "totalFrames": cursor,
        "width": settings.width,
        "height": settings.height,
        "refMaxSize": settings.ref_max_size,
        "output": _build_output(settings, continuity_mode),
        "videoClips": [],
        "video": {
            "fileName": "", "videoFile": "", "subfolder": "", "type": "input",
            "frames": [], "frameMap": [],
        },
        "global": {
            "taskType": plan.mode,
            "prompt": "",
            "commonEnabled": COMMON_ENABLED,
            "commonCollapsed": False,
            "refs": [],
            "refAudios": [],
            "refVideos": [],
        },
        "segments": rows,
        "runSelectEnabled": False,
        "runSelection": [],
    }
    if plan.mode == "fl2v":
        timeline["shots"] = [_shot_dict(segment, row)
                             for segment, row in zip(segments, rows)]
        timeline["keyframes"] = []
    return timeline


def _build_pack_json(plan: DirectorPlan, timeline: dict[str, Any]) -> dict[str, Any]:
    """``pack.json``：格式串/版本 + widgets + output（``pack.py:589-599``）。

    ``widgets.task_type`` 与 ``timeline.global.taskType`` 必须一致——导入时前者优先级
    最高（``pack.py:1056-1061``），不一致等于用一个没给用户看过的模式跑片。
    """
    return {
        "format": PACK_FORMAT,
        "formatVersion": PACK_FORMAT_VERSION,
        "taskType": plan.mode,
        "widgets": {**PACK_WIDGETS, "task_type": plan.mode},
        "output": dict(timeline["output"]),
    }


def _build_shared_params(timeline: dict[str, Any]) -> dict[str, Any]:
    """``shared_params/shared_params.json``：公共参考关闭（``pack.py:600-614``）。

    ``commonEnabled`` 直接取 ``timeline.global`` 的那一份：两处写同一个值才是「一处
    契约」，各写一份常量迟早一边被改。
    """
    return {
        "commonEnabled": timeline["global"]["commonEnabled"],
        "commonCollapsed": False,
        "prompt": "",
        "refs": [],
        "refAudios": [],
        "refVideos": [],
    }


def _build_group_json(row: dict[str, Any]) -> dict[str, Any]:
    """``asset_groups/NN/group.json``：与 timeline 段同形（插件 ``pack.py:417-451``）。

    **直接由段对象派生**（``dict(row)``），不逐键重抄一份键表（复审 H8）：实测两者的
    键集合完全一致（都是 ``_segment_dict`` 的 18 个键，无增减）。重抄的那份在
    ``_segment_dict`` 将来加键时会静默少一个——而 ``segments[].prompt`` 为空时插件
    会拿 group.json 兜底，少一个键就是兜底读到的段与时间线不同形。
    """
    return dict(row)


def _collect_media(segments: Sequence[PackedSegment]) -> list[PackedMedia]:
    collected: list[PackedMedia] = []
    for segment in segments:
        collected.extend(segment.media)
    return collected


def validate_pack_task_type(pack_json: dict[str, Any], timeline: dict[str, Any]) -> None:
    """``pack.json.widgets.task_type`` 与 ``timeline.global.taskType`` 必须一致。

    导入时 widgets 优先（插件 ``pack.py:1056-1061``），不一致＝用户会用一个没选过的
    模式跑片。**抽成纯函数**是为了能直接构造不匹配入参证明它咬得住（复审 H5）：
    此前这段内联在 ``_validate_all`` 末尾，两侧都由 ``plan.mode`` 构造、正常流程
    不可能触发，也没有用例能证明它真的会抛。
    """
    widgets_value = pack_json.get("widgets", {}).get("task_type")
    timeline_value = timeline.get("global", {}).get("taskType")
    if widgets_value != timeline_value:
        raise ValueError(
            f"pack.json.widgets.task_type={widgets_value!r} 与 "
            f"timeline.global.taskType={timeline_value!r} 不一致："
            "导入时 widgets 优先，用户会用一个没选过的模式跑片")


def _pass_fail(ok: bool) -> str:
    return "通过" if ok else "**不通过**"


def _canvas_is_aligned(value: object) -> bool:
    """画布单边是否合格：正整数且是 ``CANVAS_ALIGN`` 的倍数（bool 不是合法画布）。"""
    return (not isinstance(value, bool) and isinstance(value, int)
            and value > 0 and value % CANVAS_ALIGN == 0)


def _refs_are_continuous(refs: object) -> bool:
    """``refs[].index`` 是否从 0 连续（插件把 index 直接当槽号，跳号会错位）。"""
    rows = refs if isinstance(refs, list) else []
    return [row.get("index") for row in rows] == list(range(len(rows)))


def _fl2v_media_ok(segment: dict[str, Any]) -> bool:
    """mixed 里的 fl2v 段是否把图挂在首尾帧字段（而不是会被剥掉的 refs）。"""
    if segment.get("refs"):
        return False
    start = segment.get("startImage") or segment.get("genImage") or {}
    end = segment.get("endImage") or {}
    return bool((isinstance(start, dict) and start.get("imageFile"))
                or (isinstance(end, dict) and end.get("imageFile")))


def _render_conclusions(plan: DirectorPlan, timeline: dict[str, Any],
                        shared: dict[str, Any],
                        all_paths: Sequence[str]) -> tuple[str, ...]:
    """八条装配约束的逐条结论——**从实际值/校验结果生成**，不是手写的「通过」常量（复审 H5）。

    为什么不能手写：校验器被改坏（或被删空）时，报告照样写八行「通过」，而包已经
    导不进去——那份报告正是用户判断「能不能导入」的唯一依据。这里每条结论都各自
    重算一次判据并带上实际值（路径数、画布、模式、最多几张图），写错的结论至少能被
    读报告的人当场看出（用例 ``test_conclusions_report_failure_when_a_validator_is_emptied``
    就是把这个形态钉死）。
    """
    rows = timeline["segments"]
    output = timeline["output"]

    bad_paths = [path for path in all_paths if not is_ascii_pack_path(path)]
    shared_common = shared.get("commonEnabled")
    timeline_common = timeline.get("global", {}).get("commonEnabled")
    max_refs = max((len(row.get("refs") or []) for row in rows), default=0)
    refs_ok = (max_refs <= PICTURE_SLOT_LIMIT
               and all(_refs_are_continuous(row.get("refs")) for row in rows))
    missing_output = {
        key: output.get(key) for key, value in REQUIRED_OUTPUT.items()
        if output.get(key) != value}
    output_ok = (not missing_output
                 and output.get("mode") == OUTPUT_MODE_FIXED
                 and bool(str(output.get("continuityMode") or "").strip()))
    if plan.mode == "mixed":
        fl2v_rows = [row for row in rows if row.get("taskType") == "fl2v"]
        bad_fl2v = [row for row in fl2v_rows if not _fl2v_media_ok(row)]
        bad_tasks = [row.get("taskType") for row in rows
                     if row.get("taskType") not in MIXED_SEGMENT_TASKS]
        mixed_shape = (f"{_pass_fail(not bad_fl2v)}（{len(fl2v_rows)} 个 fl2v 段，"
                       f"违规 {len(bad_fl2v)} 个）")
        mixed_tasks = (f"{_pass_fail(not bad_tasks)}（{len(rows)} 段，"
                       f"非法任务类型 {len(bad_tasks)} 个）")
    else:
        # 非 mixed 时间线没有 fl2v 段与段内任务类型；写清「按哪个模式预检」而不是
        # 一句无信息的「本期非 mixed」——模式写错时（比如 r2v 混进 mixed 校验）能看出来。
        mixed_shape = f"通过（本期非 mixed：taskType={plan.mode}，无 fl2v 段）"
        mixed_tasks = f"通过（本期非 mixed：taskType={plan.mode}）"
    return (
        f"1. 包内路径 ASCII：{_pass_fail(not bad_paths)}"
        f"（共 {len(all_paths)} 个路径，不合规 {len(bad_paths)} 个）",
        f"2. commonEnabled=false（shared_params={shared_common!r} / "
        f"timeline.global={timeline_common!r}）："
        f"{_pass_fail(shared_common is False and timeline_common is False)}",
        f"3. 画布 {timeline['width']}×{timeline['height']} 是 {CANVAS_ALIGN} 的倍数："
        f"{_pass_fail(_canvas_is_aligned(timeline['width']) and _canvas_is_aligned(timeline['height']))}",
        f"4. refImageSize={output.get('refImageSize')!r}（只认 "
        f"{'/'.join(REF_IMAGE_SIZES)}）：{_pass_fail(output.get('refImageSize') in REF_IMAGE_SIZES)}",
        f"5. 每段参考图 ≤ {PICTURE_SLOT_LIMIT} 且 index 从 0 连续："
        f"{_pass_fail(refs_ok)}（最多 {max_refs} 张/段）",
        f"6. mixed 的 fl2v 段用首尾帧字段而非 refs：{mixed_shape}",
        f"7. output 五条引导/导出配置齐备且挂在 output 下：{_pass_fail(output_ok)}"
        f"（mode={output.get('mode')!r}，continuityMode={output.get('continuityMode')!r}，"
        f"continuityOverlapFrames={output.get('continuityOverlapFrames')!r}）",
        f"8. mixed 段类型只允许 {'/'.join(sorted(MIXED_SEGMENT_TASKS))}：{mixed_tasks}",
    )


def _validate_all(plan: DirectorPlan, timeline: dict[str, Any], pack_json: dict[str, Any],
                  shared: dict[str, Any], segments: Sequence[PackedSegment],
                  media: Sequence[PackedMedia]) -> tuple[str, ...]:
    """跑八条装配约束并返回逐条结论（**写盘之前**调用；违规抛 ValueError）。

    路径清单与写盘用的是同一个目录命名（``asset_groups/NN``，NN 是 1 起的段号补两位）：
    两处各拼一次的话，一处改了另一处不会报错——只会导出一个空目录加一堆找不到的图。
    """
    json_paths = ["pack.json", "timeline.json", "shared_params/shared_params.json"] + [
        f"asset_groups/{segment.index:02d}/group.json" for segment in segments]
    all_paths = json_paths + [item.pack_rel for item in media]
    validate_pack_paths(all_paths)
    validate_common_disabled(shared, timeline)
    validate_canvas(timeline["width"], timeline["height"])
    validate_ref_image_size(timeline["output"]["refImageSize"])
    validate_reference_capacity(timeline["segments"])
    validate_mixed_fl2v_fields(timeline["segments"], task_type=plan.mode)
    validate_output_config(timeline["output"])
    validate_mixed_segment_tasks(timeline["segments"], task_type=plan.mode)
    validate_segment_media_fields(timeline["segments"])
    validate_pack_task_type(pack_json, timeline)
    return _render_conclusions(plan, timeline, shared, all_paths)


def _write_pack(pack_dir: Path, zip_path: Path, pack_json: dict[str, Any],
                timeline: dict[str, Any], shared: dict[str, Any],
                groups: Sequence[dict[str, Any]], media: Sequence[PackedMedia]) -> None:
    """落盘：先清空旧包目录（防止上一轮的 Picture9 之类残留），再写目录与 zip。"""
    if pack_dir.exists():
        # 复用一个 GEN 目录时，上一轮的包会整份覆盖而**追加式**的东西不会；残留的
        # 媒体文件不会被任何断言发现，却会被导入侧一起拷进 input（issue #58 同形）。
        shutil.rmtree(pack_dir)
    pack_dir.mkdir(parents=True)
    (pack_dir / "pack.json").write_text(
        json.dumps(pack_json, ensure_ascii=False, indent=2), encoding="utf-8")
    (pack_dir / TIMELINE_FILENAME).write_text(
        json.dumps(timeline, ensure_ascii=False, indent=2), encoding="utf-8")
    shared_path = pack_dir / "shared_params" / "shared_params.json"
    shared_path.parent.mkdir(parents=True, exist_ok=True)
    shared_path.write_text(json.dumps(shared, ensure_ascii=False, indent=2),
                           encoding="utf-8")
    for index, group in enumerate(groups, 1):
        group_path = pack_dir / "asset_groups" / f"{index:02d}" / "group.json"
        group_path.parent.mkdir(parents=True, exist_ok=True)
        group_path.write_text(json.dumps(group, ensure_ascii=False, indent=2),
                              encoding="utf-8")
    for item in media:
        target = pack_dir / item.pack_rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(item.source, target)

    # zip 先写临时文件再换名：中途失败不会留下半个包（用户会拿它去导入）。
    temp_zip = zip_path.with_suffix(".zip.tmp")
    try:
        with zipfile.ZipFile(temp_zip, "w", zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(pack_dir.rglob("*")):
                if path.is_file():
                    archive.write(path, path.relative_to(pack_dir).as_posix())
        temp_zip.replace(zip_path)
    finally:
        if temp_zip.exists():
            temp_zip.unlink()


def prepare_director_pack(session_dir: str | Path, plan: DirectorPlan, *,
                          settings: DirectorPackSettings,
                          continuity_mode: str = CONTINUITY_DEFAULT,
                          ) -> tuple[DirectorPackJob, str]:
    """把段表、段提示词与素材打成导演包，返回 ``(job, report)``。

    **顺序是契约**：先读齐输入、再把整份包在内存里装好、跑完八条约束，最后才落盘
    （设计 §3.4「不得先写包后校验」）。落在半路的坏包会被用户直接拿去导入，插件不会
    替我们拒绝它——它只会「成功了但少几张图」。
    """
    session_dir = Path(session_dir)
    _require_pack_mode(plan.mode)
    # 首段必须是开场（唯一真源在 frames）：i2v/fl2v 的首图挂在「开场段」上，段表若把
    # 引导型段排第一，图就会挂到一段「起点在上一段终点」的段上——插件执行时首帧与
    # 段间引导互斥，画面直接换因果。规划器与 plan.json 读回都校验过，这里兜住
    # 「本函数被直接调用」这条入口。
    validate_segment_types([segment.type for segment in plan.segments], plan.mode)
    continuity_mode = validate_continuity_mode(continuity_mode)
    validate_canvas(settings.width, settings.height)
    validate_ref_image_size(settings.ref_image_size)

    texts = _read_segment_prompts(session_dir, plan)
    media_by_segment = _plan_media(session_dir, plan)
    _validate_picture_slots(plan, texts, media_by_segment)
    segments = tuple(
        PackedSegment(
            index=segment.index,
            type=segment.type,
            task_type=plan.mode,
            guided=segment.guided,
            declared_frames=segment.declared_frames,
            duration_sec=duration_sec_for_declared(segment.declared_frames),
            prompt=texts[segment.index],
            refs=media_by_segment[segment.index].refs,
            start_image=media_by_segment[segment.index].start,
            end_image=media_by_segment[segment.index].end,
        )
        for segment in plan.segments
    )

    timeline = _build_timeline(plan, segments, settings=settings,
                               continuity_mode=continuity_mode)
    pack_json = _build_pack_json(plan, timeline)
    shared = _build_shared_params(timeline)
    groups = [_build_group_json(row) for row in timeline["segments"]]
    media = _collect_media(segments)

    conclusions = _validate_all(plan, timeline, pack_json, shared, segments, media)

    pack_dir = pack_dir_for(session_dir)
    zip_path = zip_path_for(session_dir)
    _write_pack(pack_dir, zip_path, pack_json, timeline, shared, groups, media)

    job = DirectorPackJob(plan=plan, settings=settings, pack_dir=pack_dir,
                          zip_path=zip_path, segments=segments)
    return job, render_pack_report(job, conclusions=conclusions)


def render_pack_report(job: DirectorPackJob, *, conclusions: Sequence[str] = ()) -> str:
    """出站清单：两个总帧数、逐段段类型/给图/包内路径、八条约束结论、包路径。

    **必须同时写两个总帧数**（设计 §3.2「两套坐标」）：导演台里显示的是时间线长度
    （Σ 声明帧），成片会因「保完整」每段多 12 帧而更长——只写一个数，用户会以为
    「导演台里显示 28 秒」是个错误。
    """
    plan = job.plan
    settings = job.settings
    lines = [
        f"导演包已构建（{plan.mode} · {len(job.segments)} 段 · "
        f"画布 {settings.width}×{settings.height}）",
        f"  时间线总帧（Σ 声明）= {job.timeline_total_frames} 帧 / "
        f"{job.timeline_total_frames / FPS:.3f}s（导演台里显示的长度）",
        f"  成片预计帧（Σ 导出）= {job.film_total_frames} 帧 / "
        f"{job.film_total_frames / FPS:.3f}s（有引导段每段多出「保完整」的 12 帧）",
        f"  画布 {settings.width}×{settings.height}（{CANVAS_ALIGN} 对齐）· "
        f"refImageSize={settings.ref_image_size} · refMaxSize={settings.ref_max_size}",
        "",
    ]
    for packed, segment in zip(job.segments, plan.segments):
        if packed.media:
            media_text = "、".join(item.pack_rel for item in packed.media)
        else:
            media_text = "（包内无媒体）"
        lines.append(
            f"  段{packed.index} {packed.label}  {packed.task_type}  "
            f"给图：{render_give_images(segment, plan.mode)}  → {media_text}")
    lines.append("")
    lines.append("装配约束（八条，逐条结论）：")
    for conclusion in conclusions:
        lines.append(f"  {conclusion}")
    lines.append("")
    # 说清「哪些给图故意不在包里」：用户最容易在这里以为打包漏了图。终点画面要靠
    # #107 的尾帧变换提示词产出，打包时它根本不存在（设计 §3.4.1 第 3 条）。
    lines.append("未进包的给图（由 #107 出词、在导演台 UI 里逐段挂）："
                 "运镜/收尾段的终点画面；r2v 开场的可选首帧")
    lines.append("素材缺失：无（缺图会当场拒绝打包并点名，不会产出残缺包）")
    lines.append(f"包目录：{job.pack_dir}")
    lines.append(f"导演包：{job.zip_path}")
    lines.append("导入：ComfyUI 里打开导演台节点 →「导入导演包」→ 选上面的 zip，"
                 "导入后的缺失清单应为空。")
    lines.append("导入后逐段执行由 #105 跟进")
    return "\n".join(lines)


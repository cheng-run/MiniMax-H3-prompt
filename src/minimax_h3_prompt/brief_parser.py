"""brief 解析器：把用户的创意/参考资产/草稿 markdown 解析成结构化输入。

brief 文件约定（示例见 examples/）：
```
模式: ref              # ref | base
变体: T2VA             # base 模式：T2VA/I2VA/FL2VA/L2VA
时长: 5                # 秒
风格: Cinematic 真人实拍
语言: Chinese

## 剧情
一段话描述创意……

## 角色与参考图
- Picture 1: 神师 — 白发老妪，玄色长袍
- Picture 2: 韩立 — 青衫青年

## 草稿提示词
（polish 模式可选）已有提示词/剧本……
```
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # 防环：injection_card 顶层引 generation→本模块，只能单向
    from .injection_card import VideoOutputParams

_HEADING_RE = re.compile(r"^#{1,6}\s+(.*)$")
_KV_RE = re.compile(r"^([^#:#：]+)[:：]\s*(.*)$")
# refs 行：`Picture 1: 名称` 或 `- Picture 1: 名称 — 描述`（描述可省），末尾可带 `(路径)`
_REF_RE = re.compile(r"^(?:[-*]\s*)?Picture\s+(\d+)\s*[:：]\s*(.*)$", re.IGNORECASE)
_PATH_RE = re.compile(r"\(([^)]*)\)\s*$")  # 行尾 (路径)，路径含引号/反斜杠也能接
_IMG_EXT_RE = re.compile(r"\.(?:png|jpe?g|webp|bmp|heic)\s*[\"']*\s*$", re.IGNORECASE)
# 画布尺寸值：`640x1280` / `640×1280`（乘号）。x/× 两种都收——用户从渲染层抄回来的是乘号。
_CANVAS_SIZE_RE = re.compile(r"^(\d+)\s*[x×]\s*(\d+)$", re.IGNORECASE)

# 用户选定画布的载体类型：生图家族 → (宽, 高)。四处签名共用这一个名字
# （brief / 读取口 / 请求组装 / 渲染层），裸写 dict[str, tuple[int, int]] 时
# 「这坨是什么」只活在参数名里（复审点名的 Data Clump）。
CanvasMap = dict[str, tuple[int, int]]


def _normalize_path(p: str) -> str:
    """路径归一化：去掉引号、反斜杠→正斜杠、Windows 盘符→/mnt/<盘>/。"""
    p = p.strip().strip('"\'')
    if not p:
        return ""
    p = p.replace("\\", "/")
    m = re.match(r"^([A-Za-z]):/", p)
    if m:  # D:\... → /mnt/d/...
        p = "/mnt/" + m.group(1).lower() + "/" + p[m.end():]
    return p


@dataclass
class RefItem:
    picture: int  # <Picture N> 编号，从 1 起（官方规范）
    name: str
    description: str
    path: str = ""  # 参考图文件路径（可选；有则支持视觉审核）


@dataclass
class Brief:
    mode: str = "base"  # base | ref | director
    variant: str = "T2VA"  # base: T2VA/I2VA/FL2VA/L2VA
    # 导演台路线的生成模式（issue #101）：七个值见 ``DIRECTOR_MODES``，空串＝不是这条路线。
    # 它与 ``variant`` 是两套口径，**不许混用**：variant 是首尾帧链的帧形态
    # （T2VA/I2VA/FL2VA/L2VA），导演台模式是导演包的任务类型（t2v/…/mixed）。混用会让
    # 帧链判据（``FRAME_VARIANTS``）把一条导演台会话读成首尾帧（ADR 0024）。
    director_mode: str = ""
    duration: float = 5.0
    style: str = "Cinematic"
    language: str = "Chinese"
    plot: str = ""
    # 起步前置澄清的结论（向导在主题之后、生成之前问出来的「钉具体」要求）。
    # **必须是独立字段**：plot 同时喂着主题地点守卫的**子串**匹配与生图常识判官的判据，
    # 把自由文本拼进去会随机改写两条闸门的判据（docs/adr/0005）。不走 brief 文件解析，
    # 只由向导写入，故 parse_brief_text 不读它。
    clarifications: str = ""
    # 用户选定的生图侧画布（issue #42）：家族 → (宽, 高)。空 = 未选，消费方回落
    # profile 默认表（`image_canvas_size` 的兜底）。选定值是**数据**：构图词分套与
    # 画布显示都以它为准，不再用系统钦定的「推荐尺寸」。
    canvases: CanvasMap = field(default_factory=dict)
    # 视频输出三元组（issue #45）：比例档 + megapixels + 对齐倍数（59- 节点 widgets
    # 口径）。None = 未声明，消费方按画布自动推导（issue #49：比例档继承画布比例、
    # megapixels 按面积反推取档）；写了任一键即成三元组，缺的键取默认——显式声明
    # 始终优先于推导。W×H 不是输入（不支持任意宽高直填），由三元组按节点公式推导。
    video_output: VideoOutputParams | None = None
    refs: list[RefItem] = field(default_factory=list)
    draft: str = ""
    raw: str = ""

    @property
    def is_ref(self) -> bool:
        """多参路线（委托 ``is_ref_mode``）：路线判据不许在这个属性里再写一遍。"""
        return is_ref_mode(self.mode)

    @property
    def is_director(self) -> bool:
        """导演台路线（委托 ``is_director_mode``）：同上一句，判据只许在模块级写一次。"""
        return is_director_mode(self.mode)


# 帧锚点变体：参考图是"首帧/尾帧"，不是角色/场景
FRAME_VARIANTS = ("I2VA", "FL2VA", "L2VA")


def is_ref_mode(mode: str) -> bool:
    """路线判据的**唯一真源**（父 spec #77）：多参路线＝``mode == "ref"``。

    产线此前有三份同义写法（``Brief.is_ref``、``str(mode) == "ref"``、
    ``generation._route_emits_asset_prompts``），改一处漏一处的后果是「产物出口按多参、
    素材登记按首尾帧」——父 spec 逐字点名的错判据，也是本仓「同一契约两处口径」的
    老形态。所有路线分叉都从这里派生，判据本身**只在这里**写一次。

    只认精确小写 ``ref``：``REF`` / ``Ref`` 是拼错的路线值，静默放宽等于把一条本来
    该报错的路读成多参（``parse_brief_text`` 的「模式」键已按小写归一，这里的严格
    只兜住会话文件与外部调用方）。
    """
    return mode == "ref"


# 路线的三个合法值（父 spec #100）。顺序即「老路线优先」：base 是默认值。
ROUTE_MODES = ("base", "ref", "director")


def validate_route_mode(mode: str) -> str:
    """路线值只认三个精确小写值；未知值/大小写变体**当场报错**（issue #101 AC3）。

    判据函数（``is_ref_mode``/``is_director_mode``）只回答「是不是这一条路线」，对
    ``Director`` 这类拼错值一律返回 False——单看判据，一条写错的路线会被静默当成
    ``base``（本仓「静默失败」最坏的形态：用户以为在跑导演台，实际在跑首尾帧链）。
    所以会话读回口（``session_store._brief_from_dict``）必须先过这一道，让「不是任何
    已知路线」与「不是这条路线」分开。
    """
    if mode not in ROUTE_MODES:
        raise ValueError(
            f"路线无效：{mode!r}（应为 {'/'.join(ROUTE_MODES)} 之一，精确小写）")
    return mode


# 导演台路线的七个生成模式（父 spec #100 / ADR 0024）。**唯一声明**：向导菜单、
# 会话读回的校验、后续按模式分派的段规划与提示词出口都从这里取。顺序即菜单顺序。
DIRECTOR_MODES = ("t2v", "i2v", "fl2v", "r2v", "v2v", "rv2v", "mixed")


def is_director_mode(mode: str) -> bool:
    """路线判据的**唯一真源**（父 spec #100）：导演台路线＝``mode == "director"``。

    与 ``is_ref_mode`` 并列、不合并成一个「是不是新路线」的模糊判断：两条新路线的
    产物出口不同（导演包 vs 多参六段式），任何一处把二者混读都会让产物形状错位。

    只认精确小写 ``director``：``DIRECTOR`` / ``Director`` 是拼错的路线值，静默放宽
    等于把一条本该报错的路读成导演台。``parse_brief_text`` 的「模式」键虽按小写归一，
    但 brief 文件入口本票**不开**（见该函数末尾的显式拒绝），所以这里的严格只兜住
    会话文件与外部调用方。
    """
    return mode == "director"


def validate_director_mode(value: str) -> str:
    """校验导演台模式值并原样返回；**坏值大声报错**（父 spec #100「七种之一」）。

    七个值是精确数据不是自由文本：``T2V``/``Mixed`` 这类大小写变体如果在入口被
    ``lower()`` 悄悄扶正，后续按模式分派（#103）就会拿一个用户没选过的值去写提示词——
    本仓「绝不静默失败」要消灭的正是这种形态。

    调用者只有**会话读回**（``session_store._director_mode_from_dict``）：向导菜单的
    编号→值直接取 ``DIRECTOR_MODES`` 本身（来源已经是合法值，不必再校验一遍），
    brief 文件入口本票不开（``parse_brief_text`` 读到 director 直接报错）。
    """
    if value not in DIRECTOR_MODES:
        raise ValueError(
            f"导演台模式无效：{value!r}（应为 {'/'.join(DIRECTOR_MODES)} 之一，精确小写）")
    return value


# 导演台里**产资产图提示词**的生成模式（设计 §4；父 spec #100「阶段 1 的产出随模式变化」）。
# r2v 用 `<Picture N>` 指称参考素材，rv2v/mixed 也含参考素材段；t2v/i2v/fl2v 没有参考素材，
# v2v 只吃源视频——给它们出资产图就是白付三次调用，还会让素材登记凭空多出条目。
DIRECTOR_ASSET_MODES = ("r2v", "rv2v", "mixed")

# 导演台生成模式 → 首尾帧链的帧变体（``director_frame_variant`` 的唯一映射）。
# 导演台的 ``Brief.variant`` 恒为 T2VA 占位（ADR 0024：模式与变体是两套口径），照读它
# 会让 i2v/fl2v 都按 T2VA 出词——首帧词静默不产，用户要等到导入导演包才发现段里没图。
_DIRECTOR_FRAME_VARIANTS = {"i2v": "I2VA", "fl2v": "FL2VA"}


def route_emits_asset_prompts(brief: Brief) -> bool:
    """该 brief 是否产**资产图提示词**（人物/道具/场景的生图词）。

    路线能力判据的**唯一真源**（设计 §4）：替换 ``graph/nodes.py`` 里三处 ``brief.is_ref``
    早退与 ``session_store._route_filtered_state`` 的过滤判据。为什么必须按模式分派而不是
    「``is_ref`` 或 ``is_director``」：导演台的 t2v/i2v/fl2v 根本没有参考素材，
    一句 ``or`` 会让每个导演台模式都白跑三个资产图节点，并把三键写进会话。

    ref 恒为真、base 恒为假——两条老路线的判定与改动前逐位相同。
    """
    if is_director_mode(brief.mode):
        return brief.director_mode in DIRECTOR_ASSET_MODES
    return is_ref_mode(brief.mode)


def director_frame_variant(brief: Brief) -> str:
    """该 brief 的**有效帧变体**：导演台按生成模式映射，其余路线用它自己的 ``variant``。

    为什么要有这一层：导演台的 ``variant`` 是 T2VA 占位（ADR 0024），而 ``i2v``/``fl2v``
    实际需要首帧/首尾帧生图词。``_make_fl2va_frame_prompt_node`` 若照 ``brief.variant``
    取句式，两条模式都会落进「T2VA 不需要帧」的早退（或按 T2VA 出词）——静默少图。
    非导演台路线的返回值与 ``str(brief.variant).upper()`` 逐字相同（零变化）。
    """
    if is_director_mode(brief.mode):
        return _DIRECTOR_FRAME_VARIANTS.get(brief.director_mode, "")
    return str(brief.variant).upper()


def route_emits_frame_prompts(brief: Brief) -> bool:
    """该 brief 是否产**关键帧生图提示词**（首帧/尾帧）。

    导演台只有 ``i2v``（首帧）与 ``fl2v``（首尾帧）要帧图；base 按变体判；ref 恒为假
    （多参没有帧槽，锚定由参考素材承担，ADR 0011）。判据同样只住这一处。
    """
    if is_director_mode(brief.mode):
        return director_frame_variant(brief) in FRAME_VARIANTS
    # 非导演台路线的判据与改动前逐字相同（``variant not in FRAME_VARIANTS`` 的补集）：
    # 老会话里 ref+T2VA 恒为假、base+I2VA/FL2VA/L2VA 恒为真，一处行为都不动。
    return str(brief.variant).upper() in FRAME_VARIANTS


def brief_uses_refs(brief: Brief) -> bool:
    """该 brief 是否需要参考图：ref 模式（角色/场景）或帧变体（首/尾帧锚点）。"""
    return brief.mode == "ref" or brief.variant in FRAME_VARIANTS


def min_refs(brief: Brief) -> int:
    """该输入方式最少需要的参考图张数。"""
    if brief.mode == "ref":
        return 1
    if brief.variant == "FL2VA":  # 首尾帧：首帧 + 尾帧
        return 2
    if brief.variant in ("I2VA", "L2VA"):  # 仅首帧 / 仅尾帧
        return 1
    return 0  # T2VA 纯文字


def parse_canvas_size(value: str) -> tuple[int, int]:
    """解析画布尺寸值 `640x1280` / `640×1280`；坏值**大声报错**（issue #42）。

    画布是精确数据不是自由文本：静默回落默认表的后果是「用户以为选了 700×1300、
    实际按默认出图」——本仓「绝不静默失败」要消灭的正是这种形态。brief 文件的
    画布键与向导自定义输入共用这一个解析口（两处各写一份正则就是第二份可写来源）。
    """
    m = _CANVAS_SIZE_RE.match(value.strip())
    if not m:
        raise ValueError(f"画布尺寸无效：{value!r}（应为 宽x高，如 640x1280）")
    width, height = int(m.group(1)), int(m.group(2))
    if width <= 0 or height <= 0:
        raise ValueError(f"画布尺寸必须为正：{value!r}")
    return width, height


# 行首列表符（markdown 各家写法）：`- 画布 …`、`* 画布 …`、`+ 画布 …`、`> 画布 …`、
# `1. 画布 …`。画布键归一前剥掉——键形态的手误不能成为「静默忽略、悄悄回落默认表」
# 的门（issue #42 复审两轮都在这一类上开洞）。
_LIST_MARKER_RE = re.compile(r"^(?:[-*+>]|\d+[.)])\s*")


def _canvas_key_core(key: str) -> str:
    """画布键的归一核：剥行首列表符与大小写，返回前缀后的家族段。

    `画布 zimage` / `画布zimage` / `canvas_zimage` / `- 画布 zimage` / `1. 画布 …`
    都归到同一入口。
    """
    normalized = _LIST_MARKER_RE.sub("", key.strip().lower()).strip()
    for prefix in ("画布", "canvas"):
        if normalized.startswith(prefix):
            return normalized[len(prefix):].strip().lstrip("_-").strip()
    return ""


def _is_canvas_key(key: str) -> bool:
    normalized = _LIST_MARKER_RE.sub("", key.strip().lower()).strip()
    return normalized.startswith(("画布", "canvas"))


def _apply_canvas_line(key: str, value: str, brief: Brief) -> None:
    """应用一行画布键：裸 `画布: WxH` 套全部家族，`画布 <家族>: WxH` 单选一个。

    家族名对照生图默认表**当场校验**：`画布 zimge:` 这类拼写错误必须在这里报，
    否则它会被静默忽略、画布悄悄回落默认表。表格从 ``generation`` 函数级导入——
    模块级是反向依赖（generation 顶部就 import 本模块的 Brief），加载期会成环。
    """
    from .generation import IMAGE_PROFILE_DEFAULTS

    family = _canvas_key_core(key)
    size = parse_canvas_size(value)
    if not family:
        for name in IMAGE_PROFILE_DEFAULTS:
            brief.canvases[name] = size
        return
    if family not in IMAGE_PROFILE_DEFAULTS:
        raise ValueError(f"未知生图模型：{family}（画布键只认 {', '.join(IMAGE_PROFILE_DEFAULTS)}）")
    brief.canvases[family] = size


# 视频输出三元组的 brief 键（issue #45）：比例档 + megapixels + 对齐倍数，中英双键。
# 值口径全在 ``injection_card``（比例归一/区间校验一个门），这里只认键与派活。
_VIDEO_OUTPUT_KEYS = {
    "视频比例": "ratio",
    "视频像素预算": "megapixels",
    "视频对齐倍数": "multiple",
    "video_ratio": "ratio",
    "video_megapixels": "megapixels",
    "video_multiple": "multiple",
}


def _video_output_field(key: str) -> str:
    """键 → 三元组字段名；非视频输出键返回空串（行首列表符先剥，画布同款）。"""
    normalized = _LIST_MARKER_RE.sub("", key.strip().lower()).strip()
    return _VIDEO_OUTPUT_KEYS.get(normalized, "")


def _video_output_line_error(stripped: str) -> ValueError:
    """视频输出行解析失败的**唯一**报错构造（两处探针同文案——各拼一遍就会漂）。"""
    return ValueError(f"视频输出行无法解析：{stripped!r}"
                      "（应为「视频比例/视频像素预算/视频对齐倍数: 值」）")


def _apply_video_output_line(key: str, value: str, brief: Brief) -> None:
    """应用一行视频输出键：三元组按行累积，缺的键取默认（写任一键即成三元组）。

    坏值**当场报**（画布同纪律）：`视频比例: 5:6` 静默回落就是「用户以为选了 5:6、
    实际按默认 9:16 出片」。构造走 ``injection_card.VideoOutputParams`` 函数级导入
    ——模块级会经 generation 成环（画布表同款防环口径）；``replace`` 保留未改字段，
    校验照走 ``__post_init__`` 同一个门。
    """
    from dataclasses import replace

    from .injection_card import VideoOutputParams

    field_name = _video_output_field(key)
    current = brief.video_output or VideoOutputParams()
    text = value.strip()
    if field_name == "ratio":
        updated = replace(current, ratio=text)
    elif field_name == "megapixels":
        try:
            megapixels = float(text)
        except ValueError as exc:
            raise ValueError(f"视频像素预算无效：{value!r}（应为数字，如 0.4）") from exc
        updated = replace(current, megapixels=megapixels)
    else:
        try:
            multiple = int(text)
        except ValueError as exc:
            raise ValueError(f"视频对齐倍数无效：{value!r}（应为整数，如 32）") from exc
        updated = replace(current, multiple=multiple)
    brief.video_output = updated


def parse_brief(path: str | Path) -> Brief:
    text = Path(path).read_text(encoding="utf-8")
    return parse_brief_text(text)


def parse_brief_text(text: str) -> Brief:
    brief = Brief(raw=text)
    current_section = "meta"  # 首个标题前是配置区
    section_buf: dict[str, list[str]] = {"meta": []}

    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            hashes = len(stripped) - len(stripped.lstrip("#"))
            if hashes >= 2:  # ## 及以上才是小节；一级标题只是文件标题，不切换 section
                current_section = stripped.lstrip("#").strip()
                section_buf.setdefault(current_section, [])
            continue
        section_buf.setdefault(current_section, []).append(stripped)

    # --- 配置区（meta / 无标题的键值对）---
    # 优先级：输入（首帧/尾帧/首尾帧/多参） > 模型（fl2va/ref2va） > 模式/变体
    input_seen = model_seen = False
    for line in section_buf["meta"]:
        stripped = line.strip()
        probe = _LIST_MARKER_RE.sub("", stripped.lower()).strip()
        kv = _KV_RE.match(stripped)
        if not kv:
            # 画布行/视频输出行**长得像该键却解析不了**（缺冒号、`=` 分隔、空格分隔）
            # 要当场报：沉默跳过就是「用户以为选了、实际回落默认」（issue #42 复审）。
            if probe.startswith(("画布", "canvas")):
                raise ValueError(f"画布行无法解析：{stripped!r}（应为「画布 <家族>: 宽x高」）")
            if any(probe.startswith(name) for name in _VIDEO_OUTPUT_KEYS):
                raise _video_output_line_error(stripped)
            continue
        key, value = kv.group(1).strip(), kv.group(2).strip()
        # 画布行/视频输出行先于「空值跳过」处理：空值是坏值（解析口会报），不是「没写」
        # ——通用的 `if not value: continue` 曾把 `画布 zimage:` 静默吞掉（复审点名）。
        if _is_canvas_key(key):
            value = re.split(r"\s+#", value, maxsplit=1)[0].strip()
            _apply_canvas_line(key, value, brief)
            continue
        if _video_output_field(key):
            value = re.split(r"\s+#", value, maxsplit=1)[0].strip()
            _apply_video_output_line(key, value, brief)
            continue
        # 形似视频输出键但键没归一（`视频比例=9:16` 会被 KV 正则截成键「视频比例=9」、
        # 值「16」）：这种行进了别的分支就是静默吞——当场报。
        if any(probe.startswith(name) for name in _VIDEO_OUTPUT_KEYS):
            raise _video_output_line_error(stripped)
        if not value:
            continue
        value = re.split(r"\s+#", value, maxsplit=1)[0].strip()  # 去掉行内注释（空格+#）
        k = key.lower()
        if k in ("输入", "input"):
            input_seen = True
            m = value.strip().lower()
            if m in ("多参考", "多参", "ref2va"):  # 「多参考」是旧写法，「多参」是术语正名（issue #41）
                brief.mode = "ref"
            elif m in ("首帧", "i2va"):
                brief.mode = "base"
                brief.variant = "I2VA"
            elif m in ("尾帧", "l2va"):
                brief.mode = "base"
                brief.variant = "L2VA"
            elif m in ("首尾帧", "fl2va"):
                brief.mode = "base"
                brief.variant = "FL2VA"
        elif k in ("模型", "model") and not input_seen:
            model_seen = True
            m = value.strip().lower()
            if m == "ref2va":
                brief.mode = "ref"
            elif m in ("t2va", "i2va", "fl2va", "l2va"):
                brief.mode = "base"
                brief.variant = m.upper()
        elif k in ("模式", "mode") and not (input_seen or model_seen):
            brief.mode = value.lower()
        elif k in ("变体", "variant") and not (input_seen or model_seen):
            brief.variant = value.strip().upper()
        elif k in ("时长", "duration"):
            m = re.search(r"\d+(?:\.\d+)?", value)
            if m:
                brief.duration = float(m.group())
        elif k in ("风格", "style"):
            brief.style = value
        elif k in ("语言", "language"):
            brief.language = value

    # --- 剧情 ---
    for sec, buf in section_buf.items():
        if "剧情" in sec or "创意" in sec:
            brief.plot = "\n".join(buf).strip()
            break

    # --- 角色与参考图 ---
    for sec, buf in section_buf.items():
        if "参考图" in sec or "角色" in sec:
            for line in buf:
                rm = _REF_RE.match(line.strip())
                if not rm:
                    continue
                picture = int(rm.group(1))
                rest = rm.group(2).strip()
                path = ""
                pm = _PATH_RE.search(rest)
                if pm and _IMG_EXT_RE.search(pm.group(1)):  # 行尾 (路径) 且以图片扩展名结尾
                    path = _normalize_path(pm.group(1))
                    rest = rest[: pm.start()].strip()
                name, desc = rest, ""
                # 分隔符：优先中文破折号 ——，其次空格包围的 - / –（避免 Z-Image 这类连字符误拆）
                sm = re.search(r"(?:—|－)", rest)
                if not sm:
                    sm = re.search(r"\s+[-–]\s+", rest)
                if sm:
                    name = rest[: sm.start()].strip()
                    desc = rest[sm.end():].strip()
                brief.refs.append(RefItem(picture=picture, name=name, description=desc, path=path))
            break

    # --- 草稿（polish 模式）---
    for sec, buf in section_buf.items():
        if "草稿" in sec or "草稿提示词" in sec or "draft" in sec.lower():
            brief.draft = "\n".join(buf).strip()
            break

    # 若没有任何剧情，把整个文件作为剧情（兜底）
    if not brief.plot and brief.mode == "base":
        brief.plot = text.strip()

    # 多参路线的 variant 无意义（issue #41 复审）：归一成 T2VA 占位——否则
    # 「变体: FL2VA」写在「输入: 多参」之前的 brief 会带着 FL2VA 进 ref 管线，
    # 帧链照跑（fl2va_frame_prompts 只判 variant）、产物按 FL2VA 形状走。
    if brief.mode == "ref":
        brief.variant = "T2VA"

    # 导演台路线（issue #101）本票只开**向导入口**：brief 文件里没有「选模式」的问答位，
    # `--brief` 快路径也没有打包/陪跑接线（#104–#108）。读到就当场报错——静默按 base 跑
    # 会拿首尾帧链的步骤跑一条导演台 brief，而且要到阶段 2 才露馅。
    if is_director_mode(brief.mode):
        raise ValueError(
            "brief 文件暂不支持导演台路线（模式: director）：请用交互向导起步。")

    return brief

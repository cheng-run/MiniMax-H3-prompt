"""导演台帧数学（issue #102）：17k 网格、声明/导出帧换算、段类型与给图清单。

**纯函数模块，不做 I/O**：这里的每个值都由常量派生，供 ``planning.py`` 的段表、
``#104`` 的打包与出站清单共用——帧口径只住这一处（设计 §1「一个契约只住一处」）。

**不要复用既有档表** ``H3_TIER_FRAMES``（17k+5）：它有引导段导出恒为 17 的倍数，
与那张表永不相交（ADR 0024「分歧的算术」）。两套判据必须分住两条路线，否则将来
有人「顺手统一」会把另一条路线改坏。
"""
from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Sequence

FPS = 24

# 导演台插件的对齐函数是 17k+5，只向上取整（``ComfyUI_MiniMaxH3_Director/director/
# frame_align.py:8``）。这两个数是**插件契约**，插件升级要重核。
GRID_STEP = 17
GRID_OFFSET = 5

# 「保完整」（``continuityKeepTail``）常开时对齐补出的余量：有引导段导出帧 =
# align(声明帧) + 12。原因：declared 已在 17k+5 上，而上下文帧数 {5,22,39,56} 也
# 全部 ≡5 (mod 17)，相加恒 ≡10 → align 再补 12（ADR 0024）。
KEEP_TAIL_FRAMES = 12

# 五种段类型（spec「段类型与给图规则」）。顺序即出站展示顺序；**唯一声明**：
# 引导开关、给图清单、plan.json 的合法性校验都从这里派生，别在调用点各写一份。
SEGMENT_TYPES = ("opening", "continue", "camera_same", "camera_cut", "ending")

# 各模式**允许**的段类型（ADR 0026，2026-10-30 修订版）：i2v 只允许开场，且整条段表
# 只能一段（见下面的 ``_MODE_MAX_SEGMENTS``）。依据是插件执行分支，不是风格偏好：
#
# 1. ``gen_timeline.py:304-310``：i2v 的**每一段**都必须自带源图——``GEN_IMAGE_KEYS``
#    （``:22``）只收 i2v，而我们的包写 ``timelineMode="prompt_batch"``，插件在
#    ``:393-394`` 把 ``edit_mode`` 强制成 ``"segment"``，于是 ``_resolve_gen_image_ref``
#    （``:126-144``）只读段内 ``genImage``/段级 ``imageFile``，「全局一张图」分支不可达。
# 2. ``executor_core.py:851-856``：``i2v_new_anchor = task_key == "i2v" and first_frame
#    is not None`` → ``use_motion_context`` 恒假。i2v 段必然有首帧图，于是**段间引导
#    对 i2v 永久失效**。
#
# 两条合起来＝i2v 在插件里没有「续接」这个语义：多段只会得到 N 个互不相接的片段，接缝
# 是硬切。``continue``/``ending`` 这类靠「起点就是上一段终点」成立的类型，在 i2v 下是
# 做不到的声明；运镜/切镜同理属 fl2v（用户原话：「fl2v 是一个高级 i2v，因为有首尾帧
# 可以运镜……你 i2v 来凑什么热闹」）。
# 没登记的模式＝全部五型（t2v/r2v/fl2v 与二期的 v2v/rv2v/mixed 一个都不收窄）。
_SEGMENT_TYPES_BY_MODE: dict[str, tuple[str, ...]] = {
    "i2v": ("opening",),
}

# 各模式段表**最多几段**（未登记＝不限）。i2v 恒为 1：插件的 i2v 是「一张首帧 → 一段
# 视频」，分段对它没有意义（依据见上一条）。单段制顺带让插件「每段都要有源图」这条
# 契约在我们的产物里恒成立——包里只有一段，也就只有一张首帧，第 2 段不可能缺图。
_MODE_MAX_SEGMENTS: dict[str, int] = {
    "i2v": 1,
}

# 单段模式的时长上限：全仓「单段视频 4–15 秒」的口径住 ``tools/preflight.py:32-33``。
# 这里刻意不 import 它——preflight 会拉 cv2/numpy，而本模块是纯算术件。两个 15 由用例
# ``test_single_segment_max_matches_preflight`` 交叉校验：改一处不改另一处当场报红。
SINGLE_SEGMENT_MAX_S = 15.0


def segment_types_for_mode(mode: str) -> tuple[str, ...]:
    """该模式允许的段类型清单；规划请求、解析与读回校验**都从这里取**。

    为什么要有这一层：清单若在解析处与请求处各写一份，改一处漏一处，模型照着旧清单
    产出的段类型会在读回时才被判红——用户已经为那次规划付过钱。
    """
    return _SEGMENT_TYPES_BY_MODE.get(mode, SEGMENT_TYPES)


def max_segments_for_mode(mode: str) -> int | None:
    """该模式的段表最多几段；``None``＝不限（ADR 0026：i2v 单段）。"""
    return _MODE_MAX_SEGMENTS.get(mode)


def max_total_seconds_for_mode(mode: str) -> float | None:
    """该模式整片最多多少**秒**；``None``＝不限。

    单段模式（i2v）的整片就是那一段，所以整片上限＝单段上限。用秒而不是帧，是因为
    这道闸要拦在用户刚答完总时长的时候——那时手上只有秒，还没有段表。
    """
    return SINGLE_SEGMENT_MAX_S if max_segments_for_mode(mode) == 1 else None

SEGMENT_TYPE_LABELS = {
    "opening": "开场",
    "continue": "续接",
    "camera_same": "运镜·同场景",
    "camera_cut": "运镜·切镜",
    "ending": "收尾定格",
}

# 引导开＝起点就是上一段终点（类型语义里「引用上段」）。段间引导四个条件之一是
# 段号 > 0，所以首段**只能**是 opening——这条规则在 ``validate_segment_types`` 里
# 把「做不到的声明」拦在规划期，而不是让它到插件里静默降级成硬切。
GUIDED_SEGMENT_TYPES = frozenset({"continue", "camera_same", "ending"})
FIRST_SEGMENT_TYPE = "opening"

# 出站给图清单的 token（plan.json 里 ``give_images`` 的取值）；中文显示由
# ``planning.render_plan_listing`` 一处翻译。
IMAGE_FIRST = "first"
IMAGE_END = "end"
IMAGE_ASSETS = "assets"

# 一期可做**叙事规划**的模式（#102）。v2v/rv2v 的段边界取自源视频时间轴、不由叙事
# 规划决定（设计 §3.10 / #109），mixed 是二期（#110）——它们一律当场报错，不许被
# 当成四个模式之一静默跑一遍。
PLAN_MODES = ("t2v", "i2v", "fl2v", "r2v")

# 一段最少要有一个网格格（17 帧 ≈ 0.71s）：比这还短的段在导演台里没有执行意义，
# 也凑不进 17 的倍数。
MIN_SEGMENT_FRAMES = GRID_STEP


def _require_positive_int(value: object, what: str) -> int:
    """帧数只接受正整数；``bool`` 是 ``int`` 的子类，也要挡住。"""
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{what} 必须是正整数帧，实际 {value!r}")
    return value


def align_frames(frames: int) -> int:
    """把帧数向上吸附到插件网格 ``17k+5``（最小 5）。

    只向上取整是插件的行为（``frame_align.py:8``）：采样窗口补到网格上、补出来的
    帧是模型真生成的画面，所以**不能**向下贴——向下等于替插件裁掉句尾（ADR 0024）。
    """
    if isinstance(frames, bool) or not isinstance(frames, int):
        raise ValueError(f"帧数必须是整数，实际 {frames!r}")
    if frames < 0:
        raise ValueError(f"帧数不能为负：{frames}")
    steps = (frames - GRID_OFFSET + GRID_STEP - 1) // GRID_STEP
    return max(GRID_OFFSET, steps * GRID_STEP + GRID_OFFSET)


def is_guided(segment_type: str) -> bool:
    """段类型 → 引导开关。未知类型**报错**，不回落（票面硬约束）。"""
    if segment_type not in SEGMENT_TYPES:
        raise ValueError(
            f"未知段类型：{segment_type!r}（应为 {'/'.join(SEGMENT_TYPES)} 之一）")
    return segment_type in GUIDED_SEGMENT_TYPES


def segment_type_label(segment_type: str) -> str:
    """段类型 → 中文显示（出站清单用）；未知类型报错。"""
    is_guided(segment_type)  # 复用合法性校验，报错文案同一份
    return SEGMENT_TYPE_LABELS[segment_type]


def validate_segment_types(types: Sequence[str], mode: str) -> None:
    """校验整条段表类型序列：非空、段数不超本模式上限、类型属于**本模式允许清单**、
    首段必须是开场。

    首段约束不是风格要求：段间引导要求「段数 ≥ 2、段号 > 0、引用上段」三条同时成立
    （CONTEXT 术语「段间引导」），首段写成引导型等于声明一件插件做不到的事，且插件
    会**静默硬切**——正是本仓要抢在它前面报错的那类降级。

    ``mode`` 必填（ADR 0026）：i2v 下运镜/切镜段是**做不到的声明**，不静默改判成别的
    类型（改判会擅改用户内容），也不留给打包时才发现。**段数上限**同理：i2v 单段，
    多段在插件里必然缺源图（``gen_timeline.py:304-310``）或互不衔接
    （``executor_core.py:851-856``），所以在这里拦，而不是让用户导入后看英文报错。
    """
    if not types:
        raise ValueError("段表为空：导演台至少要有一段")
    limit = max_segments_for_mode(mode)
    if limit is not None and len(types) > limit:
        raise ValueError(
            f"{mode} 模式的段表最多 {limit} 段，实际 {len(types)} 段："
            "插件的 i2v 是「一张首帧 → 一段视频」，每段都必须自带源图、且只要有首帧"
            "就跳过段间引导（gen_timeline.py:304-310 / executor_core.py:851-856），"
            "多段只会得到互不相接的片段。长片请走 fl2v（ADR 0026）")
    allowed = segment_types_for_mode(mode)
    for index, segment_type in enumerate(types, 1):
        if segment_type not in SEGMENT_TYPES:
            raise ValueError(
                f"第 {index} 段类型非法：{segment_type!r}"
                f"（应为 {'/'.join(SEGMENT_TYPES)} 之一）")
        if segment_type not in allowed:
            raise ValueError(
                f"第 {index} 段类型 {segment_type!r} 不属于 {mode} 模式："
                f"本模式只允许 {'/'.join(allowed)}"
                "（运镜与切镜段要首尾帧才能钉住起止点，属于 fl2v——ADR 0026）")
    if types[0] != FIRST_SEGMENT_TYPE:
        raise ValueError(
            f"第 1 段必须是 {FIRST_SEGMENT_TYPE}（开场），实际 {types[0]!r}："
            "段间引导在段号 0 上不生效，引导型首段会被插件静默降级成硬切")


def give_images_for(segment_type: str, mode: str) -> tuple[str, ...]:
    """该段**必填**给图清单（出站清单，不是打包清单；设计 §3.1 表）。

    尾帧（终点画面）打包时还不存在：它由 #107 的尾帧变换提示词产出、用户在导演台
    UI 里逐段挂。``opening`` 段按模式分派；``fl2v`` 的尾帧与 ``r2v`` 的首帧是**可选**
    的，见 ``optional_images_for``。``i2v`` 段内不要终点画面（见下方分支）。
    """
    if mode not in PLAN_MODES:
        raise ValueError(
            f"导演台模式 {mode!r} 本票不做叙事规划（一期只支持 "
            f"{'/'.join(PLAN_MODES)}；v2v/rv2v 见 #109、mixed 见 #110）")
    is_guided(segment_type)  # 类型合法性
    if segment_type == "continue":
        return ()
    # i2v 单段（ADR 0026 修订版）：唯一那段就是开场，给首帧；其余段类型在本模式下
    # 根本不允许出现（``segment_types_for_mode`` 只放行 opening，段数上限又是 1），
    # 所以这里不再给不可达情形发明规则——给不可达分支写规则就是下一个误读源。
    if mode == "i2v":
        return (IMAGE_FIRST,) if segment_type == "opening" else ()
    if segment_type == "camera_same":
        return (IMAGE_END,)
    if segment_type == "camera_cut":
        return (IMAGE_FIRST, IMAGE_END)
    if segment_type == "ending":
        return (IMAGE_END,)
    # opening：按模式分派（t2v 无图；i2v/fl2v 给首帧；r2v 给参考素材）
    return {
        "t2v": (),
        "i2v": (IMAGE_FIRST,),
        "fl2v": (IMAGE_FIRST,),
        "r2v": (IMAGE_ASSETS,),
    }[mode]


def optional_images_for(segment_type: str, mode: str) -> tuple[str, ...]:
    """该段**可选**给图（设计 §3.1 表：``fl2v`` 开场尾帧可选、``r2v`` 开场首帧可选）。"""
    give_images_for(segment_type, mode)  # 复用模式/类型合法性校验
    if segment_type != "opening":
        return ()
    if mode == "fl2v":
        return (IMAGE_END,)
    if mode == "r2v":
        return (IMAGE_FIRST,)
    return ()


def declared_frames_for_export(export_frames: int, *, guided: bool) -> int:
    """导出帧 → 声明帧（写包用）：有引导段减 12，无引导段相等。"""
    _require_positive_int(export_frames, "导出帧")
    if not guided:
        return export_frames
    if export_frames % GRID_STEP:
        raise ValueError(
            f"有引导段的导出帧必须是 {GRID_STEP} 的倍数（17k），实际 {export_frames}；"
            "导出帧凑不出 17k+5 网格（ADR 0024）")
    declared = export_frames - KEEP_TAIL_FRAMES
    if declared <= 0:
        raise ValueError(f"导出帧 {export_frames} 太小，减掉保完整余量后没有声明帧")
    return declared


def export_frames_for_declared(declared_frames: int, *, guided: bool) -> int:
    """声明帧 → 导出帧（读回校验用）：有引导段 align(声明) + 12，无引导段相等。"""
    _require_positive_int(declared_frames, "声明帧")
    if not guided:
        return declared_frames
    return align_frames(declared_frames) + KEEP_TAIL_FRAMES


def is_on_grid(frames: int) -> bool:
    """帧数是否落在 ``17k+5`` 上（ADR 0025：声明帧一律必须在网格上）。"""
    return isinstance(frames, int) and not isinstance(frames, bool) \
        and frames >= GRID_OFFSET and (frames - GRID_OFFSET) % GRID_STEP == 0


def _grid_floor(value: int) -> int:
    """不大于 ``value`` 的最大 ``17k+5``（可能小于 5，调用处按最小段长过滤）。"""
    return ((value - GRID_OFFSET) // GRID_STEP) * GRID_STEP + GRID_OFFSET


def _grid_candidates(value: int, *, minimum: int = MIN_SEGMENT_FRAMES) -> tuple[int, ...]:
    """``value`` 附近的两个网格值（下、上），且都不低于 ``minimum``。"""
    floor_value = _grid_floor(value)
    return tuple(v for v in (floor_value, floor_value + GRID_STEP) if v >= minimum)


def _nearest_grid(raw_frames: int) -> int:
    """把提议帧吸附到最近的网格值（同距取上界：保完整别主动吞句尾，ADR 0024）。"""
    candidates = _grid_candidates(raw_frames)
    if not candidates:
        # raw 小到连一个网格格都撑不到：直接抬到最小网格段
        return _grid_floor(MIN_SEGMENT_FRAMES) + GRID_STEP
    return min(candidates, key=lambda v: (abs(v - raw_frames), -v))


def duration_sec_for_declared(declared_frames: int) -> float:
    """声明帧 → 包内要写的 ``durationSec``（1 位小数），两者**严格自洽**（ADR 0025）。

    自洽判据就是插件的换算：``align(round(sec × 24)) == 声明帧``（``gen_timeline.py:80-99``
    与 ``minimax_gen_timeline.js:93-107``）。ADR 的样例：107 ↔ 4.0s、141 ↔ 5.8s——
    超过 4.0 的下一个 0.1s（4.5s）会吸附成 124，就与声明帧对不上了。

    取值规则：能取整秒就取整秒（UI 上干净），否则取**最大**的合法 0.1s。口径只住这里，
    #104 打导演包时从这里取，不要另写一份换算（同一契约两处口径）。
    """
    _require_positive_int(declared_frames, "声明帧")
    if not is_on_grid(declared_frames):
        raise ValueError(
            f"声明帧 {declared_frames} 不在 17k+5 网格上，反演不出自洽的 durationSec"
            "（ADR 0025：声明帧必须在网格上）")
    # 搜索区间：合法时段宽度小于 1 秒，留 2 秒余量即可覆盖全部候选。
    limit = declared_frames * 10 // FPS + 20
    valid = [
        tenth
        for tenth in range(1, limit + 1)
        # 与 JS 的 ``Math.round`` 同口径（半值向上），避免 Python 银行家舍入。
        if align_frames((tenth * FPS + 5) // 10) == declared_frames
    ]
    whole_seconds = [tenth for tenth in valid if tenth % 10 == 0]
    if whole_seconds:
        return max(whole_seconds) / 10
    if not valid:  # 理论上不可达：网格值一定有反演秒
        raise ValueError(f"声明帧 {declared_frames} 反演不出任何 0.1s 秒值")
    return max(valid) / 10


def _export_of(segment_type: str, declared_frames: int) -> int:
    return export_frames_for_declared(declared_frames, guided=is_guided(segment_type))


def land_exports(types: Sequence[str], raw_frames: Sequence[int],
                 target_frames: int, *, mode: str) -> tuple[list[int], str | None]:
    """把每段声明帧落到 17k+5 网格，并让导出合计尽量逼近 ``target_frames``（AC1/AC4）。

    ADR 0025 的两条硬规则决定这里不能「先均分再四舍五入」：

    1. **每段声明帧都必须在 ``17k+5`` 上**（后端与 UI 都按 ``durationSec`` 吸附，非网格
       值会被改写，总长静默漂走）；
    2. 导出帧 = 声明帧（无引导）或声明帧 + 12（有引导）→ 有引导段导出恒为 17 的倍数，
       于是 **n 段总量恒 ≡ 5×无引导段数 (mod 17)**：目标总量常常**不可达**（30 秒 / 5 段
       的 720 ≡ 6 就取不到）。所以这里枚举可达总量，取与目标差最小者（最大差 8 帧）。

    做法：每段先吸附到最近的网格值；最后一个无引导段（余额吸收者）吃 ``目标 − 其余
    之和``。余额低于最小段长时，把**代价最小**的段往下挪一格再试（提议边界可能挤得
    放不下余额）。余额本身再吸附到最近网格——同距取上界，保住句尾。

    返回值第二项是**总长说明**：落位与目标不等时给出「目标 / 实际 / 差几帧」，相等时
    为 None。这条路线不可能精确到任意秒（ADR 0025「后果」），说明必须出站，不能静默。
    """
    types = list(types)
    validate_segment_types(types, mode)
    raw = [_require_positive_int(value, f"第 {index} 段提议帧")
           for index, value in enumerate(raw_frames, 1)]
    if len(raw) != len(types):
        raise ValueError(f"提议帧数 {len(raw)} 与段数 {len(types)} 不一致")
    target_frames = _require_positive_int(target_frames, "目标总帧")
    if target_frames < MIN_SEGMENT_FRAMES * len(types):
        raise ValueError(
            f"目标总帧 {target_frames} 装不下 {len(types)} 段"
            f"（每段至少 {MIN_SEGMENT_FRAMES} 帧）")

    unguided = [index for index, segment_type in enumerate(types) if not is_guided(segment_type)]
    # 首段必须是 opening（validate_segment_types 已保证），所以这里一定有无引导段；
    # 防将来有人放宽首段规则时落进「全引导 → 总量恒 17 的倍数」的不可达死路。
    if not unguided:
        raise ValueError("段表里没有无引导段：总量只能取 17 的倍数，任何目标都可能凑不出")
    balancer = unguided[-1]
    others = [index for index in range(len(types)) if index != balancer]

    declared = {index: _nearest_grid(raw[index]) for index in others}

    while True:
        others_total = sum(_export_of(types[index], declared[index]) for index in others)
        ideal = target_frames - others_total
        if ideal >= MIN_SEGMENT_FRAMES:
            break
        # 余额装不下一段：把代价最小的一段往下挪一格（提议边界太挤时才会走到这里）。
        shrinkable = [
            index for index in others
            if declared[index] - GRID_STEP >= MIN_SEGMENT_FRAMES]
        if not shrinkable:
            raise ValueError(
                f"目标总帧 {target_frames} 在 {len(types)} 段上无合法落位"
                f"（每段至少 {MIN_SEGMENT_FRAMES} 帧、声明帧须在 {GRID_STEP}k+{GRID_OFFSET} 网格上）")

        def shrink_cost(index: int) -> int:
            """往下挪一格对「离提议多远」的代价：挪最便宜的段，别动已经偏很多的。"""
            return (abs(declared[index] - GRID_STEP - raw[index])
                    - abs(declared[index] - raw[index]))

        cheapest = min(shrinkable, key=lambda index: (shrink_cost(index), index))
        declared[cheapest] -= GRID_STEP

    # 余额吸附到最近网格：两个候选都比距离，落定「最接近目标的可达总量」。
    best: tuple[tuple, int, int] | None = None
    for candidate in _grid_candidates(ideal):
        total = others_total + _export_of(types[balancer], candidate)
        key = (abs(total - target_frames), abs(candidate - raw[balancer]), -total)
        if best is None or key < best[0]:
            best = (key, candidate, total)
    if best is None:
        raise ValueError(f"目标总帧 {target_frames} 无法落位：余额 {ideal} 附近没有合法网格值")

    _, balancer_declared, total = best
    declared[balancer] = balancer_declared
    exports = [_export_of(types[index], declared[index]) for index in range(len(types))]

    if total == target_frames:
        return exports, None
    note = (
        f"总帧目标 {target_frames} 帧，实际落位 {total} 帧（差 {abs(target_frames - total)} 帧）："
        f"声明帧全部落在 {GRID_STEP}k+{GRID_OFFSET} 网格上，{len(unguided)} 段无引导时"
        f"可达总量恒 ≡ {(GRID_OFFSET * len(unguided)) % GRID_STEP} (mod {GRID_STEP})"
        "，目标值不可达，取最近可达值（ADR 0025）")
    return exports, note


@dataclass(frozen=True)
class DirectorSegment:
    """``plan.json`` 里的一段（设计 §3.2）。秒是**派生值**：从帧现算，不落第二份真源。"""

    index: int              # 1-based：与账本 ``segment`` 字段、ADR 里的「段1」同号
    type: str
    guided: bool
    # 累计**导出帧**（成片坐标，ADR 0025 的两套坐标之一）。导演台 timeline 的
    # ``start``/``totalFrames`` 用声明坐标（累加 declared_frames），#104 打包时换算。
    start_frame: int
    end_frame: int
    declared_frames: int
    export_frames: int
    summary: str
    end_hook: str
    give_images: tuple[str, ...]

    @property
    def start_s(self) -> float:
        return self.start_frame / FPS

    @property
    def end_s(self) -> float:
        return self.end_frame / FPS

    @property
    def label(self) -> str:
        return SEGMENT_TYPE_LABELS[self.type]

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "type": self.type,
            "guided": self.guided,
            "start_frame": self.start_frame,
            "end_frame": self.end_frame,
            "declared_frames": self.declared_frames,
            "export_frames": self.export_frames,
            "start_s": self.start_s,
            "end_s": self.end_s,
            "summary": self.summary,
            "end_hook": self.end_hook,
            "give_images": list(self.give_images),
        }


@dataclass(frozen=True)
class DirectorPlan:
    """导演台的段表（``GENxxx/director/plan.json``，设计 §3.2 2026-10-29 修订）。

    与 ``segment_planner.SegmentPlan`` **是两套契约**：后者按整数秒、被 base/ref 两处
    规划与成片覆盖判据共用（设计 §3.2「不碰 SegmentPlan」）。这里以帧为真源。
    """

    mode: str
    total_frames: int
    segments: tuple[DirectorSegment, ...]
    # 规划期生成的总长说明（目标与可达总量的差）。**不写进 plan.json**：它是「这一次
    # 规划怎么定的」，读回的表不该带上一轮的说明（同一契约两处口径）。
    notes: tuple[str, ...] = ()

    @property
    def total_s(self) -> float:
        return self.total_frames / FPS

    def to_dict(self) -> dict:
        return {
            "route": "director",
            "mode": self.mode,
            "fps": FPS,
            "total_frames": self.total_frames,
            "segments": [segment.to_dict() for segment in self.segments],
        }

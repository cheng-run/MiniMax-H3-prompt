"""导演台段规划（issue #102）：LLM 提议 → 确定性吸附/反解/凑总帧 → ``plan.json``。

规划师（LLM）只给三样东西：段边界（**秒**）、段类型、``summary`` / ``end_hook``。
``guided``、声明帧/导出帧、给图清单**必须**由 ``frames.py`` 派生——设计 §3.1
「不接受 LLM 直接给」。理由不是洁癖：17k 网格的声明/导出换算错一帧，整条片子的
总长就对不上（ADR 0024 的决定 6 要求 720 帧在规划期精确落位），而模型手算 720
的正确率远低于它判断「这段该是续接还是切镜」的正确率。

``plan.json`` 与 ``segment_planner.SegmentPlan`` 是**两套契约**：后者按整数秒、被
base/ref 两处规划与成片覆盖判据共用（设计 §3.2「不碰 SegmentPlan」）。这里以帧为
真源，读回严格——续跑照错表读下去就是接错线。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from ..ref_capacity import MAX_PICTURES
from .film_end import render_film_end_block
from .frames import (
    FPS,
    GRID_OFFSET,
    GRID_STEP,
    IMAGE_ASSETS,
    IMAGE_END,
    IMAGE_FIRST,
    PLAN_MODES,
    SEGMENT_TYPES,
    DirectorPlan,
    DirectorSegment,
    declared_frames_for_export,
    give_images_for,
    is_guided,
    is_on_grid,
    land_exports,
    max_segments_for_mode,
    max_total_seconds_for_mode,
    optional_images_for,
    segment_type_label,
    segment_types_for_mode,
    validate_segment_types,
)
# 关键帧读图块与 JSON 抓取都**复用 base/ref 那一份**（同一段文本、同一套宽容度）：
# 导演台只是第三条路线，不是另一套模型输出约定。``_extract_json`` 是私有名但语义
# 稳定、无副作用，重抄一份会让两条路对模型输出的宽容度各自漂。
from ..segment_planner import FRAME_CONTEXT_HEADER, _extract_json
# 执行段 4–15 秒档：与 preflight 共用一份常量，别在规划层再抄一遍数字
# （issue #46 AC6 定档；导演台路线此前没有校验，P7）。
from ..tools.preflight import SEGMENT_MAX_S, SEGMENT_MIN_S

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"
_INSTRUCTION_NAME = "segment_planner_director.md"

# 直调路径在事件与成本表里的角色名：**必须**与 ``role_segment_planner`` 分开
# （ADR 0019 的记账口径）——混在一处就分不清钱花在哪条路线上。
DIRECTOR_PLANNER_ROLE = "role_director_segment_planner"

# 首次回复之后最多再问几次（#187 / ADR 0029 决定 5；#196 起摆位问题共用这一个
# 预算）。这是调用预算而不是重试策略——回灌内容是可执行的二选一，两次还不改说明
# 规划师在这条约束上帮不上忙，继续问只是把同一笔钱再付一遍。
MAX_BUDGET_REQUEUES = 2

# 摆位 ``end_hook`` 判据的谓词表（#196 / 父 spec #191 U3）。**只此一份**：判据、
# 回灌反馈与报错文案都从这里派生。别处再抄一份字面，改了这个表就会留下一条不说话
# 的死规则（本仓「守卫形态从常量派生」已经咬过一次）。
# ``"按住不动"`` 是 ``"不动"`` 的子串，判定上冗余；保留它是为了与规格原表逐条对齐，
# 删掉不改变任何判定（表驱动 10 条真实段尾仍全过）。
END_HOOK_STILL_WORDS = ("站", "坐", "停", "静", "不动", "定格", "摆好", "按住不动")
END_HOOK_MOTION_WORDS = ("走", "跑", "冲", "推", "拉", "抓", "转", "扑", "举", "递",
                         "落下", "撞", "绕")

# 规划产物落 **`GENxxx/director/plan.json`**（设计 §3.2，2026-10-29 修订）。刻意不写
# `segments/plan.json`：共享读取口 ``tools/ledger.read_plan_segment_count`` 只认 JSON
# **数组**，同一路径放第二种形态（dict）会让它在导演台会话上**静默返回 None**。新形态
# 住路线自己的目录，旧读取口零改动；段提示词与后续产物也统一落 ``director/``。
DIRECTOR_DIRNAME = "director"
PLAN_FILENAME = "plan.json"

# 秒是派生值，只用于「提议边界是否连续覆盖」这一道解析闸；LLM 给 30 与 30.0
# 同义，浮点累加也会带尾巴（0.83+7.3=8.129999…），所以按毫秒级容差比较。
_BOUNDARY_TOLERANCE = 1e-3


def load_instruction() -> str:
    """导演台规划指令**只在新建的第三份文件里**（设计 §2：别塞进老两份）。"""
    return (PROMPTS_DIR / _INSTRUCTION_NAME).read_text(encoding="utf-8")


def plan_path(session_dir: Path) -> Path:
    """段表落点：``GENxxx/director/plan.json``（设计 §3.2，2026-10-29 修订）。

    所有导演台产物都住 ``director/`` 下：``segments/`` 是 base/ref 的地盘，两套读取口
    混在同一路径就会互相污染（``read_plan_segment_count`` 只认数组）。
    """
    return Path(session_dir) / DIRECTOR_DIRNAME / PLAN_FILENAME


def _require_mode(mode: str) -> str:
    if mode not in PLAN_MODES:
        raise ValueError(
            f"导演台模式无效：{mode!r}（应为 {'/'.join(PLAN_MODES)} 之一，精确小写）")
    return mode


def _require_number(value: object, what: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{what} 必须是数字秒，实际 {value!r}")
    return float(value)


def _require_mode_duration(mode: str, total_s: float) -> None:
    """单段模式（i2v）的整片时长上限；超了**当场**报错，不把请求发给模型。

    这道闸必须在调模型之前（``plan_director_segments`` 也调它）：i2v 单段时整片＝那
    一段，而一段的时长上限是全仓口径 4–15 秒（``frames.SINGLE_SEGMENT_MAX_S``）。
    等模型答完再拦，用户为一次注定作废的规划付过钱（ADR 0026 修订版）。
    """
    limit_s = max_total_seconds_for_mode(mode)
    if limit_s is not None and total_s > limit_s + _BOUNDARY_TOLERANCE:
        raise ValueError(
            f"{mode} 模式的整片时长 {total_s:g} 秒超过单段上限 {limit_s:g} 秒："
            "该模式在插件里是「一张首帧 → 一段视频」（单段），这一段的时长就是整片"
            "时长。要更长的片子请选 fl2v（ADR 0026）")


def _build_segments(mode: str, types: list[str], exports: list[int],
                    summaries: list[str], hooks: list[str]) -> tuple[DirectorSegment, ...]:
    """确定性层：把「类型 + 已落位导出帧」装配成段表（声明帧/累计帧/给图全在这里派生）。"""
    segments: list[DirectorSegment] = []
    cursor = 0
    for index, (segment_type, export_frames, summary, end_hook) in enumerate(
            zip(types, exports, summaries, hooks), 1):
        guided = is_guided(segment_type)
        declared = declared_frames_for_export(export_frames, guided=guided)
        end_frame = cursor + export_frames
        segments.append(DirectorSegment(
            index=index,
            type=segment_type,
            guided=guided,
            start_frame=cursor,
            end_frame=end_frame,
            declared_frames=declared,
            export_frames=export_frames,
            summary=summary,
            end_hook=end_hook,
            give_images=give_images_for(segment_type, mode),
        ))
        cursor = end_frame
    return tuple(segments)


def is_placement_end_hook(end_hook: str) -> bool:
    """``end_hook`` 是不是「摆位式」：有静止谓词、且一个动作词都没有（#196）。

    判据**刻意是窄的**：它只咬「三个人各自摆好位置站定、画面静止」这一种实测形态，
    被它放过的 ``end_hook`` 未必都好。不做宽判据的理由是代价不对称——规划回灌比写段
    回灌贵一个数量级，宽的启发式会把一次规划反复拖成五次（#191 U3）。

    也不复用 ``tailframe.is_static_segment``：那一个判「无运镜 + end_hook 是状态
    变化」，只在尾帧链上被调用，与「段尾切点是不是摆好的造型」不是同一件事。
    """
    text = str(end_hook or "")
    if not any(word in text for word in END_HOOK_STILL_WORDS):
        return False
    return not any(word in text for word in END_HOOK_MOTION_WORDS)


def parse_director_plan(text: str, total_s: float, *, mode: str) -> DirectorPlan:
    """解析规划回复并跑确定性层；**任何契约违规都抛 ``ValueError``**。

    与 ``segment_planner.parse_segment_plan`` 的「内容病返回 None」不同：这条路线
    没有机械拆分可以回退（段边界还要按 17k 网格反解），把坏回复放过去只会让 #103
    拿着一张算不对的表写词。调用病（卡住/断流）仍由 ``plan_director_segments``
    抛 ``RuntimeError``——两种病因分开表达（ADR 0019）。
    """
    _require_mode(mode)
    total_s = _require_number(total_s, "视频总时长")
    if total_s <= 0:
        raise ValueError(f"视频总时长必须为正，实际 {total_s}")
    _require_mode_duration(mode, total_s)
    total_frames = round(total_s * FPS)

    data = _extract_json(text)
    if not isinstance(data, dict):
        raise ValueError(
            "规划回复不是 JSON 对象：导演台规划要严格只有 JSON（不要解释 / markdown）")
    raw_segments = data.get("segments")
    if not isinstance(raw_segments, list) or not raw_segments:
        raise ValueError("规划回复缺 segments 数组（或为空）：段表无法落位")

    types: list[str] = []
    summaries: list[str] = []
    hooks: list[str] = []
    windows: list[tuple[float, float]] = []
    allowed_types = segment_types_for_mode(mode)
    for position, item in enumerate(raw_segments, 1):
        if not isinstance(item, dict):
            raise ValueError(f"第 {position} 段不是 JSON 对象：{item!r}")
        segment_type = item.get("type")
        if not isinstance(segment_type, str) or segment_type not in SEGMENT_TYPES:
            raise ValueError(
                f"第 {position} 段类型非法：{segment_type!r}"
                f"（应为 {'/'.join(SEGMENT_TYPES)} 之一；缺失同样算非法）")
        if segment_type not in allowed_types:
            # ADR 0026：i2v 下判出运镜/切镜段是**做不到的声明**。不静默改判成
            # continue——那会把一段本要换镜的内容写成连续镜头，擅改用户内容。
            raise ValueError(
                f"第 {position} 段类型 {segment_type!r} 不属于 {mode} 模式："
                f"本模式只允许 {'/'.join(allowed_types)}"
                "（运镜与切镜段要首尾帧才能钉住起止点，属于 fl2v）")
        start = _require_number(item.get("start_s"), f"第 {position} 段 start_s")
        end = _require_number(item.get("end_s"), f"第 {position} 段 end_s")
        if end - start <= 0:
            raise ValueError(f"第 {position} 段窗口 {start}–{end} 非正时长")
        # 4–15 秒是规划师自己的契约（``segment_planner_director.md`` 硬约束），但
        # 之前只写在提示词里、代码不校验（L1 §四「本轮不实现」）。#137 首轮因此拿到
        # 39 帧＝1.625s 的段，打包与陪跑全盘接受。这里在**装成段表之前**拦：不合格
        # 的窗口就是一次作废的规划，不能往下游传。
        seconds = end - start
        if (seconds < SEGMENT_MIN_S - _BOUNDARY_TOLERANCE
                or seconds > SEGMENT_MAX_S + _BOUNDARY_TOLERANCE):
            raise ValueError(
                f"第 {position} 段窗口 {start}–{end} 秒＝{seconds:.3f}s，"
                f"超出 {SEGMENT_MIN_S:g}–{SEGMENT_MAX_S:g} 秒契约：每段至少 "
                f"{SEGMENT_MIN_S:g} 秒、至多 {SEGMENT_MAX_S:g} 秒，且不短于本段内"
                "最长的镜头。改段边界后重试")
        types.append(segment_type)
        windows.append((start, end))
        summaries.append(str(item.get("summary", "")))
        hooks.append(str(item.get("end_hook", "")))

    if abs(windows[0][0]) > _BOUNDARY_TOLERANCE:
        raise ValueError(
            f"段表必须从 0 秒起（第 1 段 start_s={windows[0][0]}）：导演台按总帧精确"
            "落位，前面不许留空")
    # 类型序列先于窗口校验：首段不是开场是**结构性**错误（引导在段号 0 上不生效），
    # 比「没覆盖到总时长」更该先报——顺带让 `land_exports` 的校验不被窗口 mess 掩住。
    validate_segment_types(types, mode)
    for position, (start, end) in enumerate(windows, 1):
        if position > 1 and abs(start - windows[position - 2][1]) > _BOUNDARY_TOLERANCE:
            raise ValueError(
                f"第 {position} 段起点 {start} 必须等于上一段终点 "
                f"{windows[position - 2][1]}（段表不许有缝/重叠）")
        if not hooks[position - 1].strip():
            raise ValueError(
                f"第 {position} 段缺 end_hook：它是本段切开的最后一帧，"
                "写段（#103）与尾帧变换（#107）都要拿它当约束")
    if abs(windows[-1][1] - total_s) > _BOUNDARY_TOLERANCE:
        raise ValueError(
            f"段表覆盖 0–{windows[-1][1]} 秒，没有收在总时长 {total_s} 秒："
            "导演台按总帧精确落位，不能欠也不能超")

    # 提议（秒）→ 提议（帧）→ 确定性落位。raw 只是「节奏意图」，最终帧数以
    # ``land_exports`` 的结果为准（声明帧吸附 17k+5 网格，余额由最后一个无引导段吸收；
    # 目标总量常常不可达，取最近可达值——ADR 0025）。
    target_frames = total_frames
    raw_frames = [
        max(1, round(target_frames * (end - start) / total_s))
        for start, end in windows
    ]
    exports, note = land_exports(types, raw_frames, target_frames, mode=mode)
    segments = _build_segments(mode, types, exports, summaries, hooks)
    # plan.json 的 ``total_frames`` 是**实际落位**（成片坐标），不是目标：读回校验
    # 要求 ``Σ export == total_frames``，而目标只出现在出站说明里（ADR 0025）。
    landed_frames = sum(exports)
    if segments[-1].end_frame != landed_frames:
        # land_exports 已保证 Σ==落位总量；这里是同一条契约的第二道读法，防将来
        # 有人改落位函数时两处口径悄悄分家。
        raise ValueError(
            f"段表累计导出帧 {segments[-1].end_frame} 与落位总量 {landed_frames} 不一致")
    notes = [note] if note else []
    # 残余兜底（#141 之后落位层已守住 4s 下限，正常不再触发）：将来若有人改落位
    # 逻辑又把合规窗口压出带外，至少让用户看见，而不是静默接受。不报错——报错会让
    # 「4s 档」永远规划不出来。
    for segment in segments:
        landed = segment.export_frames / FPS
        if (landed < SEGMENT_MIN_S - _BOUNDARY_TOLERANCE
                or landed > SEGMENT_MAX_S + _BOUNDARY_TOLERANCE):
            notes.append(
                f"第 {segment.index} 段落位 {landed:.3f}s 超出 "
                f"{SEGMENT_MIN_S:g}–{SEGMENT_MAX_S:g} 秒（规划窗口合格，"
                "是 17k+5 网格吸附造成）")
    return DirectorPlan(mode=mode, total_frames=landed_frames, segments=segments,
                        notes=tuple(notes))


def plan_director_segments(shot_table: str, total_s: float, llm, *, mode: str,
                           frame_context: str = "",
                           revision_context: str = "",
                           film_end_description: str = "",
                           registry: Iterable[Sequence] = (),
                           subject_definitions: str = "") -> DirectorPlanningResult:
    """调 LLM 规划导演台分段（直调走看门狗，与 ``plan_segments`` 同形）。

    失败两分法沿用 ADR 0019：**调用失败**（卡住到上限 / 中流断流 / 其他异常）抛
    ``RuntimeError``（带病因与次数），**回复不合格**抛 ``ValueError``。这条路线没有
    机械回退，两种病都交回调用方决定怎么说。

    ``frame_context`` / ``revision_context`` 与 ``plan_segments`` 同源同义：真实
    关键帧读图结果压过分镜表，用户累积修订按注入优先序进来。本函数**不依赖 state**，
    「规划层到底看到了什么」在调用点一眼可见（issue #26 的教训）。

    ``registry`` / ``subject_definitions`` 只对 r2v 有意义（#186）：前者是素材台账
    （编号 + 语义名），进请求的「素材清单」；后者是全片英文定义块，算预算闭包用。
    返回值把**段表与预算分开**交回——``plan.json`` 形态不变、不存预算复本
    （ADR 0029 决定 6），预算由调用方在写段之前单独落盘。

    r2v 的预算超限（闭包 + 锚降级之后 ``pictures`` 仍 > ``MAX_PICTURES``）时，把超限
    清单回灌给**同一个**规划师重挑，最多 ``MAX_BUDGET_REQUEUES`` 次（#187）。仍超抛
    ``AssetBudgetOverflowError``：代码不自动截断到 9、也不自动整片重规划——砍哪张没有
    依据，且重规划等于整片重来，只能由用户决定（ADR 0029 决定 5）。

    ``end_hook`` 被判为静止摆位时走**同一条**回灌路径与同一个重问预算（#196）：两块
    反馈可以同时附上（互不吞掉），两次重问后仍摆位抛 ``EndHookPlacementError``。
    """
    _require_mode(mode)
    total_s = _require_number(total_s, "视频总时长")
    # 时长上限必须在调模型之前拦：i2v 单段时整片＝那一段，等模型答完再拒，用户为一次
    # 注定作废的规划付过钱（ADR 0026 修订版）。
    _require_mode_duration(mode, total_s)
    total_frames = round(total_s * FPS)
    frame_block = (
        f"\n{FRAME_CONTEXT_HEADER}\n{frame_context}\n"
        if frame_context.strip() else ""
    )
    revision_block = f"\n{revision_context}\n" if revision_context.strip() else ""
    # 素材清单只进 r2v：别的模式没有参考素材，给一张空表只会让规划师编 `assets`。
    material_block = render_material_list(registry) if mode == "r2v" else ""
    # 全片尾帧描述只对 fl2v 有意义：其它模式没有这张图（票面：只在 fl2v 且盘上真有
    # 图时注入）。调用方已按盘面判过，这里再按模式守一道，避免别处误传把终点约束
    # 塞进没有终点图的请求。
    film_end_block = (
        render_film_end_block(film_end_description, for_planning=True)
        if mode == "fl2v" and film_end_description.strip() else ""
    )
    allowed = segment_types_for_mode(mode)
    # 允许清单是**本次请求的数据**（系统提示词是五型共用一份，不知道模式）：写死在
    # 提示词里，i2v 就会照着别的模式的口径判运镜段，用户已经付过规划的钱才发现。
    mode_scope = (
        f"本视频是导演台 {mode} 模式，段类型只能从以下清单里选：{'/'.join(allowed)}\n"
        f"（其他段类型在本模式下一律不许出现，程序会判为不合格。）\n"
    )
    if mode == "r2v":
        # r2v 的画面来源只有参考素材：首/尾帧是 fl2v 的槽位（``frames.give_images_for``
        # 在 r2v 下对五种类型都返回 assets）。系统提示词是五型共用一份、按 fl2v 口径
        # 写着首帧/尾帧，不在这里点明，规划师就会把 continue/camera_cut 记成首尾帧段。
        mode_scope += (
            f"本模式每段都只给参考素材（≤{MAX_PICTURES} 张），**没有首帧/尾帧槽**；"
            "段类型只决定起点与是否引导，**不决定给图**（静态表里的首帧/尾帧是 fl2v 口径）。\n"
        )
    if max_segments_for_mode(mode) == 1:
        # 段数上限也要进请求：系统提示词不知道模式，不写在这里模型会照「多段叙事」的
        # 习惯给五段，然后整份回复被判不合格——同样白付一次规划（ADR 0026 修订版）。
        mode_scope += "本模式的段表只能有 1 段：整片就是一个镜头，不要拆分段。\n"
    request = (
        f"{load_instruction()}\n\n"
        f"视频总时长：{total_s:g} 秒（总帧 {total_frames} 帧，{FPS}fps）\n"
        f"{mode_scope}"
        f"{material_block}"
        f"{frame_block}"
        f"{film_end_block}"
        f"{revision_block}"
        f"\n分镜表：\n{shot_table}"
    )
    # 延迟导入：``agents`` 会拉起 langchain 的角色装配栈，纯件测试不该为它付入口成本。
    from ..agents import run_direct_call  # noqa: PLC0415 - 避免入口期拉起角色装配栈

    # 首次 + 最多两次重问。每次回灌都从**原始请求**重新拼（附上最新一轮的超限清单），
    # 而不是在上一次请求上叠加：叠加会把前几轮的清单越滚越长，模型更容易照抄旧回复。
    request_now = request
    requeues_left = MAX_BUDGET_REQUEUES
    while True:
        result = run_direct_call(llm, request_now, role=DIRECTOR_PLANNER_ROLE)
        if not result.ok:
            raise RuntimeError(result.error)
        planning = _planning_result(result.text, total_s, mode=mode, registry=registry,
                                    subject_definitions=subject_definitions)
        # 非 r2v 的预算是空表，这一支自然为假；锚不计数由 ``slot_count`` 保证。
        over = _over_budget_segments(planning.plan, planning.budget)
        placed = _placement_segments(planning.plan)
        if not over and not placed:
            return planning
        if requeues_left == 0:
            if placed:
                raise _placement_error(placed, over)
            raise _budget_overflow_error(over)
        requeues_left -= 1
        # 两种问题同时出现时两块都附上：只报一种会让模型修完一处又带着另一处回来，
        # 白烧一次重问预算（#196）。它们仍然共用这一个 ``requeues_left``。
        blocks = [
            text for text in (
                _render_budget_feedback(over) if over else "",
                _render_placement_feedback(placed) if placed else "",
            ) if text
        ]
        request_now = f"{request}\n\n" + "\n\n".join(blocks)


def write_plan_json(path: Path, plan: DirectorPlan) -> Path:
    """把段表写成 ``plan.json``（形态见设计 §3.2）。

    写之前**不**做校验：段表由 ``parse_director_plan`` 的确定性层装配，坏表应该
    在装配处就报错；读回才是第二道闸（续跑从磁盘拿表）。
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(plan.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return path


def _require_int(raw: dict, key: str, where: str) -> int:
    value = raw.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{where} 的 {key} 必须是整数帧，实际 {value!r}")
    return value


def read_plan_json(path: Path) -> DirectorPlan:
    """严格读回 ``plan.json``：未知类型 / 非法帧数 / 合计不符一律 ``ValueError``。

    为什么严格：续跑要照**当初定案的那张表**继续（同一教训见
    ``SegmentPlan.from_dict``）。导演台的帧数是执行契约（导出帧决定成片总长），
    一处算错不会自己露馅——只会让成片短掉或长掉几帧。
    """
    path = Path(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"plan.json 读不出：{exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("plan.json 顶层必须是对象")
    if data.get("route") != "director":
        raise ValueError(f"plan.json 的 route 不是 director：{data.get('route')!r}")
    mode = data.get("mode")
    if mode not in PLAN_MODES:
        raise ValueError(
            f"plan.json 的模式无效：{mode!r}（应为 {'/'.join(PLAN_MODES)} 之一）")
    if data.get("fps") != FPS:
        raise ValueError(f"plan.json 的 fps 必须是 {FPS}，实际 {data.get('fps')!r}")
    total_frames = data.get("total_frames")
    if isinstance(total_frames, bool) or not isinstance(total_frames, int) or total_frames <= 0:
        raise ValueError(f"plan.json 的 total_frames 必须是正整数，实际 {total_frames!r}")
    raw_segments = data.get("segments")
    if not isinstance(raw_segments, list) or not raw_segments:
        raise ValueError("plan.json 缺 segments 数组（或为空）")

    segments: list[DirectorSegment] = []
    for position, item in enumerate(raw_segments, 1):
        where = f"第 {position} 段"
        if not isinstance(item, dict):
            raise ValueError(f"{where}不是 JSON 对象")
        segment_type = item.get("type")
        if segment_type not in SEGMENT_TYPES:
            raise ValueError(
                f"{where}未知段类型：{segment_type!r}"
                f"（应为 {'/'.join(SEGMENT_TYPES)} 之一）")
        if segment_type not in segment_types_for_mode(mode):
            # 读回也要按模式校验：旧表或手改过的表在续跑时同样不许带进 i2v 的运镜段
            # （ADR 0026）。判据与规划期同源（``segment_types_for_mode``）。
            raise ValueError(
                f"{where}类型 {segment_type!r} 不属于 {mode} 模式："
                f"本模式只允许 {'/'.join(segment_types_for_mode(mode))}")
        index = item.get("index")
        # bool 是 int 的子类：True == 1 会让首段的 index 校验静默放行，得单独挡。
        if isinstance(index, bool) or not isinstance(index, int) or index != position:
            raise ValueError(f"{where}的 index 是 {index!r}，应为 {position}")
        start_frame = _require_int(item, "start_frame", where)
        end_frame = _require_int(item, "end_frame", where)
        declared = _require_int(item, "declared_frames", where)
        export = _require_int(item, "export_frames", where)
        if export <= 0 or declared <= 0 or start_frame < 0 or end_frame <= start_frame:
            raise ValueError(
                f"{where}帧数非法：start={start_frame} end={end_frame} "
                f"declared={declared} export={export}")
        guided = is_guided(segment_type)
        if item.get("guided") is not guided:
            raise ValueError(
                f"{where}的 guided={item.get('guided')!r} 与类型 {segment_type} 派生值 "
                f"{guided} 不符（引导开关只能从段类型派生）")
        # 换算与合法性只走 frames.py 那一份：这里再抄一次「-12 / 17 的倍数」就是
        # 同一契约两处口径，常量一改读回校验会静默失效。
        if not is_on_grid(declared):
            raise ValueError(
                f"{where}声明帧 {declared} 不在 {GRID_STEP}k+{GRID_OFFSET} 网格上："
                "后端与 UI 都按 durationSec 吸附，非网格值会被改写（ADR 0025）")
        try:
            expected_declared = declared_frames_for_export(export, guided=guided)
        except ValueError as exc:
            raise ValueError(f"{where}：{exc}") from exc
        if declared != expected_declared:
            raise ValueError(
                f"{where}声明帧 {declared} 与导出帧 {export} 不符（应为 {expected_declared}）")
        expected_images = list(give_images_for(segment_type, mode))
        if item.get("give_images") != expected_images:
            raise ValueError(
                f"{where}给图清单 {item.get('give_images')!r} 与类型派生的 "
                f"{expected_images!r} 不符（给图清单不接受手写）")
        end_hook = item.get("end_hook")
        if not isinstance(end_hook, str) or not end_hook.strip():
            raise ValueError(f"{where}缺 end_hook：写段（#103）与尾帧变换（#107）要拿它当约束")
        segments.append(DirectorSegment(
            index=position,
            type=segment_type,
            guided=guided,
            start_frame=start_frame,
            end_frame=end_frame,
            declared_frames=declared,
            export_frames=export,
            summary=str(item.get("summary", "")),
            end_hook=end_hook,
            give_images=tuple(expected_images),
        ))

    # 整表规则（段数上限、首段必须是开场）也要在读回时跑一遍：手改过的表、或按旧口径
    # 留下的 5 段 i2v 表（2026-10-30 那一版假定 i2v 能靠引导续接），续跑时同样不许
    # 带进打包——那时的表会让插件在建计划期抛 ``Segment #2 has no source image``。
    validate_segment_types([segment.type for segment in segments], mode)
    if sum(segment.export_frames for segment in segments) != total_frames:
        raise ValueError(
            f"导出合计 {sum(segment.export_frames for segment in segments)} 帧 ≠ "
            f"总帧 {total_frames} 帧（帧为真源，合计必须精确）")
    cursor = 0
    for segment in segments:
        if segment.start_frame != cursor:
            raise ValueError(
                f"第 {segment.index} 段起点帧 {segment.start_frame} 与累计导出帧 {cursor} "
                "不一致")
        cursor = segment.end_frame
    if cursor != total_frames:
        raise ValueError(
            f"第 {segments[-1].index} 段帧区间 {segments[-1].start_frame}–"
            f"{segments[-1].end_frame} 与总帧 {total_frames} 不一致（末段必须收在总帧）")
    return DirectorPlan(mode=mode, total_frames=total_frames, segments=tuple(segments))


_IMAGE_LABELS = {
    IMAGE_FIRST: "首帧",
    IMAGE_END: "尾帧（终点画面）",
    # r2v 每段挂的是**本段素材子集**（≤ MAX_PICTURES，插件单请求上限）：标签里带上
    # 这张上限，用户看到「参考素材」时就知道这不是「全片素材一口气挂上去」。
    # 数字从常量来：官方上限一改，这句用户可见的措辞不会留在旧值上。
    IMAGE_ASSETS: f"参考素材（本段 ≤{MAX_PICTURES} 张）",
}


def render_give_images(segment: DirectorSegment, mode: str, *,
                       single_segment: bool = False,
                       film_end: bool = False) -> str:
    """给图清单的中文展示（段表清单与写段出站清单**同一份**，不各写一份 token 翻译）。

    ``single_segment`` / ``film_end`` 只影响 fl2v 开场的尾帧槽（#147）：全片尾帧在
    单段片子里就是这一段的终点画面，多段时属于最后一段。收口在 ``optional_images_for``
    里——同一个 ``single_segment and film_end`` 判据在这里再求一次值，只为决定后缀
    措辞（兑现时不写「（可选）」），不另立一条段数规则。
    """
    required = [_IMAGE_LABELS[token] for token in segment.give_images]
    optional = [f"{_IMAGE_LABELS[token]}{'' if single_segment and film_end else '（可选）'}"
                for token in optional_images_for(segment.type, mode,
                                                 single_segment=single_segment,
                                                 film_end=film_end)]
    if not required and not optional:
        # 引导段靠上一段末帧接续，不是「纯文字」：后者会让用户以为这一段没有任何
        # 画面来源（2026-10-30 的 i2v 给图改动暴露了这句）。i2v 单段制下不会走到
        # 这里（唯一段是 opening，必给首帧），t2v/fl2v 的引导段仍靠它；r2v 每段都有
        # 参考素材（必填），走不到这一支。
        if segment.guided:
            return "无（只靠段间引导）"
        return "无（纯文字）"
    return " + ".join(required + optional)


def render_plan_listing(plan: DirectorPlan, *, film_end: bool = False,
                        budget: AssetBudget | None = None) -> str:
    """出站分段清单：逐段展示段号 / 类型 / 声明帧 / 导出帧 / 给图清单（AC3）。

    段类型误判会让该段**静默丢弃首帧**（spec 的风险清单），所以「类型 + 给图」必须
    与帧数排在同一行，让用户一眼能核；总长说明（AC4：目标与可达总量的差）跟在合计
    之后，不埋在别处。

    ``film_end`` 由调用方从盘上现查（``pack.film_end_still``）：清单与写段请求必须
    说同一件事，否则单段 fl2v 会在清单里「只有首帧」、在请求里「首+尾」。

    ``budget`` 是 r2v 的段内素材预算（#186）；给了就在每段后面写出本段挑了几张图。
    规划师没挑图的段必须**点名**「未挑图」：空预算本身不是错误（票面 AC6），但用户
    得看见它是空的，否则会以为这一段的参考图后面会自己出现。
    """
    lines = [
        f"导演台分段清单（{plan.mode} · 总 {plan.total_frames} 帧 / "
        f"{plan.total_s:.3f}s · {len(plan.segments)} 段）",
    ]
    single_segment = len(plan.segments) == 1
    for segment in plan.segments:
        images = render_give_images(segment, plan.mode,
                                    single_segment=single_segment, film_end=film_end)
        line = (
            f"  段{segment.index} {segment_type_label(segment.type)}  "
            f"声明 {segment.declared_frames} / 导出 {segment.export_frames}  "
            f"给图：{images}")
        if budget is not None:
            line += f"  素材预算：{_budget_summary(budget.for_segment(segment.index))}"
        lines.append(line)
    total = sum(segment.export_frames for segment in plan.segments)
    lines.append(f"  合计导出 {total} 帧 = 总帧 {plan.total_frames} 帧")
    for note in plan.notes:
        lines.append(f"  说明：{note}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 段内素材预算（#186 / ADR 0029）：规划期给定「每段能用哪几张图」
# ---------------------------------------------------------------------------
# 与 ``plan.json`` 同目录、按**段号**对齐；``plan.json`` 不存复本——两份各存一份预算
# 迟早会各推一套口径（ADR 0029 决定 1）。槽位数只看 ``pictures``；``anchors`` 不挂图、
# 不计数，只留溯源。
ASSET_BUDGET_FILENAME = "asset-budget.json"

# 出镜图的角色标记。目前只有这一种取值：锚根本**不进** ``pictures``（它不占槽），
# 所以 ``role`` 不是分类器，而是把「这条记录代表要画出来的图」写进产物供下游读取。
BUDGET_ROLE_DEPICTED = "depicted"


@dataclass(frozen=True)
class BudgetPicture:
    """本段要**挂上去**的一张参考图（占槽）。``number`` 是全局登记号。"""

    number: int
    name: str
    role: str = BUDGET_ROLE_DEPICTED

    def to_dict(self) -> dict:
        return {"number": self.number, "name": self.name, "role": self.role}

    @classmethod
    def from_dict(cls, raw: object, where: str) -> "BudgetPicture":
        if not isinstance(raw, dict):
            raise ValueError(f"{where} 的条目必须是对象：{raw!r}")
        number = raw.get("number")
        # bool 是 int 的子类：True 会让编号校验静默放行，单独挡。
        if isinstance(number, bool) or not isinstance(number, int):
            raise ValueError(f"{where} 的 number 必须是整数：{number!r}")
        return cls(number=number, name=str(raw.get("name", "")),
                   role=str(raw.get("role", BUDGET_ROLE_DEPICTED)))


@dataclass(frozen=True)
class BudgetAnchor:
    """本段**不占槽**的一致性锚：图不挂，只留语义名与「被哪条定义勾进来」。"""

    number: int
    name: str
    via: str = ""

    def to_dict(self) -> dict:
        return {"number": self.number, "name": self.name, "via": self.via}

    @classmethod
    def from_dict(cls, raw: object, where: str) -> "BudgetAnchor":
        if not isinstance(raw, dict):
            raise ValueError(f"{where} 的条目必须是对象：{raw!r}")
        number = raw.get("number")
        if isinstance(number, bool) or not isinstance(number, int):
            raise ValueError(f"{where} 的 number 必须是整数：{number!r}")
        return cls(number=number, name=str(raw.get("name", "")),
                   via=str(raw.get("via", "")))


@dataclass(frozen=True)
class SegmentBudget:
    """一段的素材预算：出镜（占槽）与锚（不占槽）分开存。"""

    pictures: tuple[BudgetPicture, ...] = ()
    anchors: tuple[BudgetAnchor, ...] = ()

    @property
    def slot_count(self) -> int:
        """占槽张数：**只看 pictures**，锚不参与（ADR 0029 决定 2）。"""
        return len(self.pictures)

    def to_dict(self) -> dict:
        return {
            "pictures": [item.to_dict() for item in self.pictures],
            "anchors": [item.to_dict() for item in self.anchors],
        }

    @classmethod
    def from_dict(cls, raw: object, where: str) -> "SegmentBudget":
        if not isinstance(raw, dict):
            raise ValueError(f"{where} 必须是对象：{raw!r}")
        pictures = raw.get("pictures")
        if not isinstance(pictures, list):
            raise ValueError(f"{where} 缺 pictures 数组：{pictures!r}")
        # 锚是 #183 才有的概念；缺键＝没有锚（不把旧产物读废）。
        anchors = raw.get("anchors", [])
        if not isinstance(anchors, list):
            raise ValueError(f"{where} 的 anchors 必须是数组：{anchors!r}")
        return cls(
            pictures=tuple(BudgetPicture.from_dict(item, f"{where}.pictures")
                           for item in pictures),
            anchors=tuple(BudgetAnchor.from_dict(item, f"{where}.anchors")
                          for item in anchors),
        )


@dataclass(frozen=True)
class AssetBudget:
    """整份段内素材预算：段号 → 该段预算。

    空表有两层含义，调用方按 ``segments`` 是否为空区分：非 r2v（没有参考素材）与
    旧会话（盘上没有预算文件）。r2v 规划出来的预算**每段都有一条**（没挑图就是空
    ``pictures``），所以「有预算但没挑图」与「根本没有预算」不会混成一句话。
    """

    segments: Mapping[int, SegmentBudget] = field(default_factory=dict)

    def for_segment(self, index: int) -> SegmentBudget | None:
        return self.segments.get(int(index))

    def to_dict(self) -> dict[str, dict]:
        return {str(index): self.segments[index].to_dict()
                for index in sorted(self.segments)}

    @classmethod
    def from_dict(cls, raw: object, where: str = ASSET_BUDGET_FILENAME) -> "AssetBudget":
        if not isinstance(raw, dict):
            raise ValueError(f"{where} 顶层必须是对象（段号 → 段预算）")
        segments: dict[int, SegmentBudget] = {}
        for key, value in raw.items():
            try:
                index = int(key)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{where} 的段号必须是整数：{key!r}") from exc
            if index <= 0:
                raise ValueError(f"{where} 的段号必须从 1 起：{key!r}")
            segments[index] = SegmentBudget.from_dict(value, f"{where} 第 {index} 段")
        return cls(segments=segments)


@dataclass(frozen=True)
class DirectorPlanningResult:
    """规划入口的两个**独立**结果：段表与预算。

    ``plan.json`` 形态不变、不存预算复本（ADR 0029 决定 6），所以预算不能挂在
    ``DirectorPlan`` 上随计划序列化；调用方拿到后在**写段之前**单独落盘（#186）。
    """

    plan: DirectorPlan
    budget: AssetBudget


def asset_budget_path(session_dir: Path) -> Path:
    """预算落点：``GENxxx/director/asset-budget.json``（与 ``plan.json`` 同目录）。"""
    return Path(session_dir) / DIRECTOR_DIRNAME / ASSET_BUDGET_FILENAME


def render_material_list(registry: Iterable[Sequence]) -> str:
    """规划请求里的「素材清单」块（#186）：``<Picture N> = 语义名``，一行一张。

    清单是规划师挑图的**唯一来源**：它只按编号写 ``assets``，中间不引入第二套标识
    （语义名会改，编号不会）。台账为空时整块省略——给一张空表只会让它编号。
    """
    rows = [f"<Picture {int(item[0])}> = {item[1]}" for item in registry]
    if not rows:
        return ""
    return (
        "\n素材清单（编号 = 语义名；给每段挑图时在 assets 里写编号）：\n"
        + "\n".join(rows)
        + "\n"
    )


def build_asset_budget(plan: DirectorPlan, planner_assets: Mapping[int, Sequence[int]], *,
                       subject_definitions: str,
                       registry: Iterable[Sequence]) -> AssetBudget:
    """规划师的 ``assets`` → 预算：闭包 → 锚降级 → 出镜/锚分栏（#186 / ADR 0029）。

    闭包与降级都调 ``segment_refs`` 的**同一份**实现（#181/#183）：规划期算预算与
    写段期算子集必须同源，否则同一张图会在两个阶段被分成两类，而且不报错。

    **没挑图 = 空预算**，直接跳过闭包：``compute_asset_closure`` 的「种子为空→保留
    全部登记图」是写段期「分不清用什么」的兜底，用在预算上会把整份台账塞进每段
    （#180 事故形态：28 张全量）。这里是「规划师明确没挑」，语义不同。

    未登记的图号是契约违规（规划师只能按清单挑）：预算里塞一个挂不上的号，写段与
    打包会对不上，所以在**落盘之前**抛 ``ValueError`` 点名段号与图号。
    """
    # 延迟导入：``segment_refs`` 拉配置栈，纯段表用例不该为它付入口成本。
    from ..segment_refs import compute_asset_closure, demote_consistency_anchors  # noqa: PLC0415

    rows = [(int(item[0]), str(item[1])) for item in registry]
    names = dict(rows)
    segments: dict[int, SegmentBudget] = {}
    for segment in plan.segments:
        numbers = tuple(int(number) for number in planner_assets.get(segment.index, ()))
        if not numbers:
            segments[segment.index] = SegmentBudget()
            continue
        unknown = [number for number in numbers if number not in names]
        if unknown:
            listed = "、".join(str(number) for number in unknown)
            known = "、".join(str(number) for number in sorted(names)) or "（空台账）"
            raise ValueError(
                f"第 {segment.index} 段的 assets 含未登记图号 {listed}："
                f"素材清单里只有 {known}。规划师只能按清单挑图（编号逐字照抄，"
                "不得自编）")
        closure = compute_asset_closure(
            seed_pictures=numbers, subject_definitions=subject_definitions,
            registry=rows)
        depicted, anchors = demote_consistency_anchors(closure, names=names)
        segments[segment.index] = SegmentBudget(
            pictures=tuple(BudgetPicture(number=number, name=names[number])
                           for number in depicted),
            anchors=tuple(BudgetAnchor(number=item.original_picture, name=item.name,
                                       via=item.via)
                          for item in anchors),
        )
    return AssetBudget(segments=segments)


def _planner_assets(text: str) -> dict[int, tuple[int, ...]]:
    """从**已通过** ``parse_director_plan`` 校验的同一份回复里取每段 ``assets``。

    缺键＝规划师没挑图（票面 AC6：不崩溃、按空预算落盘），不当作契约违规；类型不对
    则抛 ``ValueError``——把字符串或布尔当编号会让预算写出一个错误图号。
    """
    data = _extract_json(text)
    raw_segments = data.get("segments") if isinstance(data, dict) else None
    if not isinstance(raw_segments, list):
        return {}
    assets: dict[int, tuple[int, ...]] = {}
    for position, item in enumerate(raw_segments, 1):
        if not isinstance(item, dict):
            continue
        raw = item.get("assets")
        if raw is None:
            continue
        if not isinstance(raw, list):
            raise ValueError(
                f"第 {position} 段的 assets 必须是 Picture 编号数组：{raw!r}")
        numbers: list[int] = []
        for value in raw:
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(
                    f"第 {position} 段的 assets 里 {value!r} 不是整数编号："
                    "只许写素材清单里的 <Picture N> 的 N")
            numbers.append(value)
        assets[position] = tuple(numbers)
    return assets


def _planning_result(text: str, total_s: float, *, mode: str,
                     registry: Iterable[Sequence],
                     subject_definitions: str) -> DirectorPlanningResult:
    """解析回复并算预算；非 r2v 没有参考素材，交回空预算（不写文件）。"""
    plan = parse_director_plan(text, total_s, mode=mode)
    if mode != "r2v":
        return DirectorPlanningResult(plan=plan, budget=AssetBudget())
    return DirectorPlanningResult(
        plan=plan,
        budget=build_asset_budget(
            plan, _planner_assets(text),
            subject_definitions=subject_definitions, registry=registry))


def write_asset_budget(session_dir: Path, budget: AssetBudget) -> Path | None:
    """落 ``director/asset-budget.json``；没有段预算（非 r2v）时**不写文件**。

    空表不写 ``{}``：``load_asset_budget`` 读不到文件＝旧会话（#189 要据此说「本会话
    无预算，未做预算校验」），而 r2v 的「每段都没挑图」是**有**预算文件的空条目——
    两种情形不能混成一句话。
    """
    if not budget.segments:
        return None
    path = asset_budget_path(session_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(budget.to_dict(), ensure_ascii=False, indent=2),
                    encoding="utf-8")
    return path


def load_asset_budget(session_dir: Path) -> AssetBudget:
    """读回预算；文件不在＝旧会话，返回空表（不是错误）。

    形态坏了要响（``ValueError``）：续跑照坏预算挂图就是接错线，而预算还要被打包期
    消费（#189）——静默读成空表会让「没预算」与「预算坏了」变成同一句话。
    """
    path = asset_budget_path(session_dir)
    if not path.is_file():
        return AssetBudget()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"{ASSET_BUDGET_FILENAME} 读不出：{exc}") from exc
    return AssetBudget.from_dict(raw)


def _budget_summary(entry: SegmentBudget | None) -> str:
    """清单里的一句话摘要：没挑图就点名，别让空预算看起来像「还没显示」。"""
    if entry is None or not entry.pictures:
        return "未挑图"
    text = f"{entry.slot_count} 张"
    if entry.anchors:
        text += f"（锚 {len(entry.anchors)}）"
    return text


class AssetBudgetOverflowError(ValueError):
    """规划期某段的素材预算仍超过每段上限（#187 / ADR 0029 决定 5）。

    单独立一个类型是为了让调用方能把它与「回复格式不合格」分开说：前者要用户动
    素材或剧情分配，后者是模型没守输出契约。它仍然继承 ``ValueError``——向导与
    续跑入口的 ``except (RuntimeError, ValueError)`` 必须照常接住，否则一次超限
    会把整个流程炸穿。
    """


def _over_budget_segments(
    plan: DirectorPlan, budget: AssetBudget,
) -> list[tuple[int, SegmentBudget]]:
    """超限段（按段号）：**只看 ``pictures`` 张数**，锚不占槽（ADR 0029 决定 2）。"""
    over: list[tuple[int, SegmentBudget]] = []
    for segment in plan.segments:
        entry = budget.for_segment(segment.index)
        if entry is not None and entry.slot_count > MAX_PICTURES:
            over.append((segment.index, entry))
    return over


def _placement_segments(plan: DirectorPlan) -> list[tuple[int, str]]:
    """判为摆位的段（段号 + ``end_hook`` 原文），按段号顺序（#196）。"""
    return [(segment.index, segment.end_hook) for segment in plan.segments
            if is_placement_end_hook(segment.end_hook)]


def _render_placement_feedback(placed: Sequence[tuple[int, str]]) -> str:
    """摆位回灌（#196）：点名段号与原文，只给「改成动作在途中」这一条可执行指向。"""
    still = "／".join(END_HOOK_STILL_WORDS)
    motion = "／".join(END_HOOK_MOTION_WORDS)
    lines = ["【end_hook 是静止摆位，请改成动作在途中后重写整份段表】"]
    for index, hook in placed:
        lines.append(
            f"第 {index} 段的 end_hook：「{hook}」——只写了静止谓词（{still}），"
            f"没有任何位移或动作谓词（{motion}）。")
    lines.append(
        "end_hook 是本段切开的最后一帧，不是终点构图：请让动作在途中"
        "（人物正在走、推、转向……），不要写成位置摆好、画面静止。")
    # 回复会被整份重新解析：只说「改第 N 段」会让其余段从回复里消失（与预算回灌同理）。
    lines.append("请重新给出完整段表 JSON（其余段原样保留，不要只给改动的那一段）。")
    return "\n".join(lines)


class EndHookPlacementError(ValueError):
    """两次重问后 ``end_hook`` 仍是静止摆位（#196）。

    继承 ``ValueError`` 与 ``AssetBudgetOverflowError`` 同理：向导与续跑入口的
    ``except (RuntimeError, ValueError)`` 必须照常接住，否则一次摆位会把流程炸穿。
    """


def _placement_error(placed: Sequence[tuple[int, str]],
                     over: Sequence[tuple[int, SegmentBudget]]) -> EndHookPlacementError:
    """回灌用尽仍摆位：点名段号与原文，交用户决定（代码不替它改写分镜）。"""
    details = "；".join(f"第 {index} 段「{hook}」" for index, hook in placed)
    message = (
        f"end_hook 仍是静止摆位：{details}。"
        f"已把摆位清单回灌给规划师重写 {MAX_BUDGET_REQUEUES} 次，仍是摆位。"
        "判据是「含静止谓词且不含任何动作谓词」；end_hook 是本段切开的最后一帧，"
        "要写成动作在途中，不要写成位置摆好、画面静止。")
    if over:
        # 两块问题同时存在时别把预算那块吞掉：用户要一次看到两条才能一起改。
        message += "\n" + str(_budget_overflow_error(over))
    return EndHookPlacementError(message)


def _budget_picture_list(entry: SegmentBudget) -> str:
    """完整清单（编号 + 语义名）：编号供规划师照抄，名字让人读懂是哪几张。"""
    return "、".join(f"<Picture {item.number}>「{item.name}」"
                     for item in entry.pictures)


def _render_budget_feedback(over: Sequence[tuple[int, SegmentBudget]]) -> str:
    """超限回灌（#187 / 用户故事 6、7）：只给两条**真能执行**的路。

    不提「把这一段再拆短」：4–15 秒硬下限加 17k+5 网格决定规划之后的段没有拆段
    自由，那句话只会把模型逼去改时长凑数（ADR 0029 决定 4）。
    """
    lines = [f"【素材预算超限，请重挑后重写整份段表：每段最多 {MAX_PICTURES} 张】"]
    for index, entry in over:
        lines.append(
            f"第 {index} 段列了 {entry.slot_count} 张（{_budget_picture_list(entry)}），"
            f"上限 {MAX_PICTURES}。请二选一：把其中一张改为纯文字描述，"
            "或调整本段与相邻段的剧情分配。")
    # 回复会被整份重新解析：只说「改第 N 段」会让其余段从回复里消失。
    lines.append("请重新给出完整段表 JSON（其余段原样保留，不要只给改动的那一段）。")
    return "\n".join(lines)


def _budget_overflow_error(
    over: Sequence[tuple[int, SegmentBudget]],
) -> AssetBudgetOverflowError:
    """两次重问后仍超：点名段号与完整清单，交用户决定（代码不代劳）。"""
    details = "；".join(
        f"第 {index} 段 {entry.slot_count} 张（{_budget_picture_list(entry)}）"
        for index, entry in over)
    return AssetBudgetOverflowError(
        f"素材预算超过每段上限 {MAX_PICTURES} 张：{details}。"
        f"已把超限清单回灌给规划师重挑 {MAX_BUDGET_REQUEUES} 次，仍超过上限。"
        "代码不自动截断到上限，也不替你整片重规划（ADR 0029）："
        "请手动精简这些素材，或调整本段与相邻段的剧情分配后重跑规划。")

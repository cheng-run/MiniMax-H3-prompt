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
from pathlib import Path
from typing import Any

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
    optional_images_for,
    segment_type_label,
    validate_segment_types,
)
# 关键帧读图块与 JSON 抓取都**复用 base/ref 那一份**（同一段文本、同一套宽容度）：
# 导演台只是第三条路线，不是另一套模型输出约定。``_extract_json`` 是私有名但语义
# 稳定、无副作用，重抄一份会让两条路对模型输出的宽容度各自漂。
from ..segment_planner import FRAME_CONTEXT_HEADER, _extract_json

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"
_INSTRUCTION_NAME = "segment_planner_director.md"

# 直调路径在事件与成本表里的角色名：**必须**与 ``role_segment_planner`` 分开
# （ADR 0019 的记账口径）——混在一处就分不清钱花在哪条路线上。
DIRECTOR_PLANNER_ROLE = "role_director_segment_planner"

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
            f"导演台模式 {mode!r} 本票不做叙事规划（一期只支持 {'/'.join(PLAN_MODES)}；"
            "v2v/rv2v 的段边界取自源视频时间轴见 #109、mixed 见 #110）")
    return mode


def _require_number(value: object, what: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{what} 必须是数字秒，实际 {value!r}")
    return float(value)


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
    for position, item in enumerate(raw_segments, 1):
        if not isinstance(item, dict):
            raise ValueError(f"第 {position} 段不是 JSON 对象：{item!r}")
        segment_type = item.get("type")
        if not isinstance(segment_type, str) or segment_type not in SEGMENT_TYPES:
            raise ValueError(
                f"第 {position} 段类型非法：{segment_type!r}"
                f"（应为 {'/'.join(SEGMENT_TYPES)} 之一；缺失同样算非法）")
        start = _require_number(item.get("start_s"), f"第 {position} 段 start_s")
        end = _require_number(item.get("end_s"), f"第 {position} 段 end_s")
        if end - start <= 0:
            raise ValueError(f"第 {position} 段窗口 {start}–{end} 非正时长")
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
    validate_segment_types(types)
    for position, (start, end) in enumerate(windows, 1):
        if position > 1 and abs(start - windows[position - 2][1]) > _BOUNDARY_TOLERANCE:
            raise ValueError(
                f"第 {position} 段起点 {start} 必须等于上一段终点 "
                f"{windows[position - 2][1]}（段表不许有缝/重叠）")
        if not hooks[position - 1].strip():
            raise ValueError(
                f"第 {position} 段缺 end_hook：它是本段落定的画面状态，"
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
    exports, note = land_exports(types, raw_frames, target_frames)
    segments = _build_segments(mode, types, exports, summaries, hooks)
    # plan.json 的 ``total_frames`` 是**实际落位**（成片坐标），不是目标：读回校验
    # 要求 ``Σ export == total_frames``，而目标只出现在出站说明里（ADR 0025）。
    landed_frames = sum(exports)
    if segments[-1].end_frame != landed_frames:
        # land_exports 已保证 Σ==落位总量；这里是同一条契约的第二道读法，防将来
        # 有人改落位函数时两处口径悄悄分家。
        raise ValueError(
            f"段表累计导出帧 {segments[-1].end_frame} 与落位总量 {landed_frames} 不一致")
    return DirectorPlan(mode=mode, total_frames=landed_frames, segments=segments,
                        notes=(note,) if note else ())


def plan_director_segments(shot_table: str, total_s: float, llm, *, mode: str,
                           frame_context: str = "",
                           revision_context: str = "") -> DirectorPlan:
    """调 LLM 规划导演台分段（直调走看门狗，与 ``plan_segments`` 同形）。

    失败两分法沿用 ADR 0019：**调用失败**（卡住到上限 / 中流断流 / 其他异常）抛
    ``RuntimeError``（带病因与次数），**回复不合格**抛 ``ValueError``。这条路线没有
    机械回退，两种病都交回调用方决定怎么说。

    ``frame_context`` / ``revision_context`` 与 ``plan_segments`` 同源同义：真实
    关键帧读图结果压过分镜表，用户累积修订按注入优先序进来。本函数**不依赖 state**，
    「规划层到底看到了什么」在调用点一眼可见（issue #26 的教训）。
    """
    _require_mode(mode)
    total_s = _require_number(total_s, "视频总时长")
    total_frames = round(total_s * FPS)
    frame_block = (
        f"\n{FRAME_CONTEXT_HEADER}\n{frame_context}\n"
        if frame_context.strip() else ""
    )
    revision_block = f"\n{revision_context}\n" if revision_context.strip() else ""
    request = (
        f"{load_instruction()}\n\n"
        f"视频总时长：{total_s:g} 秒（总帧 {total_frames} 帧，{FPS}fps）\n"
        f"{frame_block}"
        f"{revision_block}"
        f"\n分镜表：\n{shot_table}"
    )
    # 延迟导入：``agents`` 会拉起 langchain 的角色装配栈，纯件测试不该为它付入口成本。
    from ..agents import run_direct_call  # noqa: PLC0415 - 避免入口期拉起角色装配栈

    result = run_direct_call(llm, request, role=DIRECTOR_PLANNER_ROLE)
    if not result.ok:
        raise RuntimeError(result.error)
    return parse_director_plan(result.text, total_s, mode=mode)


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
            f"plan.json 的模式 {mode!r} 不是一期可规划的模式（{'/'.join(PLAN_MODES)}）")
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
    IMAGE_ASSETS: "参考素材",
}


def render_give_images(segment: DirectorSegment, mode: str) -> str:
    """给图清单的中文展示（段表清单与写段出站清单**同一份**，不各写一份 token 翻译）。"""
    required = [_IMAGE_LABELS[token] for token in segment.give_images]
    optional = [f"{_IMAGE_LABELS[token]}（可选）"
                for token in optional_images_for(segment.type, mode)]
    if not required and not optional:
        if segment.type == "continue":
            return "无（只靠段间引导）"
        return "无（纯文字）"
    return " + ".join(required + optional)


def render_plan_listing(plan: DirectorPlan) -> str:
    """出站分段清单：逐段展示段号 / 类型 / 声明帧 / 导出帧 / 给图清单（AC3）。

    段类型误判会让该段**静默丢弃首帧**（spec 的风险清单），所以「类型 + 给图」必须
    与帧数排在同一行，让用户一眼能核；总长说明（AC4：目标与可达总量的差）跟在合计
    之后，不埋在别处。
    """
    lines = [
        f"导演台分段清单（{plan.mode} · 总 {plan.total_frames} 帧 / "
        f"{plan.total_s:.3f}s · {len(plan.segments)} 段）",
    ]
    for segment in plan.segments:
        lines.append(
            f"  段{segment.index} {segment_type_label(segment.type)}  "
            f"声明 {segment.declared_frames} / 导出 {segment.export_frames}  "
            f"给图：{render_give_images(segment, plan.mode)}")
    total = sum(segment.export_frames for segment in plan.segments)
    lines.append(f"  合计导出 {total} 帧 = 总帧 {plan.total_frames} 帧")
    for note in plan.notes:
        lines.append(f"  说明：{note}")
    return "\n".join(lines)

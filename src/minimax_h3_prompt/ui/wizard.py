"""两阶段生产流向导。

阶段 1：主题/时长/风格 → 管线跑到声音设计 → 首尾帧生图提示词落进会话目录 → 人工确认/意见循环。
阶段 2：提交真实帧图片（缺帧自动降级 I2VA/L2VA/T2VA）→ 暂存进会话 frames/ → qwen3.7-plus 读图
→ 视频提示词组装 + QA 精修。

两阶段之间可关终端：阶段 1 的 state 经 session_store 落盘，重启后自动发现待续接会话。
"""
from __future__ import annotations

import math
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Callable, NamedTuple

if TYPE_CHECKING:  # 返回注解用；运行时经函数级导入取（防环，brief_parser 同款）
    from collections.abc import Sequence

    from ..asset_loop import QueueItem
    from ..injection_card import VideoOutputParams
    from ..ref_segments import RefSegmentJob, RefSegmentResult

from ..brief_parser import Brief, RefItem
from ..config import Config
from ..generation import canvas_display, render_fl2va_frame_markdown
from ..graph.pipeline import (
    rerun_silent_nodes,
    run_stage1,
    run_stage2,
    stage1_rerun_plan,
)
from ..observability import COST_TABLE_KEY, token_meter
from ..resume_point import forget as forget_answer
from ..resume_point import recall as recall_answer
from ..resume_point import remember as remember_answer
from ..session_store import (
    STATUS_AWAITING_FRAMES,
    STATUS_COMPLETED,
    STATUS_REF_SEGMENTS_RUNNING,
    STATUS_SEGMENTED_RUNNING,
    STATUS_STAGE1_RUNNING,
    SessionState,
    find_awaiting_sessions,
    load_session,
    save_session,
)
from ..user_revisions import (
    FRAME_BASELINE_KEY,
    FRAME_REVOKED_KEY,
    LAYER_FRAME,
    LAYER_LABELS,
    LAYER_SPECS,
    SETTING_LAYERS,
    SETTING_REVISION_KEY,
    active_revisions,
    applied_revisions,
    begin_round,
    log_frame_round,
    mark_revision_applied,
    next_round,
    pending_revisions,
    record_user_revision,
    remove_user_revision,
    render_applied_list,
    render_baseline_block,
    render_revision_list,
    render_revocation_block,
    revocation_note,
)
from .progress import (
    TextProgress,
    event_scope,
    format_step_banner,
    step_scope,
)


def resolve_effective_variant(requested: str, have_first: bool, have_last: bool) -> str:
    """按用户请求变体 + 实际拥有的关键帧决定有效变体（缺帧降级矩阵，请求优先）。

    用户显式选 I2VA/L2VA 时保持该变体（即使提供了多余的帧也不静默升级）；
    FL2VA 按实际拥有帧降级；未知请求按 FL2VA 处理（兼容旧会话）。
    """
    requested = str(requested).upper()
    if requested == "I2VA":
        return "I2VA" if have_first else "T2VA"
    if requested == "L2VA":
        return "L2VA" if have_last else "T2VA"
    if requested == "T2VA":
        return "T2VA"
    # FL2VA（或未知）：拥有帧 → 变体
    if have_first and have_last:
        return "FL2VA"
    if have_first:
        return "I2VA"
    if have_last:
        return "L2VA"
    return "T2VA"


def _drain_stdin() -> None:
    """排空控制台输入缓冲里滞留的按键（生成期间误敲的回车等）。

    生成阶段是长阻塞，用户此时敲的键会滞留在行缓冲里，结束后被第一个
    input() 吞掉造成"跳问"。仅在真实交互终端生效；pytest/管道 stdin
    直接跳过，绝不触碰键盘缓冲。
    """
    try:
        if not sys.stdin.isatty():
            return
    except (ValueError, OSError):
        return
    try:
        import msvcrt  # 仅 Windows

        while msvcrt.kbhit():
            msvcrt.getwch()
    except ImportError:
        try:
            import termios

            termios.tcflush(sys.stdin.fileno(), termios.TCIFLUSH)
        except Exception:
            pass  # 宁可不排也不崩


def _prompt(text: str) -> str:
    # 每次提问前先排水：清掉阻塞期间滞留的旧按键，提示出现后的新输入不受影响。
    _drain_stdin()
    return input(text).strip()


def _record_answer(state: dict, key: str, value: str, *, generation_dir: Path,
                   brief: Brief) -> None:
    """记一条答案并**立刻写盘**（issue #51 续跑点：每答一问即落盘）。

    「中断后只补真正缺的部分」的全部机制就是这个函数：答案只要落进会话文件，
    重跑向导时 `_ask_*` 那一族就能把同一道题换成「沿用上次」。答完马上写、不等
    产物落定——窗口就在「答了但还没变成产物」那一段。
    """
    remember_answer(state, key, value)
    save_session(generation_dir, brief, state, status=STATUS_AWAITING_FRAMES)


def _asset_path_key(name: str) -> str:
    """素材交回路径的答案键（按语义名区分：一张一条，换版时各走各的）。"""
    return f"asset_path:{name}"


def _asset_cost_keys(name: str) -> tuple[str, str]:
    """外部生成成本（耗时秒/次数）的答案键（issue #52）：两个数各记一条，不拼串。

    键名取术语表的口径（``external_cost`` ＝ CONTEXT.md「外部生成成本」），不漂成
    「素材成本」那类近义词——漂了之后 grep 一个词搜不全同一件事（复审）。拼成
    ``90/3`` 那种串还要在两头各写一遍解析，格式一漂就对不上账；答案账本来就装字符串，
    分两条最省事。答过但还没入库的那笔成本住在这里，入库时挂到台账上再作废。
    """
    return f"external_cost_seconds:{name}", f"external_cost_attempts:{name}"


def _ask_asset_external_cost(name: str) -> tuple[float, int]:
    """问外部生成成本（ComfyUI 出图耗时秒/次数），两问都可留空＝不登记。

    系统看不到外部生成，这两个数只能由人报；每问独立跳过（只记得起耗时的人不必编次数）。
    非法输入重问，不静默当 0——静默吞掉就是账本上少一笔而没人知道。
    """
    seconds = 0.0
    while True:
        raw = _prompt(f"[外部成本·{name}] 出图耗时（秒，回车=跳过）：").strip()
        if not raw:
            break
        try:
            seconds = float(raw)
            # float() 会收下 inf/nan：它们进台账就不是「像样的数」了（同 bool 挡法；
            # 权威守卫在 asset_ledger._require_external_cost，这里只是别让人白填）
            if seconds < 0 or not math.isfinite(seconds):
                raise ValueError
            break
        except ValueError:
            print("  耗时要填非负数字（单张图的生成秒数），或回车跳过。")
    attempts = 0
    while True:
        raw = _prompt(f"[外部成本·{name}] 出图次数（回车=跳过）：").strip()
        if not raw:
            break
        if raw.isdigit():
            attempts = int(raw)
            break
        print("  次数要填非负整数（这张图掷了几次才满意），或回车跳过。")
    return seconds, attempts


def _pending_asset_cost(state: dict, name: str) -> tuple[float, int]:
    """读回答案账里这张图待挂的外部成本；没登记过返回 (0.0, 0)。"""
    seconds_key, attempts_key = _asset_cost_keys(name)
    seconds = float(recall_answer(state, seconds_key) or 0)
    attempts = int(recall_answer(state, attempts_key) or 0)
    return seconds, attempts


def _register_asset_external_cost(state: dict, brief: Brief, generation_dir: Path,
                                  item: QueueItem) -> None:
    """审查菜单第 5 项（issue #52）：登记这张图的外部生成成本。

    落点随产物走：**已入库** → 直接写台账（盘上那一版就是这里说的「这一版」，事后
    补登记走这条）；**还没入库** → 记进答案账，交回入库时随正本一起挂上（产物接答案
    的班）。再登记一次就是覆盖这次登记的成本，不问人「要不要清空」——那是把一句话
    拆成两问。
    """
    from ..asset_ledger import KIND_PICTURE, load_asset_ledger, record_external_cost

    seconds, attempts = _ask_asset_external_cost(item.name)
    if not (seconds or attempts):
        print(f"[外部成本·{item.name}] 未登记（两问都跳过了）。")
        return
    if load_asset_ledger(generation_dir).get(item.name, KIND_PICTURE) is not None:
        record_external_cost(generation_dir, item.name, KIND_PICTURE,
                             seconds=seconds, attempts=attempts)
        print(f"[外部成本·{item.name}] 已登记到台账（耗时 {seconds:g}s / 次数 {attempts}）。")
        return
    seconds_key, attempts_key = _asset_cost_keys(item.name)
    remember_answer(state, seconds_key, f"{seconds:g}")
    remember_answer(state, attempts_key, str(attempts))
    save_session(generation_dir, brief, state, status=STATUS_AWAITING_FRAMES)
    print(f"[外部成本·{item.name}] 已记下，交回入库时挂到这一版上。")


def _clean_path_input(raw: str) -> str:
    """清洗交互输入的文件路径：剥空白与首尾成对引号。

    Windows 资源管理器"复制为路径"自带双引号，直接 Path(raw) 会得到
    不存在的路径。只剥"首尾成对"的引号，路径中间内容原样保留——
    无引号输入的行为与清洗前完全一致。
    """
    text = raw.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in ("'", '"'):
        text = text[1:-1].strip()
    return text


def _confirm(text: str, default: bool = True) -> bool:
    suffix = " [Y/n]: " if default else " [y/N]: "
    while True:
        raw = _prompt(text + suffix).lower()
        if not raw:
            print(f"[提示] 未输入，按默认 {'是' if default else '否'} 处理。")
            return default
        if raw in ("y", "yes"):
            return True
        if raw in ("n", "no"):
            return False


def _choose_generation_mode() -> str:
    """阶段 1 视频生成方式四选一：首帧(I2VA)/尾帧(L2VA)/首尾帧(FL2VA，默认)/多参(REF2VA)。

    多参（issue #41）：与首尾帧**并存**的另一条路线，参考素材＋文本驱动、无桥接帧。
    返回 "REF2VA" 不是帧变体——由 ``_phase1_new`` 转成 ``Brief(mode="ref")``，
    帧逻辑一律按 ``FRAME_VARIANTS`` 判，不看它。
    """
    from ..workflow_config import workflow_settings

    # 登记名调用时现取（复审 S4）：换配置即换菜单文案，与注入卡同源。
    name = workflow_settings().ref_workflow_name
    print("请选择视频生成方式：")
    print("  1. 首帧生成视频（I2VA，以首帧图起笔，向前发展）")
    print("  2. 尾帧生成视频（L2VA，从合理开场逐步收敛到尾帧图）")
    print("  3. 首尾帧生成视频（FL2VA，首尾帧锚定，默认）")
    print(f"  4. 多参生成视频（ref2va，参考素材＋文本驱动；登记工作流：{name}）")
    while True:
        raw = _prompt("输入 1/2/3/4（回车=3 首尾帧）：").strip()
        if not raw:
            return "FL2VA"
        if raw in ("1", "2", "3", "4"):
            return {1: "I2VA", 2: "L2VA", 3: "FL2VA", 4: "REF2VA"}[int(raw)]
        print("无效输入，请输入 1/2/3/4。")


def _choose_canvases() -> dict[str, tuple[int, int]]:
    """生图侧画布选择器（issue #42）：选比例→选尺寸→不符可自定义，**逐模型家族**。

    「取代系统钦定的推荐尺寸」：比例没有回车默认档——必须显式选（坏输入重问），
    尺寸档不符走自定义自由输入（保留兜底）。每个现存家族各走一遍三步（Flux.2 全链
    退役后只剩 zimage 一家，issue #48——家族清单从默认表取，随表自动收缩）：
    构图词分套绑选定画布的**画幅族**（Q21(c)）。返回家族 → (宽, 高)，落进
    Brief.canvases，随请求【画布】块与产物画布行同源下发。
    """
    from ..generation import IMAGE_FAMILY_LABELS, IMAGE_PROFILE_DEFAULTS

    return {
        family: _choose_canvas(IMAGE_FAMILY_LABELS[family])
        for family in IMAGE_PROFILE_DEFAULTS
    }


def _menu_pick(title: str, count: int) -> int:
    """编号菜单的统一收口（选比例/选尺寸两步共用，复审点名的重复）。

    返回选中的 1..count+1（count+1 = 自定义逃生口）。坏输入重问，不静默取默认——
    画布是数据，错值必须当场拦（同 `parse_canvas_size` 的口径）。
    """
    top = count + 1
    while True:
        raw = _prompt(f"{title}，输入 1-{top}：").strip()
        if raw.isdigit() and 1 <= int(raw) <= top:
            return int(raw)
        print("无效输入，请重输。")


def _choose_canvas(label: str) -> tuple[int, int]:
    """单个家族的三步选择：第 1 步比例、第 2 步尺寸，档外/不符走自由输入。

    选定后回显**最终**画布（显示名+族类+分套指示）：自定义尺寸可以不合第 1 步选的
    比例（自由输入就是自由输入），菜单预告的构图后果以回显为准——不回显的话用户
    以为选的是 9:16 的横幅通用词，实际落点却按 7:13 走了竖幅套（复审点名）。
    """
    from ..generation import (
        CANVAS_RATIO_PRESETS,
        canvas_display,
        canvas_family,
        composition_hint,
    )

    print(f"\n请选择 {label} 画布——第 1 步：比例")
    for index, preset in enumerate(CANVAS_RATIO_PRESETS, 1):
        # 菜单直接标画幅族：用户选比例时就看见构图词后果（近竖/近方/横幅→通用）。
        print(f"  {index}. {preset.name}（{canvas_family(*preset.sizes[0])}）")
    custom = len(CANVAS_RATIO_PRESETS) + 1
    print(f"  {custom}. 自定义比例/尺寸（自由输入 宽x高，如 540x1260；9:21 这类档外比例走这里）")
    choice = _menu_pick(f"[{label}] 第 1 步·比例", len(CANVAS_RATIO_PRESETS))
    if choice == custom:
        width, height = _ask_custom_canvas(label)
    else:
        preset = CANVAS_RATIO_PRESETS[choice - 1]
        print(f"\n请选择 {label} 画布尺寸——第 2 步（比例 {preset.name}）：")
        for index, (size_w, size_h) in enumerate(preset.sizes, 1):
            print(f"  {index}. {size_w} × {size_h}")
        size_custom = len(preset.sizes) + 1
        print(f"  {size_custom}. 自定义尺寸（自由输入 宽x高，如 700x1300）")
        size_choice = _menu_pick(f"[{label}] 第 2 步·尺寸（比例 {preset.name}）", len(preset.sizes))
        if size_choice == size_custom:
            width, height = _ask_custom_canvas(label)
        else:
            width, height = preset.sizes[size_choice - 1]
    print(f"[已选] {label} 画布：{canvas_display(width, height)}；{composition_hint(width, height)}")
    return width, height


def _ask_custom_canvas(label: str) -> tuple[int, int]:
    """自定义画布的自由输入：格式坏了当场重问，不静默回落默认表。"""
    from ..brief_parser import parse_canvas_size

    while True:
        raw = _prompt(f"自定义尺寸（{label}，宽x高，如 700x1300）：").strip()
        try:
            return parse_canvas_size(raw)
        except ValueError as exc:  # 坏值大声报错（画布是精确数据），但交互里重问而不是炸流程
            print(f"[提示] {exc}，请重输。")


# LangGraph 节点名 → 用户可读环节名（用于节点级进度）。
_NODE_LABELS = {
    "producer": "制作人",
    "director": "导演",
    "creative_rt": "创意圆桌",
    "screenwriter": "编剧",
    "parallel_designers": "人物/场景/道具设计",
    "art_director": "美术指导",
    "parallel_image_prompts": "资产图生图提示词",
    "storyboard": "分镜设计",
    "parallel_decisions": "镜头/身份决策",
    "parallel_visual": "摄影与一致性校验",
    "fl2va_frame_prompts": "首尾帧生图提示词",
    "parallel_sound": "声音设计与配乐",
    "prompt_engineer": "视频提示词组装",
    "finalize": "终检收尾",
}


def _report_node(node_name: str) -> None:
    label = _NODE_LABELS.get(node_name, node_name)
    print(f"—— 环节：{label} ——", flush=True)


def _progress_saver(generation_dir: Path, brief: Brief) -> Callable[[str, dict], None]:
    """阶段 1 的增量落盘回调：每完成一个节点就把进度写进会话（崩溃可续接）。

    抽出来是因为它有三个主人——新跑（``_phase1_new``）、阶段 1 断点续接（``_resume_stage1``）
    与设定级截断重跑（``_rerun_setting_layer``）。三处各写一份正是本仓反复出现的形态：
    某一处的断点标记改了名，另外两处不报错，只是那条路径上的续接从此读不到断点。
    """
    def _save(node_name: str, current_state: dict) -> None:
        progress_state = dict(current_state)
        progress_state["_progress"] = {"last_completed_node": node_name}
        save_session(generation_dir, brief, progress_state, status=STATUS_STAGE1_RUNNING)

    return _save


@contextmanager
def _progress_scope(title: str):
    """订阅事件总线，把各角色进度实时打到终端；退出（含异常）必解绑并报总用时。

    订阅走 ``event_scope``（复审 T6）：它自己带嵌套引用计数，与 ``step_scope``
    嵌套时只挂一个 ``TextProgress``。原先这里直接 ``reporter.subscribe``，两层各订
    一个监听器，同一条心跳会打两遍——用户会以为系统跑了两轮（issue #71 修的是同一个
    形态，只是从这个入口漏过去了）。标题行、用时统计与 ``_drain_stdin`` 保持不变。
    """
    start = time.time()
    print(f"\n{title}", flush=True)
    try:
        with event_scope():
            yield
    finally:
        _drain_stdin()
        m, s = divmod(int(time.time() - start), 60)
        print(f"[完成] 本段用时 {m} 分 {s:02d} 秒。\n", flush=True)


def run_wizard(config: Config) -> int:
    """无参数主入口：两阶段向导。返回进程退出码。"""
    if not sys.stdin.isatty():
        raise SystemExit("向导需要交互终端运行；脚本场景请用 --brief 快路径。")

    session = _offer_resume(config)
    frames_shown = False
    if session is not None:
        # 续跑入口把上一次的成本表接回来（issue #52）：新进程的计量表是空表，不接就
        # 「跑完的成本」归零。快照替换语义——文件里那张就是当时全量账；只在进程刚起
        # 的这一处接（阶段入口不清零，见 ``graph.pipeline.run_stage1``）。
        token_meter.restore(session.stage_state.get(COST_TABLE_KEY))
        # 接完就把它从 state 里摘掉：账只住两处——活账（计量表）与产出物（会话文件、
        # 阶段 2 断点，两者都由写入方现取活账）。state 里留一份就多一份会变旧的真相，
        # 未来谁读了它都是读到某个时刻的数（ADR 0005 接线纪律）。
        session.stage_state.pop(COST_TABLE_KEY, None)
    if session is None:
        session = _phase1_new(config)
        if session is None:
            return 1
        frames_shown = True  # _phase1_new 收尾已展示生图提示词
    # 阶段 1 完成（或完成了一半）后若断在阶段 1 中途，先从中断节点续跑完阶段 1
    if session.status == STATUS_STAGE1_RUNNING:
        session = _resume_stage1(config, session)
        if session is None:
            return 1
        frames_shown = True  # _resume_stage1 收尾已展示生图提示词
    # 分段陪跑中途退出：整条提示词与帧锚都已落盘，直接回到陪跑续接。
    # 绝不能再走阶段 2——那正是 #17 的代价（重做阶段 2 + 已完成的所有段重来）。
    if session.status == STATUS_SEGMENTED_RUNNING:
        return _resume_segmented(config, session)
    # 多参逐段陪跑中途退出（issue #58）：落到**多参自己的**续接路径，绝不共用上面那条
    # ——两种陪跑的段文件、进度账与有无桥接帧全都不同（见 ``session_store`` 的状态注释）。
    if session.status == STATUS_REF_SEGMENTS_RUNNING:
        return _resume_ref_handoff(config, session)
    # 续接到「阶段 1 已完成、等帧图」的会话：补上同一个修改循环（issue #27）。它原本直接
    # 掉进阶段 2 的收图提问——累积清单看不到、意见提不了，而这条路径正是「重启后清单仍然
    # 非空」的真实现场（用户在首帧循环里提过意见、还没提交帧图就退出）。
    if session.status == STATUS_AWAITING_FRAMES and not frames_shown:
        session = _resume_awaiting_frames(config, session)
        if session is None:
            return 1
        # 帧路线在修改循环首步展示过；多参路线不再在这里展示（issue #50：素材循环
        # 逐张出词，出站节奏由 `_asset_loop` 管），这个标志只管帧路线的补展示。
        frames_shown = True
    # 阶段 1 完成后直接进入阶段 2（生图很快，无需暂停等人工确认）。
    # 多参路线（issue #41）走直出：不收帧图、不进桥接链，≤15 秒一份 ref 六段式。
    if session.brief.mode == "ref":
        return _phase2_ref_finish(config, session)
    return _phase2_collect_and_finish(config, session, show_frame_prompts=not frames_shown)


# ---------------------------------------------------------------------------
# 恢复入口
# ---------------------------------------------------------------------------

def _offer_resume(config: Config) -> SessionState | None:
    sessions = find_awaiting_sessions(config.sessions_root)
    if not sessions or not _confirm(f"发现 {len(sessions)} 个未完成的两阶段任务，是否继续其中一个？", default=True):
        return None
    for index, item in enumerate(sessions, 1):
        print(f"  {index}. [{item.brief.plot[:40]}] 时长 {item.brief.duration:.0f}s（{item.directory}）")
    while True:
        raw = _prompt("选择序号（回车=1）：")
        if not raw:
            choice = 1
        elif raw.isdigit() and 1 <= int(raw) <= len(sessions):
            choice = int(raw)
        else:
            print("无效序号，请重输。")
            continue
        return sessions[choice - 1]


def _resume_stage1(config: Config, session: SessionState) -> SessionState | None:
    """阶段 1 中断续跑：从上次最后一个完成节点的下一阶段继续。"""
    progress = session.stage_state.get("_progress") or {}
    last_node = str(progress.get("last_completed_node", ""))
    if not last_node:
        print("[提示] 会话没有记录阶段 1 断点位置，从头开始跑。")
        # 直接走阶段 1 全新跑法
        return _phase1_new(config)

    # 阶段 1 链中下一个节点
    from ..graph.pipeline import _STAGE1_CHAIN
    if last_node not in _STAGE1_CHAIN:
        print(f"[提示] 上次完成节点 {last_node} 不在阶段 1 链里，从头开始。")
        return _phase1_new(config)
    next_index = _STAGE1_CHAIN.index(last_node) + 1
    brief = session.brief
    generation_dir = session.directory
    model = agents = None
    if next_index >= len(_STAGE1_CHAIN):
        # 节点已全部跑完、只是状态还停在 running，就不重跑图：这种现场来自 `_save_progress`
        # 在**最后一个节点**完成时就把窗口推到了链路尽头，而它之后还有常识质检循环
        # （`common_sense_qa: true`，最多 2 轮 LLM，好几分钟）——在那期间中断就落在这一格。
        # 此前这里直接 return：提示词不展示、意见提不了（连 2026-09-24 补的展示都没有），
        # 状态还一直挂在 stage1_running 这个「可续接」态上。收尾与下面那条路径共用。
        state = dict(session.stage_state)
        print("[续接] 阶段 1 的节点已全部完成（只差收尾没落盘），直接进入生图提示词确认。")
    else:
        resume_node = _STAGE1_CHAIN[next_index]
        print(f"[续接] 上次跑到「{last_node}」，从「{resume_node}」继续阶段 1……")
        initial_state = dict(session.stage_state)
        with _progress_scope(f"[阶段 1·续接] 从「{resume_node}」继续……"):
            state, model, agents = run_stage1(
                brief, config,
                on_node=_report_node,
                resume_from_node=resume_node,
                initial_state=initial_state,
                on_step_done=_progress_saver(generation_dir, brief),
            )
        _drain_stdin()

    # 复用 _phase1_new 的收尾逻辑
    if "_progress" in state:
        state.pop("_progress")
    save_session(generation_dir, brief, state, status=STATUS_AWAITING_FRAMES)
    print("\n阶段 1 已恢复完成。")
    if brief.mode == "ref":
        # 多参路线（issue #41）：直进阶段 2 直出，人机修改循环（画面级锚在帧提示词上）
        # 对它无意义。资产图提示词**不在这里批量展**（issue #50 AC1 一次只出一张）——
        # 素材循环逐张出词，画布行走 brief.canvases（AC5）。
        return load_session(generation_dir)
    # 新跑路径（_phase1_new）在进阶段 2 前会展示生图提示词；续接路径漏了这一步，
    # 用户没有提示词就无法生成关键帧图，阶段 2 的「首帧图片路径」无从提交（2026-09-24 实测）。
    # 修改入口走**同一个**循环（issue #27）：原本这里只展示提示词就走，用户断了线重跑
    # 就再也提不了意见。提示词由循环自己展示（它是循环的第一步），这里不再单独调一次。
    if _user_revision_loop(state, generation_dir, brief, config,
                            model=model, agents=agents) == RESTART_FLOW:
        return _phase1_new(config)
    return load_session(generation_dir)


def _resume_awaiting_frames(config: Config, session: SessionState) -> SessionState | None:
    """续接到「阶段 1 已完成、等帧图」的会话：补上同一个修改循环（issue #27）。

    这条路径原本不展示清单也进不了循环——用户提过意见、还没提交帧图就退出，重跑向导后
    直接掉进阶段 2 的收图提问。补上循环后「累积修订还在、还能接着提」。

    model/agents 留给循环**按需就地构建**：这条路径手上没有 ``run_stage1`` 的返回值，
    而它原本是零 LLM 成本的一步，不该为「看一眼提示词」先建一个客户端。
    """
    if session.brief.mode == "ref":
        # 多参路线（issue #41）：没有帧图可等、没有帧提示词可改，直接交回主流程
        # 进 _phase2_ref_finish——素材循环逐张出词（issue #50），再直出视频提示词。
        # 不再在这里补展全量提示词：一次只出一张（AC1），出站由素材循环按队列驱动；
        # 落盘也随出站逐张累积（`_sync_released_asset_files`），断点续接不丢。
        return load_session(session.directory)
    state = dict(session.stage_state)
    if _user_revision_loop(state, session.directory, session.brief, config) == RESTART_FLOW:
        return _phase1_new(config)
    return load_session(session.directory)


# ---------------------------------------------------------------------------
# 阶段 1
# ---------------------------------------------------------------------------

def _ask_clarifications(config: Config, topic: str) -> str:
    """起步前置澄清：总是问，一句话即可跳过；返回**独立澄清块**（空串=没有澄清）。

    结论存进 ``Brief.clarifications``，**不**拼进 ``brief.plot``：``plot`` 同时喂着主题
    地点守卫的子串匹配与生图常识判官的判据，拼进去会随机改写两条闸门的判据，且有否定句
    反噬（docs/adr/0005）。

    问题由主模型按主题现生成（固定清单问不出「钉具体」）。生成失败在这里**报出来再跳过**，
    不静默——澄清是可选前置，不该让网络抖动把整条流程炸掉。
    """
    limit = int(config.max_clarification_rounds)
    if limit <= 0:
        print("[提示] 配置里澄清轮数上限为 0，本次不做起步澄清。")
        return ""
    from ..clarification import generate_clarifying_questions, is_skip, render_clarification_block
    from ..model_factory import build_chat_model

    try:
        questions = generate_clarifying_questions(topic, build_chat_model(), limit)
    except Exception as exc:  # noqa: BLE001 — 可选前置，报出来再跳过；绝不静默
        print(f"[提示] 起步澄清问题生成失败（{exc}），本次跳过澄清，直接进入原流程。")
        return ""
    if not questions:
        print("[提示] 这一步没有需要澄清的问题，直接进入原流程。")
        return ""

    print(f"\n开始前先问 {len(questions)} 个问题，把主题钉具体（输入「跳过」可随时结束澄清）：")
    pairs: list[tuple[str, str]] = []
    for index, question in enumerate(questions, 1):
        answer = _prompt(f"[澄清 {index}/{len(questions)}] {question}\n"
                         "（回车=不回答这条，输入 跳过=结束澄清直接开始）：")
        if is_skip(answer):
            print("[提示] 已结束澄清，按你已答的部分继续。")
            break
        if answer:
            pairs.append((question, answer))
    block = render_clarification_block(pairs)
    if block:
        print("\n[本次澄清结论]\n" + block)
    return block


def _phase1_new(config: Config) -> SessionState | None:
    # 新开一份生成＝新开一本成本账（issue #52）：上一条废弃/重启流程里攒下的累计
    # 成本不许串进来（菜单上那句「估算费用」说的是**这一份**的钱）。清零点放在这里
    # 而不是阶段入口：阶段入口清零会把阶段 1 的账在阶段 2 前抹掉。
    token_meter.reset()
    topic = ""
    while not topic:
        topic = _prompt("请输入视频主题（必填）：")
        if not topic:
            print("视频主题不能为空。")
    clarifications = _ask_clarifications(config, topic)
    duration_raw = _prompt(f"视频时长秒数（回车默认 {config.default_duration:.0f}）：")
    try:
        duration = float(duration_raw) if duration_raw else config.default_duration
        if duration <= 0:
            raise ValueError
    except ValueError:
        print(f"时长无效，使用默认 {config.default_duration:.0f}s。")
        duration = config.default_duration
    style = _prompt("视觉风格（回车=AI 根据主题自行确定）：")
    route = _choose_generation_mode()
    # 多参路线（issue #41）：mode=ref，与首尾帧并存。variant 对多参无意义，取 T2VA 占位
    # ——帧逻辑一律按 FRAME_VARIANTS 判，不读它。
    is_ref = route == "REF2VA"
    # 画布选择器（issue #42）：选定值进 brief，请求【画布】块与产物画布行同取。
    canvases = _choose_canvases()

    brief = Brief(
        mode="ref" if is_ref else "base",
        variant="T2VA" if is_ref else route,
        duration=duration,
        style=style or "",
        language=config.default_language,
        plot=topic,
        clarifications=clarifications,
        canvases=canvases,
        raw=topic,
    )
    generation_dir = _session_dir(config, topic)

    # 阶段 1 标题按路线说对话（issue #66）：多参路线的实际产出是**资产图**生图提示词，
    # 沿用「首尾帧」措辞会让用户误以为自己在跑首尾帧链（票面用户现场）。首尾帧链措辞
    # 原样保留——≤9 张的既有流程与老产物描述不受影响。
    stage1_title = (
        "[阶段 1] 正在生成剧本、设计与资产图生图提示词……（预计几分钟，期间无需输入）"
        if brief.mode == "ref" else
        "[阶段 1] 正在生成剧本、设计与首尾帧生图提示词……（预计几分钟，期间无需输入）"
    )
    with _progress_scope(stage1_title):
        state, model, agents = run_stage1(
            brief, config, on_node=_report_node,
            on_step_done=_progress_saver(generation_dir, brief),
        )
    _drain_stdin()
    bundle = state.get("fl2va_prompt_bundle") or {}
    state.setdefault("fl2va_frame_descriptions", [])
    state.setdefault("frame_images", [])
    save_session(generation_dir, brief, state, status=STATUS_AWAITING_FRAMES)

    if brief.mode == "ref":
        # 多参路线（issue #41）：无关键帧提示词可改、无帧图可交——人机修改循环的
        # 画面级重出锚在 fl2va_frame_prompts 上，对多参是空转。
        # 资产图提示词由**素材循环逐张出站**（issue #50 AC1，一次只出一张、画布同源 AC5），
        # 这里不再一次性全量给出——「8 条一口气全出、全生完才发现问题」正是要杀的节奏。
        print("\n阶段 1 完成。下一步进入素材循环：逐张出词→生成→审查→交回→校对→裁决。")
        return load_session(generation_dir)

    # 修改循环是与两条续接路径**共用**的那一份（issue #27）：三处各写一遍的代价本仓见过
    # 太多次——改一处漏一处，最后表现成某条路径上「意见说了没人听」。
    if _user_revision_loop(state, generation_dir, brief, config,
                            model=model, agents=agents) == RESTART_FLOW:
        return _phase1_new(config)
    print("\n阶段 1 完成。请复制上面的生图提示词到 ComfyUI 对应生图节点生成图片。")
    return load_session(generation_dir)


def _topic_slug(topic: str) -> str:
    """主题摘要命名：可读前缀 + 哈希后缀，如 `雨夜旧信-a3f2b1c0`。"""
    import hashlib
    import re

    text = re.sub(r"[^\w-]+", "-", topic.strip(), flags=re.UNICODE).strip("-")
    text = re.sub(r"-{2,}", "-", text)[:20].strip("-") or "topic"
    return f"{text}-{hashlib.sha256(topic.strip().encode('utf-8')).hexdigest()[:8]}"


def _session_dir(config: Config, topic: str) -> Path:
    """该主题的会话目录：``<sessions_root>/<topic_slug>/GEN00N``。

    续接规则：最新 GEN 的会话**未完成** → 复用续接；``completed`` 或无会话文件 →
    自动开下一个 GEN 编号（同主题重新生成 = 新的一次验收运行，2026-09-22 裁定）。

    「未完成」指 ``awaiting_frames`` / ``stage1_running`` / ``segmented_running`` /
    ``ref_segments_running`` 四态。这里只按「不是 completed」判，所以新增未完成态自动
    获得续接语义——但前提是**确实有代码在写那个态**：``segmented_running`` 曾长期只
    存在于本 docstring 里（分段陪跑从不改会话状态），于是「分段进行中」这一态在运行时
    不存在，向导会另开新 GEN、``progress.json`` 永远读不到（issue #17）。

    复用 GEN 有个副作用要留意（issue #58 复审实测）：用户**拒绝续接**再重走流程时，
    同一个 GEN 的段产物会被新一轮交付整份覆盖，而**追加式**的账（多参的段产出账）
    不会——所以新一次交付开头要把它挪开（``ref_segments.rotate_segment_ledger``）。
    """
    from ..session_store import STATUS_COMPLETED, load_session

    slug_dir = Path(config.sessions_root) / _topic_slug(topic)
    existing = sorted(slug_dir.glob("GEN*"), key=lambda p: p.name)
    if existing:
        latest = existing[-1]
        try:
            session = load_session(latest)
        except ValueError as exc:
            # 字段损坏（如画布错形）与 JSON 损坏同待遇：警告 + 当它不存在（开新 GEN）。
            # 不许裸抛——`_session_dir` 是重跑向导输已有主题的必经点，抛出去整个向导
            # traceback，用户连绕开的机会都没有（issue #42 复审：严格读回的连带面）。
            print(f"[警告] 最近的会话文件损坏，已跳过续接：{latest}（{exc}）")
            session = None
        if session is not None and session.status != STATUS_COMPLETED:
            directory = latest  # 未完成 → 续接
        else:
            directory = slug_dir / f"GEN{len(existing) + 1:03d}"  # 已完成/无会话/损坏 → 新 GEN
    else:
        directory = slug_dir / "GEN001"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _show_frame_prompts(state: dict, generation_dir: Path) -> None:
    bundle = state.get("fl2va_prompt_bundle")
    print("\n" + "=" * 60)
    print(f"输出目录：{generation_dir}")
    print("=" * 60)
    if not isinstance(bundle, dict):
        print("（本次流程没有产出关键帧生图提示词）")
        return
    from ..generation import FL2VAPromptBundle

    parsed = FL2VAPromptBundle.from_dict(bundle)
    for frame in ("first", "last"):
        rendered = render_fl2va_frame_markdown_from_bundle(parsed, frame)
        if rendered:
            print(rendered)
            print()


# `_show_asset_prompts` 已删（2026-10-23，#40 回滚）：它是资产图提示词的批量出站口，
# 只被首尾帧链的两个调用点用过——而资产图是**多参（ref2va）素材循环**的产物。
# 多参路线早在 #50 就改用逐张出站（`_issue_asset_prompt` + `_sync_released_asset_files`），
# 这个函数对首尾帧链是渗入、对多参是死代码。别因为「看起来还能用」把它加回来：
# 首帧路线要的出站物是关键帧图提示词（`_show_frame_prompts`），不是素材图。


def _show_ref_registry(state: dict, brief: Brief) -> None:
    """多参参考素材登记一览：语义名 → <Picture N>（编号按剧本出场顺序，issue #41）。"""
    from ..generation import ref_asset_registry
    from ..workflow_config import workflow_settings

    registry = ref_asset_registry(state, brief)
    # 登记名调用时现取（复审 S4）：与注入卡、菜单同源，换配置即换这一行。
    print(f"\n[参考素材登记] 编号按剧本出场顺序"
          f"（登记工作流：{workflow_settings().ref_workflow_name}）")
    if not registry:
        print("  （无）")
        return
    for item in registry:
        print(f"  <Picture {item.picture}> {item.name}")
    # 未在剧本出场的语义名会被登记函数排到尾部（稳定序）——这里必须点名（issue #41 复审
    # F5，绝不静默失败）：语义名与剧本措辞稍有出入（「红裙女孩」vs「穿红裙的姑娘」）就会
    # 落进这档，「编号即叙事顺序」就此落空，用户却看不见。brief.refs 用户登记的不算。
    if not brief.refs:
        script = str(state.get("script", ""))
        unmatched = [item.name for item in registry if item.name and item.name not in script]
        if unmatched:
            print(f"  [提示] 以下语义名未在剧本中找到出场，编号排在最后"
                  f"（素材照常登记，剧情不硬塞）：{'、'.join(unmatched)}")


def _show_revision_list(state: dict) -> None:
    """展示带编号的累积修订清单 + 轮次与条数（T2 AC-1/AC-4）。

    清单**空也照打**：撤空之后「现在一条都不剩」是用户必须看到的事实（否则他会以为
    撤销没生效），而累积 0 条正是重出的合法输入。

    **两个轮次号都打**（票面 AC-4 要「当前轮次」，撤销/台账对账要「下一次的轮次号」）：
    只打当前轮次，用户撤完一条后无从知道这一轮会被记成第几轮；只打下一次轮次，屏幕上的
    数字会指向一轮**可能根本不会发生**的重出（用户若当场说「没有意见了」，那个号就落空了）。
    首版（``frame_round`` 为 0）说「首版」而不是「第 0 轮」。
    """
    items = active_revisions(state)
    landed = applied_revisions(state)
    done = int(state.get("frame_round") or 0)
    current = f"第 {done} 轮" if done else "首版（尚未重出）"
    # 「累积 M 条」数的是**全部**（含已落地）：那是用户一共提过几条。已落地的另标一条，
    # 于是「M 条里有 K 条不用再管」一眼可见（票面 AC-4）。
    landed_note = f"（其中 {len(landed)} 条已落地）" if landed else ""
    print(f"\n[用户修订] {current} · 累积 {len(items)} 条{landed_note} · "
          f"下一次重出记为第 {next_round(state)} 轮")
    listing = render_revision_list(pending_revisions(state))
    print(listing if listing else "  （清单为空：重出时不会带上任何修订）")
    if landed:
        # 单列一段且**不编号**：这几条撤不掉（改动已经在设定产物里），编进撤销编号会让人
        # 照着念号撤错人——编号是「模型看到的第几条」与「撤销念的号」共用的那一套。
        print(f"[已落地] {len(landed)} 条——改动已写进设定产物，此后各注入通道不再带它：")
        print(render_applied_list(landed))


def _set_round_input(target: dict, key: str, block: str) -> None:
    """把「本轮的入参块」放进传参 dict；块为空就摘掉键。

    基线（#25）与撤销（#24）走同一条规则，「有块就设键、没块就摘键」各写一遍迟早漏一处，
    而台账的 ``base_used`` 只看基线的键在不在——漏摘会让「这轮没上基线」记成上了。
    """
    if block:
        target[key] = block
    else:
        target.pop(key, None)


def _revoke_revisions(state: dict, generation_dir: Path, brief: Brief) -> list[dict]:
    """按屏幕编号撤销清单条目，返回被撤掉的条目（空列表＝用户一条都没撤）。

    **一次可以连撤多条**（撤完回车），撤一条就立刻落盘：撤销是人对清单的修改，崩一次
    就得重撤的话，续接路径上「我的意见还在吗」又要重新怀疑一遍。
    每撤一条都重打清单——编号是位置号，撤掉一条后其余条重排序，不重打的话用户照着
    上一屏的编号再撤就会撤错人（AC-1：「编号与删除操作一一对应」）。
    """
    revoked: list[dict] = []
    while True:
        # 取**待落地**清单：撤销编号就是它（``remove_user_revision`` 同一份）。拿着全量
        # 清单打屏幕，已落地的条目会占掉一个号，用户照屏幕念「2」撤掉的就是另一个人。
        items = pending_revisions(state)
        if not items:
            # 空清单有两条来路，措辞必须分开：一进来就是空的（选错了），与**刚被撤空**。
            # 后者再说一句「没有可撤销的条目」，紧跟上一行的「[已撤销] 第 N 条」，
            # 会被读成「刚才那下没撤掉」——正是撤销这个功能最不能有的错觉。
            # 已落地的条目另说一句：它们不在编号里（改动已写进设定产物），不提的话
            # 「清单里明明还有一条」会让人以为撤销坏了。
            landed = len(applied_revisions(state))
            hint = f"（另有 {landed} 条已落地：改动已写进设定产物，撤不掉）" if landed else ""
            print(("[提示] 清单已撤空：本次重出不会带上任何修订。" if revoked
                   else "[提示] 清单里没有可撤销的条目。") + hint)
            return revoked
        print("\n[撤销修订] 输入要撤掉的编号（照下方清单念）；撤完直接回车，按当前清单重出。")
        print(render_revision_list(items))
        raw = _prompt("要撤销的编号（回车=撤完）：").strip()
        if not raw:
            return revoked
        try:
            # 编号由这里原样转交、由真源校验（越界与非数字都在那一处报错）：两处各转一次
            # 就有两种措辞，用户看到的到底是哪一条规则也说不清。
            entry = remove_user_revision(state, raw)
        except ValueError as exc:
            print(f"[提示] {exc}")
            continue
        revoked.append(entry)
        print(f"[已撤销] 第 {raw} 条：{entry['text']}")
        save_session(generation_dir, brief, state, status=STATUS_AWAITING_FRAMES)


def _ask_revision_layer() -> str:
    """问本轮这条意见属于哪一层（层次＝重跑起点）；回车＝只改画面。

    层次**必须由人声明**：只有人知道这条是画面口味还是设定变更，让机器猜是不可证启发式
    （票面）。默认值是「只改画面」——回车走的就是与今天逐字相同的那条路（只重出首帧提示词
    一个节点），设定级才要额外付一次重跑与一次确认。
    """
    top = len(SETTING_LAYERS) + 1  # 编号 1 是画面级，2..top 是设定级三档
    print("\n这条意见属于哪一层？（层次＝重跑的起点，决定要重跑哪些步骤）")
    print(f"  1. {LAYER_LABELS[LAYER_FRAME]}：只重出首帧提示词，代价最小（回车默认）")
    for index, spec in enumerate(SETTING_LAYERS, 2):
        print(f"  {index}. {spec.label}：{spec.note}（会替换你已经看过的产物）")
    while True:
        raw = _prompt(f"输入 1-{top}（回车=1 只改画面）：").strip()
        if not raw or raw == "1":
            return LAYER_FRAME
        if raw.isdigit() and 2 <= int(raw) <= top:
            return SETTING_LAYERS[int(raw) - 2].value
        print(f"[提示] 请输入 1-{top} 之一（回车=1 只改画面）。")


def _confirm_setting_rerun(plan, mode: str) -> bool:
    """重跑前的确认门：念清「将重跑 N 步」与「哪些已看过的产物会被替换」，默认**不跑**。

    为什么必须问：重跑会替换用户已经看过、可能已经拿去生图的产物，而 LLM 生成**不可复现
    同一版本**——这是不可逆动作（票面）。默认取「否」，回车不该是那个不可逆的选项。

    环节串与步数按**路线**收敛（issue #81）：首尾帧不产出资产图提示词，那个节点也不报
    进度，念全切片就多报一步。``plan`` 本身仍是全切片（一张图、一条链），过滤只发生在
    这一层——计划对象改动会连带改掉两条结构测试的口径（父 spec #77）。

    ``mode`` **没有默认值**（issue #77 复审 D）：计划对象不带路线，默认值会让将来新增的
    调用点漏传时静默按首尾帧念步数——重跑代价这句话正是用户判断风险的依据。
    """
    silent = rerun_silent_nodes(mode)
    steps = tuple(name for name in plan.nodes if name not in silent)
    print(f"\n[设定级重跑] 层次：{plan.spec.label}")
    print(f"将重跑 {len(steps)} 步："
          + " → ".join(_NODE_LABELS.get(name, name) for name in steps))
    print("下列你已经看过的产物会被替换（LLM 生成不可复现同一版本，替换不可逆）：")
    for label in plan.artifacts:
        print(f"  - {label}")
    return _confirm("确认重跑？", default=False)


def _rerun_setting_layer(state: dict, generation_dir: Path, brief: Brief, config: Config, *,
                         plan, revision: dict) -> None:
    """把一条设定级修订**回灌**到对应产物层：从该层起点截断到链尾重跑，成功后标已落地。

    重跑**复用续接路径的截断机制**（``run_stage1(resume_from_node=...)``），不另写一套：
    「从某节点跑到链尾」在这仓里只有一个实现，链的顺序也只有一份——另写一份正是本仓反复
    出现的「同一件事两处接线、改一处漏一处」。

    **不上修订基线**：基线那句「用户没提过的部分原样保留」针对的是首帧画面，而设定级重跑
    要改的正是设定产物本身，拿旧画面当基线会与新设定打架。台账如实记 ``base_used=False``。
    """
    spec = plan.spec
    initial_state = dict(state)
    # 本轮这条修订经**独立入参键**进重跑（唯一读者在 user_revisions.setting_revision_block）：
    # 层次节点据此判断自己吃不吃它——重跑起点是节点组，三个设计师一次全跑。
    # 只带 layer 与 text：轮次在台账那一列，块里带上它没有读者。
    initial_state[SETTING_REVISION_KEY] = {"layer": spec.value, "text": str(revision["text"])}

    with _progress_scope(f"[设定级重跑] 正在回灌「{spec.label}」并重跑其下游……"
                         "（预计几分钟，期间无需输入）"):
        rerun_state, _model, _agents = run_stage1(
            brief, config,
            on_node=_report_node,
            resume_from_node=plan.start,
            initial_state=initial_state,
            on_step_done=_progress_saver(generation_dir, brief),
        )

    merged = dict(rerun_state)
    # 断点元数据与本轮的入参块都不是产物：前者给续接看、后者只在这**一次**重跑里有效，
    # 都不许顺着 state 流到阶段 2 或下一次重出上去（与基线/撤销同一条规矩）
    merged.pop("_progress", None)
    merged.pop(SETTING_REVISION_KEY, None)
    state.clear()
    state.update(merged)
    # 先标已落地再落盘：清单与台账记的必须是同一个事实（这条到底落没落地）。
    # 标在**重跑返回之后**——抛异常时走不到这里，清单不会谎报（票面 AC-6）。
    mark_revision_applied(state, revision)
    save_session(generation_dir, brief, state, status=STATUS_AWAITING_FRAMES)
    log_frame_round(
        generation_dir,
        round_index=int(revision["round"]),
        layer=spec.value,
        feedback_this_round=str(revision["text"]),
        active_revisions=active_revisions(state),
        bundle=state.get("fl2va_prompt_bundle"),
        base_used=False,  # 设定级重跑不上基线（它改的就是设定产物本身）
    )


# 「整个流程重来」：修改循环的返回值哨兵。循环自己不知道从哪一步重跑阶段 1（新跑路径与两条
# 续接路径的重启起点不同），只把「用户要求推倒重来」这件事报给调用方。
RESTART_FLOW = "restart"

# 主菜单的三个编号：**文案与分支共用同一份编号**。各写一份的话，改了文案里的编号而忘了改
# 分支（或反之）不会报错，只会静默把用户送进另一条路。
_MENU_REDO, _MENU_RESTART, _MENU_REVOKE = "1", "2", "3"

# 修改循环的主菜单文案。**「会清空」必须写进这句**（issue #27）：清空累积修订不可逆，只在
# 事后补一行「已清空」等于人已经踩进来才告知；而且「修订是不是全局财产」这件事只有这里说得清
# ——不说，用户会以为重来之后旧要求仍在生效。
_REVISION_MENU = (
    f"输入 {_MENU_REDO} 提修改意见并重出（下一步声明层次：只改画面／人物设定／美术场景道具／剧情分镜），"
    f"输入 {_MENU_RESTART} 整个流程重来（会清空当前累积的修订清单，回到起步重新生成），"
    f"输入 {_MENU_REVOKE} 撤销清单里的某条：")


def _build_role_agents_on_demand(brief: Brief):
    """就地构建 model 与角色 agents（返回 ``(model, agents)``），供按需重出的各条路用。

    为什么按需构建而不是进循环前一律先建好：续接到「等帧图/等素材」的会话手上没有
    ``run_stage1`` 的返回值，而那条路径原本是零 LLM 成本的——先建好的话，每个「其实
    没有意见要提」的续接都要白付一次建客户端与 agent 的开销。

    它同时服务两条重出路：关键帧提示词（issue #27）与素材的按模型重出（issue #88，
    ``nodes.regenerate_asset_prompt`` 要的是同一份 ``{role: agent}`` 表）。
    """
    from ..agents import build_role_agents
    from ..model_factory import build_chat_model

    model = build_chat_model()
    return model, build_role_agents(model, brief.refs, brief.mode, brief.duration, brief.variant)


def _user_revision_loop(state: dict, generation_dir: Path, brief: Brief, config: Config,
                        *, model=None, agents=None) -> str | None:
    """阶段 1 首帧生图提示词的**人机修改循环**——三条入口共用这一份（issue #27）。

    入口：新跑（``_phase1_new``）、阶段 1 断点续接（``_resume_stage1``）、续接到等帧图的
    会话（``_resume_awaiting_frames``）。抽成共享单元的理由是本仓反复出现的形态：同一件事
    的几处接线各写一份，改一处漏一处，最后表现成某条路径上「意见说了没人听」。

    每轮先由用户**声明层次**（issue #28）：只改画面＝今天这条老路（只重出首帧提示词一个
    节点）；设定级＝回灌该层产物并从该层起点截断到链尾重跑（``_rerun_setting_layer``），
    重跑前有一道确认门——它会替换用户已经看过、可能已拿去生图的产物。

    返回 ``None``：用户说没有更多意见（产物与清单已落盘，收尾交给调用方）。
    返回 ``RESTART_FLOW``：用户选了「整个流程重来」（累积清单已就地清空并落盘）。

    与它长得像的**自动常识质检循环**（``pipeline._stage1_frame_qa_loop``）语义不同、不可
    合并：那是对当前产物重算的**替换**语义，本循环是**人说的话按轮累积**（ADR 0005）。

    ``model`` / ``agents`` 由调用方给得出来就传（新跑与阶段 1 续接路径手上已有
    ``run_stage1`` 的返回值），给不出来就留空，真要重出时才就地构建。
    """
    from ..graph.nodes import make_nodes

    while True:
        _show_frame_prompts(state, generation_dir)
        # 资产图提示词**不在这里出站**（#40 回滚，依据 #41 AC1「首尾帧路线不受影响」）：
        # 它是多参（ref2va）素材循环的产物——用户拿它生素材图、交回、进注入卡接线；
        # 首尾帧链要的是关键帧图（首/尾帧）＋桥接帧，资产提示词没有落点。
        # 本函数是**首尾帧链专用**（多参在 `_phase1_new` / `_resume_stage1` /
        # `_resume_awaiting_frames` 三处都先分流走了），所以这里不是「按路线分叉」，
        # 而是「多参产物不许进这条路线」——2026-10-23 现场：首帧路线跑一次就打印并
        # 落盘了 character-prompt.md 等三份多参文件，用户看到「首帧路线变成了多参」。
        # 每轮重出**前后**都摆一次清单（重出后再回到循环顶端）：它是「我到底说过什么」的
        # 唯一凭据，也是撤销时照着念编号的那张表——没摆出来，撤销就只剩「撤最后一条」。
        _show_revision_list(state)
        if not _confirm("对生图提示词有修改意见？", default=False):
            return None
        mode = _prompt(_REVISION_MENU)
        if mode == _MENU_RESTART:
            # 「整个流程重来」＝这条路线整个不对：清空累积修订，否则被推翻路线上的约束会被
            # 带进新一版，等于灌垃圾。**立刻落盘**——只在内存里清的话，重来后第一次生成就崩、
            # 再续接一次，旧要求又会冒出来。提问文案已明说会清空（见 ``_REVISION_MENU``）。
            state["user_revisions"] = []
            save_session(generation_dir, brief, state, status=STATUS_AWAITING_FRAMES)
            print("[提示] 已清空累积的修订清单——本次回到起步重新生成，"
                  "新一版不会带上之前那些要求。")
            return RESTART_FLOW
        revoked: list[dict] = []
        if mode == _MENU_REVOKE:
            revoked = _revoke_revisions(state, generation_dir, brief)
            if not revoked:
                continue  # 一条没撤＝没有要重出的东西，回上一步重看提示词
            # 撤销轮没有新增条目，轮次号得自己推（连续两轮撤销不能共用同一个轮次号，
            # 否则台账里出现两行 round 相同，事后无法按轮对齐）。
            round_index = begin_round(state)
            feedback = revocation_note(revoked)
        else:
            feedback = _prompt("请输入修改意见：")
            if not feedback:
                continue
            # 层次由用户自己声明（票面）：只有人知道这条是画面口味还是设定变更。
            # 回车＝只改画面，走的就是今天那条路。
            layer = _ask_revision_layer()
            spec = LAYER_SPECS.get(layer)
            if spec is not None:
                plan = stage1_rerun_plan(layer, mode=brief.mode)
                if not _confirm_setting_rerun(plan, mode=brief.mode):
                    # 拒绝＝不跑也不留痕（票面 AC-3）：不记这条、不推轮次、不写台账——
                    # 只有内存里的这条意见被丢掉，产物一个字节都没变。
                    print("[提示] 已放弃本次设定级重跑：这条意见没有记录，任何产物也没有被改动。"
                          "（要只改画面，可重新提一次并选「1 只改画面」）")
                    continue
                revision = record_user_revision(state, layer=layer, text=feedback)
                # 先把这条意见落盘再重跑：重跑要几分钟，中途崩了也不能变成「用户说了系统
                # 没听见」。此刻它还没标已落地——失败或中断就是不标（票面 AC-6）。
                save_session(generation_dir, brief, state, status=STATUS_AWAITING_FRAMES)
                _rerun_setting_layer(state, generation_dir, brief, config,
                                     plan=plan, revision=revision)
                print(f"[已落地] 这条「{spec.label}」修订已写进对应产物，此后各注入通道不再带它。")
                continue
            # 意见进**独立真源**（state["user_revisions"]），按轮累积：说了就不忘。
            # 重出的上下文由首帧节点从 state 读（不是由这里拼进 brief.plot——plot 同时喂
            # 地点守卫的子串匹配与常识判官的判据，拼进去等于随机改写两条闸门，见 ADR 0005）。
            revision = record_user_revision(state, layer=LAYER_FRAME, text=feedback)
            round_index = revision["round"]
        if model is None or agents is None:
            model, agents = _build_role_agents_on_demand(brief)
        nodes = make_nodes(agents, model, brief, config)
        frame_node = nodes["fl2va_frame_prompts"]
        updated = dict(state)
        updated["brief"] = brief  # 主题字段只装主题：修订不再经它传递
        # 修订基线（#25）：以上一轮**完整产物**为基线重出，用户没提过的细节才不会跟着
        # 重掷一起漂走（实测第 3 轮连第 1 轮就有的门环包浆都漂没了）。经**独立入参键**
        # 传给节点——自动质检循环不设该键，它「对当前产物重算」的替换语义逐字不变。
        # 尚无上一版产物时渲染成空串：不设键也不报错，台账如实记 False。
        baseline = render_baseline_block(state.get("fl2va_prompt_bundle"))
        _set_round_input(updated, FRAME_BASELINE_KEY, baseline)
        # 本轮撤销（#24）：被撤的修订不再生效，其造成的画面改动要跟着撤回。只摘清单不够——
        # 基线那句「未提到的部分原样保留」正是把上一版里已落地的改动保下来的那只手。
        # 独立键、不混进基线块：台账的 base_used 只看基线的键在不在。非撤销轮 ``revoked``
        # 是空列表，渲染成空串即摘键（与基线同走 _set_round_input 一条规则）。
        _set_round_input(updated, FRAME_REVOKED_KEY, render_revocation_block(revoked))
        with _progress_scope("[重新生成] 正在按您的意见重出首尾帧生图提示词……（请稍候，期间无需输入）"):
            update = frame_node(updated)
        # 台账的 base_used 取「节点**真的**拿到了基线」这件事，而不是再判一次渲染结果：
        # 否则哪天这段接线断了，台账还会记 True——事后按它分组就分错了（它是判定累积
        # 到底生效没有的唯一分组变量）。取在 update 合并之前，免得被节点输出影响。
        base_used = FRAME_BASELINE_KEY in updated
        if isinstance(update, dict):
            updated.update(update)
        # 基线块与本轮撤销都是**本轮的入参**，不是产物：跑完就摘掉，不让它们顺着 state 流到
        # 阶段 2 或续接路径上去——那些地方没有「本轮以上一版为基线／撤了哪几条」这回事。
        updated.pop(FRAME_BASELINE_KEY, None)
        updated.pop(FRAME_REVOKED_KEY, None)
        state.clear()
        state.update(updated)
        # 先落会话、再写台账：这一轮的产物（花过 LLM 钱的那份）先保住，台账是它的证据。
        # 两个写盘都不吞异常——写失败要声张，绝不静默留下「有产物没账」或「有账没产物」。
        save_session(generation_dir, brief, state, status=STATUS_AWAITING_FRAMES)
        log_frame_round(
            generation_dir,
            round_index=round_index,
            layer=LAYER_FRAME,
            feedback_this_round=feedback,
            active_revisions=active_revisions(state),
            bundle=state.get("fl2va_prompt_bundle"),
            base_used=base_used,  # 如实记：没有上一版产物（空基线）就是没上基线
        )
        print("[已更新] 请查看下方新版本的提示词。")


def render_fl2va_frame_markdown_from_bundle(bundle, frame: str) -> str:
    """渲染单帧提示词（单模型 Z-Image，不依赖 GenerationResult）；空帧返回空串。

    Flux.2 已全链退役（issue #48）：旧产物里的 flux2 行在 bundle 解析处就弃读了，
    这里不再做第二道静默过滤——展示层只管渲染拿到的行。
    """
    if frame not in {"first", "last"}:
        raise ValueError(f"帧类型无效：{frame}")
    rows = bundle.first if frame == "first" else bundle.last
    if not rows:
        return ""
    title = "首帧" if frame == "first" else "尾帧"
    variant = "FL2VA" if (bundle.first and bundle.last) else "I2VA" if bundle.first else "L2VA"
    parts = [f"# {variant} {title}生图提示词（Z-Image）", ""]
    for item in rows:
        parts.extend([
            # 画布行与请求【画布】块同取 `canvas_display`（issue #42 AC3）——本渲染器
            # 曾自写一份「推荐尺寸」排版，与 generation 的渲染层构成两个可写来源。
            f"- 画布：`{canvas_display(item.width, item.height)}`",
            f"- 提示词节点：`{item.prompt_node_id}`", "",
            "### Positive Prompt", "", item.positive_prompt, "",
        ])
        # 「操作说明」已删除（2026-10-24）：向导出站正是用户看到重复正文的那条路
        # （实测 instructions 被填成整段提示词）。理由与判据见 `generation.render_asset_rows`。
    return "\n".join(parts).rstrip()


# ---------------------------------------------------------------------------
# 阶段 2
# ---------------------------------------------------------------------------

def _ask_asset_replacement(existing, incoming) -> bool:
    """换版问人（issue #43 AC3）：新旧 SHA256 对照摆出来，默认保留原登记。

    同名文件换版是 LoRA 可变文件名教训（issue #18）的原型场景——那时旧对照静默作废；
    这里反过来：对照材料齐全、要不要换由人点头，没点头就守着原登记。
    """
    return _confirm(
        f"[换版] {existing.name}（{existing.tag}）交回内容与登记不符："
        f"旧 sha256={existing.sha256[:16]}… → 新 sha256={incoming.sha256[:16]}…。"
        f"用新版替换登记？",
        default=False)


# 单张闭环的两个去向集合（issue #50）：裸字符串信令会被打错字且无处对账，
# 与本仓 STATUS_* 的口径一致按常量走。
LOOP_SETTLED, LOOP_ABANDONED, LOOP_PAUSED = "settled", "abandoned", "paused"
PROOFREAD_DONE, PROOFREAD_REGEN, PROOFREAD_FAILED = "done", "regen", "failed"


class ProofreadOutcome(NamedTuple):
    """读图校对一张的结果：状态 ＋ 本轮偏差清单（issue #72）。

    偏差清单要**带出去**：重生成菜单的「AI 按偏差改词」按它改词，而且取的是本轮**全部**
    偏差，不是 ``ProofreadRecord`` 里那几条已裁的——人在第一条上选了「重生成」，后面的
    偏差压根没轮到人裁，但它们照样是这张图要修的地方。只在状态里留个 ``regen`` 的话，
    这份清单随函数返回一起丢，AI 改词就只能靠人复述。
    """

    status: str
    deviations: tuple = ()


# 单张闭环的续跑走向（issue #51）：这张图上次已交回、裁决还没落定，续跑给它三条路。
# 与 LOOP_* 同为信令常量——裸字符串会被打错字且无处对账。
RESUME_REPROOFREAD, RESUME_REPLACE = "reproofread", "replace"


def _ask_asset_path(label: str, *, remembered: str = "",
                    empty_hint: str = "（回车=回审查菜单）") -> str:
    """问一个素材文件路径：空由**调用方**决定怎么接（循环里＝回审查菜单，补交里＝跳过）；
    路径不存在当场报并给重交机会（同帧图收图）。

    ``remembered`` 是上次已答的路径（issue #51 续跑点）：回车即沿用，不必重敲一遍；
    敲了新路径就用新的——已答不等于锁死。**回车被「沿用」占了，就必须另留一个空出口**
    （输 `-`）：否则续跑后这一问再也回不到审查菜单／跳过，等于把「已答」变成「锁死」。
    """
    from ..task_package import resolve_input_path

    hint = f"（回车=沿用上次：{remembered}；输 - 改填）" if remembered else empty_hint
    while True:
        raw = _clean_path_input(_prompt(f"{label}{hint}："))
        if raw == "-":
            return ""  # 显式清空：这是回车被「沿用」占掉之后唯一的空出口
        raw = raw or remembered
        if not raw:
            return ""
        try:
            if not resolve_input_path(raw).is_file():
                raise FileNotFoundError(f"文件不存在：{raw}")
        except (OSError, ValueError) as exc:
            print(f"[错误] {exc}")
            if not _confirm("重新提交该项？", default=True):
                return ""
            continue
        return raw


def _pick_next_asset(pending: Sequence[QueueItem]) -> QueueItem | None:
    """队列菜单（issue #50 AC4）：回车=默认队首；序号=跳号/指定下一个出谁；q=暂停。

    「跳号」与「指定下一个」是同一问的两张脸：打一个序号就既跳过了默认那张、又指定了
    出谁；被跳的号留在队列里稍后回来，不丢。返回 None ＝ 用户「先到这里」。

    issue #66：队列已按 ``<Picture N>`` 升序（＝剧本出场序＝接线序），菜单同屏显示
    **列表序号**与 **Picture 号**——两者不总相等（跳号场景下 2 号可能挂着 Picture 5），
    只看序号填图会填错槽，故 Picture 号排在前面、与注入卡逐行对账。
    """
    from ..generation import KIND_LABELS

    print("\n[素材循环] 待办队列（顺序＝<Picture N> 升序＝剧本出场序＝接线序）：")
    for index, item in enumerate(pending, 1):
        label = KIND_LABELS.get(item.kind, item.kind)
        print(f"  {index}. <Picture {item.picture}> {item.name}（{label}）")
    default = pending[0]
    while True:
        raw = _prompt(
            f"下一个出谁？（回车=出 {default.name}；输入序号=跳号/指定下一个出谁；q=先到这里）："
        ).strip()
        if not raw:
            return default
        if raw.lower() == "q":
            return None
        if raw.isdigit() and 1 <= int(raw) <= len(pending):
            return pending[int(raw) - 1]
        print("无效序号，请重输。")


def _sync_released_asset_files(state: dict, brief: Brief, generation_dir: Path,
                               name: str) -> None:
    """把出过词的资产落进 ``{kind}-prompt.md``（issue #50 逐张出站的落盘节奏）。

    文件只收**已出站**的行——一次性写全量等于把「一口气全出」从后门放回来
    （用户照着文件就能全生完）。已落账的资产也算已出站：它们在上一次运行里出过词，
    整篇重写时不许把它们截掉（续接场景）。改写提示词后重进本函数即刷成最新版。
    """
    from ..asset_loop import note_released
    from ..asset_proofread import STATUS_DONE, ProofreadRecord, proofread_rows
    from ..generation import KIND_LABELS, image_prompt_variants, render_asset_rows
    from ..topic_generation import _ARTIFACT_FILES

    released_set = set(note_released(state, name))
    for settled_name, row in proofread_rows(state).items():
        if ProofreadRecord.from_dict(str(settled_name), row).status == STATUS_DONE:
            released_set.add(str(settled_name))
    variants = image_prompt_variants(state, brief.canvases)
    # 类型枚举取 KIND_LABELS 一份真源（不再硬编字符型元组——它与队列序常量会各漂各的）
    for kind in KIND_LABELS:
        rows = [v for v in variants if v.kind == kind and v.name in released_set]
        if not rows:
            continue
        rendered = render_asset_rows(rows, kind)
        (generation_dir / _ARTIFACT_FILES[kind]).write_text(rendered, encoding="utf-8")


def _issue_asset_prompt(state: dict, brief: Brief, generation_dir: Path, item: QueueItem) -> None:
    """出一张生图提示词（issue #50 AC1）：单张展示 + 已出站行落盘。

    画布行走 ``brief.canvases``（issue #50 AC5）：与请求【画布】块同源，不再回落默认表。

    **出词横幅带模型与理由**（issue #88 用户故事 7）：这一张是按哪一档的纪律写的、为什么
    是那一档（关键词命中／手动指定／配置默认），用户据此复核自动判断。Qwen 档另带许可提示
    ——非商用是交付红线（spec #84 D8），写在配置注释里看不见，出词时不说就等于没说。
    """
    from ..generation import KIND_LABELS, image_prompt_variants, render_asset_rows

    variants = image_prompt_variants(state, brief.canvases)
    row = next((v for v in variants if v.name == item.name), None)
    if row is None:
        raise ValueError(f"队列资产在 state 里找不到提示词条目：{item.name!r}")
    label = KIND_LABELS.get(item.kind, item.kind)
    # 出词也是用户等待的一步（issue #71）：横幅先说明在干什么；这一步是纯渲染，
    # 没有心跳可说，所以只有横幅——不伪造等待。
    print(format_step_banner("正在生成生图提示词"), flush=True)
    print(f"\n[素材循环] 当前待办：{item.name}（{label}）")
    print(_render_asset_model_line(state, item))
    print(render_asset_rows([row], item.kind))
    _sync_released_asset_files(state, brief, generation_dir, item.name)


def _asset_assignment(state: dict, item: QueueItem) -> dict[str, str]:
    """这一张当前的模型判定：条目里记的那一份，旧条目（issue #88 之前）回落到配置默认。

    回落**走同一个判定函数**（``image_routing.assignment``，不给文本＝不命中）：手搓一个
    ``{model, source, keyword}`` 字面量就是同一份记录的第二个写点，形状一改就漂。
    """
    from ..image_routing import assignment, assignment_of

    value = assignment_of(_find_prompt_entry(state, item))
    if value is not None:
        return value
    return assignment(item.kind, texts=())


def _render_asset_model_line(state: dict, item: QueueItem) -> str:
    """出词横幅里那两行：模型与理由（＋ 受限模型的许可提示）。**唯一写源**（issue #88）。

    内容从条目里记的判定现读，不重新判一遍：重新判会在提示词被改写后给出与当初不同的
    答案（同 ``image_routing`` 模块注释里那条理由）。

    许可提示按模型查 ``image_config.LICENSE_NOTES``：写死 ``== "qwen"`` 的话，加第二档
    受限模型时横幅会静默不再提示。
    """
    from ..image_config import LICENSE_NOTES
    from ..image_routing import described

    value = _asset_assignment(state, item)
    lines = [f"[生图模型] {item.name}：{described(value)}"]
    note = LICENSE_NOTES.get(value["model"])
    if note:
        lines.append(f"[许可] {note}")
    return "\n".join(lines)


def _find_prompt_entry(state: dict, item: QueueItem) -> dict:
    """按语义名定位 state 里的资产条目（读/写两处共用这一遍走法）；找不到响错不吞。"""
    rows = state.get(f"{item.kind}_image_prompts")
    if isinstance(rows, list):
        for entry in rows:
            if isinstance(entry, dict) and entry.get("name") == item.name:
                return entry
    raise ValueError(f"找不到资产提示词条目：{item.kind}/{item.name!r}")


def _current_prompt_text(state: dict, item: QueueItem) -> str:
    """取该资产当前的 positive_prompt（改写后即最新版）。"""
    return str(_find_prompt_entry(state, item).get("positive_prompt") or "")


def _update_prompt_text(state: dict, item: QueueItem, new_text: str) -> None:
    """把改写后的提示词写回 state 的资产条目（下游对照底本/登记描述同源最新版）。"""
    _find_prompt_entry(state, item)["positive_prompt"] = new_text


def _locked_features(state: dict, brief: Brief, name: str) -> Sequence[str]:
    """取一张素材的锁死清单（issue #53）：判偏差的唯一判据。

    抽成一处是因为它有两个消费者（AI 改词、回灌对齐）——两处各写一遍 ``next(...)``
    就是第二份会漂的取法（复审 T5）：将来 ``image_prompt_variants`` 的字段改名或
    清单来源改口径，漏改一处就会静默退回空清单（＝不收窄），而空清单是合法值，
    没有任何东西会报红。旧条目没有这一键时返回空元组（不收窄），与对照器同口径。
    """
    from ..generation import image_prompt_variants

    return next((v.locked_features for v in image_prompt_variants(state, brief.canvases)
                 if v.name == name), ())


def _rewrite_asset_prompt_round(state: dict, brief: Brief, generation_dir: Path, item: QueueItem,
                                *, trigger: str) -> bool:
    """「改写提示词」/「改词再掷」一轮（issue #50 AC2/AC3）：问意见 → LLM 改写一条
    → 追加式留痕 → 更新 state 并重新出词。

    只动意见点名的问题、其余逐字保留（``rewrite_asset_prompt`` 的纪律）；空意见＝取消
    不动词不留痕。留痕走 ``asset-rounds.jsonl`` 追加账——被改掉的旧词只活在这里。
    """
    feedback = _prompt("改写要求（想怎么改，如「裙子改成宝蓝色」；回车=取消改写）：").strip()
    if not feedback:
        print("已取消改写（没给意见，提示词不动）。")
        return False
    from ..asset_loop import ASSET_ROUNDS_FILENAME, log_asset_round, rewrite_asset_prompt
    from ..model_factory import build_chat_model

    old = _current_prompt_text(state, item)
    try:
        # 步骤横幅＋等待心跳（issue #71 / ADR 0019）：改写是直调，事件只有订阅者
        # 能看到；横幅与订阅成对（step_scope），不留「打了字之后干等」。
        with step_scope("正在改写生图提示词"):
            new = rewrite_asset_prompt(old, feedback, build_chat_model())
    except Exception as exc:  # noqa: BLE001 — 改写失败要响但不许裸崩：词不动、回菜单再定
        print(f"[错误] 改写失败：{exc}；提示词保持原样。")
        return False
    _update_prompt_text(state, item, new)
    log_asset_round(
        generation_dir,
        name=item.name, kind=item.kind, trigger=trigger,
        feedback=feedback, prompt_before=old, prompt_after=new,
    )
    save_session(generation_dir, brief, state, status=STATUS_AWAITING_FRAMES)
    print(f"[改写·{item.name}] 已记入轮次账（{ASSET_ROUNDS_FILENAME}）。")
    return True


def _change_asset_model_round(state: dict, brief: Brief, generation_dir: Path,
                              item: QueueItem) -> bool:
    """「改这张的生图模型」一轮（issue #88 / 父 spec #84 D8）：选档 → 按该档纪律重出词。

    返回 True ＝ 已按新模型重出（调用方回外层重出提示词、重新审查）；False ＝ 没改
    （取消、选的是同一档，或重出失败）。失败时**提示词与模型记录都保持原样**并留在菜单
    ——静默换词等于让人在不知情下用上一条没验证过的词（同 AI 改词的口径）。

    为什么是「重出」而不是「改写」：换模型的全部意义是换**纪律块**（中文短句／英文长文／
    英文＋负面）——照旧词改一遍只会把旧档的写法带进新档。重出走的是与阶段 1 同一个
    出词函数（``nodes.regenerate_asset_prompt``），纪律块由 agent 的 system prompt 决定。

    选完记成 ``source=manual``：自动路由（关键词命中）从此不再动这一张——用户故事 8 的
    「关键词误判不会卡住我」就靠这一条，而节点重跑时也认得出要保留人的选择。

    **选的是同一档也记 ``manual``**（不重出、不花调用）：那张多半是自动规则切过去的，
    人确认「就用这一档」本身就是一次选择；不记的话，日后重跑时关键词不再出现，它就
    静默回落到配置默认——AC4「手动选择覆盖自动路由」在这一点上会缺口。
    """
    from ..asset_loop import TRIGGER_REVIEW_MODEL, log_asset_round
    from ..graph import nodes
    from ..image_config import IMAGE_MODEL_KEYS, image_model_label
    from ..image_routing import SOURCE_MANUAL, put_assignment

    current = _asset_assignment(state, item)["model"]
    menu = " / ".join(f"{index}={image_model_label(key)}"
                      for index, key in enumerate(IMAGE_MODEL_KEYS, 1))
    while True:
        raw = _prompt(f"[模型·{item.name}] 当前 {image_model_label(current)}；"
                      f"改成哪个？{menu}（回车=不改）：").strip()
        if not raw:
            print("模型不改，提示词保持原样。")
            return False
        if raw.isdigit() and 1 <= int(raw) <= len(IMAGE_MODEL_KEYS):
            model = IMAGE_MODEL_KEYS[int(raw) - 1]
            break
        print(f"无效输入，请选 1-{len(IMAGE_MODEL_KEYS)}（回车=不改）。")
    if model == current:
        entry = _find_prompt_entry(state, item)
        put_assignment(entry, {"model": model, "source": SOURCE_MANUAL, "keyword": ""})
        save_session(generation_dir, brief, state, status=STATUS_AWAITING_FRAMES)
        print(f"[模型·{item.name}] 仍是 {image_model_label(model)}，已记为人选"
              "（此后自动路由不再改动这一张）；提示词不动。")
        return False
    try:
        # 横幅＋心跳（issue #71 / ADR 0019）：这次是真的一次模型调用，用户要知道在等什么。
        with step_scope("正在按新模型重出提示词"):
            _model, agents = _build_role_agents_on_demand(brief)
            fresh = nodes.regenerate_asset_prompt(
                agents, item.kind, item.name, state, brief, model=model)
    except Exception as exc:  # noqa: BLE001 — 失败要响但不许裸崩：词与模型都不动、回菜单再定
        print(f"[模型·{item.name}] 重出失败：{exc}；提示词与模型都保持原样。")
        return False
    entry = _find_prompt_entry(state, item)
    old = str(entry.get("positive_prompt") or "")
    # 四键契约里**动三个**：换模型就是换写法，旧词、旧负面、旧锁死清单都属于上一档的
    # 产物（锁死清单是「逐字摘自正文」的，正文一换它就失效）。
    entry["positive_prompt"] = str(fresh.get("positive_prompt") or "")
    entry["negative_prompt"] = str(fresh.get("negative_prompt") or "")
    entry["locked_features"] = list(fresh.get("locked_features") or [])
    put_assignment(entry, {"model": model, "source": SOURCE_MANUAL, "keyword": ""})
    # 留痕（issue #88）：换档等于整条词重写，旧词与「它原来属于哪一档」只在这本追加账里
    # ——条目上的模型记录会被这一次覆盖，不留痕就再也查不到上一版是按哪套纪律写的。
    log_asset_round(
        generation_dir,
        name=item.name, kind=item.kind, trigger=TRIGGER_REVIEW_MODEL,
        feedback=f"审查时改生图模型：{image_model_label(current)} → {image_model_label(model)}",
        prompt_before=old, prompt_after=entry["positive_prompt"],
    )
    save_session(generation_dir, brief, state, status=STATUS_AWAITING_FRAMES)
    print(f"[模型·{item.name}] 已改为 {image_model_label(model)}（手动指定），"
          "提示词已按该档纪律重出。")
    # 出词后的警告级纪律检查照跑（#68 / #87）：只警告不阻断，口径跟着新模型走。
    # 循环体与阶段 1 节点共用 ``nodes.print_image_prompt_warnings``（同一条判据、同一处接线）。
    nodes.print_image_prompt_warnings(
        entry["positive_prompt"], model, f"[纪律警告·{item.name}] ")
    return True


def _ai_reword_asset_round(state: dict, brief: Brief, generation_dir: Path, item: QueueItem,
                           deviations: Sequence[tuple[str, str, str]]) -> bool:
    """「AI 按偏差改词」一轮（issue #72 / ADR 0018）：按偏差清单强化原意 → 写回 → 留痕。

    ``deviations`` 是本轮**全部**偏差 ``(哪一处, 描述原文, 图中实见)``，调用方从
    ``ProofreadOutcome.deviations`` 换形而来。

    失败（LLM 报错、结果丢了锁死特征、模型一个字都没改）只提示、词保持原样并**留在菜单**
    ——静默换词等于让人在不知情下用上一条没验证过的改写。返回 True 才重出词。
    """
    from ..asset_loop import (
        ASSET_ROUNDS_FILENAME,
        TRIGGER_REGEN_AI_REWORD,
        ai_reword_asset_prompt,
        log_asset_round,
    )
    from ..graph import nodes
    from ..model_factory import build_chat_model

    if not deviations:
        # 防御分支：重生成菜单只在本轮判出偏差后才可达，真走到这里说明信令接错了。
        # 没有清单就无从「按偏差改词」，猜着改会把没问题的部分一起漂走。
        print("[AI 改词] 没有可用的偏差清单，提示词保持原样（不猜着改）。")
        return False
    old = _current_prompt_text(state, item)
    locked = _locked_features(state, brief, item.name)
    # 这一张按哪一档模型出的词，就按哪一档的纪律改（issue #87）：三档的写法相反
    # （中文短句／英文长文／英文＋负面），拿错纪律改写等于把这一张往另一家的口径拽。
    # issue #88 起取**条目里记的那一档**：同一类里可能逐张不同（关键词命中切 MOSS、
    # 人或手动改过），按类别配置取会把这类资产的改写拽回配置档。
    model_key = _asset_assignment(state, item)["model"]
    try:
        new = ai_reword_asset_prompt(old, deviations, build_chat_model(),
                                     locked_features=locked, model=model_key)
    except Exception as exc:  # noqa: BLE001 — 失败要响但不许裸崩：词不动、回菜单再定
        print(f"[AI 改词·{item.name}] 改写失败：{exc}；提示词保持原样。")
        return False
    if new == old:
        # 原词回吐不是「改成功」：留痕就会给轮次账记一个假的改写轮，
        # 而「本张已重画 N 轮」正是按那本账算的（issue #72）。
        print("[AI 改词] 模型没有改动任何内容，提示词保持原样。")
        return False
    _update_prompt_text(state, item, new)
    aspects = "、".join(str(aspect) for aspect, _quote, _observed in deviations)
    log_asset_round(
        generation_dir,
        name=item.name, kind=item.kind, trigger=TRIGGER_REGEN_AI_REWORD,
        feedback=f"校对裁决重生成：AI 按偏差改词（{aspects}）",
        prompt_before=old, prompt_after=new,
    )
    save_session(generation_dir, brief, state, status=STATUS_AWAITING_FRAMES)
    # 成功文案只写系统**真的核对过**的事实（复审 S5）：「锁死清单」逐条做过子串校验
    # （ai_reword_asset_prompt 里 missing 那段）；「其余正文逐字保留」只是请求里写给
    # 模型的纪律，系统没有核对，不能对用户保证——超证据的文案比没有文案更坏。
    print(f"[AI 改词·{item.name}] 已按 {len(deviations)} 处偏差改写"
          f"（锁死清单已逐条核对；其余正文请人工确认），"
          f"并记入轮次账（{ASSET_ROUNDS_FILENAME}）。")
    # 改写也是一次出词：照跑警告级纪律检查（#68 / ADR 0018）。只警告、不阻断——
    # 这份词是人要拿去生图的，纪律问题得在出图前看得见。检查口径跟着这一张的模型走
    # （issue #87）：五类里四类只在 Z-Image 档成立。
    nodes.print_image_prompt_warnings(new, model_key, f"[纪律警告·{item.name}] ")
    return True


def _settled_asset_facts(state: dict, brief: Brief, *, exclude: str) -> list[tuple[str, str]]:
    """已定稿素材的 ``(语义名, 画面事实)``——回灌（issue #53 AC3）的取材口。

    「已定稿」＝读图校对裁决落账（``status=done``）：只有走到这一步的素材才有**对着成图
    核实过**的画面事实可回灌；出过词、交回但还没裁的那些，事实仍是待核的计划文本。

    画面事实按**素材画面事实**的既有优先序取（合并结果 ＞ 生图提示词 ＞ 用户登记描述）——
    复用 ``ref_asset_registry`` 的解析结果，不再抄一份优先序（两处各写一份必然漂）。
    已定稿素材的合并结果由 ``proofread_descriptions`` 直接给（那就是优先序的首选源，
    与在册时 ``ref_asset_registry`` 给的是同一份）。

    取材口**不许被 ``brief.refs`` 收窄**（#53 复审采纳项）：``ref_asset_registry`` 在
    ``brief.refs`` 非空时只回用户登记的那几条，只认它的话，brief 文件路线下「已定稿但
    用户没登记」的素材就静默不回灌了——而回灌的正是跨素材一致性，少一条就少一处锚。
    """
    from ..asset_proofread import STATUS_DONE, ProofreadRecord, proofread_descriptions, proofread_rows
    from ..generation import ref_asset_registry

    settled = [
        str(name) for name, row in proofread_rows(state).items()
        if ProofreadRecord.from_dict(str(name), row).status == STATUS_DONE
    ]
    registered = {r.name: r.description for r in ref_asset_registry(state, brief)}
    merged = proofread_descriptions(state)
    facts: list[tuple[str, str]] = []
    for name in settled:
        if name == exclude:
            continue
        # 已定稿 ⇒ 必有合并结果（``merged``）；登记表那一份是它在册时的同一份。
        description = str(merged.get(name) or registered.get(name) or "").strip()
        if description:
            facts.append((name, description))
    return facts


def _align_asset_with_settled(state: dict, brief: Brief, generation_dir: Path,
                              item: QueueItem) -> None:
    """回灌（issue #53 AC3）：出这张的词之前，按前面已定稿素材的画面事实对齐一次。

    只在**这张首次出词**时做（``released`` 账是「出过词」的既有账）：改写成功后的重出、
    续跑回到中途的这张都不再对齐——词是人刚改的，再自动对齐一轮会按旧事实把他的改动
    拽回去。

    没有已定稿素材（队列第 1 张）＝不回灌、不花调用。对齐失败（LLM 报错、结果丢了锁死
    特征）只提示、保持原词：回灌是让跨素材更一致，不该把整条循环卡死。

    横幅由 ``step_scope`` 与订阅成对给出（issue #71 / ADR 0019）：这是选择之后的第一个
    动作，第 1 张没有前序素材时对齐空转、毫秒返回，也照样打这一行——用户敲完「下一个
    出谁」后第一眼必须看到系统在动作。
    """
    from ..asset_loop import align_asset_prompt, loop_state
    from ..model_factory import build_chat_model

    with step_scope("正在与前序素材对齐"):
        if item.name in {str(x) for x in loop_state(state)["released"]}:
            return
        facts = _settled_asset_facts(state, brief, exclude=item.name)
        if not facts:
            return
        old = _current_prompt_text(state, item)
        locked = _locked_features(state, brief, item.name)
        try:
            new = align_asset_prompt(old, facts, build_chat_model(), locked_features=locked)
        except Exception as exc:  # noqa: BLE001 — 回灌失败要响但不许裸崩：词不动、继续出词
            print(f"[回灌] {item.name} 对齐失败：{exc}；提示词保持原样。")
            return
        if new == old:
            return
        _update_prompt_text(state, item, new)
        save_session(generation_dir, brief, state, status=STATUS_AWAITING_FRAMES)
        print(f"[回灌] {item.name} 已按前面 {len(facts)} 条已定稿素材的画面事实对齐"
              f"（{'、'.join(name for name, _ in facts)}）；其余部分逐字保留。")


def _abandon_asset(state: dict, brief: Brief, generation_dir: Path, item: QueueItem) -> None:
    """显式放弃这张资产（issue #50 AC1 的另一出口）：不再出图，队列前进。

    若此前已交回过一版（只可能来自「重生成」后的弃坑），撤不撤登记**问人**——撤＝
    废图不进片（注入卡按台账接线）；留着＝那张图照旧进片。默认撤下，但绝不静默删：
    台账的 SHA256 登记与正本复制是「可复测」的凭据（CONTEXT.md「素材台账」），
    删它得有人的点头，同「换版问人」一个道理。
    **不写轮次账**：轮次账记的是「改写」修订轮，放弃不是修订；放弃事实落在
    ``asset_loop.abandoned`` 与屏幕交代上，续接据此跳过。
    """
    from ..asset_ledger import KIND_PICTURE, load_asset_ledger, remove_asset
    from ..asset_loop import note_abandoned, set_current

    entry = load_asset_ledger(generation_dir).get(item.name, KIND_PICTURE)
    tail = ""
    if entry is not None:
        if _confirm(f"这张此前已交回过（{entry.tag}）。放弃时把这版从素材台账一并撤下"
                    "（废图不进片）？", default=True):
            remove_asset(generation_dir, item.name, KIND_PICTURE)
            tail = "（已一并撤下此前交回的登记）"
        else:
            tail = "（登记保留：注入卡仍会按它接线）"
    note_abandoned(state, item.name)
    set_current(state, None)
    save_session(generation_dir, brief, state, status=STATUS_AWAITING_FRAMES)
    print(f"[放弃] {item.name}：不再出图，出下一张{tail}。")


def _ask_deviation_choice(dev, idx: int, total: int) -> str:
    """偏差三选（issue #44 AC2）：回车默认「按图改文案」；无效输入重问，不静默替人决定。

    菜单文案从 ``DECISION_LABELS`` 现拼（issue #51 复审）：续跑提示要念同一批名字，
    两处各写一份必然漂——顺序也取 ``DECISIONS``，号码与裁决的对应关系只有一处。
    """
    from ..asset_proofread import (
        DECISION_KEEP,
        DECISION_LABELS,
        DECISION_REGENERATE,
        DECISION_REWRITE,
        DECISIONS,
    )

    print(f"  [偏差 {idx}/{total}] {dev.aspect}：描述「{dev.prompt_quote}」→ 图中「{dev.observed}」")
    menu = " / ".join(f"{i}={DECISION_LABELS[d]}" for i, d in enumerate(DECISIONS, 1))
    while True:
        raw = _prompt(f"  裁决：{menu}（回车=1）：").strip()
        if raw in ("", "1"):
            return DECISION_REWRITE
        if raw == "2":
            return DECISION_REGENERATE
        if raw == "3":
            return DECISION_KEEP
        print("  [错误] 只认 1/2/3（回车=1）。")


def _pin_rewrite_quote(dev, running: str, *, name: str) -> str:
    """「按图改文案」落点钉锚：返回在 ``running`` 上唯一命中、语义名之外的引文；放弃返回空串。

    失锚／多处命中／只命中语义名都引导补锚（粘贴正文原文把落点钉到唯一一处），
    回车＝放弃补锚，由调用方回三选改选其他裁决——三选必须走得到底，不许未捕获崩溃
    （评审：失锚引文的「按图改文案」路径名不副实）。补锚后的引文记进裁决记录：
    ``prompt_quote`` ＝ 实际落笔的锚，不是模型原话。
    """
    from ..asset_proofread import locate_rewrite_span

    quote = dev.prompt_quote
    while True:
        try:
            locate_rewrite_span(running, quote, name)
            return quote
        except ValueError as exc:
            print(f"  [提示] {exc}")
            quote = _prompt(
                "  补锚引文（粘贴描述正文里的原文把落点钉到唯一一处，回车=改选裁决）：").strip()
            if not quote:
                return ""


def _proofread_one_asset(state: dict, brief: Brief, generation_dir: Path, name: str):
    """读图校对**一张**（issue #44 机制，issue #50 逐张闭环调用）。

    返回 ``ProofreadOutcome``：``status`` 是 ``PROOFREAD_DONE``（裁决落账）/ ``PROOFREAD_REGEN``
    （该张裁了「重生成」）/ ``PROOFREAD_FAILED``（读图失败——不放行，图跑偏无人纠）；裁了
    重生成时 ``deviations`` 带上本轮全部偏差（重生成菜单的 AI 改词按它改，issue #72）。
    对照底本恒取生图提示词最新版：换版重交/改词再掷的
    新图是按**当前词**画的，不是按上一轮合并结果画的。
    """
    from ..asset_ledger import KIND_PICTURE, load_asset_ledger, resolve_asset_path
    from ..asset_loop import count_regen_rounds, render_regen_hint
    from ..asset_proofread import (
        DECISION_LABELS,
        DECISION_REGENERATE,
        DECISION_REWRITE,
        PROOFREADS_KEY,
        STATUS_DONE,
        STATUS_PENDING_REGEN,
        Adjudication,
        Deviation,
        ProofreadProgress,
        ProofreadRecord,
        build_proofread_request,
        clear_progress,
        compare_descriptions,
        load_progress,
        merge_asset_description,
        parse_reading,
        pop_decision,
        proofread_rows,
        render_proofread_report,
        store_progress,
    )
    from ..generation import image_prompt_variants
    from ..tools.reference_auditor import describe_image

    variants = image_prompt_variants(state, brief.canvases)
    bases = {v.name: v.positive_prompt for v in variants}
    if brief is not None:
        for r in brief.refs:
            bases.setdefault(r.name, r.description)
    base = bases.get(name)
    # 锁死清单（issue #53）：判偏差的唯一判据。旧条目没有这一键 → 空表 ＝ 不收窄
    # （对照器与读图请求两处口径一致：没声明清单就走旧口径）。
    locked = next((v.locked_features for v in variants if v.name == name), ())
    ledger = load_asset_ledger(generation_dir)
    entry = ledger.get(name, KIND_PICTURE)
    if entry is None or not base:
        raise ValueError(f"资产 {name!r} 无台账登记或无对照底本，不能校对")
    records = proofread_rows(state)

    def _save() -> None:
        state[PROOFREADS_KEY] = records
        save_session(generation_dir, brief, state, status=STATUS_AWAITING_FRAMES)

    def _checkpoint(updated: ProofreadProgress) -> ProofreadProgress:
        """一条裁决一个续跑点（issue #51 AC1）：写完立刻落盘。

        人裁是这张图最贵的一步——每条偏差都要人读、可能补锚、可能改替换文案；
        攒到最后才写，中途断一次就全白问。读图结果一起存，续跑连视觉调用都不重付。
        """
        store_progress(state, updated)
        _save()
        return updated

    print(f"\n[读图校对] {entry.name}（{entry.tag}）")
    # 重画轮次（issue #72）：从轮次账现算（不另存计数器）。提示放在**裁决之前**——
    # 「还能按图改文案保留这一版」是给人裁决时的信息，等到重生成菜单才说就晚了一步。
    regen_hint = render_regen_hint(count_regen_rounds(generation_dir, name=entry.name))
    if regen_hint:
        print(f"[重画轮次] {regen_hint}")
    progress = load_progress(state, entry.name, sha256=entry.sha256, base=base)
    if progress is not None:
        # 图未换版、词未改写 → 上次那份读图结果与已裁条目仍然成立
        print(f"[续跑] 沿用上次的读图结果（{entry.tag} 未换版），只问没裁过的条目。")
        report = compare_descriptions(base, progress.reading(), locked)
    else:
        try:
            # 读完图之前不能只有一行 `[读图校对]`（issue #71 / ADR 0019）：读图客户端
            # 走流式后首字节/生成中都是事件，横幅与订阅必须成对出现。
            with step_scope("正在读图校对"):
                # 正本位置走统一解析口（issue #85）：素材在素材库里，不在生成目录
                raw = describe_image(
                    resolve_asset_path(ledger, entry, session_dir=generation_dir),
                    build_proofread_request(base, locked))
            reading = parse_reading(raw)
        except Exception as exc:  # noqa: BLE001 — 读图失败要响且不放行
            print(f"[读图校对] {entry.name} 读图失败：{exc}")
            print("已停在等交回：图没读成就不放行；重跑向导可重试。")
            _save()
            return ProofreadOutcome(PROOFREAD_FAILED)
        report = compare_descriptions(base, reading, locked)
        # 读图结果**先落盘**再问裁决：这张的视觉调用不该因为中间断了重付一次
        progress = _checkpoint(ProofreadProgress(
            name=entry.name, sha256=entry.sha256, base=base, claims=reading.claims))
    print(render_proofread_report(report))

    adjudications: list[Adjudication] = []
    voided = False
    running = base  # 补锚校验的落笔文本：merge 按序改它，锚要在演进文本上钉得住
    decided = list(progress.decisions)  # 上次已裁的条目池：认领一条少一条
    for idx, dev in enumerate(report.deviations, 1):
        reused = pop_decision(decided, dev)
        if reused is not None:
            if reused.decision == DECISION_REWRITE:
                try:
                    running = merge_asset_description(running, [reused], [], name=entry.name)
                except ValueError as exc:
                    # 落点对不上了（读图这次给的清单与上次不同）：宁可重问，不许乱改
                    print(f"  [续跑] 上次这条裁决的落点对不上（{exc}），重问这一条。")
                    reused = None
            if reused is not None:
                print(f"  [续跑·偏差 {idx}/{len(report.deviations)}] 沿用上次裁决："
                      f"{DECISION_LABELS.get(reused.decision, reused.decision)}")
                adjudications.append(reused)
                if reused.decision == DECISION_REGENERATE:
                    voided = True
                    break
                continue
        while True:  # 补锚放弃＝回三选重选；三选必须走得到底（评审）
            choice = _ask_deviation_choice(dev, idx, len(report.deviations))
            if choice == DECISION_REGENERATE:
                # 图要重画：本轮其余裁决无意义（新图交回后重新校对），不并入任何改动
                adj = Adjudication(dev, choice)
                adjudications.append(adj)
                progress = _checkpoint(progress.with_decision(adj))
                voided = True
                break
            if choice != DECISION_REWRITE:
                adj = Adjudication(dev, choice)
                adjudications.append(adj)
                progress = _checkpoint(progress.with_decision(adj))
                break
            quote = _pin_rewrite_quote(dev, running, name=entry.name)
            if not quote:
                continue  # 放弃补锚：回三选改选其他裁决
            repl = _prompt(f"  改后文案（回车=用图中实见「{dev.observed}」）：").strip()
            adj = Adjudication(Deviation(dev.aspect, quote, dev.observed), choice, repl)
            adjudications.append(adj)
            running = merge_asset_description(running, [adj], [], name=entry.name)
            progress = _checkpoint(progress.with_decision(adj))
            break
        if voided:
            break

    accepted: list[str] = []
    if report.omissions and not voided:
        if progress.omissions is not None:
            # 这个回答也是答案（答过就不重问）；不并入是有效答案，不是「没答」
            accepted = [o.fact for o in report.omissions] if progress.omissions else []
            print(f"[续跑] 补漏沿用上次的回答：{'并入' if progress.omissions else '不并入'}"
                  f" {len(report.omissions)} 条。")
        else:
            take = _confirm(f"把 {len(report.omissions)} 条补漏事实并入素材画面事实？", default=True)
            progress = _checkpoint(progress.with_omissions(take))
            accepted = [o.fact for o in report.omissions] if take else []

    if voided:
        # 写账走 ProofreadRecord（读侧同型）：手拼 dict 是同一 schema 的第二写点
        records[entry.name] = ProofreadRecord(
            name=entry.name, sha256=entry.sha256, status=STATUS_PENDING_REGEN,
            description=base, adjudications=tuple(adjudications),
        ).to_dict()
        clear_progress(state, entry.name)  # 记录接进度班的时刻：进度此后只剩旧话
        _save()  # 逐张落盘：后面的张中途炸不丢这张的裁决（评审：崩溃丢账）
        return ProofreadOutcome(PROOFREAD_REGEN, report.deviations)
    # name=语义名：改写落点不许落在名字上（名字是 registry 键不是偏差处，
    # 评审点名的缺陷场景——全局 replace 连名改会与登记名失同步）。
    merged = merge_asset_description(base, adjudications, accepted, name=entry.name)
    records[entry.name] = ProofreadRecord(
        name=entry.name, sha256=entry.sha256, status=STATUS_DONE,
        description=merged, adjudications=tuple(adjudications),
        omissions=tuple(accepted),
    ).to_dict()
    clear_progress(state, entry.name)
    _save()  # 逐张落盘（同上）
    if merged != base:
        print(f"  素材画面事实（合并后）：{merged}")
    return ProofreadOutcome(PROOFREAD_DONE)


def _resume_returned_asset(state: dict, item: QueueItem, entry) -> str:
    """这张图**已入库**、裁决还没落定（issue #51 AC2 的续跑菜单）。

    返回 ``RESUME_REPROOFREAD``（接着校对这一版）/ ``RESUME_REPLACE``（换一张图重新交回）/
    ``LOOP_SETTLED``（其实已落账，防御分支）/ ``LOOP_ABANDONED`` / ``LOOP_PAUSED``。

    为什么不默默跳过：已入库不等于锁死——图是既成事实，人可能就是想换一张。
    但也不该拿「满意吗/路径是多少」重问一遍：那两问的产物（正本 + 台账）已经在盘上。

    已裁「重生成」的那张（``pending_regen``）走 ``RESUME_REPLACE`` 进审查菜单而不是回
    重掷菜单：重掷菜单已经把话说完（原词再掷），续跑回来时人手上正拿着**新图**要交，
    再让他看一遍「原词再掷/改词再掷」等于把已经做过的事重做一遍。
    """
    from ..asset_proofread import STATUS_DONE, STATUS_PENDING_REGEN, ProofreadRecord, proofread_rows

    row = proofread_rows(state).get(item.name)
    record = None
    if row is not None:
        record = ProofreadRecord.from_dict(item.name, row)
        if record.sha256 != entry.sha256:
            record = None  # 换版后的新图还没被裁过，旧记录不冒充
    if record is not None and record.status == STATUS_DONE:
        print(f"[续跑·{item.name}] 这一版已裁决落定（{entry.tag}），跳过。")
        return LOOP_SETTLED
    if record is not None and record.status == STATUS_PENDING_REGEN:
        print(f"[续跑·{item.name}] 上次裁了「重生成」（{entry.tag}）：请交回重掷后的新图。")
        return RESUME_REPLACE

    print(f"\n[续跑·{item.name}] 这一版图已入库（{entry.tag} · sha {entry.sha256[:12]}…），"
          "上次停在这张，裁决还没落定。")
    while True:
        raw = _prompt(
            "  1=接着校对这一版（默认，不重填路径）  2=换一张图重新交回\n"
            "  3=显式放弃这张资产  q=先到这里（回车=1）："
        ).strip()
        if raw in ("", "1"):
            return RESUME_REPROOFREAD
        if raw == "2":
            return RESUME_REPLACE
        if raw == "3":
            return LOOP_ABANDONED
        if raw.lower() == "q":
            return LOOP_PAUSED
        print("无效输入，请选 1/2/3/q。")


def _asset_cycle(state: dict, brief: Brief, generation_dir: Path, item: QueueItem) -> str:
    """一张资产的单张闭环（issue #50）：出词→审查→交回→校对→裁决。

    返回 ``LOOP_SETTLED``（裁决落账）/ ``LOOP_ABANDONED``（显式放弃）/ ``LOOP_PAUSED``（用户
    先到这）。**审查在填路径之前**——打回两出口都不入库：外部重掷系统连看都不看
    （不登记被拒图），改写提示词只动词、留痕一轮修订。校对裁了「重生成」走重掷菜单
    （issue #72 起四档）：默认原词再掷、改词再掷（词图皆错时连词一起换）、AI 按偏差改词
    （模型按偏差清单把被画漏的细节写显式）、显式放弃。「按图改文案」始终是校对裁决
    里的那一档，由人按键，**没有任何自动兜底路径**。
    """
    from ..asset_loop import (
        TRIGGER_REGEN_REROLL,
        TRIGGER_REGEN_REWORD,
        TRIGGER_REVIEW_REWORD,
        count_regen_rounds,
        log_asset_round,
        render_regen_hint,
        set_current,
    )
    from ..asset_ledger import (
        KIND_PICTURE,
        AssetSubmission,
        intake_assets,
        load_asset_ledger,
        render_ledger,
        render_report,
    )
    from ..generation import ref_asset_registry

    registry = ref_asset_registry(state, brief)

    # 回灌（issue #53 AC3）：出词**之前**按前面已定稿素材的画面事实对齐一次。放在外层
    # 循环之外＝改写成功后的重出不再对齐（那会把用户刚改的词按旧事实拽回去）。
    # 横幅与心跳在函数内的 step_scope 里成对出现（issue #71）。
    _align_asset_with_settled(state, brief, generation_dir, item)

    # 续跑菜单只在**首次**进这一张时给（`first_pass`）：改写成功／原词再掷之后的外层
    # 重出，正是用户「回去重掷了、回来交回」的那一刻——那里要的是审查菜单（含填路径），
    # 拿「已入库，接着校对」拦住他，新版图就永远交不进来了。
    first_pass = True
    report_deviations: tuple = ()  # 本轮校对判出的全部偏差（重生成菜单的 AI 改词按它改）
    while True:  # 外层：出词（首次 + 改写成功后重出）
        _issue_asset_prompt(state, brief, generation_dir, item)
        outcome = None  # None ＝ 本轮没走到裁决（改写成功要重出词）
        # 续跑（issue #51 AC2）：这张图上次**已经交回**、裁决还没落定——中断就落在
        # 交回与裁决之间。此时再问「这张图满意吗 / 路径是多少」是拿已完成的事重问一遍。
        existing = load_asset_ledger(generation_dir).get(item.name, KIND_PICTURE)
        if existing is not None and first_pass:
            action = _resume_returned_asset(state, item, existing)
            if action == LOOP_SETTLED:
                set_current(state, None)
                save_session(generation_dir, brief, state, status=STATUS_AWAITING_FRAMES)
                return LOOP_SETTLED
            if action == LOOP_ABANDONED:
                _abandon_asset(state, brief, generation_dir, item)
                return LOOP_ABANDONED
            if action == LOOP_PAUSED:
                return LOOP_PAUSED
            if action == RESUME_REPROOFREAD:
                result = _proofread_one_asset(state, brief, generation_dir, item.name)
                outcome, report_deviations = result.status, result.deviations
            elif action != RESUME_REPLACE:
                # 未知走向不静默掉进审查菜单：那会把「认不出的信令」伪装成正常流程
                raise ValueError(f"未知的续跑走向：{action!r}")
            # action == RESUME_REPLACE：用户要换一张图（或上次裁了重生成），掉进审查→交回
        first_pass = False
        while outcome is None:  # 内层：审查 → 交回（打回/取消都不动台账）
            ledger = load_asset_ledger(generation_dir)
            existing = ledger.get(item.name, KIND_PICTURE)
            mark = f"（已入库 {existing.tag}，换版将替换）" if existing else ""
            choice = _prompt(
                f"[审查·{item.name}] 这张图满意吗？{mark}\n"
                "  1=满意，交回这张（下一步填路径）  2=打回：外部重掷（不登记被拒图）\n"
                "  3=打回：改写提示词（留痕一轮修订）  4=显式放弃这张资产\n"
                "  5=登记外部生成成本（耗时/次数）  6=改这张的生图模型（换档重出词）\n"
                "  q=先到这里（回车=1）："
            ).strip()
            if choice in ("", "1"):
                remembered = recall_answer(state, _asset_path_key(item.name)) or ""
                raw = _ask_asset_path(f"{item.name} 素材图路径", remembered=remembered)
                if not raw:
                    print("未填路径，回审查菜单。")
                    continue  # 留在审查菜单，不重刷提示词
                if raw != remembered:
                    # 答完立刻落盘（issue #51 AC1）：中断若落在「答了、还没入库」之间，
                    # 续跑按这条答案接着走，用户不必重敲整个路径。
                    _record_answer(state, _asset_path_key(item.name), raw,
                                   generation_dir=generation_dir, brief=brief)
                cost_seconds, cost_attempts = _pending_asset_cost(state, item.name)
                submissions = [AssetSubmission(
                    name=item.name, kind=KIND_PICTURE, source=raw,
                    # 审查菜单里登记过就随正本一起挂上（issue #52）：成本与 SHA256 同一次
                    # 落盘，才不会出现「登记在答案账、产物在台账」的两本账。
                    external_seconds=cost_seconds, external_attempts=cost_attempts,
                )]
                report = intake_assets(
                    generation_dir, submissions,
                    registry=registry, script=str(state.get("script", "")),
                    on_replace=_ask_asset_replacement,
                )
                rendered = render_report(report)
                if rendered:
                    print(rendered)
                if report.ledger.entries:
                    print(render_ledger(report.ledger))
                # 产物接答案的班：正本与台账已落盘，答案账里那条不再有信息量
                forget_answer(state, _asset_path_key(item.name))
                cost_seconds_key, cost_attempts_key = _asset_cost_keys(item.name)
                forget_answer(state, cost_seconds_key)
                forget_answer(state, cost_attempts_key)
                save_session(generation_dir, brief, state, status=STATUS_AWAITING_FRAMES)
                result = _proofread_one_asset(state, brief, generation_dir, item.name)
                outcome, report_deviations = result.status, result.deviations
                break
            if choice == "2":
                print("被拒图不登记。请在 ComfyUI 重掷后回来重新审查。")
                continue
            if choice == "3":
                # 改写成功＝新词要重新出站（外层重出提示词）再审；取消＝留在审查菜单
                if _rewrite_asset_prompt_round(
                        state, brief, generation_dir, item, trigger=TRIGGER_REVIEW_REWORD):
                    break
                continue
            if choice == "4":
                _abandon_asset(state, brief, generation_dir, item)
                return LOOP_ABANDONED
            if choice == "5":
                _register_asset_external_cost(state, brief, generation_dir, item)
                continue
            if choice == "6":
                # 换档重出成功＝新词要重新出站再审；没改成（取消/失败/同一档）留在菜单
                if _change_asset_model_round(state, brief, generation_dir, item):
                    break
                continue
            if choice.lower() == "q":
                return LOOP_PAUSED
            print("无效输入，请选 1/2/3/4/5/6/q。")

        if outcome is None:
            continue  # 改写成功：新词重新出站再审
        if outcome == PROOFREAD_FAILED:
            return LOOP_PAUSED
        if outcome == PROOFREAD_DONE:
            set_current(state, None)
            save_session(generation_dir, brief, state, status=STATUS_AWAITING_FRAMES)
            return LOOP_SETTLED
        # outcome == PROOFREAD_REGEN：重生成菜单（issue #50 AC3，issue #72 扩到四档）
        while True:
            # 轮次提示每轮现算（重画轮次从轮次账推导，不另存计数器）：0 轮不占版面。
            hint = render_regen_hint(count_regen_rounds(generation_dir, name=item.name))
            regen = _prompt(
                f"[重生成·{item.name}] 图要重画" + (f"（{hint}）" if hint else "") + "：\n"
                "  1=原词再掷（默认，提示词不动）  2=改词再掷（留痕一轮修订）\n"
                "  3=AI 按偏差改词（留痕一轮修订，锁死清单逐字保留）  4=显式放弃这张资产\n"
                "  q=先到这里（回车=1）："
            ).strip()
            if regen in ("", "1"):
                log_asset_round(
                    generation_dir,
                    name=item.name, kind=item.kind, trigger=TRIGGER_REGEN_REROLL,
                    feedback="校对裁决重生成：原词再掷",
                    prompt_before=_current_prompt_text(state, item),
                    prompt_after=_current_prompt_text(state, item),
                )
                save_session(generation_dir, brief, state, status=STATUS_AWAITING_FRAMES)
                print("请用原提示词重掷这张图，回来重新审查。")
                break
            if regen == "2":
                # 改词成功＝新词重新出站；取消＝留在重掷菜单（不许默默当原词再掷）
                if _rewrite_asset_prompt_round(
                        state, brief, generation_dir, item, trigger=TRIGGER_REGEN_REWORD):
                    break
                continue
            if regen == "3":
                # AI 改词同理：失败留在菜单，词与账都不动（issue #72 AC「失败提示保持原样」）
                triples = [(d.aspect, d.prompt_quote, d.observed) for d in report_deviations]
                if _ai_reword_asset_round(state, brief, generation_dir, item, triples):
                    break
                continue
            if regen == "4":
                _abandon_asset(state, brief, generation_dir, item)
                return LOOP_ABANDONED
            if regen.lower() == "q":
                return LOOP_PAUSED
            print("无效输入，请选 1/2/3/4/q。")


def _asset_loop(state: dict, brief: Brief, generation_dir: Path) -> bool:
    """素材循环（issue #50）：队列驱动的逐张闭环，一次只维持一张「当前待办」。

    队列顺序＝纯 ``<Picture N>`` 升序（issue #66 起；＝剧本出场序＝接线序），支持
    跳号/指定下一个出谁（AC4）；每张要么裁决落账、要么显式放弃才轮到下一张（AC1）。
    返回 True ＝ 队列清空，继续注入卡与阶段 2；
    False ＝ 用户「先到这里」，会话保持可续接（进度已逐张落盘）。
    """
    from ..asset_ledger import KIND_PICTURE, IntakeReport, load_asset_ledger, render_report, verify_asset_ledger
    from ..asset_loop import asset_queue, loop_state, set_current
    from ..asset_proofread import STATUS_DONE, ProofreadRecord, proofread_rows
    from ..generation import image_prompt_variants, ref_asset_registry

    variants = image_prompt_variants(state, brief.canvases)
    registry = ref_asset_registry(state, brief)
    pictures = {r.name: r.picture for r in registry}
    queue = asset_queue(
        [(v.kind, {"name": v.name}) for v in variants],
        pictures=pictures,
    )
    # 台账漂移照报（换版可发现：正本文件被换过而登记没换）——出词前摆在最前面，
    # 免得用户拿着一份对不上账的清单去生成（旧批量收口同款，issue #50 挪到循环入口）。
    ledger = load_asset_ledger(generation_dir)
    drift = verify_asset_ledger(generation_dir)
    if drift:
        print(render_report(IntakeReport(ledger=ledger, drift=drift)))
    if not queue:
        print("\n[素材循环] 本次没有资产图要出（队列为空）。")
        return True

    loop = loop_state(state)  # 唯一读口：错形在这里响，不许被 or {} 洗成空表
    abandoned = {str(x) for x in loop["abandoned"]}
    records = proofread_rows(state)

    def _settled(name: str) -> bool:
        """已落账＝记录 status=done **且钉的正是当前正本**（sha 同）。

        sha 不同＝这本正本被换过（循环外 intake 接受换版），旧裁决不许冒充新版：
        该资产退回待办重走一遍（issue #44「换版入库后自动重新校对」的同一口径）。

        **台账每次现读**（issue #51 复审实测）：循环里也会换版——续跑菜单「换一张图」
        与重交新图都会改写登记。用循环入口那份快照比对，换过版的这张就永远对不上
        sha、永远「未落账」，真实形态是同一张被无限重排进待办（问一次、换一次、再来）。
        """
        row = records.get(name)
        if row is None or ProofreadRecord.from_dict(name, row).status != STATUS_DONE:
            return False
        entry = load_asset_ledger(generation_dir).get(name, KIND_PICTURE)
        return entry is None or ProofreadRecord.from_dict(name, row).sha256 == entry.sha256

    def _pending():
        return [q for q in queue if q.name not in abandoned and not _settled(q.name)]

    # 循环整体纳入事件订阅（issue #71 / ADR 0019）：读图、改写、回灌是三步已知的
    # 长调用，但**串起它们的那层**原先在进展作用域之外——任何一处漏套 step_scope
    # 都会退回「没有订阅者的静默窗口」。这里兜底订阅；每一步的横幅仍由 step_scope
    # 自己打（嵌套共用同一个监听器，不会把心跳打两遍）。
    with event_scope():
        while True:
            pending = _pending()
            if not pending:
                set_current(state, None)
                save_session(generation_dir, brief, state, status=STATUS_AWAITING_FRAMES)
                print(f"\n[素材循环] 队列清空：{len(queue)} 张全部落账/放弃。")
                return True
            # 续接时「当前待办」直接接上，不再问一遍出谁（系统只维持一张当前待办）
            current_name = str(loop.get("current") or "")
            item = None
            if current_name:
                item = next((q for q in pending if q.name == current_name), None)
            if item is None:
                # 只剩一张待办时不问「出谁」——没有可跳的号；暂停出口仍在审查菜单（q）。
                item = pending[0] if len(pending) == 1 else _pick_next_asset(pending)
                if item is None:
                    save_session(generation_dir, brief, state, status=STATUS_AWAITING_FRAMES)
                    print("[素材循环] 已保留进度；重跑向导从当前待办继续。")
                    return False
            set_current(state, item.name)
            save_session(generation_dir, brief, state, status=STATUS_AWAITING_FRAMES)
            outcome = _asset_cycle(state, brief, generation_dir, item)
            if outcome == LOOP_PAUSED:
                save_session(generation_dir, brief, state, status=STATUS_AWAITING_FRAMES)
                print("[素材循环] 已保留进度；重跑向导从当前待办继续。")
                return False
            # LOOP_SETTLED / LOOP_ABANDONED：已落账/已放弃，继续出下一张
            abandoned = {str(x) for x in loop["abandoned"]}
            records = proofread_rows(state)


def _collect_extra_media(state: dict, brief: Brief, generation_dir: Path) -> None:
    """素材视频/音频按需挂账（issue #43 三型对齐官方标签）＋无提示词登记项补交。

    视频/音频没有「出词」环节（按需挂账），不进素材循环；无生图提示词的登记项
    （brief.refs 用户登记名）同理——循环只管有词可出的资产图。
    """
    from ..asset_ledger import (
        KIND_AUDIO, KIND_PICTURE, KIND_VIDEO,
        AssetSubmission, format_tag,
        intake_assets, load_asset_ledger, render_ledger, render_report,
    )
    from ..generation import image_prompt_variants, ref_asset_registry

    registry = ref_asset_registry(state, brief)
    prompted = {v.name for v in image_prompt_variants(state, brief.canvases)}
    promptless = [r for r in registry if r.name not in prompted]

    submissions: list[AssetSubmission] = []
    recorded: list[str] = []  # 本轮记进答案账的键（入库落定后一并作废）
    for item in promptless:
        existing = load_asset_ledger(generation_dir).get(item.name, KIND_PICTURE)
        if existing is not None:
            # 续跑跳过已入库（issue #51 AC2）：这一项的正本与登记都在盘上，再问一遍路径
            # 就是拿已完成的事重问。要换版走素材循环的「换一张图」，不在这条补交口。
            print(f"{format_tag(KIND_PICTURE, item.picture)} {item.name} 已入库"
                  f"（{existing.tag}），跳过。")
            continue
        remembered = recall_answer(state, _asset_path_key(item.name)) or ""
        raw = _ask_asset_path(
            f"{format_tag(KIND_PICTURE, item.picture)} {item.name} 素材图路径",
            remembered=remembered, empty_hint="（无生图提示词，回车=跳过）")
        if raw:
            if raw != remembered:
                _record_answer(state, _asset_path_key(item.name), raw,
                               generation_dir=generation_dir, brief=brief)
                recorded.append(_asset_path_key(item.name))
            submissions.append(AssetSubmission(name=item.name, kind=KIND_PICTURE, source=raw))

    # 视频/音频这一段**故意不记答案**（issue #51 复审）：它是「还要加吗」的确认而非
    # 一道题，且每次都要能反悔（加一段、再加一段）。把它记成「已经答过不再问」，
    # 续跑后就再也加不进素材视频/音频了。
    while _confirm("还要交回素材视频/音频吗？", default=False):
        kind_raw = _prompt("类型（1=素材视频 2=素材音频，回车=取消）：").strip()
        if not kind_raw:
            break
        if kind_raw not in ("1", "2"):
            print("[错误] 只认 1（素材视频）/ 2（素材音频）。")
            continue
        name = _prompt("语义名（如「运镜参考」「红裙女孩音色」）：").strip()
        if not name:
            print("[错误] 语义名不能为空。")
            continue
        raw = _ask_asset_path("文件路径：")
        if not raw:
            continue
        kind = KIND_VIDEO if kind_raw == "1" else KIND_AUDIO
        submissions.append(AssetSubmission(name=name, kind=kind, source=raw))

    if not submissions:
        return
    report = intake_assets(
        generation_dir, submissions,
        registry=registry, script=str(state.get("script", "")),
        on_replace=_ask_asset_replacement,
    )
    for key in recorded:
        forget_answer(state, key)  # 产物接答案的班（同素材循环）
    save_session(generation_dir, brief, state, status=STATUS_AWAITING_FRAMES)
    rendered = render_report(report)
    if rendered:
        print(rendered)
    print(render_ledger(report.ledger))


def _deliver_ref_segments(state: dict, brief: Brief, prompt: str,
                          generation_dir: Path):
    """>15 秒多参分段**准备**（issue #46）＋按需写段（issue #70）：薄接线——编排/文案
    全在 ``ref_segments`` 一份。

    返回 ``RefSegmentJob``（不是 ``RefSegmentResult``）：本函数只定案段边界、落 plan.json
    与顺序卡，**一段提示词都不写**。正文由逐段陪跑在当前段轮到时写（``_run_ref_segment_
    handoff``）——先写完再陪跑，用户会在还没生成第 1 段时就先看到第 2、3 段的词
    （#55 AC1/#56：一次只维持一个当前待办）。

    单段素材超限（issue #75）返回 **None** 并当面说清：这是设计边界，不是崩溃——
    片子还没开始生成，用户这时改剧本/拆段最便宜（等到第 5 段才发现，前面几小时的
    生成都白做）。
    """
    from ..ref_segments import prepare_ref_segments
    from ..segment_refs import SegmentAssetOverflowError

    # 多参交付里的拆段是直调路径：横幅＋心跳都在这里成对出现（issue #67）。
    # 报告仍由本函数打印——它是「产物交付」的结论，不是等待反馈。
    try:
        with step_scope("正在按镜头切点拆分执行段"):
            job, report = prepare_ref_segments(
                whole_prompt=prompt, total_s=brief.duration, out_dir=generation_dir,
                state=state, brief=brief)
    except SegmentAssetOverflowError as exc:
        print(f"\n[错误] 本片无法按段交付：{exc}")
        print("  提示词与素材台账都已就绪，问题只在「某一段要喂的图太多」——"
              "改剧本的镜头划分或精简素材后重跑阶段 2 即可。")
        return None
    print(report)
    # 交付也是长阻塞：清掉生成期间误敲的键，别让第一个问答吞掉它们。
    _drain_stdin()
    return job


def _deliver_ref_single_segment(state: dict, brief: Brief, prompt: str,
                                generation_dir: Path) -> RefSegmentResult | None:
    """≤15 秒（issue #57 AC5）：整片六段式即本片**唯一执行段**——不拆段，收尾与 >15s 同一套。

    直出路线**不重写、不套分段注记**（见 ``ref_segments.single_segment_result``）：整片
    六段式是阶段 2 组装出来的产物，本身就是能直接喂给 H3 的一段（H3 单次 4–15 秒，≤15 秒
    刚好一段）。段文件/plan.json/顺序卡照落盘，于是回传 → 两道闸 → 段产出账 → 成片这条
    收尾与 >15s 完全同路（spec #54「收尾不分叉」）。

    校验问题**当面打**（同 ``format_ref_segment_report`` 的纪律）：直出文本在阶段 2 的
    质检循环里已经用同一个校验器核过一遍，但交付这一刻的结论照样不许静默。

    返回 None ＝ 超出官方容量上限（issue #86 AC4）：直出把整片一次喂进去，不受「每段
    子集」那条校验管，所以 9/3/3/12 在这里独立咬一次；拦住即停（与 >15s 的超限同款
    收口，错误文案里已点名超出的素材）。
    """
    from ..generation import ref_asset_registry
    from ..ref_capacity import ReferenceCapacityError
    from ..ref_segments import single_segment_result
    from ..tools.h3_validator import format_issues

    registry = ref_asset_registry(state, brief)
    try:
        result = single_segment_result(
            whole_prompt=prompt, total_s=brief.duration, out_dir=generation_dir,
            ref_meta=[(r.picture, r.name, r.description) for r in registry])
    except ReferenceCapacityError as exc:
        print(f"\n[错误] 本片无法直出：{exc}")
        print("  提示词与素材台账都已就绪，问题只在「一次喂进去的素材太多」——"
              "精简素材或把时长压到更少的参考后重跑阶段 2 即可。")
        return None
    errors = [issue for issue in result.issues[0]
              if getattr(issue, "severity", "") == "error"]
    if errors:
        print(f"[提醒] 整片提示词有 {len(errors)} 项校验未过：")
        print(format_issues(errors))
    return result


def _assemble_ref_final_film(result, generation_dir: Path):
    """全段通过后自动拼成成片（issue #57 AC1/AC2）：编排与文案都在 ``ref_segments`` 一份。

    ``result`` 只需有 ``plans`` 与 ``total_s``（``RefSegmentResult`` 与「按需写段」的
    ``RefSegmentJob`` 同形——拼接判据不该按产物来源分叉）。

    「未通过即不拼」住在 ``assemble_final_film`` 里（**不是这里的 if**）：这条规矩必须住在
    被拼接的那一处，否则它就是一个可以被绕过的调用方约定（本仓「规则在、接线不在」已犯
    七次）。所以这里**无条件调用**——缺段时它自己拒绝，并把卡在哪一段当面打出来（AC3）。
    """
    from ..ref_segments import assemble_final_film, format_final_film, passed_segment_rows

    film = assemble_final_film(
        directory=generation_dir, plans=result.plans, total_s=result.total_s,
        rows=passed_segment_rows(generation_dir))
    print(format_final_film(film))


def _finish_ref_handoff(result, generation_dir: Path, brief: Brief,
                        state: dict, finished: bool) -> None:
    """逐段陪跑的收尾（首跑与续跑**同一份**，issue #57/#58）。

    抽出来是因为它有两个主人——首跑（``_phase2_ref_finish``）与续跑
    （``_resume_ref_handoff``）。两处各写一遍的代价不是重复几行，而是**「跑完才转
    completed」这个决定有两个落点**：续跑那一份漏改一次，跑完的会话就还挂在可续接
    列表里（同 #17 的收尾，本仓「规则在、接线不在」的老形态）。
    """
    from ..ref_segments import segment_ledger_path

    # 全段通过后自动拼成成片（issue #57 AC1）；某段没过时它自己拒绝并说清卡在哪一段
    # （AC3）——「未通过即不拼」这条规矩住在被拼接的那一处，不在这里的 if 里。
    _assemble_ref_final_film(result, generation_dir)
    # 账本路径当面给出：没跑完时它是「已经通过的那几段」的唯一去处，用户要接着
    # 往下做就得知道去哪儿找（账里的行只在段产出账里，不在别处）。
    print(f"段产出账（{'全部段已通过' if finished else '已通过的段'}）："
          f"{segment_ledger_path(generation_dir)}")
    if finished:
        # 跑完才转 completed（issue #58）：转早了，中断的会话就不再是「可续接」的，
        # 续跑语义会被永久劫持（同 #17 的收尾）。
        save_session(generation_dir, brief, state, status=STATUS_COMPLETED)
        return
    print(f"[续接] 本次没有跑完全部段，会话保持「多参分段进行中」：下次重跑向导会落回 "
          f"{generation_dir.name}，从已通过的那一段之后继续（阶段 1/2 不重跑）。")


def _write_ref_segment_now(source, position: int, total: int) -> tuple[str, list]:
    """取当前段正文：>15s 的按需写段作业**此刻**写；≤15s 直出取已落盘的整片文本。

    写这一段被 ``step_scope`` 包住：横幅「正在写第 N/M 段」＋等待心跳成对出现
    （issue #70 / ADR 0019）——写段是直调长调用，卡住/重试只有订阅者看得见。
    写完立刻落盘（``RefSegmentJob.write_segment`` 负责），打印交给调用方。
    """
    from ..ref_segments import RefSegmentJob

    if isinstance(source, RefSegmentJob):
        with step_scope(f"正在写第 {position}/{total} 段",
                        round_no=position, total_rounds=total):
            return source.write_segment(position)
    # ≤15s 直出：整片六段式就是本段提示词，交付时已落盘（不重写、不套注记）。
    return source.texts[position - 1], []


def _run_ref_segment_handoff(result, generation_dir: Path,
                             *, passed: dict[int, dict] | None = None) -> bool:
    """多参逐段陪跑（issue #55/#56/#70）：**轮到哪段才写哪段** → 出词 → 交回本段输出视频
    → **可证层**硬阻断 → **语义层**剥首/末帧读图并排人判 → 五档出路 → 落段产出账。

    节奏与素材循环（#50）同形：系统只维持一个「当前待办」，交回并验收通过才出下一段。
    两条票面要求在这里同时成立（issue #70 的裁定）：

    - **写完立刻落盘并打印**：写那一段的调用被 ``step_scope`` 包住（横幅「正在写第 N/M
      段」＋心跳），写完当场落盘、当场把正文打到终端；
    - **段 N 的词只能在段 N-1 通过之后出现**（#55 AC1/#56）：所以写段发生在**这里**的
      循环里，而不是交付阶段把整片一次写完——``result`` 若是 ``RefSegmentJob``（>15s
      按需写段作业）就现场写，若是 ``RefSegmentResult``（≤15s 直出）则文本早已落盘。

    两道闸的边界不同（#56 的裁定）：**可证层**（``_gate_segment_handoff``）判得出来，
    不合格硬阻断；**语义层**判不出来（「画面是否达到声明的起止状态」是启发式），
    所以只劝阻、由人下结论，人可显式覆盖。

    「落进度」在这里就是落段产出账那一行通过行：被拦下的段、以及判为「重跑本段/修改
    提示词/停下」的那几轮都**不落**（``event=output`` 的行在不在＝这段过没过），但每次
    遭遇都落一行 ``event=encounter``（判定/出路/尝试次数），改写轮次落 ``event=rewrite``。

    **闸门的牙齿有边界**：可证层按实测帧数档表判（``H3_TIER_FRAMES`` 只有 4/5/6s），
    所以「用错了时长档」在**交回的那一段落进已知档**时才报 error；计划与交回**双双**
    落在 7–15s 未知档时只能报 warning（测不出事实就不许阻断，见 ``check_segment_video``）。
    那是既有的可证性分级，不在这票放宽或收紧。

    ``passed``：段产出账里**已通过**的段（``{段号: 行}``），续跑（issue #58）据此跳段——
    已通过的段**不重出提示词、不重跑、不重付**：段文本本来就落盘着
    （``segments/segment-NN.md``），重打一遍既让用户以为要再生成一次，也会把这些段的
    语义层读图（视觉计费）再付一遍。

    **两个调用方的分工**：续跑入口（``_resume_ref_handoff``）先过
    ``read_ref_handoff_progress`` 的段表核对，再把结果传进来；首跑路径
    （``_phase2_ref_finish``）不传、由这里现读——那个时刻账**必然为空**（有账就说明
    陪跑开始过，会话会走续跑入口而不是首跑），所以「账与段表对不上」的核对留在续跑
    那一处即可，不必在这里再抛一次会变成裸 traceback 的错。

    返回 True ＝ 全部段交回并通过；False ＝ 停住（空输入，或被拦下后不再交）。
    """
    from ..ref_segments import (
        format_segment_issue_lines,
        log_segment_output,
        passed_segment_rows,
        render_copy_block,
        render_ref_segment_briefing,
    )

    plans = list(result.plans)
    total = len(plans)
    if passed is None:
        passed = passed_segment_rows(generation_dir)
    # 全片时长取**交付时那个**（``total_s``＝注记句与交付校验共用的口径），不用
    # ``plans[-1].end_s``：规划器允许末段 end 略超 total（留余量），拿末段 end 当 total
    # 会让改写后的重过校验按另一个期望注记判「缺注记」——同一份文本交付时是过的
    # （复审实测，issue #56）。
    total_s = result.total_s
    for position, plan in enumerate(plans, 1):
        if position in passed:
            # 续跑跳段（issue #58 AC2）：不重出提示词、不重跑、不重付。当面说清跳了哪一段、
            # 用的是哪份视频（账上记的路径＋SHA256 是事后复跑那次验收的唯一入口）。
            row = passed[position]
            print(f"\n[续接] 第 {position}/{total} 段已通过验收，跳过"
                  f"（不重出提示词、不重跑、不重付）：{row.get('video', '')}")
            continue
        segment_file = result.segment_paths[position - 1]
        # 轮到这一段才写（issue #70）：横幅＋心跳包住这一次直调，写完立刻落盘——
        # 打印就在下面，与交回口紧挨着，用户第一眼看到的就是「现在该做什么」。
        text, issues = _write_ref_segment_now(result, position, total)
        # 逐段当前文本：改写一轮就地替换它（下一轮改写以新词为底），不回写 result.texts
        # ——那个字段是**交付时**那一份（≤15s 直出），改写后的事实以段文件为准。
        print("\n" + "=" * 60)
        print(render_ref_segment_briefing(plan, position, total, segment_file))
        # 本段接线卡在复制块**之前**（issue #75 AC4）：先看这段要接哪几张图、接哪个槽，
        # 再看要复制进 ComfyUI 的正文。文本与 `segments/injection-card-NN.md` 同源
        # （同一渲染函数），两处各拼一遍就会各漂各的。
        card = getattr(result, "segment_asset_card", None)
        card_text = card(position) if callable(card) else ""
        if card_text:
            print(card_text)
        # 正文包在复制边界里（issue #74）：抬头已在上方、交回提示在下方，两者都在
        # 标记之外——用户在终端划选时不会把段号或问句带进 ComfyUI 提示词框。
        print(render_copy_block(text))
        # 交付前校验不当场跑：分段是「产出即交付」的路径，error 级结论绝不静默
        # （warning 不刷屏，理由同 ``format_ref_segment_report``）。
        for line in format_segment_issue_lines(position, issues):
            print(line)
        print("-" * 60)
        attempt = 0
        while True:
            raw = _clean_path_input(_prompt(
                f"第 {position}/{total} 段输出视频路径（必填；回车=停下）："))
            if not raw:
                # 空输入＝停住（AC3）：既不跳过也不降级——跳过会让成片缺一段，
                # 降级（拿提示词当产出）会让账上记着一段没人生成过的视频。
                print(f"\n⚠ 第 {position}/{total} 段没有交回输出视频：逐段陪跑停在这里——"
                      "这一段不落账，下一段的提示词也不出。"
                      "已交回并通过的段留在段产出账里。", flush=True)
                return False
            video = Path(raw)
            if not video.is_file():
                # 「文件不在」是可证事实，不许走「量不出来」那条降级路（那条只留给
                # 真产物读不出元数据的场合）；再说下面算 SHA256 也会直接抛异常。
                print(f"[错误] 读不到这个文件：{video}——"
                      "请交回本段在 ComfyUI 生成好的输出视频路径。", flush=True)
                continue
            if _gate_segment_handoff(str(video), float(plan.duration_s)):
                print("→ 请重新交回**本段**的输出视频（回车=停下）。", flush=True)
                continue
            attempt += 1
            first_frame, last_frame, first_desc, last_desc = _capture_ref_segment_frames(
                str(video), position, generation_dir, attempt)
            outcome = _judge_ref_segment_semantics(
                generation_dir=generation_dir, plan=plan, position=position, total=total,
                first_description=first_desc, last_description=last_desc,
                first_frame=first_frame, last_frame=last_frame, attempt=attempt)
            if outcome in (GATE_CONTINUE, GATE_ACCEPT):
                log_segment_output(
                    generation_dir, segment=position,
                    start_s=plan.start_s, end_s=plan.end_s, video_path=video)
                print(f"[完成] 第 {position}/{total} 段已交回并通过可证层与语义层校验，"
                      "已记入段产出账。")
                break
            if outcome == GATE_REWORD:
                revised = _revise_ref_segment_round(
                    text, position=position, total=total, plan=plan, total_s=total_s,
                    segment_file=segment_file, generation_dir=generation_dir,
                    annotated=result.annotated)
                if revised is not None:
                    text = revised
                # 取消/失败都回交回口：词没动，用户可重交或重选出路
                continue
            if outcome == GATE_RERUN:
                print(f"→ 重跑本段（不改词）：回 ComfyUI 用**原提示词**重新生成"
                      f"第 {position}/{total} 段，然后把**新的**输出视频路径交上来。",
                      flush=True)
                continue
            # GATE_STOP：默认出路。停下不是失败——已通过的段留在账上，重跑向导会从
            # 最后一个已通过的段之后接着走（issue #58）。
            print(f"\n⚠ 第 {position}/{total} 段停在语义层：这一段不落账，"
                  "下一段的提示词也不出。已交回并通过的段留在段产出账里。", flush=True)
            return False
    print(f"\n✓ 全部 {total} 段已交回并通过可证层与语义层校验。")
    return True


def _capture_ref_segment_frames(video_path: str, position: int, generation_dir: Path,
                                attempt: int,
                                ) -> tuple[Path | None, Path | None, str | None, str | None]:
    """剥本段**首帧与末帧**并读图（issue #56 AC1）。

    与桥接链的 ``_capture_bridge_frame`` 差别在取哪一帧：多参段没有「下一段的首帧」
    这回事，核的是**本段自己的开场与收尾**，所以首帧取 0.00s 那一帧（不做锐度优选）。

    文件名带**本段的尝试次数**（``segment-NN-tryK-first/last.png``）：同一段会被反复
    交回（重跑/改词再掷），定名覆盖会让旧遭遇行里记的帧图路径指向新一轮的帧，而那条
    路径正是「事后复跑那次判定」的唯一入口（本仓对视频记 SHA256 是同一条理由）。

    返回 ``(首帧图, 末帧图, 首帧读图, 末帧读图)``。剥帧/读图任一步失败都只警告降级：
    **绝不伪造读图记录**，也绝不中断人工陪跑——语义核验照旧摆出来，缺的那一半由
    ``render_ref_segment_semantic_check`` 说破（缺一半的并排看起来像「没问题」）。
    """
    from ..tools.frame_auditor import (
        describe_segment_frame,
        extract_first_frame,
        extract_last_frame,
    )

    frames: list[Path | None] = []
    descriptions: list[str | None] = []
    for role, label, extract in (("first", "首帧", extract_first_frame),
                                 ("last", "末帧", extract_last_frame)):
        target = (Path(generation_dir) / "segments" / "frames"
                  / f"segment-{position:02d}-try{int(attempt)}-{role}.png")
        try:
            frame = extract(Path(video_path), target)
        except Exception as exc:  # noqa: BLE001 - 剥帧失败不阻塞人工流程
            print(f"[警告] 剥本段{label}失败（{exc}）：本次没有取到帧，绝不伪造读图记录"
                  "（语义核验照旧摆出来，缺的那一半会说破）。")
            frames.append(None)
            descriptions.append(None)
            continue
        print(f"[本段帧] ✓ 已剥出本段{label}：{frame}")
        with _progress_scope(f"正在读取本段{label}实际画面……"):
            descriptions.append(describe_segment_frame(frame, role))
        frames.append(frame)
    return frames[0], frames[1], descriptions[0], descriptions[1]


def _judge_ref_segment_semantics(*, generation_dir: Path, plan, position: int, total: int,
                                 first_description: str | None, last_description: str | None,
                                 first_frame: Path | None, last_frame: Path | None,
                                 attempt: int) -> str:
    """多参语义层主判定（issue #56）：把「本段声明的起止状态」与「首/末帧实际画面」并排摆给人判。

    这一层存在的理由与桥接链的锚帧语义核验同源：证据曾经没在决策时刻被摆到一起——
    ``start_state``/``end_hook`` 只在展示提示词时出现过（比视频交回早十几分钟），剥帧
    当轮只打印读图结果，人得在两个时刻各记一半再凭记忆判。所以这里**并排**打，且打在
    人做决定的那一刻。

    **系统只劝阻、不硬阻断**（AC2）：画面是否「达到」那个状态是启发式，没有可靠的确定性
    判据，故结论由人下；判不合格只给五档出路（默认停下），其余全是显式覆盖。

    返回值是出路之一；**每次遭遇都落一行账**（``event=encounter``，含判定/出路/尝试次数
    ／帧图路径），但**不写**首尾帧链的 gate-log——两本账混在一起，将来给自动判据取阈值
    时分不开（AC8）。
    """
    from ..ref_segments import log_segment_encounter, render_ref_segment_semantic_check

    print("\n" + "-" * 60)
    print(render_ref_segment_semantic_check(
        plan=plan, position=position, total=total,
        first_description=first_description, last_description=last_description,
        first_frame=first_frame, last_frame=last_frame))
    print("-" * 60)

    reached = _confirm("首帧与末帧画面达到上面声明的起止状态了吗？", default=True)
    outcome = GATE_CONTINUE if reached else _choose_ref_segment_outcome(
        attempt, position=position, total=total)
    log_segment_encounter(
        generation_dir, segment=position,
        verdict="reached" if reached else "not_reached", outcome=outcome,
        attempt=attempt, first_frame=first_frame, last_frame=last_frame)
    return outcome


def _revise_ref_segment_round(text: str, *, position: int, total: int, plan,
                              total_s: float, segment_file: Path,
                              generation_dir: Path, annotated: bool = True) -> str | None:
    """「修改提示词」一轮（issue #56 AC4/AC6/AC7）：问意见 → 只改画面正文 → 落盘留痕。

    与「重跑本段」的分界：这一档**动词**（把人的意见写回本段提示词再掷），那一档不改词。
    空意见＝取消、改写失败（含模型动了锁死声明）＝不采用——两种情况都返回 None 且**词
    保持原样**，用户留在出路菜单里重选（静默采用半成品等于让人拿着自相矛盾的提示词去生成）。

    改写后**重过交付前校验**：LLM 动过的文本没理由比首次出词更可信。不合格**当面打出**
    但仍采用——用户的意见是他自己下的，产物交付、问题当面说（同 ``format_ref_segment_report``
    的纪律）；校验能拦的那类（模型动了锁死声明）在 ``revise_ref_segment_body`` 里已经拦下。

    定义块在改写里逐字取回（只动正文），交付时已按登记素材校验过 ``SEGMENT_DEFS_INCOMPLETE``，
    所以这里重过的是**正文相关**的那几条（指代前文/时间戳/对齐行/注记与状态句在位）。

    ``annotated``＝本段文本带不带分段注记（``RefSegmentResult.annotated``），**决定校验口径**：
    ≤15 秒直出的段是整片文本原样（不套注记），拿分段校验器去判它，那两条注记判据是
    结构性必挂的假错——口径要跟着文本的形态走，不是跟着「这是第几段」走。
    """
    from ..model_factory import build_chat_model
    from ..ref_segments import (
        log_segment_rewrite_round,
        revise_ref_segment_body,
        validate_segment_text,
    )
    from ..tools.h3_validator import format_issues

    feedback = _prompt(
        "改写意见（想怎么改，如「末态应该是罐子已经放下」；回车=取消改写）：").strip()
    if not feedback:
        print("已取消改写（没给意见，提示词不动）。")
        return None
    try:
        # 步骤横幅＋等待心跳成对出现（issue #70 / ADR 0019）：改写是一次直调长调用，
        # 与写段同形的反馈（横幅＝在做什么，心跳＝还活着吗）。
        with step_scope(f"正在按意见改写第 {position}/{total} 段"):
            new_text = revise_ref_segment_body(text, feedback, build_chat_model())
    except Exception as exc:  # noqa: BLE001 — 改写失败要响但不许裸崩：词不动、回菜单再定
        print(f"[错误] 改写失败：{exc}；本段提示词保持原样（可重选出路）。")
        return None
    issues = validate_segment_text(
        new_text, annotated=annotated, duration=plan.duration_s,
        segment_number=position, total_segments=total, start_s=plan.start_s,
        total_s=total_s)
    errors = [issue for issue in issues if getattr(issue, "severity", "") == "error"]

    segment_file.write_text(new_text, encoding="utf-8")
    log_segment_rewrite_round(
        generation_dir, segment=position, feedback=feedback,
        prompt_before=text, prompt_after=new_text)
    print(f"\n[改写] 第 {position}/{total} 段提示词已改写并写回 {segment_file}"
          "（本轮前后全文已记入段产出账）。请用**新提示词**重新生成，"
          "然后把新的输出视频路径交上来。")
    print("-" * 60)
    print(new_text)
    print("-" * 60)
    if errors:
        print(f"[提醒] 改写后的本段提示词有 {len(errors)} 项交付前校验未过：")
        print(format_issues(errors))
    return new_text


def _deliver_injection_card(generation_dir: Path, brief: Brief) -> bool:
    """注入卡交付（issue #45/#49）：视频输出定案 → 回写 brief → 落盘接线清单。

    视频尺寸**不再单独提问**（issue #49，2026-10-18 裁定）——用户在画布已经选过
    尺寸，第二遍问答就是实测反馈里的「重复问」。定案规则（声明优先/画布推导）全在
    ``resolve_video_output`` 一份；特殊需求在卡上手改（写卡不覆盖手改，见
    ``write_injection_card``）。三元组**回写 brief**（复审：只落卡不回写，快照记
    null、卡上有值，两处对不上账）：随后 ``save_session``/``brief_snapshot`` 的
    逐字段清单就带着定案值。

    返回 False ＝ 超出官方容量上限（issue #86），调用方**停止本片**：这是设计边界，
    不是崩溃——片子还没开始生成，此刻改素材/拆段最便宜。台账与素材都还在盘上，
    改完重跑阶段 2 即可（与 ``_deliver_ref_segments`` 的超限同一套收口语）。

    容量判据按**路线**挑（``check_film_capacity``）：整片卡在 >15 秒路线上是库存清单，
    图数留给每段判；只有 ≤15 秒（整片即一次请求）才按整片判图数。
    """
    from ..asset_ledger import load_asset_ledger
    from ..injection_card import resolve_video_output, write_injection_card
    from ..ref_capacity import ReferenceCapacityError, check_film_capacity
    from ..ref_segments import is_single_segment_route

    resolution = resolve_video_output(brief.video_output, brief.canvases)
    params, notes = resolution.params, resolution.notes
    brief.video_output = params
    width, height = params.size
    if resolution.canvas is None:
        print(f"\n[视频输出] 采用既有声明（优先于画布推导）：{params.ratio} · "
              f"{params.megapixels:g} MP · 对齐 {params.multiple} → {width} × {height}")
    else:
        canvas_w, canvas_h = resolution.canvas
        print(f"\n[视频输出] 由画布 {canvas_w} × {canvas_h} 推导：{params.ratio} · "
              f"{params.megapixels:g} MP · 对齐 {params.multiple} → {width} × {height}")
        for note in notes:
            print(f"[提示] {note}（特殊需求可在卡上手改）")
    try:
        check_film_capacity(load_asset_ledger(generation_dir),
                            single_request=is_single_segment_route(brief.duration),
                            where="整片注入卡")
    except ReferenceCapacityError as exc:
        # 报错原文已点名超出的素材（第几张/哪几个文件），再补一句「接下来做什么」
        print(f"\n[错误] 本次多参请求超出官方容量上限，已停止（未写注入卡）：\n{exc}")
        print("  素材与台账都已落盘，改完素材或把镜头拆短后重跑阶段 2 即可。")
        return False
    result = write_injection_card(generation_dir, params, notes=notes)
    print(result.text)
    if result.written:
        print(f"✓ 注入卡已写入：{result.path}")
    else:
        print(f"✓ 注入卡已存在（可能含手改），保留原卡：{result.path}"
              "（需重出请删除该文件后重跑）")
    return True


def _phase2_ref_finish(config: Config, session: SessionState) -> int:
    """多参路线阶段 2（issue #41/#43/#45/#46/#50）：素材循环 → 视频音频挂账 → 注入卡 → 直出官方 ref 六段式。

    与首尾帧的阶段 2 是两件事：多参没有桥接帧／首尾帧可交，画面参考是**素材图**
    （画面描述以生图提示词为描述主体，素材画面事实规则）。总时长 **≤15 秒直出一份**
    （59- 工作流时长节点 max=15）；>15 秒按镜头边界拆成 4–15s 整数执行段，逐段出自
    包含六段式＋段清单/拼接顺序卡（issue #46；整片六段式仍落盘存档，拆段从它取材）。
    资产图不再一次性全量出词（issue #50）：`_asset_loop` 队列驱动逐张闭环（出词→审查→
    交回→校对→裁决），一张落账/放弃才出下一张。注入卡（issue #45）出在台账收口之后：
    台账→参考槽接线清单＋视频输出三元组，用户照卡进 ComfyUI 手动接线（系统不自动投喂）。
    """
    from ..ref_segments import is_single_segment_route, rotate_segment_ledger

    brief = session.brief
    state = dict(session.stage_state)
    generation_dir = session.directory

    _show_ref_registry(state, brief)
    # 素材循环（issue #50）：逐张出词→审查→交回→校对→裁决，一张落账/放弃才出下一张。
    # False ＝ 用户「先到这里」，会话保持可续接（进度已逐张落盘）。
    if not _asset_loop(state, brief, generation_dir):
        return 0
    # 视频/音频按需挂账 + 无提示词登记项补交（issue #43 三型）；在注入卡之前收口。
    _collect_extra_media(state, brief, generation_dir)
    if not _deliver_injection_card(generation_dir, brief):
        # 超出官方容量上限（issue #86）：卡片面点名了超出的素材，这里停住——
        # 再往下走只会烧掉一次注定被拒的组装。
        return 1
    # 组装开跑前先把入参落盘（issue #51 AC3）：视频输出三元组刚由画布推导/声明定案、
    # 回写进 brief，而组装是最贵一步（可能十几分钟）。此前这里只在 run_stage2 跑完
    # 之后才 save_session，于是「组装窗口内 Ctrl-C」会把尺寸定案与整份会话状态一起丢
    # ——正是 issue #47 实测的第一次中断。
    save_session(generation_dir, brief, state, status=STATUS_AWAITING_FRAMES)

    with _progress_scope("[阶段 2] 正在组装官方 ref 六段式视频提示词……（预计几分钟，期间无需输入）"):
        final_state, prompt = run_stage2(state, brief, config,
                                         on_node=_report_node, checkpoint_dir=generation_dir)
    _drain_stdin()
    # 逐段陪跑**开始之前**就不能标 completed（issue #58）：陪跑跨越几小时的人工生成，
    # 中途 Ctrl-C/关机时若会话已是 completed，重跑向导会另开新 GEN、阶段 1/2 重做，
    # 而段产出账里的进度**永远读不到**——与首尾帧链 #17 完全同形（#55 在这一处留了
    # 注释点名本票）。标成 ref_segments_running，跑完才转 completed。
    # 不复用 segmented_running：那个值会把多参会话路由进 base 的续接分支。
    save_session(generation_dir, brief, final_state, status=STATUS_REF_SEGMENTS_RUNNING)

    output_file = generation_dir / "video-prompt.md"
    output_file.write_text(prompt, encoding="utf-8")
    # 交付主体换位（issue #55）：H3 单次只能生成 4–15 秒，整片六段式是一坨不可执行的
    # 文本——>15s 时降级为**存档预览**（拆段从它取材），交付物是逐段交回的那几段视频。
    # ≤15 秒（issue #57 AC5）走**同一套收尾**：整片文本刚好就是那一段的提示词（段文件
    # 是它的副本），回传 → 两道闸 → 段产出账 → 成片一样不缺，短片的体验不与长片分叉。
    # 走到这里就说明会话没有「陪跑进行中」的进度（那种会话由 ``run_wizard`` 路由到续跑
    # 入口），所以这**是一次新的陪跑**：同一个 GEN 里可能还躺着上一次交付的段产出账
    # （用户拒绝了续接、重走一遍流程时会复用 GEN），挪开它，本轮进度从零记起。
    # 不挪的后果是拿上一次的通过行索引这一次的段——复审实测可达（issue #58）。
    rotated = rotate_segment_ledger(generation_dir)
    if rotated is not None:
        print(f"[提示] 本生成目录里还留着上一次陪跑的段产出账，已挪到 {rotated}"
              "（本轮从第 1 段重新记）。")

    if is_single_segment_route(brief.duration):
        result = _deliver_ref_single_segment(state, brief, prompt, generation_dir)
        if result is None:
            # ≤15 秒直出同样受官方 9 图上限约束（issue #86 AC4）：错误已当面打全，
            # 片子还没开始生成——与 >15s 的单段超限同一套收口。
            return 1
        print(f"\n[本片单段] 整片 ref 六段式已写入 {output_file}"
              "——本片只有一段，整片文本就是本段提示词"
              f"（段文件 {result.segment_paths[0]} 同文），并留作存档预览。")
    else:
        result = _deliver_ref_segments(state, brief, prompt, generation_dir)
        if result is None:
            # 单段素材超限（issue #75）：错误已当面打全，片子还没开始生成——
            # 不再往下陪跑（没有段可写），会话保持可续接，改完剧本重跑阶段 2 即可。
            return 1
        print(f"\n[存档预览] 整片 ref 六段式已写入 {output_file}"
              "（仅存档，不作交付物——交付物是逐段交回的那几段视频）。")
    finished = _run_ref_segment_handoff(result, generation_dir)
    _finish_ref_handoff(result, generation_dir, brief, final_state, finished)
    return 0


def _ask_optional_path(label: str) -> str:
    """询问可选的图片路径；空=跳过，但需二次确认，防止误触静默降级 T2VA。"""
    while True:
        raw = _clean_path_input(_prompt(f"{label}图片路径（回车=跳过）："))
        if raw:
            return raw
        print(f"[警告] 未提供{label}将影响变体（可能降级为纯文字 T2VA，视频没有画面参考）。")
        if _confirm(f"确认跳过{label}？", default=False):
            return ""
        # 不确认则重新询问


def _collect_frame_paths(variant: str) -> tuple[str, str]:
    """收集首/尾帧路径；空=跳过（需二次确认）。返回 (first_path, last_path)。"""
    roles = {role for _, role in _required_frames(variant)}
    first = last = ""
    if "first" in roles or variant.upper() == "FL2VA":
        first = _ask_optional_path("首帧")
    if "last" in roles or variant.upper() == "FL2VA":
        last = _ask_optional_path("尾帧")
    return first, last


def _by_role(rows) -> dict[str, dict]:
    """把帧路线那两份「按帧位成表的列表」索引成 role → 条目（只留对象行）。

    ``frame_images``（入库记录）与 ``fl2va_frame_descriptions``（读图结果）都是
    「每条带 role 的列表」，续跑的两处判断（路径从哪来、描述能不能复用）各自要按
    role 取——取法写两遍就多一个会漂的地方。
    """
    return {str(r.get("role")): r for r in (rows or []) if isinstance(r, dict)}


def _remembered_frame_paths(state: dict, variant: str) -> tuple[str, str] | None:
    """上一轮已入库的关键帧路径（issue #51 续跑点）；没有可沿用的记录时返回 None。

    判据是 ``frame_images``（`_store_frame` 写的入库记录）里**该变体要求的帧位都在**、
    且文件仍可解析：``source`` 用户原路径优先（人认它），正本副本 ``target`` 兜底。
    只沿用它不校验内容——静默比对反而会假装识别得了「这张图是不是同一张」。
    """
    from ..task_package import resolve_input_path

    records = _by_role(state.get("frame_images"))
    first = last = ""
    for _, role in _required_frames(variant):
        record = records.get(role)
        if record is None:
            return None
        path = ""
        for key in ("source", "target"):
            raw = str(record.get(key) or "")
            if not raw:
                continue
            try:
                if resolve_input_path(raw).is_file():
                    path = raw
                    break
            except (OSError, ValueError):
                continue
        if not path:
            return None
        if role == "first":
            first = path
        else:
            last = path
    return (first, last) if (first or last) else None


def _reusable_frame_descriptions(state: dict, records: list[dict]) -> list[dict] | None:
    """同一批帧图（sha 未变）上次的读图结果；有一个对不上就整批重读。

    为什么整批判断：只复用一部分会让「同一段视频的两帧」由两次不同调用产出，
    两次的措辞风格不一致，正文写出来两头打架。sha 是 ``_store_frame`` 现算的，
    换一张图必然变——这正是「不重做」与「别拿旧描述说新图」之间的那条线。
    """
    previous_records = _by_role(state.get("frame_images"))
    previous_descriptions = {
        role: row for role, row in _by_role(state.get("fl2va_frame_descriptions")).items()
        if str(row.get("description") or "").strip()
    }
    if not records:
        return None
    out: list[dict] = []
    for record in records:
        role = str(record.get("role"))
        before = previous_records.get(role)
        description = previous_descriptions.get(role)
        if before is None or description is None:
            return None
        if str(before.get("sha256")) != str(record.get("sha256")):
            return None
        out.append(dict(description))
    return out


def _required_frames(variant: str) -> tuple[tuple[int, str], ...]:
    from ..tools.frame_auditor import required_frames
    return required_frames(variant)


def _resolve_frame_file(raw: str, role: str) -> Path | None:
    if not raw:
        return None
    from ..task_package import resolve_input_path

    path = resolve_input_path(raw)
    if not path.is_file():
        raise FileNotFoundError(f"{role}图片不存在：{path}")
    return path


def _phase2_collect_and_finish(
    config: Config, session: SessionState, *, show_frame_prompts: bool = True
) -> int:
    brief = session.brief
    state = dict(session.stage_state)
    generation_dir = session.directory

    # 进入阶段 2 前展示生图提示词：续接一个已完成阶段 1 的会话时，用户手里没有提示词，
    # 就无法生成关键帧图、也就没有路径可提交（2026-09-24 实测）。同进程内已展示过
    # （新跑/续接阶段 1 的收尾）就不重复刷屏。
    if show_frame_prompts:
        _show_frame_prompts(state, generation_dir)
        # 资产图提示词不随关键帧提示词出站（#40 回滚，同 `_user_revision_loop` 的理由）：
        # 本函数只服务首尾帧链；多参的素材循环走 `_phase2_ref_finish`。上一版在这里
        # 无条件多打一遍三份资产提示词并落盘，首帧用户会以为自己进了多参流程。
    print("\n[阶段 2] 请提交生成好的关键帧图片（回车=跳过该项）。")
    remembered_frames = _remembered_frame_paths(state, brief.variant)
    while True:
        try:
            if remembered_frames is not None:
                # 续跑（issue #51 AC1）：上一轮已经交过并入库过同一批帧图，路径与读图
                # 结果都在盘上——不再问一遍路径，也不重付读图。
                first_raw, last_raw = remembered_frames
                print(f"[续跑] 沿用上次提交的关键帧图：首帧={first_raw or '（无）'}，"
                      f"尾帧={last_raw or '（无）'}（要换图请新开一次生成）。")
            else:
                first_raw, last_raw = _collect_frame_paths(brief.variant)
            first_path = _resolve_frame_file(first_raw, "首帧")
            last_path = _resolve_frame_file(last_raw, "尾帧")
        except FileNotFoundError as exc:
            print(f"[错误] {exc}")
            if not _confirm("重新提交路径？", default=True):
                return 1
            remembered_frames = None  # 沿用的路径坏了：退回正常问答
            continue
        break

    have_first, have_last = first_path is not None, last_path is not None
    original = brief.variant
    effective = resolve_effective_variant(original, have_first, have_last)
    downgraded = ""
    if effective != original:
        brief.variant = effective
        downgraded = f"{original}→{effective}"
        label = {"I2VA": "仅首帧模式（I2VA）", "L2VA": "仅尾帧模式（L2VA）", "T2VA": "纯文字模式（T2VA）", "FL2VA": "首尾帧模式（FL2VA）"}[effective]
        print(f"[提示] 未提供完整首尾帧，已切换为{label}。")
    if first_path and last_path and first_path == last_path:
        print("[警告] 首帧与尾帧是同一张图，视频将几乎静止。")

    # 帧图入库 + 读图
    descriptions = []
    frame_records = []
    if have_first or have_last:
        from ..tools.frame_auditor import audit_frame_images

        refs = []
        frame_slots = {role: picture for picture, role in _required_frames(effective)}
        if have_first:
            record = _store_frame(first_path, generation_dir, "first")
            frame_records.append(record)
            refs.append(RefItem(picture=frame_slots.get("first", 1), name="首帧", description="", path=str(first_path)))
        if have_last:
            record = _store_frame(last_path, generation_dir, "last")
            frame_records.append(record)
            refs.append(RefItem(picture=frame_slots.get("last", 2), name="尾帧", description="", path=str(last_path)))
        def report_frame(frame_role: str) -> None:
            print(f"正在读取{frame_role}的实际画面……", flush=True)

        # 入库记录**先落盘**再读图（issue #51 AC3）：读图是一次慢速网络调用，中断若落在
        # 它上面，路径与正本副本已经攒齐、只差描述——先写盘，续跑就不必再问一遍路径。
        state["frame_images"] = list(frame_records)
        save_session(generation_dir, brief, state, status=STATUS_AWAITING_FRAMES)
        descriptions = _reusable_frame_descriptions(state, frame_records)
        if descriptions is not None:
            # 续跑（issue #51）：帧图没换（sha 同）→ 上次的读图结果仍然成立，不重付一次
            # 视觉调用，也不覆盖，用户手里的提示词与上次一致。
            print("[续跑] 沿用上次的关键帧读图结果（帧图未换），不重付读图。")
        else:
            with _progress_scope(f"{config.vision_model.model} 正在读取关键帧实际画面……"):
                audits = audit_frame_images(refs, brief.variant, on_frame=report_frame)
            descriptions = [a.to_dict() for a in audits]
            for audit in audits:
                label = "第一帧" if audit.role == "first" else "最后一帧"
                print(f"\n[{label}实际画面] {audit.description}")

    state["fl2va_frame_descriptions"] = descriptions
    state["frame_images"] = frame_records
    # 组装开跑前把帧图入参落盘（issue #51 AC3）：帧图路径、入库记录与读图结果都在
    # 上面这几行里才攒齐，而 run_stage2 是最贵一步。此前只在它跑完之后才 save_session，
    # 于是组装窗口内 Ctrl-C 会让用户重填路径、重付一次读图（issue #47 实测现场）。
    save_session(generation_dir, brief, state, status=STATUS_AWAITING_FRAMES)

    with _progress_scope("[阶段 2] 正在组装最终视频提示词并进行质检精修……（预计几分钟，期间无需输入）"):
        final_state, prompt = run_stage2(state, brief, config, on_node=_report_node, checkpoint_dir=generation_dir)
    _drain_stdin()
    # 长视频接下来要进分段陪跑：此刻**不能**标 completed，否则陪跑中断后重跑向导会
    # 另开新 GEN、阶段 2 重做（issue #17）。标成 segmented_running，跑完才转 completed。
    long_form = brief.duration > 10
    save_session(generation_dir, brief, final_state,
                 status=STATUS_SEGMENTED_RUNNING if long_form else STATUS_COMPLETED)
    if downgraded:
        session_downgraded = downgraded
        _record_downgrade(generation_dir, session_downgraded)

    output_file = generation_dir / "video-prompt.md"
    output_file.write_text(prompt, encoding="utf-8")

    # 中文摘要：从大段提示词中提炼整体走向供快速核对；失败降级不阻塞。
    summary_obj = None
    try:
        from ..model_factory import build_chat_model

        with _progress_scope("正在生成中文摘要（便于核对视频走向）……"):
            summary_obj = _load_or_make_summary(prompt, build_chat_model(), generation_dir)
        _drain_stdin()
    except Exception:  # noqa: BLE001 - 摘要是增强体验，生成失败仅提示
        summary_obj = None
    if summary_obj is not None:
        # 只展示整体走向：逐镜头清单已由分段陪跑的 [本段中文摘要] 逐段展示，避免重复
        print("\n" + "=" * 60)
        print("[中文摘要] 整体视频走向（供人工核对）")
        print("=" * 60)
        print("【整条视频走向】")
        print(summary_obj.overall)
    else:
        print(f"[提示] 中文摘要生成失败，完整提示词见文件：{output_file}")

    if brief.duration > 10:
        # 长视频：不倾倒整条提示词，改走逐段陪跑（每段独立完成+验收后才给下一段）。
        # 必须传**阶段 2 之后的 state**：帧读图结果写在上面的局部副本里（旧 session 对象上没有），
        # 分段流程拿不到它就会退回照分镜表写（2026-09-22 首帧不锚定缺陷）。
        print(f"\n✓ 完整提示词已写入：{output_file}（供存档，长视频请按下方分段执行）")
        # 走到这里就说明会话没有「分段进行中」的进度（那种会话由 run_wizard 路由到
        # ``_resume_segmented``），所以这**是一次新的分段陪跑**：同一个 GEN 里可能还
        # 躺着上一次交付的段产出账与旧进度（用户拒绝了续接、重走一遍流程）。不挪开
        # 就会被读成本轮进度（追加账从不清理，多参侧实测过同形缺陷，issue #58），
        # 或者让本轮被永久判成旧会话（于是新账永远不写、成片永远拼不出来）。
        from ..shot_ledger import rotate_shot_progress

        rotated = rotate_shot_progress(generation_dir)
        if rotated:
            print("[提示] 本生成目录里还留着上一次陪跑的进度记录，已挪到 "
                  + "、".join(str(path) for path in rotated)
                  + "（本轮从第 1 段重新记）。")
        _run_long_form(brief, session, prompt, summary_obj, final_state, generation_dir)
    else:
        # 短视频：直接展示完整提示词，方便立即复制进 ComfyUI。
        print(f"\n✓ 最终视频提示词已写入：{output_file}")
        print("\n" + "=" * 60)
        print(prompt)
        print("=" * 60)
    return 0


def _load_or_make_summary(prompt: str, llm, directory: Path):
    """生成或复用中文摘要；结果持久化为 summary-zh.json / summary-zh.md。

    续接（progress.json 已存在）时直接读 JSON，避免重复 LLM 调用。
    """
    import json

    from ..summary import PromptSummary, render_summary_zh, summarize_prompt_zh

    json_path = directory / "summary-zh.json"
    if json_path.is_file():
        try:
            payload = json.loads(json_path.read_text(encoding="utf-8"))
            if isinstance(payload, dict) and payload.get("overall"):
                return PromptSummary(
                    overall=str(payload["overall"]),
                    shots=payload.get("shots") if isinstance(payload.get("shots"), list) else [],
                )
        except (OSError, ValueError):
            pass  # 损坏则重新生成
    summary = summarize_prompt_zh(prompt, llm)
    if summary is None:
        return None
    try:
        json_path.write_text(
            json.dumps({"overall": summary.overall, "shots": summary.shots},
                       ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        (directory / "summary-zh.md").write_text(render_summary_zh(summary) + "\n", encoding="utf-8")
    except OSError:
        pass  # 落盘失败不影响本次展示
    return summary


# ---------------------------------------------------------------------------
# 长视频分段陪跑（>10s：ComfyUI H3 时长选项只有 4-10s 整数档，必须逐段生成）
# ---------------------------------------------------------------------------

def _gate_segment_text(position: int, text: str, duration: float | None,
                       start_s: float) -> list:
    """交付前校验一段分段提示词并当面示警；返回 error 级 issue（空 = 放行）。

    分段是唯一「产出即交付」的路径：不合格的提示词会被用户直接粘进 H3，
    所以必须显式告知坏在哪，绝不静默交付。两条分段路径（规划式与回退式）共用。

    本函数**不收 variant**：分段恒为单图开场锚，校验口径与写段模板同源
    （``segment_prompts.SEGMENT_ANCHOR_VARIANT``，理由见该常量）。跟着
    ``brief.variant`` 走会让整片锚定行漏进段的产出被判合格。
    """
    from ..segment_prompts import validate_segment
    from ..tools.h3_validator import format_issues, only_errors

    issues = validate_segment(text, duration, start_s)
    if not issues:
        return []
    errors = only_errors(issues)
    print("\n" + "!" * 60)
    print(
        f"[校验未通过] 第 {position + 1} 段有 {len(errors)} 项 error，直接喂给 H3 会跑偏："
        if errors
        else f"[校验提醒] 第 {position + 1} 段有 {len(issues)} 项 warning："
    )
    print(format_issues(issues))
    print("!" * 60)
    return errors


def _gate_segment_handoff(video_path: str, expected_seconds: float | None) -> list:
    """可证层交接校验：本段交上来的输出视频可信吗（issue #13）。

    返回 error 级 issue（空 = 放行）；调用方据此**硬阻断**（不写下一段、不落盘进度）。
    warning 级一律显式打出，绝不静默放行。

    判据取 ``tools/preflight.py`` 的三档（可证 → error／启发式 → warning）。这里**故意
    不调** ``check_input_frame``：向导的锚帧是当场从这段视频剥出来的，拿它比**同一段视频**
    的尾窗恒等（窗口最小 mad 必然 < ``MAD_MAYBE``）——接上去是惰性闸门，比不接更坏
    （看起来有牙齿）。那个函数的 MAD 档要等「用户回报实际投喂的那张图」的输入点才可能报
    （见 issue #14）。
    """
    from ..tools.h3_validator import format_issues
    from ..tools.preflight import Issue, check_segment_video

    path = Path(video_path)
    if path.is_file():
        issues = check_segment_video(path, expected_seconds)
    else:
        # 「文件不在」是**可证错误**，不是「量不出来」：``check_segment_video`` 对读不到的
        # 文件只报 warning（测不出事实就不许阻断），而剥帧失败那条降级路会让它一路放行。
        # 后果在 #94 之后变成硬的：段产出账要给这段视频算 SHA256，账上还必须留一条
        # #96 拼得动的路径。多参陪跑早就是这条口径（``_run_ref_segment_handoff`` 先判
        # ``is_file``），这里只是把同一条补上。
        issues = [Issue("error", "segment_video_missing",
                        f"读不到这段输出视频：{path}（文件不在，或路径打错了）")]
    if not issues:
        return []
    errors = [i for i in issues if i.severity == "error"]
    print("\n" + "!" * 60)
    print(
        f"[交接校验未通过] 本段输出视频有 {len(errors)} 项可证错误，不进入下一段："
        if errors
        else f"[交接校验提醒] 本段输出视频有 {len(issues)} 项提示："
    )
    print(format_issues(issues))  # 与 _gate_segment_text 同一套渲染（preflight.Issue 同形）
    if errors:
        # 出路必须与**本闸门实际判的东西**对齐：它判的是这段视频（时长/尺寸），
        # 所以"换一张帧图"在这里无效——那种口子要等 #14 的「实际投喂图」输入点。
        print("出路：确认交上来的是**本段**的输出视频，且时长与本段计划一致"
              "（交错了段、用错了时长档都会在这里被拦下）。")
    print("!" * 60)
    return errors


# 闸门遭遇的判定（#14 动作集）。这些字面值会写进 segments/gate-log.jsonl，是将来给
# 自动判据（#15）校准阈值**唯一**的素材来源，所以不许悄悄改含义。
# 多参路线（#56）复用其中四个（continue/accept/rerun/stop）记进**段产出账**——
# 同一套字面值、两本账（gate-log 是首尾帧链的锚帧人裁，段产出账是多参段自己的遭遇）；
# 两条路线的「继续/显式接受/重跑本段/停下」是同一个意思，再造一套同义字面值只会让
# 将来合并样本时对不上。
GATE_CONTINUE = "continue"                  # 人判：尾帧画面达到了上一段的末态
GATE_ACCEPT = "accept"                      # 明知未达到，显式覆盖「接受并继续」
GATE_RERUN = "rerun"                        # 重跑本段（回 ComfyUI 重生成，再交一次视频）
GATE_MANUAL_FRAME = "manual_frame"          # 手工换帧（换一张尾帧图，仍在桥接链上）
GATE_KEYFRAME_RESTART = "keyframe_restart"  # 改用关键帧图另起（放弃桥接链；#10 的 B，只作兜底）
GATE_STOP = "stop"                          # 停下（默认出路）
# 多参专有（#56 AC4）：把用户意见写回本段提示词再掷——对位素材循环的「改词再掷」。
# 首尾帧链没有这一档（它的偏差是「下游锚帧」问题，不是「这段提示词写错了」）。
GATE_REWORD = "rewrite_prompt"

_OUTCOME_LABELS = {
    GATE_CONTINUE: "画面达到末态，继续",
    GATE_ACCEPT: "显式覆盖：接受并继续",
    GATE_RERUN: "重跑本段",
    GATE_MANUAL_FRAME: "手工换帧",
    GATE_KEYFRAME_RESTART: "改用关键帧图另起",
    GATE_STOP: "停下（默认）",
}

# 多参语义层的五档出路（#56 AC3）。**菜单文案与取值映射同住这一处**：两处各写一遍
# 就是又一个会各自漂移的字面量，而这里漂了没人看得出（选项错位照样能跑）。
# 顺序＝菜单顺序：1 是默认（停下），其余全是显式覆盖。
_REF_OUTCOME_ORDER = (GATE_STOP, GATE_REWORD, GATE_RERUN, GATE_CONTINUE, GATE_ACCEPT)
_REF_OUTCOME_LABELS = {
    GATE_STOP: "停下（默认）——回 ComfyUI 重做本段，改好再来",
    GATE_REWORD: "修改提示词——把你的意见写回本段提示词（只动画面正文），再掷一次",
    GATE_RERUN: "重跑本段——不改词，用原提示词重新生成，再交一次视频",
    GATE_CONTINUE: "继续——重掷后已达标（我改判它达到了）",
    GATE_ACCEPT: "显式接受——明知没达到仍继续（会记进段产出账）",
}


def _log_gate(directory: Path, record: dict) -> None:
    """把一次闸门遭遇追加进 ``segments/gate-log.jsonl``（**每次**遭遇都记）。

    为什么两种判定都记：「自动判据」票（#15）要的是「尾帧图 ＋ 末态声明 ＋ 人的判定」
    这套**分离边界**，只留失败样本会让边界偏斜。记录里的帧图路径是事后复跑的唯一入口
    ——没有它，攒下来的样本只是一堆无法重放的结论。
    """
    import json

    seg_dir = Path(directory) / "segments"
    seg_dir.mkdir(parents=True, exist_ok=True)
    with open(seg_dir / "gate-log.jsonl", "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def _semantic_reference(previous_text: str | None) -> str | None:
    """从一段提示词正文取「该达到的末态」参照：收尾句。

    v2 规划路径用 ``plan.end_hook``（规划层显式声明的末态）；机械拆分不产出它，
    所以回退路径退而取上一段正文的最后一行——官方格式下它就是该段最后一个动作节拍。
    展示时会标明是降级参照，不假装它是 end_hook。
    """
    if not previous_text:
        return None
    lines = [line.strip() for line in previous_text.strip().splitlines() if line.strip()]
    return lines[-1] if lines else None


def _previous_segment_tail(seg_dir: Path, position: int) -> str | None:
    """读上一段**落盘的那份**提示词（``shot-NN.md``）的收尾句。

    读磁盘而不是内存里的 ``segments[position - 1]``：续接时前面几段不在内存里，
    而磁盘上那份正是当时交付给用户、也就是 H3 实际吃进去的文本。
    """
    if position <= 0:
        return None
    path = Path(seg_dir) / f"shot-{position:02d}.md"
    if not path.is_file():
        return None
    try:
        return _semantic_reference(path.read_text(encoding="utf-8"))
    except OSError:
        return None


def _anchor_state(state: dict, position: int, description: str | None) -> None:
    """语义判定**放行后**才把这张图写成本段的开场锚（段号 0-based，消费方按此取）。

    读图失败就什么都不写：诚实降级成立的前提是绝不伪造读图记录。写在判定之后而不是
    剥帧之后，是因为判定可能判「重跑本段」——提前写进去，那段错锚就会留在 state 里，
    而 ``_bridge_frame_descriptions`` 同段号取最后一个，会把错锚当成事实喂给写段 LLM。
    """
    if not description:
        return
    state.setdefault("bridge_frame_descriptions", []).append(
        {"segment": position, "description": description}
    )


def _promised_last_frame_description(state: dict) -> str | None:
    """用户提交的**尾帧**读图结果；没有则 None。

    这是 FL2VA/L2VA 的硬承诺：整条视频必须收在这张画面上。没有它就没有可核的东西——
    无尾帧变体（I2VA / T2VA / 用户没交尾帧）**什么都不做**，凭空报一句「未达到」就是误报。
    """
    for item in state.get("fl2va_frame_descriptions") or []:
        if not isinstance(item, dict) or str(item.get("role")) != "last":
            continue
        description = str(item.get("description", "")).strip()
        if description:
            return description
    return None


def _choose_gate_outcome(attempt: int, *, last_segment: bool = False) -> str:
    """未达到时的出路。默认**停下**（劝阻），其余都是显式覆盖。

    #10 裁定：动作集**不含自动重掷**——重掷不是独立实验（同一载荷换个 seed 不算新证据）。
    「改用关键帧图另起」是 #10 的 B 方案，只作兜底不作常规路径，所以到第二次被拦才提示它。

    ``last_segment=True`` 时菜单少掉「手工换帧／改用关键帧图另起」：那两条都是**换一个
    下游锚**，而末段没有下游——整条视频已经结束，没有下一段的 Picture 1 可换。
    """
    print("\n" + "!" * 60)
    print("[劝阻] 画面没有达到上面那个声明——默认**不继续**。")
    if last_segment:
        print("  整条视频的最后画面就是承诺本身：这里不一致，成片就没有兑现承诺。")
        print("  出路：")
        print("    1. 停下（默认）——回 ComfyUI 重做末段，改好再来")
        print("    2. 重跑末段——末段已重生成好，重新交一次输出视频")
        print("    3. 接受并继续——明知没落在承诺的尾帧上仍继续（会记进 gate-log.jsonl）")
        mapping = {"": GATE_STOP, "1": GATE_STOP, "2": GATE_RERUN, "3": GATE_ACCEPT}
    else:
        print("  桥接链是逐段累积的：这一段的偏差会被下一段当成既成事实继承下去。")
        print("  出路：")
        print("    1. 停下（默认）——回 ComfyUI 重做本段，改好再来")
        print("    2. 重跑本段——本段已重生成好，重新交一次输出视频")
        print("    3. 手工换帧——自己换一张尾帧图（仍在桥接链上）")
        print("    4. 改用关键帧图另起——放弃桥接链，用一张关键帧图作本段首帧（兜底）")
        print("    5. 接受并继续——明知不一致仍继续（会记进 gate-log.jsonl）")
        mapping = {
            "": GATE_STOP, "1": GATE_STOP, "2": GATE_RERUN, "3": GATE_MANUAL_FRAME,
            "4": GATE_KEYFRAME_RESTART, "5": GATE_ACCEPT,
        }
    if attempt >= 2 and not last_segment:
        print(f"  ⚠ 同一段已被拦下 {attempt} 次：反复对不上时考虑第 4 条另起"
              "（#10 裁定的兜底路径，不作常规走法）。")
    elif attempt >= 2:
        print(f"  ⚠ 同一段已被拦下 {attempt} 次：末段没有下游锚可换，"
              "这里只能整段重做或接受现状（换一段收尾属于整片级重做，不在本闸门范围内）。")
    print("!" * 60)
    while True:
        raw = _prompt("选择出路（回车=1 停下）：").strip()
        if raw in mapping:
            return mapping[raw]
        print("无效选项，重输。")


def _choose_ref_segment_outcome(attempt: int, *, position: int, total: int) -> str:
    """多参段判为「未达到」时的五档出路（issue #56 AC3）。默认**停下**，其余都是显式覆盖。

    与首尾帧链的 ``_choose_gate_outcome`` 差在「换锚帧」那两档：多参没有帧槽可换（锚定
    由本段用到的参考素材承担，ADR 0011），取而代之的是**改词再掷**（``GATE_REWORD``）
    ——多参段的偏差通常是「这段提示词没写对」，桥接链的偏差是「下游锚帧不对」。

    「继续」与「显式接受」都继续，但**记的不是同一件事**：前者是人改判「它其实达到了」
    （verdict=reached），后者是明知没达到仍继续（verdict=not_reached）。合并成一档，
    将来给自动判据取阈值时就分不开「判据误报」与「人故意放行」。
    """
    print("\n" + "!" * 60)
    print("[劝阻] 本段首/末帧画面没有达到上面声明的起止状态——默认**不继续**。")
    print("  多参段间没有桥接帧：这一段的偏差不会被下一段继承（锚定靠本段用到的参考素材），")
    print("  但拼接之后它就是成片里的一段，偏差留在成片里。")
    print("  出路：")
    for index, outcome in enumerate(_REF_OUTCOME_ORDER, 1):
        print(f"    {index}. {_REF_OUTCOME_LABELS[outcome]}")
    print("  注：「修改提示词」只动画面正文，时间轴注记与起止状态声明**锁死**——")
    print("      那两句写错属于「计划错了」，走「停下」重跑分段规划。")
    if attempt >= 2:
        print(f"  ⚠ 第 {position}/{total} 段已被拦下 {attempt} 次：反复对不上时考虑停下"
              "重跑分段规划，或显式接受这一段的现状。")
    print("!" * 60)
    mapping = {str(index): outcome for index, outcome in enumerate(_REF_OUTCOME_ORDER, 1)}
    mapping[""] = GATE_STOP  # 回车＝默认＝停下（不继续）
    while True:
        raw = _prompt("选择出路（回车=1 停下）：").strip()
        if raw in mapping:
            return mapping[raw]
        print("无效选项，重输。")


def _judge_bridge_semantics(*, directory: Path, anchor_segment: int, judged_video_segment: int,
                            total: int, reference: str | None, reference_label: str,
                            description: str | None, frame_path: Path | None,
                            attempt: int) -> str:
    """语义层主判定（#14）：把「该达到的末态」与「尾帧实际画面」**并排**摆给人判。

    这一层存在的理由就是「证据曾经从没在决策时刻被摆到一起」：``end_hook`` 只在展示
    提示词时打印（比那张帧存在早约十分钟），剥帧当轮只打印读图结果——人得在两个时刻
    各记一半，再凭记忆判。所以这里必须**并排**打，且打在人做决定的那一刻。

    **段号必须分开记**（``anchor_segment`` / ``judged_video_segment``）：两条分段路径
    调用本闸门的**时机相反**——v2 在刚跑完那段末尾（交上来的是本段视频），回退在下一段
    开头（交上来的是上一段视频）。用一个数字在两条路径上指不同的段，这份 log 就没法给
    自动判据（#15）当素材用了。

    判不合格 → 劝阻（默认不继续，可显式覆盖）。返回值是 #14 动作集之一。
    """
    print("\n" + "-" * 60)
    print(f"[锚帧语义核验] 第 {anchor_segment}/{total} 段的首帧锚"
          f"（＝第 {judged_video_segment} 段的尾帧）")
    print("-" * 60)
    print(f"  该达到的末态{reference_label}：")
    print(f"    {reference or '（本段路径没有留下可用的末态声明）'}")
    print("  尾帧实际画面（读图结果）：")
    print(f"    {description or '（读图失败：没有可对照的画面描述）'}")
    print(f"  尾帧图：{frame_path if frame_path else '（未取得）'}")
    print("-" * 60)

    reached = _confirm("尾帧画面达到上面那个末态了吗？", default=True)
    outcome = GATE_CONTINUE if reached else _choose_gate_outcome(attempt)
    _log_gate(directory, {
        # 这一帧**成了哪一段**的开场锚；以及被核的那段**输出视频**属于哪一段。
        # 两个都留：只留一个，两条路径的含义就对不上（见 docstring）。
        "anchor_segment": anchor_segment,
        "judged_video_segment": judged_video_segment,
        "reference_label": reference_label,
        "end_hook": reference,
        "bridge_frame_description": description,
        "bridge_frame_path": str(frame_path) if frame_path else None,
        "verdict": "reached" if reached else "not_reached",
        "action": outcome,
        "action_label": _OUTCOME_LABELS[outcome],
        "attempt": attempt,
    })
    return outcome


def _use_supplied_frame(position: int, generation_dir: Path, *, outcome: str) -> Path | None:
    """让用户直接给一张图当本段首帧锚（#14 的「手工换帧」/「改用关键帧图另起」）。

    两条出路落到同一机制（给一张现成的图），差别在**意图**：手工换帧仍留在桥接链上
    （只是换一张尾帧图），改用关键帧图另起是放弃这条链（#10 裁定的兜底 B）。所以动作名
    分两个取值记进 gate-log，将来校准判据时能分开看。落点仍用 ``bridge_frames/
    shot-NN-start.png``，与剥帧共用同一个规范位置。

    返回帧图路径；用户没给（回车）或复制失败返回 None，由调用方决定回到哪一步。
    """
    import shutil

    label = ("关键帧图路径（放弃桥接链，用这张图作本段首帧）"
             if outcome == GATE_KEYFRAME_RESTART else "尾帧图路径（你自己换的那一张）")
    raw = _clean_path_input(_prompt(f"{label}："))
    if not raw:
        return None
    source = Path(raw)
    if not source.is_file():
        print(f"[警告] 找不到这张图：{source}——没有换成，仍停在原地。")
        return None
    target = Path(generation_dir) / "bridge_frames" / f"shot-{position + 1:02d}-start.png"
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.copyfile(source, target)
    except OSError as exc:
        print(f"[警告] 复制帧图失败（{exc}）——没有换成，仍停在原地。")
        return None
    print(f"[锚帧] ✓ 已改用你给的这张图：{target}")
    return target


def _acquire_bridge_frame(state: dict, position: int, generation_dir: Path, *,
                          expected_seconds: float | None, ask: str,
                          segment_number: int, total: int,
                          anchor_segment: int, judged_video_segment: int,
                          reference: str | None = None,
                          reference_label: str = "（上一段 plan.end_hook）",
                          on_pass: Callable[[str], None] | None = None) -> bool:
    """拿到锚帧：索要输出视频 → 剥帧 → **可证层**判定 → 读图 → **语义层**人判。

    顺序按 #14 重排：**剥帧读图在前，「本段满意」的判断在后**——证据必须在决策时刻
    摆在人面前，而不是让人先去别处记一半、再回来凭记忆做决定。所以返回 True 的含义是
    「锚帧已就位**且**人看着并排证据判了继续」。

    ``position``：这一帧要当**哪一段**（0-based）的开场锚，决定帧图文件名与 state 段号。
    ``segment_number``：只用于**话术**（「第 N/M 段没有拿到输出视频」），两条路径各自
    取与自己的提问措辞一致的那个段号。
    ``anchor_segment`` / ``judged_video_segment``：写进 gate-log 的两个事实，由调用点
    各自声明——两条路径调用本闸门的时机相反，用一个数字当「段号」会指向不同的段。

    名字里的 acquire 是**拿到**，不是**验过**：人可以在判为「未达到」之后显式选
    「接受并继续」（GATE_ACCEPT），那时返回的 True 带着一个明知不合格的帧。名字从前叫
    ``..._until_clean``，那个词把这个覆盖行为藏起来了，反而更容易让人误判安全性。

    返回 True = 放行；False = 停下（默认出路，或用户交不出东西）。

    ``on_pass``：放行那一刻把**被放行的那段视频**交出去（issue #94 的段产出账）。
    放在这里而不是让调用方再问一遍路径：路径是用户在闸门内部交的，调用方手上没有；
    而「行在＝这段过了」必须写在**两道闸都放行之后**的同一个点位上。

    出路做在会话内（改交一段正确的视频即可重剥），不是"从头重跑"：这条出路过去其实
    不成立——阶段 2 结束就把会话标成 ``completed``，向导重跑会**另开一个新 GEN**、
    阶段 2 整条重做，``segments/progress.json`` 的续接在向导路径上不可达（已核实，见
    #17）。#17 落地后会话在陪跑期间是 ``segmented_running``，重跑向导会落回同一 GEN
    并从已完成段之后续接，这里返回 False 的代价才是「停在这一段」而不是「整条重来」。
    """
    attempt = 0
    while True:
        video_raw = _clean_path_input(_prompt(ask))
        if not video_raw:
            print(f"\n⚠ 第 {segment_number}/{total} 段没有拿到输出视频：交接校验是本流程的"
                  "必经环节，已停在这里——下一段不会被写，其提示词也不会去声明一张不存在的 "
                  "Picture 1。", flush=True)
            return False
        errors, description, frame = _capture_bridge_frame(
            video_raw, position, generation_dir, expected_seconds=expected_seconds)
        if errors:
            attempt += 1
            print("→ 请**重新剥帧**：改交本段正确的输出视频（时长需与本段计划一致，"
                  "非末段必须提供）；回车则放弃继续。", flush=True)
            if attempt >= 2:
                print(f"  ⚠ 第 {segment_number}/{total} 段已连续被拦下 {attempt} 次："
                      "若反复交不出合格的视频，可在下一步改用关键帧图另起（兜底路径）。",
                      flush=True)
            continue
        outcome = _judge_bridge_semantics(
            directory=generation_dir, anchor_segment=anchor_segment,
            judged_video_segment=judged_video_segment, total=total,
            reference=reference, reference_label=reference_label,
            description=description, frame_path=frame, attempt=attempt + 1)
        if outcome in (GATE_CONTINUE, GATE_ACCEPT):
            _anchor_state(state, position, description)
            if on_pass is not None:
                on_pass(video_raw)
            return True
        if outcome == GATE_RERUN:
            attempt += 1
            print(f"→ 重跑本段：回 ComfyUI 重做第 {segment_number}/{total} 段，"
                  "然后把**新的**输出视频路径交上来。", flush=True)
            continue
        if outcome in (GATE_MANUAL_FRAME, GATE_KEYFRAME_RESTART):
            supplied = _use_supplied_frame(position, generation_dir, outcome=outcome)
            if supplied is None:
                attempt += 1
                continue
            from ..tools.frame_auditor import describe_bridge_frame

            with _progress_scope("正在读取你给的这张图的画面……"):
                supplied_description = describe_bridge_frame(supplied)
            _anchor_state(state, position, supplied_description)
            # 「手工换帧 / 改用关键帧图另起」换的是**下一段的锚图**，本段输出视频照样
            # 通过了可证层，流程也照样越过这一段——所以它和「接受并继续」一样算通过。
            # 口径（用户 2026-10-27 裁定）：票面「判为换帧的轮次不落通过行」按
            # **「失败轮次不落行」**解释——被判「未达到」的那一轮遭遇不落行，最终被
            # 接受的换帧结果仍落行，且记的是**那段视频**（已过可证层、成片要用它），
            # 不是锚帧。GATE_RERUN / GATE_STOP / 被可证层拦下的轮次照旧不落行。
            # 不落行的后果是这段在账上永久缺席，而流程已经往下走（续跑从错位置起、
            # 账跳号、#96 以「缺第 N 段」拒绝出片）。
            if on_pass is not None:
                on_pass(video_raw)
            return True
        return False  # GATE_STOP


def _capture_bridge_frame(video_path: str, position: int, generation_dir: Path, *,
                          expected_seconds: float | None = None,
                          frame_suffix: str = "start"
                          ) -> tuple[list, str | None, Path | None]:
    """可证层交接校验 → 剥一段视频的尾帧 → 读图（issue #12 / #13 / #94）。

    ``expected_seconds``＝交出这段视频的那一段的**计划时长**，供可证层校验（交错段、
    用错时长档都会在这里被拦下）。

    ``frame_suffix``：段间桥接用 ``start``（``shot-NN-start.png``＝第 NN 段的开场锚）；
    末段收尾核验（#16）用 ``end``——它没有下一段，核的是**整条视频的最后画面**，
    放进 ``-start`` 会名不副实。

    **本函数只判与说，不写 state、不做流程决策**：语义层判定（#14）还在后面，判定之前
    就往 state 里写读图结果，一旦人判「重跑本段」，那段错锚就留在了 state 里
    （``_bridge_frame_descriptions`` 同段号取最后一个，会把它当成事实喂给写段 LLM）。

    返回 ``(可证层 error issue, 读图结果, 帧图路径)``。剥帧/读图任一步失败都只警告
    降级——写段请求退回仅按图片锚定（无读图记录），绝不伪造读图记录，也绝不中断人工
    陪跑流程。两条分段路径共用。
    """
    from ..tools.frame_auditor import describe_bridge_frame, extract_last_frame

    # 可证层**先**判：不合格的视频不必再花一次剥帧/读图调用，也绝不把错误的交接物喂进
    # state。顺序放在剥帧之前还有一处硬理由（issue #94）：「文件不在」曾是这条路径的
    # 降级放行口（剥帧失败只警告），而段产出账要给它算 SHA256——先判就把这条路堵在入口。
    errors = _gate_segment_handoff(video_path, expected_seconds)
    if errors:
        return errors, None, None
    try:
        frame = extract_last_frame(
            Path(video_path),
            Path(generation_dir) / "bridge_frames" / f"shot-{position + 1:02d}-{frame_suffix}.png",
        )
        size_kb = frame.stat().st_size / 1024
        print("[桥接帧] ✓ 已从上一段视频提取尾帧：")
        print(f"  位置：{frame}")
        print(f"  大小：{size_kb:.0f} KB")
        if frame_suffix == "start":
            print("  → 请在 H3 中把这张图设为本段的 first frame 输入。")
    except Exception as exc:  # noqa: BLE001 - 剥帧失败不阻塞人工流程
        print(f"[警告] 剥帧失败（{exc}）：本次没有取到帧，绝不伪造读图记录，"
              "按「量不出来」降级（桥接帧可手动截图代替；末段核验本次跳过）。")
        return [], None, None
    with _progress_scope("正在读取桥接帧实际画面……"):
        description = describe_bridge_frame(frame)
    if description:
        print(f"[桥接帧实际画面] {description}")
        print("  → 本段提示词将以这张图的实际画面为开场锚（图中没有的事物不得写成已存在）。")
    else:
        print("[提示] 本段提示词未携带桥接帧读图结果，开场一致性仅由图片本身锚定。")
    return [], description, frame


def _judge_last_frame(*, directory: Path, segment_number: int, total: int,
                      end_hook: str | None, promised: str, description: str | None,
                      frame_path: Path | None, attempt: int) -> str:
    """末段尾帧核验的判定块：**三方**并排（该段末态 / 承诺的尾帧画面 / 实际末帧画面）。

    复用 #14 的对照机制与 ``gate-log.jsonl`` 记录形状（多一个 ``promised_last_frame_description``
    字段把「承诺」与「末态声明」分开记）；差别在末段**没有下游锚可换**，故动作集只有
    停下／重跑末段／接受并继续。
    """
    print("\n" + "-" * 60)
    print(f"[末段尾帧核验] 第 {segment_number}/{total} 段的收尾（＝整条视频的最后画面）")
    print("-" * 60)
    print("  该段末态声明：")
    print(f"    {end_hook or '（本路径没有 plan 末态声明）'}")
    print("  用户提交的尾帧实际画面（**承诺的落点**）：")
    print(f"    {promised}")
    print("  末段实际末帧画面（读图结果）：")
    print(f"    {description or '（读图失败：没有可对照的画面描述）'}")
    print(f"  末帧图：{frame_path if frame_path else '（未取得）'}")
    print("-" * 60)

    reached = _confirm("整条视频的最后画面落在上面那张承诺的尾帧上了吗？", default=True)
    outcome = GATE_CONTINUE if reached else _choose_gate_outcome(attempt, last_segment=True)
    _log_gate(directory, {
        # 末帧核验核的是末段**自己**的收尾，不是下一段的开场锚 → 没有 anchor_segment。
        # 字段仍保留（值为 None）而不是省略：两份 log 的记录形状保持一致，#15 才好统一消费。
        "anchor_segment": None,
        "judged_video_segment": segment_number,
        "reference_label": "（末段收尾：承诺尾帧 vs 实际末帧）",
        "end_hook": end_hook,
        "promised_last_frame_description": promised,
        "bridge_frame_description": description,
        "bridge_frame_path": str(frame_path) if frame_path else None,
        "verdict": "reached" if reached else "not_reached",
        "action": outcome,
        "action_label": _OUTCOME_LABELS[outcome],
        "attempt": attempt,
    })
    return outcome


def _acquire_last_video(*, segment_number: int, total: int,
                        expected_seconds: float | None,
                        on_pass: Callable[[str], None] | None = None) -> bool:
    """无尾帧变体的末段收尾：**只**做可证层，不剥帧、不读图（issue #94）。

    为什么无尾帧也要索要视频：成片由各段视频接成，末段没有视频就拼不出来（#96）。
    但**没有可核的承诺**时不许凭空报「画面未达到」——I2VA / T2VA 没有承诺尾帧，
    凭空报一句「未达到」就是误报（#16 的裁定仍然成立）。所以这里只判阶段之外
    可证的事实：时长档与尺寸（``_gate_segment_handoff``）。
    """
    attempt = 0
    while True:
        video_raw = _clean_path_input(
            _prompt("末段输出视频路径（必填，用于拼成片）："))
        if not video_raw:
            print(f"\n⚠ 第 {segment_number}/{total} 段没有拿到末段输出视频：没有它拼不出"
                  "成片，已停在这里。", flush=True)
            return False
        errors = _gate_segment_handoff(video_raw, expected_seconds)
        if errors:
            attempt += 1
            print("→ 请**重新交回**末段正确的输出视频（时长需与本段计划一致）；"
                  "回车则放弃继续。", flush=True)
            if attempt >= 2:
                print(f"  ⚠ 第 {segment_number}/{total} 段已连续被拦下 {attempt} 次："
                      "交上来的是不是**本段**的输出视频？", flush=True)
            continue
        if on_pass is not None:
            on_pass(video_raw)
        return True


def _verify_last_frame(state: dict, position: int, generation_dir: Path, *,
                       end_hook: str | None, segment_number: int, total: int,
                       expected_seconds: float | None,
                       on_pass: Callable[[str], None] | None = None) -> bool:
    """末段收尾（issue #16/#94）：链的**另一端**，并且**一律索要末段输出视频**。

    链式闸门只管**段间**（末段不剥帧：``if position + 1 < total``）。而 FL2VA/L2VA 下
    末段必须收在用户提交的那张尾帧上——此前只有提示词里的一句**声明**
    （``_frame_anchor_note``），没有任何校验。整条视频的最后画面是否真的落在承诺的
    那张尾帧上，是 FL2VA/L2VA 的核心承诺，不能只靠人自己盯。

    **无论有没有尾帧承诺，都必须拿到末段输出视频**（issue #94）：成片由各段视频接成，
    末段缺席就拼不出成片（#96）。两份行为的差别只在语义层：
    - 有承诺（FL2VA/L2VA）→ 剥末帧读图，三方并排人判（#16 原样）；
    - 无承诺（I2VA/T2VA）→ **只**做可证层，不剥帧不读图，也不写 gate-log：没有可核
      的承诺，凭空报「未达到」就是误报。

    ``on_pass``：通过那一刻交出末段视频路径（段产出账，issue #94）——与段间闸门
    同一个「两道闸都放行之后」的点位。

    返回 True = 放行（含「量不出来」的降级）；False = 停下。
    """
    promised = _promised_last_frame_description(state)
    if promised is None:
        print(f"\n[末段产出] 本会话没有承诺尾帧：末段无画面可核，但**末段输出视频**仍要"
              f"交回——它是成片 final.mp4 的最后一段，没有它拼不出成片。"
              f"本次只做可证层（时长/尺寸）校验。")
        return _acquire_last_video(
            segment_number=segment_number, total=total,
            expected_seconds=expected_seconds, on_pass=on_pass)
    print(f"\n[末段尾帧核验] 本会话提交了尾帧：FL2VA/L2VA 下整条视频必须收在它上面，"
          f"现在核第 {segment_number}/{total} 段的末帧。")
    attempt = 0
    while True:
        video_raw = _clean_path_input(_prompt("末段输出视频路径（必填，用于核末帧）："))
        if not video_raw:
            print(f"\n⚠ 第 {segment_number}/{total} 段没有拿到输出视频：末帧核验是 "
                  "FL2VA/L2VA 的核心承诺，已停在这里。", flush=True)
            return False
        errors, description, frame = _capture_bridge_frame(
            video_raw, position, generation_dir, expected_seconds=expected_seconds,
            frame_suffix="end")
        if errors:
            attempt += 1
            print("→ 请**重新剥帧**：改交末段正确的输出视频（时长需与本段计划一致）；"
                  "回车则放弃继续。", flush=True)
            continue
        outcome = _judge_last_frame(
            directory=generation_dir, segment_number=segment_number, total=total,
            end_hook=end_hook, promised=promised, description=description,
            frame_path=frame, attempt=attempt + 1)
        if outcome in (GATE_CONTINUE, GATE_ACCEPT):
            if on_pass is not None:
                on_pass(video_raw)
            return True
        if outcome == GATE_RERUN:
            attempt += 1
            print(f"→ 重跑末段：回 ComfyUI 重做第 {segment_number}/{total} 段，"
                  "然后把**新的**输出视频路径交上来。", flush=True)
            continue
        return False  # GATE_STOP


def _read_cached_summary(directory: Path):
    """只读已落盘的中文摘要；续接不该为展示再烧一次 LLM（也不该依赖模型可达）。"""
    import json

    from ..summary import PromptSummary

    json_path = directory / "summary-zh.json"
    if not json_path.is_file():
        return None
    try:
        payload = json.loads(json_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict) or not payload.get("overall"):
        return None
    shots = payload.get("shots")
    return PromptSummary(overall=str(payload["overall"]),
                         shots=shots if isinstance(shots, list) else [])


# 续拼短路（issue #97）的三种判读，见 ``_film_only_resume``。
RESUME_ASSEMBLE = "assemble"        # 全段已过 + 段表在 → 直接拼接
RESUME_NO_TABLE = "no-table"        # 账满 1..N、但段表缺席 → 停住（别重跑规划）
RESUME_KEEP_GOING = "keep-going"    # 还没到「只差成片」→ 走常规续跑


def _film_only_resume(directory: Path) -> str:
    """「只差成片」了吗？该直接拼、还是停住？（issue #97 续拼短路的判据）

    为什么要在跑任何流程**之前**问这一句：拼接失败之后会话停在 ``segmented_running``
    （#96 的失败语义），用户重进向导就落回这条续接路径。若先跑 ``_run_segmented_flow``，
    它会在 ``cached and done`` 分支里**先构造模型**——哪怕一段都不用写——模型构造失败
    就直接停在拼接口之前，用户已经付过的生成与验收成本永远换不回成片；FL2VA/L2VA 下
    它还会无条件走末段核验，把末段视频**再要一遍**（勘察 §F-6）。段已全过时这些都该
    整段跳过。

    为什么不干脆把「账满但没有段表」也交给下游：``_cached_plans`` 返回 None 时那条路径
    会**先重跑一次 ``plan_segments``**（LLM 调用），之后才因为「账已满 + 这是另一套分段」
    停下——一次纯浪费的付费调用，而「这条片子拼不了」盘上早就看得出来。机械回退路径的
    段表只在全部段通过之后补写（``_write_mechanical_plan``），段提示词没有可用时间戳时
    它压根不写，所以这条现场真实存在（#96 交接）。

    三种判读：
    - ``RESUME_ASSEMBLE``：段表读得回来 + 账上每段都有通过行 + 账与段表对得上；
    - ``RESUME_NO_TABLE``：账是满的（1..N 连续、非空）却**没有** ``segments/plan.json``；
    - ``RESUME_KEEP_GOING``：其余（还没跑完，或读不回来）。

    **读不回来一律走常规续跑、不在这里报错**——那条既有路径
    （``_run_segmented_flow`` → ``_segment_progress`` / ``validate_shot_progress``）会用
    同一套口径把病因打出来并停住；在这里再打一份就是同一句话两处口径（#56/#57 的教训）。
    那也**不是**「从第 1 段重来」：账读不回来时那条路径同样会停住，不会重做任何段。

    旧会话（没有新账、只有 ``segments/progress.json``）**不算**只差成片：它从没记过段
    视频路径，拼接本来就不可用（#96 已当面说破），走既有路径标完成即可。

    已知边界（没有段表就看不出总段数）：「账满 1..N」既可能是全过，也可能是一份**半途**
    的机械回退账——两种都判 ``RESUME_NO_TABLE``。后者的出路与今天一样（补段表，或删账
    从第 1 段重做），少的只是一次白跑的规划调用。
    """
    from ..shot_ledger import passed_shot_rows, validate_shot_progress

    try:
        passed = passed_shot_rows(directory)
    except (OSError, ValueError):
        return RESUME_KEEP_GOING
    # 只有 1..N 连续（非空）才谈得上「账满」；跳号/越界交给下游按跳号口径报出来。
    if sorted(passed) != list(range(1, len(passed) + 1)):
        return RESUME_KEEP_GOING
    try:
        plans = _cached_plans(directory)
    except ValueError:
        return RESUME_KEEP_GOING
    if plans is None:
        return RESUME_NO_TABLE if passed else RESUME_KEEP_GOING
    try:
        validate_shot_progress(passed, total_segments=len(plans))
    except ValueError:
        return RESUME_KEEP_GOING
    return RESUME_ASSEMBLE if len(passed) == len(plans) else RESUME_KEEP_GOING


def _run_long_form(brief: Brief, session: SessionState, prompt: str, summary, state: dict,
                   directory: Path) -> dict[int, dict]:
    """长视频分段陪跑：跑之前标「分段进行中」，**全部段确认完成**才转「已完成」。

    这两次状态写入就是 #17 的全部要点。标成未完成，中断（闸门硬阻断 / Ctrl-C / 重启）
    之后重跑向导会落回**同一个 GEN**，并由段产出账从第 N+1 段续接；半途而废**不得**
    被标成完成——那会让下次重跑另开新 GEN、阶段 2 重做（实测可超 10 分钟）＋ 已完成
    的所有段重来，账永远读不到。

    ``state`` 必须是**阶段 2 之后的**那份：它带着 ``fl2va_frame_descriptions``（帧图
    读图结果），拿旧 session 对象落盘会把它丢掉，分段提示词就退回照分镜表写
    （2026-09-22 首帧不锚定缺陷）。

    **返回值＝全部通过行**（``{段号: 账行}``，段号从 1 起连续），供收尾处的成片拼接
    （#96）直接取用；未跑完返回空表。旧会话（无新账）也返回空表——那正是「拼接不可用」
    的形态，与账读口同一份语义（见 ``_segment_progress``）。

    **续拼短路**（issue #97）：段已全过、只差成片时**整段跳过** ``_run_segmented_flow``，
    直接进拼接；账满而段表缺席时同样不进它，就地停住（那条路径会先重跑一次分段规划）。
    理由见 ``_film_only_resume``：那条路径会先构造模型、再走末段核验，两条都与「只差
    成片」无关，却都能把用户挡在拼接口之前。

    **收尾处自动接出成片**（#96）：全段通过之后、标完成之前，按 ``segments/plan.json``
    的段序与段产出账的通过行拼出 ``<生成目录>/final.mp4``。拼接判据（缺段／SHA256／
    段表覆盖）住在共用件 ``final_film.assemble_film`` 里——「未通过即不拼」必须住在被
    拼接的那一处，不能退化成调用方的 if（本仓「规则在、接线不在」已犯过多次）。
    拼不出来（含找不到段表）**不标完成**：会话停在 ``segmented_running``，续拼由 #97
    接着做；标早了下次重跑会另开新 GEN，已通过的段全白做（同 #17 的代价）。
    """
    save_session(directory, brief, state, status=STATUS_SEGMENTED_RUNNING)

    verdict = _film_only_resume(directory)
    if verdict == RESUME_NO_TABLE:
        # 第一句与 #96 收尾处**同一句**（不另造一套）：段表缺席＝段序无从确定。
        # 放在流程之前说，是因为下游那条路会先花一次 LLM 重跑规划才得出同一个结论。
        print("\n[成片] 成片未产出：找不到分段规划 segments/plan.json——段序无从确定。")
        # 第二句不能沿用 #96 的「从拼接这一步继续」：这里也可能是一份**半途**的机械
        # 回退账（没有段表就看不出总段数，见 ``_film_only_resume`` 的已知边界），那种
        # 情况下恢复段表后重进是接着往下走、不是从拼接继续。说「接着往下走」两种都真。
        print("[续接] 会话保持「分段进行中」：恢复段表后重进向导会接着往下走"
              "（已通过的段不重跑）。")
        return {}
    if verdict == RESUME_ASSEMBLE:
        # 续拼（#97）：不再问段视频、不重剥帧、不重跑闸门，也不再构造模型。
        # 这里**必须**早于 ``_run_segmented_flow``：它内部的两处 LLM 相关步骤（构造模型、
        # 末段核验）都与「只差成片」无关，却任一失败都能把用户永久挡在拼接口之前。
        print("\n[续拼] 全部段已通过验收，只差成片：直接从拼接这一步继续"
              "（不再要段视频、不重剥帧、不重跑闸门）。")
        return _assemble_film_and_complete(directory, brief, state)

    finished = _run_segmented_flow(brief, session, prompt, summary=summary, state=state)
    if not finished:
        print(f"\n[续接] 本次没有跑完全部段，会话保持「分段进行中」：下次重跑向导会落回 "
              f"{directory.name}，从已完成的那一段之后继续（阶段 1/2 不重跑）。")
        return {}
    return _assemble_film_and_complete(directory, brief, state)


def _assemble_film_and_complete(directory: Path, brief: Brief,
                                state: dict) -> dict[int, dict]:
    """收尾：把已通过的段接成 ``<生成目录>/final.mp4``，**成功才**标完成（issue #96）。

    首跑与续拼（#97）共用这一份：两条路只是「怎么走到这里」不同，走到之后要做的事
    一模一样，各写一遍迟早出现一份改了另一份没改。

    返回全部通过行（成功）或空表（拼不出来／账读不回来）。**拼不出来不标完成**：会话
    停在 ``segmented_running``，下次重进向导还能续拼；标早了下次重跑会另开新 GEN，
    已通过的段全白做（同 #17 的代价）。

    幂等（issue #97）：账上全过而 ``final.mp4`` 已存在时（写完成片、还没标完成就崩了，
    或用户重复进入）照样重跑一次拼接——同名的旧成片会被覆盖，换来的是「重进就收敛到
    完成」，而不是一条永远停在 running 的会话。被覆盖的那份是**这套段**接出来的，
    重接不会换来一份不同的片子（段视频的身份由账上的 SHA256 钉住，见共用件）。
    """
    from ..final_film import ConcatStrategy, assemble_film, format_final_film
    from ..shot_ledger import (
        legacy_progress_path,
        passed_shot_rows,
        shot_ledger_path,
        validate_shot_progress,
    )

    try:
        # 收尾点（#96 的拼接落点）：账上的全部通过行在这里取得。读不回来就**不标完成**
        # ——账是续跑的进度来源，标了完成会让下次重跑另开新 GEN（同 #17 的代价）。
        passed = passed_shot_rows(directory)
    except (OSError, ValueError) as exc:
        print(f"[错误] 段产出账读不回来（{exc}）：不把会话标成完成——账是续跑的进度来源。")
        return {}

    # 旧会话没有新账（只有旧的 progress.json）：它从没记过段视频路径与 SHA256，
    # 拼接所需的输入根本不存在。这不是失败——旧会话的既定边界，照旧标完成。
    legacy = (legacy_progress_path(directory).is_file()
              and not shot_ledger_path(directory).is_file())
    if legacy:
        print("\n[成片] 旧会话没记段视频，拼接不可用。")
    else:
        try:
            plans = _cached_plans(directory)
        except ValueError as exc:
            print(f"\n[成片] 成片未产出：分段规划读不回来（{exc}）")
            print("[续接] 会话保持「分段进行中」：修好 segments/plan.json 后重进向导"
                  "会从拼接这一步继续（已通过的段不重跑）。")
            return {}
        if plans is None:
            # 段序真源缺席：机械回退路径在收尾补写 plan.json（见 _write_mechanical_plan），
            # 规划式在开跑时就写了。两处都没有＝落盘物不一致，不猜段序。
            print("\n[成片] 成片未产出：找不到分段规划 segments/plan.json——段序无从确定。")
            print("[续接] 会话保持「分段进行中」：恢复段表后重进向导会从拼接这一步继续"
                  "（已通过的段不重跑）。")
            return {}
        try:
            # 账与段表必须对得上（越界/跳号都算）：账上的段号只对产出它的那套分段有意义，
            # 拿一份对不上的账去拼接，等于把另一次拆段的产物接进这条片子（全程不报错）。
            validate_shot_progress(passed, total_segments=len(plans))
        except ValueError as exc:
            print(f"\n[成片] 成片未产出：{exc}")
            print("[续接] 会话保持「分段进行中」：修好段产出账后重进向导会从拼接这一步继续。")
            return {}
        film = assemble_film(
            plans=plans, rows=passed, total_s=int(brief.duration),
            # 成片落点与多参同名（成片是同一件产物）；「放哪」是路线的事，由这一层定
            # （见 ``ref_segments.FINAL_FILM_FILENAME`` 的同一口径）。
            out_path=directory / "final.mp4",
            # 本票不裁接缝首帧（#98 才把这一档换成 TRIM_FIRST_FRAME）：先用多参同款
            # 「优先 stream-copy」，画质档按 spec #92 取 CRF 16。
            strategy=ConcatStrategy.STREAM_COPY_FIRST,
            reencode_crf=16,
            # 出路必须是**本路**的：首尾帧链没有换段拼接命令，替它讲一句
            # ``ref-concat --replace`` 就是误导（spec #92 明令不提供该命令）。
            remedy_hint="重跑本段并重新验收通过后，成片会自动取用最新的那份视频。",
        )
        if film.path is None:
            print(f"\n[成片] 成片未产出：{film.reason}")
            print("[续接] 会话保持「分段进行中」：重进向导会从拼接这一步继续"
                  "（已通过的段不重跑，也不必重新交回视频）。")
            return {}
        print(format_final_film(film))
        # 段数当面给出（落点已由 format_final_film 给出）：它是「这条片子由几段接成」的
        # 直接答案，也是与段表现场对账时最短的一句。
        print(f"  段数：{len(plans)} 段")

    save_session(directory, brief, state, status=STATUS_COMPLETED)
    return passed


def _resume_segmented(config: Config, session: SessionState) -> int:
    """分段陪跑续接：不碰阶段 1/2，用已落盘的整条提示词与帧锚直接回到陪跑。

    阶段 2 的产物（``video-prompt.md``、``summary-zh.json``、带帧图读图结果的
    ``stage_state``）都在会话目录里，所以这里零 LLM 调用就能续接。

    「零 LLM」不是本函数保证的，而是 ``_run_long_form`` 的续拼短路保证的（issue #97）：
    段已全过、只差成片时它整段跳过陪跑，连模型都不构造——否则一个与成片无关的失败
    （缺 API key）就能让用户永远拿不到那份已经该有的成片。
    """
    directory = session.directory
    prompt_file = directory / "video-prompt.md"
    if prompt_file.is_file():
        prompt = prompt_file.read_text(encoding="utf-8")
    else:
        # 走 v2 时 prompt 用不上（写段只用 plan + state）；纯回退路径才需要它，
        # 那种情况下重跑一次阶段 2 是唯一的出路——把话说清楚，不静默降级。
        prompt = ""
        print(f"[警告] 找不到 {prompt_file}：若本次只能走机械拆分的回退路径，"
              f"将无法续接（v2 规划路径不受影响）。")
    print(f"\n[续接] 上一次分段陪跑没有跑完，回到 {directory.name} 继续（阶段 1/2 不重跑）。")
    _run_long_form(session.brief, session, prompt, _read_cached_summary(directory),
                   session.stage_state, directory)
    return 0


def _ref_delivery_started(directory: Path) -> bool:
    """本片是否**已经交付过**（段表/段文件/段产出账三样里只要有一件在，就是）。

    用来把两种「读不回来」分开（issue #58 复审实测）：交付那一步就中断的窗口里**确实**
    一段都没做，说「这不是一段都没做」是撒谎；已经交付过而落盘物丢了，才是进度有风险。
    """
    from ..ref_segments import segment_ledger_path

    segments = directory / "segments"
    return ((segments / "plan.json").is_file()
            or segment_ledger_path(directory).is_file()
            or any(segments.glob("segment-*.md")))


def _resume_ref_handoff(config: Config, session: SessionState) -> int:
    """多参逐段陪跑续接（issue #58）：回到同一个 GEN，已通过的段不重出、不重跑、不重付。

    与 ``_resume_segmented`` 是**两条路线的续接**，不共用：base 那边靠
    ``segments/progress.json`` 的 ``done=N`` 与 ``shot-NN.md``、接着还要剥桥接帧；
    多参这边靠**段产出账的通过行**与 ``segment-NN.md``、没有桥接帧。拿 base 的
    ``_run_long_form`` 走多参会话，等于拿另一条路线的步骤去续它。

    零重复付费：段表从盘上读回、已落盘的段词直接复用（``load_ref_segments_for_resume``，
    按需写段下**还没轮到的段本来就没写过**，由陪跑轮到它时写一次），阶段 1/2 一步不动。
    读不回来就**响错停住**（AC3 裁定）：段表缺失/读不出、已通过段的段文件缺失、账行错形、
    账与段表对不上——这些形态下「从零开始」等于把已通过的段当没做过。
    """
    from ..generation import ref_asset_registry
    from ..model_factory import build_chat_model
    from ..ref_segments import (
        load_ref_segments_for_resume,
        read_ref_handoff_progress,
    )

    directory = session.directory
    brief = session.brief
    try:
        # 未轮到的段要在陪跑里现场写，所以这里备一个模型：构造失败降级机械拼装（不静默）。
        try:
            llm = build_chat_model()
        except Exception as exc:  # noqa: BLE001 - 降级机械拼装，不静默失败
            llm = None
            print(f"[提示] 模型构造失败（{exc}）：续跑时还没写过的段改走机械拼装。")
        registry = ref_asset_registry(session.stage_state, brief)
        job = load_ref_segments_for_resume(
            directory, total_s=brief.duration, llm=llm,
            ref_meta=[(r.picture, r.name, r.description) for r in registry])
        passed = read_ref_handoff_progress(directory, total_segments=len(job.plans))
        # 已通过的段必须有段文件：账说「这段过了」，词却不见了＝落盘物互相矛盾。
        # 未轮到的段缺文件是**正常形态**（按需写段：没轮到就不写），不在此列。
        for position in sorted(passed):
            path = job.segment_path(position)
            if not path.is_file():
                raise FileNotFoundError(
                    f"第 {position} 段的提示词文件不见了：{path}"
                    "（这一段已在段产出账里记为通过，续跑不重出已交付的段——"
                    "重出要重付 LLM，且新词会与已通过的那几段脱节）")
    except (OSError, ValueError) as exc:
        print(f"[错误] 多参逐段陪跑的落盘物读不回来：{exc}")
        if _ref_delivery_started(directory):
            print("  这不是「一段都没做」——静默从零开始会把已通过的段当没做过"
                  "（已生成的视频得重新交回，已付的读图也白付）。")
            print(f"  出路：恢复 {directory / 'segments'} 下的 plan.json / segment-NN.md / "
                  "segment-ledger.jsonl 后重跑；或删掉会话文件另开一次生成。")
        else:
            # 交付那一步就中断了（会话已标「陪跑进行中」，但段表/段文件/账都还没落）。
            # 这时**确实**一段都没做，说「这不是一段都没做」是撒谎——这个窗口是复审
            # 实测指出的（issue #58）：状态写在交付之前，而交付要跑 LLM，几分钟的窗口。
            print("  这一段**还没开始做**：段表、段文件、段产出账三样都还没有，"
                  "所以没有任何进度会丢。")
            print("  出路：删掉本会话文件（session-state.json）后重跑——阶段 1/2 会重做；"
                  "或把上一次交付的 segments/ 恢复回来再续。")
        return 1
    print(f"\n[续接] 上一次多参逐段陪跑没有跑完，回到 {directory.name} 继续"
          f"（阶段 1/2 不重跑）：已通过 {len(passed)}/{len(job.plans)} 段。")
    finished = _run_ref_segment_handoff(job, directory, passed=passed)
    # 收尾（拼接 → 账本路径 → 转 completed）与首跑**同一份**：见 ``_finish_ref_handoff``。
    _finish_ref_handoff(job, directory, brief, session.stage_state, finished)
    return 0


def _show_revisions_vs_frame(state: dict) -> None:
    """写段前把「累积用户修订」与「关键帧读图结果」**并排**摆一次（issue #26）。

    为什么并排摆而不是让机器判：图与修订是否矛盾要**语义对比**才判得出，不可证，项目
    明令不做这类自动判定（设计稿 §6、ADR 0005）。而「提了修订却没重出图」这种冲突在
    分段阶段真实存在——不摆出来，用户会以为修订已经落到图上，等到成片才发现。

    摆的就是**写段请求里那块**（``render_revision_context`` 的原样输出，含优先序声明）：
    用户看到的与模型看到的必须是同一份，否则「它为什么没听我的修订」就无从对账。
    没有修订时整块不打：没有可冲突的对象，空块只是噪声。
    """
    from ..segment_prompts import frame_anchor_context, render_revision_context

    revisions = render_revision_context(state)
    if not revisions:
        return
    frames = frame_anchor_context(state) or "（没有关键帧读图结果：本片无关键帧，或读图未完成）"
    print("\n" + "=" * 60)
    print("[写段前核对] 下面两块并列摆出。系统**不判**它们是否矛盾（语义对比不可证）——")
    print("             冲突请你现在发现并提出。")
    print("-" * 60)
    print(revisions)
    print("-" * 60)
    print("[关键帧读图结果]（用户提交的图，**唯一事实源**）")
    print(frames)
    print("注：第 2 段起锚定的是上一段剥出的**桥接帧**，此刻尚未产生，故不在此并列；")
    print("    它们在每段剥帧后立刻读图，只进那一段自己的请求。")
    print("=" * 60)


class _SegmentProgress(NamedTuple):
    """续跑起点与已通过段（issue #94）。

    ``start``：本次从第 ``start + 1`` 段（1-based）开始。
    ``passed``：新账（``segments/shot-ledger.jsonl``）的通过行；旧会话为空表。
    ``legacy``：旧会话回退读 ``segments/progress.json``——真时不写新账、继续写旧进度
    （理由见 ``_segment_progress``）。
    """

    start: int
    passed: dict[int, dict]
    legacy: bool


def _segment_progress(directory: Path) -> _SegmentProgress:
    """读「第几段过了」：**新账是唯一真源**，旧会话回退读旧进度并当面说破。

    判定顺序（票面「旧会话判定」）：新账在 → 只认它；新账不在 + ``progress.json`` 在
    → 旧会话；两者都不在 → 新会话、从第 1 段起。

    旧会话**不回溯补账**（票面 Out of Scope：段视频多已不在），所以它继续用旧进度
    续跑、继续写旧进度文件。不写新账还有一条硬理由：从 ``done=N`` 继跑时前 N 段本来
    就没有行，新账会立刻出现**跳号**，下一次续跑就会响错停住——等于亲手把会话弄坏。
    """
    from ..shot_ledger import legacy_progress_path, passed_shot_rows, shot_ledger_path

    if shot_ledger_path(directory).is_file():
        passed = passed_shot_rows(directory)
        return _SegmentProgress(start=len(passed), passed=passed, legacy=False)
    if not legacy_progress_path(directory).is_file():
        return _SegmentProgress(start=0, passed={}, legacy=False)
    done = _segments_done(directory)
    if done:
        print("[提示] 旧会话：沿用 segments/progress.json 的进度续跑"
              f"（已完成 {done} 段）。但旧进度只记了段数、没记各段视频路径——"
              "**拼接不可用**：成片需要每段的视频路径与 SHA256。")
    return _SegmentProgress(start=done, passed={}, legacy=True)


def _write_mechanical_plan(directory: Path, segments: list) -> bool:
    """把机械拆分的段表补写成 ``segments/plan.json``（issue #96）。

    回退路径没有 plan 层，而收尾的成片拼接只认 ``plan.json`` 当段序真源（与多参同一
    份口径：不按文件名、不按时间排）。**只在全部段通过之后写**：中途写会让中断后的续跑
    在 ``_run_segmented_flow`` 的 ``cached and done`` 分支误走规划式重写——机械段边界与
    规划器不是一套，按它续写等于换了一套段（issue #17 的原形态）。

    机械拆分没有 plan 层的概述与末态声明：``summary``/``end_hook`` 留空，不编一句
    （编出来的话会被后续读者当成「规划器确实写过这句话」）。

    返回 False＝段提示词里没有可用的起止时间戳：没有时间窗就核不出「段表覆盖
    ``[0, 起步总时长]``」，不写一份对不上总时长的段表，由调用方说破拼接不可用。
    """
    import json

    from ..segment_planner import SegmentPlan

    plans: list[SegmentPlan] = []
    for position, segment in enumerate(segments, 1):
        start, duration = segment.start_seconds, segment.duration_seconds
        if start is None or duration is None:
            return False
        plans.append(SegmentPlan(
            index=position - 1,
            # 段边界按契约是整数秒（``_shot_blocks`` 已把时间戳取整），这里只是落类型。
            start_s=int(round(start)),
            end_s=int(round(start + duration)),
            shots_in_segment=(segment.shot_number,),
            summary="",
            end_hook="",
        ))
    seg_dir = Path(directory) / "segments"
    seg_dir.mkdir(parents=True, exist_ok=True)
    (seg_dir / "plan.json").write_text(
        json.dumps([plan.to_dict() for plan in plans], ensure_ascii=False, indent=2),
        encoding="utf-8")
    return True


def _run_segmented_flow(brief: Brief, session: SessionState, prompt: str, summary=None,
                        state: dict | None = None) -> bool:
    """把整条提示词按 [Shot N] 拆成单镜头提示词，逐段陪跑。返回**是否跑完全部段**。

    state：阶段 2 之后的最终 state（含 ``fl2va_frame_descriptions`` 帧图读图结果）；
    省略时退回 ``session.stage_state``（续跑/测试兼容）。

    循环规则：先尽量用 LLM 把本段 soundscape/music 按时间窗重写成独立提示词（
    失败自动回退机械拆分并明示）；显示信息卡（段号/Shot/时长/文件路径）→ 人工复制
    到 H3 生成 → 回车确认 → 才显示下一段；第 2 段起可提交上一段视频路径，自动剥尾帧
    作为本段首帧图；中断后由段产出账续接（旧会话回退读 progress.json）。

    返回值是给调用方判「该不该把会话标成 completed」用的（issue #17）：闸门硬阻断、
    Ctrl-C 都走不到正常结尾，那种情况必须保持「分段进行中」，否则下次重跑另开新 GEN。
    """
    import json

    from ..model_factory import build_chat_model
    from ..segment_planner import plan_segments
    from ..segment_prompts import (
        frame_anchor_context,
        render_revision_context,
        rewrite_segment_prompt,
        split_shots_from_prompt,
        write_segment_v2,
    )

    # ① 先走新流程：segment_planner 规划整秒分段边界 → 每段独立细写（官方英文格式，spec 2026-09-22）
    state = state if state is not None else session.stage_state
    # 写段前核对（issue #26）：两条产出路径都在本函数内分叉，故摆块也只需这一处——
    # 摆在分叉之前，v2 与回退路径都只看到一次。
    _show_revisions_vs_frame(state)
    try:
        progress = _segment_progress(session.directory)
    except (OSError, ValueError) as exc:
        # 账行错形/读不出必须**响错停住**（issue #94 AC6）：静默从第 1 段重来会把
        # 已通过的段当没做过，也没人会知道账上有过哪几段。
        print(f"[错误] 段产出账读不回来：{exc}")
        print("  这不是「一段都没做」——静默从第 1 段重来会把已通过的段当没做过"
              "（已生成的视频要重新交回）。")
        print(f"  出路：恢复 {session.directory / 'segments' / 'shot-ledger.jsonl'} 后重跑；"
              "或删掉它让它从第 1 段重做（已通过的段视频不会被认回）。")
        return False
    done = progress.start
    try:
        cached = _cached_plans(session.directory)
    except ValueError as exc:
        # 计划在、但读不回来，而进度说已经做过段：两个落盘物**对不上**。
        # 静默重规划是最坏的选择——新分段边界 + 旧 done 会跳过从没写过的段，且全程不报错
        # （正是紧邻注释要防的 bug）。停在这里，把选择权交回人。
        print(f"[错误] {exc}")
        if done:
            print(f"  而 progress.json 说已完成 {done} 段——两者对不上，不能自动继续。")
        print("  出路：恢复 segments/plan.json；或删掉 segments/progress.json 让它从第 1 段重做"
              "（分段的 shot-NN.md 会按新分段逐段重写，不会被旧产物顶替）。")
        return False
    if cached and done:
        # 续接：必须用**产出 progress.json 的那套分段**。重跑 plan_segments 是 LLM 调用，
        # 分段边界会变，``done=N`` 就会指向另一套分段（issue #17）。
        try:
            llm = build_chat_model()
        except Exception as exc:  # noqa: BLE001 - 与下面兄弟分支同一套降级口径
            print(f"[错误] 续接时构造模型失败（{exc}）：分段提示词写不出来，停在这里。"
                  "计划与进度都已落盘，重跑向导会回到同一条续接路径（不必重做任何段）。")
            return False
        print(f"[续接] 复用已落盘的分段规划（{len(cached)} 段），不重跑规划。")
        return _run_segmented_flow_v2(session, cached, state, llm, summary=summary,
                                      plans_from_cache=True, progress=progress)

    plans = None
    try:
        llm = build_chat_model()
        if state.get("shot_table"):
            # 帧图实际画面一并交给规划层：分镜表只是计划，用户可能复用/改了图（冲突时以图为准）
            # 步骤横幅＋等待心跳（issue #67 / ADR 0019）：拆段走直调看门狗后，卡住/
            # 重试/生成中都是事件，必须有订阅者才会变成终端上的行；横幅与订阅成对
            # 出现（step_scope），不留下「打了横幅却听不到心跳」的半套接线。
            with step_scope("正在分段规划"):
                plans = plan_segments(
                    str(state.get("shot_table", "")), brief.duration, llm,
                    frame_context=frame_anchor_context(state),
                    # 修订经**显式入参**交给规划层：它不依赖 state（issue #26 AC-4）
                    revision_context=render_revision_context(state),
                )
            # 规划是长阻塞：用户此时误敲的键会滞留在行缓冲里，被下一个 input() 吞掉
            # （「跳问」）。这一句原先挂在 `_progress_scope` 的退出路径上，换成
            # step_scope 后必须在这里补回来，否则这一步之后的所有问答都会受影响。
            _drain_stdin()
    except Exception as exc:  # noqa: BLE001 - 规划失败回退旧路径
        print(f"[提示] 分段规划失败（{exc}），回退到按 [Shot N] 机械拆分。")

    if plans:
        print(f"[分段规划] 规划了 {len(plans)} 段：" + ", ".join(f"{p.start_s}-{p.end_s}s" for p in plans))
        return _run_segmented_flow_v2(session, plans, state, llm, summary=summary)

    # ② 回退：依旧按 [Shot N] 拆分
    segments = split_shots_from_prompt(prompt, total_duration=brief.duration)
    if len(segments) < 2:
        print("[警告] 提示词未能拆出多个镜头（模型可能只产出了单镜头结构），回退为整段提示词。")
        print("\n" + "=" * 60)
        print(prompt)
        print("=" * 60)
        return False

    seg_dir = session.directory / "segments"
    seg_dir.mkdir(parents=True, exist_ok=True)
    progress_path = seg_dir / "progress.json"
    done = progress.start
    total = len(segments)
    # 账与段表必须对得上（跳号/越界都算），**两条路径都要核**：v2 在
    # ``_run_segmented_flow_v2`` 里核，回退这里若不核，``progress.start = len(passed)``
    # 会把 {1,3} 读成「过了 2 段」并从第 3 段（position=2）起跑——**第 2 段永远不做、
    # 还不报错**，正是 AC6 要拦的形态（2026-10-27 复审实测缺口）。
    from ..shot_ledger import validate_shot_progress

    try:
        validate_shot_progress(progress.passed, total_segments=total)
    except ValueError as exc:
        print(f"[错误] 段产出账与本次分段对不上：{exc}")
        print("  这不是「一段都没做」——按错的账续跑会跳过从没做过的段。")
        print("  出路：恢复与账配套的分段后重跑（走续接）；或删掉 "
              f"{seg_dir / 'shot-ledger.jsonl'}（旧会话是 segments/progress.json）"
              "让它从第 1 段重做（已通过的段视频不会被认回）。")
        return False
    print(f"\n已按镜头拆分为 {total} 段逐次执行（每段提示词同时存入 {seg_dir}）。")
    if done:
        print(f"[续接] 检测到已完成 {done}/{total} 段，从第 {done + 1} 段继续。")

    try:
        rewrite_llm = build_chat_model()
    except Exception:
        rewrite_llm = None

    from ..shot_ledger import append_shot_output

    def record_pass(segment_number: int, shot, video: str) -> None:
        """回退路径的落账口径：段号＝**被核那段**（1-based），起止秒取它自己的时间窗。

        旧会话不写新账（见 ``_segment_progress``：从 done=N 继跑会让前 N 段在账上缺席，
        下一次续跑立刻跳号）。
        """
        if progress.legacy:
            return
        start_s = shot.start_seconds
        end_s = (start_s + shot.duration_seconds
                 if start_s is not None and shot.duration_seconds is not None else None)
        append_shot_output(session.directory, segment=segment_number, start_s=start_s,
                           end_s=end_s, video_path=video)

    for position in range(done, total):
        segment = segments[position]
        # ① 桥接帧：剥上一段尾帧 → 可证层交接校验 → 读图 → 写进 state（先于本段重写，
        #    否则写段 LLM 在信息上不可能写出与桥接帧一致的开场状态；issue #12/#13）
        # 非末段必须交出上一段视频（同上：不给 opt-out，否则闸门可被静默绕过）
        # 续跑时已通过的那一段不再问路径（issue #94：账上记着的就是它交回的那份）。
        if position > 0 and position not in progress.passed:
            judged = segments[position - 1]
            if not _acquire_bridge_frame(
                state, position, session.directory,
                expected_seconds=judged.duration_seconds,
                ask="上一段输出视频路径（必填，用于剥尾帧作本段首帧）：",
                segment_number=position + 1, total=total,
                # 回退路径在**本段开头**调闸门：交上来的是上一段的视频（judged＝position），
                # 这一帧成为本段的开场锚（anchor＝position+1，1-based）
                anchor_segment=position + 1, judged_video_segment=position,
                # 回退路径没有 plan 层，末态参照取上一段**落盘那份**提示词的收尾句
                reference=_previous_segment_tail(seg_dir, position),
                reference_label="（上一段提示词收尾句；本路径无 plan 末态声明）",
                # 落账点在闸门放行那一刻：被拦下/判重跑/停下的轮次走不到这里
                on_pass=lambda video, number=position, shot=judged:
                    record_pass(number, shot, video),
            ):
                return False  # 硬阻断（issue #13）：交接物不合格，不能算「已跑完」
        # ② 每段重写：soundscape/music 为本段重写英文摘要句（官方 §4.6/§4.7）；失败回退机械拆分并明示
        rewritten: str | None = None
        if rewrite_llm is not None:
            # 步骤横幅＋等待心跳（issue #70 / ADR 0019）：回退写段也走直调看门狗，
            # 卡住/重试只有订阅者看得见。横幅回答「在写第几段」，心跳回答「还活着吗」。
            with step_scope(f"正在写第 {position + 1}/{total} 段",
                            round_no=position + 1, total_rounds=total):
                rewritten = rewrite_segment_prompt(
                    segment, prompt, rewrite_llm,
                    state=state, is_first=position == 0, is_last=position + 1 == total,
                    position_index=position,
                )
        display_text = rewritten or segment.text
        fallback_label = "" if rewritten else "[回退] 声音描述保持整条原样（未按段重写）"

        duration_line = (
            f"{segment.duration_seconds:g}s"
            if segment.duration_seconds is not None else "未知时长"
        )
        window_line = (
            f"（全局时间窗：{segment.start_seconds:g}s 起）"
            if segment.start_seconds is not None else ""
        )
        (seg_dir / f"shot-{position + 1:02d}.md").write_text(display_text, encoding="utf-8")
        # 交付前校验：回退路径同样是「产出即交付」，同样不得静默
        _gate_segment_text(position, display_text, segment.duration_seconds,
                           segment.start_seconds or 0.0)
        print("\n" + "=" * 60)
        print(f"第 {position + 1}/{total} 段（Shot {segment.shot_number}）· 本段时长 {duration_line}{window_line}")
        if fallback_label:
            print(fallback_label)
        # 本段中文摘要：回车确认前强制展示，作为人工验收的对照锚
        if summary is not None:
            from ..summary import render_summary_zh

            print("-" * 60)
            print("[本段中文摘要]")
            print(render_summary_zh(summary, shot_number=segment.shot_number))
            print("对照摘要检查：人物/场景/动作/声音是否符合预期走向？")
        print("=" * 60)
        print(display_text)
        print("-" * 60)
        print(f"提示词文件：{seg_dir / f'shot-{position + 1:02d}.md'}")
        while True:
            # 同 v2：这里只宣告「已生成好」，不宣告「满意」——是否算过留给剥帧后的语义核验
            raw = _prompt("本段已在 ComfyUI 生成好后回车；输入 r 重显完整提示词，s 重看中文摘要：").lower()
            if raw in ("", "y", "yes"):
                break
            if raw == "r":
                print(display_text)
                continue
            if raw == "s" and summary is not None:
                from ..summary import render_summary_zh

                print(render_summary_zh(summary, shot_number=segment.shot_number))
                continue
            print("回车=已生成好，r=重显完整提示词，s=重看中文摘要。")
        if progress.legacy:
            # 旧会话仍以 progress.json 为进度（新账不回溯补账，见 _segment_progress）；
            # 新会话**停写**它：段产出账是唯一真源。
            progress_path.write_text(
                json.dumps({"done": position + 1, "total": total},
                           ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        if position + 1 < total:
            print(f"[完成] 第 {position + 1}/{total} 段。下面给出第 {position + 2} 段……")
    # 链的另一端（#16）：末段没有下一段的锚可核，这里补上「是否收在承诺的尾帧上」。
    # 并且**一律索要末段输出视频**（issue #94）：成片要用它。续跑时账上已通过就不再问。
    if total not in progress.passed:
        if not _verify_last_frame(state, total - 1, session.directory,
                                  # 回退路径没有 plan 层，末态参照取末段**落盘那份**提示词的收尾句
                                  end_hook=_previous_segment_tail(seg_dir, total),
                                  segment_number=total, total=total,
                                  expected_seconds=segments[-1].duration_seconds,
                                  on_pass=lambda video, shot=segments[-1]:
                                      record_pass(total, shot, video)):
            return False
    # 段序真源（#96）：机械回退路径没有 plan 层，全部段通过之后补写段表，收尾处的成片
    # 拼接照它排段。只在**全部段通过**时写（中途写的后果见函数 docstring）；旧会话不写
    # ——它没有段视频账，拼接本就不可用（票面 Out of Scope：不回溯补账）。
    if not progress.legacy and not _write_mechanical_plan(session.directory, segments):
        print("[成片] 分段的提示词里没有可用的起止时间戳：段表补不出来，拼接不可用。")
    print(f"\n✓ 全部 {total} 段已人工确认完成。")
    return True


def _segments_done(directory: Path) -> int:
    """``segments/progress.json`` 里的 ``done=N``：**旧会话兼容读口**（issue #94）。

    新会话**不再写**这个文件——段产出账（``segments/shot-ledger.jsonl``）是「第几段
    过了」的唯一真源。它留着只为两件事：旧会话没有新账时回退读它（并当面说破「拼接
    不可用」，见 ``_segment_progress``），以及旧会话继续写它作为自己的进度。

    读不到文件即 0；**文件在但读不出来会显式告警**（静默返回 0 与「一段都没做过」
    无法区分——那正是本票要消灭的形态）。

    两条路径写入 ``done`` 的**时机不同**，别把它当成同一个东西：
    - v2：闸门（+ 语义核验）通过之后才写，所以 ``done=N`` 同时意味着「前 N 段的交接物
      已验过」；
    - 回退：本段确认后立即写，而它输出视频的闸门要到**下一轮开头**才跑，所以
      ``done=N`` 只保证「前 N 段的提示词已交付」。
    两者都**可以安全跳过前 N 段**：回退路径跳过后立刻会补跑那一次闸门（它在
    ``for position in range(done, total)`` 的第一件事），没有闸门被绕过。
    """
    import json

    path = directory / "segments" / "progress.json"
    if not path.is_file():
        return 0
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"[警告] 读不出 {path}（{exc}）：按「一段都没做过」处理。")
        return 0
    if not isinstance(raw, dict):
        print(f"[警告] {path} 不是对象，按「一段都没做过」处理。")
        return 0
    try:
        return max(0, int(raw.get("done", 0)))
    except (TypeError, ValueError):
        print(f"[警告] {path} 的 done 字段不是整数，按「一段都没做过」处理。")
        return 0


def _cached_plans(directory: Path) -> list | None:
    """已落盘的分段规划（``segments/plan.json``）；**文件不在**返回 None。

    续接必须复用它：``plan_segments`` 是 LLM 调用，重跑会得到**另一套**分段边界，
    而 ``progress.json`` 的 ``done=N`` 只对产出它的那套分段有意义——用旧进度去索引
    新分段，会跳过没做过的段、重做做过的段。GEN005 当初靠手抄一份 ``plan.json``
    绕过这个（``resume_gen005.py``，issue #17）。

    文件**在**但读不回来（截断、格式错、字段缺）会**抛出**——不能返回 None 让调用方
    当成「没有缓存」去重规划：那会静默走进上面那个 bug。调用方按「落盘物不一致」处理。
    """
    import json

    from ..segment_planner import SegmentPlan

    path = directory / "segments" / "plan.json"
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"分段规划读不出来（{path}）：{exc}") from exc
    if not isinstance(raw, list) or not raw:
        raise ValueError(f"分段规划不是非空列表（{path}）")
    try:
        return [SegmentPlan.from_dict(item) for item in raw]
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"分段规划字段不全（{path}）：{exc}") from exc


def _run_segmented_flow_v2(session: SessionState, plans: list, state: dict, llm,
                           summary=None, *, plans_from_cache: bool = False,
                           progress: _SegmentProgress | None = None) -> bool:
    """v2 陪跑：边生成边展示——当前段确认完成后，先剥本段尾帧读图（下一段的
    Picture 1 锚，issue #12），再后台预写下一段；用户看第 N 段提示词、去 ComfyUI
    生成、贴回尾帧，回车进入下一段时若已写完直接展示，否则等待。segments/shot-NN.md
    逐段落盘，**段产出账**（``segments/shot-ledger.jsonl``）支持中断续跑。返回值＝
    是否跑完全部段。

    ``plans_from_cache``：``plans`` 是否是从 ``segments/plan.json`` 读回来的。**这条
    不变量是硬要求**：账里的段号只对产出它的那套分段有意义，所以只要账上有通过行，
    就只允许用读回来的那一份继续；否则（重规划过／plan.json 被删）新分段边界配旧
    进度会**跳过从没写过来的段**，而且不报错。

    ``progress``：续跑起点与已通过段（issue #94）。不传就现读——直接调用本函数的
    测试与兄弟入口走这条路；生产路径由 ``_run_segmented_flow`` 读一次后传进来，
    免得旧会话的「拼接不可用」提示打两遍。

    预取收益建立在「下一段的桥接帧已就位」之上：下一段桥接帧来自本段输出视频，
    本段完成前它不存在，所以预取必须在本段剥帧读图之后启动（正确性优先于并行）。

    不收 ``brief``（issue #26）：它只被转手喂给写段请求的那个死参数，写段改从 state 读
    修订之后它就没有读者了——死接线留着，下一个人就会以为它是有用的。
    """
    import json
    import threading
    import time

    from ..segment_prompts import write_segment_v2
    from ..shot_ledger import append_shot_output, validate_shot_progress

    seg_dir = session.directory / "segments"
    seg_dir.mkdir(parents=True, exist_ok=True)
    progress_path = seg_dir / "progress.json"
    plan_cache = seg_dir / "plan.json"
    total = len(plans)
    if progress is None:
        try:
            progress = _segment_progress(session.directory)
        except (OSError, ValueError) as exc:
            print(f"[错误] 段产出账读不回来：{exc}")
            print("  这不是「一段都没做」——静默从第 1 段重来会把已通过的段当没做过。")
            return False
    try:
        # 段表与账必须对得上（段号越界/跳号都算）：按错的账续跑会跳过从没做过的段。
        validate_shot_progress(progress.passed, total_segments=total)
    except ValueError as exc:
        print(f"[错误] 段产出账与本次分段对不上：{exc}")
        print("  出路：恢复与账配套的 segments/plan.json 后重跑；或删掉 "
              f"{seg_dir / 'shot-ledger.jsonl'} 让它从第 1 段重做"
              "（已通过的段视频不会被认回）。")
        return False
    done = progress.start
    if done and not plans_from_cache:
        # 账说做过 done 段，但这次的分段不是产出那份账的分段。重规划换掉了边界，
        # ``done`` 就不再指向同一批段——照旧从第 done+1 段开始会跳过没写过的段。
        # 停在这里，并且**先不覆盖 plan.json**（它可能是唯一能救回正确分段的东西）。
        print(f"[错误] 段产出账说已通过 {done} 段，但本次的分段规划不是产出它的那一份"
              f"（重规划过，或 {plan_cache} 缺失）。拿旧进度索引新分段会跳过没写过的段，"
              "所以不能继续。")
        print("  出路：恢复 segments/plan.json 后重跑（走续接）；或删掉 "
              f"{seg_dir / 'shot-ledger.jsonl'}（旧会话是 segments/progress.json）"
              "让它从第 1 段重做。")
        return False

    def record_pass(segment_number: int, plan, video: str) -> None:
        """本段落账（issue #94）：段号 1-based，起止秒取本段 plan 的时间窗。

        旧会话不写新账：它从 done=N 起跑，前 N 段没有行，写进去就是一条**跳号**的账。
        """
        if progress.legacy:
            return
        append_shot_output(session.directory, segment=segment_number,
                           start_s=plan.start_s, end_s=plan.end_s, video_path=video)
    plan_cache.write_text(
        json.dumps([p.to_dict() for p in plans], ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\n已规划为 {total} 段逐次执行（每段文件存入 {seg_dir}）。")
    if done:
        print(f"[续接] 检测到已完成 {done}/{total} 段，从第 {done + 1} 段继续。")

    def seg_file(index: int) -> Path:
        return seg_dir / f"shot-{index + 1:02d}.md"

    def generate(position: int) -> str | None:
        """写第 position 段；已有落盘文件则直接复用（续跑不重写）。

        不再在这里手发 agent_start/agent_done：写段已改走直调看门狗（issue #70），
        那一次调用自己会发同形事件并进成本表——再手发一份就是每段两次记账。
        """
        f = seg_file(position)
        if f.is_file():
            try:
                return f.read_text(encoding="utf-8")
            except OSError:
                pass
        text = write_segment_v2(plans[position], plans, state, llm)
        if text:
            f.write_text(text, encoding="utf-8")
        return text

    loader = TextProgress()
    from ..observability import reporter
    reporter.subscribe(loader)

    # 后台预取：当前段确认后，下一段在后台写。inflight 记录在途线程：预取与
    # take() 兜底都走这里，只查结果在不在会把「线程在途」误判成「没在写」，
    # 同一段被两个线程各写一遍（两次 LLM 调用，结果互相覆盖）。
    prefetch: dict = {}
    # 主线程 add、工作线程 finally discard：CPython set 的单操作原子（GIL），
    # 不另加锁；只做「在不在」判断，不做跨多步的 check-then-act。
    inflight: set[int] = set()

    def start_prefetch(position: int) -> None:
        if position >= total or position in prefetch or position in inflight:
            return
        inflight.add(position)

        def work() -> None:
            try:
                prefetch[position] = generate(position)
            except Exception as exc:  # noqa: BLE001 - 异常若逃逸，take() 的等待循环会永远自旋
                print(f"[警告] 第 {position + 1} 段撰写线程异常：{exc}")
                prefetch[position] = None
            finally:
                inflight.discard(position)

        threading.Thread(target=work, daemon=True).start()

    def take(position: int) -> str | None:
        if position not in prefetch:
            start_prefetch(position)  # 没预取过就现在跑（首段；或旧产物续跑文件缺失时）
        while position not in prefetch:
            time.sleep(0.3)
        return prefetch.pop(position)

    try:
        for position in range(done, total):
            plan = plans[position]
            # 当前段：等到写出来（若后台已在跑，很快返回）
            if position not in prefetch:
                # 步骤横幅（issue #70 / ADR 0019）：与 ref 路径同一句话式。心跳由上面
                # 全局订阅的 TextProgress 承担（它还兼管后台预取段的事件，所以这里
                # 不另开 step_scope，免得同一事件打两行）。
                print(format_step_banner(f"正在写第 {position + 1}/{total} 段",
                                         round_no=position + 1, total_rounds=total),
                      flush=True)
            text = take(position)
            if text is None:
                # 病因与尝试次数已由写段层当面打出（issue #70）：这里只说这一段被跳过，
                # 不重复转述病因（同一句话打两遍，用户会当成两次失败）。
                print(f"[警告] 第 {position + 1} 段没有产出（病因见上），跳过本段。")
                continue
            # 交付前校验：分段是唯一「产出即交付」的路径，不合格必须当面示警（绝不静默）
            errors = _gate_segment_text(position, text, float(plan.duration_s),
                                        float(plan.start_s))
            if errors:
                print(
                    f"\n⚠ 第 {position + 1}/{total} 段未通过校验：请先按上面的 issue 修正，再复制到 H3。",
                    flush=True,
                )
            else:
                print(f"\n✓ 第 {position + 1}/{total} 段提示词已生成，可立即复制到 H3 生成视频。", flush=True)

            print("\n" + "=" * 60)
            print(f"第 {position + 1}/{total} 段 · 时长 {plan.start_s}-{plan.end_s}s（{plan.duration_s}s）· 覆盖 Shot {plan.shots_in_segment}")
            print("=" * 60)
            # 本段中文摘要：提示词正文是英文，走向核对靠 plan 层的中文 summary + end_hook 展示
            if plan.summary:
                print(f"[本段中文摘要] {plan.summary}")
            print(text)
            print("-" * 60)
            print(f"提示词文件：{seg_file(position)}")
            if plan.end_hook:
                print(f"本段末态（段尾钩子，桥接帧验收对照）：{plan.end_hook}")
            while True:
                # 这里只宣告「已在 ComfyUI 生成好」，**不宣告「满意」**：本段是否算过，
                # 由接着的剥帧 + 语义核验（并排摆出末态与尾帧实际画面）来定（#14 重排）。
                raw = _prompt("本段已在 ComfyUI 生成好后回车；输入 r 重显提示词：").lower()
                if raw in ("", "y", "yes"):
                    break
                if raw == "r":
                    print(text)
                    continue
                print("回车=已生成好，r=重显提示词。")
            # 桥接帧：本段完成后，剥本段输出视频的尾帧 → 可证层交接校验 → 读图 →
            # 写进 state，作为下一段（position+1）的 Picture 1 锚（issue #12/#13）。
            # 必须发生在预取下一段**之前**：预取线程组装写段请求时要拿到下一段桥接帧的
            # 读图结果，「先预取后剥帧」会让写段 LLM 在时序上不可能锚定开场画面。
            # 非末段必须交出本段输出视频：可证层交接校验是本流程的必经环节，不给 opt-out
            # （ADR 0002：留 opt-out 等于闸门可被静默绕过，且下一段会去声明一张不存在的
            # Picture 1）。原「是否用本段尾帧…」开关与 #14 第 4 条重复，已在此一并落地。
            if position + 1 < total:
                if not _acquire_bridge_frame(
                    state, position + 1, session.directory,
                    expected_seconds=float(plan.duration_s),
                    ask="本段输出视频路径（必填，用于剥尾帧作下一段首帧）：",
                    segment_number=position + 1, total=total,
                    # v2 在**本段末尾**调闸门：交上来的是本段的视频（judged＝position+1），
                    # 这一帧成为下一段的开场锚（anchor＝position+2，1-based）
                    anchor_segment=position + 2, judged_video_segment=position + 1,
                    # 语义层参照＝**本段**（刚跑完的这段）声明的末态，而锚帧正是它的尾帧
                    reference=plan.end_hook,
                    reference_label="（本段 plan.end_hook）",
                    # 落账点＝闸门放行那一刻（issue #94）：被拦下/判重跑/停下的轮次
                    # 走不到这里，账上就不会有这一段的通过行。
                    on_pass=lambda video, number=position + 1, current=plan:
                        record_pass(number, current, video),
                ):
                    # 硬阻断（issue #13）：错误的交接物不得被当成下一段的事实喂下去。
                    # 账（旧会话是进度）也**不得**落盘——否则被拦下的这一段会被续跑跳过。
                    return False
            if progress.legacy:
                # 旧会话仍以 progress.json 为进度（新账不回溯补账，见 _segment_progress）；
                # 新会话**停写**它：段产出账是唯一真源。
                progress_path.write_text(
                    json.dumps({"done": position + 1, "total": total},
                               ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
            # 立即预取下一级（此时 state 已含下一段桥接帧的读图结果）
            start_prefetch(position + 1)
            if position + 1 < total:
                print(f"  （后台正在同时撰写第 {position + 2} 段提示词）", flush=True)
                print(f"[完成] 第 {position + 1}/{total} 段。")
    finally:
        reporter.unsubscribe(loader)
    # 链的另一端（#16）：末段不剥帧，所以段间闸门从没管过「整条视频是否收在承诺的尾帧上」。
    # 并**一律索要末段输出视频**（issue #94）：成片要用它。放在宣布完成**之前**：
    # 没通过就不算跑完，会话保持「分段进行中」。续跑时账上已通过就不再问一次。
    if total not in progress.passed:
        # 账上只会在「前 total 段都通过」时含 total 这一行，所以这里取 plans[-1] 安全。
        last_plan = plans[-1]
        if not _verify_last_frame(state, total - 1, session.directory,
                                  end_hook=last_plan.end_hook,
                                  segment_number=total, total=total,
                                  expected_seconds=float(last_plan.duration_s),
                                  on_pass=lambda video, current=last_plan:
                                      record_pass(total, current, video)):
            return False
    print(f"\n✓ 全部 {total} 段已人工确认完成。")
    return True


def _store_frame(source: Path, generation_dir: Path, role: str) -> dict:
    """把用户提交的关键帧复制进会话 ``frames/`` 目录，返回溯源记录（只复制不移动）。"""
    import hashlib
    import json
    import shutil

    frames_dir = Path(generation_dir) / "frames"
    frames_dir.mkdir(parents=True, exist_ok=True)
    suffix = Path(source).suffix or ".png"
    target = frames_dir / f"{role}{suffix}"
    digest = hashlib.sha256(Path(source).read_bytes()).hexdigest()
    if not target.exists() or hashlib.sha256(target.read_bytes()).hexdigest() != digest:
        shutil.copy2(source, target)
    record = {"role": role, "source": str(source), "target": str(target), "sha256": digest}
    manifest = frames_dir / "source.json"
    existing = {"schema_version": "frame-import.v1", "frames": []}
    if manifest.exists():
        try:
            raw = json.loads(manifest.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                existing = raw
        except (OSError, json.JSONDecodeError):
            pass
    frames = {item.get("role"): item for item in existing.get("frames", [])}
    frames[role] = record
    existing["frames"] = list(frames.values())
    manifest.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")
    return record


def _record_downgrade(directory: Path, downgraded: str) -> None:
    """把变体降级事实补写进 session-state.json。"""
    import json

    target = directory / "session-state.json"
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
        raw["variant_downgraded"] = downgraded
        target.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
    except (OSError, json.JSONDecodeError):
        pass


__all__ = ["run_wizard", "resolve_effective_variant"]

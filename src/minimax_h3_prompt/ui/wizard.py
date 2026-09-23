"""两阶段生产流向导。

阶段 1：主题/时长/风格 → 管线跑到声音设计 → 首尾帧生图提示词落进会话目录 → 人工确认/意见循环。
阶段 2：提交真实帧图片（缺帧自动降级 I2VA/L2VA/T2VA）→ 暂存进会话 frames/ → qwen3.7-plus 读图
→ 视频提示词组装 + QA 精修。

两阶段之间可关终端：阶段 1 的 state 经 session_store 落盘，重启后自动发现待续接会话。
"""
from __future__ import annotations

import sys
import time
from contextlib import contextmanager
from pathlib import Path

from ..brief_parser import Brief, RefItem
from ..config import Config
from ..generation import render_fl2va_frame_markdown
from ..graph.pipeline import run_stage1, run_stage2
from ..session_store import (
    STATUS_AWAITING_FRAMES,
    STATUS_COMPLETED,
    STATUS_SEGMENTED_RUNNING,
    STATUS_STAGE1_RUNNING,
    SessionState,
    find_awaiting_sessions,
    load_session,
    save_session,
)
from .progress import TextProgress


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
    """阶段 1 视频生成方式三选一：首帧(I2VA)/尾帧(L2VA)/首尾帧(FL2VA，默认)。"""
    print("请选择视频生成方式：")
    print("  1. 首帧生成视频（I2VA，以首帧图起笔，向前发展）")
    print("  2. 尾帧生成视频（L2VA，从合理开场逐步收敛到尾帧图）")
    print("  3. 首尾帧生成视频（FL2VA，首尾帧锚定，默认）")
    while True:
        raw = _prompt("输入 1/2/3（回车=3 首尾帧）：").strip()
        if not raw:
            return "FL2VA"
        if raw in ("1", "2", "3"):
            return {1: "I2VA", 2: "L2VA", 3: "FL2VA"}[int(raw)]
        print("无效输入，请输入 1/2/3。")


# LangGraph 节点名 → 用户可读环节名（用于节点级进度）。
_NODE_LABELS = {
    "producer": "制作人",
    "director": "导演",
    "creative_rt": "创意圆桌",
    "screenwriter": "编剧",
    "parallel_designers": "人物/场景/道具设计",
    "art_director": "美术指导",
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


@contextmanager
def _progress_scope(title: str):
    """订阅事件总线，把各角色进度实时打到终端；退出（含异常）必解绑并报总用时。"""
    from ..observability import reporter

    listener = TextProgress()
    start = time.time()
    print(f"\n{title}", flush=True)
    reporter.subscribe(listener)
    try:
        yield
    finally:
        reporter.unsubscribe(listener)
        _drain_stdin()
        m, s = divmod(int(time.time() - start), 60)
        print(f"[完成] 本段用时 {m} 分 {s:02d} 秒。\n", flush=True)


def run_wizard(config: Config) -> int:
    """无参数主入口：两阶段向导。返回进程退出码。"""
    if not sys.stdin.isatty():
        raise SystemExit("向导需要交互终端运行；脚本场景请用 --brief 快路径。")

    session = _offer_resume(config)
    if session is None:
        session = _phase1_new(config)
        if session is None:
            return 1
    # 阶段 1 完成（或完成了一半）后若断在阶段 1 中途，先从中断节点续跑完阶段 1
    if session.status == STATUS_STAGE1_RUNNING:
        session = _resume_stage1(config, session)
        if session is None:
            return 1
    # 分段陪跑中途退出：整条提示词与帧锚都已落盘，直接回到陪跑续接。
    # 绝不能再走阶段 2——那正是 #17 的代价（重做阶段 2 + 已完成的所有段重来）。
    if session.status == STATUS_SEGMENTED_RUNNING:
        return _resume_segmented(config, session)
    # 阶段 1 完成后直接进入阶段 2（生图很快，无需暂停等人工确认）。
    return _phase2_collect_and_finish(config, session)


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
    if next_index >= len(_STAGE1_CHAIN):
        # 阶段 1 已跑完但状态还是 running（比如写盘刚好中断在收尾），直接进阶段 2
        return session
    resume_node = _STAGE1_CHAIN[next_index]
    print(f"[续接] 上次跑到「{last_node}」，从「{resume_node}」继续阶段 1……")

    brief = session.brief
    generation_dir = session.directory
    initial_state = dict(session.stage_state)

    def _save_progress(node_name: str, current_state: dict) -> None:
        progress_state = dict(current_state)
        progress_state["_progress"] = {"last_completed_node": node_name}
        save_session(generation_dir, brief, progress_state, status=STATUS_STAGE1_RUNNING)

    with _progress_scope(f"[阶段 1·续接] 从「{resume_node}」继续……"):
        state, model, agents = run_stage1(
            brief, config,
            on_node=_report_node,
            resume_from_node=resume_node,
            initial_state=initial_state,
            on_step_done=_save_progress,
        )
    _drain_stdin()

    # 复用 _phase1_new 的收尾逻辑（这里不重跳生图提示词修改循环，沿用已有的 bundle）
    if "_progress" in state:
        state.pop("_progress")
    save_session(generation_dir, brief, state, status=STATUS_AWAITING_FRAMES)
    print("\n阶段 1 已恢复完成。")
    return load_session(generation_dir)


# ---------------------------------------------------------------------------
# 阶段 1
# ---------------------------------------------------------------------------

def _phase1_new(config: Config) -> SessionState | None:
    topic = ""
    while not topic:
        topic = _prompt("请输入视频主题（必填）：")
        if not topic:
            print("视频主题不能为空。")
    duration_raw = _prompt(f"视频时长秒数（回车默认 {config.default_duration:.0f}）：")
    try:
        duration = float(duration_raw) if duration_raw else config.default_duration
        if duration <= 0:
            raise ValueError
    except ValueError:
        print(f"时长无效，使用默认 {config.default_duration:.0f}s。")
        duration = config.default_duration
    style = _prompt("视觉风格（回车=AI 根据主题自行确定）：")
    variant = _choose_generation_mode()

    brief = Brief(
        mode="base",
        variant=variant,
        duration=duration,
        style=style or "",
        language=config.default_language,
        plot=topic,
        raw=topic,
    )
    generation_dir = _session_dir(config, topic)

    # 阶段 1 断点：每完成一个节点就立刻落盘（含 _progress 记录跑到哪个节点）
    def _save_progress(node_name: str, current_state: dict) -> None:
        progress_state = dict(current_state)
        progress_state["_progress"] = {"last_completed_node": node_name}
        save_session(generation_dir, brief, progress_state, status=STATUS_STAGE1_RUNNING)

    with _progress_scope("[阶段 1] 正在生成剧本、设计与首尾帧生图提示词……（预计几分钟，期间无需输入）"):
        state, model, agents = run_stage1(
            brief, config, on_node=_report_node, on_step_done=_save_progress
        )
    _drain_stdin()
    bundle = state.get("fl2va_prompt_bundle") or {}
    state.setdefault("fl2va_frame_descriptions", [])
    state.setdefault("frame_images", [])
    save_session(generation_dir, brief, state, status=STATUS_AWAITING_FRAMES)

    while True:
        _show_frame_prompts(state, generation_dir)
        if not _confirm("对生图提示词有修改意见？", default=False):
            break
        mode = _prompt("输入 1 只重出画面提示词（附意见），输入 2 整个流程重来：")
        if mode.strip() == "2":
            return _phase1_new(config)
        feedback = _prompt("请输入修改意见：")
        if not feedback:
            continue
        from ..graph.nodes import make_nodes

        brief_feedback = Brief(**{**brief.__dict__})
        nodes = make_nodes(agents, model, brief_feedback, config)
        frame_node = nodes["fl2va_frame_prompts"]
        updated = dict(state)
        updated["frame_feedback"] = feedback
        # 复用节点函数重出首尾帧；把意见注入其上下文最直接的方式是临时改写 plot 追加约束
        brief_retry = Brief(**{**brief.__dict__, "plot": f"{brief.plot}\n（用户修改意见：{feedback}）"})
        updated["brief"] = brief_retry
        with _progress_scope("[重新生成] 正在按您的意见重出首尾帧生图提示词……（请稍候，期间无需输入）"):
            update = frame_node(updated)
        updated.update(update)
        state.clear()
        state.update(updated)
        save_session(generation_dir, brief_retry, state, status=STATUS_AWAITING_FRAMES)
        print("[已更新] 请查看下方新版本的提示词。")

    print("\n阶段 1 完成。请复制上面的生图提示词到 ComfyUI（Z-Image）生成图片。")
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

    「未完成」指 ``awaiting_frames`` / ``stage1_running`` / ``segmented_running`` 三态。
    这里只按「不是 completed」判，所以新增未完成态自动获得续接语义——但前提是
    **确实有代码在写那个态**：``segmented_running`` 曾长期只存在于本 docstring 里
    （分段陪跑从不改会话状态），于是「分段进行中」这一态在运行时不存在，向导会另开
    新 GEN、``progress.json`` 永远读不到（issue #17）。
    """
    from ..session_store import STATUS_COMPLETED, load_session

    slug_dir = Path(config.sessions_root) / _topic_slug(topic)
    existing = sorted(slug_dir.glob("GEN*"), key=lambda p: p.name)
    if existing:
        latest = existing[-1]
        session = load_session(latest)
        if session is not None and session.status != STATUS_COMPLETED:
            directory = latest  # 未完成 → 续接
        else:
            directory = slug_dir / f"GEN{len(existing) + 1:03d}"  # 已完成/无会话 → 新 GEN
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


def render_fl2va_frame_markdown_from_bundle(bundle, frame: str) -> str:
    """渲染单帧双模型提示词（不依赖 GenerationResult）；空帧返回空串。"""
    if frame not in {"first", "last"}:
        raise ValueError(f"帧类型无效：{frame}")
    rows = bundle.first if frame == "first" else bundle.last
    rows = [item for item in rows if item.model_family == "zimage"]  # 只展示 Z-Image
    if not rows:
        return ""
    title = "首帧" if frame == "first" else "尾帧"
    variant = "FL2VA" if (bundle.first and bundle.last) else "I2VA" if bundle.first else "L2VA"
    parts = [f"# {variant} {title}生图提示词（Z-Image）", ""]
    for item in rows:
        parts.extend([
            f"- 推荐尺寸：`{item.width} × {item.height}`",
            f"- 提示词节点：`{item.prompt_node_id}`", "",
            "### Positive Prompt", "", item.positive_prompt, "",
        ])
        if item.instructions:
            parts.extend(["### 操作说明", "", *[f"- {x}" for x in item.instructions], ""])
    return "\n".join(parts).rstrip()


# ---------------------------------------------------------------------------
# 阶段 2
# ---------------------------------------------------------------------------

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


def _phase2_collect_and_finish(config: Config, session: SessionState) -> int:
    brief = session.brief
    state = dict(session.stage_state)
    generation_dir = session.directory

    print("\n[阶段 2] 请提交生成好的关键帧图片（回车=跳过该项）。")
    while True:
        try:
            first_raw, last_raw = _collect_frame_paths(brief.variant)
            first_path = _resolve_frame_file(first_raw, "首帧")
            last_path = _resolve_frame_file(last_raw, "尾帧")
        except FileNotFoundError as exc:
            print(f"[错误] {exc}")
            if not _confirm("重新提交路径？", default=True):
                return 1
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

        with _progress_scope(f"{config.vision_model.model} 正在读取关键帧实际画面……"):
            audits = audit_frame_images(refs, brief.variant, on_frame=report_frame)
        descriptions = [a.to_dict() for a in audits]
        for audit in audits:
            label = "第一帧" if audit.role == "first" else "最后一帧"
            print(f"\n[{label}实际画面] {audit.description}")

    state["fl2va_frame_descriptions"] = descriptions
    state["frame_images"] = frame_records

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
    from ..tools.preflight import check_segment_video

    issues = check_segment_video(Path(video_path), expected_seconds)
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
GATE_CONTINUE = "continue"                  # 人判：尾帧画面达到了上一段的末态
GATE_ACCEPT = "accept"                      # 明知未达到，显式覆盖「接受并继续」
GATE_RERUN = "rerun"                        # 重跑本段（回 ComfyUI 重生成，再交一次视频）
GATE_MANUAL_FRAME = "manual_frame"          # 手工换帧（换一张尾帧图，仍在桥接链上）
GATE_KEYFRAME_RESTART = "keyframe_restart"  # 改用关键帧图另起（放弃桥接链；#10 的 B，只作兜底）
GATE_STOP = "stop"                          # 停下（默认出路）

_OUTCOME_LABELS = {
    GATE_CONTINUE: "画面达到末态，继续",
    GATE_ACCEPT: "显式覆盖：接受并继续",
    GATE_RERUN: "重跑本段",
    GATE_MANUAL_FRAME: "手工换帧",
    GATE_KEYFRAME_RESTART: "改用关键帧图另起",
    GATE_STOP: "停下（默认）",
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
                          reference_label: str = "（上一段 plan.end_hook）") -> bool:
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
            return True
        return False  # GATE_STOP


def _capture_bridge_frame(video_path: str, position: int, generation_dir: Path, *,
                          expected_seconds: float | None = None,
                          frame_suffix: str = "start"
                          ) -> tuple[list, str | None, Path | None]:
    """剥一段视频的尾帧 → 可证层交接校验 → 读图（issue #12 / #13）。

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
    # 可证层先判：不合格就不必再花一次读图调用，也绝不把错误的交接物喂进 state
    errors = _gate_segment_handoff(video_path, expected_seconds)
    if errors:
        return errors, None, frame
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


def _verify_last_frame(state: dict, position: int, generation_dir: Path, *,
                       end_hook: str | None, segment_number: int, total: int,
                       expected_seconds: float | None) -> bool:
    """末段尾帧核验（issue #16）：链的**另一端**，此前无人校验。

    链式闸门只管**段间**（末段不剥帧：``if position + 1 < total``）。而 FL2VA/L2VA 下
    末段必须收在用户提交的那张尾帧上——此前只有提示词里的一句**声明**
    （``_frame_anchor_note``），没有任何校验，末段连输出视频路径都不会索要。整条视频的
    最后画面是否真的落在承诺的那张尾帧上，是 FL2VA/L2VA 的核心承诺，不能只靠人自己盯。

    没有 ``role: last`` 的读图结果（I2VA / T2VA / 用户没交尾帧）→ 什么都不做直接放行：
    没有可核的承诺，凭空报一句「未达到」就是误报。

    返回 True = 放行（含「没东西可核」与「量不出来」两种降级）；False = 停下。
    """
    promised = _promised_last_frame_description(state)
    if promised is None:
        return True
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


def _run_long_form(brief: Brief, session: SessionState, prompt: str, summary, state: dict,
                   directory: Path) -> None:
    """长视频分段陪跑：跑之前标「分段进行中」，**全部段确认完成**才转「已完成」。

    这两次状态写入就是 #17 的全部要点。标成未完成，中断（闸门硬阻断 / Ctrl-C / 重启）
    之后重跑向导会落回**同一个 GEN**，并由 ``segments/progress.json`` 从第 N+1 段续接；
    半途而废**不得**被标成完成——那会让下次重跑另开新 GEN、阶段 2 重做（实测可超
    10 分钟）＋ 已完成的所有段重来，``progress.json`` 永远读不到。

    ``state`` 必须是**阶段 2 之后的**那份：它带着 ``fl2va_frame_descriptions``（帧图
    读图结果），拿旧 session 对象落盘会把它丢掉，分段提示词就退回照分镜表写
    （2026-09-22 首帧不锚定缺陷）。
    """
    save_session(directory, brief, state, status=STATUS_SEGMENTED_RUNNING)
    finished = _run_segmented_flow(brief, session, prompt, summary=summary, state=state)
    if not finished:
        print(f"\n[续接] 本次没有跑完全部段，会话保持「分段进行中」：下次重跑向导会落回 "
              f"{directory.name}，从已完成的那一段之后继续（阶段 1/2 不重跑）。")
        return
    save_session(directory, brief, state, status=STATUS_COMPLETED)


def _resume_segmented(config: Config, session: SessionState) -> int:
    """分段陪跑续接：不碰阶段 1/2，用已落盘的整条提示词与帧锚直接回到陪跑。

    阶段 2 的产物（``video-prompt.md``、``summary-zh.json``、带帧图读图结果的
    ``stage_state``）都在会话目录里，所以这里零 LLM 调用就能续接。
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


def _run_segmented_flow(brief: Brief, session: SessionState, prompt: str, summary=None,
                        state: dict | None = None) -> bool:
    """把整条提示词按 [Shot N] 拆成单镜头提示词，逐段陪跑。返回**是否跑完全部段**。

    state：阶段 2 之后的最终 state（含 ``fl2va_frame_descriptions`` 帧图读图结果）；
    省略时退回 ``session.stage_state``（续跑/测试兼容）。

    循环规则：先尽量用 LLM 把本段 soundscape/music 按时间窗重写成独立提示词（
    失败自动回退机械拆分并明示）；显示信息卡（段号/Shot/时长/文件路径）→ 人工复制
    到 H3 生成 → 回车确认 → 才显示下一段；第 2 段起可提交上一段视频路径，自动剥尾帧
    作为本段首帧图；中断后由 progress.json 续接。

    返回值是给调用方判「该不该把会话标成 completed」用的（issue #17）：闸门硬阻断、
    Ctrl-C 都走不到正常结尾，那种情况必须保持「分段进行中」，否则下次重跑另开新 GEN。
    """
    import json

    from ..model_factory import build_chat_model
    from ..segment_planner import plan_segments
    from ..segment_prompts import (
        frame_anchor_context,
        rewrite_segment_prompt,
        split_shots_from_prompt,
        write_segment_v2,
    )

    # ① 先走新流程：segment_planner 规划整秒分段边界 → 每段独立细写（官方英文格式，spec 2026-09-22）
    state = state if state is not None else session.stage_state
    done = _segments_done(session.directory)
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
        return _run_segmented_flow_v2(brief, session, cached, state, llm, summary=summary,
                                      plans_from_cache=True)

    plans = None
    try:
        llm = build_chat_model()
        if state.get("shot_table"):
            # 帧图实际画面一并交给规划层：分镜表只是计划，用户可能复用/改了图（冲突时以图为准）
            with _progress_scope("[分段规划] 正在按 4-10s 整数边界切分剧情并锁定衔接……"):
                plans = plan_segments(
                    str(state.get("shot_table", "")), brief.duration, llm,
                    frame_context=frame_anchor_context(state),
                )
    except Exception as exc:  # noqa: BLE001 - 规划失败回退旧路径
        print(f"[提示] 分段规划失败（{exc}），回退到按 [Shot N] 机械拆分。")

    if plans:
        print(f"[分段规划] 规划了 {len(plans)} 段：" + ", ".join(f"{p.start_s}-{p.end_s}s" for p in plans))
        return _run_segmented_flow_v2(brief, session, plans, state, llm, summary=summary)

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
    done = _segments_done(session.directory)
    total = len(segments)
    print(f"\n已按镜头拆分为 {total} 段逐次执行（每段提示词同时存入 {seg_dir}）。")
    if done:
        print(f"[续接] 检测到已完成 {done}/{total} 段，从第 {done + 1} 段继续。")

    try:
        rewrite_llm = build_chat_model()
    except Exception:
        rewrite_llm = None

    for position in range(done, total):
        segment = segments[position]
        # ① 桥接帧：剥上一段尾帧 → 可证层交接校验 → 读图 → 写进 state（先于本段重写，
        #    否则写段 LLM 在信息上不可能写出与桥接帧一致的开场状态；issue #12/#13）
        # 非末段必须交出上一段视频（同上：不给 opt-out，否则闸门可被静默绕过）
        if position > 0:
            if not _acquire_bridge_frame(
                state, position, session.directory,
                expected_seconds=segments[position - 1].duration_seconds,
                ask="上一段输出视频路径（必填，用于剥尾帧作本段首帧）：",
                segment_number=position + 1, total=total,
                # 回退路径在**本段开头**调闸门：交上来的是上一段的视频（judged＝position），
                # 这一帧成为本段的开场锚（anchor＝position+1，1-based）
                anchor_segment=position + 1, judged_video_segment=position,
                # 回退路径没有 plan 层，末态参照取上一段**落盘那份**提示词的收尾句
                reference=_previous_segment_tail(seg_dir, position),
                reference_label="（上一段提示词收尾句；本路径无 plan 末态声明）",
            ):
                return False  # 硬阻断（issue #13）：交接物不合格，不能算「已跑完」
        # ② 每段重写：soundscape/music 为本段重写英文摘要句（官方 §4.6/§4.7）；失败回退机械拆分并明示
        rewritten: str | None = None
        if rewrite_llm is not None:
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
        progress_path.write_text(
            json.dumps({"done": position + 1, "total": total}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        if position + 1 < total:
            print(f"[完成] 第 {position + 1}/{total} 段。下面给出第 {position + 2} 段……")
    # 链的另一端（#16）：末段没有下一段的锚可核，这里补上「是否收在承诺的尾帧上」。
    if not _verify_last_frame(state, total - 1, session.directory,
                              # 回退路径没有 plan 层，末态参照取末段**落盘那份**提示词的收尾句
                              end_hook=_previous_segment_tail(seg_dir, total),
                              segment_number=total, total=total,
                              expected_seconds=segments[-1].duration_seconds):
        return False
    print(f"\n✓ 全部 {total} 段已人工确认完成。可在剪辑工具中按顺序拼接 segments/ 下的各段输出。")
    return True


def _segments_done(directory: Path) -> int:
    """``segments/progress.json`` 里已完成（**该段提示词已交付并确认**）的段数。

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


def _run_segmented_flow_v2(brief: Brief, session: SessionState, plans: list, state: dict, llm,
                           summary=None, *, plans_from_cache: bool = False) -> bool:
    """v2 陪跑：边生成边展示——当前段确认完成后，先剥本段尾帧读图（下一段的
    Picture 1 锚，issue #12），再后台预写下一段；用户看第 N 段提示词、去 ComfyUI
    生成、贴回尾帧，回车进入下一段时若已写完直接展示，否则等待。segments/shot-NN.md
    逐段落盘，progress.json 支持中断续跑（已写入文件的段不重写）。返回值＝是否跑完全部段。

    ``plans_from_cache``：``plans`` 是否是从 ``segments/plan.json`` 读回来的。**这条
    不变量是硬要求**：``progress.json`` 的 ``done=N`` 只对产出它那套分段有意义，所以
    只要 ``done > 0``，就只允许用读回来的那一份继续；否则（重规划过／plan.json 被删）
    新分段边界配旧进度会**跳过从没写过的段**，而且不报错。

    预取收益建立在「下一段的桥接帧已就位」之上：下一段桥接帧来自本段输出视频，
    本段完成前它不存在，所以预取必须在本段剥帧读图之后启动（正确性优先于并行）。
    """
    import json
    import threading
    import time

    from ..segment_prompts import write_segment_v2

    seg_dir = session.directory / "segments"
    seg_dir.mkdir(parents=True, exist_ok=True)
    progress_path = seg_dir / "progress.json"
    plan_cache = seg_dir / "plan.json"
    done = _segments_done(session.directory)
    if done and not plans_from_cache:
        # 进度说做过 done 段，但这次的分段不是产出那份进度的分段。重规划换掉了边界，
        # ``done`` 就不再指向同一批段——照旧从第 done+1 段开始会跳过没写过的段。
        # 停在这里，并且**先不覆盖 plan.json**（它可能是唯一能救回正确分段的东西）。
        print(f"[错误] progress.json 说已完成 {done} 段，但本次的分段规划不是产出它的那一份"
              f"（重规划过，或 {plan_cache} 缺失）。拿旧进度索引新分段会跳过没写过的段，"
              "所以不能继续。")
        print("  出路：恢复 segments/plan.json 后重跑（走续接）；或删掉 segments/progress.json "
              "让它从第 1 段重做。")
        return False
    plan_cache.write_text(
        json.dumps([p.to_dict() for p in plans], ensure_ascii=False, indent=2), encoding="utf-8"
    )
    total = len(plans)
    print(f"\n已规划为 {total} 段逐次执行（每段文件存入 {seg_dir}）。")
    if done:
        print(f"[续接] 检测到已完成 {done}/{total} 段，从第 {done + 1} 段继续。")

    def seg_file(index: int) -> Path:
        return seg_dir / f"shot-{index + 1:02d}.md"

    def generate(position: int) -> str | None:
        """写第 position 段；已有落盘文件则直接复用（续跑不重写）。"""
        f = seg_file(position)
        if f.is_file():
            try:
                return f.read_text(encoding="utf-8")
            except OSError:
                pass
        from ..observability import reporter
        reporter.emit({"type": "agent_start", "role": f"segment_{position + 1}_writer"})
        t0 = time.time()
        text = write_segment_v2(plans[position], plans, state, brief, llm)
        reporter.emit({"type": "agent_done", "role": f"segment_{position + 1}_writer",
                       "duration": time.time() - t0, "out_len": len(text or "")})
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
                print(f"\n[分段撰写] 正在写第 {position + 1}/{total} 段提示词（{plan.start_s}-{plan.end_s}s）……", flush=True)
            text = take(position)
            if text is None:
                print(f"[警告] 第 {position + 1} 段生成失败，跳过。")
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
                ):
                    # 硬阻断（issue #13）：错误的交接物不得被当成下一段的事实喂下去。
                    # 进度也**不得**落盘——否则被拦下的这一段的锚会被续跑直接跳过。
                    return False
            # 闸门通过后才落盘进度：被拦下的段不能被记成已完成
            progress_path.write_text(
                json.dumps({"done": position + 1, "total": total}, ensure_ascii=False, indent=2),
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
    # 放在宣布完成**之前**：没通过就不算跑完，会话保持「分段进行中」。
    if not _verify_last_frame(state, total - 1, session.directory,
                              end_hook=plans[-1].end_hook,
                              segment_number=total, total=total,
                              expected_seconds=float(plans[-1].duration_s)):
        return False
    print(f"\n✓ 全部 {total} 段已人工确认完成。可在剪辑工具中按顺序拼接 segments/ 下的各段输出。")
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

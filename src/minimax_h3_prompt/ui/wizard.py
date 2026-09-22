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
    """该主题的会话目录：``<sessions_root>/<topic_slug>/GEN001``（幂等，已存在则复用）。"""
    directory = Path(config.sessions_root) / _topic_slug(topic) / "GEN001"
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
    save_session(generation_dir, brief, final_state, status=STATUS_COMPLETED)
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
        print(f"\n✓ 完整提示词已写入：{output_file}（供存档，长视频请按下方分段执行）")
        _run_segmented_flow(brief, session, prompt, summary=summary_obj)
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

def _run_segmented_flow(brief: Brief, session: SessionState, prompt: str, summary=None) -> None:
    """把整条提示词按 [Shot N] 拆成单镜头提示词，逐段陪跑。

    循环规则：先尽量用 LLM 把本段 soundscape/music 按时间窗重写成独立提示词（
    失败自动回退机械拆分并明示）；显示信息卡（段号/Shot/时长/文件路径）→ 人工复制
    到 H3 生成 → 回车确认 → 才显示下一段；第 2 段起可提交上一段视频路径，自动剥尾帧
    作为本段首帧图；中断后由 progress.json 续接。
    """
    import json

    from ..model_factory import build_chat_model
    from ..segment_planner import plan_segments
    from ..segment_prompts import rewrite_segment_prompt, split_shots_from_prompt, write_segment_v2
    from ..tools.frame_auditor import extract_last_frame

    # ① 先走新流程：segment_planner 规划整秒分段边界 → 每段独立细写（官方英文格式，spec 2026-09-22）
    state = session.stage_state
    plans = None
    try:
        llm = build_chat_model()
        if state.get("shot_table"):
            with _progress_scope("[分段规划] 正在按 4-10s 整数边界切分剧情并锁定衔接……"):
                plans = plan_segments(str(state.get("shot_table", "")), brief.duration, llm)
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
        return

    seg_dir = session.directory / "segments"
    seg_dir.mkdir(parents=True, exist_ok=True)
    progress_path = seg_dir / "progress.json"
    done = 0
    if progress_path.exists():
        try:
            done = int(json.loads(progress_path.read_text(encoding="utf-8")).get("done", 0))
        except (OSError, json.JSONDecodeError, ValueError):
            done = 0
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
        # ① 每段重写：soundscape/music 为本段重写英文摘要句（官方 §4.6/§4.7）；失败回退机械拆分并明示
        rewritten: str | None = None
        if rewrite_llm is not None:
            rewritten = rewrite_segment_prompt(segment, prompt, rewrite_llm)
        display_text = rewritten or segment.text
        fallback_label = "" if rewritten else "[回退] 声音描述保持整条原样（未按段重写）"

        # ② 桥接帧：剥上一段尾帧，输出显式落盘信息（路径 + 大小）
        if position > 0 and _confirm("是否用上一段视频的尾帧作为本段首帧图（保证画面连续）？", default=True):
            video_raw = _clean_path_input(_prompt("上一段输出视频路径（回车=跳过）："))
            if video_raw:
                try:
                    frame = extract_last_frame(
                        Path(video_raw),
                        session.directory / "bridge_frames" / f"shot-{position + 1:02d}-start.png",
                    )
                    size_kb = frame.stat().st_size / 1024
                    print("[桥接帧] ✓ 已从上一段视频提取尾帧：")
                    print(f"  位置：{frame}")
                    print(f"  大小：{size_kb:.0f} KB")
                    print("  → 请在 H3 中把这张图设为本段的 first frame 输入。")
                except Exception as exc:  # noqa: BLE001 - 剥帧失败不阻塞人工流程
                    print(f"[警告] 剥尾帧失败（{exc}），可手动截图作为首帧图。")

        duration_line = (
            f"{segment.duration_seconds:g}s"
            if segment.duration_seconds is not None else "未知时长"
        )
        window_line = (
            f"（全局时间窗：{segment.start_seconds:g}s 起）"
            if segment.start_seconds is not None else ""
        )
        (seg_dir / f"shot-{position + 1:02d}.md").write_text(display_text, encoding="utf-8")
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
            raw = _prompt("本段生成并检查满意后回车进入下一段；输入 r 重显完整提示词，s 重看中文摘要：").lower()
            if raw in ("", "y", "yes"):
                break
            if raw == "r":
                print(display_text)
                continue
            if raw == "s" and summary is not None:
                from ..summary import render_summary_zh

                print(render_summary_zh(summary, shot_number=segment.shot_number))
                continue
            print("回车=完成本段，r=重显完整提示词，s=重看中文摘要。")
        progress_path.write_text(
            json.dumps({"done": position + 1, "total": total}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        if position + 1 < total:
            print(f"[完成] 第 {position + 1}/{total} 段。下面给出第 {position + 2} 段……")
    print(f"\n✓ 全部 {total} 段已人工确认完成。可在剪辑工具中按顺序拼接 segments/ 下的各段输出。")


def _run_segmented_flow_v2(brief: Brief, session: SessionState, plans: list, state: dict, llm, summary=None) -> None:
    """v2 陪跑：边生成边展示——当前段提示词一写完立即交付，后台线程同时写下一级。

    用户看第 N 段提示词、去 ComfyUI 生成、贴回尾帧，这段时间里后台已经把第 N+1 段写好；
    回车进入下一段时若已写完直接展示，否则等待。segments/shot-NN.md 逐段落盘，
    progress.json 支持中断续跑（已写入文件的段不重写）。
    """
    import json
    import threading
    import time

    from ..segment_prompts import write_segment_v2
    from ..tools.frame_auditor import extract_last_frame

    seg_dir = session.directory / "segments"
    seg_dir.mkdir(parents=True, exist_ok=True)
    progress_path = seg_dir / "progress.json"
    plan_cache = seg_dir / "plan.json"
    plan_cache.write_text(
        json.dumps([p.to_dict() for p in plans], ensure_ascii=False, indent=2), encoding="utf-8"
    )

    done = 0
    if progress_path.exists():
        try:
            done = int(json.loads(progress_path.read_text(encoding="utf-8")).get("done", 0))
        except (OSError, json.JSONDecodeError, ValueError):
            done = 0
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

    # 后台预取：当前段展示期间，下一级在后台写
    prefetch: dict = {}

    def start_prefetch(position: int) -> None:
        if position >= total or position in prefetch:
            return

        def work() -> None:
            prefetch[position] = generate(position)

        threading.Thread(target=work, daemon=True).start()

    def take(position: int) -> str | None:
        if position not in prefetch:
            start_prefetch(position)  # 没预取过就现在跑（理论上不会到这，除非首段被跳过）
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
            # 立即预取下一级
            start_prefetch(position + 1)
            print(f"\n✓ 第 {position + 1}/{total} 段提示词已生成，可立即复制到 H3 生成视频。", flush=True)
            if position + 1 < total:
                print(f"  （后台正在同时撰写第 {position + 2} 段提示词）", flush=True)

            # 桥接帧：剥上一段尾帧作为本段首帧
            if position > 0 and _confirm("是否用上一段视频的尾帧作为本段首帧图（保证画面连续）？", default=True):
                video_raw = _clean_path_input(_prompt("上一段输出视频路径（回车=跳过）："))
                if video_raw:
                    try:
                        frame = extract_last_frame(
                            Path(video_raw),
                            session.directory / "bridge_frames" / f"shot-{position + 1:02d}-start.png",
                        )
                        size_kb = frame.stat().st_size / 1024
                        print("[桥接帧] ✓ 已从上一段视频提取尾帧：")
                        print(f"  位置：{frame}")
                        print(f"  大小：{size_kb:.0f} KB")
                        print("  → 请在 H3 中把这张图设为本段的 first frame 输入。")
                    except Exception as exc:  # noqa: BLE001
                        print(f"[警告] 剥尾帧失败（{exc}），可手动截图作为首帧图。")

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
                raw = _prompt("本段生成并检查满意后回车进入下一段；输入 r 重显提示词：").lower()
                if raw in ("", "y", "yes"):
                    break
                if raw == "r":
                    print(text)
                    continue
                print("回车=完成本段，r=重显提示词。")
            progress_path.write_text(
                json.dumps({"done": position + 1, "total": total}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            if position + 1 < total:
                print(f"[完成] 第 {position + 1}/{total} 段。")
    finally:
        reporter.unsubscribe(loader)
    print(f"\n✓ 全部 {total} 段已人工确认完成。可在剪辑工具中按顺序拼接 segments/ 下的各段输出。")


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

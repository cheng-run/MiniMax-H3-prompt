"""两阶段生产流向导。

阶段 1：主题/时长/风格 → 管线跑到声音设计 → 首尾帧生图提示词落进资产库 → 人工确认/意见循环。
阶段 2：提交真实帧图片（缺帧自动降级 I2VA/L2VA/T2VA）→ 复制入库 → qwen3.7-plus 读图
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
from ..project_store import ProjectStore
from ..session_store import (
    STATUS_AWAITING_FRAMES,
    STATUS_COMPLETED,
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
    resumed = session is not None
    if session is None:
        session = _phase1_new(config)
        if session is None:
            return 1
    # 显式门禁：阶段 1 刚完成时不直通阶段 2（用户需先去 ComfyUI 出图）；
    # 续接路径不加门禁——主动选续接就是要进阶段 2。
    if not resumed and not _confirm(
        "是否立即继续阶段 2 提交关键帧图片？（通常需先复制生图提示词到 ComfyUI 出图）",
        default=False,
    ):
        print("已暂停。生成好图片后重新运行 `uv run launch.py` 选择续接即可进入阶段 2。")
        return 0
    return _phase2_collect_and_finish(config, session)


# ---------------------------------------------------------------------------
# 恢复入口
# ---------------------------------------------------------------------------

def _offer_resume(config: Config) -> SessionState | None:
    sessions = find_awaiting_sessions(config.assets_root)
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
    from ..project_generation import topic_slug

    store = ProjectStore(config.assets_root)
    resolved_topic_id = topic_slug(topic)
    project_id = store_next_project_id(store, resolved_topic_id)
    document = store.init_project(
        resolved_topic_id,
        project_id,
        topic[:120],
        duration_seconds=duration,
        variant=variant,
        global_style=style,
    )
    generation_dir = document.directory / "generations" / "GEN001"

    with _progress_scope("[阶段 1] 正在生成剧本、设计与首尾帧生图提示词……（预计几分钟，期间无需输入）"):
        state, model, agents = run_stage1(brief, config, on_node=_report_node)
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

    print("\n阶段 1 完成。请复制上面的生图提示词到 ComfyUI（Z-Image/Flux.2）生成图片。")
    print("可以关闭本窗口；生成好图片后重新运行 `uv run launch.py` 选择续接即可进入阶段 2。")
    return load_session(generation_dir)


def store_next_project_id(store: ProjectStore, topic_id: str) -> str:
    """在主题目录下分配下一个 project-NNN。"""
    root = store.root / topic_id / "projects"
    if not root.exists():
        return "project-001"
    numbers = []
    for path in root.iterdir():
        if path.is_dir() and path.name.startswith("project-"):
            try:
                numbers.append(int(path.name.rsplit("-", 1)[1]))
            except ValueError:
                continue
    return f"project-{max(numbers, default=0) + 1:03d}"


def _show_frame_prompts(state: dict, generation_dir: Path) -> None:
    bundle = state.get("fl2va_prompt_bundle")
    print("\n" + "=" * 60)
    print(f"资产库目录：{generation_dir}")
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
    if not rows:
        return ""
    title = "首帧" if frame == "first" else "尾帧"
    variant = "FL2VA" if (bundle.first and bundle.last) else "I2VA" if bundle.first else "L2VA"
    parts = [f"# {variant} {title}生图提示词", ""]
    for item in rows:
        model_title = "Z-Image" if item.model_family == "zimage" else "Flux.2"
        parts.extend([
            f"## {model_title}", "",
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
        raw = _prompt(f"{label}图片路径（回车=跳过）：")
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
        from ..execution import copy_frame_image
        from ..tools.frame_auditor import audit_frame_images

        refs = []
        frame_slots = {role: picture for picture, role in _required_frames(effective)}
        if have_first:
            record = copy_frame_image(
                first_path, topic_id=_topic_dir_name(session), generation_id=generation_dir.name,
                role="first", assets_root=config.assets_root,
            )
            frame_records.append(record)
            refs.append(RefItem(picture=frame_slots.get("first", 1), name="首帧", description="", path=str(first_path)))
        if have_last:
            record = copy_frame_image(
                last_path, topic_id=_topic_dir_name(session), generation_id=generation_dir.name,
                role="last", assets_root=config.assets_root,
            )
            frame_records.append(record)
            refs.append(RefItem(picture=frame_slots.get("last", 2), name="尾帧", description="", path=str(last_path)))
        def report_frame(frame_role: str) -> None:
            print(f"正在读取{frame_role}的实际画面……", flush=True)

        with _progress_scope("qwen3.7-plus 正在读取关键帧实际画面……"):
            audits = audit_frame_images(refs, brief.variant, on_frame=report_frame)
        descriptions = [a.to_dict() for a in audits]
        for audit in audits:
            label = "第一帧" if audit.role == "first" else "最后一帧"
            print(f"\n[{label}实际画面] {audit.description}")

    state["fl2va_frame_descriptions"] = descriptions
    state["frame_images"] = frame_records

    with _progress_scope("[阶段 2] 正在组装最终视频提示词并进行质检精修……（预计几分钟，期间无需输入）"):
        final_state, prompt = run_stage2(state, brief, config, on_node=_report_node)
    _drain_stdin()
    save_session(generation_dir, brief, final_state, status=STATUS_COMPLETED)
    if downgraded:
        session_downgraded = downgraded
        _record_downgrade(generation_dir, session_downgraded)

    output_file = generation_dir / "video-prompt.md"
    output_file.write_text(prompt, encoding="utf-8")
    print("\n" + "=" * 60)
    print(prompt)
    print("=" * 60)
    print(f"\n✓ 最终视频提示词已写入：{output_file}")
    return 0


def _topic_dir_name(session: SessionState) -> str:
    """从 generation 目录路径提取 topic_id（Assets/<topic>/projects/<pid>/generations/<gen>）。"""
    return session.directory.parents[3].name


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

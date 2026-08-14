"""交互菜单（questionary）：主菜单 → brief 选择 → 参数收集 → 运行 → 结果循环。"""
from __future__ import annotations

from pathlib import Path

import questionary
from rich.console import Console
from rich.panel import Panel

from ..brief_parser import Brief, min_refs, parse_brief
from ..config import PROJECT_ROOT, config
from ..graph.pipeline import run_pipeline
from ..observability import token_meter
from .progress import LiveProgress

console = Console()


def choose_model() -> str:
    """返回 'i2va' | 'l2va' | 'fl2va' | 'ref2va' | 'polish' | 'dry-run' | 'exit'。"""
    choice = questionary.select(
        "选择出片方式（对应你的 ComfyUI 工作流和参考输入）",
        choices=[
            "① 首帧生成视频（工作流 62 · 1 张首帧图）",
            "② 尾帧生成视频（工作流 62 · 1 张尾帧图）",
            "③ 首尾帧生成视频（工作流 62 · 首帧+尾帧）",
            "④ 多参考图生成视频（工作流 63 · 角色/场景多张）",
            "⑤ 润色已有草稿（polish）",
            "⑥ 仅解析 brief 自检（dry-run）",
            "⑦ 退出",
        ],
    ).ask()
    if choice is None:  # 用户取消 / 非交互终端
        raise SystemExit("已取消。")
    return {
        "① 首帧生成视频（工作流 62 · 1 张首帧图）": "i2va",
        "② 尾帧生成视频（工作流 62 · 1 张尾帧图）": "l2va",
        "③ 首尾帧生成视频（工作流 62 · 首帧+尾帧）": "fl2va",
        "④ 多参考图生成视频（工作流 63 · 角色/场景多张）": "ref2va",
        "⑤ 润色已有草稿（polish）": "polish",
        "⑥ 仅解析 brief 自检（dry-run）": "dry-run",
        "⑦ 退出": "exit",
    }[choice]


def choose_brief() -> Path:
    examples = sorted((PROJECT_ROOT / "examples").glob("*.md"))
    choices = [f"examples/{p.name}" for p in examples] + ["输入自定义路径…"]
    sel = questionary.select("选择 brief 文件（创意输入）", choices=choices).ask()
    if sel is None:
        raise SystemExit("已取消。")
    if sel.startswith("输入自定义"):
        p = questionary.text("输入 brief 文件路径（相对项目根或绝对路径）：").ask()
        if not p:
            raise SystemExit("未输入路径，已取消。")
    else:
        p = sel
    path = Path(p)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    if not path.exists():
        raise FileNotFoundError(f"brief 文件不存在：{path}")
    return path


def collect_params(brief: Brief) -> Brief:
    """带默认值收集可选参数（回车跳过）。变体由所选模型决定，不再单独问。"""
    dur = questionary.text(f"时长（秒，回车用默认 {brief.duration:.0f}）：").ask()
    if dur and dur.strip():
        try:
            brief.duration = float(dur)
        except ValueError:
            pass
    style = questionary.text(f"风格（回车用默认 {brief.style}）：").ask()
    if style and style.strip():
        brief.style = style
    lang = questionary.text(f"语言（回车用默认 {brief.language}）：").ask()
    if lang and lang.strip():
        brief.language = lang
    return brief


def show_brief(brief: Brief) -> None:
    refs = "\n".join(
        f"  <Picture {r.picture}> {r.name} — {r.description[:40]}" for r in brief.refs
    ) or "  （无）"
    lines = [
        f"模式: {brief.mode}" + (f" / 变体: {brief.variant}" if brief.mode == "base" else ""),
        f"时长: {brief.duration:.0f}s | 风格: {brief.style} | 语言: {brief.language}",
        f"剧情: {brief.plot[:100]}{'…' if len(brief.plot) > 100 else ''}",
        f"参考图:\n{refs}" if brief.mode == "ref" else "",
        f"草稿: {brief.draft[:60]}{'…' if len(brief.draft) > 60 else ''}" if brief.draft else "",
    ]
    console.print(Panel("\n".join(x for x in lines if x), title="Brief 摘要", border_style="magenta"))


def run_interactive() -> None:
    """统一入口：交互菜单循环。"""
    while True:
        model = choose_model()
        if model == "exit":
            console.print("再见 👋")
            return

        path = choose_brief()
        brief = parse_brief(path)
        if model in ("i2va", "l2va", "fl2va"):  # 工作流 62 的三种帧输入
            brief.mode = "base"
            brief.variant = model.upper()
        elif model == "ref2va":
            brief.mode = "ref"
        if model == "polish" and not brief.draft:
            console.print("[yellow]⚠ 该 brief 没有「草稿提示词」段落，将按全流程生成。[/]")
        need = min_refs(brief)
        if need and len(brief.refs) < need:
            console.print(f"[red]⚠ 该输入方式最少需要 {need} 张参考图，当前只有 {len(brief.refs)} 张。[/]")
            if not questionary.confirm("继续？（缺参考图可能效果不佳）", default=False).ask():
                continue

        collect_params(brief)
        show_brief(brief)

        # 参考图审核：brief 里带图片路径则用 qwen3.7-plus 读图，人工确认描述
        if any(r.path for r in brief.refs):
            if questionary.confirm("用 qwen3.7-plus 审核参考图？", default=True).ask():
                try:
                    from ..tools.reference_auditor import audit_refs

                    for res in audit_refs(brief.refs):
                        r = next(x for x in brief.refs if x.picture == res["picture"])
                        console.print(f"[cyan]<Picture {res['picture']}> {res['name']}[/]（{res['path']}）")
                        console.print(f"模型认为：{res['draft']}")
                        new = questionary.text("确认/修改描述（回车沿用草稿）：", default=res["draft"]).ask()
                        if new and new.strip():
                            r.description = new.strip()
                    show_brief(brief)
                except Exception as e:
                    console.print(f"[red]参考图审核失败（不影响后续）：{e}[/]")

        if model == "dry-run":
            console.print(f"[green]✓ 解析通过，未运行管线。[/] 中间产物目录: {config.stages_path}")
            if not questionary.confirm("继续？", default=True).ask():
                return
            continue

        if not questionary.confirm("开始生成？", default=True).ask():
            continue

        progress = LiveProgress(
            title=f"MiniMax-H3 · {brief.variant or brief.mode.upper()} · {path.name}",
            console=console,
        )
        final_prompt = progress.run(lambda: run_pipeline(brief, config))

        config.output_path.parent.mkdir(parents=True, exist_ok=True)
        config.output_path.write_text(final_prompt, encoding="utf-8")
        console.print(Panel(final_prompt, title="最终 H3 提示词", border_style="green", expand=False))
        console.print(f"[green]✓ 已保存到 {config.output_path}[/]")
        console.print(f"[cyan]各阶段产物：{config.stages_path} | {token_meter.format()}[/]")

        if not questionary.confirm("继续生成下一个？", default=True).ask():
            return

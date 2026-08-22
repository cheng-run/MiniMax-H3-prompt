"""CLI 入口：无参数进交互菜单；带参数走非交互快路径。

统一入口：
  uv run minimax-h3-prompt             → 交互菜单（选择出片方式 / brief / 参数）
  uv run minimax-h3-prompt --brief …   → 非交互快路径（脚本 / 自动化用）
"""
from __future__ import annotations

import argparse
import json
import sys


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="minimax-h3-prompt",
        description="MiniMax-H3 多智能体提示词生成系统",
    )
    parser.add_argument("--brief", default=None, help="brief 文件路径（不填则进交互菜单）")
    parser.add_argument("--model", choices=["fl2va", "ref2va", "t2va", "i2va", "l2va"], default=None,
                        help="出片模型（fl2va=工作流62首尾帧 / ref2va=工作流63多参考）")
    parser.add_argument("--mode", choices=["ref", "base"], default=None, help="覆盖 brief 中的模式（旧写法）")
    parser.add_argument("--variant", choices=["T2VA", "I2VA", "FL2VA", "L2VA"], default=None, help="base 模式变体（旧写法）")
    parser.add_argument("--polish", action="store_true", help="polish 模式：润色 brief 中已有的草稿提示词")
    parser.add_argument("--dry-run", action="store_true", help="只解析 brief 不跑 LLM 管线（自检用）")

    project = parser.add_subparsers(dest="command")
    project_parser = project.add_parser("project", help="管理长视频项目文档（离线，不调用模型）")
    project_parser.add_argument("--root", default=r"D:\笔记\Assets", help="Assets 根目录")
    project_commands = project_parser.add_subparsers(dest="project_command", required=True)

    init_parser = project_commands.add_parser("init", help="初始化项目文档")
    init_parser.add_argument("--root", dest="root", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    init_parser.add_argument("--topic-id", required=True, help="主题 ID")
    init_parser.add_argument("--project-id", required=True, help="项目 ID")
    init_parser.add_argument("--title", required=True, help="项目标题")
    init_parser.add_argument("--duration", type=float, default=60.0, help="项目时长（秒）")
    init_parser.add_argument("--variant", choices=["T2VA", "I2VA", "FL2VA", "L2VA"], default="FL2VA")
    init_parser.add_argument("--global-style", default="", help="全局视觉风格")

    for name, help_text in (("validate", "校验项目文档"), ("show", "展示项目摘要")):
        command_parser = project_commands.add_parser(name, help=help_text)
        command_parser.add_argument("--root", dest="root", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
        command_parser.add_argument("--topic-id", required=True, help="主题 ID")
        command_parser.add_argument("--project-id", required=True, help="项目 ID")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.command == "project":
        return _run_project_command(args)

    if not args.brief:
        # 统一入口：无 --brief → 交互菜单
        from .ui.menu import run_interactive

        try:
            run_interactive()
        except (KeyboardInterrupt, EOFError):
            print("\n再见 👋")
        return 0

    return _run_cli(args)


def _run_project_command(args: argparse.Namespace) -> int:
    """执行离线项目命令；不调用 LLM、ComfyUI 或 output 扫描。"""
    from .project_store import ProjectStore

    store = ProjectStore(getattr(args, "root", r"D:\笔记\Assets"))
    if args.project_command == "init":
        document = store.init_project(
            args.topic_id,
            args.project_id,
            args.title,
            duration_seconds=args.duration,
            variant=args.variant,
            global_style=args.global_style,
        )
        print(json.dumps({"directory": str(document.directory), "project_id": document.project_id}, ensure_ascii=False, indent=2))
        return 0

    if args.project_command == "validate":
        issues = store.validate(args.topic_id, args.project_id)
        for issue in issues:
            print(f"[{issue.severity}] {issue.code}: {issue.message}")
        return 1 if any(issue.severity == "error" for issue in issues) else 0

    if args.project_command == "show":
        print(json.dumps(store.show(args.topic_id, args.project_id), ensure_ascii=False, indent=2))
        return 0

    raise ValueError(f"未知项目命令：{args.project_command}")


def _run_cli(args: argparse.Namespace) -> int:
    """非交互快路径：--brief 必填，直接跑。"""
    from .brief_parser import parse_brief
    from .config import config

    brief = parse_brief(args.brief)
    if args.model:  # 模型优先（fl2va → base/FL2VA；ref2va → ref）
        if args.model == "ref2va":
            brief.mode = "ref"
        else:
            brief.mode = "base"
            brief.variant = args.model.upper()
    if args.mode:
        brief.mode = args.mode
    if args.variant:
        brief.variant = args.variant
    if args.polish and not brief.draft:
        print("[提示] --polish 需要 brief 里有「草稿提示词」段落；当前没有草稿，按全流程生成。")

    print(f"模式: {brief.mode}" + (f" / 变体: {brief.variant}" if brief.mode == "base" else ""))
    print(f"时长: {brief.duration}s | 风格: {brief.style} | 语言: {brief.language}")
    print(f"剧情: {brief.plot[:80]}{'…' if len(brief.plot) > 80 else ''}")
    if brief.refs:
        print("参考图:")
        for r in brief.refs:
            print(f"  <Picture {r.picture}> {r.name} — {r.description[:60]}")
    if brief.draft:
        print(f"草稿: {brief.draft[:80]}{'…' if len(brief.draft) > 80 else ''}")

    if args.dry_run:
        print("\n[dry-run] 解析通过，未运行管线。")
        return 0

    from .graph.pipeline import run_pipeline

    final_prompt = run_pipeline(brief, config)
    config.output_path.parent.mkdir(parents=True, exist_ok=True)
    config.output_path.write_text(final_prompt, encoding="utf-8")
    print(f"\n已完成，最终提示词已写入: {config.output_path}")
    print("=" * 60)
    print(final_prompt)
    return 0


if __name__ == "__main__":
    sys.exit(main())

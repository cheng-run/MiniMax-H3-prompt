"""CLI 入口：无参数进交互菜单；带参数走非交互快路径。

统一入口：
  uv run minimax-h3-prompt             → 交互菜单（选择出片方式 / brief / 参数）
  uv run minimax-h3-prompt --brief …   → 非交互快路径（脚本 / 自动化用）
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


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

    plan_parser = project_commands.add_parser("plan", help="生成镜头的人工 ComfyUI 执行指引（只读）")
    plan_parser.add_argument("--root", dest="root", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    plan_parser.add_argument("--topic-id", required=True, help="主题 ID")
    plan_parser.add_argument("--project-id", required=True, help="项目 ID")
    plan_parser.add_argument("--shot", required=True, help="镜头 ID（如 SH001）")
    plan_parser.add_argument("--workflow-root", default=r"D:\Comfyui\ComfyUI\user\default\workflows", help="ComfyUI 工作流根目录")
    plan_parser.add_argument("--out", default=None, help="把指引写到文件（默认打印到 stdout）")

    import_parser = project_commands.add_parser("import-output", help="导入人工 ComfyUI 执行输出并标记任务 executed")
    import_parser.add_argument("--root", dest="root", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    import_parser.add_argument("--topic-id", required=True, help="主题 ID")
    import_parser.add_argument("--project-id", required=True, help="项目 ID")
    import_parser.add_argument("generation", help="generation_id（如 SH001-G001）")
    import_parser.add_argument("source", help="ComfyUI output 源文件路径")

    review_parser = project_commands.add_parser("review", help="人工视觉/听觉验收资产或镜头")
    review_parser.add_argument("--root", dest="root", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    review_parser.add_argument("--topic-id", required=True, help="主题 ID")
    review_parser.add_argument("--project-id", required=True, help="项目 ID")
    review_parser.add_argument("--entity", required=True, help="验收实体 ID（C01/S01/P01 资产或 SH001 镜头）")
    review_parser.add_argument("--kind", choices=["visual", "audio"], required=True, help="视觉或听觉验收")
    review_parser.add_argument("--outcome", choices=["approved", "rejected"], required=True, help="验收结论")
    review_parser.add_argument("--reviewer", default="", help="验收人标识")

    verify_parser = project_commands.add_parser("verify", help="一键检查执行资格闸门（只读）")
    verify_parser.add_argument("--root", dest="root", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    verify_parser.add_argument("--topic-id", required=True, help="主题 ID")
    verify_parser.add_argument("--project-id", required=True, help="项目 ID")
    verify_parser.add_argument("--shot", default=None, help="只检查指定镜头（默认全部）")
    verify_parser.add_argument("--workflow-root", default=r"D:\Comfyui\ComfyUI\user\default\workflows", help="ComfyUI 工作流根目录")

    profile_parser = project_commands.add_parser("profile", help="Profile 状态升级（verified/approved，写 docs/profiles）")
    profile_parser.add_argument("--root", dest="root", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    profile_parser.add_argument("--topic-id", required=True, help="主题 ID")
    profile_parser.add_argument("--project-id", required=True, help="项目 ID")
    profile_parser.add_argument("--profile", required=True, help="Profile ID（如 h3_fl2va_v2）")
    profile_parser.add_argument("--status", choices=["verified", "approved"], required=True, help="升级目标状态")
    profile_parser.add_argument("--reviewer", default="", help="验收人标识")
    profile_parser.add_argument("--note", default="", help="附加说明")
    profile_parser.add_argument("--generation", default="", help="关联的 generation_id（approved 时需要）")
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

    from .execution import (
        apply_review,
        build_execution_card,
        check_executability,
        import_generation,
        promote_profile,
        render_card,
    )

    topic, project = args.topic_id, args.project_id
    if args.project_command == "plan":
        card = build_execution_card(
            store, topic, project, args.shot, workflow_root=Path(args.workflow_root)
        )
        markdown = render_card(card)
        if args.out:
            Path(args.out).write_text(markdown, encoding="utf-8")
            print(f"执行指引已写入: {args.out}")
        else:
            print(markdown)
        return 0

    if args.project_command == "import-output":
        result = import_generation(store, topic, project, args.generation, args.source)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    if args.project_command == "review":
        result = apply_review(store, topic, project, args.entity, args.kind, args.outcome, reviewer=args.reviewer)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    if args.project_command == "verify":
        result = check_executability(
            store, topic, project, shot_id=args.shot, workflow_root=Path(args.workflow_root)
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        shots_ok = all(shot["can_execute"] for shot in result["shots"])
        documents_ok = not any(issue["severity"] == "error" for issue in result["document_issues"])
        return 0 if shots_ok and documents_ok else 1

    if args.project_command == "profile":
        result = promote_profile(
            store, topic, project, args.profile, args.status,
            reviewer=args.reviewer, note=args.note, generation_id=args.generation,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
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

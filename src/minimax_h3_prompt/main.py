"""由 ``launch.py`` 调用的 CLI 实现；项目唯一启动入口是 ``uv run launch.py``。

``main()`` 保留为内部可测试函数，不作为独立命令注册。
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="launch.py",
        description="MiniMax-H3 多智能体提示词生成系统",
    )
    parser.add_argument("--brief", default=None, help="brief 文件路径（不填则进交互菜单）")
    parser.add_argument("--model", choices=["fl2va"], default=None,
                        help="出片模型（当前产品流程固定使用 FL2VA 首尾帧）")
    parser.add_argument("--mode", choices=["base"], default=None, help="当前产品固定使用 base/FL2VA")
    parser.add_argument("--variant", choices=["I2VA", "L2VA", "FL2VA"], default=None, help="视频生成方式（首帧 I2VA / 尾帧 L2VA / 首尾帧 FL2VA，缺省按 brief 声明或默认 FL2VA）")
    parser.add_argument("--polish", action="store_true", help="polish 模式：润色 brief 中已有的草稿提示词")
    parser.add_argument("--dry-run", action="store_true", help="只解析 brief 不跑 LLM 管线（自检用）")

    project = parser.add_subparsers(dest="command")
    create_parser = project.add_parser("create-video", help="输入主题，自动生成剧本与视频/关键帧提示词")
    create_parser.add_argument("--topic", required=True, help="视频主题")
    create_parser.add_argument("--root", default=r"D:\\笔记\\Assets", help="Assets 根目录")
    create_parser.add_argument("--duration", type=float, default=None, help="视频时长（秒，默认使用配置）")
    create_parser.add_argument("--style", default=None, help="视觉风格（默认使用配置）")
    create_parser.add_argument("--language", default=None, help="提示词语言（默认使用配置）")
    create_parser.add_argument("--variant", choices=["I2VA", "L2VA", "FL2VA"], default="FL2VA")

    project_parser = project.add_parser("project", help="管理长视频项目文档（离线，不调用模型）")
    project_parser.add_argument("--root", default=r"D:\笔记\Assets", help="Assets 根目录")
    project_commands = project_parser.add_subparsers(dest="project_command", required=True)

    init_parser = project_commands.add_parser("init", help="初始化项目文档")
    init_parser.add_argument("--root", dest="root", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    init_parser.add_argument("--topic-id", required=True, help="主题 ID")
    init_parser.add_argument("--project-id", required=True, help="项目 ID")
    init_parser.add_argument("--title", required=True, help="项目标题")
    init_parser.add_argument("--duration", type=float, default=60.0, help="项目时长（秒）")
    init_parser.add_argument("--variant", choices=["FL2VA"], default="FL2VA")
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

    generate_parser = project_commands.add_parser("generate-prompts", help="从 brief 生成剧本与视频/关键帧提示词并保存到项目")
    generate_parser.add_argument("--root", dest="root", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    generate_parser.add_argument("--topic-id", required=True, help="主题 ID")
    generate_parser.add_argument("--project-id", required=True, help="项目 ID")
    generate_parser.add_argument("--brief", required=True, help="brief 文件路径")
    generate_parser.add_argument("--generation-id", default=None, help="指定生成 ID（默认自动分配）")
    generate_parser.add_argument("--model", choices=["fl2va"], default=None)
    generate_parser.add_argument("--mode", choices=["base"], default=None)
    generate_parser.add_argument("--variant", choices=["I2VA", "L2VA", "FL2VA"], default=None)
    generate_parser.add_argument("--dry-run", action="store_true", help="只解析 brief，不调用模型或落盘")
    generate_parser.add_argument("--overwrite", action="store_true", help="允许覆盖同一 generation_id")

    show_parser = project_commands.add_parser("show-prompts", help="查看项目中的剧本与提示词")
    show_parser.add_argument("--root", dest="root", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    show_parser.add_argument("--topic-id", required=True, help="主题 ID")
    show_parser.add_argument("--project-id", required=True, help="项目 ID")
    show_parser.add_argument("--generation", required=True, help="generation_id")
    show_parser.add_argument("--kind", choices=["script", "video", "fl2va", "first-frame", "last-frame"], default=None)
    show_parser.add_argument("--raw", action="store_true", help="输出正文，供手动复制")
    return parser


def _topic_progress(event: dict) -> None:
    """把主题单入口的模型调用进度显示给最终用户。"""
    event_type = event.get("type")
    role = str(event.get("role", "模型")).removeprefix("role_")
    if event_type == "agent_start":
        print(f"[进行中] {role} 正在处理……", flush=True)
    elif event_type == "agent_done":
        duration = event.get("duration", 0.0)
        print(f"[完成] {role}（{duration:.1f}s）", flush=True)


def _create_video_with_progress(topic: str, config, **kwargs):
    """运行主题生成，并确保异常或中断时解除进度订阅。"""
    from .observability import reporter
    from .project_generation import create_video_from_topic

    reporter.subscribe(_topic_progress)
    try:
        return create_video_from_topic(topic, config, **kwargs)
    finally:
        reporter.unsubscribe(_topic_progress)


def _print_generation_result(result, directory) -> None:
    print(f"\n项目已创建：{result.project_id}")
    print(f"生成结果目录：{directory}")
    print("\n剧本：\n" + result.script)
    if result.fl2va_prompt_bundle is not None:
        from .generation import render_fl2va_frame_markdown
        print(f"\n{'=' * 20} FL2VA 首帧提示词 {'=' * 20}\n{render_fl2va_frame_markdown(result, 'first')}")
        print(f"\n{'=' * 20} FL2VA 尾帧提示词 {'=' * 20}\n{render_fl2va_frame_markdown(result, 'last')}")
        print(f"\nFL2VA 汇总文件：{directory / 'fl2va-prompt.md'}")
        print(f"首帧文件：{directory / 'first-frame-prompt.md'}")
        print(f"尾帧文件：{directory / 'last-frame-prompt.md'}")
        print("请将首帧/尾帧图片人工绑定到 FL2VA 的 first_frame/last_frame 输入槽；项目不会自动运行 ComfyUI。")
    else:
        for kind, label in (("character", "人物提示词"), ("prop", "道具提示词"), ("scene", "场景提示词"), ("video", "视频剧情提示词")):
            print(f"\n{'=' * 20} {label} {'=' * 20}\n{result.artifact(kind).content}")
    print("\n以上内容已保存；请手动复制到对应模型并自行审查生成结果。")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.command == "create-video":
        from .config import config
        from .generation import render_generation
        from .project_generation import create_video_from_topic

        result, directory = _create_video_with_progress(
            args.topic, config, root=args.root, duration=args.duration,
            style=args.style, language=args.language, variant=args.variant,
        )
        _print_generation_result(result, directory)
        return 0

    if args.command == "project":
        return _run_project_command(args)

    if not args.brief:
        # 面向最终用户的两阶段向导：先生图提示词 → 人工生图 → 提交图片 → 视频提示词。
        from .config import config
        from .ui.wizard import run_wizard

        try:
            return run_wizard(config)
        except (KeyboardInterrupt, EOFError):
            print("\n已取消。")
            return 1

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

    if args.project_command == "generate-prompts":
        from .brief_parser import parse_brief
        from .graph.pipeline import run_pipeline_structured

        brief = parse_brief(args.brief)
        # 产品主流程固定 base 模式；生成方式默认 FL2VA，允许 brief 声明或 --variant 显式指定。
        brief.mode = "base"
        if args.variant:
            brief.variant = args.variant.upper()
        elif brief.variant == "T2VA":
            brief.variant = "FL2VA"
        if args.model and args.model != "fl2va":
            raise ValueError("当前产品只支持 fl2va 模型")
        if args.mode and args.mode != "base":
            raise ValueError("当前产品只支持 base 模式")
        if args.dry_run:
            print(json.dumps({"topic_id": args.topic_id, "project_id": args.project_id, "brief": {"mode": brief.mode, "variant": brief.variant, "duration": brief.duration, "style": brief.style, "language": brief.language, "plot": brief.plot}, "dry_run": True}, ensure_ascii=False, indent=2))
            return 0
        from .config import config
        existing = store.list_generation_results(args.topic_id, args.project_id)
        generation_id = args.generation_id or f"GEN{len(existing) + 1:03d}"
        # brief 参考图行带 (图片路径) 时：先读图，真实画面描述注入管线（阶段 2 锚定）。
        frame_descriptions = []
        if any(ref.path for ref in brief.refs):
            try:
                audits = audit_frame_images(brief.refs, brief.variant)
                frame_descriptions = [a.to_dict() for a in audits]
                if frame_descriptions:
                    print(f"[读图] 已读取 {len(frame_descriptions)} 张关键帧图片（qwen3.7-plus）。")
            except FileNotFoundError as exc:
                print(f"[警告] 关键帧图片读取失败，回退生图提示词锚定：{exc}")
            except Exception as exc:  # noqa: BLE001 - 读图失败不阻塞生成
                print(f"[警告] 读图服务异常，回退生图提示词锚定：{exc}")
        result = run_pipeline_structured(
            brief, config, generation_id=generation_id,
            topic_id=args.topic_id, project_id=args.project_id,
            frame_descriptions=frame_descriptions,
        )
        directory = store.save_generation_result(args.topic_id, args.project_id, result, overwrite=args.overwrite)
        if result.fl2va_prompt_bundle is not None:
            names = ["script.md", "video-prompt.md", "fl2va-prompt.md"]
            if result.fl2va_prompt_bundle.first:
                names.append("first-frame-prompt.md")
            if result.fl2va_prompt_bundle.last:
                names.append("last-frame-prompt.md")
        else:
            names = ["script.md", "video-prompt.md", "character-prompt.md", "prop-prompt.md", "scene-prompt.md"]
        print(json.dumps({"generation_id": generation_id, "directory": str(directory), "artifacts": [str(directory / name) for name in names]}, ensure_ascii=False, indent=2))
        return 0

    if args.project_command == "show-prompts":
        print(json.dumps(store.show_generation(args.topic_id, args.project_id, args.generation, kind=args.kind, raw=args.raw), ensure_ascii=False, indent=2))
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
    # 产品主流程固定 base 模式；生成方式默认 FL2VA，允许 brief 声明或 --variant 显式指定。
    brief.mode = "base"
    if args.variant:
        brief.variant = args.variant.upper()
    elif brief.variant == "T2VA":
        brief.variant = "FL2VA"
    if args.model and args.model != "fl2va":
        raise ValueError("当前产品只支持 fl2va 模型")
    if args.mode and args.mode != "base":
        raise ValueError("当前产品只支持 base 模式")
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

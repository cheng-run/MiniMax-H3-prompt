"""由 ``launch.py`` 调用的 CLI 实现；项目唯一启动入口是 ``uv run launch.py``。

``main()`` 保留为内部可测试函数，不作为独立命令注册。

资产库（Assets 主题 / 项目文档 / Workflow Profile / 分段执行闭环）已整体移除，
本 CLI 只保留面向用户的两阶段向导与 brief 快路径，以及 generate-prompts 入口。
"""
from __future__ import annotations

import argparse
import json


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
    create_parser.add_argument("--duration", type=float, default=None, help="视频时长（秒，默认使用配置）")
    create_parser.add_argument("--style", default=None, help="视觉风格（默认使用配置）")
    create_parser.add_argument("--language", default=None, help="提示词语言（默认使用配置）")
    create_parser.add_argument("--variant", choices=["I2VA", "L2VA", "FL2VA"], default="FL2VA")

    generate_parser = project.add_parser("generate-prompts", help="从 brief 生成剧本与视频/关键帧提示词")
    generate_parser.add_argument("--brief", "-b", required=False, help="brief 文件路径")
    generate_parser.add_argument("--topic", default=None, help="视频主题")
    generate_parser.add_argument("--duration", type=float, default=None, help="视频时长（秒）")
    generate_parser.add_argument("--style", default=None, help="视觉风格")
    generate_parser.add_argument("--language", default=None, help="提示词语言")
    generate_parser.add_argument("--variant", choices=["I2VA", "L2VA", "FL2VA"], default="FL2VA")
    generate_parser.add_argument("--dry-run", action="store_true", help="只解析 brief，不调用模型或落盘")

    ledger_parser = project.add_parser("ledger", help="生成台账：从磁盘重建镜头链路")
    ledger_sub = ledger_parser.add_subparsers(dest="ledger_command")
    ledger_rebuild = ledger_sub.add_parser("rebuild", help="重扫并写出 ledger.json")
    ledger_rebuild.add_argument("session_dir", help="会话目录（含 bridge_frames/ 与 segments/）")
    ledger_rebuild.add_argument("--output-dir", action="append", required=True,
                                help="ComfyUI 输出目录，可重复")
    ledger_rebuild.add_argument("--threshold", type=float, default=5.0,
                                help="mad 阈值（默认 5.0）")
    ledger_rebuild.add_argument("--json", action="store_true", help="只输出 JSON，不写文件")
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
    from .topic_generation import create_video_from_topic

    reporter.subscribe(_topic_progress)
    try:
        return create_video_from_topic(topic, config, **kwargs)
    finally:
        reporter.unsubscribe(_topic_progress)


def _print_generation_result(result, directory) -> None:
    print(f"\n执行完成：{directory}")
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


def _run_create_video(args) -> int:
    from .config import config
    from .topic_generation import create_video_from_topic

    result, directory = _create_video_with_progress(
        args.topic, config, duration=args.duration,
        style=args.style, language=args.language, variant=args.variant,
    )
    _print_generation_result(result, directory)
    return 0


def _run_generate_prompts(args) -> int:
    from .brief_parser import parse_brief
    from .config import config
    from .topic_generation import create_video_from_topic

    if args.brief:
        brief = parse_brief(args.brief)
        brief.mode = "base"
        if args.variant:
            brief.variant = args.variant.upper()
        elif brief.variant == "T2VA":
            brief.variant = "FL2VA"
        if args.dry_run:
            print(json.dumps({
                "brief": {"mode": brief.mode, "variant": brief.variant, "duration": brief.duration,
                          "style": brief.style, "language": brief.language, "plot": brief.plot, "draft": brief.draft},
                "dry_run": True,
            }, ensure_ascii=False, indent=2))
            return 0
        result, directory = create_video_from_topic(
            brief.plot, config,
            duration=brief.duration, style=brief.style, language=brief.language, variant=brief.variant,
        )
        _print_generation_result(result, directory)
        return 0

    # 无 --brief：用主题直接跑（等价 create-video，统一一条入口）
    if not args.topic:
        raise ValueError("generate-prompts 需要 --brief 文件或 --topic 主题")
    result, directory = _create_video_with_progress(
        args.topic, config, duration=args.duration,
        style=args.style, language=args.language, variant=args.variant,
    )
    _print_generation_result(result, directory)
    return 0


def _run_ledger(args: argparse.Namespace) -> int:
    """从磁盘重建生成台账，只读输入，仅写会话目录下的 ledger.json。"""
    from pathlib import Path

    from .tools.ledger import rebuild

    session_dir = Path(args.session_dir)
    if not session_dir.is_dir():
        print(f"[错误] 会话目录不存在：{session_dir}")
        return 1
    if getattr(args, "ledger_command", None) != "rebuild":
        print("[错误] 请指定子命令，例如：ledger rebuild <session_dir> --output-dir <dir>")
        return 1

    write = not args.json
    ledger = rebuild(session_dir, [Path(d) for d in args.output_dir],
                     threshold=args.threshold, write=write)
    if args.json:
        print(json.dumps(ledger.to_dict(), ensure_ascii=False, indent=2))
        return 0

    print(f"镜头 {len(ledger.shots)} 个：" + " → ".join(s.video for s in ledger.shots))
    for item in ledger.discarded:
        print(f"[废片] {item.video}（{item.confidence}）— {item.reason}")
    for item in ledger.interruptions:
        print(f"[中断] 第 {item.at_shot} 段未产出（{item.confidence}）— {item.hint}")
    for note in ledger.review_needed:
        print(f"[待确认] {note}")
    for note in ledger.warnings:
        print(f"[警告] {note}")
    print(f"已写出：{session_dir / 'ledger.json'}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.command == "create-video":
        return _run_create_video(args)
    if args.command == "generate-prompts":
        return _run_generate_prompts(args)
    if args.command == "ledger":
        return _run_ledger(args)

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


if __name__ == "__main__":
    raise SystemExit(main())
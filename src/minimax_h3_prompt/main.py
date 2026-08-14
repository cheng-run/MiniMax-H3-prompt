"""CLI 入口：无参数进交互菜单；带参数走非交互快路径。

统一入口：
  uv run minimax-h3-prompt             → 交互菜单（选择出片方式 / brief / 参数）
  uv run minimax-h3-prompt --brief …   → 非交互快路径（脚本 / 自动化用）
"""
from __future__ import annotations

import argparse
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
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if not args.brief:
        # 统一入口：无 --brief → 交互菜单
        from .ui.menu import run_interactive

        try:
            run_interactive()
        except (KeyboardInterrupt, EOFError):
            print("\n再见 👋")
        return 0

    return _run_cli(args)


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

#!/usr/bin/env python
"""#137 L5 单独一轮：用当前 main 的关键帧词，在 A 臂的状态上重出首帧词（本机一次性脚本，不入库）。

为什么不进片子：票面要求 B 臂同参考图（开场首帧 start.png 字节相同），B 臂自己的
首帧词没有机会生成图；而 L5 的判据（5-1 指代不自包含 / 5-2 通行几何 / 5-5 模型档
错配）看的正是那张图。这个脚本只在片子之外重出一份词——你按它生成一张图，
用来判 L5，不改变两臂的片子对照。

它不重跑阶段 1：直接把 A 臂会话里的 script / shot_table / 设计产物喂给当前 main 的
`fl2va_frame_prompts` 节点，所以两臂的首帧词面对的是同一份剧本与分镜。

用法（在工作树里、代码已切到当前 main 之后）：
    PYTHONPATH="$(pwd -W)/src" D:/项目/python/MiniMax-H3-prompt/.venv/Scripts/python.exe \
        ../../scripts/ab_l5_frame.py <A 会话目录> [--out <输出.md>]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description="L5 单独一轮：当前 main 的首帧词")
    parser.add_argument("session", help="A 臂会话目录（GENxxx）")
    parser.add_argument("--out", default="", help="输出 Markdown（默认 <会话目录>/l5-frame-prompts.md）")
    args = parser.parse_args()

    session_dir = Path(args.session).resolve()

    from minimax_h3_prompt.session_store import load_session

    session = load_session(session_dir)
    if session is None:
        raise SystemExit(f"[错误] 会话读不回来：{session_dir}")
    brief = session.brief
    if brief.director_mode != "fl2v":
        raise SystemExit(f"[错误] 这条会话不是 fl2v（{brief.director_mode}），本脚本不适用")

    state = dict(session.stage_state)
    state["brief"] = brief

    from minimax_h3_prompt.agents import build_role_agents
    from minimax_h3_prompt.graph.nodes import _make_fl2va_frame_prompt_node
    from minimax_h3_prompt.model_factory import build_chat_model

    model = build_chat_model()
    agents = build_role_agents(
        model, brief.refs, brief.mode, brief.duration, brief.variant)
    node = _make_fl2va_frame_prompt_node(agents)
    result = node(state)
    bundle_raw = result.get("fl2va_prompt_bundle")
    if not bundle_raw:
        raise SystemExit("[错误] 关键帧节点没有产出 bundle（看上面一行警告）")

    from minimax_h3_prompt.generation import FL2VAPromptBundle
    from minimax_h3_prompt.ui.wizard import render_fl2va_frame_markdown_from_bundle

    new_bundle = FL2VAPromptBundle.from_dict(bundle_raw)
    old_raw = session.stage_state.get("fl2va_prompt_bundle") or {}
    old_bundle = FL2VAPromptBundle.from_dict(old_raw) if old_raw else None

    parts = [
        "# L5 单独一轮：首帧词（A 旧词 vs 当前 main 新词）",
        "",
        "> 本轮只判 L5，不参与片子对照；两臂片子仍用同一张开场首帧。",
        "",
        "## A 臂（9fbcb6b，片子用的那张图就是按它生成的）",
        "",
        render_fl2va_frame_markdown_from_bundle(old_bundle, "first")
        if old_bundle else "（A 会话里没有首帧词）",
        "",
        "## B 臂（当前 main，只出词、不进片子）",
        "",
        render_fl2va_frame_markdown_from_bundle(new_bundle, "first"),
        "",
    ]
    text = "\n".join(parts)
    out = Path(args.out) if args.out else session_dir / "l5-frame-prompts.md"
    out.write_text(text, encoding="utf-8")
    print(text)
    print(f"\n[已写入] {out}")
    print("下一步：把上面 B 臂的首帧词贴进 ComfyUI 生图节点，生成后把图片路径发我"
          "（它与 A 的开场首帧并排判 L5）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())

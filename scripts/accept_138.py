#!/usr/bin/env python
"""#138 验收：只跑剧情链（编剧 → 分镜 → 规划），不跑片、不改代码。

用法：
    PYTHONIOENCODING=utf-8 PYTHONPATH=src .venv/Scripts/python.exe \
        scripts/accept_138.py

产物落 output/accept-138/：
- stage1/<节点名>.md      每个剧情链节点的原始产出（证据）
- script.md               分场剧本（state["script"]）
- storyboard.md           分镜表（state["shot_table"]）
- plan.json               规划器按分镜表首行体裁档出的段表
- plan-fallback.json      去掉 `形式：` 行后的规划（验「短叙事兜底」）
- run-summary.json        首行比对、段数、时长等机械事实

链路与外层向导（run_stage1）前 7 个节点相同；只跑到 storyboard 为止，
不跑首尾帧词 / 声音 / 阶段 2（票面「不跑片」）。
"""
from __future__ import annotations

import json
import sys
import traceback
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "output" / "accept-138"

# 与 _STAGE1_CHAIN 前段一致（producer → director → creative_rt → screenwriter
# → parallel_designers → art_director → storyboard）。分镜师要读 art_design，
# 所以设计节点必须在前。
CHAIN = [
    "producer",
    "director",
    "creative_rt",
    "screenwriter",
    "parallel_designers",
    "art_director",
    "storyboard",
]

PLOT = (
    "暴雨夜，一个外卖骑手骑车穿过积水街区，在便利店门口避雨，"
    "接到催单电话，最后把餐送到客户楼下并抬头看了一眼亮灯的窗户。"
)
STYLE = "电影感，电影运镜手法，写实，真实，符合现实人物环境"
CLARIFY = (
    "1. 暴雨发生在现代城市还是年代久远的街区，骑手和电动车的外观要不要保留明显的时代特征？"
    " → 年代久远的街区，保留\n（以上是用户在生成前给出的澄清结论，属于用户原始要求的一部分："
    "必须落实，不得当成无中生有删掉。）"
)


def _write(name: str, text: str) -> None:
    path = OUT / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    print(f"[落盘] {path}（{len(text)} 字）")


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)

    from minimax_h3_prompt.brief_parser import Brief
    from minimax_h3_prompt.config import config
    from minimax_h3_prompt.graph.pipeline import build_pipeline_graph
    from minimax_h3_prompt.model_factory import build_chat_model
    from minimax_h3_prompt.tools.ref_metadata import to_tuple

    brief = Brief(
        mode="director",
        variant="T2VA",
        director_mode="fl2v",
        director_continuity_mode="guide",
        duration=20.0,
        style=STYLE,
        language="Chinese",
        plot=PLOT,
        # 澄清不走 brief 文件解析（brief_parser.py:87-91 只由向导写入），这里按会话里
        # 同一字段原样给，等价于 A/B 那次向导跑出来的 brief。
        clarifications=CLARIFY,
    )
    _write("brief-source.md", "# 本次 brief（脚本内构造，字段与 #137 冻结 brief 相同）\n\n"
           f"- plot: {PLOT}\n- style: {STYLE}\n- duration: 20.0\n- director_mode: fl2v\n"
           f"- clarifications: {CLARIFY}\n")

    model = build_chat_model()
    graph, _agents = build_pipeline_graph(model, brief, config, stage=1, chain=CHAIN)
    initial = {
        "brief": brief,
        "mode": brief.mode,
        "variant": brief.variant,
        "director_mode": brief.director_mode,
        "duration": brief.duration,
        "ref_meta": to_tuple(brief.refs),
        "iterations": 0,
        "qa_issues": [],
    }
    state: dict = dict(initial)
    for chunk in graph.stream(initial, stream_mode="updates"):
        for node_name, update in chunk.items():
            if not update:
                print(f"[跳过] {node_name}（无产出）")
                continue
            state.update(update)
            print(f"[完成] {node_name}")
            for key, value in update.items():
                if isinstance(value, str) and value.strip():
                    _write(f"stage1/{node_name}.{key}.md", value)

    if "script" not in state or "shot_table" not in state:
        print("[错误] 剧情链没有产出分场剧本或分镜表，停止")
        return 1

    script = str(state["script"])
    shot_table = str(state["shot_table"])
    _write("script.md", script)
    _write("storyboard.md", shot_table)

    script_first = script.strip().splitlines()[0].strip() if script.strip() else ""
    board_lines = [ln for ln in shot_table.splitlines() if ln.strip()]
    board_first = board_lines[0].strip() if board_lines else ""

    # 规划：与 _run_director_flow 同一调用（movie 20s、fl2v；frame_context 为空＝
    # 没有真实关键帧读图结果，revision_context 为空＝没有用户累积修订）。
    from minimax_h3_prompt.director.planning import (
        plan_director_segments,
        render_plan_listing,
        write_plan_json,
    )

    llm = build_chat_model()

    print("\n[规划] 用分镜表原样规划（首行体裁档在场）")
    plan = plan_director_segments(shot_table, brief.duration, llm, mode="fl2v")
    write_plan_json(OUT / "plan.json", plan)
    listing = render_plan_listing(plan)
    _write("plan-listing.md", listing)
    print(listing)

    # 兜底验证：删掉 `形式：` 行再规划一次。票面 AC 要求「无声明时按短叙事兜底」。
    fallback_lines = [ln for ln in shot_table.splitlines()
                      if not ln.strip().startswith("形式：")]
    fallback_table = "\n".join(fallback_lines)
    _write("storyboard-no-form.md", fallback_table)
    print("\n[规划] 去掉 `形式：` 行后重新规划（验短叙事兜底）")
    plan_no_form = plan_director_segments(fallback_table, brief.duration, llm, mode="fl2v")
    write_plan_json(OUT / "plan-fallback.json", plan_no_form)
    _write("plan-fallback-listing.md", render_plan_listing(plan_no_form))
    print(render_plan_listing(plan_no_form))

    summary = {
        "script_first_line": script_first,
        "storyboard_first_line": board_first,
        "form_line_equal": script_first == board_first,
        "form_line_present": script_first.startswith("形式：") and board_first.startswith("形式："),
        "plan_segments": [
            {"index": s.index, "type": s.type, "start_s": s.start_s, "end_s": s.end_s,
             "declared_frames": s.declared_frames, "export_frames": s.export_frames,
             "summary": s.summary, "end_hook": s.end_hook}
            for s in plan.segments
        ],
        "plan_total_frames": plan.total_frames,
        "plan_seconds_total": plan.segments[-1].end_s,
        "fallback_segments": [
            {"index": s.index, "type": s.type, "start_s": s.start_s, "end_s": s.end_s,
             "declared_frames": s.declared_frames}
            for s in plan_no_form.segments
        ],
        "fallback_segment_count": len(plan_no_form.segments),
    }
    _write("run-summary.json", json.dumps(summary, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:  # noqa: BLE001 - 一次性验收脚本，异常要原样留下
        traceback.print_exc()
        sys.exit(2)

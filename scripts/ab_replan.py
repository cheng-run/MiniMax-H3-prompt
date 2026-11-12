#!/usr/bin/env python
"""#137 A 臂段表重规划：5 段 → 3 段（本机一次性脚本，不入库）。

背景（2026-11-02，票 #137 执行修订）：
- A 臂首轮规划从分镜表给了 5 段，其中段3 只有 39 帧 = 1.625 秒，低于规划器提示词
  自定的「每段 4–15 秒」；用户裁定压到 3 段（opening + camera_cut + ending）。
- 只改**规划请求**：给规划器一份加了三段指令的分镜表；落盘的 `shot_table` 不动，
  写段仍用原分镜表。两臂共用同一份 `plan.json`。

用法（在工作树里、代码仍是 9fbcb6b）：
    PYTHONPATH="$(pwd -W)/src" <主树>/.venv/Scripts/python.exe \
        ../../scripts/ab_replan.py <A 会话目录> [--attempts 4]

行为：
1. 把现有 5 段派生件（plan/segment/tailframe/pack/zip/账本）挪到 `director/.bak-pre-3seg/`，
   保留 `director/assets/01/start.*`（开场首帧不重做）；
2. 状态回 `awaiting_frames`，重新进 `_run_director_flow`；
3. 规划器最多试 `--attempts` 次，直到得到 3 段且类型/时长合格；
4. 写段 → 打包 → 进入逐段陪跑（停在第 1 段贴视频的那一问）。
阶段 1 不重跑。
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

EXPECTED_TYPES = ("opening", "camera_cut", "ending")
FPS = 24.0
MIN_SECONDS = 4.0
MAX_SECONDS = 15.0

DIRECTIVE = (
    "形式：短叙事\n\n"
    "**本次规划的分段要求（必须遵守，覆盖下面的镜头划分）**：\n"
    "- 本片只规划 **3 段**，严格按以下时间轴，不要改动：\n"
    "  第 1 段 `opening` 0–7 秒；第 2 段 `camera_cut` 7–13 秒；"
    "第 3 段 `ending` 13–20 秒。\n"
    "- 每段必须 ≥4 秒、≤15 秒。**收尾段不得因为最后一个镜头只有 3 秒而变短**："
    "把前一个镜头并进第 3 段，补足 7 秒。\n"
    "- 不要出现 `continue` / `camera_same`。\n"
    "- 第 2 段是从街道切到便利店雨棚的切镜段（起点在别处，给首帧 + 尾帧）。\n"
    "- 把下面的分镜镜头按这 3 段归组，**不要逐镜分段**；相邻镜头合并进同一段。\n\n"
)


def backup_and_reset(session_dir: Path) -> Path:
    """把旧段表与派生件挪开、状态回可重规划态；开场首帧留在原地。"""
    from minimax_h3_prompt.session_store import (
        STATUS_AWAITING_FRAMES,
        load_session,
        save_session,
    )

    director = session_dir / "director"
    backup = director / ".bak-pre-3seg"
    if backup.exists():
        # 上一次重规划可能已经在规划阶段失败（没写新 plan）：此时搬移已经做过，
        # 不能因为备份目录存在就拒绝重试。跳过搬移，只把状态再归一次。
        print(f"[备份] 已存在 {backup}，跳过搬移（上一次已重置）")
    else:
        backup.mkdir(parents=True)
        for pattern in ("plan.json", "segment-*.md", "tailframe-*.md",
                        "initial.mmxpack.zip", "final*", "frame-*-last.png"):
            for path in director.glob(pattern):
                shutil.move(str(path), str(backup / path.name))
        for name in ("pack",):
            source = director / name
            if source.exists():
                shutil.move(str(source), str(backup / name))
        segments = session_dir / "segments"
        if segments.exists():
            shutil.move(str(segments), str(backup / "segments"))

    session = load_session(session_dir)
    if session is None:
        raise SystemExit(f"[错误] 会话读不回来：{session_dir}")
    save_session(
        session_dir, session.brief, session.stage_state,
        status=STATUS_AWAITING_FRAMES)
    return backup


def _summary(plan) -> str:
    rows = []
    for segment in plan.segments:
        declared_s = segment.declared_frames / FPS
        export_s = segment.export_frames / FPS
        rows.append(
            f"  段{segment.index} {segment.type} 声明 {segment.declared_frames} 帧"
            f"（{declared_s:.2f}s）/ 导出 {segment.export_frames} 帧（{export_s:.2f}s）")
    return "\n".join(rows)


def _valid(plan) -> tuple[bool, str]:
    types = tuple(segment.type for segment in plan.segments)
    if len(plan.segments) != len(EXPECTED_TYPES):
        return False, f"段数 {len(plan.segments)}，应为 {len(EXPECTED_TYPES)}"
    if types != EXPECTED_TYPES:
        return False, f"类型 {types}，应为 {EXPECTED_TYPES}"
    for segment in plan.segments:
        seconds = segment.declared_frames / FPS
        if seconds < MIN_SECONDS - 1e-6 or seconds > MAX_SECONDS + 1e-6:
            return False, f"段{segment.index} 声明 {seconds:.2f}s，超出 4–15 秒"
    return True, ""


def main() -> int:
    parser = argparse.ArgumentParser(description="A 臂段表重规划：5 段 → 3 段")
    parser.add_argument("session", help="A 臂会话目录（GENxxx）")
    parser.add_argument("--attempts", type=int, default=4)
    args = parser.parse_args()
    session_dir = Path(args.session).resolve()

    backup = backup_and_reset(session_dir)
    print(f"[备份] 旧段表与派生件已挪到 {backup}")

    from minimax_h3_prompt.session_store import load_session

    session = load_session(session_dir)
    if session is None:
        raise SystemExit("[错误] 会话读不回来")
    shot_table = str(session.stage_state.get("shot_table", ""))
    if not shot_table.strip():
        raise SystemExit("[错误] 会话没有分镜表")
    modified = DIRECTIVE + shot_table

    import minimax_h3_prompt.director.planning as planning

    original = planning.plan_director_segments

    def _plan_three(_shot_table, total_s, llm, **kwargs):
        last_error = ""
        for attempt in range(1, args.attempts + 1):
            plan = original(modified, total_s, llm, **kwargs)
            ok, error = _valid(plan)
            print(f"[重规划 {attempt}/{args.attempts}] {len(plan.segments)} 段")
            print(_summary(plan))
            if ok:
                return plan
            last_error = error
            print(f"  → 不合格：{error}，重试")
        raise RuntimeError(
            f"{args.attempts} 次规划都不合格（最后一次：{last_error}）；"
            "可以重跑本脚本，或改 DIRECTIVE 后重试")

    planning.plan_director_segments = _plan_three

    from minimax_h3_prompt.ui.wizard import _run_director_flow

    session = load_session(session_dir)
    if session is None:
        raise SystemExit("[错误] 会话读不回来")
    return_code = _run_director_flow(session)

    plan_path = session_dir / "director" / "plan.json"
    if plan_path.is_file():
        from minimax_h3_prompt.director.planning import read_plan_json
        plan = read_plan_json(plan_path)
        print("\n[结果] 新段表：")
        print(_summary(plan))
    return return_code


if __name__ == "__main__":
    sys.exit(main())

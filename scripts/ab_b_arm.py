#!/usr/bin/env python
"""#137 B 臂启动器（本机一次性脚本，不入库）。

为什么不走普通向导：票面要求「不重规划、plan.json 原样复用」，而向导每次进
``_run_director_flow`` 都会调 ``plan_director_segments``。这个脚本只替换规划函数：
读 A 臂的 plan.json，其余（写段 → 打包 → 逐段陪跑 → 交付）全部走原产线函数。

用法（在工作树里、代码已切到当前 main 之后）：
    PYTHONPATH="$(pwd -W)/src" D:/项目/python/MiniMax-H3-prompt/.venv/Scripts/python.exe \
        ../../scripts/ab_b_arm.py <A 会话目录> <B 会话目录> --cache-name ab-b

落盘纪律（A → B 只搬「共享输入」）：
- 复制：session-state.json、director/plan.json、director/assets/*/start.*、
  director/assets/film-end.*；
- 不复制（B 必须用自己的新词重新产出）：segment-*.md、tailframe-*.md、pack/、
  initial.mmxpack.zip、final-*、frame-*-last.png、segments/segment-ledger.jsonl。
B 目录必须为空——不覆盖是硬约束：覆盖会静默混入上一臂的段词与账。
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import sys
from pathlib import Path


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def prepare(a_dir: Path, b_dir: Path, cache_name: str) -> None:
    """建 B 会话：只搬共享输入，派生件一律不带。"""
    a_plan = a_dir / "director" / "plan.json"
    if not a_plan.is_file():
        raise SystemExit(f"[错误] A 臂缺 plan.json：{a_plan}")
    if not (a_dir / "session-state.json").is_file():
        raise SystemExit(f"[错误] A 臂不是会话目录：{a_dir}")
    if b_dir.exists() and any(b_dir.iterdir()):
        raise SystemExit(f"[错误] B 目录非空，拒绝覆盖：{b_dir}")
    b_dir.mkdir(parents=True, exist_ok=True)

    shutil.copy2(a_dir / "session-state.json", b_dir / "session-state.json")
    (b_dir / "director").mkdir(parents=True, exist_ok=True)
    shutil.copy2(a_plan, b_dir / "director" / "plan.json")

    # 共享参考图：开场首帧是 fl2v 唯一进包的图；全片尾帧（若 A 交了）也共用。
    assets_a = a_dir / "director" / "assets"
    for source in assets_a.glob("*/start.*"):
        target = b_dir / "director" / "assets" / source.parent.name / source.name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    for source in assets_a.glob("film-end.*"):
        target = b_dir / "director" / "assets" / source.name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)

    from minimax_h3_prompt.session_store import (
        STATUS_AWAITING_FRAMES,
        load_session,
        save_session,
    )

    session = load_session(b_dir)
    if session is None:
        raise SystemExit("[错误] B 会话读不回来（session-state.json 无效）")
    if not str(session.stage_state.get("shot_table", "")).strip():
        raise SystemExit("[错误] 复制过来的会话没有分镜表：写段会失败，先检查 A 的会话")

    # 缓存名必须与 A 分开：段缓存目录名是 <cache_name>_<节点 id>，同名会让 B 的账行
    # 指到 A 的段缓存（判定仍按视频，但证据会脏）。节点 id 与引导模式沿用 A。
    session.brief.director_cache_name = cache_name
    save_session(
        b_dir, session.brief, session.stage_state, status=STATUS_AWAITING_FRAMES)


def main() -> int:
    parser = argparse.ArgumentParser(description="B 臂：复用 A 的段表，走当前 main 写段")
    parser.add_argument("a_dir", help="A 臂会话目录（GENxxx）")
    parser.add_argument("b_dir", help="B 臂会话目录（必须为空）")
    parser.add_argument("--cache-name", default="ab-b",
                        help="B 臂导演台缓存名（默认 ab-b，需与 ComfyUI 节点一致）")
    args = parser.parse_args()

    a_dir = Path(args.a_dir).resolve()
    b_dir = Path(args.b_dir).resolve()
    prepare(a_dir, b_dir, args.cache_name)

    a_plan = a_dir / "director" / "plan.json"
    b_plan = b_dir / "director" / "plan.json"
    a_sha = sha256_file(a_plan)
    b_sha = sha256_file(b_plan)
    print(f"[B 臂] A plan.json sha256：{a_sha}")
    print(f"[B 臂] B plan.json sha256：{b_sha}")
    if a_sha != b_sha:
        raise SystemExit("[错误] 复制后 plan.json 不一致，停止")

    # 只替换规划函数：_run_director_flow 在函数体内 `from ..director.planning import
    # plan_director_segments`，所以改模块属性就能接住它，不用改产线代码。
    import minimax_h3_prompt.director.planning as planning

    reused = planning.read_plan_json(a_plan)

    def _reuse_plan(*_args, **_kwargs):
        print("[B 臂] 不重规划：直接复用 A 臂的 plan.json（票面要求）")
        return reused

    planning.plan_director_segments = _reuse_plan  # type: ignore[assignment]

    from minimax_h3_prompt.session_store import load_session
    from minimax_h3_prompt.ui.wizard import _run_director_flow

    session = load_session(b_dir)
    if session is None:
        raise SystemExit("[错误] B 会话读不回来")
    return_code = _run_director_flow(session)

    after = sha256_file(b_plan)
    print(f"[B 臂] 跑完后 B plan.json sha256：{after}（A：{a_sha}）")
    if after != a_sha:
        print(
            "[警告] B 臂重写出的 plan.json 与 A 臂字节不同：内容可能相同、格式不同。"
            "记录表里要写明；若只是空白/键序差异，用 A 的文件覆盖回 B 再复核。",
            file=sys.stderr,
        )
    return return_code


if __name__ == "__main__":
    sys.exit(main())

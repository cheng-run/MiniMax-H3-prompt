"""长片验收取数（只读，不改任何产物）。

「配器是不是三支曲子」「接缝接没接上」要靠人听人看，但**可比对的事实可以机器取**：
把会话参数、成片参数、段账逐段、每段 ``non_diegetic_music`` 原文、桥接帧清单一口气
打出来，人对着读。脚本只取数、不下结论，也不往会话目录写任何文件。

用法（仓库根）：

    PYTHONPATH=src .venv/Scripts/python.exe scripts/accept_longform.py <会话目录>

例：

    PYTHONPATH=src .venv/Scripts/python.exe scripts/accept_longform.py \
        "output/sessions/一个日本女高中生-傍晚放学之后-背着书包-84080c09/GEN001"

两条路线的段账都认：首尾帧链是 ``segments/shot-ledger.jsonl``（只有通过行、记秒），
多参/导演台是 ``segments/segment-ledger.jsonl``（按 ``event``、记帧区间）。逐段提示词
三种落点都认（``segments/shot-NN.md`` / ``director/segment-NN.md`` /
``segments/segment-NN.md``）。桥接帧优先 ``segments/bridge_frames/``，CLI 旧会话回退
会话根的 ``bridge_frames/``。

读视频参数要 ``frame_match``（底层是 cv2）；缺依赖、文件不是视频时如实打「读不出」，
不中断取数。
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

# 配乐一节：`non_diegetic_music:` 之后到下一个字段头（行首 ``字段名:``）或文件结尾。
# 正文既可能紧跟在冒号同一行（真实产物就是这种），也可能从下一行起（桩管线），
# 所以只要求冒号后到字段头之间是任意文本。
_MUSIC_RE = re.compile(
    r"^non_diegetic_music:[ \t]*(.*?)(?=^[a-z][a-z_]*:[ \t]*|\Z)",
    re.S | re.M,
)

# 段账两种落点：先认首尾帧链（base 长片），再认多参/导演台。同一会话只会有一本。
_LEDGER_NAMES = ("shot-ledger.jsonl", "segment-ledger.jsonl")

# 逐段提示词三种命名体系；不同路线各用一套，全收了也不会重名。
_PROMPT_GLOBS = ("segments/shot-*.md", "director/segment-*.md", "segments/segment-*.md")

# 桥接帧两种落点：工作台/新布局在 segments/ 下，CLI 历史布局在会话根。
_BRIDGE_DIRS = ("segments/bridge_frames", "bridge_frames")


def _load_state(root: Path) -> dict[str, Any] | None:
    """读 ``session-state.json``；缺失或读不出返回 None（取数脚本不让它炸掉）。"""
    path = root / "session-state.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _read_rows(path: Path) -> tuple[list[dict[str, Any]], int]:
    """逐行读 jsonl，返回（行, 坏行数）。

    坏行计数而不是抛：追加账坏一行说明那一页缺了，但其余行仍是可比对的事实，
    整份不读等于把好行一起丢掉。
    """
    rows: list[dict[str, Any]] = []
    bad = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            bad += 1
            continue
        if isinstance(row, dict):
            rows.append(row)
        else:
            bad += 1
    return rows, bad


def _video_meta(path: Path) -> dict[str, Any] | None:
    """视频参数；缺 ``frame_match``/cv2 或文件读不出时返回 None。"""
    try:
        from minimax_h3_prompt.tools.frame_match import video_meta
    except ImportError:
        return None
    try:
        meta = video_meta(path)
    except Exception:  # noqa: BLE001 - 取数脚本的降级口，不该因一个坏文件整份读不出
        return None
    return meta or None


def _meta_text(meta: dict[str, Any] | None) -> str:
    """把 ``video_meta`` 的结果打成一行；读不出就说「读不出」。"""
    if not meta:
        return "读不出"
    duration = float(meta.get("duration_s") or 0.0)
    return (f"帧数={meta.get('frames')} fps={meta.get('fps')} "
            f"分辨率={meta.get('width')}x{meta.get('height')} "
            f"时长={duration:.3f}s")


def _music_from_segment(text: str) -> str:
    """从段词正文里取 ``non_diegetic_music`` 一节的处理后原文。"""
    match = _MUSIC_RE.search(text)
    return match.group(1).strip() if match else ""


def _ledger_row_line(row: dict[str, Any]) -> str:
    """段账一行：时间、帧区间、导出帧数、视频在/缺与其参数。

    两条路线记的字段不同（base 记秒、导演台记帧），缺的那一栏如实写「未记」——
    留空会让人以为是 0。
    """
    segment = row.get("segment", "?")
    event = row.get("event")
    tag = f" [{event}]" if event else ""
    seconds = "未记"
    if row.get("start_s") is not None and row.get("end_s") is not None:
        seconds = f"{row['start_s']}–{row['end_s']}"
    frames = "未记"
    if row.get("start_frame") is not None and row.get("end_frame") is not None:
        frames = f"{row['start_frame']}–{row['end_frame']}"
    export = row.get("export_frames", "未记")

    video = row.get("video")
    if not video:
        video_text, meta_text = "未记", "—"
    else:
        path = Path(str(video))
        if path.is_file():
            video_text, meta_text = "在", _meta_text(_video_meta(path))
        else:
            video_text, meta_text = f"缺（{path}）", "—"
    return (f"  段 {segment}{tag}：时间={seconds}s 帧区间={frames} "
            f"导出帧数={export} 视频={video_text} 参数={meta_text}")


def _ledger_lines(root: Path) -> list[str]:
    for name in _LEDGER_NAMES:
        path = root / "segments" / name
        if not path.is_file():
            continue
        rows, bad = _read_rows(path)
        header = f"\n[段账] segments/{name}：{len(rows)} 条"
        if bad:
            header += f"（坏行 {bad}）"
        return [header, *(_ledger_row_line(row) for row in rows)]
    return ["\n[段账] 未找到（segments/shot-ledger.jsonl 或 segments/segment-ledger.jsonl）"]


def _prompt_lines(root: Path) -> list[str]:
    files: list[Path] = []
    for pattern in _PROMPT_GLOBS:
        files.extend(sorted(root.glob(pattern)))
    if not files:
        return ["\n[段词] 未找到（segments/shot-NN.md / director/segment-NN.md / "
                "segments/segment-NN.md）"]
    lines: list[str] = []
    for path in files:
        text = path.read_text(encoding="utf-8")
        music = _music_from_segment(text)
        lines.append(f"\n[段词] {path.relative_to(root).as_posix()}（{len(text)} 字符）")
        lines.append(f"  non_diegetic_music：{music or '（未取出）'}")
    return lines


def _bridge_lines(root: Path) -> list[str]:
    for name in _BRIDGE_DIRS:
        directory = root / name
        if directory.is_dir():
            frames = sorted(path for path in directory.iterdir() if path.is_file())
            lines = [f"\n[桥接帧] {name}：{len(frames)} 个"]
            for frame in frames:
                lines.append(f"  {frame.name}  {frame.stat().st_size} 字节")
            return lines
    return ["\n[桥接帧] 未找到（segments/bridge_frames/ 或 bridge_frames/）"]


def render_report(root: Path) -> str:
    """把五类事实拼成一份人读报告（纯字符串，不写盘）。"""
    lines: list[str] = ["=" * 70, f"会话： {root}"]

    state = _load_state(root)
    if state is None:
        lines.append("  （session-state.json 缺失或读不出：会话参数未取）")
    else:
        raw_brief = state.get("brief")
        brief: dict[str, Any] = raw_brief if isinstance(raw_brief, dict) else {}
        for key in ("mode", "variant", "duration"):
            lines.append(f"  {key} = {brief.get(key)!r}")
        lines.append(f"  status = {state.get('status')!r}")

    final = root / "final.mp4"
    if final.is_file():
        lines.append(f"\n[成片] final.mp4：{_meta_text(_video_meta(final))}")
    else:
        lines.append("\n[成片] final.mp4：缺失")

    lines.extend(_ledger_lines(root))
    lines.extend(_prompt_lines(root))
    lines.extend(_bridge_lines(root))
    lines.append("\n[提示] 脚本只取数、不下结论：配器与接缝要人听人看。")
    return "\n".join(lines) + "\n"


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__)
        return 2
    root = Path(argv[1])
    if not root.is_dir():
        print(f"[错误] 不是目录（不存在或不是会话目录）：{root}")
        return 2
    print(render_report(root), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

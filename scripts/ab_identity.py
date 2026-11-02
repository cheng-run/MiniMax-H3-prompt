#!/usr/bin/env python
"""#137 基准片 A/B 的身份采集与两臂比对（本机一次性脚本，不入库）。

为什么需要它：票 #137 的「锁 A 臂身份」要求把 plan.json、开场首帧、pack 的
widgets/output 整块、每段成片视频的 sha256 固定下来；B 臂逐项相同才算同一片。
这些值散在三个文件（plan.json / pack.json / segment-ledger.jsonl）里，手抄会抄错，
所以做成脚本，记录表直接贴它的输出。

用法：
    python scripts/ab_identity.py <会话目录>
    python scripts/ab_identity.py --compare <A 会话目录> <B 会话目录>
    python scripts/ab_identity.py <会话目录> --json <输出.json>

「会话目录」= output/sessions/<主题>-<hash>/GENxxx（含 session-state.json）。
只读文件，不改任何产物。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

SESSION_FILE = "session-state.json"


def sha256_file(path: Path) -> str:
    """分块算文件 sha256：视频动辄上百 MB，不整份读进内存。"""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_sha(value: object) -> str:
    """把 JSON 块规范化后求哈希：键序与空白不影响结果，语义相同即同值。"""
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def load_json(path: Path):
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def collect(session_dir: Path) -> dict:
    """采集一份会话的全部身份项；缺 plan.json 直接报错（没有段表就不是一次有效运行）。"""
    if not (session_dir / SESSION_FILE).is_file():
        raise SystemExit(f"[错误] 不是会话目录（缺 {SESSION_FILE}）：{session_dir}")
    director = session_dir / "director"
    plan_path = director / "plan.json"
    plan = load_json(plan_path)
    if plan is None:
        raise SystemExit(f"[错误] 缺 plan.json：{plan_path}")

    segments = plan.get("segments") or []
    report: dict = {
        "session": str(session_dir),
        "name": session_dir.name,
        "plan": {
            "path": str(plan_path),
            "sha256": sha256_file(plan_path),
            "segments": len(segments),
            "types": [row.get("type") for row in segments],
            "declared_frames": [row.get("declared_frames") for row in segments],
            "export_frames": [row.get("export_frames") for row in segments],
            "total_export_frames": sum(int(row.get("export_frames") or 0) for row in segments),
        },
    }

    # 参考图：包内的 asset_groups/*/start.* 是送进导演台的那份；assets/*/start.* 是
    # 用户交回的正本。两份都记——真正决定成片的是包内那份。
    start_images: dict[str, str] = {}
    for path in sorted(director.glob("assets/*/start.*")):
        start_images[str(path.relative_to(session_dir))] = sha256_file(path)
    for path in sorted(director.glob("pack/asset_groups/*/start.*")):
        start_images[str(path.relative_to(session_dir))] = sha256_file(path)
    report["start_images"] = start_images

    film_end: dict[str, str] = {}
    for path in sorted(director.glob("assets/film-end.*")):
        film_end[str(path.relative_to(session_dir))] = sha256_file(path)
    report["film_end"] = film_end

    pack_path = director / "pack" / "pack.json"
    pack = load_json(pack_path)
    if pack is None:
        report["pack"] = None
    else:
        widgets = pack.get("widgets")
        output = pack.get("output")
        report["pack"] = {
            "path": str(pack_path),
            "format": pack.get("format"),
            "formatVersion": pack.get("formatVersion"),
            "taskType": pack.get("taskType"),
            "widgets": widgets,
            "widgets_sha256": canonical_sha(widgets),
            "output": output,
            "output_sha256": canonical_sha(output),
        }

    ledger_path = session_dir / "segments" / "segment-ledger.jsonl"
    videos: list[dict] = []
    if ledger_path.is_file():
        for line in ledger_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            if row.get("event") != "output":
                continue
            video = str(row.get("video") or "")
            current = sha256_file(Path(video)) if video and Path(video).is_file() else None
            recorded = row.get("video_sha256")
            videos.append({
                "segment": row.get("segment"),
                "actual_frames": row.get("actual_frames"),
                "export_frames": row.get("export_frames"),
                "video": video,
                "video_sha256": recorded,
                # 现算值用于证明「账上的视频还在盘上、没被后来同路径的产物覆盖」
                "file_sha256_now": current,
                "file_intact": (recorded == current) if recorded and current else None,
                "cache": row.get("cache"),
                "cache_sha256": row.get("sha256"),
                "end_image": row.get("end_image"),
            })
    report["videos"] = videos
    return report


def render(report: dict) -> str:
    plan = report["plan"]
    lines = [f"## 身份：{report['name']}（{report['session']}）", ""]
    lines.append(f"- plan.json sha256：`{plan['sha256']}`")
    lines.append(f"- 段数：{plan['segments']}；类型：{plan['types']}")
    lines.append(
        f"- 声明帧：{plan['declared_frames']}；导出帧：{plan['export_frames']}；"
        f"Σ导出：{plan['total_export_frames']}")
    for rel, value in report["start_images"].items():
        lines.append(f"- 参考图 `{rel}`：`{value}`")
    for rel, value in report["film_end"].items():
        lines.append(f"- 全片尾帧 `{rel}`：`{value}`")
    pack = report.get("pack")
    if pack:
        lines.append(
            f"- pack：`{pack['format']}` / version {pack['formatVersion']} / "
            f"task {pack['taskType']}")
        lines.append(
            f"- widgets sha256：`{pack['widgets_sha256']}` → "
            f"`{json.dumps(pack['widgets'], ensure_ascii=False)}`")
        lines.append(
            f"- output sha256：`{pack['output_sha256']}` → "
            f"`{json.dumps(pack['output'], ensure_ascii=False)}`")
    if report["videos"]:
        lines += ["", "| 段 | 实际帧 | 导出帧 | video_sha256 | 现算 | 一致 | 视频 |",
                  "|---|---|---|---|---|---|---|"]
        for row in report["videos"]:
            lines.append(
                f"| {row['segment']} | {row['actual_frames']} | {row['export_frames']} | "
                f"`{row['video_sha256']}` | `{row['file_sha256_now']}` | "
                f"{row['file_intact']} | {row['video']} |")
    return "\n".join(lines)


def compare(a_report: dict, b_report: dict) -> int:
    """逐项比对两臂身份；只报「同/不同」，不替人下结论（判定仍按判据表）。"""
    a_pack = a_report.get("pack") or {}
    b_pack = b_report.get("pack") or {}
    rows = [
        ("plan.json sha256", a_report["plan"]["sha256"], b_report["plan"]["sha256"]),
        ("段数/类型", f"{a_report['plan']['segments']} {a_report['plan']['types']}",
         f"{b_report['plan']['segments']} {b_report['plan']['types']}"),
        ("导出帧", a_report["plan"]["export_frames"], b_report["plan"]["export_frames"]),
        ("参考图", a_report["start_images"], b_report["start_images"]),
        ("全片尾帧", a_report["film_end"], b_report["film_end"]),
        ("widgets sha256", a_pack.get("widgets_sha256"), b_pack.get("widgets_sha256")),
        ("output sha256", a_pack.get("output_sha256"), b_pack.get("output_sha256")),
        ("format/version/task",
         f"{a_pack.get('format')}/{a_pack.get('formatVersion')}/{a_pack.get('taskType')}",
         f"{b_pack.get('format')}/{b_pack.get('formatVersion')}/{b_pack.get('taskType')}"),
    ]
    failed = 0
    print("| 项 | A 臂 | B 臂 | 同 |")
    print("|---|---|---|---|")
    for name, a_value, b_value in rows:
        same = "✓" if a_value == b_value else "✗"
        if a_value != b_value:
            failed += 1
        print(f"| {name} | `{a_value}` | `{b_value}` | {same} |")
    print()
    if failed:
        print(f"[结论] {failed} 项不同：这不是同一片，先不要往下填判据表。", file=sys.stderr)
    else:
        print("[结论] 全部相同：plan/参考图/widgets/output 符合票面的「同参考图、同 widgets」。")
    return 1 if failed else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="基准片 A/B 身份采集与比对")
    parser.add_argument("session", nargs="?", help="会话目录（output/sessions/<slug>/GENxxx）")
    parser.add_argument("--compare", nargs=2, metavar=("A", "B"), help="比对两臂身份")
    parser.add_argument("--json", default="", help="把采集结果写成 JSON")
    args = parser.parse_args()

    if args.compare:
        a_report = collect(Path(args.compare[0]).resolve())
        b_report = collect(Path(args.compare[1]).resolve())
        print(render(a_report))
        print()
        print(render(b_report))
        print()
        return compare(a_report, b_report)

    if not args.session:
        parser.error("要么给一个会话目录，要么用 --compare A B")
    report = collect(Path(args.session).resolve())
    print(render(report))
    if args.json:
        Path(args.json).write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n[已写入] {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

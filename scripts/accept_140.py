#!/usr/bin/env python
"""#140 验收：对四份存量尾帧文档重跑新判据（只读，不改文档）。

用法：
    PYTHONIOENCODING=utf-8 PYTHONPATH=src .venv/Scripts/python.exe scripts/accept_140.py

产物：output/accept-140/tailframe-validation.md（人读结果）＋ .json（机读）
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parents[1]
SESSION = ROOT / "output/sessions/一段刑警侦探从街区下车到案发现场的视频-932d9e28/GEN001/director"
OUT = ROOT / "output" / "accept-140"


def main() -> int:
    from minimax_h3_prompt.director.tailframe import validate_tailframe_prompt
    from minimax_h3_prompt.image_config import load_frame_image_model

    OUT.mkdir(parents=True, exist_ok=True)
    model = load_frame_image_model()
    rows = []
    lines = [
        "# #140 尾帧存量复核（四份 tailframe）",
        "",
        f"- 校验器：`director.tailframe.validate_tailframe_prompt(text, segment_prompt=…)`",
        f"- 默认模型档：`{model}`（`load_frame_image_model()`）",
        f"- 会话：`{SESSION.relative_to(ROOT).as_posix()}`",
        "",
        "| 文档 | error | warning | 结论 |",
        "|---|---|---|---|",
    ]
    for n in (2, 3, 4, 5):
        tf_path = SESSION / f"tailframe-{n:02d}.md"
        seg_path = SESSION / f"segment-{n:02d}.md"
        text = tf_path.read_text(encoding="utf-8")
        segment = seg_path.read_text(encoding="utf-8")
        issues = validate_tailframe_prompt(text, segment_prompt=segment)
        errors = [i for i in issues if i.severity == "error"]
        warnings = [i for i in issues if i.severity == "warning"]
        codes = [i.code for i in issues]
        verdict = "FAIL" if errors else ("WARN" if warnings else "PASS")
        rows.append({
            "doc": tf_path.name,
            "errors": [{"code": i.code, "message": i.message} for i in errors],
            "warnings": [{"code": i.code, "message": i.message} for i in warnings],
            "verdict": verdict,
        })
        lines.append(f"| `{tf_path.name}` | {len(errors)} | {len(warnings)} | {verdict} |")
        print(f"{tf_path.name}: {verdict} errors={[i.code for i in errors]} "
              f"warnings={[i.code for i in warnings]}")
        assert codes is not None

    lines.append("")
    for row in rows:
        lines.append(f"## `{row['doc']}` — {row['verdict']}")
        lines.append("")
        if not row["errors"] and not row["warnings"]:
            lines.append("（无 error、无 warning）")
        for item in row["errors"]:
            lines.append(f"- **error** `{item['code']}` — {item['message']}")
        for item in row["warnings"]:
            lines.append(f"- warning `{item['code']}` — {item['message']}")
        lines.append("")

    (OUT / "tailframe-validation.md").write_text("\n".join(lines), encoding="utf-8")
    (OUT / "tailframe-validation.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n落盘：{OUT / 'tailframe-validation.md'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""ADR 索引：从各份文件的状态行现生成，不落静态清单（#143 收尾，2026-11-02）。

为什么不手写一份 `docs/adr/README.md`：索引和 27 份文件是**同一份事实的两处口径**，
改了 ADR 忘了改索引不会有任何东西报错——本仓 issue #56 已经吃过一次同型（同一个
契约两处取数）。生成物没有这个问题：它永远等于当前树。

```bash
uv run python scripts/adr_index.py            # 全部
uv run python scripts/adr_index.py --status   # 只看「非仍生效」的（被取代/撤除）
```

数据来自 `> **状态（YYYY-MM-DD 复核）**：…` 那一行（格式由
`tests/test_adr_guard.py` 守着），所以这里不解析正文、不猜。
"""
from __future__ import annotations

import argparse
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ADR_DIR = ROOT / "docs" / "adr"
_STATUS = re.compile(r"^> \*\*状态（(\d{4}-\d{2}-\d{2}) 复核）\*\*：(.+)$")


def _rows() -> list[tuple[str, str, str, str]]:
    rows = []
    for path in sorted(ADR_DIR.glob("*.md")):
        lines = path.read_text(encoding="utf-8").splitlines()
        title = next((line.lstrip("# ").strip() for line in lines if line.startswith("# ")), "")
        status = next((m for m in (_STATUS.match(line) for line in lines[:12]) if m), None)
        if status is None:  # 守卫会报红；这里退化显示而不是崩
            rows.append((path.name[:4], title, "（缺状态行）", ""))
            continue
        rows.append((path.name[:4], title, status.group(2), status.group(1)))
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ADR 索引（从状态行生成）")
    parser.add_argument("--status", action="store_true", help="只列出非「仍生效」的")
    args = parser.parse_args(argv)

    rows = _rows()
    if args.status:
        # 「行为仍生效」「仍生效」都算生效：按前缀只看前 10 字，别把改写过的说法漏掉
        rows = [row for row in rows if "仍生效" not in row[2][:10]]

    print("| 编号 | 标题 | 状态 | 复核日期 |")
    print("|---|---|---|---|")
    for number, title, status, date in rows:
        short = title if len(title) <= 34 else title[:33] + "…"
        print(f"| {number} | {short} | {status[:60]} | {date} |")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""需求追溯：issue → 代码 → 测试（体检 §7「需求追溯 L1」/ P6）。

体检结论：本仓没有「AC → 代码 → 测试」的映射，于是「执行后功能覆盖不全」只能靠
人工记得。真实来源其实**已经在文件里**——本仓约定测试与注释都写 ``#NNN``；这个
脚本把它读出来，回答三个问题：

```bash
uv run python scripts/ac_trace.py                 # 全部 issue 的覆盖概览
uv run python scripts/ac_trace.py --issue 141     # 一张票落到哪些文件
```

判据：

- **只在注释与 docstring 里找**（不看普通字符串），避免把提示词正文里的 ``#1``
  当成票号；
- 一条 issue 至少要有**测试**文件，只有 ``src/`` 命中说明「规则在、测试不在」；
- 输出按号码倒序，方便和 GitHub 的编号对照。

它不是门禁：乱写 ``#NNN`` 照样能过。它的价值是把「哪张票没有测试」变成人一眼
能看见的东西，而不是在 1900 条用例里凭记忆。
"""
from __future__ import annotations

import argparse
import io
import re
import tokenize
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ISSUE_RE = re.compile(r"#(\d{1,5})\b")
# 太小的号码几乎都是「第 1 条」「#1 张图」这类正文引用，不是票号；票号从 #6 起
# （本仓最早的票是 issue #6，见 docs/adr 与 CONTEXT.md）。
MIN_ISSUE = 6


def _comments_and_docstrings(path: Path) -> list[str]:
    """取出文件里的注释与 docstring（tokenize 判定，字符串字面量不算）。"""
    text = path.read_text(encoding="utf-8", errors="replace")
    chunks: list[str] = []
    try:
        for token in tokenize.generate_tokens(io.StringIO(text).readline):
            if token.type == tokenize.COMMENT:
                chunks.append(token.string)
    except (tokenize.TokenError, IndentationError):
        # 解析不了的文件（极少）退回按行看注释，宁多勿漏
        chunks += [line for line in text.splitlines() if line.lstrip().startswith("#")]
    # docstring：模块/函数/类体里第一句字符串
    import ast

    try:
        tree = ast.parse(text)
    except SyntaxError:
        return chunks
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            doc = ast.get_docstring(node, clean=False)
            if doc:
                chunks.append(doc)
    return chunks


def collect() -> dict[int, dict[str, set[str]]]:
    found: dict[int, dict[str, set[str]]] = defaultdict(lambda: {"src": set(), "tests": set()})
    for group, pattern in (("src", "src/**/*.py"), ("tests", "tests/**/*.py")):
        for path in ROOT.glob(pattern):
            if "__pycache__" in path.parts:
                continue
            for chunk in _comments_and_docstrings(path):
                for match in ISSUE_RE.finditer(chunk):
                    number = int(match.group(1))
                    if number >= MIN_ISSUE:
                        found[number][group].add(str(path.relative_to(ROOT)).replace("\\", "/"))
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="issue → 代码 → 测试的覆盖概览")
    parser.add_argument("--issue", type=int, default=None, help="只看某一张票")
    args = parser.parse_args(argv)

    found = collect()
    if args.issue is not None:
        rows = found.get(args.issue)
        if not rows:
            print(f"#{args.issue} 在注释/docstring 里没有任何引用。")
            return 1
        print(f"#{args.issue}")
        for group, label in (("src", "代码"), ("tests", "测试")):
            files = sorted(rows[group])
            print(f"  {label}（{len(files)}）：")
            for file in files:
                print(f"    - {file}")
        if not rows["tests"]:
            print("  ⚠ 只有代码、没有测试：这条规则没有守卫。")
        return 0

    without_tests = [n for n, rows in found.items() if rows["src"] and not rows["tests"]]
    multi = sorted((n for n, rows in found.items() if len(rows["tests"]) >= 2), reverse=True)
    print(f"注释/docstring 里出现的票号：{len(found)} 个")
    print(f"代码与测试都有：{len(found) - len(without_tests)} 个")
    print(f"只有代码没有测试（规则在、守卫不在）：{len(without_tests)} 个")
    if without_tests:
        print("  " + ", ".join(f"#{n}" for n in sorted(without_tests)))
    print(f"两个以上测试文件覆盖的票：{len(multi)} 个")
    if multi:
        print("  " + ", ".join(f"#{n}({len(found[n]['tests'])})" for n in multi[:20]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""结构守卫：文档字符串不许写成 f-string。

为什么：Python 只把**字符串字面量**当 docstring。函数 / 类 / 模块的第一条语句若是
f-string（AST 里的 ``JoinedStr``），``__doc__`` 会变成 ``None``，而现有守卫**都不会报**——
2026-11-01 实测：ruff 的 F 规则对 ``f\"\"\"...{x}...\"\"\"`` 返回 ``All checks passed``，
1863 条全量也照绿。那一次 code-review 修复把 ``pack._segment_slots`` 的 docstring 改成了
f-string（本意是让 ``{limit}`` 真插值），是人工复跑 ``__doc__ is None`` 才抓到的。

判据只认 AST，不猜文字：不要求「必须有 docstring」（本仓不是所有函数都有），只禁止
用 f-string 冒充。改成普通字符串 + 常量名即可，需要插值就写进正文。
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

DOCSTR_OWNERS = (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)


def offenders(path: Path) -> list[tuple[int, str]]:
    """返回 [(行号, 名字)]；名字取函数 / 类名，模块用 <module>。"""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, DOCSTR_OWNERS) or not getattr(node, "body", None):
            continue
        first = node.body[0]
        # docstring 的位置只认「第一条语句、且是裸表达式」；赋值给变量不算。
        if isinstance(first, ast.Expr) and isinstance(first.value, ast.JoinedStr):
            found.append((first.lineno, getattr(node, "name", "<module>")))
    return found


def main(argv: list[str]) -> int:
    roots = [Path(arg) for arg in argv[1:]] or [Path("src")]
    bad = 0
    for root in roots:
        files = [root] if root.is_file() else sorted(root.rglob("*.py"))
        for path in files:
            for line, name in offenders(path):
                print(
                    f"{path}:{line}: {name} 的文档字符串写成了 f-string——"
                    "Python 不把它当 docstring（__doc__ 会是 None）。"
                    "改成普通字符串，需要插值的值用常量名写进正文。",
                    file=sys.stderr,
                )
                bad += 1
    if bad:
        print(f"[check] 文档字符串结构守卫：{bad} 处 f-string docstring", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

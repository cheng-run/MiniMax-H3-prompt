"""函数体内重复导入模块级已有符号的守卫（#333）。

**什么是缺陷**：模块顶层已经 ``from M import X``，函数体内又写一遍
``from M import X``。函数内那一句**不提供任何延迟效果**——模块加载时那个依赖栈
就已经被拉起了；它只让读代码的人多问一句「这里为什么要再导一次」。

**什么不是缺陷**（本用例刻意不碰）：
- 函数体内 ``from M import Y`` 而模块顶层没导入 ``M``：这是**有意的延迟导入**
  （入口期不拉起整条依赖栈，如 ``roundtable._invoke`` 里延迟取 ``run_agents``）；
- 同一模块在不同函数里各导入各的符号：两处延迟导入互相独立，合并会改变加载时序。

范围：本票（#333）涉及的 ``ui/wizard.py`` 与 ``graph/nodes.py``。全仓扫描会打中
本票范围外的同型（``main.py`` 的 ``Path``、``api/jobs.py`` 的 ``replace`` 等），
那是别的票的事，不在这里一次性红。
"""
from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "minimax_h3_prompt"

# 本票清理的两个文件（#333）。加文件＝扩范围，先想清楚是不是另一张票的事。
_FILES = ("ui/wizard.py", "graph/nodes.py")


def _module_level_symbols(tree: ast.Module) -> set[tuple[int, str | None, str]]:
    """模块顶层的 ``from M import X`` 里 (level, module, X) 的集合。"""
    symbols: set[tuple[int, str | None, str]] = set()
    for node in tree.body:  # 只看顶层，不进函数
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                symbols.add((node.level, node.module, alias.name))
    return symbols


def _duplicate_function_imports(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    module_symbols = _module_level_symbols(tree)
    found: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for child in ast.iter_child_nodes(node):  # 只看函数体的直接语句
            if not isinstance(child, ast.ImportFrom):
                continue
            for alias in child.names:
                key = (child.level, child.module, alias.name)
                if key in module_symbols:
                    found.append(
                        f"{path.relative_to(SRC.parent)}:{child.lineno}"
                        f"（{node.name} 里重复导入 {alias.name}，模块顶层已导入）")
    return found


def test_no_function_reimports_a_module_level_symbol() -> None:
    """变异检验：把 ``wizard._choose_canvas`` 里的 ``canvas_display`` 加回函数内导入，本条红。"""
    problems = [
        issue
        for relative in _FILES
        for issue in _duplicate_function_imports(SRC / relative)
    ]
    assert problems == [], (
        "这些函数体内的导入与模块顶层重复，没有延迟效果——删掉函数内那句，"
        "直接用模块级符号：\n  " + "\n  ".join(problems))

"""决议值白名单只能是具名常量（retro 2026-11-15）。

**为什么需要它**：#286 复审抓到一个真缺陷——事件侧构造 ``action_item.resolved`` 时
手抄了一份决议白名单（只写了三个值），锁内落盘用的是另一份（五个值），
``accept_image_facts`` / ``keep_description`` 于是被事件记成 ``submit_image``；快照是对的，
但 ``workbench_store._replay_snapshot`` 用事件值覆盖快照，重放一次决议就变了。
同一张白名单的两份副本，改一处、漏一处**不会有任何报错**。

判据是机械的：``src/minimax_h3_prompt`` 下，除 ``step_machine.py`` 自身外，
**出现在比较/调用表达式里的**决议值元组（≥2 个 ``DECISION_*`` 成员）一律判红。
具名常量（``_DECISIONS`` / ``_SETTLING_ASSET_DECISIONS`` / ``_ASSET_EXTRA_DECISIONS``
这一类赋值）放行——它们是这张白名单的**唯一声明**，各处都从它派生。

守卫自身会响的证明见 ``test_the_scan_recognises_an_inline_whitelist``——
永不为红的守卫等于没有。
"""
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).parents[1]
SOURCE_ROOT = ROOT / "src" / "minimax_h3_prompt"
# 真源自己：决议值常量在那里定义，元组/集合就是声明本身。
DECLARATION_FILE = SOURCE_ROOT / "step_machine.py"
COLLECTION_NODES = (ast.Tuple, ast.List, ast.Set)
# ``frozenset({...})`` / ``set([...])`` 也是白名单的声明形态。
DECLARING_CALLS = frozenset({"frozenset", "set", "tuple"})


def _decision_names() -> frozenset[str]:
    """``step_machine`` 里所有 ``DECISION_*`` 常量的名字（唯一事实源）。"""
    tree = ast.parse(DECLARATION_FILE.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        targets = (
            node.targets if isinstance(node, ast.Assign)
            else [node.target] if isinstance(node, ast.AnnAssign) else [])
        if not targets or not isinstance(node.value, ast.Constant):
            continue
        for target in targets:
            if isinstance(target, ast.Name) and target.id.startswith("DECISION_"):
                names.add(target.id)
    return frozenset(names)


def _member_name(node: ast.AST) -> str | None:
    """集合成员指向的决议常量名：``DECISION_X`` 或 ``step_machine.DECISION_X``。"""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _parents(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    mapping: dict[ast.AST, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            mapping[child] = parent
    return mapping


def _is_declaration(value: ast.AST, parents: dict[ast.AST, ast.AST]) -> bool:
    """这个字面量是不是一次**具名声明**（赋值右值，或赋值右值里的 frozenset 调用）。"""
    current = value
    while True:
        parent = parents.get(current)
        if isinstance(parent, ast.Call) and isinstance(parent.func, ast.Name) \
                and parent.func.id in DECLARING_CALLS:
            current = parent
            continue
        if isinstance(parent, (ast.Assign, ast.AnnAssign)):
            return True
        return False


def inline_whitelist_lines(source: str, decisions: frozenset[str]) -> list[int]:
    """源码里「写在表达式里的决议值白名单」的行号（空表＝这一份源码干净）。"""
    tree = ast.parse(source)
    parents = _parents(tree)
    hits: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, COLLECTION_NODES) or len(node.elts) < 2:
            continue
        members = [name for name in (_member_name(elt) for elt in node.elts)
                   if name in decisions]
        if not members or _is_declaration(node, parents):
            continue
        hits.append(node.lineno)
    return sorted(hits)


def _offenders() -> list[str]:
    decisions = _decision_names()
    offenders: list[str] = []
    for path in sorted(SOURCE_ROOT.rglob("*.py")):
        if path == DECLARATION_FILE:
            continue
        relative = path.relative_to(ROOT)
        for line in inline_whitelist_lines(path.read_text(encoding="utf-8"), decisions):
            offenders.append(f"{relative}:{line}")
    return offenders


def test_decision_whitelists_are_named_constants() -> None:
    assert _offenders() == [], (
        "决议值白名单写进了表达式：把它提成具名常量，各处从同一份派生——"
        "两份副本改一处漏一处不会报错（#286 复审：事件与快照两处口径）")


def test_the_scan_recognises_an_inline_whitelist() -> None:
    """守卫自身会响：一棵最小的坏树要被抓出来，具名声明要被放行。"""
    source = (
        "if decision not in (D1, D2):\n"
        "    pass\n"
        "ALLOWED = frozenset({D1, D2})\n"
        "if decision not in ALLOWED:\n"
        "    pass\n"
    )
    assert inline_whitelist_lines(source, frozenset({"D1", "D2"})) == [1]

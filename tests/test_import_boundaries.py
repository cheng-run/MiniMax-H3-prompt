"""导入边界（体检 P0-5，2026-11-02）。

7 模块导入结的第一刀：把资产类别与 state 键抽到 ``kinds.py``，让配置层与会话层
不再为了两个常量拉起领域/图实现。这里钉住已经断开的边——谁再加回去，当场红。

**没断的边**（如实记在这里，别让绿显得比事实多）：
``asset_ledger → brief_parser``、``graph.state → brief_parser``、
``generation → workflow_config`` 仍在；它们要等 ``RefItem``/``Brief`` 抽到纯类型模块
才能断，属于后续工作项，不在本用例的承诺范围里。
"""
from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "minimax_h3_prompt"


def _imports(path: Path) -> set[tuple[int, str | None]]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {(node.level, node.module)
            for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}


def test_config_layer_no_longer_imports_the_domain_layer():
    """``workflow_config`` 是配置层，``asset_ledger`` 是领域层——方向不许倒。"""
    assert (1, "asset_ledger") not in _imports(SRC / "workflow_config.py")


def test_generation_and_session_store_no_longer_reach_into_the_graph():
    """两个常量不该把「生成」「会话」拖到整张 LangGraph 上。"""
    for name in ("generation.py", "session_store.py"):
        assert (1, "graph.state") not in _imports(SRC / name), f"{name} 又 import 了 graph.state"


def test_kinds_is_a_leaf_module():
    """断环的锚点自己不能再拉起项目模块，否则只是把环挪了个位置。"""
    project_imports = [
        item for item in _imports(SRC / "kinds.py")
        if item[1] not in (None, "__future__")]
    assert project_imports == [], f"kinds.py 引入了项目模块：{project_imports}"


def test_the_moved_constants_are_the_same_objects_everywhere():
    """再导出必须是同一对象，不能各留一份副本。"""
    from minimax_h3_prompt import asset_ledger, kinds, workflow_config
    from minimax_h3_prompt.graph import state

    assert asset_ledger.KINDS is kinds.KINDS
    assert workflow_config.KINDS is kinds.KINDS
    assert state.ASSET_KIND_FIELDS is kinds.ASSET_KIND_FIELDS
    assert state.ASSET_PROMPT_KEYS is kinds.ASSET_PROMPT_KEYS

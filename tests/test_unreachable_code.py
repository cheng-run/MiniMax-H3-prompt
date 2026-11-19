"""不可达语句守卫，与素材台账路线判据的逐值行为（#333）。

两件事：

1. **结构守卫**：``src`` 里任何函数/分支都不许在 ``return`` / ``raise`` 之后还有语句。
   为什么 ruff 不管：它的可达性规则覆盖不到「同一分支连写两条 return」这种形态
   （2026-11-19 实测 ``ruff check`` 全绿），于是 ``route_uses_asset_ledger`` 末尾那句
   重复 return 一直躺着——第二句永不执行，读代码的人却要反复确认「是不是还有用」。
   本守卫改前红、删掉重复 return 后绿（票面 #333 的「改前红/改后绿」证据就是它）。

2. **行为钉合**：删死行不许顺手改判据。``route_uses_asset_ledger`` 的期望值从
   **规格字面**来（D7 §4 / D8 §8.1：``ref`` 整条路线 + 导演台 ``r2v``），不从实现反算；
   路线矩阵逐条列出，加路线或改名单会当场红。
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src" / "minimax_h3_prompt"

_BLOCK_FIELDS = ("body", "orelse", "finalbody")


def _unreachable_statements(path: Path) -> list[str]:
    """函数与分支体里「``return``/``raise`` 之后还有语句」的位置。"""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError:  # 语法坏掉由其它守卫报，这里不重复报
        return []
    found: list[str] = []
    for node in ast.walk(tree):
        for field in _BLOCK_FIELDS:
            block = getattr(node, field, None)
            if not isinstance(block, list):
                continue
            for index, statement in enumerate(block[:-1]):
                if isinstance(statement, (ast.Return, ast.Raise)):
                    leftover = block[index + 1].lineno
                    where = getattr(node, "name", type(node).__name__)
                    found.append(
                        f"{path.relative_to(SRC.parent)}:{leftover}"
                        f"（{where} 在 {statement.lineno} 行的 "
                        f"{type(statement).__name__} 之后还有语句）")
    return found


def test_src_has_no_statements_after_return_or_raise() -> None:
    """变异检验：把 ``route_uses_asset_ledger`` 的重复 return 加回去，本条红。"""
    problems = [
        issue
        for path in sorted(SRC.rglob("*.py"))
        for issue in _unreachable_statements(path)
    ]
    assert problems == [], (
        "这些语句在 return/raise 之后、永不执行——删掉，或把它前面的早退改成条件分支：\n  "
        + "\n  ".join(problems))


# (mode, director_mode, 该 brief 的参考素材是否登记在素材台账)
# 期望值是规格字面（D7 §4 / D8 §8.1），不是照实现抄的：ref 整条路线为真、导演台只有
# r2v 为真，其余一律为假。
_LEDGER_CASES = (
    ("ref", "", True),
    ("ref", "r2v", True),  # 模式判据优先，导演台字段在这条路线里不参与
    ("base", "", False),
    ("director", "t2v", False),
    ("director", "i2v", False),
    ("director", "fl2v", False),
    ("director", "r2v", True),
)

_IDS = [f"{mode}-{director_mode or 'none'}" for mode, director_mode, _ in _LEDGER_CASES]


@pytest.mark.parametrize(("mode", "director_mode", "expected"), _LEDGER_CASES, ids=_IDS)
def test_asset_ledger_route_matches_the_spec(mode, director_mode, expected):
    """删掉不可达 return 之后，ref 模式的路由判定结果逐字不变。"""
    from minimax_h3_prompt.brief_parser import Brief, route_uses_asset_ledger

    brief = Brief(mode=mode, director_mode=director_mode)
    assert route_uses_asset_ledger(brief) is expected

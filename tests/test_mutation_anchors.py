"""变异锚点守卫（retro 2026-11-13，放快档）。

为什么要有：``mutation_check.py`` 的每条变异都靠字面锚点定位；重构把那段代码挪走
或改个常量，锚点就静默失效——脚本把它记成「存活（守卫是摆设）」，看起来像守卫不够
强，实际是锚点该更新。2026-11-13 #252 实测三条同时漂（两条 0 次、一条在干净检出上
永不为红），全都要等到 10 分钟的全量才发现。这里把它降成秒级：锚点不唯一，提交前
就红。
"""
from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
FRONTEND_ROOT = ROOT / "frontend"


def _load_checker():
    spec = importlib.util.spec_from_file_location(
        "mutation_check_under_test", ROOT / "scripts" / "mutation_check.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # 必须先登记进 ``sys.modules``：``@dataclass`` 生成代码时要按模块名回查
    # ``sys.modules[cls.__module__]``，没登记就 AttributeError（Python 3.14 实测）。
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_every_mutation_anchor_is_unique() -> None:
    module = _load_checker()
    assert module.anchor_problems(module.MUTATIONS) == []


def test_missing_anchor_is_reported_not_treated_as_survivor(tmp_path: Path) -> None:
    module = _load_checker()
    target = tmp_path / "sample.py"
    target.write_text("value = 1\n", encoding="utf-8")
    mutation = module.Mutation(
        "missing_anchor", "sample.py", "value = 2", "value = 3", (), "")
    problems = module.anchor_problems((mutation,), root=tmp_path)
    assert len(problems) == 1
    assert "出现 0 次" in problems[0]


def test_duplicated_anchor_is_reported(tmp_path: Path) -> None:
    module = _load_checker()
    target = tmp_path / "sample.py"
    target.write_text("value = 1\nvalue = 1\n", encoding="utf-8")
    mutation = module.Mutation(
        "duplicated_anchor", "sample.py", "value = 1", "value = 3", (), "")
    problems = module.anchor_problems((mutation,), root=tmp_path)
    assert len(problems) == 1
    assert "出现 2 次" in problems[0]


# ---------------------------------------------------------------------------
# 目标用例是否存在（retro 2026-11-15）
# ---------------------------------------------------------------------------
# 上面守的是**代码锚点**（``old`` 在源码里恰好出现一次）；这一节守的是**用例锚点**
# （``tests`` 里的 pytest/vitest 节点真的存在）。两者是不同的失效面：2026-11-15 #287
# 给一条用例改名后，代码锚点全对，用例节点却指向了不存在的名字——pytest 以「收集失败」
# 非零退出，脚本把它记成「杀死」，于是守卫早就没了、报表仍然全绿。同批还查出一条
# 早于票面就失效的（``ref_brief_not_wired``，用例改名 + 代码锚点随 #279 漂走）。


def missing_test_nodes(mutations) -> list[str]:
    """目标节点里落不到真实用例的那些（空表＝每条用例锚点都有效）。

    只写文件路径的节点（整份文件跑）只校验文件存在；带 ``::`` 的再校验用例名——
    pytest 查 ``def <name>(``，vitest 查 spec 里出现该名字面。
    """
    missing: list[str] = []
    for mutation in mutations:
        runner = getattr(mutation, "runner", "pytest")
        for node in getattr(mutation, "tests", ()):
            path, separator, name = str(node).partition("::")
            base = (FRONTEND_ROOT / path) if runner == "vitest" else (ROOT / path)
            if not base.is_file():
                missing.append(f"{node}（文件不存在：{base.relative_to(ROOT)}）")
                continue
            if not separator:
                continue
            text = base.read_text(encoding="utf-8")
            if runner == "vitest":
                if f"'{name}'" not in text and f'"{name}"' not in text:
                    missing.append(f"{node}（spec 里没有这个用例名）")
            elif not re.search(rf"def {re.escape(name)}\s*\(", text):
                missing.append(f"{node}（文件里没有这个用例）")
    return missing


def test_every_referenced_test_node_exists() -> None:
    module = _load_checker()
    assert missing_test_nodes(module.MUTATIONS) == [], (
        "变异锚点指向不存在的用例：pytest 会以「收集失败」收场，脚本却记成「杀死」——"
        "这条守卫实际什么也没守（改指向的用例名，或补一条覆盖同一断言的用例）")


def test_a_missing_test_node_is_reported() -> None:
    """守卫自身会响：不存在的文件、不存在的用例名都要被点名。"""
    missing = missing_test_nodes((
        SimpleNamespace(
            runner="pytest",
            tests=("tests/test_api_asset_loop.py::test_this_case_does_not_exist",)),
        SimpleNamespace(
            runner="vitest",
            tests=("src/features/sessions/AssetsTab.spec.ts::这个用例不存在",)),
        SimpleNamespace(
            runner="pytest",
            tests=("tests/test_not_a_file.py::test_anything",)),
    ))
    assert len(missing) == 3

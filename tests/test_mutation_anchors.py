"""变异锚点守卫（retro 2026-11-13，放快档）。

为什么要有：``mutation_check.py`` 的每条变异都靠字面锚点定位；重构把那段代码挪走
或改个常量，锚点就静默失效——脚本把它记成「存活（守卫是摆设）」，看起来像守卫不够
强，实际是锚点该更新。2026-11-13 #252 实测三条同时漂（两条 0 次、一条在干净检出上
永不为红），全都要等到 10 分钟的全量才发现。这里把它降成秒级：锚点不唯一，提交前
就红。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


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

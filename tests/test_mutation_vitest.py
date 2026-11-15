"""变异运行器的 vitest 目标（#278）。

为什么需要：变异检验原先只会跑 pytest。前端行为想配变异时只能写成 pytest 源码文本
守卫——重构一抽 helper 就红，也不跑真实组件行为。这里钉住新增的 vitest 目标：命令行
怎么拼、依赖缺失时是否按「跳过」处理（而不是误报成「守卫是摆设」）。
"""
from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load():
    spec = importlib.util.spec_from_file_location(
        "mutation_check_under_test", ROOT / "scripts" / "mutation_check.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # dataclass 生成代码按 __module__ 回查 sys.modules，不登记会 AttributeError。
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_mutation_defaults_to_pytest():
    module = _load()
    mutation = module.Mutation("n", "p", "o", "w", (), "")
    assert mutation.runner == "pytest"


def test_vitest_argv_targets_the_spec_and_escapes_the_test_name():
    module = _load()
    argv = module._vitest_argv(
        ("src/features/sessions/SegmentsTab.spec.ts::a.b（差一位）",),
        node="node", vitest="vitest.mjs")

    assert argv[:3] == ["node", "vitest.mjs", "run"]
    assert "src/features/sessions/SegmentsTab.spec.ts" in argv
    # -t 按正则匹配：点号必须转义，否则会匹配到别的用例。
    assert argv[-2:] == ["-t", re.escape("a.b（差一位）")]


def test_vitest_requires_at_most_one_test_name_pattern():
    module = _load()
    with pytest.raises(ValueError):
        module._vitest_argv(("a.spec.ts::一", "b.spec.ts::二"),
                            node="node", vitest="vitest.mjs")


def test_unknown_runner_is_a_script_error():
    module = _load()
    with pytest.raises(ValueError):
        module._run_tests((), "jest")


def test_missing_node_or_frontend_deps_skips(monkeypatch):
    module = _load()
    monkeypatch.setattr(module.shutil, "which", lambda _name: None)

    with pytest.raises(module.MutationSkipped):
        module._run_vitest(("src/anything.spec.ts",))

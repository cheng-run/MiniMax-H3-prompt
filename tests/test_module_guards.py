"""``scripts/module_guards.py`` 的秒级守卫（#371）。

为什么要有：那支脚本是「模块 → 守卫用例」的导航工具，**不是门禁**（同 ``ac_trace.py``
的定位）。所以这里不测命令行外观，只钉它的三条核心判据，每条都给一条反例自检：

1. 高置信层＝``MUTATIONS`` 里 ``path`` 指向该模块的条目。拿真仓数据断言：随便挑一条
   已知有变异的 ``path``，它的用例必须出现。
2. 零变异模块的高置信层为空，宽口径层仍能列出 import 它的测试（或明说为空）。
3. 宽口径层必须带「提到 ≠ 真在守」的噪声声明——两层不是同一份结论。

脚本本身**不是门禁**，所以不给它的输出外观登记变异；但它的核心判据 ``_covers``
（反向匹配曾让 131/238 模块假命中，宽口径层退化成噪声本身）登记了一条变异，由本文件
的反例用例咬住——那是「脚本算错 → 清单就错」的真不变式，不是外观。
"""
from __future__ import annotations

import functools
import importlib.util
import sys
from pathlib import Path

# 这行必须是**真注释**：早先它写在模块 docstring 里，行首正则把它当成纯注释，一条
# 运行时不起作用的声明就过了 #370 的门（retro 2026-11-27 复审抓出，检测已改 tokenize）。
# fast-tier: excluded — 一次查询要加载 3000 行 MUTATIONS 并扫全 tests/，约 3s；本文件是导航工具的守卫，不属于秒级结构性判据。

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "module_guards.py"

_SPEC = importlib.util.spec_from_file_location("module_guards_under_test", SCRIPT)
assert _SPEC and _SPEC.loader
module_guards = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = module_guards
_SPEC.loader.exec_module(module_guards)

# 一次 render 要把 ``tests/`` 全扫一遍；同一份真仓输入在多条用例里复用，缓存住，
# 别让导航工具的守卫自己变成慢测（本文件不进快档，理由见文件开头的声明）。
@functools.lru_cache(maxsize=8)
def _render(query: str) -> str:
    return module_guards.render(query)

# 已知有变异覆盖的模块（``MUTATIONS`` 里 ``path`` 就是它）。选一个稳定的、不会因为
# 别的票重构路径就消失的：``segment_planner`` 在 #343–#347 落地时登记了三条变异。
COVERED_MODULE = "src/minimax_h3_prompt/segment_planner.py"
# 已知零变异的模块：``final_film.py`` 有测试 import 它，但 ``MUTATIONS`` 里没有条目。
ZERO_MODULE = "src/minimax_h3_prompt/final_film.py"


# ---------------------------------------------------------------------------
# 模块名 / 路径归一
# ---------------------------------------------------------------------------


def test_path_dotted_and_stem_forms_resolve_to_the_same_file() -> None:
    """三种常见输入（路径 / 点分名 / 裸词干）落到同一份仓内相对路径。"""
    expected = [COVERED_MODULE]
    assert module_guards.module_candidates(COVERED_MODULE) == expected
    assert module_guards.module_candidates(
        "minimax_h3_prompt.segment_planner") == expected
    assert module_guards.module_candidates("segment_planner") == expected


def test_unknown_module_resolves_to_nothing() -> None:
    """叫不出来的模块返回空——调用方据此给「找不到」而不是假装零变异。"""
    assert module_guards.module_candidates("definitely_not_a_module_xyz") == []


def test_ambiguous_stem_keeps_every_candidate_and_labels_each_mutation() -> None:
    """裸词干命中多份同名模块时全部保留；已覆盖的每条变异要标出它咬的是哪份源文件。"""
    candidates = module_guards.module_candidates("ledger")
    assert len(candidates) >= 2, candidates  # director/ledger.py 与 tools/ledger.py
    # 高置信层逐条带 ``[源文件]``，看串时能对上：挑一个肯定有变异的模块验格式。
    text = _render(COVERED_MODULE)
    assert f"[{COVERED_MODULE}]" in text


# ---------------------------------------------------------------------------
# 第一层：高置信（MUTATIONS）
# ---------------------------------------------------------------------------


def test_known_mutation_path_is_covered_by_the_high_confidence_layer() -> None:
    """真仓里随便挑一条有变异的 ``path``，它的用例必须被高置信层列出。"""
    mutations = module_guards.load_mutations()
    target = next(m for m in mutations if m.path == COVERED_MODULE)
    hits = module_guards.mutations_for_module([COVERED_MODULE], mutations)
    assert target in hits
    # 脚本给高置信层收集的用例确实来自这条变异本身。
    text = _render(COVERED_MODULE)
    assert target.name in text
    for node in target.tests:
        assert node in text, f"高置信层漏了这条变异指向的用例：{node}"


def test_zero_mutation_module_has_an_empty_high_confidence_layer() -> None:
    """零变异模块的第一层必须为空，并明说是覆盖缺口。"""
    mutations = module_guards.load_mutations()
    assert module_guards.mutations_for_module([ZERO_MODULE], mutations) == []
    assert "覆盖缺口" in _render(ZERO_MODULE)


# ---------------------------------------------------------------------------
# 第二层：宽口径（import 它的测试）与噪声声明
# ---------------------------------------------------------------------------


def test_zero_mutation_module_still_lists_importing_tests_in_the_wide_layer() -> None:
    """第一层为空时，第二层仍要能列出 import 它的测试——两层互补，不互相顶替。"""
    hits = module_guards.tests_importing(
        module_guards.dotted_name(ZERO_MODULE), ROOT / "tests")
    assert hits, "final_film 至少被 test_final_film.py import，宽口径层不该全空"
    assert "test_final_film.py" in _render(ZERO_MODULE)


def test_wide_layer_warns_that_mentions_are_noise() -> None:
    """输出里必须有「提到 ≠ 真在守」——不写这句，宽口径清单会被当成结论。"""
    text = _render(ZERO_MODULE)
    assert module_guards.NOISE_WARNING in text
    assert "提到 ≠ 真在守" in text


def test_a_bare_package_import_does_not_match_every_module(tmp_path: Path) -> None:
    """反例自检：``import minimax_h3_prompt`` 只拉包，不该把每个子模块都算成被引用。"""
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_imports_pkg.py").write_text(
        "import minimax_h3_prompt\n", encoding="utf-8")
    (tests_dir / "test_imports_mod.py").write_text(
        "from minimax_h3_prompt.segment_planner import plan_segments\n", encoding="utf-8")

    # 裸 import 包：两个测试都「提到」了包根，但都不是 segment_planner 的引用。
    assert module_guards.tests_importing(
        "minimax_h3_prompt.segment_planner", tests_dir) == ["test_imports_mod.py"]


def test_submodule_import_counts_for_the_parent(tmp_path: Path) -> None:
    """反例自检：``import pkg.mod.sub`` 会让 ``pkg.mod`` 也被加载，算命中。"""
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_sub.py").write_text(
        "import minimax_h3_prompt.api.routes.assets\n", encoding="utf-8")
    assert module_guards.tests_importing(
        "minimax_h3_prompt.api.routes", tests_dir) == ["test_sub.py"]


# ---------------------------------------------------------------------------
# 零变异归属（#372 的扩展点）
# ---------------------------------------------------------------------------


def test_exempt_list_moves_a_zero_module_out_of_the_coverage_gap() -> None:
    """#372 的接缝：登记进 ``ZERO_MUTATION_EXEMPT`` 的模块不再算覆盖缺口。"""
    mutations = module_guards.load_mutations()
    exempt = {ZERO_MODULE: "有一条守卫但没登记变异（示例理由）"}
    assert module_guards.coverage_bucket([ZERO_MODULE], mutations) == "zero"
    assert module_guards.coverage_bucket([ZERO_MODULE], mutations, exempt) == "exempt"
    text = module_guards.render(ZERO_MODULE, exempt=exempt)
    assert "本就不需要变异" in text
    assert "覆盖缺口" not in text


def test_every_exemption_carries_a_concrete_reason_and_nothing_else_is_forgiven() -> None:
    """#372 把「本就不需要变异」的模块填进豁免表：每一条都要有具体理由，且只豁免这些。

    #371 时该表是空的占位（空＝零变异一律算覆盖缺口）。#372 落地后它非空，于是守卫从
    「为空」换成同样要防的形态：**不许静默豁免**——每个键都要指向真实 ``src/*.py``、
    理由不能是空串或三个字了事；表里没登记的零变异模块仍照成覆盖缺口。
    """
    exempt = module_guards.ZERO_MUTATION_EXEMPT
    assert exempt, "表里至少要有一条——若确无豁免模块，说明 #372 的分类结论需重议"
    for path, reason in exempt.items():
        assert path.startswith("src/") and path.endswith(".py"), path
        assert (ROOT / path).is_file(), f"豁免清单指向不存在的文件：{path}"
        assert len(reason.strip()) >= 12, f"{path} 的豁免理由太敷衍：{reason!r}"

    # 没登记的零变异模块仍算覆盖缺口：豁免是白名单，不是「零变异都放过」。
    mutations = module_guards.load_mutations()

    # 反向跌落：真在表里的模块，摘掉它自己那条豁免后必须重新落回覆盖缺口。
    # 用表里现有的键（前面已 assert 非空）而不是另一个从未豁免的模块——否则
    # `== "zero"` 对「本来就不在表里」的模块恒真，这条测试什么也没咬住。
    real = next(iter(exempt))
    stripped = {path: reason for path, reason in exempt.items() if path != real}
    assert module_guards.coverage_bucket([real], mutations, exempt) == "exempt"
    assert module_guards.coverage_bucket([real], mutations, stripped) == "zero"


# ---------------------------------------------------------------------------
# 零变异清单可复现（#372 step-1 的落盘产出）
# ---------------------------------------------------------------------------


def test_coverage_gaps_lists_every_src_module_grouped_by_directory() -> None:
    """#372 的量化清单：按目录分组、每条带行数与档，末尾给三个合计——可复现产出。

    只钉结构与不变量（每个 ``src/*.py`` 都被收进去、档只取三种、合计等于逐条累加），
    不钉具体数字（模块增删会让数字漂，那是清单的正常变化，不是守卫该红的事）。
    """
    grouped = module_guards.coverage_gaps()
    assert grouped, "src/ 下应有模块，清单不该为空"

    listed: dict[str, str] = {}
    for directory, entries in grouped.items():
        assert "/" not in directory.rsplit("/", 1)[-1], f"目录分组不该含文件名：{directory}"
        for relative, line_count, bucket in entries:
            # 分组键就是路径本身所在的目录。
            assert relative.rsplit("/", 1)[0] == directory, relative
            assert line_count > 0, f"{relative} 行数为 0，像读错文件了"
            assert bucket in ("covered", "exempt", "zero"), bucket
            listed[relative] = bucket

    # 每一个 src/ 下的 .py 都在清单里（别静默漏掉某个子目录）。
    on_disk = {
        path.relative_to(ROOT).as_posix()
        for path in (ROOT / "src").rglob("*.py")
        if "__pycache__" not in path.parts
    }
    assert set(listed) == on_disk

    # 渲染文本要带目录标题与三个合计，且三个合计与逐条累加一致。
    text = module_guards.render_coverage_gaps()
    counts = {
        bucket: sum(1 for value in listed.values() if value == bucket)
        for bucket in ("covered", "exempt", "zero")
    }
    for directory in grouped:
        assert f"【{directory}】" in text
    assert text.rstrip().endswith(
        f"covered {counts['covered']} / exempt {counts['exempt']} / zero {counts['zero']}")

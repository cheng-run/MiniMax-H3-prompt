"""结构守卫：根路径推导只许住在 ``paths.py``（#237 / spec #235 §6）。

为什么需要这条：冻结后包安装位置换成 PyInstaller 的解包目录，任何 ``Path(__file__)``
往上爬到仓库根的推导都会指到不存在的位置，而且**不会报错**——它只会让前端 dist、
数据根、配置文件一起静默失效（spec §6 表里四行都是这个形态）。本票把这些推导收口到
``paths.py``，这条守卫负责「别再冒出来第二处」。

判据（只认 ``__file__`` 推导，别把它推广成禁 ``parents[]``）：

- 对任意运行期路径对象用 ``parents[k]`` 是合法操作（例如临时目录里的文件），不在守卫范围；
- ``Path(__file__)`` 链上允许走到**包目录**为止。文件在包内的深度 ``depth`` 就是它到
  包目录的步数（包根下的模块 ``depth=0``，``api/spa.py`` 之类 ``depth=1``）：
  ``parents[k]`` 要求 ``k <= depth``；``.parent`` 链要求步数 ``<= depth + 1``。
- 允许的例子（包内相对资源）：``Path(__file__).parent / "prompts"``、
  ``Path(__file__).resolve().parent.parent / "prompts"``（子包里的模块）；
- 禁止的例子（爬到仓库根）：``Path(__file__).resolve().parents[2]``、
  ``Path(__file__).resolve().parent.parent / "frontend"``（包根模块）。
- 唯一豁免：``paths.py`` 自身——它就是那个「唯一解析口」。
"""
from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_DIR = REPO_ROOT / "src" / "minimax_h3_prompt"
EXEMPT = {PACKAGE_DIR / "paths.py"}

_PARENTS_INDEX = re.compile(r"\bparents\[\s*(\d+)\s*\]")
_PARENT_STEP = re.compile(r"\.parent\b")


def root_derivation_violations(root: Path) -> list[str]:
    """扫描 ``root`` 下的 ``__file__`` 推导，返回「爬到包目录之外」的违规行。"""
    package = root / "minimax_h3_prompt"
    violations: list[str] = []
    for path in sorted(package.rglob("*.py")):
        if path in EXEMPT:
            continue
        depth = len(path.relative_to(package).parts) - 1
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), start=1):
            if "__file__" not in line:
                continue
            code = line.split("#", 1)[0]
            for match in _PARENTS_INDEX.finditer(code):
                index = int(match.group(1))
                if index > depth:
                    violations.append(
                        f"{path.relative_to(root)}:{lineno}: parents[{index}] 超出包目录"
                        f"（depth={depth}）——根路径推导只能走 paths.py")
            if _PARENTS_INDEX.search(code):
                continue
            steps = len(_PARENT_STEP.findall(code))
            if steps > depth + 1:
                violations.append(
                    f"{path.relative_to(root)}:{lineno}: {steps} 次 .parent 超出包目录"
                    f"（depth={depth}）——根路径推导只能走 paths.py")
    return violations


def test_source_only_paths_module_derives_roots() -> None:
    violations = root_derivation_violations(REPO_ROOT / "src")
    assert violations == [], "\n".join(["根路径推导散落在业务模块里：", *violations])


def test_exempt_module_really_derives_the_repo_root() -> None:
    """豁免不是空条文：``paths.py`` 里确实有一条根推导（否则本守卫在守空气）。"""
    text = (PACKAGE_DIR / "paths.py").read_text(encoding="utf-8")
    assert "__file__" in text and _PARENTS_INDEX.search(text)


def _write(root: Path, relative: str, body: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def test_guard_flags_repo_root_derivation_at_package_root(tmp_path: Path) -> None:
    _write(tmp_path, "minimax_h3_prompt/mod.py", "P = Path(__file__).resolve().parents[2]\n")
    violations = root_derivation_violations(tmp_path)
    assert len(violations) == 1 and "parents[2]" in violations[0]


def test_guard_flags_parent_chain_climbing_out_of_package(tmp_path: Path) -> None:
    _write(tmp_path, "minimax_h3_prompt/api/mod.py",
           'P = Path(__file__).resolve().parent.parent.parent / "frontend"\n')
    violations = root_derivation_violations(tmp_path)
    assert len(violations) == 1 and "3 次 .parent" in violations[0]


def test_guard_allows_package_relative_resources(tmp_path: Path) -> None:
    """包内相对资源是合法的（prompts / knowledge 就在包目录下）。"""
    _write(tmp_path, "minimax_h3_prompt/mod.py",
           'P = Path(__file__).resolve().parent / "prompts"\n')
    _write(tmp_path, "minimax_h3_prompt/director/mod.py",
           'P = Path(__file__).resolve().parent.parent / "prompts"\n')
    _write(tmp_path, "minimax_h3_prompt/tools/mod.py",
           'P = Path(__file__).resolve().parents[1] / "prompts"\n')
    assert root_derivation_violations(tmp_path) == []


def test_guard_ignores_unrelated_runtime_path_objects(tmp_path: Path) -> None:
    """对运行期路径对象用 ``parents[k]`` 不是根推导（临时目录里合法）。"""
    _write(tmp_path, "minimax_h3_prompt/mod.py",
           "P = some_path.parents[3] / 'x'\n")
    assert root_derivation_violations(tmp_path) == []

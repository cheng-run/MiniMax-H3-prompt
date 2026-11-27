"""模块 → 守卫用例的映射（导航工具，不是门禁；#371）。

为什么有这支脚本：#371 的拷问结论是**不建静态表**——「哪个模块被哪些守卫守着」已经
有一份零漂移的机读真源：``scripts/mutation_check.py`` 的 ``MUTATIONS``。每条变异是
「源文件路径 + 坏掉的写法 + 咬它的用例」，按 ``path`` 一匹配，就是「源文件 → 咬它的
用例」的映射。静态表会与 ``MUTATIONS`` / ``FAST_TESTS`` / 模块地图三处重复，且必过期。

所以这里只做**秒级动态查询**，给一个模块路径或模块名，输出两层：

```bash
uv run python scripts/module_guards.py src/minimax_h3_prompt/segment_planner.py
uv run python scripts/module_guards.py minimax_h3_prompt.segment_planner
uv run python scripts/module_guards.py segment_planner
```

- **高置信层**：``MUTATIONS`` 里 ``path`` 指向它的条目（源文件 → 咬它的用例）。这是
  有牙的证据：变异检验跑过一次，用例能从绿变红。
- **宽口径层**：import 它的测试文件。这只是「提到」——import 过不等于用例真咬得住
  某个改动点，会有噪声，所以输出里明写「提到 ≠ 真在守」。

零变异的模块高置信层为空，这正是覆盖缺口（#372 量化那份清单）——脚本只把它照出来，
不评判该不该补。本脚本**不是门禁**：乱写不会报错，它的价值是把「改这个模块该跑哪些
守卫」从凭记忆变成一条命令。同 ``ac_trace.py`` 的定位。
"""
from __future__ import annotations

import argparse
import ast
import functools
import importlib.util
import sys
from pathlib import Path
from typing import Iterable, Sequence

ROOT = Path(__file__).resolve().parents[1]

# 零变异模块的显式豁免清单（#372 的扩展点）。
#
# 键是仓库相对的 posix 路径（如 ``src/minimax_h3_prompt/__init__.py``），值是理由。
# #372 把人工判为「本就不需要变异」的模块填在这里——**不靠命名猜**（同 #370 的教训：
# ``__init__.py`` 猜得对，但其它形态猜不准），每一条都读过代码确认过。
#
# 只收「看过了、确认没有值得登记变异的结构化不变式」的模块。判据是「改一行能不能
# 让某条守卫从绿变红」：纯文档字符串的包标记、纯常量声明、纯 re-export 的 shim 都
# 没有这条不变式——它们的逻辑要么在别处（re-export 的源模块已登记变异），要么值由
# 上层用例逐个断言，登记「改了常量就该红」属凑数（#372 票面明令禁止）。
#
# **这不是永久勾销**：它表达「人已看过、判定不需要」。拿不准的模块宁可留在覆盖缺口
# （不在此表），由覆盖清单继续照出来。承载这层判定的函数是 ``coverage_bucket``。
ZERO_MUTATION_EXEMPT: dict[str, str] = {
    "src/minimax_h3_prompt/__init__.py":
        "2 行占位入口：main() 只 print 一句问候，没有任何分支或计算，无可守的不变式。",
    "src/minimax_h3_prompt/api/__init__.py":
        "只声明 SERVICE_NAME 一个字符串常量并导出；值由 API 层用例（test_api_spa）"
        "逐字断言，登记「改常量就该红」属凑数（票面禁）。",
    "src/minimax_h3_prompt/api/routes/__init__.py":
        "整文件只有一个文档字符串的包标记，无任何代码。",
    "src/minimax_h3_prompt/director/__init__.py":
        "纯 re-export shim：把 director.frames 的公开名搬进包命名空间 + __all__，"
        "没有自己的逻辑；真逻辑住在 frames.py（已有多条变异）。",
    "src/minimax_h3_prompt/graph/__init__.py": "单行文档字符串的包标记，无代码。",
    "src/minimax_h3_prompt/output/__init__.py": "单行文档字符串的包标记，无代码。",
    "src/minimax_h3_prompt/tools/__init__.py": "单行文档字符串的包标记，无代码。",
    "src/minimax_h3_prompt/ui/__init__.py": "单行文档字符串的包标记，无代码。",
}

# 输出里必须原样出现的噪声声明：宽口径层「提到」不等于「真在守」。测试断言这句在，
# 免得以后有人把两层合并成一个看起来像结论的清单。
NOISE_WARNING = "提到 ≠ 真在守"


def _load_mutation_module(root: Path = ROOT):
    """加载 ``scripts/mutation_check.py`` 取 ``MUTATIONS``。

    用 importlib 按文件路径加载、不 ``import scripts.mutation_check``：``scripts/`` 不是
    包，入口约定是 ``uv run python scripts/xxx.py``。必须先登记进 ``sys.modules``——
    那里的 ``@dataclass`` 生成代码会按模块名回查（见 ``tests/test_mutation_anchors.py``）。
    """
    path = root / "scripts" / "mutation_check.py"
    spec = importlib.util.spec_from_file_location("module_guards_mutation_source", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"加载不了变异真源：{path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def load_mutations(root: Path = ROOT) -> tuple:
    """加载并缓存变异真源。``mutation_check.py`` 三千行，每次查询重建太浪费——
    真源在一次查询内不会变，缓存只按 ``root`` 分键。"""
    return _cached_mutations(Path(root).resolve())


@functools.lru_cache(maxsize=4)
def _cached_mutations(root: Path) -> tuple:
    return tuple(_load_mutation_module(root).MUTATIONS)


def module_candidates(query: str, root: Path = ROOT) -> list[str]:
    """把用户给的路径 / 模块名归一成一组仓库相对的 posix 路径候选。

    输入形态很多（本脚本的便利性全在这）：
    ``src/.../x.py``、``.../x.py``、``minimax_h3_prompt.x``、裸词干 ``x``。返回所有
    能落到真实文件的形态，按优先序去重——高置信层再拿它们去撞 ``MUTATIONS.path``。
    """
    raw = query.strip().replace("\\", "/")
    if raw.startswith("./"):
        raw = raw[2:]
    found: list[str] = []

    def add(relative: str) -> None:
        if relative and relative not in found and (root / relative).is_file():
            found.append(relative)

    # 1. 原样就是仓内相对路径。
    add(raw)
    # 2. 省略了 src/ 前缀的包内路径（minimax_h3_prompt/segment_planner.py）。
    if not raw.startswith(("src/", "tests/", "scripts/", "frontend/")):
        add("src/" + raw)
    # 3. 点分模块名（minimax_h3_prompt.segment_planner → src/.../segment_planner.py）。
    if "." in raw and "/" not in raw:
        add("src/" + raw.replace(".", "/") + ".py")
    # 4. 裸词干：在 src 下找同名 .py（可能多处同名，全留，由高置信层自己筛）。
    if "/" not in raw and "." not in raw:
        for path in sorted((root / "src").rglob(f"{raw}.py")):
            add(path.relative_to(root).as_posix())
    return found


def mutations_for_module(paths: Iterable[str], mutations: Sequence) -> list:
    """``MUTATIONS`` 里 ``path`` 命中 ``paths`` 的那些——高置信层。"""
    wanted = set(paths)
    return [mutation for mutation in mutations if mutation.path in wanted]


def dotted_name(path: str) -> str | None:
    """``src/`` 下的 ``.py`` → 点分模块名；其它文件没有 import 形态，返回 None。"""
    if not path.startswith("src/") or not path.endswith(".py"):
        return None
    return path[len("src/"):-len(".py")].replace("/", ".")


def _imported_names(tree: ast.AST) -> set[str]:
    """文件里 import 到的点分名（含 ``from a.b import c`` 拼成的 ``a.b.c``）。"""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = "." * node.level + (node.module or "")
            if node.module:
                names.add(base)
            for alias in node.names:
                if alias.name != "*":
                    names.add(f"{base}.{alias.name}" if node.module else alias.name)
    return names


def _covers(imported: str, target: str) -> bool:
    """``imported`` 这个 import 名是否真的把 ``target`` 模块拉进来。

    只认「完全相等」或「import 的是 ``target`` 的子模块」（``import pkg.mod.sub`` 会让
    ``pkg.mod`` 一起被加载）。**刻意不认反方向**（``target`` 是 ``imported`` 的子模块）：
    ``import minimax_h3_prompt`` 只是拉了包，撞上这条会让「任何测试都 import 了包」把
    每个模块都算成「被引用」，宽口径层立刻退化成噪声本身（实测 131/238 假命中）。
    """
    return imported == target or imported.startswith(target + ".")


def tests_importing(dotted: str, tests_dir: Path) -> list[str]:
    """``tests_dir`` 下 import 了 ``dotted`` 模块的测试文件（仓相对无关，返回文件名）。"""
    hits: list[str] = []
    for path in sorted(tests_dir.glob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError:
            continue
        if any(_covers(name, dotted) for name in _imported_names(tree)):
            hits.append(path.name)
    return hits


def tests_mentioning_textually(name: str, tests_dir: Path) -> list[str]:
    """不可 import 的文件（脚本、文档、``.ts``）的宽口径：按文件名做字面提及。"""
    hits: list[str] = []
    for path in sorted(tests_dir.glob("*.py")):
        if name in path.read_text(encoding="utf-8", errors="replace"):
            hits.append(path.name)
    return hits


def coverage_bucket(
    paths: Sequence[str],
    mutations: Sequence | None = None,
    exempt: dict[str, str] | None = None,
) -> str:
    """零变异模块的归属：``covered`` / ``exempt`` / ``zero``。

    - ``covered``：至少一条变异 ``path`` 指向它（有机读证据在守）。
    - ``exempt``：零变异，但在 ``ZERO_MUTATION_EXEMPT`` 里登记为「本就不需要」（#372 填）。
    - ``zero``：零变异且没豁免＝覆盖缺口，该被 #372 的量化清单看到。
    """
    if mutations is None:
        mutations = load_mutations()
    if exempt is None:
        exempt = ZERO_MUTATION_EXEMPT
    if mutations_for_module(paths, mutations):
        return "covered"
    if any(path in exempt for path in paths):
        return "exempt"
    return "zero"


def _exempt_reason(paths: Sequence[str], exempt: dict[str, str]) -> str | None:
    for path in paths:
        if path in exempt:
            return exempt[path]
    return None


def render(
    query: str,
    root: Path = ROOT,
    mutations: Sequence | None = None,
    exempt: dict[str, str] | None = None,
) -> str:
    """渲染两层输出（纯函数，测试直接断言文案）。"""
    if mutations is None:
        mutations = load_mutations(root)
    if exempt is None:
        exempt = ZERO_MUTATION_EXEMPT

    candidates = module_candidates(query, root)
    lines: list[str] = []
    if not candidates:
        lines.append(f"找不到模块：{query!r}")
        lines.append("  试试仓库相对路径（src/.../x.py）、点分模块名（pkg.x）或裸词干（x）。")
        return "\n".join(lines)

    lines.append("模块：" + "、".join(candidates))
    lines.append("")

    # ---- 高置信层 ----
    covered = mutations_for_module(candidates, mutations)
    lines.append("【高置信】MUTATIONS 里覆盖它的条目（源文件 → 咬它的用例）")
    if covered:
        lines.append(f"  {len(covered)} 条变异，都是「改坏这段代码 → 这些用例会红」的证据：")
        for mutation in covered:
            # 裸词干可能命中多个同名模块，每条标明它咬的是哪份源文件，免得看串。
            lines.append(f"  - {mutation.name}  [{mutation.path}]")
            lines.append("      用例：" + "、".join(mutation.tests))
            lines.append(f"      理由：{mutation.why}")
    else:
        bucket = coverage_bucket(candidates, mutations, exempt)
        lines.append("  无。本模块没有任何变异——「永不为红＝没有」，没有机读证据证明谁在守它。")
        if bucket == "exempt":
            reason = _exempt_reason(candidates, exempt)
            lines.append(f"  已登记为「本就不需要变异」：{reason}")
        elif any(path.startswith("src/") for path in candidates):
            # 覆盖缺口只按 ``src/*.py`` 计（#372 的量化范围就是这个）；脚本与文档不在此列。
            lines.append("  覆盖缺口：零变异且未登记豁免（#372 的量化清单就收这一类）。")
    lines.append("")

    # ---- 宽口径层 ----
    lines.append("【宽口径】引用它的测试（" + NOISE_WARNING + "：import 过不代表用例咬得住"
                 "某个改动点，这一层会有噪声）")
    wide_listed = False
    for path in candidates:
        dotted = dotted_name(path)
        if dotted is None:
            continue
        hits = tests_importing(dotted, root / "tests")
        if hits:
            wide_listed = True
            lines.append(f"  import {dotted}（{len(hits)}）：")
            for name in hits:
                lines.append(f"    - {name}")
    if not wide_listed:
        # 没有 import 形态（脚本 / 文档 / .ts）：退回按文件名做字面提及，仍算宽口径噪声。
        for path in candidates:
            name = path.rsplit("/", 1)[-1]
            hits = tests_mentioning_textually(name, root / "tests")
            if hits:
                wide_listed = True
                lines.append(f"  字面提及 {name}（{len(hits)}）：")
                for test in hits:
                    lines.append(f"    - {test}")
    if not wide_listed:
        lines.append("  （没有测试 import 或提到它——这也是一个信号，但不等于它没被间接覆盖。）")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    # Windows 控制台是 cp1252：中文一 print 就 UnicodeEncodeError，整支脚本会以
    # 「崩溃」而不是「查无结果」收场（同 mutation_check.py 的教训，2026-11-02 实测）。
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(
        description="模块 → 守卫用例：高置信（MUTATIONS）+ 宽口径（import 它的测试）两层。")
    parser.add_argument("module", help="模块路径或模块名，如 src/minimax_h3_prompt/x.py 或 x")
    args = parser.parse_args(argv)

    if not module_candidates(args.module):
        print(render(args.module))
        return 1
    print(render(args.module))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

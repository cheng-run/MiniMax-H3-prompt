"""状态契约的机读版本（体检 §4.2，P3）。

体检结论：状态契约分散在四处（``PipelineState``、``ASSET_KIND_FIELDS``、
``session_store._STATE_KEYS``、各模块字面量），**光看 schema 或 grep 都拼不出完整
契约**。这里把两个方向都钉住：

- schema → 持久化：每个声明过的键，要么在白名单里，要么在
  ``PERSISTENCE_EXCLUSIONS`` 里带理由；
- 持久化 → schema：每个进白名单的键，要么声明过，要么在
  ``PERSISTED_OUTSIDE_GRAPH`` 里带理由（向导侧的书签/进度，不进图）。

变异的两种典型形态：
1. 给 ``PipelineState`` 加一个键、忘了决定要不要持久化 → 第一条红；
2. 白名单里加一个拼错的键（``shot_tabel``）→ 第二条红。

另有一条「资产三键从常量展开」的守卫：加第四类资产时白名单与 schema 一起动。

第三半（#334）：两张表的**理由**必须改成 ``模块.符号`` 指针，且指针要真的解析得到——
理由原先写的行号会随重构漂移而一处都不报错（2026-11-18 核过：全表 8 个行号没有一个
指得到它声称的那行）。
"""
from __future__ import annotations

import re
from pathlib import Path

from minimax_h3_prompt import session_store
from minimax_h3_prompt.graph.state import PipelineState
from minimax_h3_prompt.kinds import ASSET_PROMPT_KEYS

_SCHEMA_KEYS = frozenset(PipelineState.__annotations__)
_PERSISTED = frozenset(session_store._STATE_KEYS)


def test_every_schema_key_is_persisted_or_explicitly_excluded():
    undeclared = _SCHEMA_KEYS - _PERSISTED - set(session_store.PERSISTENCE_EXCLUSIONS)
    assert not undeclared, (
        f"PipelineState 新增了 {sorted(undeclared)} 却没说要不要持久化："
        "要么加进 _STATE_KEYS，要么在 PERSISTENCE_EXCLUSIONS 里写理由")


def test_every_persisted_key_is_in_the_schema_or_runs_outside_the_graph():
    undeclared = _PERSISTED - _SCHEMA_KEYS - set(session_store.PERSISTED_OUTSIDE_GRAPH)
    assert not undeclared, (
        f"白名单里有 {sorted(undeclared)} 既不在 PipelineState、也不在"
        " PERSISTED_OUTSIDE_GRAPH：拼错的键会让保存静默丢产物")


def test_exclusion_tables_do_not_go_stale():
    """反向核对：表里列的键必须仍然存在——删除 schema 键时忘了删条目就红。"""
    stale_exclusions = set(session_store.PERSISTENCE_EXCLUSIONS) - _SCHEMA_KEYS
    assert not stale_exclusions, f"PERSISTENCE_EXCLUSIONS 里有过期条目：{sorted(stale_exclusions)}"

    outside = set(session_store.PERSISTED_OUTSIDE_GRAPH)
    assert not outside - _PERSISTED, (
        f"PERSISTED_OUTSIDE_GRAPH 里有不在白名单的键：{sorted(outside - _PERSISTED)}")
    assert not outside & _SCHEMA_KEYS, (
        f"这些键已经在 schema 里了，不该再登记成「图外」：{sorted(outside & _SCHEMA_KEYS)}")


def test_every_reason_is_written_down():
    for table in (session_store.PERSISTENCE_EXCLUSIONS,
                  session_store.PERSISTED_OUTSIDE_GRAPH):
        empty = [key for key, why in table.items() if not str(why).strip()]
        assert not empty, f"这些键没有理由：{empty}"


def test_whitelist_has_no_duplicates():
    assert len(session_store._STATE_KEYS) == len(set(session_store._STATE_KEYS))


# ---------------------------------------------------------------------------
# 两张表的理由改指「模块.符号」，且指针必须真的解析得到（#334）
# ---------------------------------------------------------------------------
# **为什么需要它**：这两张表的理由原先写的是行号（``pipeline.py:297`` 之类）。行号会
# 随重构漂移而**一处都不报错**——2026-11-18 核过一遍，全表 8 个行号没有一个指得到它
# 声称的那行（``user_revisions.py:89`` 指到了改画布尺寸的菜单代码，``pipeline.py:297``
# 指到了重跑计划的产物推导）。改成 ``模块.符号`` 之后符号名本身不会漂；这条用例逐个
# import/反射，模块名写错、或者符号被重命名/删掉，都当场红。
#
# 判据**从表里现推**，用例里不重抄一份指针清单：重抄就是把同一份真相写两遍，表改了
# 用例不跟，守卫当场变摆设（本仓「守卫形态要从常量派生」的硬规则）。
_DOTTED_TOKEN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+")


def _package_dir() -> Path:
    return Path(__file__).parents[1] / "src" / "minimax_h3_prompt"


def _top_level_modules() -> frozenset[str]:
    """包根下能直接 import 的名字（模块与子包）。从文件系统现收，不手抄清单。"""
    root = _package_dir()
    names = {path.stem for path in root.glob("*.py")}
    names |= {path.name for path in root.iterdir() if path.is_dir() and (path / "__init__.py").is_file()}
    names.discard("__init__")
    return frozenset(names)


def _all_module_stems() -> frozenset[str]:
    """包内**所有**模块名（用来抓「模块路径写漏了子包」这种半对指针）。"""
    return frozenset(path.stem for path in _package_dir().rglob("*.py"))


def _walk_attributes(target, parts: list[str]):
    """沿属性链取到终点；任一段不存在返回 ``(None, 断在哪一段)``。"""
    current = target
    for index, part in enumerate(parts):
        if not hasattr(current, part):
            return None, ".".join(parts[:index + 1])
        current = getattr(current, part)
    return current, None


def dangling_pointers(reasons: dict[str, str]) -> list[str]:
    """理由表里解析不到的 ``模块.符号`` 指针。

    判据（从包里现收的名字现推）：

    - 首段是**包根模块**（``graph`` / ``ui`` / ``session_store`` …）→ 后面先取能 import
      的最长模块前缀，剩下的属性链整条都要走得到；
    - 首段是**包内子模块名**但不是包根模块（``wizard`` 实为 ``ui.wizard``）→ 半对指针，
      同样算悬空（这正是 #334 之前表里 ``wizard.py:477`` 那类写法的近亲）；
    - 首段既不是模块也不是子模块名 → 当作普通文本（``SessionState.brief`` 这类类型属性
      写法、``issue #101`` 这类短语），不进判据。
    """
    import importlib

    top = _top_level_modules()
    stems = _all_module_stems()
    bad: list[str] = []
    for key, why in reasons.items():
        for token in _DOTTED_TOKEN.findall(str(why)):
            parts = token.split(".")
            head = parts[0]
            if head in top:
                module, rest = _import_longest(importlib, parts)
                _, missing = _walk_attributes(module, rest)
                if missing is not None:
                    bad.append(f"{key}: {head}.{missing} 走不到（理由：{why}）")
            elif head in stems:
                bad.append(
                    f"{key}: {token} 的模块路径不完整（{head} 不在包根，"
                    f"要写全如 ui.{token}）（理由：{why}）")
    return bad


def _import_longest(importlib, parts: list[str]):
    """从 ``parts`` 里取**能 import 的最长模块前缀**，返回 ``(模块, 剩余属性段)``。"""
    for cut in range(len(parts), 1, -1):
        try:
            module = importlib.import_module("minimax_h3_prompt." + ".".join(parts[:cut]))
        except ImportError:
            continue
        return module, parts[cut:]
    return importlib.import_module(f"minimax_h3_prompt.{parts[0]}"), parts[1:]


def test_persistence_reasons_point_at_symbols_not_line_numbers():
    """理由里不许出现行号：``xxx.py:123`` 会漂，且没有任何检查会报错。"""
    offenders: list[str] = []
    for table in (session_store.PERSISTENCE_EXCLUSIONS,
                  session_store.PERSISTED_OUTSIDE_GRAPH):
        for key, why in table.items():
            if re.search(r"\.py:\d+", str(why)):
                offenders.append(f"{key}: {why}")
    assert not offenders, (
        "持久化契约的理由里又写上行号了：改成「模块.符号」（行号会漂且不报错）：\n  "
        + "\n  ".join(offenders))


def test_persistence_reason_pointers_resolve():
    """理由里的 ``模块.符号`` 必须真的解析得到——拼错模块名或重命名符号就红。"""
    bad = dangling_pointers(session_store.PERSISTENCE_EXCLUSIONS)
    bad += dangling_pointers(session_store.PERSISTED_OUTSIDE_GRAPH)
    assert not bad, (
        "这些指针解析不到（读的人按名字找不到东西，而文件本身还在，"
        "任何检查都不会报错）：\n  " + "\n  ".join(bad))


def test_pointer_resolver_catches_a_missing_symbol_and_ignores_prose():
    """守卫自身会响：真模块 + 不存在的符号要被抓到，类型属性写法不许误伤。"""
    reasons = {
        "ok": "见 graph.pipeline.run_stage2",
        "missing": "见 graph.pipeline.__no_such_symbol__",
        "half": "见 wizard._progress_saver（漏了 ui.）",
        "prose": "单独序列化为 SessionState.brief（同上）",
    }

    bad = dangling_pointers(reasons)

    assert len(bad) == 2, bad
    assert any("__no_such_symbol__" in item for item in bad)
    assert any("ui.wizard" in item for item in bad)

def test_asset_prompt_keys_are_persisted_and_declared():
    """资产三键：白名单从常量展开、schema 逐个声明（issue #77 复审 B）。"""
    for key in ASSET_PROMPT_KEYS:
        assert key in _PERSISTED, f"{key} 不在白名单：续跑会丢这类资产"
        assert key in _SCHEMA_KEYS, f"{key} 没在 PipelineState 声明：节点读不到它"


def test_asset_manifest_keys_are_persisted_and_declared():
    """设计端资产清单三键（issue #173 / spec #166 D）：两处都要在，缺口一个都是静默失效。

    - 不在 schema → LangGraph 只搬运声明过的键，出词节点拿不到清单；
    - 不在白名单 → 续跑后出词节点退回「本会话无资产清单」，整类清单校验静默关掉。
    这条同时钉住「键名从 ``ASSET_MANIFEST_FIELDS`` 派生」：加第四类资产时两处一起动。
    """
    from minimax_h3_prompt.kinds import ASSET_MANIFEST_KEYS

    assert ASSET_MANIFEST_KEYS, "清单键常量是空表，守卫会空转"
    for key in ASSET_MANIFEST_KEYS:
        assert key in _PERSISTED, f"{key} 不在白名单：续跑会丢设计端资产清单"
        assert key in _SCHEMA_KEYS, f"{key} 没在 PipelineState 声明：节点读不到它"


def test_film_size_confirmation_is_persisted_but_stays_outside_the_graph():
    """成片画幅确认（#176）：与清单键**相反**——要持久化，但不进图。

    ``stage_state`` 由向导侧直接读写（``director/aspect_flow.py`` 的
    ``FILM_SIZE_CONFIRM_KEY``），不参与 ``StateGraph`` 搬运，所以登记的落点是
    ``PERSISTED_OUTSIDE_GRAPH`` 而不是 ``PipelineState``。两处漏一处都会静默：
    不在白名单 → 续跑后同一件事反复问；误进 schema → 图把它当状态键搬运，
    契约上就多了一个没有节点的产物。键名从常量取，不在用例里重抄字面量。
    """
    from minimax_h3_prompt.director.aspect_flow import FILM_SIZE_CONFIRM_KEY

    assert FILM_SIZE_CONFIRM_KEY in _PERSISTED, \
        f"{FILM_SIZE_CONFIRM_KEY} 不在保存白名单：续跑会丢掉尺寸确认、反复重问"
    assert FILM_SIZE_CONFIRM_KEY in session_store.PERSISTED_OUTSIDE_GRAPH, \
        f"{FILM_SIZE_CONFIRM_KEY} 没登记成「图外」：状态契约拼不出它的归属"
    assert FILM_SIZE_CONFIRM_KEY not in _SCHEMA_KEYS, \
        f"{FILM_SIZE_CONFIRM_KEY} 进了 PipelineState：它是向导侧键，不该被图搬运"

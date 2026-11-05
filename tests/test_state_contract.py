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
"""
from __future__ import annotations

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

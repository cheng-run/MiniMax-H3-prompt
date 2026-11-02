"""并行组合节点的合并语义（体检 P1-12，2026-11-02）。

旧实现 ``merged.update(f.result())`` 让**后写者赢**，前写者静默消失；2026-10-25
探针实测（三个资产图节点共享一个键 → 只剩最后一类）。各节点本该只写自己的产物键，
这里把「共享键且值不同」变成当场报错，同值重复写（同一份 ref_meta）仍放行。

变异检验：把实现改回 ``merged.update``，第一条用例立刻红。
"""
from __future__ import annotations

import pytest

from minimax_h3_prompt.graph.nodes import make_parallel


def test_parallel_nodes_may_not_silently_overwrite_a_shared_key():
    node = make_parallel(
        lambda state: {"shared": {"character": 1}},
        lambda state: {"shared": {"prop": 2}},
    )

    with pytest.raises(ValueError, match="shared"):
        node({})


def test_equal_values_are_allowed_to_be_written_twice():
    """同一份 ref_meta 被多个节点原样带回是正常形态，不该炸。"""
    node = make_parallel(
        lambda state: {"ref_meta": [1, 2]},
        lambda state: {"ref_meta": [1, 2]},
    )

    assert node({}) == {"ref_meta": [1, 2]}


def test_each_node_writing_its_own_key_still_merges():
    node = make_parallel(
        lambda state: {"a": 1},
        lambda state: {"b": 2},
    )

    assert node({}) == {"a": 1, "b": 2}

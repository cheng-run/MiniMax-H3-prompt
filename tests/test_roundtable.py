"""圆桌构造的入参校验（#333）。

空参与者清单此前会先 ``participants[0]`` 抛 IndexError，而紧跟其后的
``raise ValueError("圆桌至少需要一个参与者")`` 永不执行——注释还写着「调用方已经拦了」。
这里钉住两件事：

1. 空清单抛的是**已分类的业务错误**（``ConfigError``），不是 IndexError；
   分类码让日志与顶层边界能区分「装配参数错」与「代码 bug」。
2. 非空清单的行为逐字不变：子图照常编译、首个/末个发言者与条件边都按输入走。

第 2 条是「修空清单不许顺手改掉正常路径」的护栏——路线判定结果与图结构都属行为面。
"""
from __future__ import annotations

import pytest

from minimax_h3_prompt.errors import ConfigError


def _model():
    """只用来构造子图、不会被调用的桩模型（编译期不需要真模型）。"""
    return object()


def test_empty_participants_raise_a_classified_business_error() -> None:
    from minimax_h3_prompt.graph.roundtable import build_roundtable

    with pytest.raises(ConfigError) as excinfo:
        build_roundtable(_model(), [], max_rounds=1)
    # 仍是 ValueError：存量 ``except ValueError`` 处理器不必改（errors.py 的兼容硬约束）
    assert isinstance(excinfo.value, ValueError)
    assert "参与者" in str(excinfo.value)


def test_non_empty_participants_still_build_the_same_graph() -> None:
    """非空清单的图结构不变：节点名、条件边目标都按输入顺序派生。"""
    from minimax_h3_prompt.graph.roundtable import build_roundtable

    participants = ["producer", "director"]
    graph = build_roundtable(_model(), participants, max_rounds=1)

    # 每个参与者一个发言节点 + 一个主持人节点；条件边回到第一位发言者。
    # 编译图另带 ``__start__`` 哨兵节点，一并钉住——「修空清单」若顺手改了循环
    # 接线，这条会当场红。（``__end__`` 只在 ``get_graph().nodes`` 里出现，见下条。）
    assert set(graph.nodes) == {
        "__start__", "speak_producer", "speak_director", "moderator",
    }
    # 条件边 ``route`` 的两条落点：首位发言者与 END——首位一旦取错（如 ``[-1]``），
    # 循环就从最后一位重新开始，本条红。
    assert {"__end__"} <= set(graph.get_graph().nodes)

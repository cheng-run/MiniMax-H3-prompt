"""直调点覆盖守卫（ADR 0019 决定 1）。

**为什么需要它**：ADR 0019 原句把「澄清、摘要、圆桌」列进了必须走
``agents.run_direct_call`` 的一批，但落地时只接了拆段/写段/素材那几条——
三个调用点仍是裸 ``llm.invoke``，于是它们既没有首字节心跳，也没有卡住重试与
调用预算（复审 S1）。这类漏接不会报错，只会在用户盯着屏幕干等时表现为「卡住」，
所以把 ADR 的决定变成一条会响的判据：**源码里除入口自身与明文豁免的非流式回退，
不许再出现裸 ``llm.invoke`` / ``model.invoke``**。

豁免只有两处，理由都写在 ADR 0019 里：
- ``agents/__init__.py``：统一入口自己的 ``llm.invoke``；
- ``tools/reference_auditor.py``：网关不支持流式时的**非流式回退**（ADR 0019 明文允许）——
  那条路分不出卡住，只诚实报已等时长。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

SRC = Path(__file__).parents[1] / "src" / "minimax_h3_prompt"

# 裸调形态只认「llm/model 直接 invoke」：``agent.invoke`` 属角色 agent 路径，
# 由 run_agent 包装，不在这条裁定的范围内。
_BARE_CALL = re.compile(r"\b(?:llm|model)\.invoke\(")

# 相对 src/minimax_h3_prompt 的路径
_ALLOWED = {
    "agents/__init__.py",
    "tools/reference_auditor.py",
}


def test_no_bare_llm_invoke_outside_the_watchdog_entry() -> None:
    offenders: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        relative = path.relative_to(SRC).as_posix()
        if relative in _ALLOWED:
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if _BARE_CALL.search(line):
                offenders.append(f"{relative}:{number}: {line.strip()}")
    assert not offenders, (
        "这些直调点绕过了统一看门狗（ADR 0019）：\n  " + "\n  ".join(offenders))


class _RecordingModel:
    """圆桌发言用的假模型：只记录 invoke 收到的 config。"""

    def __init__(self, text: str = "已锁定", *, boom: bool = False) -> None:
        self.text = text
        self.boom = boom
        self.configs: list[dict | None] = []

    def invoke(self, _msgs, config: dict | None = None):
        self.configs.append(config)
        if self.boom:
            raise RuntimeError("gateway boom")
        return type("Reply", (), {"content": self.text})()


def test_roundtable_invocation_goes_through_the_direct_call_watchdog() -> None:
    """圆桌发言/主持也要走统一入口（ADR 0019 决定 1 原句点名「圆桌」，复审 S1）。

    变异检验：把 ``model.invoke(msgs)`` 改回裸调，本轮 config 为 None，本条红。
    """
    from minimax_h3_prompt.graph import roundtable

    model = _RecordingModel("已锁定")
    assert roundtable._invoke("role_roundtable_producer", model, []) == "已锁定"
    assert model.configs and model.configs[0], "圆桌调用没带 config——仍是裸 invoke"
    assert model.configs[0].get("callbacks"), "圆桌调用没挂看门狗回调（等不出首字节心跳）"


def test_roundtable_failure_carries_the_watchdog_diagnosis() -> None:
    """失败语义照旧：抛异常（带病因），不静默返回空串让下游当成「无结论」。"""
    from minimax_h3_prompt.graph import roundtable

    with pytest.raises(RuntimeError) as excinfo:
        roundtable._invoke("role_roundtable_producer", _RecordingModel(boom=True), [])
    assert "gateway boom" in str(excinfo.value)

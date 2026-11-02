"""结构化运行日志（体检 §4.5 / P4）。

体检结论：617 处 ``print``，没有一条落盘，出事后无法从盘上复盘一次运行。

这里钉三件事：

1. **阶段入口真接线**：``run_stage1`` 跑完留下 ``run_start``/``stage_start``/
   ``stage_end`` 与 ``agent_done``（token 与耗时都在行里）；
2. **失败也落一行**：阶段抛错时 ``stage_end status=error`` 带分类码；
3. **只记元数据不记正文**：提示词/模型输出只留长度，正文不出现在文件里。

conftest 的 ``no_run_log`` 默认关掉它；本文件的两条用例各自打开。
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from minimax_h3_prompt import model_factory, observability
from minimax_h3_prompt.brief_parser import Brief
from minimax_h3_prompt.config import Config
from minimax_h3_prompt.errors import ConfigError
from minimax_h3_prompt.graph import nodes, pipeline, roundtable

_SECRET = "用户剧情里的独有句子不会进日志"


def _rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _brief() -> Brief:
    return Brief(mode="base", variant="FL2VA", duration=5.0, style="写实", plot=_SECRET)


@pytest.fixture
def logging_on(monkeypatch, tmp_path):
    monkeypatch.setenv("H3_RUN_LOG", "1")
    monkeypatch.setattr(pipeline.stage_saver, "base", tmp_path / "stages")
    observability.run_log.stop()  # 别把上一条用例的句柄带进来
    yield tmp_path
    observability.run_log.stop()


def test_stage1_writes_events_and_no_prompt_text(logging_on, monkeypatch):
    """图被换成两个空节点：本用例验的是**日志边界**，不是阶段 1 的逻辑。"""

    class _Graph:
        def stream(self, initial, stream_mode=None):
            # 模拟节点内的一次 LLM 调用上报（agent_done 是事件总线的既有事件）
            observability.reporter.emit({
                "type": "agent_done", "role": "role_storyboard",
                "duration": 1.25, "input": 120, "output": 30, "out_len": 30,
            })
            yield {"storyboard": {"shot_table": "分镜正文"}}

    # 模型构造必须打桩：_run_stage1 第一行就建客户端，本机有 .env 能过、
    # 干净检出会抛「缺失 MINIMAX_API_KEY」——2026-11-02 CI 第一次跑就是这么红的。
    monkeypatch.setattr(model_factory, "build_chat_model", lambda: object())
    monkeypatch.setattr(
        pipeline, "build_pipeline_graph", lambda *a, **k: (_Graph(), {}))
    monkeypatch.setattr(pipeline, "_stage1_frame_qa_loop", lambda state, *a: state)
    cfg = SimpleNamespace(roundtable_max_rounds=1, common_sense_qa=False,
                          max_qa_iterations=0)

    state, _model, _agents = pipeline.run_stage1(_brief(), cfg)
    assert state["shot_table"] == "分镜正文"

    logs = list((logging_on / "logs").glob("*stage1.jsonl"))
    assert len(logs) == 1, "阶段 1 没有留下运行日志"
    rows = _rows(logs[0])
    events = [row["event"] for row in rows]
    assert events[0] == "run_start" and "stage_start" in events
    assert events[-1] == "stage_end"
    assert rows[-1]["status"] == "ok" and rows[-1]["stage"] == 1
    # 事件总线上的 agent_done 也进了同一本账（token/耗时在行里）
    done = [row for row in rows if row["event"] == "agent_done"]
    assert done and {"role", "duration", "input", "output"} <= set(done[0])
    # 只记元数据：剧情正文与产物正文都不许出现
    text = logs[0].read_text(encoding="utf-8")
    assert _SECRET not in text and "分镜正文" not in text


def test_stage_failure_is_recorded_with_its_error_code(logging_on, monkeypatch):
    def boom(brief, config):
        raise ConfigError("config/agent.yaml 的 director 节必须是对象")

    monkeypatch.setattr(pipeline, "_run_stage1", boom)

    with pytest.raises(ConfigError):
        pipeline.run_stage1(_brief(), Config())

    logs = list((logging_on / "logs").glob("*stage1.jsonl"))
    rows = _rows(logs[0])
    assert rows[-1]["event"] == "stage_end"
    assert rows[-1]["status"] == "error"
    assert rows[-1]["error_code"] == "CONFIG"
    assert rows[-1]["error"] == "ConfigError"

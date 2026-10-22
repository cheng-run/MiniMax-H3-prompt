"""等待状态转折的渲染（issue #62）。

接缝只有一条：**既有事件总线**（``observability.reporter``）。事件由调用层发
（``agents.run_agent`` 与 ``FirstByteHandler``），渲染由监听器做——票面把这两件事
分开钉，所以本文件也从两头各咬一口：

- 直接构造事件 dict 喂监听器：毫秒级、不真等，断言「一行里有什么」；
- 用假 agent 驱动一次真实 ``run_agent``：断言「一次卡住→重试→成功发出什么序列」。

为什么不测 ETA：成本表不存 duration，没有历史基线可外推（spec #59 明令不做）。
"""
from __future__ import annotations

import io

import pytest
from langchain_core.messages import AIMessage
from rich.console import Console

from minimax_h3_prompt.agents import run_agent
from minimax_h3_prompt.observability import reporter
from minimax_h3_prompt.ui.progress import (
    LiveProgress,
    TextProgress,
    event_scope,
    format_step_banner,
    format_wait_event,
    step_scope,
)

ROLE = "role_art_director"
NAME = "art_director"  # role_label 剥掉 role_ 前缀后的用户可读名


class TestFormatWaitEvent:
    """事件 → 一行文本。所有数字都取自事件字段，渲染层不自己算。"""

    def test_first_byte_reports_elapsed_from_event(self):
        line = format_wait_event({
            "type": "agent_wait", "role": ROLE, "state": "first_byte", "elapsed": 0.62,
        })
        assert NAME in line
        assert "首字节" in line
        assert "0.6s" in line

    def test_generating_reports_chunks_bytes_and_waited(self):
        line = format_wait_event({
            "type": "agent_wait", "role": ROLE, "state": "generating",
            "elapsed": 24.0, "chunks": 12, "bytes": 3400,
        })
        assert NAME in line
        assert "12 块" in line
        assert "3.4 kB" in line
        assert "已等 24s" in line

    def test_bytes_switch_to_kb_at_one_thousand(self):
        """单位进位点钉住千进制（docstring 的 1000）——1010 字节应显示 1.0 kB。

        变异检验：阈值改成 1024，这条红（会显示 1010 B）；只改除数不管阈值、
        或反过来，都必须被这里咬住。
        """
        line = format_wait_event({
            "type": "agent_wait", "role": ROLE, "state": "generating",
            "elapsed": 1.0, "chunks": 1, "bytes": 1010,
        })
        assert "1.0 kB" in line
        assert "1010 B" not in line

    def test_stuck_reports_waited_seconds_and_attempt(self):
        line = format_wait_event({
            "type": "agent_wait", "role": ROLE, "state": "stuck",
            "elapsed": 30.2, "attempt": 1,
        })
        assert NAME in line
        assert "30 秒" in line  # 30.2 → 取整；阈值本身来自实测的 elapsed，不是字面量
        assert "第 1 次尝试" in line

    def test_retrying_takes_retry_number_from_event(self):
        """重试序号由发事件的一侧派生（#62 裁定 2）——渲染层再减一次就与 attempt 错位。

        attempt 故意给 99、retry 给 5：实现若自己算 ``attempt - 1``，这条立刻红。
        """
        line = format_wait_event({
            "type": "agent_wait", "role": ROLE, "state": "retrying",
            "attempt": 99, "retry": 5, "remaining_budget": 1170.0,
        })
        assert "正在重试第 5 次" in line
        assert "第 99 次尝试" in line
        assert "1170s" in line

    def test_unknown_state_renders_nothing(self):
        """未知状态安静忽略：将来加状态时旧渲染器不许打半句。"""
        assert format_wait_event({
            "type": "agent_wait", "role": ROLE, "state": "future_state",
        }) is None


class TestTextProgress:
    """文本监听器：每个状态转折打一行（flush），既有开始/完成两行逐字不变。"""

    @pytest.mark.parametrize("state,fields,must_have", [
        ("first_byte", {"elapsed": 0.62}, ["首字节", "0.6s"]),
        ("generating", {"elapsed": 24.0, "chunks": 12, "bytes": 3400}, ["12 块", "3.4 kB", "24s"]),
        ("stuck", {"elapsed": 30.2, "attempt": 1}, ["30 秒", "第 1 次尝试"]),
        ("retrying", {"attempt": 2, "retry": 1, "remaining_budget": 1170.0},
         ["正在重试第 1 次", "第 2 次尝试", "1170s"]),
    ])
    def test_each_wait_state_prints_exactly_one_line(self, capsys, state, fields, must_have):
        TextProgress()({"type": "agent_wait", "role": ROLE, "state": state, **fields})
        out = capsys.readouterr().out
        assert out.count("\n") == 1
        line = out.rstrip("\n")
        assert line.startswith("  ")  # 与既有开始/完成行同一缩进
        assert NAME in line
        for text in must_have:
            assert text in line

    def test_start_and_done_lines_are_unchanged(self, capsys):
        """既有两类事件的文案逐字不变（改的是加了一类，不是改了旧的）。"""
        progress = TextProgress(clock=lambda: 0.0)
        progress({"type": "agent_start", "role": ROLE})
        progress({"type": "agent_done", "role": ROLE,
                  "duration": 2.5, "input": 10, "output": 20})
        lines = capsys.readouterr().out.splitlines()
        assert lines[0] == "  ▶ art_director 正在处理……"
        assert lines[1].startswith(
            "  ✓ art_director 完成（2.5s | 输入 10 tok / 输出 20 tok | 累计 0:00"
        )

    def test_unrelated_or_unknown_events_print_nothing(self, capsys):
        progress = TextProgress()
        progress({"type": "agent_tool", "role": ROLE})
        progress({"type": "agent_wait", "role": ROLE, "state": "future_state"})
        assert capsys.readouterr().out == ""


def _render_text(progress: LiveProgress) -> str:
    """把面板渲染进字符串：本仓允许按渲染结果断言，不钻中间变量。"""
    buf = io.StringIO()
    console = Console(file=buf, width=120, color_system=None, force_terminal=False)
    console.print(progress._render())
    return buf.getvalue()


class TestLiveProgress:
    """富面板：状态列跟着等待转折更新，别只在 start/done 两态之间跳。"""

    @pytest.mark.parametrize("event,must_have", [
        ({"type": "agent_wait", "role": ROLE, "state": "first_byte", "elapsed": 0.6},
         "首字节"),
        ({"type": "agent_wait", "role": ROLE, "state": "generating",
          "elapsed": 24.0, "chunks": 12, "bytes": 3400}, "生成中 12 块"),
        ({"type": "agent_wait", "role": ROLE, "state": "stuck",
          "elapsed": 30.0, "attempt": 1}, "卡住"),
        ({"type": "agent_wait", "role": ROLE, "state": "retrying",
          "attempt": 3, "retry": 2, "remaining_budget": 1000.0}, "重试 2"),
    ])
    def test_status_column_follows_wait_states(self, event, must_have):
        progress = LiveProgress()
        progress(event)
        text = _render_text(progress)
        assert NAME in text
        assert must_have in text

    def test_wait_state_replaces_running_status(self):
        """`agent_start` 每次 run_agent 只发一次（#62 裁定 1），重试不再重发；
        面板若停在「运行中」，用户就永远看不出这是第几次尝试。"""
        progress = LiveProgress()
        progress({"type": "agent_start", "role": ROLE})
        assert "运行中" in _render_text(progress)

        progress({"type": "agent_wait", "role": ROLE, "state": "retrying",
                  "attempt": 3, "retry": 2, "remaining_budget": 1000.0})
        text = _render_text(progress)
        assert "重试 2" in text
        assert "运行中" not in text


class TimeoutException(Exception):
    """按类名判超时（生产形态的完整说明见 ``test_run_agent_wait.py``）。

    本类刻意叫这个名字：openai 两代 http 栈（httpx / vendored httpx2）唯一的交集
    是 MRO 里叫 ``TimeoutException`` 的基类，改名就脱离真实形态、判据会失去意义。
    """


class FakeAgent:
    """最小假 agent：只提供 run_agent 用得到的两样（``.name`` 与 ``.invoke``）。"""

    name = ROLE

    def __init__(self, behaviour):
        self.calls = 0
        self._behaviour = behaviour

    def invoke(self, payload, config=None):
        self.calls += 1
        callbacks = (config or {}).get("callbacks") or [None]
        return self._behaviour(self.calls, callbacks[0])


@pytest.fixture
def events():
    """订阅既有事件总线（不新开接缝），收集一次 run_agent 发出的全部事件。"""
    got: list[dict] = []
    reporter.subscribe(got.append)
    yield got
    reporter.unsubscribe(got.append)


class TestRunAgentEventSequence:
    """事件的**发出侧**：一次「卡住→重试→成功」在总线上长什么样。"""

    def test_stuck_retry_success_emits_start_once(self, events):
        def behaviour(call_no, handler):
            if call_no == 1:
                raise TimeoutException("首字节前卡住")
            handler.on_llm_new_token("美")
            return {"messages": [AIMessage(content="美术设定")]}

        assert run_agent(FakeAgent(behaviour), "请写美术设定") == "美术设定"

        # 精确序列：开始一次、卡住、重试、首字节、完成
        assert [(e["type"], e.get("state")) for e in events] == [
            ("agent_start", None),
            ("agent_wait", "stuck"),
            ("agent_wait", "retrying"),
            ("agent_wait", "first_byte"),
            ("agent_done", None),
        ]
        retrying = [e for e in events if e.get("state") == "retrying"][0]
        # retry 由发事件的一侧派生（第一次重试＝1），attempt 是即将发起的尝试序号
        assert retrying["retry"] == 1
        assert retrying["attempt"] == 2
        assert retrying["role"] == ROLE

    def test_two_retries_do_not_repeat_start(self, events):
        """重试不再打「正在处理」：那是第几次由 retrying 事件说，重复的开始行只会误导。"""

        def behaviour(call_no, handler):
            if call_no < 3:
                raise TimeoutException("首字节前卡住")
            handler.on_llm_new_token("ok")
            return {"messages": [AIMessage(content="ok")]}

        assert run_agent(FakeAgent(behaviour), "请写美术设定") == "ok"

        assert len([e for e in events if e["type"] == "agent_start"]) == 1
        retrying = [e for e in events if e.get("state") == "retrying"]
        assert [e["retry"] for e in retrying] == [1, 2]
        assert [e["attempt"] for e in retrying] == [2, 3]

    def test_empty_output_retry_does_not_reemit_start(self, events):
        """空产出重试是**同一次产出**内部的补一次，不是新的一次处理。"""

        def behaviour(call_no, handler):
            handler.on_llm_new_token("")
            return {"messages": [AIMessage(content="" if call_no == 1 else "正文")]}

        assert run_agent(FakeAgent(behaviour), "请写美术设定") == "正文"

        assert len([e for e in events if e["type"] == "agent_start"]) == 1


class TestTopicProgress:
    """主题单入口的文本监听器：与向导共用同一份状态文案（别成两处口径）。"""

    def test_wait_states_are_rendered(self, capsys):
        from minimax_h3_prompt.main import _topic_progress

        _topic_progress({"type": "agent_wait", "role": ROLE, "state": "stuck",
                         "elapsed": 30.0, "attempt": 1})
        out = capsys.readouterr().out
        assert out.count("\n") == 1
        assert "[等待]" in out
        assert NAME in out
        assert "第 1 次尝试" in out

    def test_start_and_done_lines_are_unchanged(self, capsys):
        from minimax_h3_prompt.main import _topic_progress

        _topic_progress({"type": "agent_start", "role": ROLE})
        _topic_progress({"type": "agent_done", "role": ROLE, "duration": 2.5})
        lines = capsys.readouterr().out.splitlines()
        assert lines == ["[进行中] art_director 正在处理……", "[完成] art_director（2.5s）"]


class TestStepBanner:
    """步骤横幅（issue #67 / ADR 0019）：回答「在干什么、第几轮」，与心跳分开。

    心跳（``format_wait_event``）回答「还活着吗」——两种信号不可互相代替，所以横幅
    不带已等时长，轮次也不许省成「进行中」。
    """

    def test_banner_carries_action_and_round(self):
        assert format_step_banner("正在分段规划") == "▶ 正在分段规划（第 1/1 轮）……"

    def test_banner_follows_given_round_numbers(self):
        """轮次取自入参（qa 复审那种多轮长步骤要用），渲染层不写死 1/1。"""
        line = format_step_banner("正在 qa 复审", round_no=2, total_rounds=3)
        assert "第 2/3 轮" in line

    def test_step_scope_prints_banner_and_renders_heartbeat(self, capsys):
        """拆段用的组合：先横幅，等待期间的心跳行由同一个作用域打印。"""
        got: list[dict] = []
        reporter.subscribe(got.append)
        try:
            with step_scope("正在分段规划"):
                reporter.emit({"type": "agent_wait", "role": ROLE, "state": "first_byte",
                               "elapsed": 0.6})
                reporter.emit({"type": "agent_wait", "role": ROLE, "state": "generating",
                               "elapsed": 12.0, "chunks": 5, "bytes": 100})
        finally:
            reporter.unsubscribe(got.append)

        out = capsys.readouterr().out
        assert "▶ 正在分段规划（第 1/1 轮）……" in out
        assert "已等 12s" in out
        assert out.index("第 1/1 轮") < out.index("已等 12s")

    def test_step_scope_unsubscribes_on_exit(self, capsys):
        """退出即解绑：否则后面每一步都会重复打印同一串心跳（监听器越叠越多）。"""
        with step_scope("正在分段规划"):
            pass
        capsys.readouterr()  # 丢掉横幅那一行
        reporter.emit({"type": "agent_wait", "role": ROLE, "state": "first_byte",
                       "elapsed": 0.5})
        assert capsys.readouterr().out == ""

    def test_event_scope_subscribes_without_a_banner(self, capsys):
        """素材循环整体订阅用它：循环里每一步各有横幅，这一层只管别出现静默窗口。"""
        with event_scope():
            reporter.emit({"type": "agent_wait", "role": ROLE, "state": "first_byte",
                           "elapsed": 0.5})
        out = capsys.readouterr().out
        assert "已有响应" in out, "订阅了却没渲染心跳"
        assert "▶" not in out, "不带横幅的作用域不该自己打横幅"

    def test_nested_scopes_share_one_listener(self, capsys):
        """嵌套（循环级 event_scope ＋ 步骤级 step_scope）只许打一遍心跳。

        issue #71 的接线正是这个形状：素材循环整体在订阅里，读图/改写再各套一个
        step_scope。两层各订一个监听器的话，同一件事会在终端上出现两遍——用户会
        以为系统跑了两轮。
        """
        with event_scope():
            with step_scope("正在读图校对"):
                reporter.emit({"type": "agent_wait", "role": ROLE, "state": "first_byte",
                               "elapsed": 0.5})
        out = capsys.readouterr().out
        assert out.count("已有响应") == 1, f"同一心跳打了两遍：\n{out}"

"""交互日志（#171 / spec #169 B）：向导问答与告警落盘，验收项从「靠人回忆」变成可 grep。

唯一接缝是向导提问函数 ``ui.wizard._prompt``（``_confirm`` 复用它，天然全覆盖）；
日志本身是外部产物：一行一条 JSON、追加写、逐条 flush，字段形状即契约。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from minimax_h3_prompt.observability import interactions
from minimax_h3_prompt.ui import wizard


def _feed(monkeypatch, answers: list[str]) -> None:
    """把内置 ``input`` 排成队列（``_prompt`` 直接用内置 ``input``）。"""
    queue = iter(answers)
    monkeypatch.setattr("builtins.input", lambda text="": next(queue))


def _rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]


@pytest.fixture
def log_path(tmp_path: Path) -> Path:
    path = tmp_path / "interactions.jsonl"
    interactions.start(path)
    return path


def test_prompt_question_and_answer_are_recorded(log_path: Path, monkeypatch) -> None:
    """问句全文 + 回答摘要 + 回答长度，一行一条。"""
    _feed(monkeypatch, ["  红裙女孩  "])
    assert wizard._prompt("请输入角色名：") == "红裙女孩"

    rows = _rows(log_path)
    assert len(rows) == 1
    row = rows[0]
    # 字段形状即契约：六个键，一个不多一个不少
    assert set(row) == {"ts", "event", "flow", "question", "answer", "answer_len"}
    assert row["event"] == "prompt"
    assert row["flow"] == "wizard"  # 缺省流
    assert row["question"] == "请输入角色名："
    assert row["answer"] == "红裙女孩"
    assert row["answer_len"] == 4
    assert row["ts"]


def test_confirm_reuses_the_prompt_seam(log_path: Path, monkeypatch) -> None:
    """确认函数复用提问函数，所以 [Y/n] 的问句与回答照样落盘。"""
    _feed(monkeypatch, ["n"])
    assert wizard._confirm("确认跳过？", default=True) is False

    row = _rows(log_path)[0]
    assert row["question"] == "确认跳过？ [Y/n]: "
    assert row["answer"] == "n"


def test_flow_context_labels_question_rows(log_path: Path, monkeypatch) -> None:
    """``flow`` 由上下文管理器设置，退出后回到外层（缺省 wizard）。"""
    _feed(monkeypatch, ["1", "2", "3", "4"])
    wizard._prompt("外层")
    with interactions.flow("asset_loop"):
        wizard._prompt("内层")
        with interactions.flow("delivery"):
            wizard._prompt("更深")
    wizard._prompt("回到外层")

    assert [r["flow"] for r in _rows(log_path)] == [
        "wizard", "asset_loop", "delivery", "wizard"]


@pytest.mark.parametrize(("answer", "summary"), [
    ("  红裙   女孩\n", "红裙 女孩"),                       # 折叠空白
    ("D:/素材库/人物/红裙-1.png", "红裙-1.png"),              # 正斜杠路径只留 basename
    (r"D:\素材库\人物\红裙-1.png", "红裙-1.png"),             # 反斜杠路径同理
    ("", ""),                                            # 空串与「没答」区分：记 ""
])
def test_answer_summary_rules(log_path: Path, monkeypatch, answer: str,
                              summary: str) -> None:
    _feed(monkeypatch, [answer])
    wizard._prompt("Q")
    row = _rows(log_path)[0]
    assert row["answer"] == summary
    # 回答长度记**用户实际提交的**（strip 后）长度，不是摘要长度
    assert row["answer_len"] == len(answer.strip())


def test_long_body_answer_content_is_not_recorded(log_path: Path, monkeypatch) -> None:
    """长正文类答案不记内容：只留长度，避免把用户素材写成日志资产。"""
    body = "很长的修改意见" * 20
    _feed(monkeypatch, [body])
    wizard._prompt("请输入修改意见：")

    row = _rows(log_path)[0]
    assert row["answer"] == ""
    assert row["answer_len"] == len(body)


def test_prompt_without_started_log_is_silent_noop(tmp_path: Path, monkeypatch,
                                                   capsys) -> None:
    """日志未启动（测试、阶段外调用）：不产生文件、不打印、不影响流程。"""
    interactions.stop()
    _feed(monkeypatch, ["1"])
    assert wizard._prompt("Q") == "1"
    assert interactions.path is None
    assert not list(tmp_path.rglob("interactions.jsonl"))
    assert capsys.readouterr().err == ""


def test_start_failure_warns_once_and_does_not_interrupt(tmp_path: Path,
                                                        monkeypatch, capsys) -> None:
    """写盘失败只警告一次；提问照常返回。"""
    blocker = tmp_path / "blocker"
    blocker.write_text("不是目录", encoding="utf-8")
    assert interactions.start(blocker / "interactions.jsonl") is None

    _feed(monkeypatch, ["1", "2"])
    assert wizard._prompt("Q1") == "1"
    assert wizard._prompt("Q2") == "2"
    assert capsys.readouterr().err.count("无法写入") == 1


def test_row_write_failure_warns_once_without_raising(log_path: Path,
                                                      monkeypatch, capsys) -> None:
    """行写入失败（序列化/IO 错）只警告一次，不打断流程。"""
    from minimax_h3_prompt import observability

    def boom(*_args, **_kwargs):
        raise ValueError("坏行")

    monkeypatch.setattr(observability.json, "dumps", boom)
    interactions.prompt("Q1", "1")
    interactions.prompt("Q2", "2")

    err = capsys.readouterr().err
    assert err.count("写交互日志失败") == 1


# ---------------------------------------------------------------------------
# 整段素材循环（票面 AC1）：盘上能找到「同脸人审问句」与用户回答
# ---------------------------------------------------------------------------

_TOPIC = "红裙女孩走进秋日庭院，把青铜钥匙交给蓝衫少年。"


def _ref_session_with_one_asset(sessions_root: Path) -> Path:
    """写一份「阶段 1 已完成、等交图」的多参会话：队列里只有一张人物图。"""
    from minimax_h3_prompt.brief_parser import Brief
    from minimax_h3_prompt.session_store import STATUS_AWAITING_FRAMES, save_session

    brief = Brief(mode="ref", variant="T2VA", duration=5.0, plot=_TOPIC)
    state = {
        "script": _TOPIC,
        "character_image_prompts": [{
            "name": "红裙女孩",
            "positive_prompt": "红裙、黑发、银项链",
            "negative_prompt": "",
            "locked_features": [],
        }],
    }
    directory = sessions_root / "红裙女孩-abc12345" / "GEN001"
    save_session(directory, brief, state, status=STATUS_AWAITING_FRAMES)
    return directory


def test_asset_loop_review_question_and_answer_land_on_disk(tmp_path: Path,
                                                            monkeypatch) -> None:
    """假输入跑一段素材循环：同脸人审问句与回答写进与会话目录同级的日志。"""
    from types import SimpleNamespace

    cfg = SimpleNamespace(
        default_duration=5.0, default_language="Chinese",
        sessions_root=tmp_path / "sessions", max_clarification_rounds=0,
    )
    directory = _ref_session_with_one_asset(cfg.sessions_root)
    monkeypatch.setattr(wizard, "_drain_stdin", lambda: None)
    monkeypatch.setattr(wizard.sys, "stdin", SimpleNamespace(isatty=lambda: True))
    # 续接确认（y）→ 选序号（回车=1）→ 审查菜单（q=先到这里）
    _feed(monkeypatch, ["y", "", "q"])

    assert wizard.run_wizard(cfg) == 0

    log = directory.parent / "interactions.jsonl"
    assert log.is_file()
    rows = _rows(log)
    review = [r for r in rows if "同角色的其他母版是不是同一张脸" in r["question"]]
    assert len(review) == 1
    assert review[0]["event"] == "prompt"
    assert review[0]["flow"] == "asset_loop"
    assert review[0]["answer"] == "q"


# ---------------------------------------------------------------------------
# 告警落盘（票面 AC4）：生图纪律与语言混排各一条具名事件
# ---------------------------------------------------------------------------


def test_image_discipline_warning_is_a_named_notice(log_path: Path) -> None:
    """生图纪律告警写成 ``notice`` 行，事件名可 grep。"""
    from minimax_h3_prompt.graph import nodes

    nodes.print_image_prompt_warnings("不要出现文字", "z_image", "[警告] 测试：")

    notices = [r for r in _rows(log_path) if r["event"] == "notice"]
    assert len(notices) == 1
    assert notices[0]["question"].startswith("[image_prompt_discipline]")
    assert "负向" in notices[0]["question"]
    assert notices[0]["answer"] == ""
    assert notices[0]["answer_len"] == 0


def test_mixed_language_warning_is_a_named_notice(log_path: Path) -> None:
    """语言混排告警写成 ``notice`` 行（#162 的兜底告警是它的唯一出口）。"""
    from minimax_h3_prompt.ui.wizard_proofread import merged_language_warning

    warning = merged_language_warning(
        "A girl in a red dress 红裙女孩走进庭院 walks slowly.",
        base="A girl in a red dress walks slowly.",
    )
    assert warning is not None

    notices = [r for r in _rows(log_path) if r["event"] == "notice"]
    assert len(notices) == 1
    assert notices[0]["question"].startswith("[language_mixed]")
    assert "红裙女孩走进庭院" in notices[0]["question"]

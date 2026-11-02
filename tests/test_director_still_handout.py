"""导演台 fl2v 收图：首帧必填、尾帧可选，且尾帧**不挂第 1 段**。

**接缝**：``ui.wizard._collect_director_opening_still``——阶段 1 收尾唯一收图入口。
断言的是外部行为：问几次、落哪个文件、跳过时留没留下东西。

2026-10-30 实跑现场（用户报告）：
- 向导打印「fl2v 的开场尾帧（end.png）是可选项」，随后却对 FL2VA 无条件必填、空输入
  重问、没有跳过路径（文案与行为相反）；
- 展示给用户的 ``last`` 提示词是**全片 20 秒**的终点画面，而打包把它挂到**第 1 段**
  的 ``endImage``——4 段各 5 秒时，第 1 段被要求从雾中远景直接跑到隧道深处。

修法参考首尾帧路线（不是照抄）：那里的「尾帧」是**末段**落到的画面
（``segment_prompts.py:666``），且收帧可跳过、只是要二次确认
（``wizard._ask_optional_path``）。
"""
from __future__ import annotations

from pathlib import Path

from minimax_h3_prompt.brief_parser import Brief
from minimax_h3_prompt.session_store import SessionState
from minimax_h3_prompt.ui import wizard


def _director_brief() -> Brief:
    return Brief(mode="director", variant="T2VA", director_mode="fl2v",
                 duration=20, plot="雾中山谷，绿皮火车驶入隧道",
                 language="Chinese")


def _session(tmp_path) -> SessionState:
    directory = tmp_path / "GEN001"
    directory.mkdir(parents=True, exist_ok=True)
    return SessionState(directory=directory, brief=_director_brief(),
                        stage_state={}, status="awaiting_frames")


def _still(tmp_path, name: str) -> Path:
    path = tmp_path / name
    path.write_bytes(b"\x89PNG\r\n\x1a\n")
    return path


def _patch_prompts(monkeypatch, answers: list[str], confirms: list[bool]):
    """按顺序喂「路径输入」与「确认」；记录被问到的题面。"""
    asked: list[str] = []
    queue = list(answers)
    confirm_queue = list(confirms)

    def fake_prompt(text: str = "") -> str:
        asked.append(text)
        return queue.pop(0)

    def fake_confirm(text: str = "", default: bool = False) -> bool:
        return confirm_queue.pop(0)

    monkeypatch.setattr(wizard, "_prompt", fake_prompt)
    monkeypatch.setattr(wizard, "_confirm", fake_confirm)
    # 修订循环会真调模型：这条用例只测收图，不测修订。
    monkeypatch.setattr(wizard, "_user_revision_loop", lambda *a, **k: "")
    return asked


def test_tail_frame_can_be_skipped_with_a_second_confirmation(tmp_path, monkeypatch):
    """尾帧空输入 → 二次确认后跳过，且**不留下任何**尾帧文件。

    跳过是合法选择：多段 fl2v 的最后一段终点画面由用户在导演台里挂，开场段只需要首帧。
    """
    session = _session(tmp_path)
    first = _still(tmp_path, "first.png")
    _patch_prompts(monkeypatch, answers=[str(first), ""], confirms=[True])

    assert wizard._collect_director_opening_still(session, config=None) is True

    assets = session.directory / "director" / "assets"
    assert (assets / "01" / "start.png").is_file()
    assert not (assets / "01" / "end.png").exists(), "跳过尾帧后仍写了第 1 段的 end"
    assert not (assets / "film-end.png").exists(), "跳过尾帧后仍写了全片尾帧"


def test_tail_frame_lands_in_the_film_end_slot_not_segment_one(tmp_path, monkeypatch):
    """尾帧落到 ``assets/film-end``，**不落** ``assets/01/end``。

    落错地方就是那个实跑错误的根：``assets/01/end`` 会被打包成第 1 段的终点画面，
    而用户给的是全片尾帧。全片尾帧属于最后一段（首尾帧路线的「末段落尾帧」口径）。
    """
    session = _session(tmp_path)
    first = _still(tmp_path, "first.png")
    last = _still(tmp_path, "last.png")
    _patch_prompts(monkeypatch, answers=[str(first), str(last)], confirms=[])

    assert wizard._collect_director_opening_still(session, config=None) is True

    assets = session.directory / "director" / "assets"
    assert (assets / "film-end.png").is_file(), "全片尾帧没落到 film-end"
    assert not (assets / "01" / "end.png").exists(), \
        "全片尾帧被写到第 1 段的 end 槽（会变成第 1 段的终点画面）"


def test_first_frame_stays_required(tmp_path, monkeypatch):
    """首帧空输入**重问**（缺了插件会拒包），不可跳过。"""
    session = _session(tmp_path)
    first = _still(tmp_path, "first.png")
    asked = _patch_prompts(monkeypatch, answers=["", str(first), ""], confirms=[True])

    assert wizard._collect_director_opening_still(session, config=None) is True

    assert sum("首帧" in text for text in asked) == 2, "首帧空输入没有重问"
    assert (session.directory / "director" / "assets" / "01" / "start.png").is_file()

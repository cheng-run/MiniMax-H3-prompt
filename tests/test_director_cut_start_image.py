"""P1：切镜段的尾帧词必须从**本段首帧**写起（2026-11-02 验收问题汇总）。

文档口径：`prompts/tailframe_writer_director.md:8`「参考图就是本段的起点画面」。
引导段里「本段起点」＝上一段终点，两种取法等价；`camera_cut` 段起点在别处，
必须取用户挂在该段**首帧槽**的那张图。

缝：`ui.wizard._write_director_tailframe` 传给 `write_tailframe_prompts` 的
``start_image``——这正是写词模型看到的参考图，外部可观察（落盘文件与模型请求都从它来）。

变异检验：把 `_write_director_tailframe` 改回「只认上一段终点图」→ 第一条用例红。
"""
from __future__ import annotations

from pathlib import Path

import pytest



def _segment(index, segment_type, start, end, declared, export, guided, give_images=()):
    from minimax_h3_prompt.director.frames import DirectorSegment

    return DirectorSegment(
        index=index, type=segment_type, guided=guided,
        start_frame=start, end_frame=end,
        declared_frames=declared, export_frames=export,
        summary=f"段 {index} 剧情", end_hook=f"段 {index} 落定画面",
        give_images=give_images,
    )


def _plan():
    from minimax_h3_prompt.director.frames import DirectorPlan, give_images_for

    segments = (
        _segment(1, "opening", 0, 107, 107, 107, False,
                 give_images_for("opening", "fl2v")),
        _segment(2, "camera_cut", 107, 260, 141, 153, False,
                 give_images_for("camera_cut", "fl2v")),
        _segment(3, "ending", 260, 413, 141, 153, True,
                 give_images_for("ending", "fl2v")),
    )
    return DirectorPlan(mode="fl2v", total_frames=413, segments=segments)


def _session(tmp_path: Path):
    from minimax_h3_prompt.brief_parser import Brief
    from minimax_h3_prompt.session_store import SessionState

    brief = Brief(mode="director", director_mode="fl2v", duration=20.0, plot="测试")
    return SessionState(
        directory=tmp_path, brief=brief, stage_state={}, status="director_running")


def _capture(monkeypatch):
    """把写词调用截下来，只记 start_image；返回记录字典。"""
    from minimax_h3_prompt.director import tailframe

    recorded: dict = {}

    def fake_write(plan, *, llm, session_dir, start_image, identity_baseline=None):
        recorded["start_image"] = Path(start_image)
        recorded["index"] = plan.segments[0].index
        return []

    monkeypatch.setattr(tailframe, "write_tailframe_prompts", fake_write)
    return recorded


def test_camera_cut_tailframe_uses_its_own_first_image(tmp_path, monkeypatch):
    from minimax_h3_prompt.director import ledger
    from minimax_h3_prompt.ui import wizard

    plan = _plan()
    segment = plan.segments[1]  # camera_cut
    cut_first = tmp_path / "cut-first.png"
    cut_first.write_bytes(b"first")
    prev_end = tmp_path / "prev-end.png"
    prev_end.write_bytes(b"end")

    asked: list[str] = []

    def fake_prompt(question: str):
        asked.append(question)
        return str(cut_first)

    monkeypatch.setattr(wizard, "_prompt", fake_prompt)
    monkeypatch.setattr(
        ledger, "passed_director_rows", lambda directory: {1: {"end_image": str(prev_end)}})
    recorded = _capture(monkeypatch)

    ok = wizard._write_director_tailframe(plan, segment, _session(tmp_path), llm=object())

    assert ok is True
    assert any("首帧" in q for q in asked), "切镜段没有问本段首帧路径"
    assert recorded["start_image"] == cut_first, (
        "切镜段的尾帧词参考图仍是上一段终点图，不是本段首帧")


def test_camera_cut_first_image_path_is_required_and_reasks(tmp_path, monkeypatch):
    """#149：切镜首帧路径必填——空输入讲清后果后重问，不退回上一段终点图。"""
    from minimax_h3_prompt.director import ledger
    from minimax_h3_prompt.ui import wizard

    plan = _plan()
    segment = plan.segments[1]
    cut_first = tmp_path / "cut-first.png"
    cut_first.write_bytes(b"first")
    prev_end = tmp_path / "prev-end.png"
    prev_end.write_bytes(b"end")
    answers = ["", str(cut_first)]

    asked: list[str] = []

    def fake_prompt(question: str):
        asked.append(question)
        return answers.pop(0)

    monkeypatch.setattr(wizard, "_prompt", fake_prompt)
    monkeypatch.setattr(
        ledger, "passed_director_rows", lambda directory: {1: {"end_image": str(prev_end)}})
    recorded = _capture(monkeypatch)

    assert wizard._write_director_tailframe(
        plan, segment, _session(tmp_path), llm=object()) is True

    assert len(asked) == 2, "空输入没有重问"
    assert recorded["start_image"] == cut_first, "重问后仍没把本段首帧交给写词"


def test_guided_segment_keeps_using_the_previous_end_image(tmp_path, monkeypatch):
    from minimax_h3_prompt.director import ledger
    from minimax_h3_prompt.ui import wizard

    plan = _plan()
    segment = plan.segments[2]  # ending：引导段，起点＝上一段终点
    prev_end = tmp_path / "prev-end.png"
    prev_end.write_bytes(b"end")

    asked: list[str] = []

    def fake_prompt(question: str):
        asked.append(question)
        return ""

    monkeypatch.setattr(wizard, "_prompt_optional_image", fake_prompt)
    monkeypatch.setattr(
        ledger, "passed_director_rows", lambda directory: {2: {"end_image": str(prev_end)}})
    recorded = _capture(monkeypatch)

    ok = wizard._write_director_tailframe(plan, segment, _session(tmp_path), llm=object())

    assert ok is True
    assert not any("首帧" in q for q in asked), "引导段不该问本段首帧"
    assert recorded["start_image"] == prev_end


def test_ending_prefers_the_film_end_still_and_skips_the_tailframe_word(
        tmp_path, monkeypatch, capsys):
    """#149：盘上有全片尾帧时，ending 直接挂它——不再产出尾帧词，旧词注明不采用。"""
    from minimax_h3_prompt.director import tailframe
    from minimax_h3_prompt.director.tailframe import tailframe_path
    from minimax_h3_prompt.ui import wizard

    plan = _plan()
    ending = plan.segments[2]
    assets = tmp_path / "director" / "assets"
    assets.mkdir(parents=True)
    film_end = assets / "film-end.png"
    film_end.write_bytes(b"film end")
    old_word = tailframe_path(tmp_path, ending.index)
    old_word.parent.mkdir(parents=True, exist_ok=True)
    old_word.write_text("旧的尾帧词", encoding="utf-8")

    def fail_write(*args, **kwargs):  # 全片尾帧优先时不应再调用写词
        raise AssertionError("ending 已有全片尾帧，却仍然产出尾帧词")

    monkeypatch.setattr(tailframe, "write_tailframe_prompts", fail_write)

    assert wizard._write_director_tailframe(
        plan, ending, _session(tmp_path), llm=object()) is True

    out = capsys.readouterr().out
    assert "全片尾帧" in out and str(film_end) in out
    assert "不采用" in out and str(old_word) in out, "旧尾帧词没有注明不采用"
    assert "旧的尾帧词" not in out, "旧词正文不该再被当作可复制内容打出来"


def test_ending_without_film_end_still_writes_the_tailframe_word(tmp_path, monkeypatch):
    """没有全片尾帧时才走尾帧词——film-end 优先不能把正常路径也跳掉。"""
    from minimax_h3_prompt.director import ledger
    from minimax_h3_prompt.ui import wizard

    plan = _plan()
    ending = plan.segments[2]
    prev_end = tmp_path / "prev-end.png"
    prev_end.write_bytes(b"end")
    monkeypatch.setattr(ledger, "passed_director_rows",
                        lambda directory: {2: {"end_image": str(prev_end)}})
    monkeypatch.setattr(wizard, "_prompt_optional_image", lambda question: "")
    recorded = _capture(monkeypatch)

    assert wizard._write_director_tailframe(
        plan, ending, _session(tmp_path), llm=object()) is True
    assert recorded["start_image"] == prev_end


# ---------------------------------------------------------------------------
# #153：按需词的模型 provenance——换 frame: 后不静默复用旧档
# ---------------------------------------------------------------------------

def _plant_meta(prompt_path, model: str = "qwen") -> None:
    from minimax_h3_prompt.director.frame_meta import write_frame_meta

    write_frame_meta(prompt_path, {
        "kind": "firstframe" if prompt_path.name.startswith("firstframe") else "tailframe",
        "segment": 2, "model": model,
    })


def test_firstframe_written_by_another_model_warns_and_keeps_by_default(
        tmp_path, monkeypatch, capsys):
    from minimax_h3_prompt import image_config
    from minimax_h3_prompt.director import firstframe
    from minimax_h3_prompt.ui import wizard

    plan = _plan()
    segment = plan.segments[1]
    path = firstframe.firstframe_path(tmp_path, segment.index)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("旧的段首帧词", encoding="utf-8")
    _plant_meta(path, model="qwen")
    monkeypatch.setattr(image_config, "load_frame_image_model", lambda: "moss")
    asked: list[tuple[str, bool]] = []

    def fake_confirm(text: str, default: bool = True) -> bool:
        asked.append((text, default))
        return False

    monkeypatch.setattr(wizard, "_confirm", fake_confirm)
    monkeypatch.setattr(
        firstframe, "write_firstframe_prompt",
        lambda *args, **kwargs: pytest.fail("默认 N 时不该重写段首帧词"))

    text = wizard._cut_first_frame_prompt(
        plan, segment, _session(tmp_path), llm=object())

    assert text.strip() == "旧的段首帧词", "默认 N 必须沿用旧词"
    out = capsys.readouterr().out
    assert "qwen" in out and "moss" in out, "警告没有说清旧档与当前档"
    assert asked and asked[0][1] is False, "「重写？」必须默认 N"


def test_confirming_the_rewrite_regenerates_the_firstframe_word(tmp_path, monkeypatch):
    from minimax_h3_prompt import image_config
    from minimax_h3_prompt.director import firstframe
    from minimax_h3_prompt.ui import wizard

    plan = _plan()
    segment = plan.segments[1]
    path = firstframe.firstframe_path(tmp_path, segment.index)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("旧的段首帧词", encoding="utf-8")
    _plant_meta(path, model="qwen")
    monkeypatch.setattr(image_config, "load_frame_image_model", lambda: "moss")
    monkeypatch.setattr(wizard, "_confirm", lambda text, default=True: True)
    calls: list[str] = []

    def fake_write(*args, **kwargs):
        calls.append("rewritten")
        return "新的段首帧词"

    monkeypatch.setattr(firstframe, "write_firstframe_prompt", fake_write)

    text = wizard._cut_first_frame_prompt(
        plan, segment, _session(tmp_path), llm=object())

    assert text == "新的段首帧词"
    assert calls == ["rewritten"]


def test_new_firstframe_word_prints_the_frame_model(tmp_path, monkeypatch, capsys):
    """#144 C / US18：新写出来的段首帧词在终端标明按哪一档出词。"""
    from minimax_h3_prompt import image_config
    from minimax_h3_prompt.director import firstframe
    from minimax_h3_prompt.ui import wizard

    plan = _plan()
    segment = plan.segments[1]
    monkeypatch.setattr(image_config, "load_frame_image_model", lambda: "moss")
    monkeypatch.setattr(
        firstframe, "write_firstframe_prompt",
        lambda *args, **kwargs: "A rider steps into the new scene.")

    wizard._cut_first_frame_prompt(plan, segment, _session(tmp_path), llm=object())

    out = capsys.readouterr().out
    assert "本次按 MOSS-Image（moss）档出词" in out, "新段首帧词没打印出图档名"


def test_reused_firstframe_word_prints_its_recorded_model(tmp_path, monkeypatch, capsys):
    """沿用盘上旧词时打印 sidecar 里**写时**的档，而不是当前配置档（#144 C）。

    当前 ``frame:`` 已换成 MOSS：终端若按配置印，用户会拿 Qwen 写的词去开 MOSS 工作流。
    """
    from minimax_h3_prompt import image_config
    from minimax_h3_prompt.director import firstframe
    from minimax_h3_prompt.ui import wizard

    plan = _plan()
    segment = plan.segments[1]
    path = firstframe.firstframe_path(tmp_path, segment.index)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("旧的段首帧词", encoding="utf-8")
    _plant_meta(path, model="qwen")
    monkeypatch.setattr(image_config, "load_frame_image_model", lambda: "moss")
    monkeypatch.setattr(wizard, "_confirm", lambda text, default=True: False)

    wizard._cut_first_frame_prompt(plan, segment, _session(tmp_path), llm=object())

    out = capsys.readouterr().out
    assert "本次按 Qwen-Image 2.1（qwen）档出词" in out, \
        "沿用的旧段首帧词没打印它写时的档名"
    assert "本次按 MOSS" not in out, "旧词被标成了当前配置的档"


def test_tailframe_written_by_another_model_warns_and_keeps_by_default(
        tmp_path, monkeypatch, capsys):
    from minimax_h3_prompt import image_config
    from minimax_h3_prompt.director import tailframe
    from minimax_h3_prompt.ui import wizard

    plan = _plan()
    ending = plan.segments[2]
    existing = tailframe.tailframe_path(tmp_path, ending.index)
    existing.parent.mkdir(parents=True, exist_ok=True)
    existing.write_text(
        "### 变换指令\n推进。\n\n### 保留项声明\n灰色风衣\n\n### 锁死清单\n- 灰色风衣\n",
        encoding="utf-8")
    _plant_meta(existing, model="qwen")
    monkeypatch.setattr(image_config, "load_frame_image_model", lambda: "moss")
    asked: list[tuple[str, bool]] = []

    def fake_confirm(text: str, default: bool = True) -> bool:
        asked.append((text, default))
        return False

    monkeypatch.setattr(wizard, "_confirm", fake_confirm)
    monkeypatch.setattr(
        tailframe, "write_tailframe_prompts",
        lambda *args, **kwargs: pytest.fail("默认 N 时不该重写尾帧词"))

    assert wizard._write_director_tailframe(
        plan, ending, _session(tmp_path), llm=object()) is True

    out = capsys.readouterr().out
    assert "qwen" in out and "moss" in out
    assert asked and asked[0][1] is False, "「重写？」必须默认 N"
    assert "重写" in asked[0][0]


# ---------------------------------------------------------------------------
# #150：终端打印本次出词的档名（正常路径与沿用旧词路径都要有）
# ---------------------------------------------------------------------------

def _written_item(tmp_path, *, model: str, index: int = 3):
    from minimax_h3_prompt.director.tailframe import WrittenTailframe, tailframe_path

    text = ("### 变换指令\n以这张输入图为参考：镜头落在中景。\n\n"
            "### 保留项声明\n保持输入图中的人物身份不变。\n\n"
            "### 锁死清单\n- 保持输入图中的人物身份不变\n")
    path = tailframe_path(tmp_path, index)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return WrittenTailframe(
        index=index, type="ending", path=path, text=text,
        transform="以这张输入图为参考：镜头落在中景。",
        preserved="保持输入图中的人物身份不变。",
        locked=("保持输入图中的人物身份不变",), issues=(), model=model)


def test_fresh_tailframe_document_prints_the_frame_model(
        tmp_path, monkeypatch, capsys):
    """#150 AC2：新写出来的尾帧词要在终端标明按哪一档出词。"""
    from minimax_h3_prompt.director import ledger, tailframe
    from minimax_h3_prompt.ui import wizard

    plan = _plan()
    ending = plan.segments[2]
    prev_end = tmp_path / "prev-end.png"
    prev_end.write_bytes(b"end")
    monkeypatch.setattr(ledger, "passed_director_rows",
                        lambda directory: {2: {"end_image": str(prev_end)}})
    monkeypatch.setattr(wizard, "_prompt_optional_image", lambda question: "")
    monkeypatch.setattr(
        tailframe, "write_tailframe_prompts",
        lambda *args, **kwargs: [_written_item(tmp_path, model="moss")])

    assert wizard._write_director_tailframe(
        plan, ending, _session(tmp_path), llm=object()) is True

    out = capsys.readouterr().out
    assert "MOSS-Image" in out and "moss" in out, "新尾帧词没打印出图档名"


def test_existing_tailframe_document_prints_its_recorded_model(
        tmp_path, monkeypatch, capsys):
    """#150 AC2：沿用盘上旧词时也要打印它**写时**的档名（sidecar 里有）。"""
    from minimax_h3_prompt.director import ledger
    from minimax_h3_prompt.ui import wizard

    plan = _plan()
    ending = plan.segments[2]
    existing = _written_item(tmp_path, model="qwen").path
    _plant_meta(existing, model="qwen")
    monkeypatch.setattr(ledger, "passed_director_rows",
                        lambda directory: {2: {"end_image": str(tmp_path / "x.png")}})

    assert wizard._write_director_tailframe(
        plan, ending, _session(tmp_path), llm=object()) is True

    out = capsys.readouterr().out
    assert "Qwen-Image 2.1" in out and "qwen" in out, \
        "沿用旧尾帧词时没打印它写时的档名"



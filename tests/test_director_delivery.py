"""导演台交付（issue #108）：成片记录、最终包归档、改动比对与向导收尾。

缝（设计 §5「缝 3」）：

- ``director/delivery.py`` 的公开入口——``deliver`` 做编排，``record_final_film`` /
  ``archive_final_pack`` / ``read_initial_timeline`` / ``read_pack_timeline`` /
  ``compare_prompts`` 各自也能单测；
- 段产出账里的 ``rewrite`` 行（唯一写口仍是 ``ref_segments.log_segment_rewrite_round``）；
- ``ui.wizard._finish_director_delivery``（全段通过之后的那一步）。

不联网、不跑 ComfyUI。**归档那一步必须真造一个 zip**：只有真文件才证得了「复制的是
同一份字节、校验值是现算的」；手写 JSON 能测比对，测不了归档。
"""
from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# 夹具：段表、初始包、最终包
# ---------------------------------------------------------------------------

def _plan(*, mode: str = "t2v"):
    """两段：开场（无引导，声明＝导出＝107）＋推进（有引导，声明 141 / 导出 153）。

    两套坐标在这里**天然不同**：Σ 声明 248、Σ 导出 260（差 12 ＝ 一段有引导，
    设计 §3.2）。成片记录取后者；取前者会让成片长度短 12 帧且没人报错。
    """
    from minimax_h3_prompt.director.frames import DirectorPlan, DirectorSegment

    segments = (
        DirectorSegment(index=1, type="opening", guided=False, start_frame=0,
                        end_frame=107, declared_frames=107, export_frames=107,
                        summary="开场", end_hook="门框", give_images=()),
        DirectorSegment(index=2, type="continue", guided=True, start_frame=107,
                        end_frame=260, declared_frames=141, export_frames=153,
                        summary="推进", end_hook="桌面", give_images=()),
    )
    return DirectorPlan(mode=mode, total_frames=260, segments=segments)


def _session_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "GEN001"
    (directory / "director").mkdir(parents=True, exist_ok=True)
    return directory


def _film_file(tmp_path: Path) -> Path:
    path = tmp_path / "final-film.mp4"
    path.write_bytes(b"not really an mp4, but it exists")
    return path


DEFAULT_PROMPTS = ("雨夜，她拆开旧信。", "镜头推近，信纸上的字迹。")


def _timeline(prompts, *, mode: str = "t2v", shot_prompts=None,
              frame_counts=None) -> dict:
    """一份最小 ``timeline.json``（只放比对用得到的键）。

    ``totalFrames`` 按**声明坐标**写（248）：导演台显示的就是它，正是「不能拿它当
    成片帧数」的那个诱饵。
    """
    rows = []
    for position, prompt in enumerate(prompts):
        row = {"id": f"seg{position + 1:02d}", "prompt": prompt}
        if frame_counts is not None:
            row["frameCount"] = frame_counts[position]
        rows.append(row)
    timeline = {
        "version": 5, "timelineMode": "prompt_batch", "totalFrames": 248,
        "segments": rows,
    }
    if shot_prompts is not None:
        timeline["timelineMode"] = "fl2v"
        timeline["shots"] = [
            {"id": f"seg{position + 1:02d}", "prompt": prompt}
            for position, prompt in enumerate(shot_prompts)
        ]
    return timeline


def _write_initial_pack(session_dir: Path, timeline: dict) -> Path:
    path = session_dir / "director" / "pack" / "timeline.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(timeline, ensure_ascii=False), encoding="utf-8")
    return path


def _write_pack_zip(path: Path, timeline: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("pack.json", json.dumps(
            {"format": "minimax-h3-director-pack", "formatVersion": 1}))
        archive.writestr("timeline.json",
                         json.dumps(timeline, ensure_ascii=False))
    return path


def _ready_session(tmp_path: Path, *, initial=None, mode: str = "t2v"):
    """造一个「初始包已落盘」的会话目录，返回 (段目录, 段表, 成片路径, 最终包路径)。"""
    from minimax_h3_prompt.director import delivery

    session_dir = _session_dir(tmp_path)
    plan = _plan(mode=mode)
    _write_initial_pack(session_dir, initial or _timeline(DEFAULT_PROMPTS))
    film = _film_file(tmp_path)
    pack_zip = _write_pack_zip(tmp_path / "exported" / "导演台最终包.zip",
                               _timeline(DEFAULT_PROMPTS))
    return session_dir, plan, film, pack_zip


# ---------------------------------------------------------------------------
# AC1：全段通过后记录成片（成片坐标，不是时间线的声明坐标）
# ---------------------------------------------------------------------------

def test_deliver_records_the_film_in_export_coordinates(tmp_path):
    """``total_frames`` 取 Σ 导出帧（260），**不是**时间线的 Σ 声明帧（248）。

    两套坐标差 ``12×有引导段数``（设计 §3.2）。写错一整套都不会报错，只会在成片长度
    与账上对不上时让人以为「导演台少导了几帧」。
    """
    from minimax_h3_prompt.director import delivery

    session_dir, plan, film, pack_zip = _ready_session(tmp_path)

    delivery.deliver(session_dir, plan=plan, film_path=film, pack_zip=pack_zip)

    record = json.loads(delivery.final_film_path(session_dir).read_text(encoding="utf-8"))
    assert record["total_frames"] == 260
    assert record["total_frames"] != 248, "不能拿 timeline 的声明坐标当片子长度"
    assert record["segments"] == 2
    assert Path(record["path"]) == film.resolve()
    assert record["at"], "记录必须带时间（哪一次交付记的）"


def test_a_missing_film_is_named_and_nothing_is_written(tmp_path):
    """成片路径不存在 → ``ValueError`` 点名，且**不落任何交付产物**（票面 AC4）。

    落半份交付比没落更坏：用户会以为已经交付过，重跑时看到 final-film.json 就不再核对。
    """
    from minimax_h3_prompt.director import delivery

    session_dir, plan, _film, pack_zip = _ready_session(tmp_path)
    missing = tmp_path / "nope" / "final.mp4"

    with pytest.raises(ValueError) as excinfo:
        delivery.deliver(session_dir, plan=plan, film_path=missing, pack_zip=pack_zip)
    assert "final.mp4" in str(excinfo.value)
    assert not delivery.final_film_path(session_dir).exists()
    assert not delivery.final_pack_path(session_dir).exists()
    assert not delivery.final_sha256_path(session_dir).exists()


# ---------------------------------------------------------------------------
# AC2：最终包归档 + 现算校验值
# ---------------------------------------------------------------------------

def test_deliver_copies_the_final_pack_and_writes_a_fresh_checksum(tmp_path):
    """复制到 ``director/final.mmxpack.zip`` 并写 ``final.sha256``：同一份字节、现算。"""
    from minimax_h3_prompt.director import delivery

    session_dir, plan, film, pack_zip = _ready_session(tmp_path)
    source_bytes = pack_zip.read_bytes()

    report = delivery.deliver(session_dir, plan=plan, film_path=film, pack_zip=pack_zip)

    archived = delivery.final_pack_path(session_dir)
    assert archived.read_bytes() == source_bytes, "归档必须是逐字节的同一份包"
    digest = hashlib.sha256(source_bytes).hexdigest()
    text = delivery.final_sha256_path(session_dir).read_text(encoding="utf-8")
    # 形如 ``sha256sum`` 的一行：摘要 + 空格 + 文件名。抄别的工具的输出时不用解释格式。
    assert text.split()[0] == digest
    assert text.split()[-1] == archived.name
    assert report.pack_sha256 == digest


def test_a_non_zip_final_pack_is_rejected(tmp_path):
    """最终包不是 zip → 点名报错；不许「先归档再说」（票面 AC4）。"""
    from minimax_h3_prompt.director import delivery

    session_dir, plan, film, _pack_zip = _ready_session(tmp_path)
    broken = tmp_path / "broken.mmxpack.zip"
    broken.write_bytes(b"this is not a zip")

    with pytest.raises(ValueError) as excinfo:
        delivery.deliver(session_dir, plan=plan, film_path=film, pack_zip=broken)
    assert "broken.mmxpack.zip" in str(excinfo.value)
    assert not delivery.final_pack_path(session_dir).exists()
    assert not delivery.final_film_path(session_dir).exists()


# ---------------------------------------------------------------------------
# AC3：读最终包比对，补出改写账行
# ---------------------------------------------------------------------------

def _rewrite_rows(session_dir: Path) -> list[dict]:
    from minimax_h3_prompt.ref_segments import (
        SEGMENT_EVENT_REWRITE, read_segment_ledger,
    )

    return read_segment_ledger(session_dir, event=SEGMENT_EVENT_REWRITE)


def test_a_changed_prompt_appends_one_rewrite_row(tmp_path):
    """UI 里改过第 2 段 → 一行 ``rewrite``：改前取初始包、改后取最终包。"""
    from minimax_h3_prompt.director import delivery

    session_dir, plan, film, _ = _ready_session(tmp_path)
    final = _timeline((DEFAULT_PROMPTS[0], "镜头缓缓推近，信纸边角烧焦。"))
    pack_zip = _write_pack_zip(tmp_path / "exported2" / "final.mmxpack.zip", final)

    report = delivery.deliver(session_dir, plan=plan, film_path=film,
                              pack_zip=pack_zip)

    rows = _rewrite_rows(session_dir)
    assert len(rows) == 1
    row = rows[0]
    assert row["segment"] == 2
    assert row["prompt_before"] == DEFAULT_PROMPTS[1]
    assert row["prompt_after"] == "镜头缓缓推近，信纸边角烧焦。"
    assert delivery.REWRITE_FEEDBACK in row["feedback"]
    assert row["round"] == 1
    assert [change.segment for change in report.changes] == [2]


def test_identical_prompts_append_nothing(tmp_path):
    """词没改就不落行：这本账只记真实发生过的改动。"""
    from minimax_h3_prompt.director import delivery

    session_dir, plan, film, pack_zip = _ready_session(tmp_path)

    delivery.deliver(session_dir, plan=plan, film_path=film, pack_zip=pack_zip)

    assert _rewrite_rows(session_dir) == []


def test_rewrite_rows_join_on_the_segment_id_not_the_array_position(tmp_path):
    """**join 键是 id**：最终包里段顺序被 UI 换了，差异仍须记到正确的那一段。

    变异检验——把 ``compare_prompts`` 改成按下标比，这条用例立刻变红（会把第 2 段的
    改动记成第 1 段）。UI 删段/重排是正常操作，下标比对没有任何断言看得见它错。
    """
    from minimax_h3_prompt.director import delivery

    session_dir, plan, film, _ = _ready_session(tmp_path)
    reordered = {
        "version": 5, "timelineMode": "prompt_batch",
        "segments": [
            {"id": "seg02", "prompt": "镜头缓缓推近，信纸边角烧焦。"},
            {"id": "seg01", "prompt": DEFAULT_PROMPTS[0]},
        ],
    }
    pack_zip = _write_pack_zip(tmp_path / "exported2" / "final.mmxpack.zip",
                               reordered)

    delivery.deliver(session_dir, plan=plan, film_path=film, pack_zip=pack_zip)

    rows = _rewrite_rows(session_dir)
    assert [row["segment"] for row in rows] == [2], "按 id 比对，改的是第 2 段"
    assert rows[0]["prompt_before"] == DEFAULT_PROMPTS[1]


def test_fl2v_compares_the_shots_prompts_too(tmp_path):
    """fl2v：``segments`` 没变、``shots`` 变了 → 仍必须补一行改写账。

    变异检验——只比 ``segments`` 时这条用例变红。UI 以 shots 为准（插件勘察 §8），
    用户在 fl2v 界面里改的词正是从这条路漏掉的。
    """
    from minimax_h3_prompt.director import delivery

    session_dir = _session_dir(tmp_path)
    plan = _plan(mode="fl2v")
    initial = _timeline(DEFAULT_PROMPTS, shot_prompts=DEFAULT_PROMPTS)
    _write_initial_pack(session_dir, initial)
    film = _film_file(tmp_path)
    final = _timeline(
        DEFAULT_PROMPTS,
        shot_prompts=(DEFAULT_PROMPTS[0], "推近，信纸边的火苗窜起。"))
    pack_zip = _write_pack_zip(tmp_path / "exported" / "final.mmxpack.zip", final)

    delivery.deliver(session_dir, plan=plan, film_path=film, pack_zip=pack_zip)

    rows = _rewrite_rows(session_dir)
    assert [row["segment"] for row in rows] == [2]
    assert rows[0]["prompt_after"] == "推近，信纸边的火苗窜起。"


def test_frame_count_differences_do_not_produce_rewrite_rows(tmp_path):
    """**不比帧数**：最终包的 ``frameCount`` 会被 UI 归一化改过，不代表用户手改。"""
    from minimax_h3_prompt.director import delivery

    session_dir, plan, film, _ = _ready_session(tmp_path)
    initial = _timeline(DEFAULT_PROMPTS, frame_counts=(107, 141))
    _write_initial_pack(session_dir, initial)
    final = _timeline(DEFAULT_PROMPTS, frame_counts=(107, 153))
    pack_zip = _write_pack_zip(tmp_path / "exported2" / "final.mmxpack.zip", final)

    delivery.deliver(session_dir, plan=plan, film_path=film, pack_zip=pack_zip)

    assert _rewrite_rows(session_dir) == []


def test_rewrite_rows_do_not_count_as_director_events(tmp_path):
    """``rewrite`` **不进** ``DIRECTOR_EVENTS``：混进去会把已通过的段判成未通过。

    ``passed_director_rows`` 取的是「最后一条 output/invalidate 行」。``rewrite`` 若被
    算进来，续跑会看到最后一条是 rewrite 从而认为该段没通过——整整一段重跑的重烧。
    """
    from minimax_h3_prompt.director import delivery, ledger
    from minimax_h3_prompt.ref_segments import SEGMENT_EVENT_REWRITE

    session_dir, plan, film, _ = _ready_session(tmp_path)
    frames = tmp_path / "cache" / "seg_0000.frames.mkv"
    frames.parent.mkdir(parents=True, exist_ok=True)
    frames.write_bytes(b"pretend frames")
    ledger.log_director_output(
        session_dir, segment=1, start_frame=0, end_frame=107,
        declared_frames=107, export_frames=107, actual_frames=107,
        cache_path=frames)

    final = _timeline(("雨夜，她拆开旧信（改）。", DEFAULT_PROMPTS[1]))
    pack_zip = _write_pack_zip(tmp_path / "exported2" / "final.mmxpack.zip", final)
    delivery.deliver(session_dir, plan=plan, film_path=film, pack_zip=pack_zip)

    assert SEGMENT_EVENT_REWRITE not in ledger.DIRECTOR_EVENTS
    assert sorted(ledger.passed_director_rows(session_dir)) == [1]


# ---------------------------------------------------------------------------
# AC4：最终包缺失 / 损坏 / 不一致一律点名报错
# ---------------------------------------------------------------------------

def test_a_pack_without_timeline_json_is_named(tmp_path):
    from minimax_h3_prompt.director import delivery

    session_dir, plan, film, _ = _ready_session(tmp_path)
    pack_zip = tmp_path / "no-timeline" / "final.mmxpack.zip"
    pack_zip.parent.mkdir(parents=True)
    with zipfile.ZipFile(pack_zip, "w") as archive:
        archive.writestr("pack.json", "{}")

    with pytest.raises(ValueError) as excinfo:
        delivery.deliver(session_dir, plan=plan, film_path=film, pack_zip=pack_zip)
    assert "timeline.json" in str(excinfo.value)
    assert not delivery.final_pack_path(session_dir).exists()


def test_a_corrupt_timeline_json_is_named(tmp_path):
    from minimax_h3_prompt.director import delivery

    session_dir, plan, film, _ = _ready_session(tmp_path)
    pack_zip = tmp_path / "corrupt" / "final.mmxpack.zip"
    pack_zip.parent.mkdir(parents=True)
    with zipfile.ZipFile(pack_zip, "w") as archive:
        archive.writestr("timeline.json", "{not json")

    with pytest.raises(ValueError) as excinfo:
        delivery.deliver(session_dir, plan=plan, film_path=film, pack_zip=pack_zip)
    assert "timeline.json" in str(excinfo.value)


def test_a_different_segment_count_is_refused(tmp_path):
    """段数不一致 → 报错，不静默只比前 N 段（少一段的差异会被当成「没改」）。"""
    from minimax_h3_prompt.director import delivery

    session_dir, plan, film, _ = _ready_session(tmp_path)
    one_segment = _timeline((DEFAULT_PROMPTS[0],))
    pack_zip = _write_pack_zip(tmp_path / "exported2" / "final.mmxpack.zip",
                               one_segment)

    with pytest.raises(ValueError) as excinfo:
        delivery.deliver(session_dir, plan=plan, film_path=film, pack_zip=pack_zip)
    message = str(excinfo.value)
    assert "段数" in message and "1" in message and "2" in message
    assert not delivery.final_film_path(session_dir).exists()


def test_an_initial_pack_for_another_plan_is_refused(tmp_path):
    """初始包与段表段数不一致 → 报错（拿错包当「改前」会算出假的改写行）。"""
    from minimax_h3_prompt.director import delivery

    session_dir, plan, film, pack_zip = _ready_session(tmp_path)
    _write_initial_pack(session_dir, _timeline((DEFAULT_PROMPTS[0],)))

    with pytest.raises(ValueError) as excinfo:
        delivery.deliver(session_dir, plan=plan, film_path=film, pack_zip=pack_zip)
    assert "段表" in str(excinfo.value)


# ---------------------------------------------------------------------------
# 向导收尾（设计 §4）：逐项问路径 → 交付 → 成功才置 completed
# ---------------------------------------------------------------------------

def _wizard_session(tmp_path: Path, *, mode: str = "t2v"):
    """一个「陪跑已全通过、状态仍是 running」的会话（交付前的那一格）。"""
    from minimax_h3_prompt.brief_parser import Brief
    from minimax_h3_prompt.session_store import (
        STATUS_DIRECTOR_SEGMENTS_RUNNING, SessionState, save_session,
    )

    session_dir = _session_dir(tmp_path)
    plan = _plan(mode=mode)
    _write_initial_pack(session_dir, _timeline(DEFAULT_PROMPTS))
    brief = Brief(mode="director", variant="T2VA", director_mode=mode,
                  duration=30.0, plot="雨夜旧信",
                  director_node_id="42", director_cache_name="雨夜",
                  director_continuity_mode="guide")
    save_session(session_dir, brief, {}, status=STATUS_DIRECTOR_SEGMENTS_RUNNING)
    session = SessionState(directory=session_dir, brief=brief, stage_state={},
                           status=STATUS_DIRECTOR_SEGMENTS_RUNNING)
    return session, plan


def test_finish_delivery_asks_both_paths_and_completes_the_session(
        tmp_path, monkeypatch, capsys):
    """先问成片、再问最终包；路径不存在就报错重问；交付成功后会话置 completed。"""
    from minimax_h3_prompt.director import delivery
    from minimax_h3_prompt.session_store import STATUS_COMPLETED, load_session
    from minimax_h3_prompt.ui import wizard

    session, plan = _wizard_session(tmp_path)
    film = _film_file(tmp_path)
    pack_zip = _write_pack_zip(tmp_path / "exported" / "final.mmxpack.zip",
                               _timeline(("雨夜，她拆开旧信（改）。",
                                          DEFAULT_PROMPTS[1])))
    answers = iter([str(tmp_path / "没有这个.mp4"), str(film), str(pack_zip)])
    asked: list[str] = []

    def fake_prompt(text: str) -> str:
        asked.append(text)
        return next(answers)

    monkeypatch.setattr(wizard, "_prompt", fake_prompt)

    assert wizard._finish_director_delivery(plan, session) == 0

    assert "成片 mp4" in asked[0] and "最终包" in asked[-1], "先问成片，再问最终包"
    assert len(asked) == 3, "第一个答案不存在 → 报错重问一次（票面裁定）"
    out = capsys.readouterr().out
    assert "不存在" in out and "没有这个.mp4" in out
    assert delivery.final_film_path(session.directory).is_file()
    assert delivery.final_pack_path(session.directory).is_file()
    loaded = load_session(session.directory)
    assert loaded is not None and loaded.status == STATUS_COMPLETED


def test_a_broken_final_pack_keeps_the_session_resumable(
        tmp_path, monkeypatch, capsys):
    """交付失败返回非零且**不置完成**：用户要能重进这条会话（重进靠的就是那个状态）。"""
    from minimax_h3_prompt.session_store import (
        STATUS_DIRECTOR_SEGMENTS_RUNNING, load_session,
    )
    from minimax_h3_prompt.ui import wizard

    session, plan = _wizard_session(tmp_path)
    film = _film_file(tmp_path)
    broken = tmp_path / "broken.mmxpack.zip"
    broken.write_bytes(b"not a zip")
    answers = iter([str(film), str(broken)])
    monkeypatch.setattr(wizard, "_prompt", lambda text: next(answers))

    assert wizard._finish_director_delivery(plan, session) == 1

    assert "[错误]" in capsys.readouterr().out
    loaded = load_session(session.directory)
    assert loaded is not None
    assert loaded.status == STATUS_DIRECTOR_SEGMENTS_RUNNING


def test_q_before_the_paths_leaves_the_session_resumable(tmp_path, monkeypatch, capsys):
    """输入 q 停在交付前：返回 0、不置完成（已通过的段留在账上，重跑接着来）。"""
    from minimax_h3_prompt.session_store import (
        STATUS_DIRECTOR_SEGMENTS_RUNNING, load_session,
    )
    from minimax_h3_prompt.ui import wizard

    session, plan = _wizard_session(tmp_path)
    monkeypatch.setattr(wizard, "_prompt", lambda text: "q")

    assert wizard._finish_director_delivery(plan, session) == 0

    assert "停在交付前" in capsys.readouterr().out
    loaded = load_session(session.directory)
    assert loaded is not None
    assert loaded.status == STATUS_DIRECTOR_SEGMENTS_RUNNING

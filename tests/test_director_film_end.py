"""全片尾帧读图注入（#160）：取描述一个口、sidecar 复用、规划与写段注入。

缝（与票面 AC 一一对应，均为**外部可观察行为**）：

1. ``director.film_end.get_film_end_description``——读图一次、落 sidecar、按图指纹复用；
2. 规划请求与写段请求里真的出现了描述（记录型桩模型抓出站字符串）；
3. 收集全片尾帧时读图失败要停下，且不落 sidecar。

sidecar 与图同目录（``director/assets/film-end.meta.json``），字段沿用
``director/frame_meta.py`` 的机制；视觉调用一律打桩（出网守卫会拦真实请求）。
"""
from __future__ import annotations

import json
import re

import pytest

from minimax_h3_prompt.brief_parser import Brief
from minimax_h3_prompt.session_store import SessionState

# 断言用的描述：模型图与段词冲突时正是靠这句话把「回头看」拉回「往前走」。
_DESCRIPTION = "一个男人背对镜头，沿积水的小路走向院门，没有回头。"


def _image(session_dir):
    path = session_dir / "director" / "assets" / "film-end.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"first-version-of-the-film-end-image")
    return path


def _sidecar(session_dir):
    return session_dir / "director" / "assets" / "film-end.meta.json"


def _session(tmp_path, mode="fl2v"):
    directory = tmp_path / "GEN001"
    directory.mkdir(parents=True, exist_ok=True)
    return SessionState(
        directory=directory,
        brief=Brief(mode="director", variant="T2VA", director_mode=mode,
                    duration=20, plot="快过年返乡", language="Chinese"),
        stage_state={}, status="awaiting_frames")


def _patch_describe(monkeypatch, behaviour):
    """打桩唯一视觉调用口；返回记录每次调用的列表。"""
    from minimax_h3_prompt.tools import reference_auditor

    calls: list = []

    def fake_describe(path, instruction=None):
        calls.append(path)
        return behaviour(len(calls), path)

    monkeypatch.setattr(reference_auditor, "describe_image", fake_describe)
    return calls


# ---------------------------------------------------------------------------
# 取描述的一个口：读一次、sidecar、指纹复用
# ---------------------------------------------------------------------------

def test_first_read_writes_a_sidecar_next_to_the_image(tmp_path, monkeypatch):
    """首次取描述：读图一次，sidecar 与图同目录，五个字段齐全。"""
    from minimax_h3_prompt.config import config
    from minimax_h3_prompt.director.frame_meta import sha256_file
    from minimax_h3_prompt.director.film_end import get_film_end_description

    session = _session(tmp_path)
    image = _image(session.directory)
    calls = _patch_describe(monkeypatch, lambda _n, _p: _DESCRIPTION)

    assert get_film_end_description(session.directory) == _DESCRIPTION
    assert calls == [image], "首次取描述应当读图一次，且读的就是盘上那张图"

    meta_path = _sidecar(session.directory)
    assert meta_path.is_file(), "sidecar 必须与图同目录落 film-end.meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    assert meta["kind"]
    assert meta["description"] == _DESCRIPTION
    assert meta["vision_model"] == config.vision_model.model, \
        "视觉模型名必须从配置现取并落盘（事后可追溯是哪个模型读的）"
    assert meta["image_sha256"] == sha256_file(image), "sidecar 要带图指纹"
    assert meta["at"], "落盘时间不能空"


def test_matching_fingerprint_reuses_the_sidecar_without_reading_again(
        tmp_path, monkeypatch):
    """图没换：第二次取描述不重读图（续跑不重复付费）。"""
    from minimax_h3_prompt.director.film_end import get_film_end_description

    session = _session(tmp_path)
    _image(session.directory)
    calls = _patch_describe(monkeypatch, lambda _n, _p: _DESCRIPTION)
    assert get_film_end_description(session.directory) == _DESCRIPTION
    assert len(calls) == 1, "首次取描述只读一次图"

    def _boom(_n, _p):
        raise AssertionError("图没换又读了图：续跑重复付费")

    monkeypatch.setattr(
        "minimax_h3_prompt.tools.reference_auditor.describe_image", _boom)
    assert get_film_end_description(session.directory) == _DESCRIPTION


def test_changed_fingerprint_rereads_and_overwrites_the_sidecar(tmp_path, monkeypatch):
    """图换了：指纹变 → 重读并覆盖 sidecar（旧描述不许继续约束段词）。"""
    from minimax_h3_prompt.director.frame_meta import sha256_file
    from minimax_h3_prompt.director.film_end import get_film_end_description

    session = _session(tmp_path)
    image = _image(session.directory)
    calls = _patch_describe(monkeypatch, lambda _n, _p: _DESCRIPTION)
    assert get_film_end_description(session.directory) == _DESCRIPTION

    image.write_bytes(b"second-version-of-the-film-end-image")
    calls = _patch_describe(monkeypatch, lambda _n, _p: "新的终点画面：男人已经站在院门里。")
    assert get_film_end_description(session.directory) == "新的终点画面：男人已经站在院门里。"
    assert len(calls) == 1, "指纹变了必须重读"

    meta = json.loads(_sidecar(session.directory).read_text(encoding="utf-8"))
    assert meta["description"] == "新的终点画面：男人已经站在院门里。"
    assert meta["image_sha256"] == sha256_file(image), "覆盖后的指纹要跟上新图"


def test_corrupt_sidecar_is_treated_as_missing_and_reread(tmp_path, monkeypatch):
    """坏 sidecar 按缺失处理：重读并修好，不把续跑拦死。"""
    from minimax_h3_prompt.director.film_end import get_film_end_description

    session = _session(tmp_path)
    _image(session.directory)
    _sidecar(session.directory).write_text("{not json", encoding="utf-8")
    calls = _patch_describe(monkeypatch, lambda _n, _p: _DESCRIPTION)

    assert get_film_end_description(session.directory) == _DESCRIPTION
    assert len(calls) == 1
    meta = json.loads(_sidecar(session.directory).read_text(encoding="utf-8"))
    assert meta["description"] == _DESCRIPTION


def test_visual_call_failure_raises_and_leaves_no_sidecar(tmp_path, monkeypatch):
    """视觉调用失败：抛错（带图路径与病因），且**不落** sidecar。"""
    from minimax_h3_prompt.director.film_end import get_film_end_description

    session = _session(tmp_path)
    image = _image(session.directory)

    def fail(_path, instruction=None):
        raise RuntimeError("网关 502")

    monkeypatch.setattr(
        "minimax_h3_prompt.tools.reference_auditor.describe_image", fail)

    with pytest.raises(RuntimeError) as excinfo:
        get_film_end_description(session.directory)
    message = str(excinfo.value)
    assert str(image) in message, "错误里要点名是哪张图"
    assert "网关 502" in message, "原始错误不能吞掉"
    assert not _sidecar(session.directory).exists(), "读图失败不许留下 sidecar"


def test_no_film_end_image_returns_none_without_reading(tmp_path, monkeypatch):
    """没有全片尾帧的模式/会话：不读图、不落盘（其它模式没有这张图）。"""
    from minimax_h3_prompt.director.film_end import get_film_end_description

    session = _session(tmp_path, mode="t2v")
    calls = _patch_describe(monkeypatch, lambda _n, _p: _DESCRIPTION)

    assert get_film_end_description(session.directory) is None
    assert calls == []
    assert not _sidecar(session.directory).exists()


# ---------------------------------------------------------------------------
# 注入：规划请求与写段请求都带上同一份描述
# ---------------------------------------------------------------------------

_PLAN_JSON = """{"segments": [
  {"start_s": 0, "end_s": 6, "type": "opening", "summary": "男人走出屋门", "end_hook": "他站在门口"},
  {"start_s": 6, "end_s": 12, "type": "continue", "summary": "沿小路走向院门", "end_hook": "他走到路中间"},
  {"start_s": 12, "end_s": 18, "type": "ending", "summary": "走到院门前", "end_hook": "他停在院门前"}
]}"""

_SINGLE_BODY = """{note}
integrated_multimodal_description:
[Shot 1] A man walks away from the camera along a wet path toward a courtyard gate.

overall_soundscape:
Footsteps on wet ground.

non_diegetic_music:
N/A
"""


class _RecordLLM:
    """记录出站请求的桩模型：规划回段表 JSON，写段回一版合格正文。"""

    def __init__(self):
        self.requests: list[str] = []

    def invoke(self, request, config=None):
        text = str(request)
        self.requests.append(text)
        if "分段规划师" in text:
            return _PLAN_JSON
        index = int(re.search(r"Segment (\d+)/", text).group(1))
        segment = _plan().segments[index - 1]
        from minimax_h3_prompt.director.prompts import render_timeline_note

        return _SINGLE_BODY.format(note=render_timeline_note(_plan(), segment))


def _plan():
    from minimax_h3_prompt.director.frames import DirectorPlan, DirectorSegment

    segments = (
        DirectorSegment(index=1, type="opening", guided=False, start_frame=0,
                        end_frame=107, declared_frames=107, export_frames=107,
                        summary="男人走出屋门", end_hook="他站在门口",
                        give_images=("first",)),
        DirectorSegment(index=2, type="continue", guided=True, start_frame=107,
                        end_frame=260, declared_frames=141, export_frames=153,
                        summary="沿小路走向院门", end_hook="他走到路中间",
                        give_images=()),
        DirectorSegment(index=3, type="ending", guided=True, start_frame=260,
                        end_frame=413, declared_frames=141, export_frames=153,
                        summary="走到院门前", end_hook="他停在院门前",
                        give_images=()),
    )
    return DirectorPlan(mode="fl2v", total_frames=413, segments=segments)


def test_planner_request_carries_the_description_and_the_image_wins_rule():
    """段规划请求带上全片尾帧描述，并写明「段词与图冲突时，以图为准」。"""
    from minimax_h3_prompt.director.planning import plan_director_segments

    llm = _RecordLLM()
    plan_director_segments("分镜表：[Shot 1] 男人走到院门前，回头望向院门。", 18.0, llm,
                           mode="fl2v", film_end_description=_DESCRIPTION)

    request = llm.requests[0]
    assert _DESCRIPTION in request, "规划师没看到全片尾帧的画面描述"
    assert "以图为准" in request, "没有写清段词与图冲突时以图为准"
    assert "段词" in request and "图" in request


def test_planner_does_not_inject_for_other_modes_or_without_a_description():
    """只有 fl2v 且真有描述时才注入：其它模式没有这张图，空描述也不该占位。"""
    from minimax_h3_prompt.director.planning import plan_director_segments

    llm = _RecordLLM()
    plan_director_segments("分镜表：[Shot 1] ……", 18.0, llm,
                           mode="fl2v", film_end_description="")
    assert "全片终点画面" not in llm.requests[0]

    llm = _RecordLLM()
    plan_director_segments("分镜表：[Shot 1] ……", 18.0, llm,
                           mode="t2v", film_end_description=_DESCRIPTION)
    assert _DESCRIPTION not in llm.requests[0], "t2v 没有全片尾帧，不该注入"


def test_planning_wording_also_covers_the_single_segment_fl2v_case():
    """#157 复审：规划最后一段的措辞不许写死 ``ending``。

    单段 fl2v 的整片只有一段，类型是 ``opening``，它同样是这段的终点；写死
    ``ending`` 会让模型以为「最后一段」在单段时不存在。
    """
    from minimax_h3_prompt.director.film_end import render_film_end_block

    block = render_film_end_block(_DESCRIPTION, for_planning=True)
    assert "ending" in block, "多段时最后一段是 ending"
    assert "opening" in block, "单段 fl2v 的最后一段是 opening，措辞必须覆盖它"

    # 写段那一半说的是「写本段正文」，不该自称在规划最后一段。
    writer = render_film_end_block(_DESCRIPTION)
    assert "写本段正文" in writer
    assert "规划最后一段" not in writer


def test_writer_injects_the_description_only_into_the_ending_request(tmp_path):
    """写段：只有 ending 段的请求带描述；前面的段不重复这份终点约束。"""
    from minimax_h3_prompt.director.prompts import write_director_segments

    llm = _RecordLLM()
    write_director_segments(_plan(), llm=llm, session_dir=tmp_path,
                            film_end=True, film_end_description=_DESCRIPTION)

    # 三个请求：段 1 / 段 2 / 段 3（ending）
    assert len(llm.requests) == 3, "每个段各一次请求"
    ending_request = llm.requests[2]
    assert _DESCRIPTION in ending_request, "ending 段的写段请求没带全片尾帧描述"
    assert "以图为准" in ending_request
    for request in llm.requests[:2]:
        assert _DESCRIPTION not in request, "非结尾段不该带全片终点约束"


def test_single_segment_fl2v_opening_carries_the_description(tmp_path):
    """单段 fl2v：opening 就是整片的那一段，film_end=True 时也要带描述。"""
    from minimax_h3_prompt.director.frames import DirectorPlan, DirectorSegment
    from minimax_h3_prompt.director.prompts import write_director_segments

    segment = DirectorSegment(
        index=1, type="opening", guided=False, start_frame=0, end_frame=107,
        declared_frames=107, export_frames=107, summary="男人走出屋门",
        end_hook="他停在院门前", give_images=("first",))
    plan = DirectorPlan(mode="fl2v", total_frames=107, segments=(segment,))

    class _SingleLLM(_RecordLLM):
        def invoke(self, request, config=None):
            text = str(request)
            self.requests.append(text)
            from minimax_h3_prompt.director.prompts import render_timeline_note

            return _SINGLE_BODY.format(note=render_timeline_note(plan, segment))

    llm = _SingleLLM()
    write_director_segments(plan, llm=llm, session_dir=tmp_path,
                            film_end=True, film_end_description=_DESCRIPTION)
    assert _DESCRIPTION in llm.requests[0]
    assert "以图为准" in llm.requests[0]


def test_rewrite_request_keeps_the_same_description():
    """e 改词也是写段请求：同一段要带同一份描述，改一次不能把终点约束改丢。"""
    from minimax_h3_prompt.director.prompts import build_rewrite_request

    plan = _plan()
    ending = plan.segments[2]
    request = build_rewrite_request(
        plan, ending, current_text="[Shot 1] 他停在院门前。",
        film_end=True, film_end_description=_DESCRIPTION)
    assert _DESCRIPTION in request, "改词请求丢了全片尾帧描述"
    assert "以图为准" in request


# ---------------------------------------------------------------------------
# 收集全片尾帧时读图：成功落 sidecar；失败停下且不落盘
# ---------------------------------------------------------------------------

def _patch_collect_inputs(monkeypatch, answers, confirms=()):
    from minimax_h3_prompt.ui import wizard

    queue = list(answers)
    confirm_queue = list(confirms)

    monkeypatch.setattr(wizard, "_prompt", lambda text="": queue.pop(0))
    monkeypatch.setattr(wizard, "_confirm",
                        lambda text="", default=False: confirm_queue.pop(0))
    monkeypatch.setattr(wizard, "_user_revision_loop", lambda *a, **k: "")


def test_collecting_the_film_end_reads_it_once_and_caches(tmp_path, monkeypatch, capsys):
    """收全片尾帧时读一次图：落 sidecar，并把描述打给用户。"""
    from minimax_h3_prompt.ui import wizard

    session = _session(tmp_path)
    first = tmp_path / "first.png"
    first.write_bytes(b"\x89PNG\r\n\x1a\n")
    last = tmp_path / "last.png"
    last.write_bytes(b"film-end-bytes")
    _patch_collect_inputs(monkeypatch, [str(first), str(last)])
    calls = _patch_describe(monkeypatch, lambda _n, _p: _DESCRIPTION)

    assert wizard._collect_director_opening_still(session, config=None) is True
    assert len(calls) == 1, "收集时应当读图一次"
    assert _sidecar(session.directory).is_file()
    out = capsys.readouterr().out
    assert _DESCRIPTION in out, "读到的终点画面描述要打给用户"


def test_collecting_the_film_end_stops_when_the_visual_call_fails(
        tmp_path, monkeypatch, capsys):
    """视觉调用失败：打印图路径与错误、返回 False、不落 sidecar（不带着猜的终点往下跑）。"""
    from minimax_h3_prompt.ui import wizard

    session = _session(tmp_path)
    first = tmp_path / "first.png"
    first.write_bytes(b"\x89PNG\r\n\x1a\n")
    last = tmp_path / "last.png"
    last.write_bytes(b"film-end-bytes")
    _patch_collect_inputs(monkeypatch, [str(first), str(last)])

    def fail(_path, instruction=None):
        raise RuntimeError("视觉网关 502")

    monkeypatch.setattr(
        "minimax_h3_prompt.tools.reference_auditor.describe_image", fail)

    assert wizard._collect_director_opening_still(session, config=None) is False
    out = capsys.readouterr().out
    assert str(session.directory / "director" / "assets" / "film-end.png") in out, \
        "失败时要打印是哪张图"
    assert "视觉网关 502" in out
    assert not _sidecar(session.directory).exists(), "失败不许落 sidecar"


# ---------------------------------------------------------------------------
# 向导流程：续跑跳过收集时，规划前仍经同一个口取描述
# ---------------------------------------------------------------------------

def _stub_flow_tail(monkeypatch, llm):
    """打桩打包与陪跑：本组用例的接缝是「取描述 → 规划 → 写段」的先后与内容。"""
    from types import SimpleNamespace

    from minimax_h3_prompt import model_factory
    from minimax_h3_prompt.director import pack as pack_module
    from minimax_h3_prompt.ui import wizard

    monkeypatch.setattr(model_factory, "build_chat_model", lambda: llm)
    monkeypatch.setattr(
        pack_module, "prepare_director_pack",
        lambda *args, **kwargs: (SimpleNamespace(), "导演包已构建（本用例打桩）"))
    monkeypatch.setattr(
        wizard, "_run_director_segment_handoff", lambda *args, **kwargs: 0)
    monkeypatch.setattr(wizard, "_drain_stdin", lambda: None)
    # #158 的画幅预检在本机配置下会超差（关键帧 1920×1088 vs 声明 736×1280），
    # 回车继续即可；本票不测预检，别的问题都不该出现。
    monkeypatch.setattr(
        wizard, "_prompt",
        lambda text: "" if "画幅" in text else pytest.fail(f"不该问：{text}"))


def _resumed_fl2v_session(tmp_path):
    """续跑形态：开场首帧已在（收集步骤会被跳过），全片尾帧也在。"""
    from minimax_h3_prompt.session_store import SessionState

    session = _session(tmp_path)
    session.brief.director_node_id = "42"
    session.brief.director_cache_name = ""
    session.brief.director_continuity_mode = "guide"
    # 段表 JSON（_RecordLLM）覆盖 0–18 秒，总时长必须同源，否则规划器按「没收在总时长」拒绝。
    session.brief.duration = 18.0
    session.stage_state["shot_table"] = "分镜表：[Shot 1] 男人走到院门前。"
    _image(session.directory)
    start = session.directory / "director" / "assets" / "01" / "start.png"
    start.parent.mkdir(parents=True, exist_ok=True)
    start.write_bytes(b"start-still")
    return SessionState(directory=session.directory, brief=session.brief,
                        stage_state=session.stage_state, status="awaiting_frames")


def test_wizard_flow_reads_the_description_before_planning_on_resume(
        tmp_path, monkeypatch):
    """收集步骤被跳过（续跑）时，规划前仍经同一个口取描述，规划与写段都带它。"""
    from minimax_h3_prompt.ui import wizard

    session = _resumed_fl2v_session(tmp_path)
    llm = _RecordLLM()
    events: list[str] = []

    original_invoke = llm.invoke

    def recording_invoke(request, config=None):
        text = str(request)
        if "分段规划师" in text:
            events.append("planning")
        return original_invoke(request, config)

    llm.invoke = recording_invoke

    from minimax_h3_prompt.tools import reference_auditor

    def fake_describe(path, instruction=None):
        events.append("vision")
        return _DESCRIPTION

    monkeypatch.setattr(reference_auditor, "describe_image", fake_describe)
    _stub_flow_tail(monkeypatch, llm)

    assert wizard._run_director_flow(session) == 0

    assert events and events[0] == "vision", "续跑时必须在规划之前补读尾帧图"
    assert events.count("vision") == 1, "同一个口只读一次（收集被跳过时由规划前补上）"
    planner_request = next(request for request in llm.requests
                           if "分段规划师" in request)
    assert _DESCRIPTION in planner_request, "规划请求没带终点画面描述"
    assert "以图为准" in planner_request
    ending_request = next(request for request in llm.requests
                          if "Segment 3/3" in request)
    assert _DESCRIPTION in ending_request, "ending 段写段请求没带终点画面描述"
    assert "以图为准" in ending_request
    assert _sidecar(session.directory).is_file()


def test_wizard_flow_stops_before_planning_when_the_description_fails(
        tmp_path, monkeypatch, capsys):
    """读图失败：停在段规划之前——不调模型、不落段表。"""
    from minimax_h3_prompt.director.planning import plan_path
    from minimax_h3_prompt.ui import wizard

    session = _resumed_fl2v_session(tmp_path)
    llm = _RecordLLM()

    def fail(_path, instruction=None):
        raise RuntimeError("视觉网关 502")

    monkeypatch.setattr(
        "minimax_h3_prompt.tools.reference_auditor.describe_image", fail)
    _stub_flow_tail(monkeypatch, llm)

    assert wizard._run_director_flow(session) == 1
    out = capsys.readouterr().out
    assert "全片尾帧读图失败" in out and "视觉网关 502" in out
    assert llm.requests == [], "读图失败后不许再调模型（不规划、不写段）"
    assert not plan_path(session.directory).exists()
    assert not _sidecar(session.directory).exists()


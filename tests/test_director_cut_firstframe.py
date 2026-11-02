"""P2：切镜段要有**段首帧**生图提示词的生产者（2026-11-02 验收问题汇总）。

缝（外部可观察）：
1. ``director.firstframe.shot_excerpt_for_window`` —— 段边界切在镜头中间时，
   起点所在那一镜必须被选中（否则首帧词按下一镜写）；
2. ``build_firstframe_request`` —— 请求里带本段窗口、镜头节选、画布与模型档；
3. ``extract_first_prompt`` —— 从角色回复的 JSON 里取出 first 的正向词；
4. ``write_firstframe_prompt`` —— 落 ``director/firstframe-NN.md``，取不到就不落半份。

变异检验：把 ``firstframe.py`` 删掉／把 handoff 里那一步删掉前，这些用例直接
ImportError／红——它们钉的是「有生产者」这件事本身。
"""
from __future__ import annotations

from pathlib import Path

from minimax_h3_prompt.director.frames import DirectorSegment


def _segment(index=2, start=90, end=192):
    return DirectorSegment(
        index=index, type="camera_cut", guided=False,
        start_frame=start, end_frame=end,
        declared_frames=102, export_frames=102,
        summary="骑手在雨棚下接电话", end_hook="他挂断电话，望向街口",
        give_images=("first", "end"),
    )


_SHOT_TABLE = """# 分镜表
形式：短叙事（20 秒）

**[Shot 1]**
- 机位运动：Tracking Shot。
- 时长与切点：5.0秒。

**[Shot 2]**
- 机位运动：Static Shot。
- 时长与切点：4.0秒；切入 At 00:05.000。

**[Shot 3]**
- 机位运动：Push In。
- 时长与切点：3.0秒；切入 At 00:09.000。
"""


def _brief():
    from minimax_h3_prompt.brief_parser import Brief

    return Brief(mode="director", director_mode="fl2v", duration=20.0,
                 plot="暴雨夜外卖骑手", style="写实")


def test_excerpt_includes_the_shot_containing_the_segment_start():
    from minimax_h3_prompt.director.firstframe import shot_excerpt_for_window

    # 段 3.75–8.0s：起点落在 Shot 1（0–5s）中间，节选必须同时含 Shot 1 与 Shot 2
    excerpt = shot_excerpt_for_window(_SHOT_TABLE, 3.75, 8.0)
    assert "[Shot 1]" in excerpt
    assert "[Shot 2]" in excerpt
    assert "[Shot 3]" not in excerpt


def test_request_carries_the_window_the_excerpt_and_the_model():
    from minimax_h3_prompt.director.firstframe import build_firstframe_request

    request = build_firstframe_request(
        _segment(), shot_table=_SHOT_TABLE, brief=_brief(), model="qwen")
    assert "第 2 段" in request
    assert "3.750s" in request and "8.000s" in request
    assert "[Shot 1]" in request and "[Shot 2]" in request
    assert "qwen" in request
    assert "只输出 `first` 一帧" in request


def test_extract_first_prompt_prefers_the_configured_model_key():
    from minimax_h3_prompt.director.firstframe import extract_first_prompt

    raw = ('```json\n{"first": {"zimage": {"positive_prompt": "错档的词"},'
           ' "qwen": {"positive_prompt": "A live-action photograph of a rider."}}}\n```')
    assert extract_first_prompt(raw, "qwen") == "A live-action photograph of a rider."


def test_extract_first_prompt_accepts_a_flat_object_and_a_list():
    from minimax_h3_prompt.director.firstframe import extract_first_prompt

    assert extract_first_prompt(
        '{"first": {"positive_prompt": "平铺"}}', "qwen") == "平铺"
    assert extract_first_prompt(
        '{"first": [{"positive_prompt": "列表"}]}', "qwen") == "列表"


def test_extract_first_prompt_returns_none_for_garbage():
    from minimax_h3_prompt.director.firstframe import extract_first_prompt

    assert extract_first_prompt("这不是 JSON", "qwen") is None
    assert extract_first_prompt('{"last": {}}', "qwen") is None


def test_write_firstframe_prompt_lands_the_positive_prompt(tmp_path, monkeypatch):
    from minimax_h3_prompt import agents
    from minimax_h3_prompt.director import firstframe

    monkeypatch.setattr(agents, "build_role_agents", lambda *a, **k: {
        "frame_prompt_engineer": object()})
    monkeypatch.setattr(
        agents, "run_agent",
        lambda agent, request: '{"first": {"qwen": {"positive_prompt": "A rider in the rain."}}}')

    text = firstframe.write_firstframe_prompt(
        _segment(), llm=object(), shot_table=_SHOT_TABLE,
        brief=_brief(), session_dir=tmp_path, model="qwen")

    assert text == "A rider in the rain."
    path = firstframe.firstframe_path(tmp_path, 2)
    assert path.is_file()
    assert path.read_text(encoding="utf-8").strip() == "A rider in the rain."


def test_write_firstframe_prompt_writes_nothing_on_unparsable_reply(tmp_path, monkeypatch):
    from minimax_h3_prompt import agents
    from minimax_h3_prompt.director import firstframe

    monkeypatch.setattr(agents, "build_role_agents", lambda *a, **k: {
        "frame_prompt_engineer": object()})
    monkeypatch.setattr(agents, "run_agent", lambda agent, request: "没有 JSON")

    assert firstframe.write_firstframe_prompt(
        _segment(), llm=object(), shot_table=_SHOT_TABLE,
        brief=_brief(), session_dir=tmp_path, model="qwen") is None
    assert not firstframe.firstframe_path(tmp_path, 2).exists()

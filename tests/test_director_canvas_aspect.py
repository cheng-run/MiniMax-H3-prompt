"""#158 画幅判定纯函数：预检与回贴核对共用的唯一一份判据。

真实样本来自 ``docs/research/2026-11-04-质量验收-快过年返乡.md`` §1.1：同一批关键帧
1920×1088，16:9 成片 864×480 只差约 2%（通过），9:16 成片 480×864 差约 218%（失败）。
这两个数就是容差取 5% 的由来，这里直接拿它们当独立真值——不按实现公式重算。
"""
from __future__ import annotations

import pytest

from minimax_h3_prompt.director.aspect import ASPECT_TOLERANCE, check_aspect


def test_real_16x9_video_passes_with_the_acceptance_sample():
    """1920×1088 vs 864×480：比例差约 2%，正常取整差不许报警。"""
    result = check_aspect(1920, 1088, 864, 480)

    assert result.exceeds is False
    # 1.8 / 1.7647 - 1 ≈ 2.0%
    assert result.relative_delta == pytest.approx(0.02, abs=0.001)
    assert result.actual_ratio == pytest.approx(1.7647, abs=0.001)
    assert result.expected_ratio == pytest.approx(1.8, abs=0.001)


def test_real_9x16_video_fails_with_the_acceptance_sample():
    """1920×1088 vs 480×864：比例差约 218%，2026-11-04 验收里锚点被裁的那一类事故。"""
    result = check_aspect(1920, 1088, 480, 864)

    assert result.exceeds is True
    # 1.7647 / 0.5556 - 1 ≈ 217.6%
    assert result.relative_delta == pytest.approx(2.176, abs=0.001)


# ---------------------------------------------------------------------------
# #163（#176）：线性尺寸判据——比例对、尺寸差一档也要能被看见
# ---------------------------------------------------------------------------

def test_size_check_flags_864x480_against_1920x1088():
    """864×480 对 1920×1088：比例只差约 2%，但宽高各差 55% 左右。

    这是票面的第二个验收样本：**比例判据看不出来**，所以尺寸判据必须并列存在。
    """
    from minimax_h3_prompt.director.aspect import SIZE_TOLERANCE, check_size

    result = check_size(864, 480, 1920, 1088)

    assert SIZE_TOLERANCE == 0.20
    assert result.exceeds is True
    # |864-1920|/1920 = 0.55；|480-1088|/1088 ≈ 0.5588
    assert result.width_delta == pytest.approx(0.55, abs=0.001)
    assert result.height_delta == pytest.approx(0.5588, abs=0.001)


def test_size_check_accepts_1920x1080_against_1920x1088():
    """1920×1080 对 1920×1088：宽相同、高只差 8 像素（0.7%），不报。"""
    from minimax_h3_prompt.director.aspect import check_size

    result = check_size(1920, 1080, 1920, 1088)

    assert result.exceeds is False
    assert result.width_delta == pytest.approx(0.0, abs=1e-9)
    assert result.height_delta == pytest.approx(8 / 1088, abs=1e-9)


def test_size_check_boundary_is_strictly_greater_than_the_tolerance():
    """「超 20%」＝严格大于：正好 20% 通过，21% 失败。"""
    from minimax_h3_prompt.director.aspect import check_size

    assert check_size(1200, 1000, 1000, 1000).exceeds is False
    assert check_size(1210, 1000, 1000, 1000).exceeds is True


@pytest.mark.parametrize("size", [(0, 100), (100, 0), (-1, 100)])
def test_size_check_refuses_non_positive_dimensions(size):
    """宽高必须为正：0 或负数不是「读不出」，是调用方传错了。"""
    from minimax_h3_prompt.director.aspect import check_size

    with pytest.raises(ValueError):
        check_size(*size, 100, 100)


# ---------------------------------------------------------------------------
# #176：r2v 的期望值取成片画幅单一真源；比例超差停、尺寸超差一次确认
# ---------------------------------------------------------------------------

def _r2v_brief(size: tuple[int, int] | None = (1920, 1088)):
    """造一份导演台 r2v brief；``size=None`` 模拟没有成片画幅答案的旧会话。"""
    from minimax_h3_prompt.brief_parser import Brief
    from minimax_h3_prompt.director.film_canvas import film_frame_canvases

    canvases = film_frame_canvases(size, "qwen") if size is not None else {}
    return Brief(mode="director", variant="T2VA", director_mode="r2v",
                 duration=10.0, plot="雨夜旧信", canvases=canvases)


def _pin_frame_model(monkeypatch):
    """把关键帧模型钉成 qwen：``film_size_for`` 的默认档读本机配置，测试不能跟着机器跑。"""
    from minimax_h3_prompt import image_config

    monkeypatch.setattr(image_config, "load_frame_image_model", lambda: "qwen")


def _verdict(video, brief, *, ask=None, stage_state=None, segno=1,
             segment_type="opening", directory=None):
    from minimax_h3_prompt.director.aspect_flow import output_aspect_verdict

    return output_aspect_verdict(
        mode="r2v", segment_index=segno, segment_type=segment_type, video=video,
        directory=directory or video.parent, brief=brief, ask=ask,
        stage_state=stage_state)


def test_r2v_matching_film_size_passes(tmp_path, tmp_video, monkeypatch, capsys):
    """r2v 的期望值取向导答的成片画幅：1920×1088 对 1920×1088 不报。"""
    _pin_frame_model(monkeypatch)
    video = tmp_video("film.mp4", width=1920, height=1088, frames=2)

    ok, note = _verdict(video, _r2v_brief())

    assert ok is True
    assert note is None
    assert "画幅" not in capsys.readouterr().out


def test_r2v_size_only_gap_asks_once_and_prints_both_values(
        tmp_path, tmp_video, monkeypatch, capsys):
    """864×480 对 1920×1088：比例差在 5% 内，尺寸差 2.2 倍——一次确认。

    票面要求这一行含两个数与来源；确认后按「模式＋向导值＋实际值」记住，
    同尺寸再核对时不再问。
    """
    from minimax_h3_prompt.director.aspect_flow import FILM_SIZE_CONFIRM_KEY

    _pin_frame_model(monkeypatch)
    video = tmp_video("film.mp4", width=864, height=480, frames=2)
    asked: list[str] = []
    state: dict = {}

    def ask(text: str) -> str:
        asked.append(text)
        return ""

    ok, note = _verdict(video, _r2v_brief(), ask=ask, stage_state=state)

    assert ok is True
    assert note is None
    assert len(asked) == 1, "尺寸差一档只确认一次"
    out = capsys.readouterr().out
    assert "1920×1088" in out and "864×480" in out, "两个数都要打印"
    assert "来源" in out and "成片画幅" in out
    assert "20%" in out, "要告诉用户判的是 20% 尺寸容差"
    assert state[FILM_SIZE_CONFIRM_KEY]["mode"] == "r2v"
    assert state[FILM_SIZE_CONFIRM_KEY]["declared"] == [1920, 1088]
    assert state[FILM_SIZE_CONFIRM_KEY]["actual"] == [864, 480]
    assert state[FILM_SIZE_CONFIRM_KEY]["at"], "确认要留时间"

    # 同模式、同向导值、同实际值 → 沿用确认，不再问。
    ok_again, note_again = _verdict(video, _r2v_brief(), ask=ask, stage_state=state)
    assert (ok_again, note_again) == (True, None)
    assert len(asked) == 1, "确认过的组合不该重问"


def test_r2v_confirmation_is_dropped_when_any_side_changes(
        tmp_path, tmp_video, monkeypatch):
    """任一边数值变化即作废重问：实际值变、向导值变都要重新确认。"""
    from minimax_h3_prompt.director.aspect_flow import FILM_SIZE_CONFIRM_KEY

    _pin_frame_model(monkeypatch)
    first = tmp_video("first.mp4", width=864, height=480, frames=2)
    second = tmp_video("second.mp4", width=1280, height=720, frames=2)
    asked: list[str] = []
    state: dict = {}

    def ask(text: str) -> str:
        asked.append(text)
        return ""

    assert _verdict(first, _r2v_brief(), ask=ask, stage_state=state)[0] is True
    assert len(asked) == 1
    assert state[FILM_SIZE_CONFIRM_KEY]["actual"] == [864, 480]

    # 实际值变了（同比例、不同尺寸）→ 重新问。
    assert _verdict(second, _r2v_brief(), ask=ask, stage_state=state)[0] is True
    assert len(asked) == 2
    assert state[FILM_SIZE_CONFIRM_KEY]["actual"] == [1280, 720]

    # 向导答案变了（实际值不变）→ 重新问。
    assert _verdict(second, _r2v_brief((2560, 1440)),
                    ask=ask, stage_state=state)[0] is True
    assert len(asked) == 3


def test_r2v_size_confirmation_refused_stops_without_remembering(
        tmp_path, tmp_video, monkeypatch, capsys):
    """一次确认被拒绝 → 停；不写确认记录（下次还得重新核对）。"""
    from minimax_h3_prompt.director.aspect_flow import FILM_SIZE_CONFIRM_KEY

    _pin_frame_model(monkeypatch)
    video = tmp_video("film.mp4", width=864, height=480, frames=2)
    state: dict = {}

    ok, note = _verdict(video, _r2v_brief(), ask=lambda text: "n", stage_state=state)

    assert ok is False
    assert note is not None and "停" in note
    assert FILM_SIZE_CONFIRM_KEY not in state


def test_r2v_ratio_gap_stops_without_asking(
        tmp_path, tmp_video, monkeypatch, capsys):
    """比例超差（480×864 对 1920×1088）→ 停止并说明，不给「确认继续」的出口。"""
    _pin_frame_model(monkeypatch)
    video = tmp_video("film.mp4", width=480, height=864, frames=2)

    def ask(text: str) -> str:
        raise AssertionError(f"比例超差不该问确认：{text}")

    ok, note = _verdict(video, _r2v_brief(), ask=ask)

    assert ok is False
    assert note is not None
    assert "1920×1088" in note and "480×864" in note
    assert "来源" in note
    assert "停止" in note


def test_r2v_old_session_without_the_answer_passes_with_one_line(
        tmp_path, tmp_video, monkeypatch):
    """旧会话没有成片画幅答案 → 放行并打一行说明，不拿别的档位硬套。"""
    _pin_frame_model(monkeypatch)
    video = tmp_video("film.mp4", width=864, height=480, frames=2)

    ok, note = _verdict(video, _r2v_brief(None))

    assert ok is True
    assert note is not None and "没有成片画幅" in note and "放行" in note


def test_i2v_size_only_gap_still_passes_without_a_question(
        tmp_path, tmp_video, monkeypatch):
    """i2v／fl2v 的既有核对不变：比例过了就放行，不再追问尺寸差。"""
    import cv2
    import numpy as np

    from minimax_h3_prompt.director.aspect_flow import output_aspect_verdict

    _pin_frame_model(monkeypatch)
    start = tmp_path / "director" / "assets" / "01" / "start.png"
    start.parent.mkdir(parents=True, exist_ok=True)
    assert cv2.imwrite(str(start), np.zeros((1088, 1920, 3), np.uint8))
    video = tmp_video("film.mp4", width=864, height=480, frames=2)

    def ask(text: str) -> str:
        raise AssertionError(f"i2v 不该出现尺寸确认：{text}")

    ok, note = output_aspect_verdict(
        mode="i2v", segment_index=1, segment_type="opening", video=video,
        directory=tmp_path, canvases={}, ask=ask, stage_state={})

    assert (ok, note) == (True, None)


def test_boundary_is_strictly_greater_than_the_tolerance():
    """「超 5%」＝严格大于：正好 5% 通过，6% 失败。"""
    assert ASPECT_TOLERANCE == 0.05
    assert check_aspect(1050, 1000, 1000, 1000).exceeds is False
    assert check_aspect(1060, 1000, 1000, 1000).exceeds is True


@pytest.mark.parametrize("size", [(0, 100), (100, 0), (-1, 100)])
def test_non_positive_dimensions_are_refused(size):
    """宽高必须为正：0 或负数不是「读不出」，是调用方传错了，应当响亮报错。"""
    with pytest.raises(ValueError):
        check_aspect(*size, 100, 100)


def test_precheck_does_not_run_for_r2v_t2v_or_matching_canvases():
    """#158：预检只在 i2v/fl2v 触发；比例相符时不打扰用户。

    t2v 没有关键帧锚点，拿帧生图画布当期望值会把每一部正常竖片都判成超差——本票是
    「导演台路线（i2v／fl2v）」的画幅核对，t2v 与 r2v 一样不参与。
    """
    from minimax_h3_prompt.ui.wizard_director import confirm_aspect

    def _no_question(text: str) -> str:
        raise AssertionError(f"不该提问：{text}")

    assert confirm_aspect(
        "r2v", frame_canvas=(1920, 1088), declared=(736, 1280),
        ask=_no_question) is True
    assert confirm_aspect(
        "t2v", frame_canvas=(1920, 1088), declared=(736, 1280),
        ask=_no_question) is True
    assert confirm_aspect(
        "i2v", frame_canvas=(736, 1280), declared=(736, 1280),
        ask=_no_question) is True


def test_precheck_requires_an_explicit_green_light(capsys):
    """超差时空回车是唯一放行口；``q``/``quit`` 停下；其余输入必须被重问。

    #157 复审：任何非 ``q`` 输入都放行等于把「乱码／n」当成同意——票面要的是
    **显式回车**。这里按序列作答，钉住「重问」这一步，而不只钉返回值。
    """
    from minimax_h3_prompt.ui.wizard_director import confirm_aspect

    asked: list[str] = []
    answers = iter(["继续", "n", ""])

    def ask(text: str) -> str:
        asked.append(text)
        return next(answers)

    assert confirm_aspect(
        "fl2v", frame_canvas=(1920, 1088), declared=(736, 1280),
        ask=ask) is True
    assert len(asked) == 3, "非放行输入必须重问，只有空回车才继续"

    out = capsys.readouterr().out
    assert "1920×1088" in out and "736×1280" in out
    assert "锚点会被裁" in out
    assert "回车" in out and "q" in out, "重问时要告诉用户哪个键放行、哪个键停下"

    for stop in ("q", "Q", "quit", "Quit"):
        assert confirm_aspect(
            "fl2v", frame_canvas=(1920, 1088), declared=(736, 1280),
            ask=lambda text, answer=stop: answer) is False


def test_missing_video_metadata_passes_with_a_warning(tmp_path, capsys):
    """读不出宽高（损坏/不是视频）→ 警告放行，不阻断落账（票面 #158）。"""
    from minimax_h3_prompt.director.aspect_flow import output_aspect_verdict

    broken = tmp_path / "not-a-video.mp4"
    broken.write_text("not a video", encoding="utf-8")

    ok, note = output_aspect_verdict(
        mode="i2v", segment_index=1, segment_type="opening", video=broken,
        directory=tmp_path, canvases={})

    assert ok is True
    assert note is not None and "元信息缺失" in note


def test_cut_segment_anchor_comes_from_the_recorded_first_frame(tmp_path):
    """切镜段的期望画幅来自用户挂过的首帧图（路径落在段首帧 sidecar）——#158。"""
    import cv2
    import numpy as np

    from minimax_h3_prompt.director.aspect_flow import expected_output_frame
    from minimax_h3_prompt.director.firstframe import record_first_frame_image

    image = tmp_path / "cut-first.png"
    assert cv2.imwrite(str(image), np.zeros((1088, 1920, 3), np.uint8))
    record_first_frame_image(tmp_path, 2, image)

    size, source = expected_output_frame(
        mode="fl2v", segment_index=2, segment_type="camera_cut",
        directory=tmp_path, canvases={})

    assert size == (1920, 1088)
    assert "第 2 段首帧图" in source


def test_ending_segment_anchor_is_the_film_end_still(tmp_path):
    """收尾段的期望画幅来自全片尾帧（``director/assets/film-end``）——#158。"""
    import cv2
    import numpy as np

    from minimax_h3_prompt.director.aspect_flow import expected_output_frame

    film_end = tmp_path / "director" / "assets" / "film-end.png"
    film_end.parent.mkdir(parents=True, exist_ok=True)
    assert cv2.imwrite(str(film_end), np.zeros((1088, 1920, 3), np.uint8))

    size, source = expected_output_frame(
        mode="fl2v", segment_index=3, segment_type="ending",
        directory=tmp_path, canvases={})

    assert size == (1920, 1088)
    assert "全片尾帧" in source

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

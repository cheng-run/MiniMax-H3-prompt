"""#179：旧会话按创建时间判定——帧模型切回 z_image 时旧画布不再被当成成片画幅。

``Brief.canvases`` 只有「家族 → [宽, 高]」，不记这格是**关键帧画布**还是**成片画幅**的
答案。旧会话（#161 之前创建）里根本没有成片画幅这一问，那格一律是画布；判新旧只能看
会话的创建时间。这里的时间样本取自真实会话文件（``output/sessions``，只读对照）。
"""
from __future__ import annotations

from datetime import timedelta, timezone

# 真实旧会话样本的 ``created_at``（UTC，早于 #161 的 2026-11-04T06:30:59Z）。
OLD_SESSION = "2026-11-03T13:00:35.825912+00:00"
# #161 之后、画布入口（#369）之前创建的会话：仍放行，但不再叫「旧会话」。
NEW_SESSION = "2026-11-05T01:00:00+00:00"


def _after_canvas_entry() -> str:
    """晚于画布入口截止点的时刻，**从常量现算**（#363 复审 B1）。

    截止点是占位值（用户裁定：上线后改回真实日期），用例若写死日期，常量一挪就
    集体失效；这里加一天再转成会话文件那种 UTC 写法。
    """
    from minimax_h3_prompt.director.film_canvas import FILM_CANVAS_ENTRY_SINCE

    return (FILM_CANVAS_ENTRY_SINCE + timedelta(days=1)).astimezone(
        timezone.utc).isoformat()


def _brief_with(canvases):
    """造一份导演台 r2v brief，画布表按用例给定。"""
    from minimax_h3_prompt.brief_parser import Brief

    return Brief(mode="director", variant="T2VA", director_mode="r2v",
                 duration=10.0, plot="雨夜旧信", canvases=canvases)


def _pin_frame_model(monkeypatch, model: str) -> None:
    """钉住关键帧模型：判据默认档读本机配置，测试不能跟着机器跑。"""
    from minimax_h3_prompt import image_config

    monkeypatch.setattr(image_config, "load_frame_image_model", lambda: model)


def _verdict(video, brief, *, created_at, ask=None, stage_state=None):
    from minimax_h3_prompt.director.aspect_flow import output_aspect_verdict

    return output_aspect_verdict(
        mode="r2v", segment_index=1, segment_type="opening", video=video,
        directory=video.parent, brief=brief, ask=ask, stage_state=stage_state,
        session_created_at=created_at)


def test_legacy_session_zimage_canvas_is_not_compared_as_a_film_aspect(
        tmp_path, tmp_video, monkeypatch):
    """帧模型切回 z_image 后，旧会话那格 1920×1080 是**关键帧画布**，不是成片画幅。

    480×864 对 1920×1080 比例差约 218%：不按创建时间放行就是一次假硬停。
    """
    _pin_frame_model(monkeypatch, "z_image")
    video = tmp_video("film.mp4", width=480, height=864, frames=2)

    ok, note = _verdict(
        video, _brief_with({"z_image": (1920, 1080)}), created_at=OLD_SESSION)

    assert ok is True
    assert note is not None and "旧会话" in note and "放行" in note


def test_new_session_same_zimage_key_is_compared_normally(
        tmp_path, tmp_video, monkeypatch):
    """新会话（#161 之后创建）行为不变：同一格照样参与比例比对并停。

    这条与上一条成对：只把判据放宽成「永远放行」就会在这里变红。
    """
    _pin_frame_model(monkeypatch, "z_image")
    video = tmp_video("film.mp4", width=480, height=864, frames=2)

    ok, note = _verdict(
        video, _brief_with({"z_image": (1920, 1080)}), created_at=NEW_SESSION)

    assert ok is False
    assert note is not None and "1920×1080" in note and "480×864" in note


def test_missing_creation_time_is_treated_as_a_legacy_session(
        tmp_path, tmp_video, monkeypatch):
    """时间缺失 → 按旧会话放行（宁可放行，不制造假停）。"""
    _pin_frame_model(monkeypatch, "z_image")
    video = tmp_video("film.mp4", width=480, height=864, frames=2)

    ok, note = _verdict(video, _brief_with({"z_image": (1920, 1080)}), created_at="")

    assert ok is True
    assert note is not None and "旧会话" in note and "放行" in note


def test_wizard_glue_forwards_the_session_creation_time(tmp_path, tmp_video, monkeypatch):
    """两处向导胶水都要把 ``session.created_at`` 传下去（#179 的接线，不是判据）。

    漏传在签名上就会抛 ``TypeError``；这条钉的是「传进来的值真的被用到」——
    胶水层里把时间换成空串这类改动静默，只有这条会红。
    """
    from minimax_h3_prompt.ui.wizard_director import (
        check_segment_aspect, confirm_final_film_aspect,
    )

    _pin_frame_model(monkeypatch, "z_image")
    brief = _brief_with({"z_image": (1920, 1080)})
    video = tmp_video("film.mp4", width=480, height=864, frames=2)
    saved: list[int] = []

    action = check_segment_aspect(
        mode="r2v", segment_index=1, segment_type="opening", video=video,
        directory=tmp_path, canvases=brief.canvases, brief=brief,
        stage_state={}, ask=lambda text: "", save=lambda: saved.append(1),
        session_created_at=OLD_SESSION)
    assert action == "pass"
    assert saved, "放行后 r2v 要落盘（确认记录随会话保存）"

    assert confirm_final_film_aspect(
        mode="r2v", film=video, brief=brief, stage_state={},
        ask=lambda text: "", save=lambda: saved.append(1),
        session_created_at=OLD_SESSION) is True


def test_session_before_the_first_cutoff_keeps_the_legacy_wording(
        tmp_path, tmp_video, monkeypatch):
    """#369：早于第一个截止点的会话缺答案时，行为与文案与改动前逐字一致。

    这一支不读答案，所以「有没有答案」都走同一条返程；拿缺答案的样本钉住，是因为
    第二个截止点正好插在同一处（``film_size_verdict`` 读不到答案那一支）。
    """
    _pin_frame_model(monkeypatch, "z_image")
    video = tmp_video("film.mp4", width=480, height=864, frames=2)

    ok, note = _verdict(video, _brief_with({}), created_at=OLD_SESSION)

    assert ok is True
    assert note is not None and "这是旧会话" in note and "放行" in note


def test_session_between_the_two_cutoffs_is_not_called_legacy(
        tmp_path, tmp_video, monkeypatch):
    """#369：两个截止点之间建的会话仍放行，但文案不再说「这是旧会话」。

    ``NEW_SESSION``（2026-11-05）晚于 #161、早于画布入口：它没有答案属正常，但用户
    该被告知的是「本会话建于画布入口之前」，而不是「旧会话」——后者指向 2026-11-04
    之前，这个会话并不是。
    """
    _pin_frame_model(monkeypatch, "z_image")
    video = tmp_video("film.mp4", width=480, height=864, frames=2)

    ok, note = _verdict(video, _brief_with({}), created_at=NEW_SESSION)

    assert ok is True
    assert note is not None and "画布入口之前" in note and "放行" in note
    assert "这是旧会话" not in note


def test_session_after_the_canvas_entry_without_an_answer_fails(
        tmp_path, tmp_video, monkeypatch):
    """#369：晚于第二个截止点且读不到答案 → 返回失败并说明原因，不再静默放行。

    这是本票的核心翻转：入口落地之后建的会话应当在建会话时就有答案（表单选择器
    #366 已上线），读不到就该停，而不是假装它是旧会话。
    """
    _pin_frame_model(monkeypatch, "z_image")
    video = tmp_video("film.mp4", width=480, height=864, frames=2)

    ok, note = _verdict(
        video, _brief_with({}), created_at=_after_canvas_entry())

    assert ok is False
    assert note is not None and "读不到" in note and "停止" in note
    assert "放行" not in note


def test_session_with_an_answer_is_compared_exactly_as_before(
        tmp_path, tmp_video, monkeypatch):
    """#369：本票不碰「有答案」这一支——宽高比对与文案逐字不变。

    防的是把整支判据改坏：新截止点只约束「缺答案」，答案在手时无论会话多新都照旧比。
    """
    _pin_frame_model(monkeypatch, "z_image")
    later = _after_canvas_entry()
    matching = tmp_video("ok.mp4", width=1920, height=1080, frames=2)
    ok, note = _verdict(
        matching, _brief_with({"z_image": (1920, 1080)}),
        created_at=later)
    assert (ok, note) == (True, None)

    mismatching = tmp_video("bad.mp4", width=480, height=864, frames=2)
    ok, note = _verdict(
        mismatching, _brief_with({"z_image": (1920, 1080)}),
        created_at=later)
    assert ok is False
    assert note is not None and "1920×1080" in note and "480×864" in note


def test_canvas_entry_cutoff_splits_the_permitted_generations():
    """第二个截止点与第一个并列、带时区，边界两侧各归哪一支。

    「正好落地时刻」与 #161 的口径一致：那一刻起算「已有画布入口」，缺答案即失败。
    只断言两个常量的关系与边界，不重抄字面时间。
    """
    from minimax_h3_prompt.director.film_canvas import (
        FILM_ASPECT_QUESTION_SINCE, FILM_CANVAS_ENTRY_SINCE,
        film_aspect_session_era,
    )

    assert FILM_ASPECT_QUESTION_SINCE < FILM_CANVAS_ENTRY_SINCE
    assert FILM_CANVAS_ENTRY_SINCE.tzinfo is not None
    entry = FILM_CANVAS_ENTRY_SINCE.isoformat()
    before = (FILM_CANVAS_ENTRY_SINCE
              - timedelta(seconds=1)).isoformat()
    assert film_aspect_session_era(before) == "pre_entry"
    assert film_aspect_session_era(entry) == "current"
    # 真实会话文件写 UTC，常量写 +08:00：等价写法必须得出同一支。
    assert film_aspect_session_era(
        FILM_CANVAS_ENTRY_SINCE.astimezone(timezone.utc).isoformat()
    ) == "current"


def test_cutoff_is_the_161_commit_time_in_any_equivalent_spelling():
    """新旧分界就是 #161 的提交时刻，且带时区的等价写法要比较得出同一结论。

    判据要能比时区（真实会话文件写的是 UTC，常量写的是 +08:00）；无时区或读不出
    的时间按旧会话处理——那种数据本来就拿不出判据。
    """
    from minimax_h3_prompt.director.film_canvas import (
        FILM_ASPECT_QUESTION_SINCE, is_legacy_film_aspect_session,
    )

    assert FILM_ASPECT_QUESTION_SINCE.isoformat() == "2026-11-04T14:30:59+08:00"
    # 正好落地时刻＝新会话（问答已经存在）。
    assert is_legacy_film_aspect_session("2026-11-04T14:30:59+08:00") is False
    assert is_legacy_film_aspect_session("2026-11-04T06:30:59+00:00") is False
    assert is_legacy_film_aspect_session("2026-11-04T14:30:58+08:00") is True
    # 无时区 / 读不出 → 旧会话（宁可放行）。
    assert is_legacy_film_aspect_session("2026-11-04T14:30:59") is True
    assert is_legacy_film_aspect_session("not-a-date") is True
    assert is_legacy_film_aspect_session(None) is True

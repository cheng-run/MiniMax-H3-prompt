"""共用拼接核心（issue #93）：与路线无关的段表覆盖、SHA 核对、拼接、时长「只报短」。

为什么单开一个文件、而不是继续写在 ``test_ref_segments.py`` 里：那批用例钉的是**多参
薄包装**的行为（用户可见面），这批钉的是**共用件本身**——首尾帧链（#96/#98）将来接的
是同一个入口，「两条路线共用一份口径」这件事得有形的东西钉住，不能靠两处各写一遍再
指望它们不漂（本仓「同一契约两处口径」已经咬过两次：issue #56 的假
``SEGMENT_TIMELINE_MISSING``、issue #57 的「各段之和 ＝ 总时长」判据）。
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from minimax_h3_prompt.final_film import ConcatStrategy, assemble_film, format_final_film


def _plan_of(index: int, start: int, end: int):
    from minimax_h3_prompt.segment_planner import SegmentPlan

    return SegmentPlan(index=index, start_s=start, end_s=end,
                       shots_in_segment=(index + 1,), summary=f"seg{index + 1}",
                       end_hook="the scene at the segment's end",
                       start_state="the scene at the segment's start")


def _row(segment: int, video: Path) -> dict:
    """一段的段产出账通过行。

    SHA256 在**用例里**用 hashlib 现算，不 import 生产侧那个 ``sha256_file``：拿被测
    代码的另一半当期望值，等于让两边一起错还测不出来（TDD 的反例：同义反复）。
    """
    return {"event": "output", "segment": segment, "video": str(video.resolve()),
            "sha256": hashlib.sha256(video.read_bytes()).hexdigest()}


def _assemble(plans, rows, total_s, out: Path, strategy=ConcatStrategy.STREAM_COPY_FIRST,
              reencode_crf: int = 18, remedy_hint: str | None = None):
    # 18＝多参现状；调用点必须显式给（共用件没有默认值，见 ``assemble_film`` 的 docstring）
    return assemble_film(plans=plans, rows=rows, total_s=total_s, out_path=out,
                         strategy=strategy, reencode_crf=reencode_crf,
                         remedy_hint=remedy_hint)


# ---------------------------------------------------------------------------
# 1. 拒绝面：缺段（成片时长对不上总时长＝伪产物）
# ---------------------------------------------------------------------------

def test_core_refuses_when_a_segment_did_not_pass(tmp_path, tmp_video):
    """缺一段就不产出成片，并点名缺哪几段（与多参当面文案同一句）。"""
    first = tmp_video("s1.mp4", frames=48)
    third = tmp_video("s3.mp4", frames=48)
    rows = {1: _row(1, first), 3: _row(3, third)}
    plans = [_plan_of(0, 0, 2), _plan_of(1, 2, 4), _plan_of(2, 4, 6)]
    out = tmp_path / "final.mp4"

    film = _assemble(plans, rows, 6, out)

    assert film.path is None
    assert not out.exists()
    assert "第 2 段" in film.reason, film.reason
    assert "未产出成片" not in film.reason, "拒绝原因里不该自带展示层文案"


# ---------------------------------------------------------------------------
# 2. 拒绝面：段表不覆盖 [0, 起步总时长]
# ---------------------------------------------------------------------------

def test_core_refuses_a_segment_table_with_a_gap(tmp_path, tmp_video):
    """段表有缺口（手改过 plan.json）→ 不产出成片，并说清哪儿不对。"""
    first = tmp_video("s1.mp4", frames=48)
    second = tmp_video("s2.mp4", frames=48)
    rows = {1: _row(1, first), 2: _row(2, second)}
    plans = [_plan_of(0, 0, 2), _plan_of(1, 3, 5)]     # 2-3s 缺口
    out = tmp_path / "final.mp4"

    film = _assemble(plans, rows, 5, out)

    assert film.path is None
    assert not out.exists()
    assert "缺口/重叠" in film.reason and "0-2s 之后是 3-5s" in film.reason, film.reason


def test_core_refuses_when_the_table_stops_short_of_the_total(tmp_path, tmp_video):
    """段表欠起步总时长 → 拒绝（这是「覆盖」判据的另一半：末段没到 total）。"""
    first = tmp_video("s1.mp4", frames=48)
    rows = {1: _row(1, first)}
    plans = [_plan_of(0, 0, 2)]
    out = tmp_path / "final.mp4"

    film = _assemble(plans, rows, 5, out)

    assert film.path is None
    assert "只覆盖到 2s" in film.reason and "5s" in film.reason, film.reason


# ---------------------------------------------------------------------------
# 3. 拒绝面：账上记的视频读不到 / 换版（SHA256 对不上）
# ---------------------------------------------------------------------------

def test_core_refuses_when_a_recorded_video_is_gone(tmp_path, tmp_video):
    """文件不在了（改名/搬走）要当场说破，不许静默少拼一段。"""
    only = tmp_video("s1.mp4", frames=48)
    rows = {1: _row(1, only)}
    only.unlink()
    out = tmp_path / "final.mp4"

    film = _assemble([_plan_of(0, 0, 2)], rows, 2, out)

    assert film.path is None
    assert "第 1 段" in film.reason and "读不到" in film.reason, film.reason


def test_core_refuses_when_a_recorded_video_changed(tmp_path, tmp_video):
    """同一路径换版（文件名没变、内容变了）→ SHA256 对不上，不许拼进成片。"""
    first = tmp_video("s1.mp4", frames=48)
    rows = {1: _row(1, first)}
    tmp_video("s1.mp4", frames=48, offset=60)     # 覆盖同名文件：这正是文件名可变引用
    out = tmp_path / "final.mp4"

    film = _assemble([_plan_of(0, 0, 2)], rows, 2, out)

    assert film.path is None
    assert "第 1 段" in film.reason and "SHA256" in film.reason, film.reason


def test_core_keeps_the_multi_route_remedy_hint_by_default(tmp_path, tmp_video):
    """不传补救文案时，多参用户看到的那句**逐字不变**（含 ``ref-concat --replace`` 指引）。

    共用件是两条路线共用的，但「换版了怎么办」的出路是**路线**的：多参有换段拼接命令，
    首尾帧链没有（spec #92 明确不提供）。所以补救文案是入参、默认值＝多参今天这句。
    """
    first = tmp_video("s1.mp4", frames=48)
    rows = {1: _row(1, first)}
    tmp_video("s1.mp4", frames=48, offset=60)

    film = _assemble([_plan_of(0, 0, 2)], rows, 2, tmp_path / "final.mp4")

    assert ("重跑该段后请用 `ref-concat <生成目录> --replace 1 <新视频>` 显式指定。"
            in film.reason), film.reason


def test_core_uses_a_custom_remedy_hint_when_given(tmp_path, tmp_video):
    """路线可以换掉那句出路：传了 hint 就只出现自己那句，不该两种命令同时出现。"""
    first = tmp_video("s1.mp4", frames=48)
    rows = {1: _row(1, first)}
    tmp_video("s1.mp4", frames=48, offset=60)

    film = _assemble([_plan_of(0, 0, 2)], rows, 2, tmp_path / "final.mp4",
                     remedy_hint="重跑该段后重新交回视频即可。")

    assert "重跑该段后重新交回视频即可。" in film.reason, film.reason
    assert "ref-concat" not in film.reason, "不该同时出现两条路线的出路"
    assert "SHA256" in film.reason, "换掉出路不改判据与拒绝理由"


# ---------------------------------------------------------------------------
# 4. 段数=1：拼接退化为拷贝改名
# ---------------------------------------------------------------------------

def test_core_degrades_to_copy_for_a_single_segment(tmp_path, tmp_video):
    """段数=1（≤15 秒直出）不劳 ffmpeg 一趟：拷贝改名，内容一字不差。"""
    only = tmp_video("s1.mp4", frames=120)
    out = tmp_path / "final.mp4"

    film = _assemble([_plan_of(0, 0, 5)], {1: _row(1, only)}, 5, out)

    assert film.path == out
    assert film.how == "copy"
    assert out.read_bytes() == only.read_bytes(), "拷贝改名：内容一字不差"
    assert round(film.measured_s, 3) == 5.0
    assert film.warnings == []


# ---------------------------------------------------------------------------
# 5. 多段拼接：段序取段表、多参这一档仍走「优先 stream-copy」
# ---------------------------------------------------------------------------

def test_core_concats_in_plan_order_and_keeps_every_frame(tmp_path, tmp_video):
    """成片按段表顺序接（不是交回顺序），帧数＝各段之和；多参档位＝无损 stream-copy。"""
    from minimax_h3_prompt.tools.frame_match import mad, read_frames_gray

    # 段 2 的方块起手位置挪开（offset）：段序错了两段的接缝帧就对不上。
    first = tmp_video("s1.mp4", frames=48, width=400)
    second = tmp_video("s2.mp4", frames=72, width=400, offset=60)
    rows = {1: _row(1, first), 2: _row(2, second)}
    plans = [_plan_of(0, 0, 2), _plan_of(1, 2, 5)]
    out = tmp_path / "final.mp4"

    film = _assemble(plans, rows, 5, out)

    assert film.path == out, film.reason
    assert film.how == "stream-copy", "多参这一档必须保持「优先无损 stream-copy」"
    frames = read_frames_gray(out)
    assert len(frames) == 48 + 72, "拼接不许丢帧/加帧"
    assert round(film.measured_s, 3) == 5.0
    assert mad(frames[0], read_frames_gray(first)[0]) < 1.0, "第 1 帧是段 1 的开场"
    assert mad(frames[48], read_frames_gray(second)[0]) < 1.0, "第 48 帧是段 2 的开场"


# ---------------------------------------------------------------------------
# 6. 覆盖判据是「末段结束 ≥ 总时长」，不是「各段之和 ＝ 总时长」
# ---------------------------------------------------------------------------def test_core_accepts_a_last_segment_that_overshoots_the_total(tmp_path, tmp_video):
    """各段之和 5s ≠ 起步总时长 4s，但末段够长（覆盖到 5s）→ **仍然出片**。

    这是本票要钉住的那一条：规划器契约允许末段留余量（``parse_segment_plan``），拿
    「各段之和 ＝ 总时长」当判据会把**全段通过**的片子判成「段表不是一套」而拒绝。
    """
    first = tmp_video("s1.mp4", frames=48)     # 2.0s
    second = tmp_video("s2.mp4", frames=72)    # 3.0s
    rows = {1: _row(1, first), 2: _row(2, second)}
    plans = [_plan_of(0, 0, 2), _plan_of(1, 2, 5)]
    out = tmp_path / "final.mp4"

    film = _assemble(plans, rows, 4, out)      # 起步 4s，段表到 5s

    assert film.path is not None, film.reason
    assert (film.total_s, film.planned_s) == (4, 5)
    assert "各段计划时长之和 5s，末段留余量" in format_final_film(film)
    assert film.warnings == [], "末段留余量不是「短了」，不该报「只报短」的提醒"


def test_core_warns_but_still_produces_when_the_film_runs_short(tmp_path, tmp_video):
    """成片短于各段计划时长之和只**报**、不拒（「只报短」）。

    实测时长是 H3 的档位量化产物、从不是整数秒；拿它要求严格相等，等于把每个真产出都
    判成伪产物。但真短掉一档（这里每段交回的比计划的短一半）必须有人看得见——所以是
    告警而不是放行得无声无息。
    """
    first = tmp_video("s1.mp4", frames=48)     # 2.0s，计划 4s
    second = tmp_video("s2.mp4", frames=72)    # 3.0s，计划 4s
    rows = {1: _row(1, first), 2: _row(2, second)}
    plans = [_plan_of(0, 0, 4), _plan_of(1, 4, 8)]
    out = tmp_path / "final.mp4"

    film = _assemble(plans, rows, 8, out)

    assert film.path is not None, film.reason
    assert any("短于各段计划时长之和 8s" in note for note in film.warnings), film.warnings
    assert "短于各段计划时长之和 8s" in format_final_film(film)


# ---------------------------------------------------------------------------
# 7. 拼接口径是入参：未实现的那一档必须显式失败，不许静默退回 stream-copy
# ---------------------------------------------------------------------------

def test_core_refuses_the_trim_strategy_instead_of_silently_stream_copying(tmp_path, tmp_video):
    """首尾帧链那一档（裁首帧 + 重编码）本票只定义取值，实现留 #96/#98。

    它**必须**返回失败原因、且一个字节也不产出：静默退回 stream-copy 会留下接缝的
    重复首帧（spec #92 实测回退 4 帧 = 0.167s），而用户看不出这是拼接方式错了。
    """
    first = tmp_video("s1.mp4", frames=48)
    second = tmp_video("s2.mp4", frames=48)
    rows = {1: _row(1, first), 2: _row(2, second)}
    plans = [_plan_of(0, 0, 2), _plan_of(1, 2, 4)]
    out = tmp_path / "final.mp4"

    film = _assemble(plans, rows, 4, out, strategy=ConcatStrategy.TRIM_FIRST_FRAME)

    assert film.path is None
    assert "尚未实现" in film.reason, film.reason
    assert not out.exists(), "未实现的档位不许退回去拼一条出来"


def test_core_refuses_the_copy_shortcut_under_the_trim_strategy(tmp_path, tmp_video):
    """段数=1 时也不能借「拷贝改名」绕过未实现的档位（策略分派先于段数分支）。"""
    only = tmp_video("s1.mp4", frames=48)
    out = tmp_path / "final.mp4"

    film = _assemble([_plan_of(0, 0, 2)], {1: _row(1, only)}, 2, out,
                     strategy=ConcatStrategy.TRIM_FIRST_FRAME)

    assert film.path is None
    assert "尚未实现" in film.reason, film.reason
    assert not out.exists()


def test_core_refuses_an_unknown_strategy(tmp_path, tmp_video):
    """取值拼错（或将来新增档位忘了接）→ 显式失败，不许猜一个最像的去拼。"""
    only = tmp_video("s1.mp4", frames=48)
    out = tmp_path / "final.mp4"

    film = _assemble([_plan_of(0, 0, 2)], {1: _row(1, only)}, 2, out,
                     strategy="trim-first-frame-typo")

    assert film.path is None
    assert "未知的拼接策略" in film.reason, film.reason
    assert not out.exists()


# ---------------------------------------------------------------------------
# 8. ffmpeg 不可用：转成「拼接失败 + 原因」，不裸抛异常
# ---------------------------------------------------------------------------

def test_core_reports_concat_failure_when_ffmpeg_is_unavailable(tmp_path, tmp_video, monkeypatch):
    """找不到 ffmpeg（``find_ffmpeg`` 抛 RuntimeError）→ 返回失败结果，不往外抛。

    用户此刻已经跑完整条链、每段都验收过了：一个缺依赖不该把收尾炸成异常栈。
    这里**故意不写 pytest.raises**——异常冒出来这条用例就红。
    """
    def _missing_ffmpeg():
        raise RuntimeError("找不到 ffmpeg。请执行 `uv add imageio-ffmpeg`，或把 ffmpeg 放进 PATH。")

    # 打在 ``merge_videos`` 实际查的那个模块上（它在调用时查模块全局，不是查 final_film）
    monkeypatch.setattr("minimax_h3_prompt.tools.seam_audit.find_ffmpeg", _missing_ffmpeg)

    first = tmp_video("s1.mp4", frames=48)
    second = tmp_video("s2.mp4", frames=48)
    rows = {1: _row(1, first), 2: _row(2, second)}
    plans = [_plan_of(0, 0, 2), _plan_of(1, 2, 4)]
    out = tmp_path / "final.mp4"

    film = _assemble(plans, rows, 4, out)

    assert film.path is None
    assert "拼接失败" in film.reason, film.reason
    assert "ffmpeg" in film.reason, "原因里要点名是 ffmpeg 的问题，别让人去猜"
    assert not out.exists()


def test_core_forwards_the_reencode_crf_to_the_concat_step(tmp_path, tmp_video, monkeypatch):
    """重编码画质档由**调用方**给、共用件只转发（issue #93 复审，勘察 §F-8）。

    多参传 18（历史写死值，成片已交付过，改它等于悄悄改画质）；首尾帧链按 spec #92
    传 16（由 #98 传）。共用件若自带默认值，这条路线属性就会藏在签名里看不见。
    """
    from minimax_h3_prompt.tools import seam_audit

    seen: dict[str, int] = {}

    def fake_merge(videos, out, *, reencode_crf):
        seen["crf"] = reencode_crf
        out = Path(out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"stub")
        return True, "re-encode"

    monkeypatch.setattr(seam_audit, "merge_videos", fake_merge)
    first = tmp_video("s1.mp4", frames=48)
    second = tmp_video("s2.mp4", frames=48)
    rows = {1: _row(1, first), 2: _row(2, second)}

    film = _assemble([_plan_of(0, 0, 2), _plan_of(1, 2, 4)], rows, 4,
                     tmp_path / "final.mp4", reencode_crf=16)

    assert film.path is not None, film.reason
    assert seen["crf"] == 16, "共用件必须把调用方给的画质档原样交给拼接本体"


# ---------------------------------------------------------------------------
# 9. 多参薄包装：策略固定为「优先 stream-copy」，用户可见行为不变
# ---------------------------------------------------------------------------

def test_multi_route_wrapper_keeps_the_stream_copy_strategy(tmp_path, tmp_video):
    """``ref_segments.assemble_final_film`` 是薄包装：段序、账、拒绝面都不动，
    策略固定成多参这一档（不是共用件的「默认值」——共用件没有默认值）。"""
    from minimax_h3_prompt.ref_segments import assemble_final_film, passed_segment_rows

    first = tmp_video("s1.mp4", frames=48)
    second = tmp_video("s2.mp4", frames=48)
    from minimax_h3_prompt.ref_segments import log_segment_output

    log_segment_output(tmp_path, segment=1, start_s=0, end_s=2, video_path=first)
    log_segment_output(tmp_path, segment=2, start_s=2, end_s=4, video_path=second)
    plans = [_plan_of(0, 0, 2), _plan_of(1, 2, 4)]

    film = assemble_final_film(directory=tmp_path, plans=plans, total_s=4,
                               rows=passed_segment_rows(tmp_path))

    assert film.path == tmp_path / "final.mp4", film.reason
    assert film.how == "stream-copy"

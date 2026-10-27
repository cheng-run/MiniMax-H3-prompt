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
# 7. 拼接口径是入参：未知档位必须显式失败，不许猜一个最像的去拼
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# 10. 首尾帧链那一档：裁掉第 2..N 段的首帧再重编码（issue #98）
# ---------------------------------------------------------------------------

def _write_flat_video(path: Path, values: list[int], *, fps: int = 24,
                      size: tuple[int, int] = (128, 72)) -> Path:
    """写一条**整幅灰度逐帧更换**的小视频（MJPG，帧内无损到肉眼看不出）。

    为什么不用 ``write_test_video`` 的方块位移：接缝判据要的是「这两帧是不是同一张
    图」。方块位移时帧间差随步长变小（重叠区不算差），而整幅灰度换值可以让「重复帧」
    与「下一帧」差出一个数量级——判据不用靠调阈值来凑。
    """
    import cv2
    import numpy as np

    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"MJPG"), fps, size)
    assert writer.isOpened(), f"VideoWriter 打不开 {path}"
    for value in values:
        writer.write(np.full((size[1], size[0], 3), value, np.uint8))
    writer.release()
    return path


def test_trim_strategy_drops_one_frame_per_seam(tmp_path, tmp_video):
    """三段真视频：成片帧数 = 各段实测帧数之和 − (段数 − 1)（spec #92 的确定性判据）。

    48 + 72 + 96 − 2 个接缝 = 214 帧。这条算式是「有没有裁掉该裁的帧」的唯一可核形态
    ——靠眼睛看接缝是看不出来的（段尾落定时差一帧肉眼不可辨）。
    """
    from minimax_h3_prompt.tools.frame_match import video_meta

    first = tmp_video("s1.mp4", frames=48)
    second = tmp_video("s2.mp4", frames=72)
    third = tmp_video("s3.mp4", frames=96)
    rows = {1: _row(1, first), 2: _row(2, second), 3: _row(3, third)}
    plans = [_plan_of(0, 0, 2), _plan_of(1, 2, 5), _plan_of(2, 5, 9)]
    out = tmp_path / "final.mp4"

    film = _assemble(plans, rows, 9, out, strategy=ConcatStrategy.TRIM_FIRST_FRAME,
                     reencode_crf=16)

    assert film.path == out, film.reason
    assert film.how == "trim-first-frame", "拼接方式必须可辨识（不是 stream-copy）"
    assert video_meta(out)["frames"] == 48 + 72 + 96 - 2
    assert not list(tmp_path.glob("*.partial.mp4")), \
        "先写临时名、成功后改名：成片目录里不许留半成品"


def test_trim_strategy_removes_the_duplicated_seam_frame(tmp_path):
    """接缝处不再是同一张图的两连帧：第 2 段的首帧被裁掉，紧接其后的是它的第二帧。

    造法是把真实现场搬进夹具：第 2 段的首帧**就是**第 1 段的末帧（尾帧接力），此后
    画面才往前走。不裁的时候这两帧几乎一模一样（mad≈0，成片在这里抽一下），裁掉之后
    接缝两侧是「段 1 末帧｜段 2 第二帧」。
    """
    from minimax_h3_prompt.tools.frame_match import mad, read_frames_gray

    first_values = [30 + i for i in range(48)]          # 段 1：30…77
    # 段 2 首帧＝段 1 末帧（尾帧接力的结构），其后整幅跳开（差 ≥80 个灰度级）
    second_values = [first_values[-1]] + [200 - (i % 40) for i in range(71)]
    first = _write_flat_video(tmp_path / "s1.mp4", first_values)
    second = _write_flat_video(tmp_path / "s2.mp4", second_values)
    rows = {1: _row(1, first), 2: _row(2, second)}
    plans = [_plan_of(0, 0, 2), _plan_of(1, 2, 5)]
    out = tmp_path / "final.mp4"

    film = _assemble(plans, rows, 5, out, strategy=ConcatStrategy.TRIM_FIRST_FRAME,
                     reencode_crf=16)

    assert film.path == out, film.reason
    frames = read_frames_gray(out)
    assert len(frames) == 48 + 72 - 1
    source_first = read_frames_gray(first)
    source_second = read_frames_gray(second)
    assert mad(source_first[-1], source_second[0]) < 1.0, \
        "夹具没造出「第 2 段首帧＝第 1 段末帧」的接力形态"
    seam = 48                                    # 成片里第 2 段第一帧的位置（第 1 段共 48 帧）
    assert mad(frames[seam - 1], frames[seam]) > 30.0, \
        "接缝两侧还是同一张图（重复首帧没被裁掉）"
    assert mad(frames[seam], source_second[1]) < 3.0, \
        "接缝右侧必须是第 2 段的**第二帧**（裁了不止一帧／裁错了位置都会在这里露）"


def test_trim_strategy_degrades_to_copy_for_a_single_segment(tmp_path, tmp_video):
    """段数=1 没有接缝可裁：退化为拷贝改名（spec #92：与多参同构的分支）。"""
    only = tmp_video("s1.mp4", frames=120)
    out = tmp_path / "final.mp4"

    film = _assemble([_plan_of(0, 0, 5)], {1: _row(1, only)}, 5, out,
                     strategy=ConcatStrategy.TRIM_FIRST_FRAME, reencode_crf=16)

    assert film.path == out, film.reason
    assert film.how == "copy"
    assert out.read_bytes() == only.read_bytes()


def test_trim_strategy_refuses_mismatched_size_and_names_the_segments(tmp_path, tmp_video):
    """某段尺寸与其它段不一致 → 拒绝并点名，而不是让用户去看 ffmpeg 报错尾巴。

    concat filter 的技术前提是各段同尺寸同帧率；这既是产品判断（不是一套输出）也是
    硬技术前提。点名第 3 段：它才是那个尺寸不同的。
    """
    first = tmp_video("s1.mp4", frames=48, width=128, height=72)
    second = tmp_video("s2.mp4", frames=48, width=128, height=72)
    third = tmp_video("s3.mp4", frames=48, width=160, height=72)
    rows = {1: _row(1, first), 2: _row(2, second), 3: _row(3, third)}
    plans = [_plan_of(0, 0, 2), _plan_of(1, 2, 4), _plan_of(2, 4, 6)]
    out = tmp_path / "final.mp4"

    film = _assemble(plans, rows, 6, out, strategy=ConcatStrategy.TRIM_FIRST_FRAME,
                     reencode_crf=16)

    assert film.path is None
    assert not out.exists()
    assert "第 3 段" in film.reason, film.reason
    assert "尺寸或帧率" in film.reason or "帧率" in film.reason, film.reason


def test_trim_strategy_refuses_mismatched_frame_rate(tmp_path, tmp_video):
    """帧率不一致同样拒绝并点名（ffmpeg 不会拦，它会把两段按各自节奏接起来）。"""
    first = tmp_video("s1.mp4", frames=48, fps=24)
    second = tmp_video("s2.mp4", frames=48, fps=30)
    rows = {1: _row(1, first), 2: _row(2, second)}
    plans = [_plan_of(0, 0, 2), _plan_of(1, 2, 4)]
    out = tmp_path / "final.mp4"

    film = _assemble(plans, rows, 4, out, strategy=ConcatStrategy.TRIM_FIRST_FRAME,
                     reencode_crf=16)

    assert film.path is None
    assert "第 2 段" in film.reason, film.reason
    assert not out.exists()


def test_trim_strategy_falls_back_to_naive_concat_and_says_so(tmp_path, tmp_video,
                                                              monkeypatch):
    """裁首帧重编码挂了 → 退回朴素拼接，但**必须说破接缝保留了重复首帧**。

    静默降级是本仓反复吃过的形态：用户手里是一条「看起来接好了」的片子，接缝处抽一下
    （重复首帧）却没有任何地方说过话。所以兜底要同时做三件事：产出成片、
    ``how`` 与多参的 ``stream-copy`` 可区分、结果里出现那句说破。
    """
    from minimax_h3_prompt.tools import seam_audit
    from minimax_h3_prompt.tools.frame_match import video_meta

    def broken_trim(*_a, **_k):
        return False, "filter 图被拒（模拟编码器挂掉）"

    monkeypatch.setattr(seam_audit, "merge_videos_trimming_first_frames", broken_trim)
    first = tmp_video("s1.mp4", frames=48)
    second = tmp_video("s2.mp4", frames=48)
    rows = {1: _row(1, first), 2: _row(2, second)}
    plans = [_plan_of(0, 0, 2), _plan_of(1, 2, 4)]
    out = tmp_path / "final.mp4"

    film = _assemble(plans, rows, 4, out, strategy=ConcatStrategy.TRIM_FIRST_FRAME,
                     reencode_crf=16)

    assert film.path == out, film.reason
    assert film.how == "stream-copy-no-trim", "兜底档必须与多参的 stream-copy 可区分"
    assert any("接缝保留了重复首帧" in note for note in film.warnings), film.warnings
    assert "接缝保留了重复首帧" in format_final_film(film), "当面文案里也要说破"
    assert video_meta(out)["frames"] == 96, "兜底＝朴素拼接，一帧不裁"


def test_trim_strategy_forwards_the_reencode_crf_to_the_trim_primitive(
        tmp_path, tmp_video, monkeypatch):
    """首尾帧链按 spec #92 传 CRF 16，共用件原样交给裁帧重编码那一步。"""
    from minimax_h3_prompt.tools import seam_audit

    seen: dict[str, object] = {}

    def fake_trim(videos, out, *, reencode_crf, fps, with_audio):
        seen.update(crf=reencode_crf, fps=fps, audio=with_audio)
        out = Path(out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"stub")
        return True, ""

    monkeypatch.setattr(seam_audit, "merge_videos_trimming_first_frames", fake_trim)
    first = tmp_video("s1.mp4", frames=48)
    second = tmp_video("s2.mp4", frames=48)
    rows = {1: _row(1, first), 2: _row(2, second)}

    film = _assemble([_plan_of(0, 0, 2), _plan_of(1, 2, 4)], rows, 4,
                     tmp_path / "final.mp4", strategy=ConcatStrategy.TRIM_FIRST_FRAME,
                     reencode_crf=16)

    assert film.path is not None, film.reason
    assert seen["crf"] == 16, "画质档必须原样交给裁帧重编码那一步"
    assert seen["fps"] == 24.0, "fps 取实测元数据（不写死 24）"
    assert seen["audio"] is False, "测试视频没有音轨：不许凭空造一条"


def test_trim_strategy_checks_shortness_against_the_trimmed_expectation(tmp_path, tmp_video):
    """「只报短」的预期值必须是 `各段计划时长之和 − (段数−1)/fps`，不是原样那个和。

    段 2 只交回 40 帧（计划 3s）——确实短了，必须报。报出来的**数字**才是这条用例的
    判据：三段两接缝，预期 9 − 2/24 = 8.9167s，所以文案里是「8.92s」；没扣的话会说
    「9s」。裁掉的那两帧不是「没接上」，不该被算进缺口里。
    """
    first = tmp_video("s1.mp4", frames=48)      # 2.0s，计划 2s
    second = tmp_video("s2.mp4", frames=40)     # 1.667s，计划 3s（真短了）
    third = tmp_video("s3.mp4", frames=96)      # 4.0s，计划 4s
    rows = {1: _row(1, first), 2: _row(2, second), 3: _row(3, third)}
    plans = [_plan_of(0, 0, 2), _plan_of(1, 2, 5), _plan_of(2, 5, 9)]
    out = tmp_path / "final.mp4"

    film = _assemble(plans, rows, 9, out, strategy=ConcatStrategy.TRIM_FIRST_FRAME,
                     reencode_crf=16)

    assert film.path == out, film.reason
    assert any("短于各段计划时长之和 8.92s" in note for note in film.warnings), film.warnings
    assert not any("短于各段计划时长之和 9s" in note for note in film.warnings), \
        "预期值没扣掉裁掉的帧数"


def test_trim_strategy_does_not_false_alarm_on_a_film_that_is_only_a_frame_short(
        tmp_path, tmp_video):
    """全接上的成片（计划时长档内差一两帧）不该被报成「可能漏了一段」。"""
    first = tmp_video("s1.mp4", frames=48)      # 2.0s，计划 2s
    second = tmp_video("s2.mp4", frames=71)     # 2.958s，计划 3s（差 1 帧）
    third = tmp_video("s3.mp4", frames=96)      # 4.0s，计划 4s
    rows = {1: _row(1, first), 2: _row(2, second), 3: _row(3, third)}
    plans = [_plan_of(0, 0, 2), _plan_of(1, 2, 5), _plan_of(2, 5, 9)]
    out = tmp_path / "final.mp4"

    film = _assemble(plans, rows, 9, out, strategy=ConcatStrategy.TRIM_FIRST_FRAME,
                     reencode_crf=16)

    assert film.path == out, film.reason
    assert film.warnings == [], "接缝裁帧本身不该被算成「成片短了」"


def _with_silent_audio(video: Path) -> Path:
    """给测试视频补一条真音轨（重封装，不动画面）。

    为什么要造音轨：真产物两种形态都有——``MiniMax-H3视频_00001.mp4`` 无音轨，
    同目录的 ``-audio.mp4`` 带 aac（2026-10-27 实测）。「音频同步裁一帧」这一支没有
    真音轨就一行都跑不到，只测无音轨形态等于没测。
    """
    import subprocess

    from minimax_h3_prompt.tools.seam_audit import find_ffmpeg

    out = video.with_name(video.stem + "-audio.mp4")
    proc = subprocess.run(
        [str(find_ffmpeg()), "-y", "-i", str(video), "-f", "lavfi",
         "-i", "anullsrc=r=32000:cl=stereo", "-shortest",
         "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", str(out)],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert proc.returncode == 0, proc.stderr[-800:]
    return out


def test_trim_strategy_trims_the_audio_track_with_the_video(tmp_path, tmp_video):
    """各段都带音轨时，音轨跟着画面一起裁一帧、成片仍有声。

    只裁画面不裁音轨会让接缝处音画错开一帧——一帧 0.042s，单听不明显，但连着几个
    接缝就是可听出来的「对不上口型」。
    """
    from minimax_h3_prompt.tools.frame_match import video_meta
    from minimax_h3_prompt.tools.seam_audit import probe_streams

    first = _with_silent_audio(tmp_video("s1.mp4", frames=48))
    second = _with_silent_audio(tmp_video("s2.mp4", frames=48))
    rows = {1: _row(1, first), 2: _row(2, second)}
    plans = [_plan_of(0, 0, 2), _plan_of(1, 2, 4)]
    out = tmp_path / "final.mp4"

    film = _assemble(plans, rows, 4, out, strategy=ConcatStrategy.TRIM_FIRST_FRAME,
                     reencode_crf=16)

    assert film.path == out, film.reason
    assert "audio" in probe_streams(out), "成片必须有音轨"
    assert video_meta(out)["frames"] == 48 + 48 - 1


@pytest.mark.parametrize("silent_position,audio_position,blamed", [
    (2, 1, "第 2 段"),   # 前段有音轨、后段没有 → 点名后段
    (1, 2, "第 1 段"),   # 反过来 → 点名前段（别只查「第 2 段之后」）
])
def test_trim_strategy_refuses_mixed_audio_and_names_the_silent_segment(
        tmp_path, tmp_video, silent_position, audio_position, blamed):
    """音轨形态不一致（一段带 aac、一段没有）→ 拒绝并点名缺音轨的那一段。

    这不是纸面情况：真产物两种形态都存在（``MiniMax-H3视频_00001.mp4`` 无音轨、
    同目录 ``-audio.mp4`` 带 aac，2026-10-27 实测），有人混着交回来时 ffmpeg 照样能
    接出一条「看起来正常」的片子，接缝处却音画错位。所以与尺寸/帧率不一致同档：
    拒绝、点名、不产出成片——让人直接去换那一段，而不是听出一条对不上的口型。
    """
    first_frames, second_frames = 48, 48
    plain_first = tmp_video("s1.mp4", frames=first_frames)
    plain_second = tmp_video("s2.mp4", frames=second_frames)
    audio_first = _with_silent_audio(plain_first)
    audio_second = _with_silent_audio(plain_second)
    videos = {1: audio_first if audio_position == 1 else plain_first,
              2: audio_second if audio_position == 2 else plain_second}
    rows = {number: _row(number, video) for number, video in videos.items()}
    plans = [_plan_of(0, 0, 2), _plan_of(1, 2, 4)]
    out = tmp_path / "final.mp4"

    film = _assemble(plans, rows, 4, out, strategy=ConcatStrategy.TRIM_FIRST_FRAME,
                     reencode_crf=16)

    assert film.path is None, "音轨不一致不许拼出一条音画错位的成片"
    assert not out.exists()
    assert not list(tmp_path.glob("*.partial.mp4")), "拒绝时不该留下半成品"
    assert blamed in film.reason, film.reason
    other = "第 2 段" if blamed == "第 1 段" else "第 1 段"
    assert other not in film.reason, f"点名要点缺音轨的那一段，别把有声的也点进去：{film.reason}"
    assert "音轨不一致" in film.reason, film.reason

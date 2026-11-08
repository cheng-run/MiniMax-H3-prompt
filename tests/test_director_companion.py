"""导演台逐段陪跑的可证层（issue #105）：段缓存定位、数帧、段产出账、判定。

缝是三个新模块的公开入口（设计 §2 模块表）：

- ``director/cache.py``：段缓存目录名与帧文件定位、实际帧数读取；
- ``director/ledger.py``：段产出账的两类行（``output`` / ``invalidate``）；
- ``director/companion.py``：纯函数的逐段判定（通过 / 告警 / 报红）。

口径来源（写断言前先读，别自造）：
- ``docs/design/2026-10-29-导演台路线实现设计.md`` §3.5（段缓存）/ §3.6（段产出账）/
  §3.7（可证层判定）；
- ``docs/research/2026-10-29-director-插件原生格式.md`` §5（段缓存全篇）与「四、硬约束
  清单」第 17–22 条（可证层），对应插件 ``director/segment_cache.py`` HEAD ``4c6245e``。

**测试绝不读真实 ComfyUI 目录、不发网络请求**：缓存全部用 ``tmp_path`` 现造，且至少
一条路径是**真造 FFV1 mkv 再数帧**（否则「数帧」这段逻辑永不被真正执行，打桩能掩盖
整段坏死）。造法与插件产物一致：FFV1 / rgb24 / 无音轨（``lib/frames_ffv1.py:30``）。
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import cv2
import imageio_ffmpeg
import pytest

from minimax_h3_prompt.director import cache as director_cache


# ---------------------------------------------------------------------------
# fixture：真造 FFV1 mkv（与插件 ``seg_XXXX.frames.mkv`` 同编码）
# ---------------------------------------------------------------------------

def _write_ffv1_mkv(path: Path, frames: int, *, width: int = 32, height: int = 24) -> Path:
    """用 imageio_ffmpeg 的 ffmpeg 把 rawvideo 编成 FFV1 mkv（插件产物同编码）。

    为什么不用 conftest 的 ``write_test_video``：它写 mp4（H.264），而 #105 的整条
    判据建立在「FFV1 无损 mkv 能被 cv2 数帧」上；换成别的编码，测的就不是现场那条路。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    # 逐帧变化的像素：纯色恒定视频在部分解码器里会被优化成单帧，数出来的帧数不可信。
    raw = bytes((index * 37) % 256 for index in range(width * height * 3 * frames))
    command = [
        imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-v", "error",
        "-f", "rawvideo", "-pix_fmt", "rgb24",
        "-s", f"{width}x{height}", "-r", "24", "-i", "pipe:0",
        "-c:v", "ffv1", "-pix_fmt", "rgb24", str(path),
    ]
    result = subprocess.run(command, input=raw, capture_output=True)
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
    return path


def _frame_colors(count: int) -> list[tuple[int, int, int]]:
    """``count`` 帧互相可辨的 RGB 纯色（R 通道逐帧 +2，107 帧内不重复）。"""
    return [((index * 2) % 256, (index * 5) % 256, (index * 11) % 256)
            for index in range(count)]


def _write_canvas_png(path: Path, *, width: int = 32, height: int = 24) -> Path:
    """写一张指定宽高的 png——#158 的画幅核对只吃比例，小图就够。

    测试视频是 32×24；把开场锚图也做成 32×24，i2v/fl2v 的既有陪跑用例才不会被
    画幅核对正确地拦下（它们验的不是画幅）。
    """
    import numpy as np

    path.parent.mkdir(parents=True, exist_ok=True)
    assert cv2.imwrite(str(path), np.zeros((height, width, 3), np.uint8))
    return path


def _write_solid_ffv1_mkv(path: Path, colors: list[tuple[int, int, int]],
                          *, width: int = 32, height: int = 24) -> Path:
    """每帧一种可辨纯色（RGB 由调用方给）：用来断言取到的**是哪一帧**。

    为什么不用 ``_write_ffv1_mkv`` 的逐字节花纹：在 32×24 上它的色阶周期恰好是 1 帧
    （``width*height*3*37 % 256 == 0``），整条视频每一帧长得一模一样——「末帧 vs 邻帧」
    无从区分，断言会变成自证。视频内容由调用方给定，也才能拿它跟缓存内容对照。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    # rawvideo 的 rgb24 是 R,G,B 顺序；cv2 读出/写出的是 BGR，测试里自己换算。
    raw = b"".join(bytes([red, green, blue]) * (width * height)
                   for red, green, blue in colors)
    command = [
        imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-v", "error",
        "-f", "rawvideo", "-pix_fmt", "rgb24",
        "-s", f"{width}x{height}", "-r", "24", "-i", "pipe:0",
        "-c:v", "ffv1", "-pix_fmt", "rgb24", str(path),
    ]
    result = subprocess.run(command, input=raw, capture_output=True)
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
    return path


def _solid_bgr_bytes(red: int, green: int, blue: int, *, width: int = 32,
                     height: int = 24) -> bytes:
    """一张纯色 png 被 cv2 读回来时应有的字节（BGR），用于跟导出的末帧逐字节比。"""
    return bytes([blue, green, red]) * (width * height)


# ---------------------------------------------------------------------------
# 段缓存定位（设计 §3.5）：目录名复刻插件 normalize_cache_name
# ---------------------------------------------------------------------------

def test_cache_name_normalisation_mirrors_the_plugin():
    """缓存名归一与插件 ``normalize_cache_name`` 逐条同口径（``segment_cache.py:155-177``）。

    用户原始输入**不能**直接拼路径：Windows 非法字符、空白与点都要折成 ``_``，
    否则「缓存名」这个字符串会变成另一个路径（点还是 ``..`` 越界片段的原料）。
    """
    normalize = director_cache.normalize_cache_name
    assert normalize("雨夜便利店") == "雨夜便利店"      # 中文保留
    assert normalize("a b") == "a_b"                    # 空白 → 下划线
    assert normalize("a.b") == "a_b"                    # 点 → 下划线
    assert normalize("..") == ""                        # 全是点 → 空（回落节点 id）
    assert normalize("  ") == ""                        # 全空白 → 空
    assert normalize("a/b\\c:d*e?f\"g<h>i|j") == "a_b_c_d_e_f_g_h_i_j"
    assert normalize("___x___") == "x"                   # 首尾下划线剥掉
    assert normalize("x" * 80) == "x" * 64               # 截断 64 字符


def test_cache_name_normalisation_prefixes_windows_device_names():
    """``CON`` / ``NUL`` 这类 Windows 保留设备名必须加前缀，否则目录建不出来。"""
    assert director_cache.normalize_cache_name("con") == "_con"
    assert director_cache.normalize_cache_name("NUL") == "_NUL"
    assert director_cache.normalize_cache_name("console") == "console"  # 不是设备名，不动


def test_cache_key_keeps_the_node_id_when_the_name_is_empty():
    """目录名＝``<缓存名>_<节点id>``；缓存名空时只有节点 id（``resolve_segment_cache_key``）。"""
    assert director_cache.resolve_cache_key("", "42") == "42"
    assert director_cache.resolve_cache_key("   ", "42") == "42"
    assert director_cache.resolve_cache_key("雨夜", "42") == "雨夜_42"


def test_cache_dir_lives_under_the_comfy_output_root(tmp_path):
    """根目录＝``<ComfyUI output>/minimax_seg_cache/<键>/``；根从入参来，不写在模块里。"""
    directory = director_cache.cache_dir(tmp_path, "雨夜", "42")
    assert directory == tmp_path / "minimax_seg_cache" / "雨夜_42"
    assert director_cache.cache_dir(tmp_path, "", "42") == (
        tmp_path / "minimax_seg_cache" / "42")


def test_frames_path_uses_zero_based_segment_numbers(tmp_path):
    """帧文件名是**0 基**段号（``seg_0000.frames.mkv``），段号 1 起 ↔ 文件 0 起。"""
    directory = tmp_path / "cache"
    assert director_cache.frames_path(directory, 1).name == "seg_0000.frames.mkv"
    assert director_cache.frames_path(directory, 4).name == "seg_0003.frames.mkv"


# ---------------------------------------------------------------------------
# 数帧（设计 §3.5 / 硬约束第 17 条）：真造 mkv 数，不许打桩糊过去
# ---------------------------------------------------------------------------

def test_cached_segments_lists_one_based_segment_numbers(tmp_path):
    """扫目录里**已有的**帧文件 → 1 基段号元组；一采（``.pre.``）与 ``.pt`` 不算终稿帧。

    勾错段的判定全靠这份清单：它必须只回答「哪些段的终稿帧真的在盘上」。
    """
    directory = tmp_path / "cache"
    directory.mkdir()
    _write_ffv1_mkv(directory / "seg_0000.frames.mkv", 5)
    _write_ffv1_mkv(directory / "seg_0002.frames.mkv", 5)
    # 一采帧与 raw 帧不是终稿：算进清单会让「这段跑完了」变成假话。
    _write_ffv1_mkv(directory / "seg_0001.pre.frames.mkv", 5)
    (directory / "seg_0003.pt").write_bytes(b"not-a-mkv")
    (directory / "seg_0001.meta.json").write_text("{}", encoding="utf-8")

    assert director_cache.cached_segments(directory) == (1, 3)


def test_cached_segments_on_a_missing_directory_is_empty(tmp_path):
    """缓存目录还不存在＝一段都没跑（不是错误：用户还没在导演台里点运行）。"""
    assert director_cache.cached_segments(tmp_path / "nope") == ()


def test_actual_frames_counts_a_real_ffv1_mkv(tmp_path):
    """真造 30 帧 FFV1 mkv，数出来必须是 30——这条走的是现场那条解码路径。"""
    path = _write_ffv1_mkv(tmp_path / "seg_0000.frames.mkv", 30)
    assert director_cache.actual_frames(path) == 30


def test_actual_frames_differs_per_file(tmp_path):
    """两段不同帧数各自数对：防「只要文件在就当同一个数」的假绿。"""
    first = _write_ffv1_mkv(tmp_path / "seg_0000.frames.mkv", 29)
    second = _write_ffv1_mkv(tmp_path / "seg_0001.frames.mkv", 30)
    assert director_cache.actual_frames(first) == 29
    assert director_cache.actual_frames(second) == 30


def test_actual_frames_refuses_an_unreadable_file(tmp_path):
    """文件在但读不出（截断/损坏）→ 报错。**读不出来不是 0 帧**：静默归零会把
    「解码器不认这个容器」误判成「这段只生成了 0 帧」。"""
    path = tmp_path / "seg_0000.frames.mkv"
    path.write_bytes(b"\x1aE\xdf\xa3" + b"\x00" * 64)  # mkv 魔数 + 垃圾
    with pytest.raises(ValueError) as excinfo:
        director_cache.actual_frames(path)
    assert "读不出" in str(excinfo.value)


def test_actual_frames_falls_back_to_decoding_when_the_header_lies(tmp_path, monkeypatch):
    """容器头报 0 帧时逐帧解码兜底（``read_frames_gray`` 口径）。

    ``CAP_PROP_FRAME_COUNT`` 对 mkv 不一定可靠（FFV1 常无 ``nb_frames``），插件的
    ``_ffprobe_count_frames`` 也是真解码一遍才敢报数。头部不可信就必须落到解码。
    """
    path = _write_ffv1_mkv(tmp_path / "seg_0000.frames.mkv", 7)
    monkeypatch.setattr(director_cache, "video_meta", lambda _path: {"frames": 0, "fps": 24.0})
    assert director_cache.actual_frames(path) == 7


# ---------------------------------------------------------------------------
# 可证层判定（设计 §3.7）：通过 / 告警 / 报红
# ---------------------------------------------------------------------------

def _segment(index: int, segment_type: str, *, declared: int, export: int):
    """按 ``frames.py`` 的段类型派生 ``guided``——判据的唯一真源是类型，不是入参。"""
    from minimax_h3_prompt.director.frames import DirectorSegment, is_guided

    return DirectorSegment(
        index=index, type=segment_type, guided=is_guided(segment_type),
        start_frame=0, end_frame=export,
        declared_frames=declared, export_frames=export,
        summary="s", end_hook="h", give_images=(),
    )


def _guided(index: int = 2):
    """30 秒五段算例的第 2 段（续接 / 有引导）：声明 141、导出 153（设计 §3.2）。"""
    return _segment(index, "continue", declared=141, export=153)


def _unguided(index: int = 1):
    """第 1 段（开场 / 无引导）：声明与导出相等（ADR 0025）。"""
    return _segment(index, "opening", declared=107, export=107)


def test_guided_segment_passes_when_the_frames_match_the_plan():
    """有引导段：实际帧等于导出帧 → 通过。"""
    from minimax_h3_prompt.director.companion import judge_segment

    verdict = judge_segment(_guided(), actual_frames=153, cached_segments=(2,))
    assert verdict.status == "pass"
    assert verdict.passed is True


def test_guided_segment_warns_when_a_few_frames_were_trimmed():
    """有引导段被下一段相位对齐裁掉尾部几帧 → **告警**但仍算通过（容差 5）。

    为什么不能判「等于」：插件会因下一段的相位对齐把上一段尾部裁掉
    （``_phase_aligned_tail_start``，插件勘察 §4.3）。声明帧在网格上时 gap 恒 0，
    但那是算术推导，现场实裁几帧是正常形态，不能阻断。
    """
    from minimax_h3_prompt.director.companion import judge_segment

    verdict = judge_segment(_guided(), actual_frames=150, cached_segments=(2,))
    assert verdict.status == "warn"
    assert verdict.passed is True          # 容差内的告警不阻断（票面三种结论之一）
    assert "3" in verdict.message and "帧" in verdict.message


def test_guided_segment_fails_when_the_gap_exceeds_the_tolerance():
    """少 6 帧超过容差 5 → 报红：这不是相位对齐能解释的差额。"""
    from minimax_h3_prompt.director.companion import judge_segment

    verdict = judge_segment(_guided(), actual_frames=147, cached_segments=(2,))
    assert verdict.status == "fail"
    assert verdict.passed is False


def test_guided_segment_fails_when_it_has_more_frames_than_planned():
    """多于计划帧也报红：导出帧是定案值，多出来的帧说明缓存不是这一段的产物。"""
    from minimax_h3_prompt.director.companion import judge_segment

    verdict = judge_segment(_guided(), actual_frames=170, cached_segments=(2,))
    assert verdict.status == "fail"
    assert "170" in verdict.message


def test_unguided_segment_requires_exact_declared_frames():
    """无引导段：实际帧必须**等于**声明帧（没有相位对齐的裁尾理由，也没有容差）。"""
    from minimax_h3_prompt.director.companion import judge_segment

    assert judge_segment(_unguided(), actual_frames=107,
                         cached_segments=(1,)).status == "pass"
    short = judge_segment(_unguided(), actual_frames=106, cached_segments=(1,))
    assert short.status == "fail"
    assert "106" in short.message and "107" in short.message


def test_wrong_segment_selected_is_refused_not_silently_passed():
    """**勾错段报红**：请求第 4 段，缓存里只有第 1–3 段 → 必须报红，不许静默通过。

    这条是票面 AC3 的用例：用户在执行 `runSelection` 时勾错了段，缓存里根本不存在
    第 4 段的新帧——若按「段号不在缓存里就跳过」，账上会记一段没人生成过的段。
    """
    from minimax_h3_prompt.director.companion import judge_segment

    verdict = judge_segment(_guided(index=4), actual_frames=None,
                            cached_segments=(1, 2, 3))
    assert verdict.status == "fail"
    assert verdict.code == "SEGMENT_MISMATCH"
    assert "4" in verdict.message and "3" in verdict.message


def test_not_run_segment_is_refused():
    """该跑未跑（缓存目录里一段都没有）→ 报红并点名「还没跑」。"""
    from minimax_h3_prompt.director.companion import judge_segment

    verdict = judge_segment(_guided(), actual_frames=None, cached_segments=())
    assert verdict.status == "fail"
    assert verdict.code == "SEGMENT_NOT_RUN"


def test_a_later_segment_existing_does_not_mask_the_missing_one():
    """缓存里只有**后面**的段（跑到了 5 号却没有 4 号）同样是勾错段，不能放过。"""
    from minimax_h3_prompt.director.companion import judge_segment

    verdict = judge_segment(_guided(index=4), actual_frames=None,
                            cached_segments=(2, 3, 5))
    assert verdict.status == "fail"
    assert verdict.code == "SEGMENT_MISMATCH"


def test_tolerance_constant_is_the_single_source(monkeypatch):
    """**变异检验**：把容差常量改成 0，容差内的告警必须变成报红。

    常量必须在**调用时**取值（不能写成函数默认参数——默认参数在定义时求值，
    monkeypatch 改常量对它无效，「永不为红」的守卫等于没有）。
    """
    from minimax_h3_prompt.director import companion

    assert companion.judge_segment(_guided(), actual_frames=150,
                                   cached_segments=(2,)).status == "warn"
    monkeypatch.setattr(companion, "DIRECTOR_FRAME_TOLERANCE", 0)
    assert companion.judge_segment(_guided(), actual_frames=150,
                                   cached_segments=(2,)).status == "fail"


# ---------------------------------------------------------------------------
# 段产出账（设计 §3.6）：沿用 segments/segment-ledger.jsonl，只写 output / invalidate
# ---------------------------------------------------------------------------

def _output_row(**overrides):
    values = dict(
        segment=1, start_frame=0, end_frame=107, declared_frames=107,
        export_frames=107, actual_frames=107,
        cache_path=Path("cache/seg_0000.frames.mkv"),
    )
    # #112 起判定证据是成片视频；没显式给视频时拿（可能被覆盖后的）缓存路径顶替，
    # 只测账行形状的用例不必再造一份文件（账本只对**存在的**文件算摘要）。
    video = overrides.pop("video_path", None)
    values.update(overrides)
    values["video_path"] = values["cache_path"] if video is None else video
    return values


def test_output_row_records_video_evidence_with_and_without_a_cache(tmp_path):
    """通过行必须含**成片视频绝对路径与 video_sha256**（#112 AC1），缓存只是附带线索。

    两行合测「同一个来源」：判定证据只有 ``video``；``cache``/``sha256`` 在缓存确实
    存在时照记，不存在时写 ``null``——写空串会让「没有缓存」与「路径是空」混在一起。
    """
    import hashlib

    from minimax_h3_prompt.director import ledger
    from minimax_h3_prompt.ref_segments import read_segment_ledger, segment_ledger_path

    film = _write_ffv1_mkv(tmp_path / "films" / "segment-01.mkv", 107)
    frame_file = _write_ffv1_mkv(tmp_path / "cache" / "seg_0000.frames.mkv", 3)
    ledger.log_director_output(
        tmp_path, **_output_row(video_path=film, cache_path=frame_file))
    ledger.log_director_output(
        tmp_path, **_output_row(segment=2, video_path=film, cache_path=None))

    rows = read_segment_ledger(tmp_path, event=None)
    assert len(rows) == 2
    row = rows[0]
    assert row["event"] == "output"
    assert row["segment"] == 1
    assert row["actual_frames"] == 107
    assert row["declared_frames"] == 107 and row["export_frames"] == 107
    assert Path(row["video"]) == film.resolve()
    assert row["video_sha256"] == hashlib.sha256(film.read_bytes()).hexdigest()
    assert row["source"] == "video"
    assert Path(row["cache"]) == frame_file.resolve()
    assert row["sha256"] == hashlib.sha256(frame_file.read_bytes()).hexdigest()
    assert row["at"]
    # 缓存不存在：两个字段都在（键必须在），值是 null。
    assert rows[1]["cache"] is None and rows[1]["sha256"] is None
    # 落点是共用的那本账，不新开第三本（设计 §3.6）。
    assert segment_ledger_path(tmp_path).is_file()


def test_output_row_records_the_optional_endpoint_image(tmp_path):
    """终点图（挂在尾帧槽的那张）记进通过行；没有时写 null，键仍在。

    下一段写尾帧词时先找它：视频末帧每段都会漂，终点图是干净的目标准图。
    旧账没有这个键 → 读到 None，回退视频末帧（所以它不能进**必填**集）。
    """
    from minimax_h3_prompt.director import ledger
    from minimax_h3_prompt.ref_segments import read_segment_ledger

    film = _write_ffv1_mkv(tmp_path / "film-01.mkv", 107)
    endpoint = tmp_path / "endpoint-01.png"
    endpoint.write_bytes(b"fake-png")  # 账本只记路径，不解析图

    ledger.log_director_output(
        tmp_path, **_output_row(video_path=film, cache_path=None,
                                end_image_path=endpoint))
    ledger.log_director_output(
        tmp_path, **_output_row(segment=2, video_path=film, cache_path=None))

    rows = read_segment_ledger(tmp_path, event=None)
    assert Path(rows[0]["end_image"]) == endpoint.resolve()
    assert rows[1]["end_image"] is None


def test_director_ledger_only_writes_output_and_invalidate(tmp_path):
    """导演台**只写两类行**：``encounter`` 属于多参的语义层，导演台不写。"""
    from minimax_h3_prompt.director import ledger
    from minimax_h3_prompt.ref_segments import read_segment_ledger

    frame_file = _write_ffv1_mkv(tmp_path / "cache" / "seg_0000.frames.mkv", 3)
    ledger.log_director_output(tmp_path, **_output_row(cache_path=frame_file))
    ledger.log_director_invalidate(tmp_path, segment=1, reason="改了第 1 段的提示词")

    events = [row["event"] for row in read_segment_ledger(tmp_path, event=None)]
    assert events == ["output", "invalidate"]
    assert sorted(ledger.DIRECTOR_EVENTS) == ["invalidate", "output"]


def test_invalidate_row_beats_an_earlier_output_row(tmp_path):
    """``passed`` 判定＝按段号取**最后一条** output/invalidate 行，是 output 才算通过。

    直接用 ``ref_segments.passed_segment_rows`` 会把 ``invalidate`` 忽略掉（它只按
    event 过滤 output），于是「已作废的段」会被当成通过——这正是本仓「同一契约两处
    口径」的坑，判据只能住这里。
    """
    from minimax_h3_prompt.director import ledger

    frame_file = _write_ffv1_mkv(tmp_path / "cache" / "seg_0000.frames.mkv", 3)
    ledger.log_director_output(tmp_path, **_output_row(cache_path=frame_file))
    assert 1 in ledger.passed_director_rows(tmp_path)

    ledger.log_director_invalidate(tmp_path, segment=1, reason="上游段改词")
    assert ledger.passed_director_rows(tmp_path) == {}

    ledger.log_director_output(tmp_path, **_output_row(cache_path=frame_file))
    assert 1 in ledger.passed_director_rows(tmp_path)


def test_invalidate_of_another_segment_does_not_disturb_passed_rows(tmp_path):
    """作废第 2 段不许影响第 1 段的通过行（级联是 #106 的事，但读判据现在就要对）。"""
    from minimax_h3_prompt.director import ledger

    frame_file = _write_ffv1_mkv(tmp_path / "cache" / "seg_0000.frames.mkv", 3)
    ledger.log_director_output(tmp_path, **_output_row(cache_path=frame_file))
    ledger.log_director_invalidate(tmp_path, segment=2, reason="第 1 段改词")
    assert set(ledger.passed_director_rows(tmp_path)) == {1}


def test_progress_refuses_rows_that_lack_the_evidence(tmp_path):
    """通过行缺基本字段 → 报错。行写不全就不是「这段过了」的证据。

    一条只剩 ``{"event":"output","segment":1}`` 的行拿它跳段，等于放行一段没人
    生成过的段（多参那边 #58 复审实测过同形缺陷）。
    """
    from minimax_h3_prompt.director import ledger
    from minimax_h3_prompt.ref_segments import _append_row, segment_ledger_path

    _append_row(segment_ledger_path(tmp_path), {"event": "output", "segment": 1,
                                                "at": "2026-10-29T00:00:00+00:00"})
    with pytest.raises(ValueError) as excinfo:
        ledger.read_director_progress(tmp_path, total_segments=3)
    assert "cache" in str(excinfo.value) or "actual_frames" in str(excinfo.value)


def test_progress_treats_a_legacy_row_without_video_as_no_evidence(tmp_path):
    """#112 之前写的通过行（有缓存、没有 video）＝**无证据**：不算通过，也不许读崩。

    票面口径：续跑时这些段要重新贴视频，并在出站点名。如果因为缺新字段整本账读
    失败，用户会被卡在「连进陪跑都做不到」——这比静默沿用更糟、也不是本票要的。
    """
    import hashlib

    from minimax_h3_prompt.director import ledger
    from minimax_h3_prompt.ref_segments import _append_row, segment_ledger_path

    frame_file = _write_ffv1_mkv(tmp_path / "cache" / "seg_0000.frames.mkv", 3)
    _append_row(segment_ledger_path(tmp_path), {
        "event": "output", "segment": 1, "start_frame": 0, "end_frame": 107,
        "declared_frames": 107, "export_frames": 107, "actual_frames": 107,
        "cache": str(frame_file.resolve()),
        "sha256": hashlib.sha256(frame_file.read_bytes()).hexdigest(),
        "at": "2026-10-30T00:00:00+00:00",
    })

    assert ledger.read_director_progress(tmp_path, total_segments=3) == {}
    assert ledger.read_director_missing_video(tmp_path, total_segments=3) == (1,)


def test_progress_refuses_a_segment_number_outside_the_plan(tmp_path):
    """账上出现段表里没有的段号 → 报错（续跑不能拿一张对不上账的表）。"""
    from minimax_h3_prompt.director import ledger

    frame_file = _write_ffv1_mkv(tmp_path / "cache" / "seg_0000.frames.mkv", 3)
    ledger.log_director_output(tmp_path, **_output_row(segment=9, cache_path=frame_file))
    with pytest.raises(ValueError) as excinfo:
        ledger.read_director_progress(tmp_path, total_segments=5)
    assert "9" in str(excinfo.value)


def test_rotate_moves_the_previous_ledger_aside(tmp_path):
    """新交付开头轮转：复用 GEN 时追加账不会自动清空（#58 实测），续跑则不轮转。"""
    from minimax_h3_prompt.director import ledger
    from minimax_h3_prompt.ref_segments import segment_ledger_path

    frame_file = _write_ffv1_mkv(tmp_path / "cache" / "seg_0000.frames.mkv", 3)
    ledger.log_director_output(tmp_path, **_output_row(cache_path=frame_file))

    moved = ledger.rotate_director_ledger(tmp_path)
    assert moved is not None and moved.is_file()
    assert not segment_ledger_path(tmp_path).exists()
    assert ledger.passed_director_rows(tmp_path) == {}
    # 没有账可轮转时返回 None（首次交付）。
    assert ledger.rotate_director_ledger(tmp_path) is None


# ---------------------------------------------------------------------------
# 配置（设计 §4）：comfy_output_root 与 frame_tolerance 都从 config/agent.yaml 读
# ---------------------------------------------------------------------------

def _config_with(monkeypatch, director_raw: dict, *, comfy_env: str | None = None):
    """造一个只填了 ``director:`` 节的 Config——YAML 内容是我给的，不是仓里那份。"""
    from minimax_h3_prompt import config as config_module

    monkeypatch.setattr(config_module, "load_dotenv", lambda *_a, **_k: None)
    monkeypatch.setattr(config_module, "_load_yaml",
                        lambda _path: {"director": director_raw})
    if comfy_env is None:
        monkeypatch.delenv("H3_COMFY_OUTPUT_ROOT", raising=False)
    else:
        monkeypatch.setenv("H3_COMFY_OUTPUT_ROOT", comfy_env)
    return config_module.Config()


def test_comfy_output_root_comes_from_the_director_section(monkeypatch):
    """``comfy_output_root`` 从 ``director:`` 节读——**不写在模块里**（设计 §3.5）。"""
    cfg = _config_with(monkeypatch, {"comfy_output_root": "X:/some/comfy/output"})
    assert str(cfg.director_comfy_output_root()).replace("\\", "/") == "X:/some/comfy/output"


def test_comfy_output_root_env_var_wins(monkeypatch):
    """``H3_COMFY_OUTPUT_ROOT`` 覆盖 YAML：换机器时不必改仓库里的配置文件。"""
    cfg = _config_with(
        monkeypatch, {"comfy_output_root": "X:/some/comfy/output"},
        comfy_env="Y:/elsewhere/output")
    assert str(cfg.director_comfy_output_root()).replace("\\", "/") == "Y:/elsewhere/output"


def test_frame_tolerance_comes_from_the_director_section(monkeypatch):
    """``frame_tolerance`` 从 ``director:`` 节读，0 是合法值（严格判等）。"""
    assert _config_with(monkeypatch, {"frame_tolerance": 3}).director_frame_tolerance() == 3
    assert _config_with(monkeypatch, {"frame_tolerance": 0}).director_frame_tolerance() == 0


def test_frame_tolerance_defaults_to_the_companion_constant(monkeypatch):
    """没配时回落 ``companion.DIRECTOR_FRAME_TOLERANCE``——默认值只有一份真源。"""
    from minimax_h3_prompt.director import companion

    cfg = _config_with(monkeypatch, {})
    assert cfg.director_frame_tolerance() == companion.DIRECTOR_FRAME_TOLERANCE


def test_bad_frame_tolerance_is_refused(monkeypatch):
    """非整数/负数一律报错：容差是判据的一部分，配错了应当在这一步响亮报错。"""
    for bad in (True, "5", -1):
        cfg = _config_with(monkeypatch, {"frame_tolerance": bad})
        with pytest.raises(ValueError, match="frame_tolerance"):
            cfg.director_frame_tolerance()


def test_bad_comfy_output_root_is_refused(monkeypatch):
    """空路径/非字符串报错：空根会让缓存定位落在当前工作目录上。"""
    for bad in ("", "   ", 42):
        cfg = _config_with(monkeypatch, {"comfy_output_root": bad})
        with pytest.raises(ValueError, match="comfy_output_root"):
            cfg.director_comfy_output_root()


def test_shipped_yaml_carries_both_keys():
    """仓里的 ``config/agent.yaml`` 必须真的写了这两个键（票面第 5 条）。"""
    import yaml

    from minimax_h3_prompt.config import CONFIG_PATH

    raw = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    director = raw["director"]
    assert director["comfy_output_root"] == "D:/Comfyui/ComfyUI/output"
    assert isinstance(director["frame_tolerance"], int)


# ---------------------------------------------------------------------------
# 末帧导出（#107 的起点画面由 #105 的缓存读取口给，设计 §3.3）
# ---------------------------------------------------------------------------

def test_export_last_frame_writes_a_real_png(tmp_path):
    """把缓存帧文件的**最后一帧**导出成 png——尾帧变换提示词的参考图。

    为什么取末帧：下一段的起点画面就是上一段的终点画面（设计 §3.3：输入是
    「上一段缓存末帧」）。灰度流不够——写词模型要看的是画面本身。
    """
    from minimax_h3_prompt.director import cache

    frames_file = _write_ffv1_mkv(tmp_path / "seg_0000.frames.mkv", 9, width=32, height=24)
    out = cache.export_last_frame(frames_file, tmp_path / "frames" / "frame-01-last.png")

    assert out.is_file() and out.suffix == ".png"
    image = cv2.imread(str(out))
    assert image is not None
    assert image.shape[:2] == (24, 32)


def test_export_last_frame_writes_to_a_chinese_path(tmp_path):
    """末帧图必须能落到**中文会话目录**（2026-10-31 真实验收现场）。

    现场：会话目录名是「一段刑警侦探……」，``export_last_frame`` 报「目录不可写或
    扩展名不支持」；探针实测目录可写、帧也正常，真正原因是 Windows 上
    ``cv2.imwrite`` 对非 ASCII 路径返回 False。同一帧 ``imencode`` + ``tofile``
    可以写——所以修法是统一走 Unicode 安全写口，而不是改文案。
    """
    from minimax_h3_prompt.director import cache
    from minimax_h3_prompt.tools.frame_match import imread_unicode

    frames_file = _write_ffv1_mkv(tmp_path / "seg_0000.frames.mkv", 9, width=32, height=24)
    out = cache.export_last_frame(
        frames_file, tmp_path / "中文目录" / "frame-01-last.png")

    assert out.is_file() and out.suffix == ".png"
    # 读回也要走 Unicode 安全口：cv2.imread 在这里同样打不开。
    image = imread_unicode(out)
    assert image is not None
    assert image.shape[:2] == (24, 32)


def test_export_last_frame_restarts_from_frame_zero_when_the_seek_fails(
        tmp_path, monkeypatch):
    """头可信、seek 不可信：回退循环必须**从第 0 帧重读**，不许接着 seek 后的位置读。

    2026-10-31 复审实测形态：``CAP_PROP_FRAME_COUNT`` 报了正数，但
    ``set(POS_FRAMES, total-1)`` 之后 ``read()`` 失败；旧回退直接 ``while read()``
    从当前位置续读，读到的是空/错的位置，末帧根本取不到（或取成别的段）。假 capture
    把「seek 之后读失败、从 0 起读正常」这条现场形态钉死。
    """
    import numpy as np

    from minimax_h3_prompt.director import cache

    frames = [np.full((4, 5, 3), value, dtype=np.uint8) for value in (10, 20, 30)]

    class _HeaderOnlyCapture:
        """头可信（报 3 帧）、只要 seek 到非 0 位置，之后 read 一律失败。"""

        def __init__(self, _path):
            self._pos = 0
            self._failed_seek = False

        def isOpened(self):
            return True

        def get(self, _prop):
            return len(frames)

        def set(self, _prop, value):
            self._pos = int(value)
            self._failed_seek = int(value) != 0   # 这个样本的 seek 只在 0 上可靠
            return True

        def read(self):
            if self._failed_seek:
                return False, None
            if self._pos >= len(frames):
                return False, None
            frame = frames[self._pos]
            self._pos += 1
            return True, frame

        def release(self):
            pass

    monkeypatch.setattr(cache.cv2, "VideoCapture", _HeaderOnlyCapture)
    path = tmp_path / "seg_0000.frames.mkv"
    path.write_bytes(b"decode-is-stubbed-by-the-fake-capture")

    out = cache.export_last_frame(path, tmp_path / "last.png")
    image = cv2.imread(str(out))
    assert image is not None
    assert int(image[0, 0, 0]) == 30, "回退必须从第 0 帧重读，末帧才是第 3 帧"


def test_export_last_frame_refuses_a_missing_file(tmp_path):
    """帧文件不在 → 报错，不写一张假图（用户会拿它去生成终点画面）。"""
    from minimax_h3_prompt.director import cache

    with pytest.raises(ValueError) as excinfo:
        cache.export_last_frame(tmp_path / "nope.frames.mkv", tmp_path / "out.png")
    assert "nope.frames.mkv" in str(excinfo.value)


# ---------------------------------------------------------------------------
# 向导逐段陪跑（设计 §4 / §3.7）
# ---------------------------------------------------------------------------

def _director_brief(**overrides):
    from minimax_h3_prompt.brief_parser import Brief

    values = dict(mode="director", variant="T2VA", director_mode="t2v",
                  duration=30.0, plot="雨夜旧信",
                  director_node_id="42", director_cache_name="雨夜",
                  director_continuity_mode="guide")
    values.update(overrides)
    return Brief(**values)


def _plan_types(types: tuple[str, ...], *, mode: str = "t2v"):
    """按段类型造一张**读得回**的段表（帧数全走 frames.py，不手写余数）。"""
    from minimax_h3_prompt.director.frames import (
        DirectorPlan, DirectorSegment, export_frames_for_declared, give_images_for,
        is_guided,
    )

    segments = []
    cursor = 0
    for index, segment_type in enumerate(types, 1):
        guided = is_guided(segment_type)
        declared = 141 if guided else 107
        export = export_frames_for_declared(declared, guided=guided)
        segments.append(DirectorSegment(
            index=index, type=segment_type, guided=guided,
            start_frame=cursor, end_frame=cursor + export,
            declared_frames=declared, export_frames=export,
            summary=f"段{index}概述", end_hook=f"段{index}落定画面",
            give_images=tuple(give_images_for(segment_type, mode)),
        ))
        cursor += export
    return DirectorPlan(mode=mode, total_frames=cursor, segments=tuple(segments))


def _director_session(tmp_path: Path, types: tuple[str, ...] = ("opening", "continue"),
                      *, mode: str = "t2v"):
    """落一张段表 + 段提示词文件，返回「陪跑进行中」的会话（模拟续跑读到的东西）。"""
    from minimax_h3_prompt.director.planning import plan_path, write_plan_json
    from minimax_h3_prompt.director.prompts import segment_path
    from minimax_h3_prompt.session_store import (
        STATUS_DIRECTOR_SEGMENTS_RUNNING, SessionState,
    )

    directory = tmp_path / "GEN001"
    plan = _plan_types(types, mode=mode)
    write_plan_json(plan_path(directory), plan)
    for segment in plan.segments:
        path = segment_path(directory, segment.index)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"段 {segment.index} 的提示词\n", encoding="utf-8")
    return SessionState(directory=directory,
                        brief=_director_brief(director_mode=mode),
                        stage_state={}, status=STATUS_DIRECTOR_SEGMENTS_RUNNING,
                        # #179 起画幅核对按创建时间判新旧：这些用例模拟的是 #161
                        # 之后的新会话，必须带一个落地时刻之后的时间。
                        created_at="2026-11-05T09:00:00+00:00")


def _fl2v_tailframe_session(tmp_path: Path, monkeypatch):
    """尾帧词接线的陪跑会话：**fl2v**，第 2 段是运镜段。

    尾帧词本来就只对「有尾帧槽的段」有意义（#200）：t2v 的给图契约归零后，这里
    必须迁到 fl2v，否则轮到第 2 段时不再触发写词，用例测的东西就消失了。
    期望画布对齐夹具视频的 32×24：fl2v 逐段贴回会核画幅，而这些用例验的不是画幅。
    """
    from minimax_h3_prompt.director import aspect_flow

    monkeypatch.setattr(aspect_flow, "frame_canvas", lambda *args, **kwargs: (32, 24))
    return _director_session(tmp_path, ("opening", "camera_same"), mode="fl2v")


def _cache_dir_for(config, brief) -> Path:
    from minimax_h3_prompt.director import cache

    return cache.cache_dir(config.director_comfy_output_root(),
                           brief.director_cache_name, brief.director_node_id)


def _plant_cache(directory: Path, segment: int, frames: int) -> Path:
    """在**测试自己的**缓存目录里造一段真 FFV1 帧文件（绝不碰真实 ComfyUI 输出）。"""
    from minimax_h3_prompt.director import cache

    return _write_ffv1_mkv(cache.frames_path(directory, segment), frames)


def _run_config(monkeypatch, tmp_path: Path):
    return _config_with(monkeypatch, {"comfy_output_root": str(tmp_path)})


def _is_rerun_question(text: str) -> bool:
    """陪跑首尾那两问（#106）：作答按**问题文本**匹配，不按调用次序数。

    按次序数会在每次加一问时整体错位（#57 实测：追加一行输入被上游菜单吃掉，流程挪
    一步还发了真请求）——所以判据是问题里那句话。
    """
    return "有要重跑的段吗" in text or "有要改哪一段重来吗" in text


def _is_segment_question(text: str) -> bool:
    """「第 N 段跑完后贴本段成片视频路径」那一问（每段一次；#112 起统一为视频）。"""
    return "成片视频路径" in text


def test_director_running_status_is_resumable_and_distinct(tmp_path):
    """新状态必须在 ``find_awaiting_sessions`` 的可续接元组里，且不复用多参那个值。

    漏登记＝中断的导演台会话不会被列出，用户只能重开 GEN（#17/#58 同形代价）。
    """
    from minimax_h3_prompt.session_store import (
        STATUS_DIRECTOR_SEGMENTS_RUNNING, STATUS_REF_SEGMENTS_RUNNING,
        find_awaiting_sessions,
    )

    assert STATUS_DIRECTOR_SEGMENTS_RUNNING != STATUS_REF_SEGMENTS_RUNNING
    session = _director_session(tmp_path, ("opening",))
    from minimax_h3_prompt.session_store import save_session

    save_session(session.directory, session.brief, {}, status=session.status)
    found = find_awaiting_sessions(tmp_path)
    assert [item.directory for item in found] == [session.directory]


def test_handoff_passes_every_segment_and_records_the_ledger(tmp_path, monkeypatch, capsys):
    """逐段陪跑主流程：贴成片视频 → 按视频帧数判定 → 写含 video 的通过行（#112）。

    本用例**一段缓存都不造**：插件默认 raw 编码写出的东西本项目读不了，缓存存在与
    否不该成为拦路条件（票面 AC1「段缓存不存在也不报这一段还没跑」）。
    """
    import hashlib

    from minimax_h3_prompt.director import ledger
    from minimax_h3_prompt.ui import wizard

    session = _director_session(tmp_path)
    config = _run_config(monkeypatch, tmp_path)
    film_one = _write_ffv1_mkv(tmp_path / "film-01.mkv", 107)   # 无引导段：等于声明帧
    film_two = _write_ffv1_mkv(tmp_path / "film-02.mkv", 153)   # 有引导段：等于导出帧

    monkeypatch.setattr(wizard, "_drain_stdin", lambda: None)
    asked: list[str] = []

    def fake_prompt(text: str) -> str:
        asked.append(text)
        if _is_segment_question(text):
            return str(film_one) if "第 1/2 段" in text else str(film_two)
        # 全段通过后接的是**交付**（#108）：本用例只验陪跑落账，交付一进来就停下。
        if "成片 mp4 路径" in text:
            return "q"
        return ""          # 尾问（有要改哪一段重来吗）答「不用」

    monkeypatch.setattr(wizard, "_prompt", fake_prompt)

    assert wizard._resume_director_handoff(config, session) == 0

    out = capsys.readouterr().out
    # 出站必须写明只用 runSelection 勾当前段（设计 §3.7 最后一条）。
    assert "runSelection" in out and "第 1 段" in out and "第 2 段" in out
    # #112：简报不再把段缓存当证据，也不许报「这一段还没跑」；但多段仍要给出缓存
    # 目录**只供定位**（2026-10-31 复审采纳：用户要找现场产物时得有路径）。
    assert "段缓存目录" in out and str(_cache_dir_for(config, session.brief)) in out
    assert "只供定位" in out and "不是判定依据" in out
    assert "还没跑" not in out
    # 全新开跑（账里没有通过行）不问「有要重跑的段吗」。
    assert not any(_is_rerun_question(text) and "有要重跑的段吗" in text
                   for text in asked)
    # 全通过后下一步是交付（#108）：成片与最终包两条路径由用户逐项给。
    assert any("成片 mp4 路径" in question for question in asked)
    assert "停在交付前" in out
    rows = ledger.passed_director_rows(session.directory)
    assert sorted(rows) == [1, 2]
    assert rows[1]["actual_frames"] == 107
    assert Path(rows[1]["video"]) == film_one.resolve()
    assert rows[1]["video_sha256"] == hashlib.sha256(film_one.read_bytes()).hexdigest()
    assert rows[1]["source"] == "video"
    # 缓存不存在：键在、值是 null，且不参与判定。
    assert rows[1]["cache"] is None and rows[1]["sha256"] is None
    assert rows[2]["actual_frames"] == 153
    assert Path(rows[2]["video"]) == film_two.resolve()


def test_r2v_camera_cut_gets_no_firstframe_prompt(tmp_path, monkeypatch, capsys):
    """#149：r2v 的 camera_cut 没有首帧槽——不产首帧词，也不提示挂首帧。"""
    from minimax_h3_prompt.director.firstframe import firstframe_path
    from minimax_h3_prompt.ui import wizard

    session = _director_session(tmp_path, ("opening", "camera_cut"), mode="r2v")
    config = _run_config(monkeypatch, tmp_path)
    # r2v 两段都是无引导段：导出帧＝声明帧 107。
    film_one = _write_ffv1_mkv(tmp_path / "film-01.mkv", 107)
    film_two = _write_ffv1_mkv(tmp_path / "film-02.mkv", 107)

    def fake_prompt(text: str) -> str:
        if _is_segment_question(text):
            return str(film_one) if "第 1/2 段" in text else str(film_two)
        if "成片 mp4 路径" in text:
            return "q"
        return ""

    monkeypatch.setattr(wizard, "_drain_stdin", lambda: None)
    monkeypatch.setattr(wizard, "_prompt", fake_prompt)

    assert wizard._resume_director_handoff(config, session) == 0

    out = capsys.readouterr().out
    assert "段首帧" not in out, "r2v 的切镜段没有首帧槽，不该产生首帧词"
    assert "挂**首帧**" not in out, "r2v 不该提示去挂一张不存在的首帧图"
    assert not firstframe_path(session.directory, 2).exists()


def test_cut_first_frame_path_q_stops_cleanly_with_code_zero(
        tmp_path, monkeypatch, capsys):
    """#149：切镜首帧路径问到时输入 q＝干净停下（退出码 0），不回退上一段终点图。"""
    from minimax_h3_prompt.director import ledger
    from minimax_h3_prompt.director.firstframe import firstframe_path
    from minimax_h3_prompt.ui import wizard

    session = _director_session(tmp_path, ("opening", "camera_cut"), mode="fl2v")
    config = _run_config(monkeypatch, tmp_path)
    film_one = _write_ffv1_mkv(tmp_path / "film-01.mkv", 107)
    # #158 起贴回成片要核画幅：开场锚图做成与测试视频同比例，别让本用例被画幅拦下。
    _write_canvas_png(session.directory / "director" / "assets" / "01" / "start.png")
    # 段首帧词已在盘上：跳过写词（否则会真建模型），直接走到「贴首帧图」那一问。
    cut_word = firstframe_path(session.directory, 2)
    cut_word.parent.mkdir(parents=True, exist_ok=True)
    cut_word.write_text("A rider under the awning.", encoding="utf-8")
    asked: list[str] = []

    def fake_prompt(text: str) -> str:
        asked.append(text)
        if _is_segment_question(text):
            return str(film_one)
        if "首帧路径" in text:
            return "q"
        return ""

    monkeypatch.setattr(wizard, "_drain_stdin", lambda: None)
    monkeypatch.setattr(wizard, "_prompt", fake_prompt)

    assert wizard._resume_director_handoff(config, session, llm=object()) == 0

    out = capsys.readouterr().out
    assert "停在" in out and "不落账" in out
    assert any("首帧路径" in question for question in asked)
    rows = ledger.passed_director_rows(session.directory)
    assert sorted(rows) == [1], "第 2 段在拿到首帧图之前就停下了，不该落账"


def test_handoff_refuses_a_mistaken_selection_then_accepts_the_rerun(
        tmp_path, monkeypatch, capsys):
    """**帧数对不上报红**（#112 形态）：第 2 段先贴错片 → 不落账；贴对后才通过。

    旧版靠「缓存里没有这一段」识别勾错段；证据换成视频后，勾错段/贴错片的可见形态
    就是**帧数与计划不符**。判定仍复用 ``judge_video_evidence``（同一份帧数口径）。
    """
    from minimax_h3_prompt.director import ledger
    from minimax_h3_prompt.ui import wizard

    session = _director_session(tmp_path)
    config = _run_config(monkeypatch, tmp_path)
    film_one = _write_ffv1_mkv(tmp_path / "film-01.mkv", 107)
    wrong = _write_ffv1_mkv(tmp_path / "film-02-wrong.mkv", 141)   # 少 12 帧，超容差
    right = _write_ffv1_mkv(tmp_path / "film-02.mkv", 153)
    segment_asks = {"n": 0}

    def fake_prompt(text: str) -> str:
        if _is_segment_question(text):
            segment_asks["n"] += 1
            if "第 1/2 段" in text:
                return str(film_one)
            return str(wrong) if segment_asks["n"] == 2 else str(right)
        if "成片 mp4 路径" in text:   # #108 的交付：本用例只数陪跑问答
            return "q"
        return ""          # 尾问答「不用」

    monkeypatch.setattr(wizard, "_drain_stdin", lambda: None)
    monkeypatch.setattr(wizard, "_prompt", fake_prompt)

    assert wizard._resume_director_handoff(config, session) == 0
    out = capsys.readouterr().out
    assert "FRAME_SHORT" in out and "超出容差" in out
    # #113：判定不合格的出口从「重跑本段 / 停下」扩成三档，改词入口要看得见。
    assert "输入 e" in out and "改本段提示词" in out
    assert segment_asks["n"] == 3, "报红那一次不该落账，必须再问一次视频路径"
    assert sorted(ledger.passed_director_rows(session.directory)) == [1, 2]


def test_handoff_skips_segments_already_passed(tmp_path, monkeypatch, capsys):
    """续跑：账上已通过的段不重跑、不重问（不消耗视频路径）。"""
    from minimax_h3_prompt.director import ledger
    from minimax_h3_prompt.ui import wizard

    session = _director_session(tmp_path)
    config = _run_config(monkeypatch, tmp_path)
    film_one = _write_ffv1_mkv(tmp_path / "film-01.mkv", 107)
    ledger.log_director_output(
        session.directory, segment=1, start_frame=0, end_frame=107,
        declared_frames=107, export_frames=107, actual_frames=107,
        video_path=film_one)
    film_two = _write_ffv1_mkv(tmp_path / "film-02.mkv", 153)

    asked: list[str] = []

    def fake_prompt(text: str) -> str:
        asked.append(text)
        if _is_segment_question(text):
            return str(film_two)
        if "成片 mp4 路径" in text:   # #108 的交付：本用例只数陪跑问答
            return "q"
        return ""          # 首问（有要重跑的段吗）与尾问都答「不用」

    monkeypatch.setattr(wizard, "_drain_stdin", lambda: None)
    monkeypatch.setattr(wizard, "_prompt", fake_prompt)

    assert wizard._resume_director_handoff(config, session) == 0
    out = capsys.readouterr().out
    assert "已通过验收，跳过" in out and "第 1/2 段" in out
    # 续跑且账里有有效通过行 → 首问照问一次。
    assert sum(1 for text in asked if "有要重跑的段吗" in text) == 1
    segment_asks = [text for text in asked if _is_segment_question(text)]
    assert len(segment_asks) == 1, "只该为第 2 段问一次视频路径"
    assert "第 2/2 段" in segment_asks[0]


def test_handoff_stops_cleanly_without_recording_a_failed_segment(
        tmp_path, monkeypatch, capsys):
    """输入 q 停下：不落账、不报错（进度留在账本上，重跑向导接着走）。"""
    from minimax_h3_prompt.director import ledger
    from minimax_h3_prompt.ui import wizard

    session = _director_session(tmp_path)
    config = _run_config(monkeypatch, tmp_path)
    monkeypatch.setattr(wizard, "_drain_stdin", lambda: None)
    # 全新开跑不问首问（账里没有通过行）；q 按在「贴成片视频路径」那一问上。
    monkeypatch.setattr(wizard, "_prompt", lambda text: "q")

    assert wizard._resume_director_handoff(config, session) == 0
    assert ledger.passed_director_rows(session.directory) == {}
    assert "停在轮到第 1/2 段" in capsys.readouterr().out


def test_handoff_does_not_let_a_raw_cache_block_the_video_evidence(
        tmp_path, monkeypatch, capsys):
    """插件默认 ``cache_frames_codec="raw"`` 的 ``.pt`` **不再拦路**（#112 核心）。

    旧陪跑只认缓存帧数，遇到 ``.pt`` 就打处置、重问，用户第 1 段都走不到（2026-10-31
    现场）。现在判定只认用户贴回来的成片视频：缓存是可选项，有就照记（连同摘要），
    没有就写 null。
    """
    import hashlib

    from minimax_h3_prompt.director import cache, ledger
    from minimax_h3_prompt.ui import wizard

    session = _director_session(tmp_path, ("opening",))
    config = _run_config(monkeypatch, tmp_path)
    cache_directory = _cache_dir_for(config, session.brief)
    # 只造 `.pt`（torch 张量，本项目没有 torch 读不了），没有任何 `.frames.mkv`。
    raw = cache.raw_frames_path(cache_directory, 1)
    raw.parent.mkdir(parents=True, exist_ok=True)
    raw.write_bytes(b"torch uint8 [N,H,W,3] placeholder")
    film = _write_ffv1_mkv(tmp_path / "film.mkv", 107)

    def fake_prompt(text: str) -> str:
        if _is_segment_question(text):
            return str(film)
        if "成片 mp4 路径" in text:
            return "q"
        return ""

    monkeypatch.setattr(wizard, "_drain_stdin", lambda: None)
    monkeypatch.setattr(wizard, "_prompt", fake_prompt)

    assert wizard._resume_director_handoff(config, session) == 0
    out = capsys.readouterr().out

    assert "cache_frames_codec" not in out, "缓存编码不该再是拦路条件"
    assert "SEGMENT_NOT_RUN" not in out and "还没跑" not in out
    row = ledger.passed_director_rows(session.directory)[1]
    assert row["actual_frames"] == 107, "判定要按视频帧数，不是缓存"
    assert Path(row["cache"]) == raw.resolve(), "缓存存在时要照记（含 raw .pt）"
    assert row["sha256"] == hashlib.sha256(raw.read_bytes()).hexdigest()
    assert Path(row["video"]) == film.resolve()


def test_handoff_writes_the_tailframe_prompt_from_the_previous_video(
        tmp_path, monkeypatch, capsys):
    """轮到运镜段时产出 ``tailframe-NN.md``，起点画面取**上一段视频的末帧**（#114）。

    #111 的现场是插件缓存为 ``raw`` 编码、本项目读不了；旧实现在这里取
    ``cache.frames_path`` 失败就中止整轮陪跑。现在起点画面只从账内上一段的 ``video``
    剥帧，所以本用例**一个段缓存都不造**——「缓存不存在也能出词」就是票面 AC1。

    末帧用可辨纯色视频钉住：取到邻帧、或偷偷改回缓存，断言都会红。
    """
    from minimax_h3_prompt.director import tailframe
    from minimax_h3_prompt.ui import wizard

    session = _fl2v_tailframe_session(tmp_path, monkeypatch)
    config = _run_config(monkeypatch, tmp_path)
    colors_one = _frame_colors(107)
    film_one = _write_solid_ffv1_mkv(tmp_path / "film-01.mkv", colors_one)
    film_two = _write_solid_ffv1_mkv(tmp_path / "film-02.mkv", _frame_colors(153))

    good = (
        "### 变换指令\n以这张输入图为参考：镜头推到雨幕后的门框，起点地标现在在身后，人物脚下是石阶，门框在前方几步。\n\n"
        "### 保留项声明\n保持雨衣的灰色与招牌的橙光不变。\n\n"
        "### 锁死清单\n- 雨衣的灰色\n- 招牌的橙光\n"
    )

    class _TailLLM:
        def invoke(self, request, config=None):
            return good

    seen_images: list[Path] = []

    def fake_describe(image, instruction=""):
        seen_images.append(Path(image))
        return "起止画面：雨衣的人是灰色的，招牌是橙色的。"

    monkeypatch.setattr(tailframe, "describe_image", fake_describe)
    monkeypatch.setattr(wizard, "_drain_stdin", lambda: None)

    def fake_prompt(text: str) -> str:
        if _is_segment_question(text):
            return str(film_one) if "第 1/2 段" in text else str(film_two)
        if "成片 mp4 路径" in text:   # 本用例只看尾帧接线，交付一问就停
            return "q"
        return ""          # 尾问答「不用」

    monkeypatch.setattr(wizard, "_prompt", fake_prompt)

    assert wizard._resume_director_handoff(
        config, session, llm=_TailLLM()) == 0

    text = (session.directory / "director" / "tailframe-02.md").read_text(encoding="utf-8")
    assert "变换指令" in text and "锁死清单" in text
    assert seen_images, "起点画面必须真读一次图"
    assert seen_images[0].name == "frame-01-last.png"
    # 票面 AC1：缓存目录自始至终不存在，尾帧词照样产出（raw 缓存不再拦路）。
    assert not _cache_dir_for(config, session.brief).exists(), "本用例故意不造段缓存"
    exported = cv2.imread(str(session.directory / "director" / "frame-01-last.png"))
    assert exported is not None
    last_red, last_green, last_blue = colors_one[-1]
    assert exported.tobytes() == _solid_bgr_bytes(last_red, last_green, last_blue), \
        "起点画面必须是上一段视频的**最后一帧**"
    prev_red, prev_green, prev_blue = colors_one[-2]
    assert exported.tobytes() != _solid_bgr_bytes(prev_red, prev_green, prev_blue), \
        "取到邻帧就是错的（末帧与邻帧在 fixture 里刻意不同）"
    out = capsys.readouterr().out
    assert "尾帧" in out
    # 2026-10-31 验收反馈：生成前要有进度；生成后终端只展示可直接复制的两节正文，
    # 锁死清单不再展示（它留在文件里做生成后的核对）。
    assert "正在取上一段起点画面并写终点画面提示词" in out
    assert "镜头推到雨幕后的门框" in out
    assert "保持雨衣的灰色与招牌的橙光不变" in out
    assert "### 锁死清单" not in out
    # 官方编辑口径是一整段 prompt：段头也不展示给用户复制（文件里仍有三段式）。
    assert "### 变换指令" not in out
    assert "### 保留项声明" not in out
    assert "锁死清单已留在文件里" in out


def test_tailframe_start_frame_ignores_the_segment_cache(
        tmp_path, monkeypatch, capsys):
    """同一段既有缓存又有视频时，起点画面取**视频**末帧——缓存不许顶替（#114）。

    缓存与视频在这里是两份内容不同的 FFV1：视频每帧一种纯色，缓存整条是另一种深灰。
    若实现还从 ``cache.frames_path`` 取帧，导出的 png 会变成缓存的颜色，断言必红。
    """
    from minimax_h3_prompt.director import tailframe
    from minimax_h3_prompt.ui import wizard

    session = _fl2v_tailframe_session(tmp_path, monkeypatch)
    config = _run_config(monkeypatch, tmp_path)
    cache_directory = _cache_dir_for(config, session.brief)
    # 缓存刻意与视频不同色：它只是「当时盘上有没有产物」的现场线索。
    _write_solid_ffv1_mkv(director_cache.frames_path(cache_directory, 1),
                          [(9, 9, 9)] * 107)
    colors_one = _frame_colors(107)
    film_one = _write_solid_ffv1_mkv(tmp_path / "film-01.mkv", colors_one)
    film_two = _write_solid_ffv1_mkv(tmp_path / "film-02.mkv", _frame_colors(153))

    good = (
        "### 变换指令\n以这张输入图为参考：镜头推到雨幕后的门框，起点地标现在在身后，人物脚下是石阶，门框在前方几步。\n\n"
        "### 保留项声明\n保持雨衣的灰色与招牌的橙光不变。\n\n"
        "### 锁死清单\n- 雨衣的灰色\n- 招牌的橙光\n"
    )

    class _TailLLM:
        def invoke(self, request, config=None):
            return good

    monkeypatch.setattr(tailframe, "describe_image",
                        lambda image, instruction="": "起止画面：灰色雨衣、橙色招牌。")
    monkeypatch.setattr(wizard, "_drain_stdin", lambda: None)

    def fake_prompt(text: str) -> str:
        if _is_segment_question(text):
            return str(film_one) if "第 1/2 段" in text else str(film_two)
        if "成片 mp4 路径" in text:   # 交付一问就停
            return "q"
        return ""

    monkeypatch.setattr(wizard, "_prompt", fake_prompt)

    assert wizard._resume_director_handoff(
        config, session, llm=_TailLLM()) == 0

    exported = cv2.imread(str(session.directory / "director" / "frame-01-last.png"))
    assert exported is not None
    last_red, last_green, last_blue = colors_one[-1]
    assert exported.tobytes() == _solid_bgr_bytes(last_red, last_green, last_blue), \
        "起点画面要取账内视频的末帧，不能拿段缓存顶替"
    assert exported.tobytes() != _solid_bgr_bytes(9, 9, 9), "缓存颜色出现＝取错了来源"


def test_tailframe_prefers_the_previous_endpoint_image_over_the_video_tail(
        tmp_path, monkeypatch, capsys):
    """上一段账里有终点图 → 起点画面用它；视频末帧只作兜底（2026-10-31）。

    视频末帧每段都会漂（实跑：第 3 段起墨镜、第 4 段胡子），拿它当参考会把漂移
    逐段放大；终点图是干净的目标准图。这里断言读图拿到的是终点图，且用了它之后
    不再导出 ``frame-01-last.png``。
    """
    from minimax_h3_prompt.director import ledger, tailframe
    from minimax_h3_prompt.ui import wizard

    session = _fl2v_tailframe_session(tmp_path, monkeypatch)
    config = _run_config(monkeypatch, tmp_path)
    film_one = _write_solid_ffv1_mkv(tmp_path / "film-01.mkv", _frame_colors(107))
    endpoint = tmp_path / "endpoint-01.png"
    endpoint.write_bytes(b"fake-png")  # 读图被打桩，只要求路径存在
    ledger.log_director_output(
        session.directory, segment=1, start_frame=0, end_frame=107,
        declared_frames=107, export_frames=107, actual_frames=107,
        video_path=film_one, end_image_path=endpoint)
    film_two = _write_solid_ffv1_mkv(tmp_path / "film-02.mkv", _frame_colors(153))

    good = (
        "### 变换指令\n以这张输入图为参考：镜头推到雨幕后的门框，起点地标现在在身后，人物脚下是石阶，门框在前方几步。\n\n"
        "### 保留项声明\n保持雨衣的灰色与招牌的橙光不变。\n\n"
        "### 锁死清单\n- 雨衣的灰色\n- 招牌的橙光\n"
    )

    class _TailLLM:
        def invoke(self, request, config=None):
            return good

    seen: list[Path] = []

    def fake_describe(image, instruction=""):
        seen.append(Path(image))
        return "起止画面：灰色雨衣、橙色招牌。"

    monkeypatch.setattr(tailframe, "describe_image", fake_describe)
    monkeypatch.setattr(wizard, "_drain_stdin", lambda: None)

    def fake_prompt(text: str) -> str:
        if _is_segment_question(text):
            return str(film_two)
        if "成片 mp4 路径" in text:   # 交付一问就停
            return "q"
        return ""

    monkeypatch.setattr(wizard, "_prompt", fake_prompt)

    assert wizard._resume_director_handoff(
        config, session, llm=_TailLLM()) == 0

    assert seen and seen[0] == endpoint.resolve(), "起点画面应取账内终点图"
    assert not (session.directory / "director" / "frame-01-last.png").exists(), \
        "用了终点图就不再导出视频末帧"


def test_handoff_reuses_an_existing_tailframe_prompt_even_without_the_video(
        tmp_path, monkeypatch, capsys):
    """盘上已有该段的终点画面提示词 → 沿用、不重付、不覆盖（#58 的同一条教训）。

    这一段用户可能已经拿旧词生成过终点画面并挂上去了；重写一份新词会让「挂上去的图」
    与文件里的词不再对应（而账上不会记这件事）。

    #114 的顺序硬要求：**先查词、后取视频末帧**。反过来，用户在拿到词之后清理了
    上一段视频再续跑，就会从「沿用」变成「报错」——他在导演台里已经挂好的终点画面
    也就白挂了。本用例故意把上一段视频删掉。
    """
    from minimax_h3_prompt.director import ledger, tailframe
    from minimax_h3_prompt.ui import wizard

    session = _fl2v_tailframe_session(tmp_path, monkeypatch)
    config = _run_config(monkeypatch, tmp_path)
    film_one = _write_solid_ffv1_mkv(tmp_path / "film-01.mkv", _frame_colors(107))
    ledger.log_director_output(
        session.directory, segment=1, start_frame=0, end_frame=107,
        declared_frames=107, export_frames=107, actual_frames=107,
        video_path=film_one)
    film_one.unlink()          # 词在盘上、视频被删（用户清理过中间文件）
    film_two = _write_solid_ffv1_mkv(tmp_path / "film-02.mkv", _frame_colors(153))

    old_text = (
        "### 变换指令\n旧词：以这张输入图为参考，镜头推到雨幕后的门框，起点地标现在在身后，人物脚下是石阶，门框在前方几步。\n\n"
        "### 保留项声明\n保持雨衣的灰色与招牌的橙光不变。\n\n"
        "### 锁死清单\n- 雨衣的灰色\n- 招牌的橙光\n"
    )
    path = session.directory / "director" / "tailframe-02.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(old_text, encoding="utf-8")

    def _boom(*args, **kwargs):
        raise AssertionError("已有尾帧文档还去读图/重写")

    monkeypatch.setattr(tailframe, "describe_image", _boom)
    monkeypatch.setattr(wizard, "_drain_stdin", lambda: None)

    asked: list[str] = []

    def fake_prompt(text: str) -> str:
        asked.append(text)
        if _is_segment_question(text):
            return str(film_two)
        if "成片 mp4 路径" in text:   # 本用例只看尾帧复用，交付一问就停
            return "q"
        return ""

    monkeypatch.setattr(wizard, "_prompt", fake_prompt)

    class _TailLLM:
        def invoke(self, request, config=None):
            raise AssertionError("已有尾帧文档还去调模型")

    assert wizard._resume_director_handoff(config, session, llm=_TailLLM()) == 0
    assert path.read_text(encoding="utf-8") == old_text
    out = capsys.readouterr().out
    assert "沿用" in out
    # 沿用分支也要展示盘上旧词；否则用户续跑时同样只拿到一个路径。
    assert "旧词：以这张输入图为参考" in out
    assert "### 锁死清单" not in out
    segment_asks = [text for text in asked if _is_segment_question(text)]
    assert len(segment_asks) == 1 and "第 2/2 段" in segment_asks[0], \
        "只有第 2 段该问视频路径（第 1 段已通过、被跳过）"


def test_resumed_tailframe_check_reads_the_segment_prompt(
        tmp_path, monkeypatch, capsys):
    """续跑沿用盘上旧词时，也要拿本段视频词复核「背景冻结 vs 机位在动」（L4-05）。

    同一判据只在写词路径生效就是半接线：用户续跑看到的旧词可以带着一条互斥要求，
    而终端上什么问题都不报。
    """
    from minimax_h3_prompt.director import ledger, tailframe
    from minimax_h3_prompt.ui import wizard

    session = _fl2v_tailframe_session(tmp_path, monkeypatch)
    config = _run_config(monkeypatch, tmp_path)
    film_one = _write_solid_ffv1_mkv(tmp_path / "film-01.mkv", _frame_colors(107))
    ledger.log_director_output(
        session.directory, segment=1, start_frame=0, end_frame=107,
        declared_frames=107, export_frames=107, actual_frames=107,
        video_path=film_one)
    film_two = _write_solid_ffv1_mkv(tmp_path / "film-02.mkv", _frame_colors(153))

    # 本段视频词：机位在动（运镜词）。尾帧旧词的保留项却要求背景相对位置不变。
    (session.directory / "director" / "segment-02.md").write_text(
        "[Shot 2] the camera tracks backward with large amplitude at slow speed; "
        "final composition places him at the corner.",
        encoding="utf-8")
    old_text = (
        "### 变换指令\n以这张输入图为参考：镜头推到雨幕后的门框，起点地标现在在身后，人物脚下是石阶，门框在前方。\n\n"
        "### 保留项声明\n保持雨衣的灰色与招牌的橙光不变，保持建筑、店面与路牌之间的相对位置不变。\n\n"
        "### 锁死清单\n- 雨衣的灰色\n- 招牌的橙光\n"
    )
    path = session.directory / "director" / "tailframe-02.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(old_text, encoding="utf-8")

    monkeypatch.setattr(tailframe, "describe_image",
                        lambda *args, **kwargs: "_boom_")
    monkeypatch.setattr(wizard, "_drain_stdin", lambda: None)

    def fake_prompt(text: str) -> str:
        if _is_segment_question(text):
            return str(film_two)
        if "成片 mp4 路径" in text:
            return "q"
        return ""

    monkeypatch.setattr(wizard, "_prompt", fake_prompt)

    class _TailLLM:
        def invoke(self, request, config=None):
            raise AssertionError("已有尾帧文档还去调模型")

    assert wizard._resume_director_handoff(config, session, llm=_TailLLM()) == 0
    out = capsys.readouterr().out
    assert "TAILFRAME_BACKGROUND_FROZEN" in out, \
        "续跑沿用旧词时没有拿本段视频词复核（判据只接在写词路径＝半接线）"


def test_tailframe_failure_stops_at_the_current_segment_without_half_a_prompt(
        tmp_path, monkeypatch, capsys):
    """上一段视频缺失 → 报错停在当前段：不写半份词、本段不落账（#114）。

    旧实现是「取不到缓存就 ``return 1`` 整轮退出」，用户看到的是陪跑突然没了下文；
    #114 要求停法明确：告诉用户**修好后重跑向导会从这一段接着走**。段未落账这一点
    由账本断言——账上有行就等于放行了一段没生成过的段。
    """
    from minimax_h3_prompt.director import ledger, tailframe
    from minimax_h3_prompt.ui import wizard

    session = _fl2v_tailframe_session(tmp_path, monkeypatch)
    config = _run_config(monkeypatch, tmp_path)
    film_one = _write_solid_ffv1_mkv(tmp_path / "film-01.mkv", _frame_colors(107))
    ledger.log_director_output(
        session.directory, segment=1, start_frame=0, end_frame=107,
        declared_frames=107, export_frames=107, actual_frames=107,
        video_path=film_one)
    film_one.unlink()          # 上一段视频被删 / 不在原位

    def _boom(*args, **kwargs):
        raise AssertionError("取不到视频末帧时不该去读图或调模型")

    monkeypatch.setattr(tailframe, "describe_image", _boom)
    monkeypatch.setattr(wizard, "_drain_stdin", lambda: None)
    asked: list[str] = []

    def fake_prompt(text: str) -> str:
        asked.append(text)
        return ""          # 首问答「不用」；走到这里也不该再问第 2 段视频

    monkeypatch.setattr(wizard, "_prompt", fake_prompt)

    class _TailLLM:
        def invoke(self, request, config=None):
            raise AssertionError("取不到视频末帧时不该去调模型")

    assert wizard._resume_director_handoff(config, session, llm=_TailLLM()) == 1
    out = capsys.readouterr().out
    assert "取不到" in out and "重跑向导会从这一段接着走" in out
    assert not (session.directory / "director" / "tailframe-02.md").exists(), \
        "取不到起点画面就不许落半份提示词"
    assert sorted(ledger.passed_director_rows(session.directory)) == [1], \
        "第 2 段没有通过，不许落账"
    assert not any(_is_segment_question(text) for text in asked), \
        "停在出词之前：第 2 段的视频路径一问都不该出现"


# ---------------------------------------------------------------------------
# 单段计划（2026-10-30 真实跑现场）：证据是成片视频，不是段缓存
# ---------------------------------------------------------------------------

def test_single_shot_judges_the_film_against_the_declared_frames():
    """单段计划：实际帧数**等于声明帧**才过；差一帧就报红（无引导段没有容差）。

    判据与多段同源（``judge_segment`` 的帧数分支），只是证据来源换成成片视频。
    """
    from minimax_h3_prompt.director.companion import judge_video_evidence

    assert judge_video_evidence(_unguided(), actual_frames=107).passed
    short = judge_video_evidence(_unguided(), actual_frames=106)
    assert short.passed is False
    assert short.code == "FRAME_MISMATCH"


def test_single_shot_refuses_to_judge_without_a_measured_frame_count():
    """读不出帧数不是「0 帧」：调用方必须先报错，不许拿 None 混进判定。"""
    from minimax_h3_prompt.director.companion import judge_video_evidence

    with pytest.raises(ValueError, match="实际帧数"):
        judge_video_evidence(_unguided(), actual_frames=None)


def test_cumulative_export_from_the_director_counts_the_prefix_not_the_segment():
    """导演包是「全部导出」：贴回前 N 段合并视频时，总帧数是 ``end_frame[N]``。

    2026-10-31 现场：第 2 段导出 187 帧，贴回 226 帧（前两段合并），旧判据报
    FRAME_OVERFLOW。这里用同一张五段表：第 2 段导出 153、累计 260。
    """
    from minimax_h3_prompt.director.companion import judge_video_evidence

    plan = _plan_types(("opening", "continue", "camera_same", "camera_same", "ending"))
    segment = plan.segments[1]

    prefix = judge_video_evidence(segment, actual_frames=260, plan=plan)
    assert prefix.passed and prefix.code == "FRAME_OK"
    assert "前 2 段合并导出" in prefix.message and "153" in prefix.message

    full = judge_video_evidence(segment, actual_frames=719, plan=plan)
    assert full.passed and "整片合并导出" in full.message

    middle = judge_video_evidence(segment, actual_frames=413, plan=plan)
    assert middle.passed and "前 3 段合并导出" in middle.message


def test_a_single_segment_export_still_passes():
    """分段导出模式/用户自己裁过的单段视频：仍按原帧数判据。"""
    from minimax_h3_prompt.director.companion import judge_video_evidence

    plan = _plan_types(("opening", "continue", "camera_same"))
    segment = plan.segments[1]

    verdict = judge_video_evidence(segment, actual_frames=153, plan=plan)
    assert verdict.passed and verdict.code == "FRAME_OK"


def test_a_video_that_does_not_match_any_export_form_fails():
    """既不是单段导出也不是任何一段的合并导出：报红并点名列出的合法总帧数。"""
    from minimax_h3_prompt.director.companion import judge_video_evidence

    plan = _plan_types(("opening", "continue", "camera_same", "camera_same", "ending"))
    segment = plan.segments[1]

    # 前 1 段（107 帧）没有包含第 2 段，比本段导出帧还短 → 按少帧报
    assert judge_video_evidence(
        segment, actual_frames=107, plan=plan).code == "FRAME_SHORT"
    unknown = judge_video_evidence(segment, actual_frames=500, plan=plan)
    assert unknown.code == "FRAME_OVERFLOW"
    assert "260" in unknown.message and "153" in unknown.message


def test_prefix_shortfall_is_allowed_only_within_the_seam_tolerance():
    """每个接缝的相位对齐最多裁掉前一段 5 帧：前 3 段的合法下界是 413-10=403。"""
    from minimax_h3_prompt.director.companion import judge_video_evidence

    plan = _plan_types(("opening", "continue", "camera_same", "camera_same", "ending"))
    segment = plan.segments[1]

    assert judge_video_evidence(segment, actual_frames=403, plan=plan).passed
    assert judge_video_evidence(
        segment, actual_frames=402, plan=plan).code == "FRAME_OVERFLOW"


def test_single_segment_plan_takes_the_film_path_instead_of_a_cache(
        tmp_path, monkeypatch, capsys):
    """单段计划（i2v）：陪跑改成收**成片视频路径**并数它的帧，账上记这条路径。

    2026-10-30 真实跑现场：单段 i2v 跑出 243 帧视频，``output/minimax_seg_cache``
    整个目录都不存在——插件只在「分段重跑 / refine / 段间引导」时才写盘
    （``executor_core.py:106-125``，且 ``segment_continuity.py:192-207`` 对
    ``segment_count < 2`` 直接关掉引导）。旧陪跑只数缓存，于是对着一条已经跑完的片子
    报「这一段还没跑」（SEGMENT_NOT_RUN）。
    """
    from minimax_h3_prompt.director import ledger
    from minimax_h3_prompt.director.planning import plan_path, write_plan_json
    from minimax_h3_prompt.director.prompts import segment_path
    from minimax_h3_prompt.session_store import (
        STATUS_DIRECTOR_SEGMENTS_RUNNING, SessionState,
    )
    from minimax_h3_prompt.ui import wizard

    directory = tmp_path / "GEN001"
    plan = _plan_types(("opening",), mode="i2v")
    write_plan_json(plan_path(directory), plan)
    for segment in plan.segments:
        path = segment_path(directory, segment.index)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("段 1 的提示词\n", encoding="utf-8")
    session = SessionState(
        directory=directory,
        brief=_director_brief(director_mode="i2v", director_node_id="365",
                              director_cache_name="", duration=4.0),
        stage_state={}, status=STATUS_DIRECTOR_SEGMENTS_RUNNING)
    config = _run_config(monkeypatch, tmp_path)
    declared = plan.segments[0].declared_frames
    film = _write_ffv1_mkv(tmp_path / "film.mkv", declared)
    # #158 起贴回成片要核画幅：开场锚图做成与测试视频同比例，本用例验的是「单段没有
    # 段缓存也能按视频数帧」，不是画幅。
    _write_canvas_png(directory / "director" / "assets" / "01" / "start.png")
    asked: list[str] = []

    def fake_prompt(text: str) -> str:
        asked.append(text)
        if _is_segment_question(text):
            return str(film)
        # 首尾两问（#106）：全新开跑不问首问；尾问与交付都停在「不用 / q」。
        if "成片 mp4 路径" in text:
            return "q"
        return ""

    monkeypatch.setattr(wizard, "_drain_stdin", lambda: None)
    monkeypatch.setattr(wizard, "_prompt", fake_prompt)

    assert wizard._resume_director_handoff(config, session) == 0

    row = ledger.passed_director_rows(directory)[1]
    assert row["actual_frames"] == declared
    assert row["video"] == str(film.resolve()), "账上要记交回来的那条视频"
    assert row["video_sha256"] and row["source"] == "video"
    assert row["cache"] is None, "单段没有段缓存：键在、值为 null"
    out = capsys.readouterr().out
    assert "成片视频路径" in "".join(asked)
    assert "段缓存目录" not in out, "单段说明里不许再报一个永远不存在的缓存目录"


# ---------------------------------------------------------------------------
# #112：视频证据的错误面、旧账无证据、容差告警
# ---------------------------------------------------------------------------

def test_handoff_rejects_bad_video_paths_and_reasks_without_writing(
        tmp_path, monkeypatch, capsys):
    """路径不存在 / 空 / 不是视频：每次当场报错重问，账上一行都不许多（票面 AC2）。"""
    from minimax_h3_prompt.director import ledger
    from minimax_h3_prompt.ui import wizard

    session = _director_session(tmp_path, ("opening",))
    config = _run_config(monkeypatch, tmp_path)
    missing = tmp_path / "not-there.mkv"
    not_video = tmp_path / "notes.txt"
    not_video.write_text("这不是视频", encoding="utf-8")
    film = _write_ffv1_mkv(tmp_path / "film.mkv", 107)
    answers = ["", str(missing), str(not_video), str(film)]
    segment_asks = {"n": 0}

    def fake_prompt(text: str) -> str:
        if _is_segment_question(text):
            segment_asks["n"] += 1
            return answers.pop(0) if answers else "q"
        if "成片 mp4 路径" in text:
            return "q"
        return ""

    monkeypatch.setattr(wizard, "_drain_stdin", lambda: None)
    monkeypatch.setattr(wizard, "_prompt", fake_prompt)

    assert wizard._resume_director_handoff(config, session) == 0
    out = capsys.readouterr().out
    assert "成片视频路径不能为空" in out
    assert "成片视频不存在" in out
    assert "不是能数帧的成片视频" in out
    assert segment_asks["n"] == 4, "三种坏输入各重问一次，第四问才拿到真视频"
    assert sorted(ledger.passed_director_rows(session.directory)) == [1]


def test_resume_asks_for_a_video_when_a_legacy_row_has_no_video(
        tmp_path, monkeypatch, capsys):
    """旧账行缺 ``video``：不读崩、不算通过、点名要重新贴视频（票面 AC4）。"""
    import hashlib

    from minimax_h3_prompt.director import ledger
    from minimax_h3_prompt.ref_segments import _append_row, segment_ledger_path
    from minimax_h3_prompt.ui import wizard

    session = _director_session(tmp_path)   # 两段
    config = _run_config(monkeypatch, tmp_path)
    # #112 之前形态的通过行：字段齐全、就是没有 video。
    legacy_cache = tmp_path / "legacy" / "seg_0000.frames.mkv"
    legacy_cache.parent.mkdir(parents=True, exist_ok=True)
    legacy_cache.write_bytes(b"legacy frames")
    _append_row(segment_ledger_path(session.directory), {
        "event": "output", "segment": 1, "start_frame": 0, "end_frame": 107,
        "declared_frames": 107, "export_frames": 107, "actual_frames": 107,
        "cache": str(legacy_cache.resolve()),
        "sha256": hashlib.sha256(legacy_cache.read_bytes()).hexdigest(),
        "at": "2026-10-30T00:00:00+00:00",
    })
    film_one = _write_ffv1_mkv(tmp_path / "film-01.mkv", 107)
    film_two = _write_ffv1_mkv(tmp_path / "film-02.mkv", 153)
    asked: list[str] = []

    def fake_prompt(text: str) -> str:
        asked.append(text)
        if _is_segment_question(text):
            return str(film_one) if "第 1/2 段" in text else str(film_two)
        if "成片 mp4 路径" in text:
            return "q"
        return ""

    monkeypatch.setattr(wizard, "_drain_stdin", lambda: None)
    monkeypatch.setattr(wizard, "_prompt", fake_prompt)

    assert wizard._resume_director_handoff(config, session) == 0
    out = capsys.readouterr().out
    assert "第 1 段的旧账没有视频记录" in out
    # 旧账行没有 video → 不算有效通过行，但它仍是「账里已有通过行」：首问要在开头
    # 出现，用户才能指定从第 N 段重来（2026-10-31 复审采纳；old 版只在有效通过行
    # 非空时问，纯旧账的会话一进来就被迫从第 1 段顺序走）。
    assert sum(1 for text in asked if "有要重跑的段吗" in text) == 1
    # 首问答「不用」（本用例的 fake_prompt 对未知问题回空串），两段都要重新贴视频。
    segment_asks = [text for text in asked if _is_segment_question(text)]
    assert len(segment_asks) == 2
    assert "第 1/2 段" in segment_asks[0] and "第 2/2 段" in segment_asks[1]
    row = ledger.passed_director_rows(session.directory)[1]
    assert Path(row["video"]) == film_one.resolve()


def test_handoff_warns_within_tolerance_and_still_records(tmp_path, monkeypatch, capsys):
    """容差内少几帧＝告警照常入账（与旧判定同一口径），入账证据仍是那条视频。"""
    from minimax_h3_prompt.director import ledger
    from minimax_h3_prompt.ui import wizard

    session = _fl2v_tailframe_session(tmp_path, monkeypatch)
    config = _run_config(monkeypatch, tmp_path)
    # 本用例只看视频告警口径：第 2 段需要尾帧词，那会把模型与上一段末帧拖进来。
    from minimax_h3_prompt.director import tailframe

    monkeypatch.setattr(tailframe, "needs_tailframe", lambda *args, **kwargs: False)
    film_one = _write_ffv1_mkv(tmp_path / "film-01.mkv", 107)
    # 有引导段导出 153 帧；少 3 帧在默认容差 5 内。
    film_two = _write_ffv1_mkv(tmp_path / "film-02.mkv", 150)

    def fake_prompt(text: str) -> str:
        if _is_segment_question(text):
            return str(film_one) if "第 1/2 段" in text else str(film_two)
        if "成片 mp4 路径" in text:
            return "q"
        return ""

    monkeypatch.setattr(wizard, "_drain_stdin", lambda: None)
    monkeypatch.setattr(wizard, "_prompt", fake_prompt)

    assert wizard._resume_director_handoff(config, session) == 0
    out = capsys.readouterr().out
    assert "少 3 帧" in out and "容差 5 内" in out and "放行" in out
    rows = ledger.passed_director_rows(session.directory)
    assert sorted(rows) == [1, 2]
    assert rows[2]["actual_frames"] == 150


# ---------------------------------------------------------------------------
# #113：每段「不满意就改词」（e）——只动画面正文、只重打导演包
# ---------------------------------------------------------------------------

# 改词后模型应产出的形态：**相对本段起点**的时间戳、注记留给组装器注入。
# 两个用例的段 1（起点 0）与段 2（起点 107）都落在本段窗口内，所以同一份回复通用。
# #130 起 t2v/i2v/fl2v 的段词是官方三字段（缺节即判不合格），改词回复也必须是这个形态。
_REWRITTEN_BODY = (
    "integrated_multimodal_description:\n"
    "The camera holds on the rain-soaked street outside the shop.\n"
    "[Shot 1] A woman in a grey trench coat turns toward the awning.\n"
    "[Shot 2] At 00:01.000, she opens the envelope and looks up.\n"
    "\noverall_soundscape:\n"
    "Rain taps on the awning while a car passes in the distance.\n"
    "\nnon_diegetic_music:\n"
    "N/A\n"
)


class _RewritingLLM:
    """改词用的假模型：记录每条请求，按给定回复作答（不联网、不发真实请求）。

    ``_direct_write`` 的调用形是 ``llm.invoke(request, config={...})``；``config``
    要收下（看门狗会往里面塞 callbacks），但假模型不看它。
    """

    def __init__(self, reply):
        self._reply = reply
        self.requests: list[str] = []

    def invoke(self, request, config=None):
        self.requests.append(request)
        if isinstance(self._reply, Exception):
            raise self._reply
        return self._reply


def test_rewrite_entry_updates_the_segment_and_repacks_only_that_prompt(
        tmp_path, monkeypatch, capsys):
    """#113 AC：等路径时输入 ``e`` → 改词落盘 → **只重打**导演包 → 回到等待。

    走真实路径（真校验器、真打包器、真账本），只把模型换成假模型。四条断言各守一个
    容易被「实现看起来对」骗过去的点：

    - 段文件和导演包 timeline 的该段 prompt 一起变（先落盘再重打；反过来包里的词是旧的）；
    - **其它段一个字不动**（不是「重新规划/重写整片」）；
    - 段产出账多一行 rewrite、修前/修后原文可对照（feedback 点名陪跑改词）；
    - 出站告诉用户「重新导入导演包 + runSelection 只勾本段」，且改完还能贴视频通过。
    """
    import json

    from minimax_h3_prompt.director import ledger
    from minimax_h3_prompt.director.prompts import segment_path
    from minimax_h3_prompt.ref_segments import (
        SEGMENT_EVENT_REWRITE,
        read_segment_ledger,
    )
    from minimax_h3_prompt.ui import wizard

    session = _director_session(tmp_path)
    # 写段上下文只在 stage_state 里（持久化白名单）：陪跑原本没读它，这是本票的接线点。
    session.stage_state["shot_table"] = "分镜表：[Shot 1] 雨夜便利店门口……"
    config = _run_config(monkeypatch, tmp_path)
    film_one = _write_ffv1_mkv(tmp_path / "film-01.mkv", 107)
    film_two = _write_ffv1_mkv(tmp_path / "film-02.mkv", 153)
    first_asks = {"n": 0}

    def fake_prompt(text: str) -> str:
        if _is_segment_question(text):
            if "第 1/2 段" in text:
                first_asks["n"] += 1
                # 第一次答 e（大写也认，大小写不敏感）；改完回到同一问再贴视频。
                return "E" if first_asks["n"] == 1 else str(film_one)
            return str(film_two)
        if "成片 mp4 路径" in text:   # 尾问之后的交付：本用例停在陪跑账
            return "q"
        return ""

    monkeypatch.setattr(wizard, "_drain_stdin", lambda: None)
    monkeypatch.setattr(wizard, "_prompt", fake_prompt)
    llm = _RewritingLLM(_REWRITTEN_BODY)

    assert wizard._resume_director_handoff(config, session, llm=llm) == 0

    out = capsys.readouterr().out
    after = segment_path(session.directory, 1).read_text(encoding="utf-8")
    assert "grey trench coat" in after, "e 必须真的改掉段提示词"
    assert segment_path(session.directory, 2).read_text(encoding="utf-8") \
        == "段 2 的提示词\n", "只改轮到的那一段，其它段一个字都不许动"

    # 请求 = 同一份写段上下文（state 里的分镜表）+ 当前段提示词 + 改词要求。
    assert "雨夜便利店门口" in llm.requests[0]
    assert "段 1 的提示词" in llm.requests[0]
    assert "只改画面正文" in llm.requests[0]

    # 导演包重打过：timeline 里只有这一段换了词（打包读到的是**新**段文件）。
    timeline = json.loads(
        (session.directory / "director" / "pack" / "timeline.json").read_text(
            encoding="utf-8"))
    assert timeline["segments"][0]["prompt"] == after.strip()
    assert timeline["segments"][1]["prompt"] == "段 2 的提示词"

    rewrites = read_segment_ledger(session.directory, event=SEGMENT_EVENT_REWRITE)
    assert len(rewrites) == 1
    assert rewrites[0]["segment"] == 1
    assert "陪跑改词" in rewrites[0]["feedback"]
    assert rewrites[0]["prompt_before"] == "段 1 的提示词"
    assert rewrites[0]["prompt_after"] == after.strip()

    assert "重新导入导演包" in out
    assert "runSelection" in out and "只勾第 1 段" in out
    # 改完没有打断流程：两段随后都按视频通过、落账。
    assert sorted(ledger.passed_director_rows(session.directory)) == [1, 2]


def test_rewrite_that_fails_validation_keeps_the_original_prompt(
        tmp_path, monkeypatch, capsys):
    """内容不合格（重写预算用尽）→ 不覆盖原段提示词、不重打包、不落 rewrite 账。

    票面：不合格按既有重写预算处置；仍不合格交回原文并说明，**不静默写坏词**。
    所以断言两件事同时成立：模型被问了 ``MAX_SEGMENT_ATTEMPTS`` 次且第二轮带着
    问题清单；磁盘上的段文件与导演包却还是原样。
    """
    from minimax_h3_prompt.director import ledger
    from minimax_h3_prompt.director.prompts import segment_path
    from minimax_h3_prompt.ref_segments import (
        SEGMENT_EVENT_REWRITE,
        read_segment_ledger,
    )
    from minimax_h3_prompt.ui import wizard

    original = "段 1 的提示词\n"
    session = _director_session(tmp_path)
    config = _run_config(monkeypatch, tmp_path)
    film_one = _write_ffv1_mkv(tmp_path / "film-01.mkv", 107)
    film_two = _write_ffv1_mkv(tmp_path / "film-02.mkv", 153)
    # 单段模式缺三字段里的两节 → DIRECTOR_SECTION_MISSING；两轮回复都不改。
    bad = "overall_soundscape:\nrain\n[Shot 1] A woman turns toward the awning.\n"
    llm = _RewritingLLM(bad)
    asks = {"n": 0}

    def fake_prompt(text: str) -> str:
        if _is_segment_question(text):
            if "第 1/2 段" in text:
                asks["n"] += 1
                return "e" if asks["n"] == 1 else str(film_one)
            return str(film_two)
        if "成片 mp4 路径" in text:
            return "q"
        return ""

    monkeypatch.setattr(wizard, "_drain_stdin", lambda: None)
    monkeypatch.setattr(wizard, "_prompt", fake_prompt)

    assert wizard._resume_director_handoff(config, session, llm=llm) == 0
    out = capsys.readouterr().out

    assert segment_path(session.directory, 1).read_text(encoding="utf-8") == original
    assert len(llm.requests) == 2, "重写预算 MAX_SEGMENT_ATTEMPTS 必须用尽才交回原文"
    assert "DIRECTOR_SECTION_MISSING" in llm.requests[1], "第二轮请求要带上一版的问题清单"
    assert "DIRECTOR_SECTION_MISSING" in out and "保持原样" in out
    assert read_segment_ledger(session.directory, event=SEGMENT_EVENT_REWRITE) == []
    assert not (session.directory / "director" / "pack").exists(), \
        "改词没成功就不许重打导演包"
    # 失败不打断会话：原词照样能贴视频通过。
    assert sorted(ledger.passed_director_rows(session.directory)) == [1, 2]


def test_rewrite_call_failure_keeps_the_original_prompt(
        tmp_path, monkeypatch, capsys):
    """模型调用失败 → 不覆盖原段提示词、不落账，回到等待（不把会话打断）。"""
    from minimax_h3_prompt.director import ledger
    from minimax_h3_prompt.director.prompts import segment_path
    from minimax_h3_prompt.ref_segments import (
        SEGMENT_EVENT_REWRITE,
        read_segment_ledger,
    )
    from minimax_h3_prompt.ui import wizard

    original = "段 1 的提示词\n"
    session = _director_session(tmp_path)
    config = _run_config(monkeypatch, tmp_path)
    film_one = _write_ffv1_mkv(tmp_path / "film-01.mkv", 107)
    film_two = _write_ffv1_mkv(tmp_path / "film-02.mkv", 153)
    llm = _RewritingLLM(RuntimeError("网关 502"))
    asks = {"n": 0}

    def fake_prompt(text: str) -> str:
        if _is_segment_question(text):
            if "第 1/2 段" in text:
                asks["n"] += 1
                return "e" if asks["n"] == 1 else str(film_one)
            return str(film_two)
        if "成片 mp4 路径" in text:
            return "q"
        return ""

    monkeypatch.setattr(wizard, "_drain_stdin", lambda: None)
    monkeypatch.setattr(wizard, "_prompt", fake_prompt)

    assert wizard._resume_director_handoff(config, session, llm=llm) == 0
    out = capsys.readouterr().out

    assert segment_path(session.directory, 1).read_text(encoding="utf-8") == original
    assert "改词调用失败" in out and "保持原样" in out
    assert read_segment_ledger(session.directory, event=SEGMENT_EVENT_REWRITE) == []
    assert not (session.directory / "director" / "pack").exists()
    assert sorted(ledger.passed_director_rows(session.directory)) == [1, 2]


def test_rewrite_repack_failure_stops_the_handoff_loudly(
        tmp_path, monkeypatch, capsys):
    """改完词但导演包重打失败 → 大声报错并 ``return 1``：不许带着坏包回等待。

    用户下一步就是「重新导入导演包」；如果这里静默吞掉，他会导入旧包、按提示只跑本段，
    拿到的仍是改前那一版（这类错不报错，只让人以为改词没生效）。

    次序也是判据：打包失败时**段文件回滚成改前原文、改写账一行不记**——留下「新词 +
    旧包」或「账说改过、包说没改」都是同一件事的两处口径，续跑不会自动重打包。
    """
    from minimax_h3_prompt.director import pack as pack_module
    from minimax_h3_prompt.director.prompts import segment_path
    from minimax_h3_prompt.ref_segments import (
        SEGMENT_EVENT_REWRITE,
        read_segment_ledger,
    )
    from minimax_h3_prompt.ui import wizard

    original = "段 1 的提示词\n"
    session = _director_session(tmp_path)
    config = _run_config(monkeypatch, tmp_path)
    asks = {"n": 0}

    def fake_prompt(text: str) -> str:
        if _is_segment_question(text):
            asks["n"] += 1
            return "e"          # 一直按 e：打包坏掉后流程必须停下，不该再问
        return ""

    def boom(*args, **kwargs):
        raise ValueError("第 2 段缺提示词文件")

    monkeypatch.setattr(pack_module, "prepare_director_pack", boom)
    monkeypatch.setattr(wizard, "_drain_stdin", lambda: None)
    monkeypatch.setattr(wizard, "_prompt", fake_prompt)

    assert wizard._resume_director_handoff(
        config, session, llm=_RewritingLLM(_REWRITTEN_BODY)) == 1
    out = capsys.readouterr().out
    assert "导演包重打失败" in out and "第 2 段缺提示词文件" in out
    assert "已恢复为改前版本" in out and "导演包保持旧词" in out
    assert asks["n"] == 1, "打包失败就停：不许回等待继续问"
    # 回滚判据：段文件回到改前原文，改写账一行不记（新词、旧包、假账三者不许并存）。
    assert segment_path(session.directory, 1).read_text(encoding="utf-8") == original
    assert read_segment_ledger(session.directory, event=SEGMENT_EVENT_REWRITE) == []


def test_rewrite_round_threads_the_stage_context_from_the_session(
        tmp_path, monkeypatch, capsys):
    """#113 最易漏的接线点：陪跑原本不读 ``shot_table``，改词请求就没有分镜窗口。

    分镜表只住在 ``session.stage_state``（持久化白名单）。本用例把 prompts 层的改词
    入口换成记录器，钉住「向导读到了 state 并原样传下去」。

    r2v 的定义块**不从 state 取**（2026-11-01 真实现场）：阶段 1 的 ``subject_defs``
    是中文 markdown 素材，段文件里的 1、3 节才是英文、按本段裁过子集的那一份。t2v
    没有定义块，这里一并钉住 ``sections is None``。
    """
    from minimax_h3_prompt.director import prompts as director_prompts
    from minimax_h3_prompt.ui import wizard

    session = _director_session(tmp_path)
    session.stage_state["shot_table"] = "分镜表：雨夜便利店门口"
    session.stage_state["subject_defs"] = "subject_definitions: 雨衣女人\n"
    config = _run_config(monkeypatch, tmp_path)
    film_one = _write_ffv1_mkv(tmp_path / "film-01.mkv", 107)
    film_two = _write_ffv1_mkv(tmp_path / "film-02.mkv", 153)
    seen: dict[str, str] = {}
    asks = {"n": 0}

    def spy(plan, segment, *, llm, current_text, shot_table="", sections=None,
            **kwargs):
        seen["shot_table"] = shot_table
        seen["sections"] = sections
        # 原词照回：本用例只验接线，不验改词内容。
        return director_prompts.RewriteOutcome(text=current_text, issues=())

    def fake_prompt(text: str) -> str:
        if _is_segment_question(text):
            if "第 1/2 段" in text:
                asks["n"] += 1
                return "e" if asks["n"] == 1 else str(film_one)
            return str(film_two)
        if "成片 mp4 路径" in text:
            return "q"
        return ""

    monkeypatch.setattr(director_prompts, "rewrite_director_segment", spy)
    monkeypatch.setattr(wizard, "_drain_stdin", lambda: None)
    monkeypatch.setattr(wizard, "_prompt", fake_prompt)

    assert wizard._resume_director_handoff(config, session, llm=object()) == 0

    assert seen == {"shot_table": "分镜表：雨夜便利店门口", "sections": None}, \
        "t2v 段没有定义块：改词请求不该凭空带两节，也不该退回阶段 1 的中文素材"


_R2V_SEGMENT_TEXT = """subject_definitions:
<Picture 1> is the girl.
<Picture 2> is the door viewer.

summary:
[reference generation] the girl looks at the door.

retention_analysis:
<Picture 1>: fully_preserved.
<Picture 2>: fully_preserved.

detailed_description:
Segment {index} of 2, covering 0.000s-4.458s of the full 10.833s video.
[Shot 1] <Picture 1> sits on the sofa; <Picture 2> is by the door.

overall_soundscape:
A television murmurs.

non_diegetic_music:
N/A
"""


def test_r2v_rewrite_round_reads_the_definition_block_from_the_segment_file(
        tmp_path, monkeypatch):
    """r2v 改词的定义块取**盘上这段自己的** 1、3 节，不取阶段 1 的中文素材。

    2026-11-01 真实现场：阶段 1 的 ``subject_defs`` 是中文 markdown（解析为空字典），
    而段文件里那两节是英文、已按本段裁成子集。改词若退回 state，改出来的段会和同目录
    其它段用两套编号、两套语言。
    """
    from minimax_h3_prompt.director import prompts as director_prompts
    from minimax_h3_prompt.director.planning import plan_path, write_plan_json
    from minimax_h3_prompt.director.prompts import segment_path
    from minimax_h3_prompt.session_store import STATUS_DIRECTOR_SEGMENTS_RUNNING, SessionState
    from minimax_h3_prompt.tools.h3_validator import split_ref_sections
    from minimax_h3_prompt.ui import wizard

    directory = tmp_path / "GEN001"
    plan = _plan_types(("opening", "continue"), mode="r2v")
    write_plan_json(plan_path(directory), plan)
    for segment in plan.segments:
        path = segment_path(directory, segment.index)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_R2V_SEGMENT_TEXT.format(index=segment.index), encoding="utf-8")
    session = SessionState(directory=directory, brief=_director_brief(director_mode="r2v"),
                           stage_state={"shot_table": "分镜表：雨夜"},
                           status=STATUS_DIRECTOR_SEGMENTS_RUNNING)
    config = _run_config(monkeypatch, tmp_path)
    film_one = _write_ffv1_mkv(tmp_path / "film-01.mkv", 107)
    seen: dict = {}
    asks = {"n": 0}

    def spy(plan, segment, *, llm, current_text, shot_table="", sections=None,
            **kwargs):
        seen["sections"] = sections
        return director_prompts.RewriteOutcome(text=current_text, issues=())

    def fake_prompt(text: str) -> str:
        if _is_segment_question(text):
            if "第 1/2 段" in text:
                asks["n"] += 1
                return "e" if asks["n"] == 1 else str(film_one)
            return "q"
        return "q"

    monkeypatch.setattr(director_prompts, "rewrite_director_segment", spy)
    monkeypatch.setattr(wizard, "_drain_stdin", lambda: None)
    monkeypatch.setattr(wizard, "_prompt", fake_prompt)
    # 改词成功后要重打导演包：本用例接缝是「定义块从哪来」，打包打桩。
    from types import SimpleNamespace

    from minimax_h3_prompt.director import pack as pack_module

    monkeypatch.setattr(
        pack_module, "prepare_director_pack",
        lambda *args, **kwargs: (SimpleNamespace(zip_path="x.zip"), "ok"))

    wizard._resume_director_handoff(config, session, llm=object())

    expected = split_ref_sections(
        segment_path(directory, 1).read_text(encoding="utf-8"))
    assert seen["sections"] == expected, \
        "改词必须拿到盘上这段自己的定义块（英文、已裁子集），不是阶段 1 的中文素材"


def test_rewrite_round_on_a_single_segment_tells_the_user_to_click_run(
        tmp_path, monkeypatch, capsys):
    """单段计划改词后不许说 runSelection（2026-10-31 复审采纳）。

    单段 i2v 的时间线只有一段，没有「只勾第 N 段」可勾；照抄多段那句会让用户去找一个
    不存在的勾选入口。分支判据是 ``len(plan.segments) == 1``——与简报同一份口径。
    """
    from minimax_h3_prompt.director.planning import plan_path, write_plan_json
    from minimax_h3_prompt.director.prompts import segment_path
    from minimax_h3_prompt.session_store import (
        STATUS_DIRECTOR_SEGMENTS_RUNNING, SessionState,
    )
    from minimax_h3_prompt.ui import wizard

    directory = tmp_path / "GEN001"
    plan = _plan_types(("opening",), mode="i2v")
    write_plan_json(plan_path(directory), plan)
    path = segment_path(directory, 1)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("段 1 的提示词\n", encoding="utf-8")
    # i2v 开场必须自带首图，否则重打导演包会先被 pack 拒绝（与改词分支无关）。
    image = directory / "director" / "assets" / "01" / "start.png"
    image.parent.mkdir(parents=True, exist_ok=True)
    image.write_bytes(b"\x89PNG\r\n\x1a\nfake")
    session = SessionState(
        directory=directory,
        brief=_director_brief(director_mode="i2v", duration=4.0,
                              director_node_id="365", director_cache_name=""),
        stage_state={}, status=STATUS_DIRECTOR_SEGMENTS_RUNNING)
    config = _run_config(monkeypatch, tmp_path)

    monkeypatch.setattr(wizard, "_drain_stdin", lambda: None)

    outcome = wizard._rewrite_director_segment_round(
        plan, plan.segments[0], session, config=config,
        llm=_RewritingLLM(_REWRITTEN_BODY))
    out = capsys.readouterr().out
    assert outcome == "retry"
    assert "直接点运行" in out
    assert "runSelection" not in out, "单段没有可勾的段，不许让用户去找 runSelection"


def test_handoff_rejects_a_video_whose_aspect_misses_its_anchor(
        tmp_path, monkeypatch, capsys):
    """#158 接线：贴回的成片与本段锚图画幅不符 → 不落账、打印期望/实际。

    打桩 ``_prompt_segment_video``：第一次交一段 48×86 的成片（与 1920×1088 的锚图
    差约 216%），第二次 quit——拒绝之后必须**回到同一个等视频问答**，而不是越过它
    去问终点图或写账。
    """
    import numpy as np

    from minimax_h3_prompt.director import ledger
    from minimax_h3_prompt.ui import wizard

    plan = _plan_types(("opening",), mode="fl2v")
    session = _director_session(tmp_path, ("opening",), mode="fl2v")
    config = _run_config(monkeypatch, tmp_path)
    # 本段锚图 1920×1088（与 2026-11-04 验收现场同尺寸）；成片是竖幅小视频。
    start = session.directory / "director" / "assets" / "01" / "start.png"
    start.parent.mkdir(parents=True, exist_ok=True)
    assert cv2.imwrite(str(start), np.zeros((1088, 1920, 3), np.uint8))
    film = _write_ffv1_mkv(tmp_path / "film.mkv", 107, width=48, height=86)

    calls = {"video": 0}

    def fake_video(question: str):
        calls["video"] += 1
        if calls["video"] == 1:
            return "video", film, 107
        return "quit", None, None

    monkeypatch.setattr(wizard, "_prompt_segment_video", fake_video)
    # 画幅不符时流程只该回到「贴视频」那一问，任何别的问句都说明它越过核对继续了
    # ——用 fail 而不是空串，红的时候会立刻失败而不是在交付问答里死循环。
    monkeypatch.setattr(
        wizard, "_prompt", lambda text: pytest.fail(f"不该问：{text}"))
    monkeypatch.setattr(wizard, "_drain_stdin", lambda: None)

    assert wizard._run_director_segment_handoff(plan, session, config=config) == 0

    out = capsys.readouterr().out
    assert "拒绝落账" in out
    assert "1920×1088" in out, "期望值（本段锚图）没有打印"
    assert "48×86" in out, "实际值没有打印"
    assert "width/height" in out and "重跑本段" in out, "没告诉用户下一步怎么做"
    assert calls["video"] == 2, "拒绝之后应回到同一个等视频问答"
    assert ledger.passed_director_rows(session.directory) == {}, "画幅不符却落了账"


# ---------------------------------------------------------------------------
# #176：r2v 的成片宽高核对（向导答的成片画幅 vs 实际成片）
# ---------------------------------------------------------------------------

def _r2v_session_with_film(tmp_path: Path, *, size=(1920, 1088)):
    """造一条「r2v 单段、已答成片画幅」的陪跑会话。"""
    from minimax_h3_prompt.director.film_canvas import film_frame_canvases

    session = _director_session(tmp_path, ("opening",), mode="r2v")
    session.brief.canvases = film_frame_canvases(size, "qwen")
    return session


def _pin_r2v_frame_model(monkeypatch):
    from minimax_h3_prompt import image_config

    monkeypatch.setattr(image_config, "load_frame_image_model", lambda: "qwen")
    monkeypatch.setattr(wizard_module(), "_drain_stdin", lambda: None)


def wizard_module():
    from minimax_h3_prompt.ui import wizard

    return wizard


def test_r2v_handoff_asks_once_for_a_size_only_gap_then_records(
        tmp_path, monkeypatch, capsys):
    """864×480 对 1920×1088：比例对、尺寸差 2.2 倍 → 确认一次，放行并落账。

    确认记录要进会话状态（向导侧键），续跑时同一组数值不再问。
    """
    from minimax_h3_prompt.director import ledger
    from minimax_h3_prompt.director.aspect_flow import FILM_SIZE_CONFIRM_KEY
    from minimax_h3_prompt.session_store import load_session

    wizard = wizard_module()
    _pin_r2v_frame_model(monkeypatch)
    session = _r2v_session_with_film(tmp_path)
    config = _run_config(monkeypatch, tmp_path)
    film = _write_ffv1_mkv(tmp_path / "film.mkv", 107, width=864, height=480)
    asked: list[str] = []

    monkeypatch.setattr(wizard, "_prompt_segment_video",
                        lambda question: ("video", film, 107))

    def fake_prompt(text: str) -> str:
        asked.append(text)
        if "确认继续使用这条成片" in text:
            return ""
        if _is_rerun_question(text):
            return ""          # 尾问「不用」
        if "成片 mp4 路径" in text:
            return "q"         # 交付一问就停
        return ""

    monkeypatch.setattr(wizard, "_prompt", fake_prompt)

    assert wizard._run_director_segment_handoff(plan_of(session), session,
                                                config=config) == 0

    confirmations = [text for text in asked if "确认继续使用这条成片" in text]
    assert len(confirmations) == 1, "尺寸差一档只确认一次"
    out = capsys.readouterr().out
    assert "1920×1088" in out and "864×480" in out, "两个数都要打印"
    assert "来源" in out and "成片画幅" in out, "要报来源"
    assert "20%" in out, "要报尺寸容差"
    assert sorted(ledger.passed_director_rows(session.directory)) == [1], "确认后应落账"
    assert session.stage_state[FILM_SIZE_CONFIRM_KEY]["actual"] == [864, 480]
    saved = load_session(session.directory)
    assert saved is not None and saved.stage_state[FILM_SIZE_CONFIRM_KEY]["actual"] == [864, 480]


def plan_of(session):
    """从盘上读回会话的段表（陪跑真正用的是它，不是测试手里另造的一份）。"""
    from minimax_h3_prompt.director.planning import plan_path, read_plan_json

    return read_plan_json(plan_path(session.directory))


def test_r2v_handoff_stops_when_the_size_confirmation_is_refused(
        tmp_path, monkeypatch, capsys):
    """尺寸差一档的确认被拒绝 → 停、不落账、不越过这一问。"""
    from minimax_h3_prompt.director import ledger
    from minimax_h3_prompt.director.aspect_flow import FILM_SIZE_CONFIRM_KEY

    wizard = wizard_module()
    _pin_r2v_frame_model(monkeypatch)
    session = _r2v_session_with_film(tmp_path)
    config = _run_config(monkeypatch, tmp_path)
    film = _write_ffv1_mkv(tmp_path / "film.mkv", 107, width=864, height=480)

    monkeypatch.setattr(wizard, "_prompt_segment_video",
                        lambda question: ("video", film, 107))

    def fake_prompt(text: str) -> str:
        if "确认继续使用这条成片" in text:
            return "n"          # 拒绝这一次确认
        pytest.fail(f"拒绝之后不该再问：{text}")

    monkeypatch.setattr(wizard, "_prompt", fake_prompt)

    assert wizard._run_director_segment_handoff(
        plan_of(session), session, config=config) == 0

    out = capsys.readouterr().out
    assert "已停下" in out and "未获确认" in out
    assert ledger.passed_director_rows(session.directory) == {}, "拒绝后不许落账"
    assert FILM_SIZE_CONFIRM_KEY not in session.stage_state


def test_r2v_handoff_stops_on_a_ratio_gap_without_asking(
        tmp_path, monkeypatch, capsys):
    """比例超差（480×864 对 1920×1088）→ 停止并说明，不给确认出口。"""
    from minimax_h3_prompt.director import ledger

    wizard = wizard_module()
    _pin_r2v_frame_model(monkeypatch)
    session = _r2v_session_with_film(tmp_path)
    config = _run_config(monkeypatch, tmp_path)
    film = _write_ffv1_mkv(tmp_path / "film.mkv", 107, width=480, height=864)

    monkeypatch.setattr(wizard, "_prompt_segment_video",
                        lambda question: ("video", film, 107))
    monkeypatch.setattr(wizard, "_prompt",
                        lambda text: pytest.fail(f"比例超差不该问：{text}"))

    assert wizard._run_director_segment_handoff(
        plan_of(session), session, config=config) == 0

    out = capsys.readouterr().out
    assert "停止" in out and "1920×1088" in out and "480×864" in out
    assert ledger.passed_director_rows(session.directory) == {}, "比例不符却落了账"

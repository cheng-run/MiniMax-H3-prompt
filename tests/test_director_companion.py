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


def test_actual_frames_reports_when_the_codec_is_raw(tmp_path):
    """``.pt`` 缓存必须**明确报错**并给处置，不许静默当 0 帧或跳过（硬约束第 17 条）。

    默认 ``cache_frames_codec="raw"`` 时磁盘上就是它；本项目没有 torch，读不了。
    用户的处置只有一条：把导演台的缓存编码切成 ffv1 后重跑该段。
    """
    directory = tmp_path / "cache"
    directory.mkdir()
    (directory / "seg_0000.pt").write_bytes(b"torch-pickle")
    with pytest.raises(ValueError) as excinfo:
        director_cache.actual_frames_for_segment(directory, 1)
    message = str(excinfo.value)
    assert "ffv1" in message
    assert "cache_frames_codec" in message
    assert "seg_0000" in message


def test_actual_frames_for_segment_reports_a_missing_file(tmp_path):
    """帧文件不在 → 报错点名段号与路径，「文件缺失」不是 0 帧。"""
    directory = tmp_path / "cache"
    directory.mkdir()
    with pytest.raises(ValueError) as excinfo:
        director_cache.actual_frames_for_segment(directory, 2)
    assert "seg_0001.frames.mkv" in str(excinfo.value)


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
    values.update(overrides)
    return values


def test_output_row_records_frames_cache_path_and_sha256(tmp_path):
    """通过行必须含**缓存绝对路径与实际帧数**（票面 AC4），SHA256 在写入时现算。"""
    import hashlib

    from minimax_h3_prompt.director import ledger
    from minimax_h3_prompt.ref_segments import read_segment_ledger, segment_ledger_path

    frame_file = _write_ffv1_mkv(tmp_path / "cache" / "seg_0000.frames.mkv", 3)
    ledger.log_director_output(tmp_path, **_output_row(cache_path=frame_file))

    rows = read_segment_ledger(tmp_path, event=None)
    assert len(rows) == 1
    row = rows[0]
    assert row["event"] == "output"
    assert row["segment"] == 1
    assert row["actual_frames"] == 107
    assert row["declared_frames"] == 107 and row["export_frames"] == 107
    assert Path(row["cache"]) == frame_file.resolve()
    assert row["sha256"] == hashlib.sha256(frame_file.read_bytes()).hexdigest()
    assert row["at"]
    # 落点是共用的那本账，不新开第三本（设计 §3.6）。
    assert segment_ledger_path(tmp_path).is_file()


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


def test_progress_refuses_rows_that_lack_the_cache_evidence(tmp_path):
    """通过行缺缓存路径/实际帧数 → 报错。行写不全就不是「这段过了」的证据。

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


def _director_session(tmp_path: Path, types: tuple[str, ...] = ("opening", "continue")):
    """落一张段表 + 段提示词文件，返回「陪跑进行中」的会话（模拟续跑读到的东西）。"""
    from minimax_h3_prompt.director.planning import plan_path, write_plan_json
    from minimax_h3_prompt.director.prompts import segment_path
    from minimax_h3_prompt.session_store import (
        STATUS_DIRECTOR_SEGMENTS_RUNNING, SessionState,
    )

    directory = tmp_path / "GEN001"
    plan = _plan_types(types)
    write_plan_json(plan_path(directory), plan)
    for segment in plan.segments:
        path = segment_path(directory, segment.index)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"段 {segment.index} 的提示词\n", encoding="utf-8")
    return SessionState(directory=directory, brief=_director_brief(),
                        stage_state={}, status=STATUS_DIRECTOR_SEGMENTS_RUNNING)


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
    """「第 N 段跑完后按回车」那一问（每段一次）。"""
    return "在导演台里跑完后按回车" in text


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
    """逐段陪跑主流程：读到缓存 → 判定通过 → 写通过行（含缓存路径与实际帧数）。"""
    from minimax_h3_prompt.director import ledger
    from minimax_h3_prompt.ui import wizard

    session = _director_session(tmp_path)
    config = _run_config(monkeypatch, tmp_path)
    cache_directory = _cache_dir_for(config, session.brief)
    _plant_cache(cache_directory, 1, 107)   # 无引导段：等于声明帧
    _plant_cache(cache_directory, 2, 153)   # 有引导段：等于导出帧

    monkeypatch.setattr(wizard, "_drain_stdin", lambda: None)
    asked: list[str] = []

    def fake_prompt(text: str) -> str:
        asked.append(text)
        # 全段通过后接的是**交付**（#108）：本用例只验陪跑落账，交付一进来就停下。
        if "成片 mp4 路径" in text:
            return "q"
        return ""

    monkeypatch.setattr(wizard, "_prompt", fake_prompt)

    assert wizard._resume_director_handoff(config, session) == 0

    out = capsys.readouterr().out
    # 出站必须写明只用 runSelection 勾当前段（设计 §3.7 最后一条）。
    assert "runSelection" in out and "第 1 段" in out and "第 2 段" in out
    # 全通过后下一步是交付（#108）：成片与最终包两条路径由用户逐项给。
    assert any("成片 mp4 路径" in question for question in asked)
    assert "停在交付前" in out
    rows = ledger.passed_director_rows(session.directory)
    assert sorted(rows) == [1, 2]
    assert rows[1]["actual_frames"] == 107
    assert Path(rows[1]["cache"]).name == "seg_0000.frames.mkv"
    assert Path(rows[1]["cache"]).is_absolute()
    assert rows[2]["actual_frames"] == 153


def test_handoff_refuses_a_mistaken_selection_then_accepts_the_rerun(
        tmp_path, monkeypatch, capsys):
    """**勾错段报红**（票面 AC3）：缓存里只有第 1 段，第 2 段没跑 → 报红不落账；
    补跑第 2 段后再回车才通过。"""
    from minimax_h3_prompt.director import ledger
    from minimax_h3_prompt.ui import wizard

    session = _director_session(tmp_path)
    config = _run_config(monkeypatch, tmp_path)
    cache_directory = _cache_dir_for(config, session.brief)
    _plant_cache(cache_directory, 1, 107)
    # 第 2 段**没跑**：用户以为自己勾了第 2 段，实际只跑了第 1 段。
    calls = {"n": 0}

    def prompt_and_plant(text: str) -> str:
        if _is_rerun_question(text):   # #106 的首尾两问：这一条用例不重跑任何段
            return ""
        # 全段通过后会进交付（#108）：这里只数**陪跑**的问数，交付一进来就停下。
        if "成片 mp4 路径" in text:
            return "q"
        calls["n"] += 1
        if calls["n"] == 3:   # 第三次回车之前，用户回导演台补跑了第 2 段
            _plant_cache(cache_directory, 2, 153)
        return ""

    monkeypatch.setattr(wizard, "_drain_stdin", lambda: None)
    monkeypatch.setattr(wizard, "_prompt", prompt_and_plant)

    assert wizard._resume_director_handoff(config, session) == 0
    out = capsys.readouterr().out
    assert "SEGMENT_MISMATCH" in out or "勾错了段" in out
    assert "勾错了段" in out and "第 2 段" in out
    assert calls["n"] == 3, "报红那一次不该落账，必须再等一次回车"
    assert sorted(ledger.passed_director_rows(session.directory)) == [1, 2]


def test_handoff_skips_segments_already_passed(tmp_path, monkeypatch, capsys):
    """续跑：账上已通过的段不重跑、不重问（不消耗回车）。"""
    from minimax_h3_prompt.director import ledger
    from minimax_h3_prompt.ui import wizard

    session = _director_session(tmp_path)
    config = _run_config(monkeypatch, tmp_path)
    cache_directory = _cache_dir_for(config, session.brief)
    first = _plant_cache(cache_directory, 1, 107)
    ledger.log_director_output(
        session.directory, segment=1, start_frame=0, end_frame=107,
        declared_frames=107, export_frames=107, actual_frames=107, cache_path=first)
    _plant_cache(cache_directory, 2, 153)

    asked: list[str] = []

    def fake_prompt(text: str) -> str:
        asked.append(text)
        if _is_rerun_question(text):   # #106 的首问：这条用例不重跑任何段
            return ""
        # 全段通过后会进交付（#108）：这里只数**陪跑**的问数。
        if "成片 mp4 路径" in text:
            return "q"
        return ""

    monkeypatch.setattr(wizard, "_drain_stdin", lambda: None)
    monkeypatch.setattr(wizard, "_prompt", fake_prompt)

    assert wizard._resume_director_handoff(config, session) == 0
    out = capsys.readouterr().out
    assert "已通过验收，跳过" in out and "第 1/2 段" in out
    segment_asks = [text for text in asked if _is_segment_question(text)]
    assert len(segment_asks) == 1, "只该为第 2 段问一次回车"
    assert "第 2/2 段" in segment_asks[0]


def test_handoff_stops_cleanly_without_recording_a_failed_segment(
        tmp_path, monkeypatch, capsys):
    """输入 q 停下：不落账、不报错（进度留在账本上，重跑向导接着走）。"""
    from minimax_h3_prompt.director import ledger
    from minimax_h3_prompt.ui import wizard

    session = _director_session(tmp_path)
    config = _run_config(monkeypatch, tmp_path)
    monkeypatch.setattr(wizard, "_drain_stdin", lambda: None)
    # 首问（#106）答回车＝不重跑；q 只对「段跑完后按回车」那一问被按下。
    monkeypatch.setattr(
        wizard, "_prompt",
        lambda text: "" if _is_rerun_question(text) else "q")

    assert wizard._resume_director_handoff(config, session) == 0
    assert ledger.passed_director_rows(session.directory) == {}
    assert "停在轮到第 1/2 段" in capsys.readouterr().out


def test_handoff_surfaces_the_raw_codec_disposal_instead_of_not_run(
        tmp_path, monkeypatch, capsys):
    """插件默认 ``cache_frames_codec="raw"`` 写的是 ``seg_0000.pt``：必须打出处置
    （切成 ffv1 后重跑），不能当成「这一段还没跑」静默过去（复审 S1）。

    现场：``cache.cached_segments`` 只认终稿 ``.frames.mkv``，所以只按「段号在不在
    ``cached`` 里」决定要不要读缓存时，``actual_frames_for_segment`` 的 ``.pt`` 分支
    在**生产路径上永远不可达**——用户只会一直看到 ``SEGMENT_NOT_RUN``，#105 的
    「`.pt` 明确报错并给处置」成了只活在单元测试里的规则（本仓「规则在、接线不在」）。
    """
    from minimax_h3_prompt.director import cache, ledger
    from minimax_h3_prompt.ui import wizard

    session = _director_session(tmp_path, ("opening",))
    config = _run_config(monkeypatch, tmp_path)
    cache_directory = _cache_dir_for(config, session.brief)
    # 只造 `.pt`（torch 张量，本项目没有 torch 读不了），没有任何 `.frames.mkv`。
    raw = cache.raw_frames_path(cache_directory, 1)
    raw.parent.mkdir(parents=True, exist_ok=True)
    raw.write_bytes(b"torch uint8 [N,H,W,3] placeholder")

    segment_asks = {"n": 0}

    def fake_prompt(text: str) -> str:
        if _is_rerun_question(text):   # #106 的首问：不重跑任何段
            return ""
        if _is_segment_question(text):
            segment_asks["n"] += 1
            # 第一次回车：读到 `.pt` → 打处置并**继续重问**；第二次 q 停下。
            return "" if segment_asks["n"] == 1 else "q"
        raise AssertionError(f"意外的问题：{text}")

    monkeypatch.setattr(wizard, "_drain_stdin", lambda: None)
    monkeypatch.setattr(wizard, "_prompt", fake_prompt)

    assert wizard._resume_director_handoff(config, session) == 0
    out = capsys.readouterr().out

    assert "cache_frames_codec" in out, "看不到处置提示：`.pt` 被判成了「没跑」"
    assert "ffv1" in out
    assert "SEGMENT_NOT_RUN" not in out, "有 `.pt` 就不能报「该跑未跑」"
    assert segment_asks["n"] == 2, "打出处之后必须继续重问，而不是落账或前进"
    assert ledger.passed_director_rows(session.directory) == {}, "没读到帧就不许落账"


def test_handoff_writes_the_tailframe_prompt_when_the_segment_needs_one(
        tmp_path, monkeypatch, capsys):
    """轮到运镜段时产出 ``tailframe-NN.md`` 并把校验问题打在终端上（#107 接线）。

    起点画面取**上一段缓存的末帧**（设计 §3.3）：这一段自己还没跑，拿不到自己的帧。
    """
    from minimax_h3_prompt.director import tailframe
    from minimax_h3_prompt.ui import wizard

    session = _director_session(tmp_path, ("opening", "camera_same"))
    config = _run_config(monkeypatch, tmp_path)
    cache_directory = _cache_dir_for(config, session.brief)
    _plant_cache(cache_directory, 1, 107)
    _plant_cache(cache_directory, 2, 153)

    good = (
        "### 变换指令\n把画面推到雨幕后的门框。\n\n"
        "### 保留项声明\n雨衣的灰色、招牌的橙光。\n\n"
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
    # 全段通过后会进交付（#108）：交付一问就停下（本用例只看尾帧接线）。
    monkeypatch.setattr(wizard, "_prompt",
                        lambda text: "q" if "成片 mp4 路径" in text else "")

    assert wizard._resume_director_handoff(
        config, session, llm=_TailLLM()) == 0

    text = (session.directory / "director" / "tailframe-02.md").read_text(encoding="utf-8")
    assert "变换指令" in text and "锁死清单" in text
    assert seen_images, "起点画面必须真读一次图"
    assert seen_images[0].name == "frame-01-last.png"
    assert "尾帧" in capsys.readouterr().out


def test_handoff_reuses_an_existing_tailframe_prompt(tmp_path, monkeypatch, capsys):
    """盘上已有该段的终点画面提示词 → 沿用、不重付、不覆盖（#58 的同一条教训）。

    这一段用户可能已经拿旧词生成过终点画面并挂上去了；重写一份新词会让「挂上去的图」
    与文件里的词不再对应（而账上不会记这件事）。
    """
    from minimax_h3_prompt.director import tailframe
    from minimax_h3_prompt.ui import wizard

    session = _director_session(tmp_path, ("opening", "camera_same"))
    config = _run_config(monkeypatch, tmp_path)
    cache_directory = _cache_dir_for(config, session.brief)
    _plant_cache(cache_directory, 1, 107)
    _plant_cache(cache_directory, 2, 153)

    old_text = (
        "### 变换指令\n旧词：把画面推到雨幕后的门框。\n\n"
        "### 保留项声明\n雨衣的灰色、招牌的橙光。\n\n"
        "### 锁死清单\n- 雨衣的灰色\n- 招牌的橙光\n"
    )
    path = session.directory / "director" / "tailframe-02.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(old_text, encoding="utf-8")

    def _boom(*args, **kwargs):
        raise AssertionError("已有尾帧文档还去读图/重写")

    monkeypatch.setattr(tailframe, "describe_image", _boom)
    monkeypatch.setattr(wizard, "_drain_stdin", lambda: None)
    # 全段通过后会进交付（#108）：交付一问就停下（本用例只看尾帧复用）。
    monkeypatch.setattr(wizard, "_prompt",
                        lambda text: "q" if "成片 mp4 路径" in text else "")

    class _TailLLM:
        def invoke(self, request, config=None):
            raise AssertionError("已有尾帧文档还去调模型")

    assert wizard._resume_director_handoff(config, session, llm=_TailLLM()) == 0
    assert path.read_text(encoding="utf-8") == old_text
    assert "沿用" in capsys.readouterr().out

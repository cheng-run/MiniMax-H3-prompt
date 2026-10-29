"""级联重跑（issue #106，设计 §3.8）：删该段整套缓存、作废下游通过行、回到第 N 段重跑。

三个缝（写断言前先定，别的都不测）：

1. ``director/cache.py`` 的 ``segment_cache_files`` / ``delete_segment_cache``
   ——「一段的缓存到底有哪几个文件、删哪几个」。清单真源是插件勘察
   （``docs/research/2026-10-29-director-插件原生格式.md`` §5.1 磁盘布局全表）。
2. ``director/cascade.py`` 的 ``cascade_rerun`` / ``render_cascade_notice``
   ——「作废谁、删谁、前 N-1 段不动」与两条路径（重跑／改词）的出站措辞。
3. ``ui/wizard._run_director_segment_handoff`` 的首尾两问——用户能看见、能答，
   答了真的回到第 N 段（入口口径＝2026-10-29 用户裁定：陪跑首尾各问一次）。

**绝不读真实 ComfyUI 目录**：缓存全部在 ``tmp_path`` 里现造，**空文件即可**——本票的
删除逻辑不看内容；数帧与判定是 #105 的用例在管（``tests/test_director_companion.py``）。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from minimax_h3_prompt.director import cache as director_cache

# 插件 ``seg_{idx:04d}`` 之后的全部后缀（勘察 §5.1）。**手写**而不是从被测模块抄：
# 从被测模块抄一份，删错清单也能通过（本仓「守卫形态要从常量派生」的反面）。
_FINAL_SUFFIXES = (
    ".frames.mkv", ".pt", ".meta.json", ".av.pt", ".audio.pt", ".handoff.json",
)
_FIRST_PASS_SUFFIXES = tuple(f".pre{suffix}" for suffix in _FINAL_SUFFIXES) + (".pre.low.pt",)


def _plant_segment(cache_directory: Path, segment: int) -> list[Path]:
    """按插件磁盘布局把**一段的全部**副产物造出来（0 基文件名，空文件）。"""
    cache_directory.mkdir(parents=True, exist_ok=True)
    stem = f"seg_{segment - 1:04d}"
    paths = [cache_directory / f"{stem}{suffix}"
             for suffix in _FINAL_SUFFIXES + _FIRST_PASS_SUFFIXES]
    for path in paths:
        path.write_bytes(b"")
    return paths


# ---------------------------------------------------------------------------
# 删除原语（cache.py）
# ---------------------------------------------------------------------------

def test_segment_cache_files_is_the_plugin_layout(tmp_path):
    """清单＝勘察 §5.1 全表：6 个终稿副产物 ＋ 6 个一采副产物（含 ``.pre.low.pt``）。

    一采那一整套必须在内：插件的 ``confirm_first_pass`` 命中一采缓存就跳过一采只跑
    二采（票面「一采为什么必删」），留着它等于让重跑复用改段之前的采样条件。
    """
    names = {path.name for path in director_cache.segment_cache_files(tmp_path, 1)}
    assert names == {f"seg_0000{suffix}" for suffix in _FINAL_SUFFIXES + _FIRST_PASS_SUFFIXES}
    assert len(names) == 13


def test_cache_file_list_derives_from_the_suffix_constants(tmp_path, monkeypatch):
    """变异检验：改掉后缀常量，清单必须跟着变（写死的字符串咬不住插件改版）。"""
    monkeypatch.setattr(director_cache, "FRAMES_SUFFIX", ".zzz.mkv")
    monkeypatch.setattr(director_cache, "RAW_FRAMES_SUFFIX", ".zzz.pt")

    names = {path.name for path in director_cache.segment_cache_files(tmp_path, 1)}

    assert {"seg_0000.zzz.mkv", "seg_0000.zzz.pt",
            "seg_0000.pre.zzz.mkv", "seg_0000.pre.zzz.pt"} <= names
    assert not {"seg_0000.frames.mkv", "seg_0000.pt"} & names


def test_delete_segment_cache_removes_every_artifact_of_that_segment(tmp_path):
    """删掉该段整套副产物，并**如实**返回删了哪些（调用方要拿它出站）。"""
    planted = _plant_segment(tmp_path, 3)

    deleted = director_cache.delete_segment_cache(tmp_path, 3)

    assert sorted(path.name for path in deleted) == sorted(path.name for path in planted)
    assert all(path.name.startswith("seg_0002") for path in deleted), "0 基文件名"
    assert all(not path.exists() for path in planted)


def test_delete_segment_cache_leaves_other_segments_alone(tmp_path):
    """只删第 N 段：同目录里别的段还在用（设计 §3.5「不删整个缓存目录」）。"""
    keep = _plant_segment(tmp_path, 1) + _plant_segment(tmp_path, 2) + _plant_segment(tmp_path, 4)
    neighbour = tmp_path / "notes.txt"
    neighbour.write_text("用户自己的东西", encoding="utf-8")

    director_cache.delete_segment_cache(tmp_path, 3)

    assert all(path.exists() for path in keep)
    assert neighbour.exists()


def test_delete_segment_cache_is_idempotent(tmp_path):
    """级联会对整条下游重复执行：文件本来就不在不算错，也不虚报删除。"""
    _plant_segment(tmp_path, 2)

    assert len(director_cache.delete_segment_cache(tmp_path, 2)) == 13
    assert director_cache.delete_segment_cache(tmp_path, 2) == ()


def test_delete_segment_cache_rejects_a_bad_segment_number(tmp_path):
    """段号必须是 1 起的整数：0 会让文件名变成 ``seg_-001``，字符串则谁也说不清。"""
    for bad in (0, -1, True, "3"):
        with pytest.raises(ValueError, match="段号"):
            director_cache.delete_segment_cache(tmp_path, bad)


def test_delete_segment_cache_refuses_a_path_that_escapes_the_cache_directory(tmp_path):
    """越界**报错且一个都不删**：缓存目录来自用户配置，不许在目录外动手。

    越界的现实形态是「目录里有个指向别处的链接」（用户自己或插件留下的），所以这里
    用符号链接造现场；本机建不了链接就跳过，而不是让守卫测试默默什么都不测。
    """
    cache_directory = tmp_path / "cache"
    planted = _plant_segment(cache_directory, 1)
    outside = tmp_path / "precious.mkv"
    outside.write_bytes(b"must-not-be-touched")
    link = cache_directory / "seg_0000.frames.mkv"
    link.unlink()
    try:
        link.symlink_to(outside)
    except OSError as exc:  # 没开开发者模式的 Windows：跳过并写明原因
        pytest.skip(f"本机建不了符号链接：{exc}")

    with pytest.raises(ValueError, match="缓存目录"):
        director_cache.delete_segment_cache(cache_directory, 1)

    assert outside.read_bytes() == b"must-not-be-touched"
    assert all(path.exists() for path in planted if path != link), "报错就不许删到一半"


# ---------------------------------------------------------------------------
# 级联编排（cascade.py，设计 §3.8）
# ---------------------------------------------------------------------------

def _record_pass(directory: Path, cache_directory: Path, segment: int) -> Path:
    """把某段记成「已通过」（#105 的通过行），并用真文件当它的缓存凭据。

    通过行要算 SHA256，所以凭据必须是个真文件；本票不比对摘要，只要行写得进去。
    **已有帧文件时不覆盖**：陪跑用例里那张是能真数帧/真取末帧的 FFV1，盖掉它，
    级联之后的「重新导出起点画面」就读不到文件了（第一次写这里就踩到了）。
    """
    from minimax_h3_prompt.director import ledger

    path = director_cache.frames_path(cache_directory, segment)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.is_file():
        path.write_bytes(f"segment-{segment}".encode())
    ledger.log_director_output(
        directory, segment=segment, start_frame=0, end_frame=100,
        declared_frames=100, export_frames=100, actual_frames=100, cache_path=path)
    return path


def _passed_five(tmp_path: Path) -> tuple[Path, Path]:
    """五段全部通过（各段全套副产物 ＋ 通过行），返回 (会话目录, 缓存目录)。"""
    directory = tmp_path / "GEN001"
    cache_directory = tmp_path / "cache"
    for segment in range(1, 6):
        _plant_segment(cache_directory, segment)
        _record_pass(directory, cache_directory, segment)
    return directory, cache_directory


def test_cascade_invalidates_the_target_segment_and_everything_after(tmp_path):
    """改第 3 段 → 第 3、4、5 段通过行作废、缓存删光（票面 AC1/AC2）。

    第 3 段自己也在内：票面设计写的是「第 N+1..」，但只作废下游等于让用户的
    「重跑第 3 段」被账上的通过行静默吞掉——重跑什么都不会发生。
    """
    from minimax_h3_prompt.director import cascade, ledger

    directory, cache_directory = _passed_five(tmp_path)

    result = cascade.cascade_rerun(
        directory, from_segment=3, total_segments=5, cache_directory=cache_directory)

    assert result.invalidated == (3, 4, 5)
    assert sorted(ledger.passed_director_rows(directory)) == [1, 2]
    for segment in (3, 4, 5):
        assert not any(path.exists() for path in director_cache.segment_cache_files(
            cache_directory, segment))
    invalidates = [row["segment"] for row in ledger.read_director_rows(directory)
                   if row["event"] == "invalidate"]
    assert invalidates == [3, 4, 5]
    assert len(result.deleted) == 3 * 13


def test_cascade_leaves_the_earlier_segments_untouched(tmp_path):
    """AC4：「不改动第 1、2 段的缓存」——一个文件都不许少，一行都不许加。"""
    from minimax_h3_prompt.director import cascade, ledger

    directory, cache_directory = _passed_five(tmp_path)
    untouched = [path for segment in (1, 2)
                 for path in director_cache.segment_cache_files(cache_directory, segment)]
    rows_before = [row for row in ledger.read_director_rows(directory)
                   if row["segment"] in (1, 2)]

    cascade.cascade_rerun(
        directory, from_segment=3, total_segments=5, cache_directory=cache_directory)

    assert all(path.is_file() for path in untouched), "前 2 段的缓存被误删"
    assert [row for row in ledger.read_director_rows(directory)
            if row["segment"] in (1, 2)] == rows_before


def test_cascade_refuses_a_segment_outside_the_plan(tmp_path):
    """越界段号报错且什么都不做：删到段表之外或整条段表都是事故。"""
    from minimax_h3_prompt.director import cascade, ledger

    directory, cache_directory = _passed_five(tmp_path)

    for bad in (0, -1, 6, True, "3"):
        with pytest.raises(ValueError, match="段号"):
            cascade.cascade_rerun(
                directory, from_segment=bad, total_segments=5,
                cache_directory=cache_directory)

    assert sorted(ledger.passed_director_rows(directory)) == [1, 2, 3, 4, 5]
    assert all(path.is_file() for segment in range(1, 6)
               for path in director_cache.segment_cache_files(cache_directory, segment))


def test_cascade_does_not_invalidate_segments_that_never_passed(tmp_path):
    """下游没跑过的段不凭空长作废行：账上的每一行都要对应一次真实发生的通过。"""
    from minimax_h3_prompt.director import cascade, ledger

    directory = tmp_path / "GEN001"
    cache_directory = tmp_path / "cache"
    _plant_segment(cache_directory, 1)
    _record_pass(directory, cache_directory, 1)

    result = cascade.cascade_rerun(
        directory, from_segment=2, total_segments=4, cache_directory=cache_directory)

    assert result.invalidated == ()
    assert ledger.read_director_rows(directory) == [
        row for row in ledger.read_director_rows(directory) if row["event"] == "output"]


def test_cascade_is_idempotent_when_the_cache_is_already_gone(tmp_path):
    """缓存已被删过一次时再级联不报错：级联会随「再改一段」反复执行。"""
    from minimax_h3_prompt.director import cascade

    directory, cache_directory = _passed_five(tmp_path)

    first = cascade.cascade_rerun(
        directory, from_segment=3, total_segments=5, cache_directory=cache_directory)
    second = cascade.cascade_rerun(
        directory, from_segment=3, total_segments=5, cache_directory=cache_directory)

    assert first.deleted and second.deleted == ()
    assert second.invalidated == ()  # 第二次没有「现在算通过」的段可作废


def test_cascade_notice_spells_out_the_two_rerun_paths(tmp_path):
    """两条路径都必须能走通、且出站说清差别（票面 AC3）。"""
    from minimax_h3_prompt.director import cascade

    directory, cache_directory = _passed_five(tmp_path)
    result = cascade.cascade_rerun(
        directory, from_segment=3, total_segments=5, cache_directory=cache_directory)

    notice = cascade.render_cascade_notice(result)

    # 不改词：直接回导演台重跑，不需要重新打包。
    assert "不改词" in notice and "runSelection" in notice
    # 改词：只能在导演台 UI 里改（项目侧的段文件不会自动同步过去）。
    assert "改词" in notice and "导演台 UI" in notice
    # 前 2 段没动要说出来，否则用户不知道第 1、2 段的缓存还能不能信。
    assert "第 1-2 段" in notice


def test_cascade_notice_omits_the_earlier_segment_line_when_rerunning_from_one(tmp_path):
    """从第 1 段起重跑时没有「前 N-1 段没动」可言，别印一句空话。"""
    from minimax_h3_prompt.director import cascade

    directory, cache_directory = _passed_five(tmp_path)
    result = cascade.cascade_rerun(
        directory, from_segment=1, total_segments=5, cache_directory=cache_directory)

    notice = cascade.render_cascade_notice(result)

    assert "第 1-0 段" not in notice and "前面" not in notice
    assert result.invalidated == (1, 2, 3, 4, 5)




# ---------------------------------------------------------------------------
# 派生件（2026-10-29 用户裁定）：级联连带删尾帧文档与末帧导出图
# ---------------------------------------------------------------------------
#
# 为什么必须删：``tailframe-NN.md`` 是从第 N-1 段的**旧**末帧写出来的词，
# ``frame-KK-last.png`` 是那帧的导出图。重跑第 N..末段会改变这些锚的像素，
# 不删就等于让 #105 的「已在盘上就沿用、不重付」一直喂陈旧词（用户会发现不了：
# 文件在、词也在，只有画面变了）。

def test_cascade_derived_artifacts_cover_the_range_and_the_preceding_anchor(tmp_path):
    """第 3..5 段重跑 → 删 tailframe-03..05.md 与 frame-02..04-last.png。

    ``frame-02`` 是第 N-1 段（第 2 段）的末帧导出图——它的缓存与通过行不动，但这张
    图是「第 3 段起点画面」的物化件，第 3 段重跑后它一定过期。
    ``frame-05`` 不是任何人的锚（末段没有下一段），不该删。
    """
    from minimax_h3_prompt.director import cascade

    names = {path.name for path in
             cascade.cascade_derived_artifacts(tmp_path, from_segment=3, total_segments=5)}

    assert names == {"tailframe-03.md", "tailframe-04.md", "tailframe-05.md",
                     "frame-02-last.png", "frame-03-last.png", "frame-04-last.png"}
    assert all(path.parent == tmp_path / "director" for path in
               cascade.cascade_derived_artifacts(tmp_path, from_segment=3, total_segments=5))


def test_cascade_derived_artifacts_start_at_frame_one_for_the_first_segment(tmp_path):
    """N=1 时没有第 0 段：末帧图从 ``frame-01-last.png`` 起（绝不能拼出 frame-00）。"""
    from minimax_h3_prompt.director import cascade

    names = {path.name for path in
             cascade.cascade_derived_artifacts(tmp_path, from_segment=1, total_segments=5)}

    assert {f"frame-{index:02d}-last.png" for index in (1, 2, 3, 4)} <= names
    assert "frame-00-last.png" not in names
    assert {f"tailframe-{index:02d}.md" for index in (1, 2, 3, 4, 5)} <= names


def test_cascade_deletes_the_derived_artifacts_of_the_range(tmp_path):
    """真的删：区间内的派生件消失，区间外的一个不动（第 5 段的末帧图不是任何人的锚）。"""
    from minimax_h3_prompt.director import cascade

    directory, cache_directory = _passed_five(tmp_path)
    director_dir = directory / "director"
    director_dir.mkdir(parents=True, exist_ok=True)
    planted = {}
    for segment in range(1, 6):
        md = director_dir / f"tailframe-{segment:02d}.md"
        md.write_text(f"第 {segment} 段的旧尾帧词\n", encoding="utf-8")
        planted[f"md{segment}"] = md
        png = director_dir / f"frame-{segment:02d}-last.png"
        png.write_bytes(b"old-frame")
        planted[f"png{segment}"] = png

    result = cascade.cascade_rerun(
        directory, from_segment=3, total_segments=5, cache_directory=cache_directory)

    assert {path.name for path in result.derived_deleted} == {
        "tailframe-03.md", "tailframe-04.md", "tailframe-05.md",
        "frame-02-last.png", "frame-03-last.png", "frame-04-last.png"}
    for name in ("md3", "md4", "md5", "png2", "png3", "png4"):
        assert not planted[name].exists(), f"{planted[name].name} 该删"
    for name in ("md1", "md2", "png1", "png5"):
        assert planted[name].exists(), f"{planted[name].name} 不该删"


def test_cascade_derived_deletion_is_idempotent(tmp_path):
    """文件不在不算错：级联会随「再改一段」反复执行。"""
    from minimax_h3_prompt.director import cascade

    directory, cache_directory = _passed_five(tmp_path)

    first = cascade.cascade_rerun(
        directory, from_segment=3, total_segments=5, cache_directory=cache_directory)
    second = cascade.cascade_rerun(
        directory, from_segment=3, total_segments=5, cache_directory=cache_directory)

    assert first.derived_deleted == ()      # 一条都没造过：如实报「没删到」
    assert second.derived_deleted == ()


def test_cascade_refuses_derived_paths_outside_the_session(tmp_path):
    """越出会话目录 → 报错且**缓存也不删**：先全量校验，再动手。"""
    from minimax_h3_prompt.director import cascade, ledger

    directory, cache_directory = _passed_five(tmp_path)
    director_dir = directory / "director"
    director_dir.mkdir(parents=True, exist_ok=True)
    outside = tmp_path / "outside.md"
    outside.write_text("不许动", encoding="utf-8")
    link = director_dir / "tailframe-03.md"
    try:
        link.symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"本机建不了符号链接：{exc}")

    with pytest.raises(ValueError, match="会话目录"):
        cascade.cascade_rerun(
            directory, from_segment=3, total_segments=5, cache_directory=cache_directory)

    assert outside.read_text(encoding="utf-8") == "不许动"
    assert all(path.is_file() for segment in range(1, 6)
               for path in director_cache.segment_cache_files(cache_directory, segment)), \
        "校验失败时缓存也不许删到一半"
    assert sorted(ledger.passed_director_rows(directory)) == [1, 2, 3, 4, 5]


def test_cascade_notice_lists_the_derived_artifacts_it_deleted(tmp_path):
    """出站要点名删了哪些派生件：用户可能正开着那份 md。"""
    from minimax_h3_prompt.director import cascade

    directory, cache_directory = _passed_five(tmp_path)
    director_dir = directory / "director"
    director_dir.mkdir(parents=True, exist_ok=True)
    (director_dir / "tailframe-03.md").write_text("旧词\n", encoding="utf-8")
    (director_dir / "frame-02-last.png").write_bytes(b"old")

    result = cascade.cascade_rerun(
        directory, from_segment=3, total_segments=5, cache_directory=cache_directory)

    notice = cascade.render_cascade_notice(result)

    assert "tailframe-03.md" in notice and "frame-02-last.png" in notice
    assert "派生件" in notice


def test_cascade_rejects_a_bad_range_for_the_derived_artifacts(tmp_path):
    """纯函数也要守段号：越界会让文件名落到不存在的段上。"""
    from minimax_h3_prompt.director import cascade

    for bad in (0, -1, 6):
        with pytest.raises(ValueError, match="段号"):
            cascade.cascade_derived_artifacts(tmp_path, from_segment=bad, total_segments=5)


# ---------------------------------------------------------------------------
# 向导接线（ui/wizard.py）：陪跑首尾各问一次「要重跑哪一段」（用户裁定 2026-10-29）
# ---------------------------------------------------------------------------
#
# 作答方式（#57 实测的坑）：给新问答作答必须**按问题文本**匹配，不许往输入表末尾追加
# ——追加会被上游菜单（回车=出队首）吃掉，流程整体挪一步，还会真发 LLM 请求。

_HEAD_QUESTION = "有要重跑的段吗"
_TAIL_QUESTION = "有要改哪一段重来吗"
_SEGMENT_QUESTION = "在导演台里跑完后按回车"
# #108 的交付问答：全部通过之后接在陪跑末尾。本票只测级联与两问，所以一进交付就用
# q 停在交付前——**不答回车**：空路径会被交付那层当成错误反复重问（死循环）。
_DELIVERY_QUESTION = "成片 mp4 路径"
# 交付那层的头一行（#108）：用来断言「尾问排在交付之前」。
_DELIVERY_ANNOUNCEMENT = "接下来在导演台里合并成片"


def _write_ffv1_mkv(path: Path, frames: int, *, width: int = 32, height: int = 24) -> Path:
    """造一段与插件同编码（FFV1 / rgb24）的真帧文件——陪跑要真数帧，空文件不行。"""
    import subprocess

    import imageio_ffmpeg

    path.parent.mkdir(parents=True, exist_ok=True)
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


def _director_brief():
    from minimax_h3_prompt.brief_parser import Brief

    return Brief(mode="director", variant="T2VA", director_mode="t2v",
                 duration=30.0, plot="雨夜旧信",
                 director_node_id="42", director_cache_name="雨夜",
                 director_continuity_mode="guide")


def _plan_of(types: tuple[str, ...]):
    """按段类型造一张段表；``continue`` 在 t2v 下不要终点画面，陪跑不会去调模型。"""
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
            give_images=tuple(give_images_for(segment_type, "t2v")),
        ))
        cursor += export
    return DirectorPlan(mode="t2v", total_frames=cursor, segments=tuple(segments))


def _director_session(tmp_path: Path, types: tuple[str, ...] = ("opening", "continue", "continue")):
    from minimax_h3_prompt.director.planning import plan_path, write_plan_json
    from minimax_h3_prompt.director.prompts import segment_path
    from minimax_h3_prompt.session_store import (
        STATUS_DIRECTOR_SEGMENTS_RUNNING, SessionState,
    )

    directory = tmp_path / "GEN001"
    plan = _plan_of(types)
    write_plan_json(plan_path(directory), plan)
    for segment in plan.segments:
        path = segment_path(directory, segment.index)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"段 {segment.index} 的提示词\n", encoding="utf-8")
    return SessionState(directory=directory, brief=_director_brief(),
                        stage_state={}, status=STATUS_DIRECTOR_SEGMENTS_RUNNING)


def _run_config(monkeypatch, tmp_path: Path):
    """只填 ``director:`` 节的 Config——不读仓里那份 YAML（换机器/换网关都不影响）。"""
    from minimax_h3_prompt import config as config_module

    monkeypatch.setattr(config_module, "load_dotenv", lambda *_a, **_k: None)
    monkeypatch.setattr(config_module, "_load_yaml",
                        lambda _path: {"director": {"comfy_output_root": str(tmp_path)}})
    monkeypatch.delenv("H3_COMFY_OUTPUT_ROOT", raising=False)
    return config_module.Config()


def _cache_dir_for(config, brief) -> Path:
    return director_cache.cache_dir(
        config.director_comfy_output_root(), brief.director_cache_name,
        brief.director_node_id)


def _plant_ready(config, session, frames: dict[int, int]) -> Path:
    """把若干段造好：全套副产物（空文件）＋ 一张能真数帧的终稿帧文件。"""
    cache_directory = _cache_dir_for(config, session.brief)
    for segment, count in frames.items():
        _plant_segment(cache_directory, segment)
        _write_ffv1_mkv(director_cache.frames_path(cache_directory, segment), count)
    return cache_directory


def _labels(asks: list[str]) -> list[str]:
    """把问过的题目归类（作答与断言都按问题文本，不按调用次序数）。"""
    labels = []
    for text in asks:
        if _HEAD_QUESTION in text:
            labels.append("head")
        elif _TAIL_QUESTION in text:
            labels.append("tail")
        elif _SEGMENT_QUESTION in text:
            labels.append("segment")
        else:
            labels.append("other")
    return labels


def test_handoff_head_question_cascades_and_reruns_that_segment(
        tmp_path, monkeypatch, capsys):
    """首问答「2」：级联作废第 2、3 段＋删它们的缓存，然后**从第 2 段重跑**（不回第 1 段）。"""
    from minimax_h3_prompt.director import ledger
    from minimax_h3_prompt.ui import wizard

    session = _director_session(tmp_path)
    config = _run_config(monkeypatch, tmp_path)
    cache_directory = _plant_ready(config, session, {1: 107, 2: 153, 3: 153})
    for segment in (1, 2, 3):
        _record_pass(session.directory, cache_directory, segment)

    asked: list[str] = []
    segment_asks = {"n": 0}

    def fake_prompt(text: str) -> str:
        if _DELIVERY_QUESTION in text:   # #108 的交付：停在交付前，本票不测交付
            return "q"
        asked.append(text)
        if _HEAD_QUESTION in text:
            return "2"
        if _TAIL_QUESTION in text:
            return ""
        segment_asks["n"] += 1
        # 用户回导演台重跑：第 2、3 段的帧文件已在级联里被删，这里补上。
        _write_ffv1_mkv(
            director_cache.frames_path(cache_directory, 1 + segment_asks["n"]), 153)
        return ""

    monkeypatch.setattr(wizard, "_drain_stdin", lambda: None)
    monkeypatch.setattr(wizard, "_prompt", fake_prompt)

    assert wizard._resume_director_handoff(config, session) == 0

    out = capsys.readouterr().out
    assert "[级联]" in out and "作废通过行" in out
    assert "第 1 段" in out          # 前 N-1 段没动要说出来
    # 只问了第 2、3 段：第 1 段没被重跑。
    assert _labels(asked) == ["head", "segment", "segment", "tail"]
    assert "第 2/3 段" in asked[1] and "第 3/3 段" in asked[2]
    rows = ledger.read_director_rows(session.directory)
    assert [row["segment"] for row in rows if row["event"] == "invalidate"] == [2, 3]
    assert sorted(ledger.passed_director_rows(session.directory)) == [1, 2, 3]
    # 第 1 段的**全套**副产物一个不少（AC4），第 2、3 段的一采残留被删掉了。
    assert (cache_directory / "seg_0000.pre.frames.mkv").is_file()
    assert not (cache_directory / "seg_0001.pre.frames.mkv").exists()
    assert not (cache_directory / "seg_0002.pre.frames.mkv").exists()


def test_handoff_head_question_with_a_blank_answer_changes_nothing(
        tmp_path, monkeypatch, capsys):
    """回车＝不用：账与缓存一个都不许动（首问是可选动作，默认不动）。"""
    from minimax_h3_prompt.director import ledger
    from minimax_h3_prompt.ui import wizard

    session = _director_session(tmp_path)
    config = _run_config(monkeypatch, tmp_path)
    cache_directory = _plant_ready(config, session, {1: 107, 2: 153, 3: 153})
    for segment in (1, 2, 3):
        _record_pass(session.directory, cache_directory, segment)
    rows_before = ledger.read_director_rows(session.directory)

    monkeypatch.setattr(wizard, "_drain_stdin", lambda: None)
    monkeypatch.setattr(
        wizard, "_prompt",
        lambda text: "q" if _DELIVERY_QUESTION in text else "")

    assert wizard._resume_director_handoff(config, session) == 0

    out = capsys.readouterr().out
    assert "[级联]" not in out
    assert ledger.read_director_rows(session.directory) == rows_before
    assert all((cache_directory / f"seg_{segment - 1:04d}.handoff.json").is_file()
               for segment in (1, 2, 3))


def test_handoff_head_question_reasks_on_an_illegal_answer(
        tmp_path, monkeypatch, capsys):
    """非法/越界必须**重问**，不许静默当成「不用」（用户会以为自己触发了重跑）。"""
    from minimax_h3_prompt.director import ledger
    from minimax_h3_prompt.ui import wizard

    session = _director_session(tmp_path)
    config = _run_config(monkeypatch, tmp_path)
    _plant_ready(config, session, {1: 107, 2: 153, 3: 153})

    asked: list[str] = []
    answers = iter(["abc", "9", ""])

    def fake_prompt(text: str) -> str:
        if _DELIVERY_QUESTION in text:   # #108 的交付：停在交付前
            return "q"
        asked.append(text)
        if _HEAD_QUESTION in text:
            return next(answers)
        return ""

    monkeypatch.setattr(wizard, "_drain_stdin", lambda: None)
    monkeypatch.setattr(wizard, "_prompt", fake_prompt)

    assert wizard._resume_director_handoff(config, session) == 0

    out = capsys.readouterr().out
    assert "无效段号" in out
    assert _labels(asked).count("head") == 3, "abc 与 9 都要重问，只有回车才是「不用」"
    # 重问之后按「不用」走完全程：只该有通过行，不该凭空长作废行。
    assert [row["event"] for row in ledger.read_director_rows(session.directory)
            if row["event"] == "invalidate"] == []


def test_handoff_head_question_quit_leaves_everything_alone(
        tmp_path, monkeypatch, capsys):
    """首问答 q：停在陪跑开始前，账与缓存都没被碰过（不落账、不删文件）。"""
    from minimax_h3_prompt.director import ledger
    from minimax_h3_prompt.ui import wizard

    session = _director_session(tmp_path)
    config = _run_config(monkeypatch, tmp_path)
    cache_directory = _plant_ready(config, session, {1: 107})

    monkeypatch.setattr(wizard, "_drain_stdin", lambda: None)
    monkeypatch.setattr(wizard, "_prompt", lambda text: "q")

    assert wizard._resume_director_handoff(config, session) == 0

    out = capsys.readouterr().out
    assert "陪跑开始前" in out
    assert ledger.read_director_rows(session.directory) == []
    assert all(path.is_file() for path in director_cache.segment_cache_files(
        cache_directory, 1))


def test_handoff_tail_question_sends_the_user_back_to_that_segment(
        tmp_path, monkeypatch, capsys):
    """全部通过后再问一次「有要改哪一段重来吗？」：答「2」回到第 2 段，答回车收工。"""
    from minimax_h3_prompt.director import ledger
    from minimax_h3_prompt.ui import wizard

    session = _director_session(tmp_path)
    config = _run_config(monkeypatch, tmp_path)
    cache_directory = _plant_ready(config, session, {1: 107, 2: 153, 3: 153})

    asked: list[str] = []
    captured: list[str] = []
    tail_asks = {"n": 0}
    segment_asks = {"n": 0}

    def fake_prompt(text: str) -> str:
        if _DELIVERY_QUESTION in text:   # #108 的交付：停在交付前，本票不测交付
            return "q"
        asked.append(text)
        if _HEAD_QUESTION in text:
            return ""
        if _TAIL_QUESTION in text:
            tail_asks["n"] += 1
            if tail_asks["n"] == 2:
                # 第二次尾问此刻的出站：到这里为止还不能出现交付（#108）——
                # 尾问必须排在交付**之前**，否则用户会带着一段没跑的词直接归档。
                captured.append(capsys.readouterr().out)
                assert _DELIVERY_ANNOUNCEMENT not in captured[-1]
                return ""
            return "2"
        segment_asks["n"] += 1
        # 头一轮照常跑第 1..3 段；重跑那一轮再补第 2、3 段的帧文件。
        if segment_asks["n"] == 4:
            _write_ffv1_mkv(director_cache.frames_path(cache_directory, 2), 153)
        if segment_asks["n"] == 5:
            _write_ffv1_mkv(director_cache.frames_path(cache_directory, 3), 153)
        return ""

    monkeypatch.setattr(wizard, "_drain_stdin", lambda: None)
    monkeypatch.setattr(wizard, "_prompt", fake_prompt)

    assert wizard._resume_director_handoff(config, session) == 0

    out = "".join(captured) + capsys.readouterr().out
    assert _labels(asked) == ["head", "segment", "segment", "segment",
                              "tail", "segment", "segment", "tail"]
    assert tail_asks["n"] == 2
    assert "[级联]" in out
    assert [row["segment"] for row in ledger.read_director_rows(session.directory)
            if row["event"] == "invalidate"] == [2, 3]
    assert sorted(ledger.passed_director_rows(session.directory)) == [1, 2, 3]
    assert _DELIVERY_ANNOUNCEMENT in out, "尾问答完回车之后才轮到交付"
    assert "停在交付前（成片路径）" in out, "交付按 q 停下，不归档"


def test_cascade_then_the_tailframe_prompt_is_written_again(tmp_path, monkeypatch, capsys):
    """级联删掉旧尾帧词之后，轮到该段必须**重新写**，不是「已在盘上就沿用」。

    与 #105 的 ``test_handoff_reuses_an_existing_tailframe_prompt`` 是一对
    （那条测「没级联时沿用」，这条测「级联之后不沿用」）：两条合起来才说明删除真的
    改变了行为，而不只是账面上少了几行。
    """
    from minimax_h3_prompt.director import tailframe
    from minimax_h3_prompt.ui import wizard

    session = _director_session(tmp_path, ("opening", "camera_same"))
    config = _run_config(monkeypatch, tmp_path)
    cache_directory = _plant_ready(config, session, {1: 107, 2: 153})
    for segment in (1, 2):
        _record_pass(session.directory, cache_directory, segment)

    stale = session.directory / "director" / "tailframe-02.md"
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.write_text(
        "### 变换指令\n旧词：不该被沿用。\n\n"
        "### 保留项声明\n旧。\n\n### 锁死清单\n- 旧\n", encoding="utf-8")

    fresh = (
        "### 变换指令\n把画面推到雨幕后的门框。\n\n"
        "### 保留项声明\n雨衣的灰色、招牌的橙光。\n\n"
        "### 锁死清单\n- 雨衣的灰色\n- 招牌的橙光\n"
    )

    class _TailLLM:
        def invoke(self, request, config=None):
            return fresh

    seen_images: list[Path] = []

    def fake_describe(image, instruction=""):
        seen_images.append(Path(image))
        return "起止画面：雨衣的人是灰色的，招牌是橙色的。"

    monkeypatch.setattr(tailframe, "describe_image", fake_describe)
    monkeypatch.setattr(wizard, "_drain_stdin", lambda: None)

    def fake_prompt(text: str) -> str:
        if _DELIVERY_QUESTION in text:
            return "q"
        if _HEAD_QUESTION in text:
            return "2"
        if _TAIL_QUESTION in text:
            return ""
        # 段回车：第 2 段的缓存已在级联里删掉，用户回导演台重跑后这里补上。
        _write_ffv1_mkv(director_cache.frames_path(cache_directory, 2), 153)
        return ""

    monkeypatch.setattr(wizard, "_prompt", fake_prompt)

    assert wizard._resume_director_handoff(config, session, llm=_TailLLM()) == 0

    text = stale.read_text(encoding="utf-8")
    assert "旧词" not in text, "级联之后不许沿用陈旧的尾帧词"
    assert "雨幕后的门框" in text, "要按新起点画面重写"
    assert seen_images and seen_images[0].name == "frame-01-last.png", \
        "旧末帧图已被删，起点画面必须重新导出"
    assert "tailframe-02.md" in capsys.readouterr().out

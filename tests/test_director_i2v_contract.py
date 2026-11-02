"""导演台 i2v 与插件执行契约的交叉校验（2026-10-30 真实跑纠错）。

**这三条用例不是自证 fixture**：它们断言的是**插件**的行为，不是我们自己的实现自洽。
红过的现场（`docs/handoffs/2026-10-30-导演台i2v纠错交接.md`）：20 秒 5 段 i2v 包在
ComfyUI 里第一次运行就抛 ``ValueError: Segment #2 has no source image``，而这批
测试当时全是绿的——因为旧用例断言的正是「非开场段 genImage 为空」。

插件事实（HEAD ``4c6245e``，逐行读过）：

- ``director/gen_timeline.py:22`` ``GEN_IMAGE_KEYS = frozenset({"i2v"})``：i2v 恒走
  ``gen_image`` 子模式。
- ``gen_timeline.py:393-394`` + ``:47-54``：包写 ``timelineMode="prompt_batch"`` 时
  ``edit_mode`` 被**强制**成 ``"segment"``，于是 ``_resolve_gen_image_ref``（``:126-144``）
  只读段内 ``genImage``/段级 ``imageFile``，全局图分支对我们是死路。
- ``gen_timeline.py:304-310``：任一段没有源图 → 建计划阶段直接抛
  ``Segment #N has no source image``（还没进采样）。
- ``director/executor_core.py:851-856``：``i2v_new_anchor = task_key == "i2v" and
  first_frame is not None`` → ``use_motion_context=False``。i2v 段必然有首帧图，
  于是**段间引导对 i2v 永久失效**——i2v 没有「续接」这个语义。

所以导演台 i2v 只能是一条片一段（ADR 0026 2026-10-30 修订）。下面三条分别钉住：
规划期拒绝多段、打包期拒绝多段（哪怕有人绕过规划直接构造段表）、插件契约没变。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

# 17k+5 网格上的一段 5.8 秒（141 帧）：单段 i2v 的合法落位值之一。
_SINGLE_SEGMENT = """{"segments": [
  {"start_s": 0, "end_s": 5.875, "type": "opening", "summary": "一个镜头",
   "end_hook": "人物停在窗前"}
]}"""

# 10-30 真实跑的形态：开场 + 4 段续接，20 秒。旧实现能让它通过规划与打包，
# 然后在 ComfyUI 里当场炸。
_MULTI_SEGMENT = """{"segments": [
  {"start_s": 0, "end_s": 4, "type": "opening", "summary": "a", "end_hook": "h1"},
  {"start_s": 4, "end_s": 8, "type": "continue", "summary": "b", "end_hook": "h2"},
  {"start_s": 8, "end_s": 12, "type": "continue", "summary": "c", "end_hook": "h3"},
  {"start_s": 12, "end_s": 16, "type": "continue", "summary": "d", "end_hook": "h4"},
  {"start_s": 16, "end_s": 20, "type": "continue", "summary": "e", "end_hook": "h5"}
]}"""

# 插件真身（本机路径）；CI 或别的机器上没有就跳过——但**不许**用它不存在当借口
# 把契约断言删掉。
PLUGIN_ROOT = Path("D:/Comfyui/ComfyUI/custom_nodes/ComfyUI_MiniMaxH3_Director")


def _write_prompts(session_dir: Path, plan) -> None:
    from minimax_h3_prompt.director import prompts as director_prompts

    for segment in plan.segments:
        path = director_prompts.segment_path(session_dir, segment.index)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"第 {segment.index} 段正文。\n", encoding="utf-8")


def _write_asset(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x89PNG\r\n\x1a\nfake")
    return path


def _settings():
    from minimax_h3_prompt.director.pack import DirectorPackSettings

    return DirectorPackSettings(width=736, height=1280, ref_max_size=864,
                                ref_image_size="max")


def test_i2v_planning_refuses_more_than_one_segment():
    """规划期就拒：i2v 是「一张首帧 → 一段视频」，多段在插件里互不衔接。

    拒在规划期而不是打包期，是因为用户已经为段规划付过 LLM 的钱——让他拿到一张
    「看着合法、打包才炸」的段表，正是 10-30 现场。
    """
    from minimax_h3_prompt.director.planning import parse_director_plan

    with pytest.raises(ValueError, match="i2v"):
        parse_director_plan(_MULTI_SEGMENT, 20.0, mode="i2v")


def test_i2v_pack_refuses_a_segment_without_a_source_image(tmp_path):
    """打包期兜底：手造句段表绕过规划时，也不许产出插件会拒绝的包。

    判据来自插件 ``gen_timeline.py:304-310``：段内无 ``genImage``/``imageFile``
    即抛 ``Segment #N has no source image``。我们的包一旦写出这种段，用户导入后
    只会看到一句英文报错，段表却是「成功」的。
    """
    from minimax_h3_prompt.director.frames import DirectorPlan, DirectorSegment
    from minimax_h3_prompt.director.pack import prepare_director_pack

    segments = (
        DirectorSegment(index=1, type="opening", guided=False, start_frame=0,
                        end_frame=107, declared_frames=107, export_frames=107,
                        summary="a", end_hook="h1", give_images=("first",)),
        DirectorSegment(index=2, type="continue", guided=True, start_frame=107,
                        end_frame=260, declared_frames=141, export_frames=153,
                        summary="b", end_hook="h2", give_images=()),
    )
    plan = DirectorPlan(mode="i2v", total_frames=260, segments=segments)
    session_dir = tmp_path / "GEN001"
    _write_prompts(session_dir, plan)
    _write_asset(session_dir / "director" / "assets" / "01" / "start.png")

    with pytest.raises(ValueError, match="i2v"):
        prepare_director_pack(session_dir, plan, settings=_settings())


def test_i2v_single_segment_pack_puts_a_source_image_on_that_segment(tmp_path):
    """单段 i2v 的良性形态：唯一那段必须带 ``genImage`` + 段级 ``imageFile``。"""
    from minimax_h3_prompt.director.pack import prepare_director_pack
    from minimax_h3_prompt.director.planning import parse_director_plan

    plan = parse_director_plan(_SINGLE_SEGMENT, 5.875, mode="i2v")
    session_dir = tmp_path / "GEN001"
    _write_prompts(session_dir, plan)
    _write_asset(session_dir / "director" / "assets" / "01" / "start.png")

    job, _ = prepare_director_pack(session_dir, plan, settings=_settings())
    timeline = json.loads(
        (job.pack_dir / "timeline.json").read_text(encoding="utf-8"))
    assert len(timeline["segments"]) == 1
    segment = timeline["segments"][0]
    assert segment["genImage"]["imageFile"] == "asset_groups/01/start.png"
    assert segment["imageFile"] == "asset_groups/01/start.png"


def test_single_segment_max_matches_preflight():
    """两处「单段 15 秒」必须同值：一处改了另一处不改，就会一边放行一边拒绝。

    ``frames.SINGLE_SEGMENT_MAX_S`` 与 ``tools/preflight.SEGMENT_MAX_S`` 是两个模块、
    两种用途（前者拦 i2v 整片时长，后者验交回的段视频档位），刻意不互相 import
    （frames 是纯算术件，preflight 拉 cv2）。这条用例就是它们之间唯一的钉合。
    """
    from minimax_h3_prompt.director.frames import SINGLE_SEGMENT_MAX_S
    from minimax_h3_prompt.tools.preflight import SEGMENT_MAX_S

    assert SINGLE_SEGMENT_MAX_S == SEGMENT_MAX_S


def test_single_segment_min_matches_preflight():
    """#141 的 4s 落位下限与 preflight 同源：改一处不改另一处当场报红。

    与上一条同理，``frames.SEGMENT_MIN_S`` 是纯算术件里的副本，只用来判断
    「规划窗口是否承诺了 4s」；真正的段视频档位口径在 ``tools/preflight.py``。
    """
    from minimax_h3_prompt.director.frames import SEGMENT_MIN_S
    from minimax_h3_prompt.tools.preflight import SEGMENT_MIN_S as PREFLIGHT_MIN_S

    assert SEGMENT_MIN_S == PREFLIGHT_MIN_S


def test_wizard_clamps_the_i2v_duration_before_stage_one(monkeypatch):
    """选完 i2v 后超限时长必须当场收正：回车取上限，合法输入原样采纳。

    收在阶段 1 之前（而不是规划/打包处）：阶段 1 的剧本、分镜、生图词全按这个时长
    出，跑完再拒等于让用户白付一整轮。
    """
    from minimax_h3_prompt.director.frames import SINGLE_SEGMENT_MAX_S
    from minimax_h3_prompt.ui import wizard

    answers = iter(["9.5"])
    monkeypatch.setattr(wizard, "_prompt", lambda text: next(answers))

    assert wizard._clamp_single_segment_duration(20.0) == 9.5
    # 再跑一次：回车＝采纳上限，且不会去读第二个答案
    monkeypatch.setattr(wizard, "_prompt", lambda text: "")
    assert wizard._clamp_single_segment_duration(20.0) == SINGLE_SEGMENT_MAX_S
    # 限内输入不该被问第二次（位置语义的问答表挤进一问就会整体错位）
    assert wizard._clamp_single_segment_duration(9.5) == 9.5


@pytest.mark.skipif(
    not (PLUGIN_ROOT / "director" / "gen_timeline.py").is_file(),
    reason="本机没有插件真身：契约交叉校验跳过（不删断言）",
)
def test_plugin_still_treats_i2v_as_image_driven_but_motion_context_free():
    """插件升级后重核本文件的三个前提；任何一条变了，单段制的依据就要重读。

    - ``GEN_IMAGE_KEYS`` 只收 i2v → i2v 仍要求每段一张图；
    - ``Segment #N has no source image`` 仍在 → 缺图仍是建计划期硬错；
    - ``i2v_new_anchor`` 仍在 → 「有首帧就跳过段间引导」仍成立。
    """
    gen_source = (PLUGIN_ROOT / "director" / "gen_timeline.py").read_text(
        encoding="utf-8")
    executor_source = (PLUGIN_ROOT / "director" / "executor_core.py").read_text(
        encoding="utf-8")

    assert 'GEN_IMAGE_KEYS = frozenset({"i2v"})' in gen_source
    assert "has no source image" in gen_source
    assert "i2v_new_anchor" in executor_source

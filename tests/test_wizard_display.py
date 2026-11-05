"""向导展示层的金样（体检 P5：拆 ``wizard.py`` 第一刀）。

``ui/wizard_display.py`` 是从 6400 行的 ``wizard.py`` 里搬出来的纯函数。搬家的
**验收判据是行为逐字不变**，所以这里钉的是几条**验收事故现场**留下的硬要求：

- 多段必须说「用 runSelection 只勾第 N 段」（全片点运行会整条重烧）；
- 导出是「全部导出」，跑完的视频含前面已有段（2026-10-31 现场贴回 226 帧）；
- 段缓存目录只供定位、不是判定依据（#112）；
- 切镜段要关「引用上段」（#137 段2 实测多出 12 帧保完整尾）；
- 单段计划不印缓存目录（插件根本不为单段写缓存）。

变异检验：删掉任何一句硬要求，对应用例红。
"""
from __future__ import annotations

from pathlib import Path

from minimax_h3_prompt.asset_loop import QueueItem
from minimax_h3_prompt.director.frames import DirectorPlan, DirectorSegment
from minimax_h3_prompt.ui.wizard_display import (
    render_asset_model_line,
    render_director_segment_briefing,
)


def _segment(index, segment_type, start, declared, export, guided):
    return DirectorSegment(
        index=index, type=segment_type, guided=guided,
        start_frame=start, end_frame=start + export,
        declared_frames=declared, export_frames=export,
        summary=f"段 {index} 剧情", end_hook=f"段 {index} 落定画面",
        give_images=(),
    )


def _plan(*segments, mode="t2v"):
    return DirectorPlan(mode=mode, total_frames=sum(s.export_frames for s in segments),
                        segments=tuple(segments))


def _write_prompt(tmp_path: Path, index: int, text: str) -> None:
    from minimax_h3_prompt.director.prompts import segment_path

    path = segment_path(tmp_path, index)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_multi_segment_briefing_keeps_the_acceptance_hard_lines(tmp_path):
    segments = (
        _segment(1, "opening", 0, 107, 107, False),
        _segment(2, "continue", 107, 141, 153, True),
    )
    plan = _plan(*segments)
    _write_prompt(tmp_path, 2, "第二段的提示词正文")

    text = render_director_segment_briefing(
        plan, segments[1], tmp_path,
        cache_directory=tmp_path / "cache", cache_name="我的缓存")

    assert "第 2/2 段" in text
    assert "runSelection **只勾第 2 段**" in text, "全片点运行会整条时间线重烧"
    assert "导出是「全部导出」" in text, "用户会以为贴回的合片是跑错了"
    assert "第二段的提示词正文" in text, "提示词正文要直接打出来，别只给路径"
    assert "只供定位，不是判定依据" in text
    assert "cache_name 也填：我的缓存" in text
    assert "成片视频路径贴回来" in text


def test_cut_segment_briefing_tells_you_to_disable_previous_clip(tmp_path):
    segments = (
        _segment(1, "opening", 0, 107, 107, False),
        _segment(2, "camera_cut", 107, 124, 136, False),
    )
    plan = _plan(*segments)

    text = render_director_segment_briefing(plan, segments[1], tmp_path)

    assert "本段是切镜段" in text and "引用上段" in text
    assert "continuityFromPrev=false" in text, "包内已关，用户手动勾回会多 12 帧"


def test_single_shot_briefing_omits_cache_lines(tmp_path):
    segments = (_segment(1, "opening", 0, 107, 107, False),)
    plan = _plan(*segments)

    text = render_director_segment_briefing(
        plan, segments[0], tmp_path, single_shot=True,
        cache_directory=tmp_path / "cache", cache_name="不该出现")

    assert "直接点运行" in text
    assert "段缓存目录" not in text, "单段计划插件不写缓存，印路径只会误导"
    assert "cache_name" not in text


def test_fl2v_briefing_matches_the_film_end_tail_image_rule(tmp_path):
    """单段 fl2v 且有全片尾帧时简报印「首+尾」；多段开场只印「首帧」（#147 AC2）。

    简报是用户逐段执行时唯一看得到的给图说明。它若与段表清单/打包口径不一致，
    用户会以为单段片子没有终点画面（或以为多段开场挂了尾帧）。
    """
    from dataclasses import replace

    from minimax_h3_prompt.director.frames import give_images_for
    from minimax_h3_prompt.director.pack import ASSETS_DIRNAME, DIRECTOR_DIRNAME

    opening = replace(
        _segment(1, "opening", 0, 107, 107, False),
        give_images=give_images_for("opening", "fl2v"))

    def give_line(plan, segment):
        text = render_director_segment_briefing(plan, segment, tmp_path)
        return next(line for line in text.splitlines() if "给图：" in line)

    single = _plan(opening, mode="fl2v")
    assert "尾帧" not in give_line(single, opening), "没有 film-end 时不该印尾帧"

    assets = tmp_path / DIRECTOR_DIRNAME / ASSETS_DIRNAME
    assets.mkdir(parents=True)
    (assets / "film-end.png").write_bytes(b"not a real png")
    line = give_line(single, opening)
    assert "首帧" in line and "尾帧" in line, line

    multi = _plan(opening, _segment(2, "continue", 107, 141, 153, True), mode="fl2v")
    multi_line = give_line(multi, multi.segments[0])
    assert "尾帧" not in multi_line, multi_line


def test_asset_model_line_reads_the_recorded_assignment():
    state = {"character_image_prompts": [{
        "name": "红裙女孩",
        "positive_prompt": "提示词",
        "image_model": {"model": "moss", "source": "auto", "keyword": "包装"},
    }]}
    item = QueueItem(picture=1, kind="character", name="红裙女孩")

    line = render_asset_model_line(state, item)

    assert line.startswith("[生图模型] 红裙女孩：命中『包装』")


def test_asset_model_line_shows_the_sentence_that_decided_the_model():
    """素材循环菜单与终端那一行同源：都念出命中的来源句（#167、#177）。

    两处各拼一遍理由就会各自漂——一处说命中、另一处说手动。这里走的是菜单那一条出口，
    断言与节点打印的格式一致。
    """
    state = {"prop_image_prompts": [{
        "name": "橘子汽水",
        "positive_prompt": "提示词",
        "image_model": {"model": "moss", "source": "auto", "keyword": "包装",
                        "evidence": "瓶身包装印着品牌色块"},
    }]}
    item = QueueItem(picture=1, kind="prop", name="橘子汽水")

    line = render_asset_model_line(state, item)

    assert line.startswith("[生图模型] 橘子汽水：命中『包装』→ MOSS-Image")
    assert "（来源：瓶身包装印着品牌色块）" in line


def test_asset_model_line_tolerates_a_record_without_evidence():
    """旧会话的记录没有 evidence：照常展示模型与理由，不许因为缺字段报错。"""
    state = {"prop_image_prompts": [{
        "name": "橘子汽水",
        "positive_prompt": "提示词",
        "image_model": {"model": "moss", "source": "auto", "keyword": "包装"},
    }]}
    item = QueueItem(picture=1, kind="prop", name="橘子汽水")

    line = render_asset_model_line(state, item)

    assert line.startswith("[生图模型] 橘子汽水：命中『包装』")
    assert "来源" not in line


def test_tailframe_document_prints_the_frame_model(tmp_path, capsys):
    """#150 AC2：尾帧终端要打印本次按哪一档出词——用户据此知道该开哪个工作流。"""
    from minimax_h3_prompt.ui.wizard_display import print_tailframe_document

    text = ("### 变换指令\n以这张输入图为参考：镜头落在中景。\n\n"
            "### 保留项声明\n保持输入图中的人物身份不变。\n\n"
            "### 锁死清单\n- 保持输入图中的人物身份不变\n")
    print_tailframe_document(
        text, index=3, path=tmp_path / "tailframe-03.md", model="qwen")

    out = capsys.readouterr().out
    assert "Qwen-Image 2.1" in out and "qwen" in out, "终端没打印出图档名"
    assert "本次按" in out and "档出词" in out


def test_firstframe_prompt_prints_the_frame_model(tmp_path, capsys):
    """#144 C / US18：首帧终端也要打印本次按哪一档出词（尾帧同款）。"""
    from minimax_h3_prompt.ui.wizard_display import print_firstframe_prompt

    print_firstframe_prompt("A rider under the awning.", index=2,
                            path=tmp_path / "firstframe-02.md", model="moss")

    out = capsys.readouterr().out
    assert "本次按 MOSS-Image（moss）档出词" in out, "首帧终端没打印出图档名"


def test_firstframe_prompt_reads_the_recorded_model_from_the_sidecar(tmp_path, capsys):
    """调用方没给档名时读 sidecar 的**写时**档，不现读配置（#150 同款口径）。"""
    from minimax_h3_prompt.director.frame_meta import write_frame_meta
    from minimax_h3_prompt.ui.wizard_display import print_firstframe_prompt

    path = tmp_path / "firstframe-02.md"
    path.write_text("A rider under the awning.", encoding="utf-8")
    write_frame_meta(path, {"model": "qwen"})

    print_firstframe_prompt("A rider under the awning.", index=2, path=path)

    out = capsys.readouterr().out
    assert "本次按 Qwen-Image 2.1（qwen）档出词" in out

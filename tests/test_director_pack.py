"""导演包构建与装配校验（issue #104）。

缝是 ``prepare_director_pack``（设计 §3.4「准备口形状」）：纯函数 + 临时目录，返回
``(job, report)``。测试只断言外部可观察行为——包内文件与 JSON 字段、出站文本、
拒绝路径的报错内容；不跑 ComfyUI、不断言私有函数的调用顺序。

口径来源（写断言前先读，别自造）：
- ``docs/design/2026-10-29-导演台路线实现设计.md`` §3.4 / §3.4.1（两套坐标、八条约束、
  素材来源裁定）；
- ``docs/research/2026-10-29-director-插件原生格式.md``「四、对项目侧实现的硬约束清单」
  打包 1–14 条（每条对应插件 ``director/pack.py`` 的 file:line，HEAD ``4c6245e``）。
"""
from __future__ import annotations

import json
import re
import zipfile
from pathlib import Path

import pytest

from minimax_h3_prompt.director.planning import parse_director_plan
from minimax_h3_prompt.director import prompts as director_prompts

# 30 秒五段（1 开场 + 4 续接）：设计 §3.2 的算例——时间线 671 帧（Σ 声明）/
# 成片预计 719 帧（Σ 导出）。这两个数**手写在断言里**，故意不重算：它们是 ADR 0025
# 的定案值，重算一遍等于自证。
_PLAN_JSON = """{"segments": [
  {"start_s": 0, "end_s": 6, "type": "opening", "summary": "a", "end_hook": "h1"},
  {"start_s": 6, "end_s": 12, "type": "continue", "summary": "b", "end_hook": "h2"},
  {"start_s": 12, "end_s": 18, "type": "continue", "summary": "c", "end_hook": "h3"},
  {"start_s": 18, "end_s": 24, "type": "continue", "summary": "d", "end_hook": "h4"},
  {"start_s": 24, "end_s": 30, "type": "continue", "summary": "e", "end_hook": "h5"}
]}"""

_TIMELINE_TOTAL = 671   # Σ 声明帧（导演台时间线显示的长度）
_FILM_TOTAL = 719       # Σ 导出帧（成片预计长度）


def _plan(mode: str = "t2v"):
    return parse_director_plan(_PLAN_JSON, 30.0, mode=mode)


def _write_prompts(session_dir: Path, plan, texts: dict[int, str] | None = None) -> None:
    """把段提示词直接写盘：打包器只负责读它，不负责生它（写段属 #103）。"""
    for segment in plan.segments:
        path = director_prompts.segment_path(session_dir, segment.index)
        path.parent.mkdir(parents=True, exist_ok=True)
        text = (texts or {}).get(segment.index, f"Segment {segment.index} of 5. 正文。")
        path.write_text(text + "\n", encoding="utf-8")


def _session(tmp_path: Path, plan, texts: dict[int, str] | None = None) -> Path:
    session_dir = tmp_path / "GEN001"
    _write_prompts(session_dir, plan, texts)
    return session_dir


def _settings(**overrides):
    from minimax_h3_prompt.director.pack import DirectorPackSettings

    values = dict(width=736, height=1280, ref_max_size=864, ref_image_size="max")
    values.update(overrides)
    return DirectorPackSettings(**values)


def _zip_names(job) -> list[str]:
    with zipfile.ZipFile(job.zip_path) as archive:
        return archive.namelist()


def _pack_timeline(job) -> dict:
    return json.loads((job.pack_dir / "timeline.json").read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# 包结构：解包目录 + zip、时间线用声明坐标、两个总数都在出站里
# ---------------------------------------------------------------------------

def test_prepare_t2v_pack_writes_the_importable_layout(tmp_path):
    """最小生成类包：目录布局、pack.json 与 timeline.json 的必填字段一次到位。

    导入侧只认这四处（插件 ``pack.py:1019-1067``）：``pack.json`` 的格式串与版本、
    ``timeline.json`` 本体、``shared_params/shared_params.json``、``asset_groups``。
    断言里的 671/719 来自设计 §3.2 的算例，不是对实现的重算。
    """
    from minimax_h3_prompt.director.pack import prepare_director_pack

    plan = _plan("t2v")
    job, report = prepare_director_pack(_session(tmp_path, plan), plan, settings=_settings())

    assert job.pack_dir == tmp_path / "GEN001" / "director" / "pack"
    assert job.zip_path == tmp_path / "GEN001" / "director" / "initial.mmxpack.zip"
    names = _zip_names(job)
    for required in ("pack.json", "timeline.json", "shared_params/shared_params.json"):
        assert required in names, f"包内缺 {required}"
    assert job.zip_path.is_file()

    pack_json = json.loads((job.pack_dir / "pack.json").read_text(encoding="utf-8"))
    assert pack_json["format"] == "minimax-h3-director-pack"
    assert pack_json["formatVersion"] == 1
    assert pack_json["taskType"] == "t2v"
    # 插件按 widgets.task_type > pack.taskType > timeline.global.taskType 取值，
    # 三处必须同一个模式（``pack.py:1056-1061``；研究笔记硬约束 14）。
    assert pack_json["widgets"]["task_type"] == "t2v"

    timeline = _pack_timeline(job)
    assert timeline["timelineMode"] == "prompt_batch"
    assert timeline["editMode"] == "segment"
    assert timeline["frameRate"] == 24
    assert timeline["totalFrames"] == _TIMELINE_TOTAL
    assert timeline["global"]["taskType"] == "t2v"
    assert [segment["start"] for segment in timeline["segments"]] == [0, 107, 248, 389, 530]
    assert [segment["frameCount"] for segment in timeline["segments"]] == [107, 141, 141, 141, 141]
    assert [segment["length"] for segment in timeline["segments"]] == [107, 141, 141, 141, 141]
    # durationSec 与声明帧自洽且**优先整秒**（ADR 0025；``frames.duration_sec_for_declared``
    # 是唯一换算口，这里按定案值核：107↔4.0、141↔5.8）。
    assert [segment["durationSec"] for segment in timeline["segments"]] == [4.0, 5.8, 5.8, 5.8, 5.8]
    # 段提示词正文逐字进包（#108 以 segments[].prompt 做比对源）。
    assert timeline["segments"][1]["prompt"].startswith("Segment 2 of 5.")
    # 段 id 必须唯一且 ASCII：UI 新建段用 uid()，我们的 id 是 #108 的 join 键。
    ids = [segment["id"] for segment in timeline["segments"]]
    assert len(set(ids)) == len(ids) and all(re.fullmatch(r"[A-Za-z0-9_.-]+", i) for i in ids)


def test_output_block_carries_the_continuity_contract(tmp_path):
    """引导配置只能挂 ``output`` 下（UI 的 stripTimelineContinuityRootFields 会删根级）。

    五条必填（设计 §3.4 约束 7）+ 生成类必须 ``mode="fixed"``（研究笔记硬约束 6）。
    """
    from minimax_h3_prompt.director.pack import prepare_director_pack

    plan = _plan("t2v")
    job, _ = prepare_director_pack(_session(tmp_path, plan), plan, settings=_settings())
    timeline = _pack_timeline(job)
    output = timeline["output"]
    assert output["continuityEnabled"] is True
    assert output["continuityOverlapFrames"] == 22
    assert output["continuityKeepTail"] is True
    assert output["exportMode"] == "all"
    assert output["continuityMode"] == "guide"
    assert output["mode"] == "fixed"
    assert output["width"] == 736 and output["height"] == 1280
    assert output["refImageSize"] == "max"
    # 根级不许出现同名键：最终包会被 UI 删掉，等于配置丢了（``minimax_timeline.js:271-277``）。
    for key in ("continuityEnabled", "continuityOverlapFrames", "continuityKeepTail"):
        assert key not in timeline


def test_common_reference_is_off_in_both_places(tmp_path):
    """约束 2：公共参考关闭，``shared_params.json`` 与 ``timeline.global`` 两处都要假。

    为真时全局词会拼到每段词前、参考图按槽位合并（``plan.py:202-212``），段内
    子集与 `<Picture N>` 的编号就对不上了。
    """
    from minimax_h3_prompt.director.pack import prepare_director_pack

    plan = _plan("t2v")
    job, _ = prepare_director_pack(_session(tmp_path, plan), plan, settings=_settings())
    shared = json.loads(
        (job.pack_dir / "shared_params" / "shared_params.json").read_text(encoding="utf-8"))
    assert shared["commonEnabled"] is False
    assert _pack_timeline(job)["global"]["commonEnabled"] is False


def test_zip_entries_are_ascii_by_the_plugin_regex(tmp_path):
    """约束 1 的成品侧：导入侧逐条判 ``^[A-Za-z0-9_./]+$``（``pack.py:35``、``:673``）。

    白名单字面照抄插件的正则**是故意的**：这一条要独立于实现里的常量，否则常量
    一放宽，守卫跟着放宽，等于没测（本仓「永不为红＝没有」）。
    """
    from minimax_h3_prompt.director.pack import prepare_director_pack

    plan = _plan("t2v")
    job, _ = prepare_director_pack(_session(tmp_path, plan), plan, settings=_settings())
    plugin_re = re.compile(r"^[A-Za-z0-9_./]+$")
    for name in _zip_names(job):
        assert plugin_re.fullmatch(name), f"包内路径不是 ASCII：{name}"
        assert ".." not in name.split("/") and not name.startswith("/")


def test_report_lists_both_totals_constraints_and_the_handoff_line(tmp_path):
    """出站清单：两个总帧数、逐段段类型/给图/包内路径、八条约束逐条结论、包路径。

    只报一个总帧数会让用户以为导演台里显示的 27.958s 是错的（设计 §3.2「两套坐标」）；
    末行必须说明逐段执行由 #105 跟进，不让用户以为现在就能跑。
    """
    from minimax_h3_prompt.director.pack import prepare_director_pack

    plan = _plan("t2v")
    job, report = prepare_director_pack(_session(tmp_path, plan), plan, settings=_settings())
    assert "时间线总帧" in report and str(_TIMELINE_TOTAL) in report
    assert "成片预计帧" in report and str(_FILM_TOTAL) in report
    assert "段1" in report and "开场" in report and "段5" in report and "续接" in report
    for number in range(1, 9):
        assert re.search(rf"^\s*{number}\. ", report, re.MULTILINE), f"缺约束 {number} 的结论行"
    assert str(job.pack_dir) in report and str(job.zip_path) in report
    assert report.rstrip().splitlines()[-1].strip().endswith("#105 跟进")


# ---------------------------------------------------------------------------
# 素材来源（设计 §3.4.1，2026-10-29 用户裁定）
# ---------------------------------------------------------------------------

def _picture_record(name: str, number: int, path: str):
    from minimax_h3_prompt.asset_ledger import AssetRecord

    return AssetRecord(name=name, kind="picture", number=number, path=path,
                       sha256="0" * 64)


def _write_ledger(session_dir: Path, records, *, library_root: str = "") -> None:
    """写一份真实台账文件（不放 fixture 假对象：读取口必须走生产路径）。"""
    from minimax_h3_prompt.asset_ledger import AssetLedger, save_asset_ledger

    save_asset_ledger(session_dir, AssetLedger(entries=tuple(records),
                                               library_root=library_root))


def _write_asset(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x89PNG\r\n\x1a\nfake")
    return path


def test_r2v_mounts_every_registered_picture_on_every_segment(tmp_path):
    """r2v 每段挂**全量**已登记图：全局登记号 = 段内局部槽号，段词一字不改。

    用户裁定（设计 §3.4.1）：走 ``segment_refs`` 子集就必须在打包期重编
    ``<Picture N>``，等于新增第二条改写口径。这里钉住「全量 + index 从 0 连续」。
    素材名是中文（只留在台账与提示词里），包内名必须是 ASCII 的 ``PictureK.png``。
    """
    from minimax_h3_prompt.director.pack import prepare_director_pack

    plan = _plan("r2v")
    session_dir = _session(tmp_path, plan, texts={
        1: "subject_definitions: 阿岚，<Picture 1>；灯，<Picture 2>；街，<Picture 3>。",
        2: "continue <Picture 3>", 3: "continue", 4: "continue", 5: "continue",
    })
    records = []
    for number, name in enumerate(("人物 阿岚", "道具 旧灯", "场景 雨街"), 1):
        source = _write_asset(session_dir / "assets" / f"{name}.png")
        records.append(_picture_record(name, number, f"assets/{name}.png"))
    _write_ledger(session_dir, records)

    job, report = prepare_director_pack(session_dir, plan, settings=_settings())

    timeline = _pack_timeline(job)
    for position, segment in enumerate(timeline["segments"], 1):
        assert [ref["index"] for ref in segment["refs"]] == [0, 1, 2], "每段挂全量"
        assert [ref["imageFile"] for ref in segment["refs"]] == [
            f"asset_groups/{position:02d}/Picture1.png",
            f"asset_groups/{position:02d}/Picture2.png",
            f"asset_groups/{position:02d}/Picture3.png",
        ]
        # 必须写 imageFile（只写 fileName 的导入时不会被改写，运行时找不到图）。
        assert all(ref["imageFile"] and ref["type"] == "input" for ref in segment["refs"])
    for position in range(1, 6):
        for slot in (1, 2, 3):
            assert (job.pack_dir / "asset_groups" / f"{position:02d}"
                    / f"Picture{slot}.png").is_file()
    assert "asset_groups/05/Picture3.png" in report


def test_r2v_resolves_library_root_layout(tmp_path):
    """新布局台账（``library_root``）也要走 ``resolve_asset_path`` 这一个解析口。"""
    from minimax_h3_prompt.director.pack import prepare_director_pack

    plan = _plan("r2v")
    session_dir = _session(tmp_path, plan, texts={i: "<Picture 1>" for i in range(1, 6)})
    library = tmp_path / "素材库"
    _write_asset(library / "雨夜-abc123" / "Picture1-人物.png")
    _write_ledger(session_dir, [_picture_record("人物", 1, "雨夜-abc123/Picture1-人物.png")],
                  library_root=str(library))

    job, _ = prepare_director_pack(session_dir, plan, settings=_settings())
    assert (job.pack_dir / "asset_groups" / "01" / "Picture1.png").is_file()


def test_r2v_refuses_without_registered_pictures(tmp_path):
    """台账没登记图 → 拒绝打包并点名：段词里的 `<Picture N>` 会全部悬空。"""
    from minimax_h3_prompt.director.pack import prepare_director_pack

    plan = _plan("r2v")
    session_dir = _session(tmp_path, plan, texts={i: "<Picture 1>" for i in range(1, 6)})
    with pytest.raises(ValueError, match="素材台账"):
        prepare_director_pack(session_dir, plan, settings=_settings())
    assert not (session_dir / "director" / "pack").exists(), "校验没过就不许落盘"


def test_r2v_refuses_registered_picture_missing_on_disk(tmp_path):
    """登记了但盘上没有正本 → 点名是哪一张（标签 + 语义名 + 路径）。"""
    from minimax_h3_prompt.director.pack import prepare_director_pack

    plan = _plan("r2v")
    session_dir = _session(tmp_path, plan, texts={i: "<Picture 1>" for i in range(1, 6)})
    _write_ledger(session_dir, [_picture_record("人物 阿岚", 1, "assets/不存在.png")])
    with pytest.raises(ValueError, match="Picture 1"):
        prepare_director_pack(session_dir, plan, settings=_settings())


def test_r2v_refuses_more_than_the_slot_limit(tmp_path):
    """约束 5 的素材侧：登记图超过槽位上限（9）就拒绝，不静默截断。

    变异检验：把 ``ref_capacity.MAX_PICTURES`` 改成 12，这条立刻变红（10 张会被放行）。
    """
    from minimax_h3_prompt.director.pack import prepare_director_pack

    plan = _plan("r2v")
    session_dir = _session(tmp_path, plan, texts={i: "<Picture 1>" for i in range(1, 6)})
    records = []
    for number in range(1, 11):
        _write_asset(session_dir / "assets" / f"图{number}.png")
        records.append(_picture_record(f"图{number}", number, f"assets/图{number}.png"))
    _write_ledger(session_dir, records)
    with pytest.raises(ValueError, match="9"):
        prepare_director_pack(session_dir, plan, settings=_settings())


def test_r2v_refuses_a_prompt_referencing_an_unregistered_slot(tmp_path):
    """段词引用 `<Picture 5>` 但只登记了 3 张 → 点名段号与槽号。

    这类悬空引用插件不会报错（它会按槽位顺序补前缀），跑出来是「主体换了张图」。
    """
    from minimax_h3_prompt.director.pack import prepare_director_pack

    plan = _plan("r2v")
    session_dir = _session(tmp_path, plan, texts={
        1: "<Picture 1> 与 <Picture 5>", 2: "<Picture 1>", 3: "<Picture 1>",
        4: "<Picture 1>", 5: "<Picture 1>",
    })
    records = []
    for number in range(1, 4):
        _write_asset(session_dir / "assets" / f"图{number}.png")
        records.append(_picture_record(f"图{number}", number, f"assets/图{number}.png"))
    _write_ledger(session_dir, records)
    with pytest.raises(ValueError, match="第 1 段.*Picture 5"):
        prepare_director_pack(session_dir, plan, settings=_settings())


def test_i2v_opening_requires_its_start_image(tmp_path):
    """i2v 开场缺 ``director/assets/01/start.png`` → 拒绝并点名段号。

    漏给首图时插件走「纯文字」路径（研究笔记 §7.2），段里的首帧约束静默失效。
    """
    from minimax_h3_prompt.director.pack import prepare_director_pack

    plan = _plan("i2v")
    session_dir = _session(tmp_path, plan)
    with pytest.raises(ValueError, match="第 1 段.*start"):
        prepare_director_pack(session_dir, plan, settings=_settings())


def test_i2v_writes_gen_image_and_image_file_for_the_opening_only(tmp_path):
    """i2v：首图进 ``genImage`` + 段级 ``imageFile``（**不写 startImage**）。

    写两处不同内容会静默丢一张：导出时都落 ``asset_groups/NN/start.<ext>``
    （``pack.py:237-242``）。非开场段不带媒体——它们的终点画面由 #107 出词、用户在
    UI 里挂（设计 §3.4.1 第 3 条）。
    """
    from minimax_h3_prompt.director.pack import prepare_director_pack

    plan = _plan("i2v")
    session_dir = _session(tmp_path, plan)
    _write_asset(session_dir / "director" / "assets" / "01" / "start.png")

    job, _ = prepare_director_pack(session_dir, plan, settings=_settings())
    timeline = _pack_timeline(job)
    opening = timeline["segments"][0]
    assert opening["genImage"]["imageFile"] == "asset_groups/01/start.png"
    assert opening["genImage"]["fileName"] == "start.png"
    assert opening["imageFile"] == "asset_groups/01/start.png"
    assert opening["startImage"] is None and opening["endImage"] is None
    assert (job.pack_dir / "asset_groups" / "01" / "start.png").is_file()
    for segment in timeline["segments"][1:]:
        assert segment["refs"] == []
        assert segment["genImage"]["imageFile"] == "" and segment["imageFile"] == ""
        assert segment["startImage"] is None and segment["endImage"] is None


def test_fl2v_writes_shots_and_segments_with_first_and_last_fields(tmp_path):
    """fl2v：``timelineMode=fl2v``、``shots`` 与 ``segments`` 两处同源，无 ``startImage``。

    UI 以 ``shots`` 为准、后端只读 ``segments``（研究笔记 §3.2）：两处说法不一致时
    导入后画面与提示词各按一份走。段级用 ``genImage``（首帧）+ ``endImage``（尾帧）。
    """
    from minimax_h3_prompt.director.pack import prepare_director_pack

    plan = _plan("fl2v")
    session_dir = _session(tmp_path, plan)
    _write_asset(session_dir / "director" / "assets" / "01" / "start.png")
    _write_asset(session_dir / "director" / "assets" / "01" / "end.png")

    job, _ = prepare_director_pack(session_dir, plan, settings=_settings())
    timeline = _pack_timeline(job)
    assert timeline["timelineMode"] == "fl2v"
    assert timeline["editMode"] == "segment"
    assert len(timeline["shots"]) == len(timeline["segments"]) == 5
    opening = timeline["segments"][0]
    assert opening["genImage"]["imageFile"] == "asset_groups/01/start.png"
    assert opening["endImage"]["imageFile"] == "asset_groups/01/end.png"
    assert opening["startImage"] is None, "fl2v 段级没有 startImage 这个槽（会与 genImage 撞名）"
    assert opening["negativePrompt"] == "bad video"
    shot = timeline["shots"][0]
    assert shot["startImage"]["imageFile"] == "asset_groups/01/start.png"
    assert shot["endImage"]["imageFile"] == "asset_groups/01/end.png"
    assert shot["durationSec"] == 4.0 and shot["frameCount"] == 107


def test_fl2v_end_image_is_optional(tmp_path):
    """fl2v 的尾帧可选（官方允许只给首帧）：只给 start.png 也要能打包。"""
    from minimax_h3_prompt.director.pack import prepare_director_pack

    plan = _plan("fl2v")
    session_dir = _session(tmp_path, plan)
    _write_asset(session_dir / "director" / "assets" / "01" / "start.png")
    job, _ = prepare_director_pack(session_dir, plan, settings=_settings())
    opening = _pack_timeline(job)["segments"][0]
    assert opening["endImage"] is None
    assert opening["genImage"]["imageFile"] == "asset_groups/01/start.png"


def test_repacking_replaces_the_pack_dir_so_stale_media_does_not_survive(tmp_path):
    """重打一次包必须清掉上一轮的媒体：残留的 Picture3 会被导入侧一起拷进 input。

    复用同一个 GEN 目录时，包目录是整份覆盖的（``_write_pack`` 先删再写）；这条钉住
    它——「旧图还在、新包没提它」正是 #58 复审里「复用目录吞掉账」的镜像形态。
    """
    from minimax_h3_prompt.director.pack import prepare_director_pack

    plan = _plan("r2v")
    session_dir = _session(tmp_path, plan, texts={i: "<Picture 1>" for i in range(1, 6)})
    for number in range(1, 4):
        _write_asset(session_dir / "assets" / f"图{number}.png")
    _write_ledger(session_dir, [
        _picture_record(f"图{n}", n, f"assets/图{n}.png") for n in range(1, 4)])
    prepare_director_pack(session_dir, plan, settings=_settings())
    assert (session_dir / "director" / "pack" / "asset_groups" / "01" / "Picture3.png").exists()

    _write_ledger(session_dir, [
        _picture_record(f"图{n}", n, f"assets/图{n}.png") for n in range(1, 3)])
    job, _ = prepare_director_pack(session_dir, plan, settings=_settings())
    assert not (job.pack_dir / "asset_groups" / "01" / "Picture3.png").exists()
    assert all("Picture3" not in name for name in _zip_names(job))


def test_second_phase_modes_are_refused_explicitly(tmp_path):
    """v2v/rv2v/mixed 一期不许静默套用别的模式（票面硬条，二期 #109/#110）。

    段表**绕过规划器**直接构造：规划器本来就拒绝这三个模式，而打包器不能依赖
    「上游一定拦住」——它自己的模式闸门必须独立咬得住（本仓「规则在、接线不在」）。
    """
    from minimax_h3_prompt.director.frames import DirectorPlan, DirectorSegment
    from minimax_h3_prompt.director.pack import prepare_director_pack

    segment = DirectorSegment(index=1, type="opening", guided=False, start_frame=0,
                              end_frame=107, declared_frames=107, export_frames=107,
                              summary="s", end_hook="h", give_images=("first",))
    for mode in ("v2v", "rv2v", "mixed"):
        plan = DirectorPlan(mode=mode, total_frames=107, segments=(segment,))
        session_dir = _session(tmp_path / mode, plan)
        with pytest.raises(ValueError, match="#109|#110|二期"):
            prepare_director_pack(session_dir, plan, settings=_settings())


def test_first_segment_must_be_an_opening_even_outside_the_planner(tmp_path):
    """首段不是开场就拒绝：i2v/fl2v 的首图会挂到「起点在上一段终点」的段上。

    与 ``frames.validate_segment_types`` 同一份真源——打包器被直接调用时（续跑拼表、
    外部脚本）也不能绕过这条结构性约束。
    """
    from minimax_h3_prompt.director.frames import DirectorPlan, DirectorSegment
    from minimax_h3_prompt.director.pack import prepare_director_pack

    segment = DirectorSegment(index=1, type="continue", guided=True, start_frame=0,
                              end_frame=153, declared_frames=141, export_frames=153,
                              summary="s", end_hook="h", give_images=())
    plan = DirectorPlan(mode="t2v", total_frames=153, segments=(segment,))
    session_dir = _session(tmp_path, plan)
    with pytest.raises(ValueError, match="开场|opening"):
        prepare_director_pack(session_dir, plan, settings=_settings())


# ---------------------------------------------------------------------------
# 八条装配约束：逐条拒绝路径 + 变异检验
# ---------------------------------------------------------------------------

def test_constraint_1_pack_paths_reject_non_ascii_and_escaping_names():
    """约束 1：非 ASCII / 绝对路径 / ``..`` 一律拒绝（插件导入直接拒包，``pack.py:673``）。

    变异检验：把空格或中文加进 ``ASCII_PATH_ALLOWED``，前两组断言立刻变红
    （下一条用例用程序化的方式证明这层绑定）。
    """
    from minimax_h3_prompt.director.pack import validate_pack_paths

    validate_pack_paths(["pack.json", "asset_groups/01/Picture1.png"])
    for bad in ("asset_groups/01/画面1.png", "asset_groups/01/a b.png",
                "../pack.json", "/etc/passwd", ""):
        with pytest.raises(ValueError, match="ASCII|非 ASCII"):
            validate_pack_paths([bad])


def test_constraint_1_ascii_guard_is_bound_to_the_character_set(monkeypatch):
    """ASCII 白名单**从常量派生**：放宽字符集后同一个坏路径会被放行（守卫不再咬）。

    这条是变异检验本身：只要守卫还把「空格」判成非法，放宽常量就必然让断言翻面。
    """
    from minimax_h3_prompt.director import pack

    assert pack.is_ascii_pack_path("a b.png") is False
    widened = re.compile("^[" + re.escape(pack.ASCII_PATH_ALLOWED + " ") + "]+$")
    monkeypatch.setattr(pack, "_PACK_PATH_RE", widened)
    assert pack.is_ascii_pack_path("a b.png") is True


def test_constraint_2_common_enabled_must_be_literally_false():
    """约束 2：两处都得是 ``False``；缺键（None）同样算违规。"""
    from minimax_h3_prompt.director.pack import validate_common_disabled

    validate_common_disabled({"commonEnabled": False}, {"global": {"commonEnabled": False}})
    with pytest.raises(ValueError, match="commonEnabled"):
        validate_common_disabled({"commonEnabled": True}, {"global": {"commonEnabled": False}})
    with pytest.raises(ValueError, match="commonEnabled"):
        validate_common_disabled({"commonEnabled": False}, {"global": {"commonEnabled": True}})
    with pytest.raises(ValueError, match="commonEnabled"):
        validate_common_disabled({}, {"global": {}})


def test_constraint_2_guard_bites_when_the_switch_flips(monkeypatch, tmp_path):
    """变异检验：把 ``COMMON_ENABLED`` 改成 True，打包当场报错（不是产出错包）。"""
    from minimax_h3_prompt.director import pack

    plan = _plan("t2v")
    session_dir = _session(tmp_path, plan)
    monkeypatch.setattr(pack, "COMMON_ENABLED", True)
    with pytest.raises(ValueError, match="commonEnabled"):
        pack.prepare_director_pack(session_dir, plan, settings=_settings())
    assert not (session_dir / "director" / "pack").exists()


def test_constraint_3_canvas_must_be_32_aligned():
    """约束 3：宽高都是 32 的倍数；非正整数也拒绝（bool 是 int 的子类，要单独挡）。"""
    from minimax_h3_prompt.director.pack import validate_canvas

    validate_canvas(736, 1280)
    for width, height in ((720, 1280), (736, 1279), (700, 480), (0, 480)):
        with pytest.raises(ValueError):
            validate_canvas(width, height)
    with pytest.raises(ValueError):
        validate_canvas(True, 1280)


def test_constraint_3_canvas_guard_follows_the_alignment_constant(monkeypatch):
    """变异检验：把 ``CANVAS_ALIGN`` 改成 16，720 立刻从「拒绝」变成「放行」。"""
    from minimax_h3_prompt.director import pack

    with pytest.raises(ValueError):
        pack.validate_canvas(720, 480)
    monkeypatch.setattr(pack, "CANVAS_ALIGN", 16)
    pack.validate_canvas(720, 480)  # 720 % 16 == 0


def test_constraint_4_ref_image_size_only_match_or_max():
    """约束 4：其它档在官方节点上会被静默归一成 match（``plan.py:101-107``）。"""
    from minimax_h3_prompt.director.pack import validate_ref_image_size

    for good in ("match", "max"):
        validate_ref_image_size(good)
    for bad in ("1024", "1280", "Max", "", "MAX"):
        with pytest.raises(ValueError, match="refImageSize"):
            validate_ref_image_size(bad)


def test_constraint_4_guard_follows_the_allowed_set(monkeypatch):
    """变异检验：往允许集合里加 ``1024``，原来被拒的值立刻放行。"""
    from minimax_h3_prompt.director import pack

    with pytest.raises(ValueError):
        pack.validate_ref_image_size("1024")
    monkeypatch.setattr(pack, "REF_IMAGE_SIZES", ("match", "max", "1024"))
    pack.validate_ref_image_size("1024")


def test_constraint_5_reference_capacity_and_slot_continuity():
    """约束 5：每段 ≤ 9 张、index 从 0 连续（插件把 index 直接当槽号）。"""
    from minimax_h3_prompt.director.pack import validate_reference_capacity

    nine = {"id": "seg01", "refs": [{"index": i} for i in range(9)]}
    validate_reference_capacity([nine])
    ten = {"id": "seg01", "refs": [{"index": i} for i in range(10)]}
    with pytest.raises(ValueError, match="9"):
        validate_reference_capacity([ten])
    gapped = {"id": "seg02", "refs": [{"index": 0}, {"index": 2}]}
    with pytest.raises(ValueError, match="连续"):
        validate_reference_capacity([gapped])


def test_constraint_5_guard_follows_the_slot_limit(monkeypatch):
    """变异检验：上限改成 12 后 10 张放行——上限必须从常量取，不许在判据里重抄 9。"""
    from minimax_h3_prompt.director import pack

    ten = {"id": "seg01", "refs": [{"index": i} for i in range(10)]}
    with pytest.raises(ValueError):
        pack.validate_reference_capacity([ten])
    monkeypatch.setattr(pack, "PICTURE_SLOT_LIMIT", 12)
    pack.validate_reference_capacity([ten])


def test_constraint_6_mixed_fl2v_uses_image_fields_not_refs():
    """约束 6：mixed 的 fl2v 段只认 startImage/endImage，写进 refs 会被插件剥掉。"""
    from minimax_h3_prompt.director.pack import validate_mixed_fl2v_fields

    # 非 mixed 时间线不做这条校验（独立 fl2v 有自己的时间线形态）。
    validate_mixed_fl2v_fields([{"id": "s", "taskType": "fl2v", "refs": [{"index": 0}]}],
                               task_type="fl2v")
    good = {"id": "seg01", "taskType": "fl2v",
            "startImage": {"imageFile": "asset_groups/01/start.png"}, "refs": []}
    validate_mixed_fl2v_fields([good], task_type="mixed")
    with pytest.raises(ValueError, match="refs"):
        validate_mixed_fl2v_fields(
            [{"id": "seg01", "taskType": "fl2v", "refs": [{"index": 0}]}],
            task_type="mixed")
    with pytest.raises(ValueError, match="首尾帧|一张图"):
        validate_mixed_fl2v_fields([{"id": "seg02", "taskType": "fl2v", "refs": []}],
                                   task_type="mixed")


def test_constraint_6_also_catches_gen_and_start_image_conflicts():
    """同段给出内容不同的 ``genImage`` 与 ``startImage``：导出时后一张会静默消失。"""
    from minimax_h3_prompt.director.pack import validate_segment_media_fields

    validate_segment_media_fields([{
        "id": "seg01",
        "genImage": {"imageFile": "asset_groups/01/start.png"},
        "startImage": {"imageFile": "asset_groups/01/start.png"},
    }])
    with pytest.raises(ValueError, match="startImage"):
        validate_segment_media_fields([{
            "id": "seg01",
            "genImage": {"imageFile": "asset_groups/01/start.png"},
            "startImage": {"imageFile": "asset_groups/01/other.png"},
        }])


def test_constraint_7_output_requires_every_continuity_setting():
    """约束 7：五条配置缺一不可，且生成类 ``output.mode`` 必须是 fixed。"""
    from minimax_h3_prompt.director.pack import REQUIRED_OUTPUT, validate_output_config

    good = {**REQUIRED_OUTPUT, "continuityMode": "guide", "mode": "fixed"}
    validate_output_config(good)
    for key in REQUIRED_OUTPUT:
        broken = dict(good)
        broken.pop(key)
        with pytest.raises(ValueError, match="output"):
            validate_output_config(broken)
    with pytest.raises(ValueError, match="continuityMode"):
        validate_output_config({**good, "continuityMode": ""})
    with pytest.raises(ValueError, match="fixed"):
        validate_output_config({**good, "mode": "long_edge"})


def test_constraint_7_guard_follows_the_required_map(monkeypatch):
    """变异检验：从必备表里删掉 ``continuityKeepTail``，缺它就不再报错。"""
    from minimax_h3_prompt.director import pack

    broken = {"continuityEnabled": True, "continuityOverlapFrames": 22,
              "exportMode": "all", "continuityMode": "guide", "mode": "fixed"}
    with pytest.raises(ValueError):
        pack.validate_output_config(broken)
    monkeypatch.setattr(pack, "REQUIRED_OUTPUT", {
        "continuityEnabled": True,
        "continuityOverlapFrames": 22,
        "exportMode": "all",
    })
    pack.validate_output_config(broken)


def test_constraint_8_mixed_segment_tasks_reject_empty_and_unknown():
    """约束 8：空值/未知值报错——插件会把它们**静默**改成 t2v（``gen_timeline.py:156-171``）。"""
    from minimax_h3_prompt.director.pack import validate_mixed_segment_tasks

    validate_mixed_segment_tasks(
        [{"id": f"s{i}", "taskType": value} for i, value in
         enumerate(("t2v", "i2v", "fl2v", "r2v"))],
        task_type="mixed")
    for bad in ("", None, "v2v", "rv2v", "mixed", "T2V"):
        with pytest.raises(ValueError, match="mixed"):
            validate_mixed_segment_tasks([{"id": "seg01", "taskType": bad}],
                                         task_type="mixed")
    # 非 mixed 时间线的段级任务类型插件根本不读，不该被这条拦。
    validate_mixed_segment_tasks([{"id": "seg01", "taskType": "v2v"}], task_type="t2v")


def test_constraint_8_guard_follows_the_legal_value_set(monkeypatch):
    """变异检验：把 ``v2v`` 加进合法集合，它立刻从「报错」变成「放行」。"""
    from minimax_h3_prompt.director import pack

    with pytest.raises(ValueError):
        pack.validate_mixed_segment_tasks([{"id": "seg01", "taskType": "v2v"}],
                                          task_type="mixed")
    monkeypatch.setattr(pack, "MIXED_SEGMENT_TASKS",
                        frozenset({"t2v", "i2v", "fl2v", "r2v", "v2v"}))
    pack.validate_mixed_segment_tasks([{"id": "seg01", "taskType": "v2v"}],
                                      task_type="mixed")


def test_pack_settings_are_checked_before_anything_is_written(tmp_path):
    """画布/参考图尺寸不合格时**一个文件都不许写**（票面：不得先写包后校验）。"""
    from minimax_h3_prompt.director.pack import prepare_director_pack

    plan = _plan("t2v")
    session_dir = _session(tmp_path, plan)
    with pytest.raises(ValueError, match="32"):
        prepare_director_pack(session_dir, plan, settings=_settings(width=720))
    with pytest.raises(ValueError, match="refImageSize"):
        prepare_director_pack(session_dir, plan, settings=_settings(ref_image_size="1024"))
    assert not (session_dir / "director" / "pack").exists()


def test_missing_segment_prompt_stops_the_pack_by_number(tmp_path):
    """段提示词文件缺失 → 点名段号（半份段表打出来的包会缺段内容）。"""
    from minimax_h3_prompt.director.pack import prepare_director_pack

    plan = _plan("t2v")
    session_dir = _session(tmp_path, plan)
    director_prompts.segment_path(session_dir, 4).unlink()
    with pytest.raises(ValueError, match="第 4 段"):
        prepare_director_pack(session_dir, plan, settings=_settings())


def test_empty_segment_prompt_is_refused_by_number(tmp_path):
    """空段词会被插件回落成全局提示词（全局词我们也留空）——本段内容静默丢失。"""
    from minimax_h3_prompt.director.pack import prepare_director_pack

    plan = _plan("t2v")
    session_dir = _session(tmp_path, plan)
    director_prompts.segment_path(session_dir, 2).write_text("\n", encoding="utf-8")
    with pytest.raises(ValueError, match="第 2 段.*空"):
        prepare_director_pack(session_dir, plan, settings=_settings())


# ---------------------------------------------------------------------------
# 配置：画布与参考图尺寸从 config/agent.yaml 的 director 节读（不写死在模块里）
# ---------------------------------------------------------------------------

def test_director_settings_are_read_from_config_and_canvas_aligned():
    """默认 736×1280、refImageSize=max、refMaxSize=864，且画布 32 对齐（设计 §4）。"""
    from minimax_h3_prompt.config import config

    settings = config.director_settings()
    assert (settings.width, settings.height) == (736, 1280)
    assert settings.width % 32 == 0 and settings.height % 32 == 0
    assert settings.ref_image_size == "max"
    assert settings.ref_max_size == 864


def test_director_settings_reject_bad_config_values(monkeypatch):
    """配置写错（非 32 对齐 / 非法 refImageSize）**读的时候就报错**，不许带到包里。"""
    from minimax_h3_prompt import config as config_module

    config = config_module.config
    monkeypatch.setitem(config.raw, "director", {"width": 720, "height": 1280})
    with pytest.raises(ValueError, match="32"):
        config.director_settings()
    monkeypatch.setitem(config.raw, "director",
                        {"width": 736, "height": 1280, "ref_image_size": "1024"})
    with pytest.raises(ValueError, match="refImageSize"):
        config.director_settings()


# ---------------------------------------------------------------------------
# Brief 新字段：会话开头问一次，落 Brief，重启后读回（设计 §4）
# ---------------------------------------------------------------------------

def _director_brief(**overrides):
    from minimax_h3_prompt.brief_parser import Brief

    values = dict(mode="director", variant="T2VA", director_mode="t2v",
                  duration=30.0, plot="雨夜旧信")
    values.update(overrides)
    return Brief(**values)


def test_brief_roundtrips_the_director_pack_settings(tmp_path):
    """三个字段逐个进 ``_brief_to_dict``/``_brief_from_dict``：漏一个＝重启后丢答案。"""
    from minimax_h3_prompt.session_store import load_session, save_session

    brief = _director_brief(director_node_id="42", director_cache_name="雨夜",
                            director_continuity_mode="continue")
    save_session(tmp_path / "GEN001", brief, {})
    loaded = load_session(tmp_path / "GEN001")
    assert loaded is not None
    assert loaded.brief.director_node_id == "42"
    assert loaded.brief.director_cache_name == "雨夜"
    assert loaded.brief.director_continuity_mode == "continue"


def test_non_director_route_may_not_carry_director_pack_settings(tmp_path):
    """非导演台路线带着这些值 → 读回报错（``_director_mode_from_dict`` 的严格口径）。"""
    from minimax_h3_prompt.session_store import load_session, save_session

    save_session(tmp_path / "GEN002", _director_brief(mode="base", director_mode="",
                                                      director_node_id="42"), {})
    with pytest.raises(ValueError, match="director_node_id"):
        load_session(tmp_path / "GEN002")


def test_bad_director_continuity_mode_is_refused_on_read(tmp_path):
    """引导模式是精确数据：``Guide`` 这类大小写变体不许静默扶正。"""
    from minimax_h3_prompt.session_store import load_session, save_session

    save_session(tmp_path / "GEN003",
                 _director_brief(director_continuity_mode="Guide"), {})
    with pytest.raises(ValueError, match="continuity"):
        load_session(tmp_path / "GEN003")


# ---------------------------------------------------------------------------
# 向导接线：问一次 → 写段之后打包 → 出站清单；失败大声报错并返回非零
# ---------------------------------------------------------------------------

_SINGLE_BODY = """[Shot 1] A woman in a grey trench coat steps under the awning.
[Shot 2] The camera pushes in until her face fills the frame.
"""


class _FlowLLM:
    """规划与写段共用一个模型（真实流程也是同一个网关，按角色名分派）。"""

    def __init__(self, mode: str):
        self.plan = parse_director_plan(_PLAN_JSON, 30.0, mode=mode)
        self.requests: list[str] = []

    def invoke(self, request, config=None):
        self.requests.append(request)
        if "分段规划师" in request:
            return _PLAN_JSON
        return _SINGLE_BODY


def _wizard_session(tmp_path: Path, mode: str = "t2v", **brief_overrides):
    from minimax_h3_prompt.session_store import SessionState

    return SessionState(
        directory=tmp_path / "GEN001",
        brief=_director_brief(director_mode=mode, **brief_overrides),
        stage_state={"shot_table": "分镜表：[Shot 1] 雨夜便利店……"},
        status="awaiting_frames",
    )


def _stub_llm(monkeypatch, mode: str) -> "_FlowLLM":
    from minimax_h3_prompt import model_factory

    llm = _FlowLLM(mode)
    monkeypatch.setattr(model_factory, "build_chat_model", lambda: llm)
    return llm


def test_wizard_asks_pack_settings_once_and_saves_them(tmp_path, monkeypatch, capsys):
    """进导演台流程时把节点 id / 缓存名 / 引导模式问一次并落 Brief。

    节点 id 必填（缓存目录名是「缓存名_节点id」，缺了就找不到缓存）；空输入重问，
    不许静默留空。三个答案必须进会话文件——重启续接不该再问一遍。
    """
    from minimax_h3_prompt.session_store import load_session
    from minimax_h3_prompt.ui import wizard

    _stub_llm(monkeypatch, "t2v")
    session = _wizard_session(tmp_path)
    answers = iter(["", "42", "雨夜", ""])
    asked: list[str] = []

    def fake_prompt(text: str) -> str:
        asked.append(text)
        return next(answers)

    monkeypatch.setattr(wizard, "_prompt", fake_prompt)
    monkeypatch.setattr(wizard, "_drain_stdin", lambda: None)

    assert wizard._run_director_flow(session) == 0
    assert len(asked) == 4, "节点 id 空输入必须重问一次（其余各问一次）"
    loaded = load_session(session.directory)
    assert loaded is not None
    assert loaded.brief.director_node_id == "42"
    assert loaded.brief.director_cache_name == "雨夜"
    assert loaded.brief.director_continuity_mode == "guide"  # 回车 = 官方基线
    capsys.readouterr()


def test_wizard_does_not_ask_again_when_the_answers_are_known(tmp_path, monkeypatch, capsys):
    """Brief 里已有答案就不再问：续跑反复追问同一件事是噪音，也容易被误答覆盖。"""
    from minimax_h3_prompt.ui import wizard

    _stub_llm(monkeypatch, "t2v")
    session = _wizard_session(tmp_path, director_node_id="42",
                              director_cache_name="", director_continuity_mode="guide")

    def _boom(*args, **kwargs):
        raise AssertionError("已知答案还在追问")

    monkeypatch.setattr(wizard, "_prompt", _boom)
    monkeypatch.setattr(wizard, "_drain_stdin", lambda: None)
    assert wizard._run_director_flow(session) == 0
    capsys.readouterr()


def test_wizard_flow_packs_and_prints_the_report(tmp_path, monkeypatch, capsys):
    """写段之后真的打包：出站含两个总帧数、八条约束结论与包路径，末行指向 #105。"""
    from minimax_h3_prompt.ui import wizard

    _stub_llm(monkeypatch, "t2v")
    session = _wizard_session(tmp_path, director_node_id="42",
                              director_cache_name="", director_continuity_mode="guide")
    monkeypatch.setattr(wizard, "_drain_stdin", lambda: None)

    assert wizard._run_director_flow(session) == 0
    out = capsys.readouterr().out
    assert "时间线总帧" in out and "671" in out
    assert "成片预计帧" in out and "719" in out
    assert "initial.mmxpack.zip" in out
    assert out.rstrip().splitlines()[-1].strip().endswith("#105 跟进")
    assert (session.directory / "director" / "initial.mmxpack.zip").is_file()


def test_wizard_pack_failure_is_loud_and_returns_nonzero(tmp_path, monkeypatch, capsys):
    """放包失败必须大声报错并返回非零：不许打一行警告就继续，让用户以为包好了。"""
    from minimax_h3_prompt.ui import wizard

    _stub_llm(monkeypatch, "i2v")
    session = _wizard_session(tmp_path, mode="i2v", director_node_id="42",
                              director_cache_name="", director_continuity_mode="guide")
    monkeypatch.setattr(wizard, "_drain_stdin", lambda: None)

    assert wizard._run_director_flow(session) == 1
    out = capsys.readouterr().out
    assert "[错误]" in out and "第 1 段" in out
    assert not (session.directory / "director" / "initial.mmxpack.zip").exists()

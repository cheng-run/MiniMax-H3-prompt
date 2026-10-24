"""段内参考素材子集接进产线（issue #75）：plan.json、段文件、接线卡、请求、续跑。

接缝（票面「Testing Decisions」）：**产物与请求文本**——``segments/plan.json`` 的
``assets_in_segment``、段文件里的定义块与 ``<Picture N>``、``segments/injection-card-NN.md``、
写段请求正文。全部 LLM 打桩（llm=None 走机械拼装，或只记录请求的假模型），不发网络。

背景：一次真实运行 24 张素材全喂 → Qwen3-VL 序列约 5.4 万 token → 朴素注意力
``einsum`` 要 232 GiB（2026-10-22 ``torch.OutOfMemoryError``）。段内子集是能否出片的
前置条件，不是优化（ADR 0017「代价」）。
"""
from __future__ import annotations

import json

import pytest

from minimax_h3_prompt.ref_segments import (
    load_ref_segments_for_resume,
    prepare_ref_segmentation,
)
from minimax_h3_prompt.segment_refs import SegmentAssetOverflowError
from minimax_h3_prompt.tools.h3_validator import split_ref_sections

# 30s 整片：五个镜头，切点 0/5/12/18/22/30 → 机械打包 12/10/8 三段。
# 段 1（0-12s，Shot 1-2）用图 1/2；段 2（12-22s，Shot 3-4）用图 10/1；段 3 只用图 1。
# 整片 12 张图，远超每段上限 9——正是「全片定义块不能下发给段」的场景。
_DEFS_LINES = "\n".join(
    f"<Subject {n}> is 素材{n} (the item in <Picture {n}>), with a distinct look."
    for n in range(1, 13))
_WHOLE_30S = f"""subject_definitions:
{_DEFS_LINES}

summary:
[reference generation] The target video follows 素材1 through the foyer with 素材2.

retention_analysis:
{chr(10).join(f"<Subject {n}> (appears in [Shot 1], [Shot 3]): fully_preserved - look retained." for n in range(1, 13))}
<Picture 10> (appears in [Shot 3]): fully_copy - the foyer frame is reused.

detailed_description:
The target video is in a cinematic autumn style with warm soft lighting.
[Shot 1] <Subject 1> walks in holding <Subject 2>.
[Shot 2] At 00:05.000, the shot cuts to a close-up of <Picture 2>.
[Shot 3] At 00:12.000, the camera pushes in on <Subject 10> in the foyer.
[Shot 4] At 00:18.000, <Subject 1> holds the key out to the boy.
[Shot 5] At 00:22.000, <Subject 1> bows her head.

overall_soundscape:
Soft outdoor ambience with light footsteps.

non_diegetic_music:
N/A
"""

_REGISTRY = [(n, f"素材{n}", f"素材{n}的专属生图提示词") for n in range(1, 13)]
_PATHS = {n: f"assets/picture-{n:02d}.png" for n in range(1, 13)}


def _prepare(tmp_path, llm=None):
    # 真实流程里 video-prompt.md 先落盘（续跑要从它取材），这里照同一顺序造现场。
    (tmp_path / "video-prompt.md").write_text(_WHOLE_30S, encoding="utf-8")
    return prepare_ref_segmentation(
        whole_prompt=_WHOLE_30S, total_s=30.0, out_dir=tmp_path, llm=llm,
        ref_meta=_REGISTRY, asset_paths=_PATHS)


def _plan_rows(tmp_path):
    return json.loads((tmp_path / "segments" / "plan.json").read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# plan.json：每段记「新号 → 原号」
# ---------------------------------------------------------------------------

def test_plan_json_records_assets_in_segment_with_new_to_original_mapping(tmp_path):
    """AC5：plan.json 每段带 assets_in_segment，新号→原号＋素材名。"""
    _prepare(tmp_path)
    rows = _plan_rows(tmp_path)
    first = rows[0]["assets_in_segment"]
    assert [(p["new"], p["original"], p["name"]) for p in first["pictures"]] == [
        (1, 1, "素材1"), (2, 2, "素材2")]
    second = rows[1]["assets_in_segment"]
    assert [(p["new"], p["original"]) for p in second["pictures"]] == [(1, 1), (2, 10)]


def test_segment_definitions_only_carry_this_segments_assets(tmp_path):
    """AC1/AC2：段 2 的定义块只有它用到的两张图，编号从 1 连续、没有 <Picture 10>。"""
    job = _prepare(tmp_path)
    text, issues = job.write_segment(2)
    sections = split_ref_sections(text)
    defs = sections["subject_definitions"]
    assert "<Picture 1>" in defs and "<Picture 2>" in defs
    assert "<Picture 10>" not in defs and "<Picture 11>" not in defs
    assert "<Subject 10>" in defs and "<Subject 1>" in defs
    assert "<Subject 12>" not in defs
    # 段内不再出现整片范围的定义：Subject 号保持原号（10），不是被重编号成 2
    assert "<Subject 2>" not in defs
    # 交付前校验零 error（登记/连续性都按本段范围判）
    errors = [i for i in issues if i.severity == "error"]
    assert errors == [], [(i.code, i.message) for i in errors]


def test_retention_and_summary_are_renumbered_with_the_segment(tmp_path):
    """AC2：summary / retention_analysis 里的图号跟着改成段内新号。"""
    job = _prepare(tmp_path)
    text, _issues = job.write_segment(2)
    sections = split_ref_sections(text)
    assert "<Picture 10>" not in text
    assert "<Picture 2>" in sections["retention_analysis"]
    assert "<Subject 10>" in sections["retention_analysis"]
    assert "<Subject 5>" not in sections["retention_analysis"]


def test_segment_with_more_than_nine_pictures_is_refused_with_names(tmp_path):
    """AC3：单段用到 >9 张 → 报错点名，不静默截断。"""
    many = " ".join(f"<Picture {n}>" for n in range(1, 13))
    whole = _WHOLE_30S.replace(
        "[Shot 1] <Subject 1> walks in holding <Subject 2>.", f"[Shot 1] {many}")
    with pytest.raises(SegmentAssetOverflowError) as excinfo:
        prepare_ref_segmentation(whole_prompt=whole, total_s=30.0, out_dir=tmp_path,
                                 ref_meta=_REGISTRY, asset_paths=_PATHS)
    message = str(excinfo.value)
    assert "素材12" in message and "第 1" in message


def test_the_limit_comes_from_the_workflow_config(tmp_path, monkeypatch):
    """上限读 ``config/workflow.yaml``（改配置即改边界）：写 1 张时两图的段当场被拒。"""
    from minimax_h3_prompt import workflow_config

    config = tmp_path / "workflow.yaml"
    config.write_text("refs_per_segment: 1\n", encoding="utf-8")
    monkeypatch.setattr(workflow_config, "WORKFLOW_CONFIG_PATH", config)
    with pytest.raises(SegmentAssetOverflowError):
        _prepare(tmp_path)


# ---------------------------------------------------------------------------
# 写段请求：只下发本段定义块
# ---------------------------------------------------------------------------

class _RecordingLLM:
    """记录请求、回一份合法的六段式（定义块由组装器覆盖，回什么都行）。"""

    def __init__(self):
        self.requests: list[str] = []

    def invoke(self, request, config=None):
        self.requests.append(str(request))
        return (
            "subject_definitions:\n<Subject 1> placeholder.\n\n"
            "summary:\n[reference generation] A short segment.\n\n"
            "retention_analysis:\n<Subject 1>: fully_preserved - ok.\n\n"
            "detailed_description:\nThe target video is in a cinematic style.\n"
            "[Shot 1] A medium shot frames the foyer as she enters.\n\n"
            "overall_soundscape:\nQuiet room tone.\n\n"
            "non_diegetic_music:\nN/A"
        )


def _stub_ref_plans():
    """规划式产物（本文件不测规划本身，只测写段分支；边界与机械打包一致）。"""
    from minimax_h3_prompt.segment_planner import SegmentPlan

    return [
        SegmentPlan(index=0, start_s=0, end_s=12, shots_in_segment=(1, 2),
                    summary="进门", end_hook="the girl enters", start_state="the gate opens"),
        SegmentPlan(index=1, start_s=12, end_s=22, shots_in_segment=(3, 4),
                    summary="门厅", end_hook="the key is offered",
                    start_state="the girl is in the foyer"),
        SegmentPlan(index=2, start_s=22, end_s=30, shots_in_segment=(5,),
                    summary="收尾", end_hook="she bows", start_state="the key is offered"),
    ]


def test_v2_request_carries_only_the_segment_definition_block(tmp_path, monkeypatch):
    """AC 变异点：把整片定义塞回请求 → 本条必须红（请求里出现 <Picture 10>）。"""
    from minimax_h3_prompt import ref_segments

    monkeypatch.setattr(ref_segments, "plan_segments", lambda *a, **k: _stub_ref_plans())
    llm = _RecordingLLM()
    job = _prepare(tmp_path, llm=llm)
    assert job.used_fallback is False
    job.write_segment(1)
    request = llm.requests[-1]
    assert "<Picture 1>" in request and "<Picture 2>" in request
    assert "<Picture 10>" not in request and "<Subject 10>" not in request
    assert "本段参考素材" in request
    assert "全片共享同一批参考素材" not in request


def _defs_block(request: str) -> str:
    """请求里的两段「定义块」（v2 与回退式同形）——段内作用域就判这一块。"""
    start = request.index("subject_definitions 定义块")
    end = request.index("完整提示词") if "完整提示词" in request else len(request)
    return request[start:end]


def test_fallback_rewrite_request_is_scoped_too(tmp_path):
    """规划失败回退式同样只看到本段定义块（两条写段路径同改同测）。

    回退式的「完整提示词」取材块本来就是整片正文（只准取本段时间窗内的内容），
    所以判据落在**定义块**上：整片 12 张定义不能出现在那里。
    """
    job = _prepare(tmp_path, llm=None)   # llm=None → used_fallback=True
    llm = _RecordingLLM()
    job.llm = llm
    job.write_segment(1)
    block = _defs_block(llm.requests[-1])
    assert "<Picture 1>" in block and "<Picture 2>" in block
    assert "<Picture 10>" not in block and "<Subject 10>" not in block
    assert "<Subject 2>" in block


# ---------------------------------------------------------------------------
# 每段接线卡
# ---------------------------------------------------------------------------

def test_segment_injection_card_is_written_and_matches_the_terminal_copy(tmp_path):
    """AC4：``segments/injection-card-NN.md`` 四列映射；终端打印的是同一份文本。"""
    job = _prepare(tmp_path)
    job.write_segment(2)
    card_path = tmp_path / "segments" / "injection-card-02.md"
    assert card_path.is_file()
    card = card_path.read_text(encoding="utf-8")
    assert "| <Picture 1> | <Picture 1> | 素材1 | assets/picture-01.png |" in card
    assert "| <Picture 2> | <Picture 10> | 素材10 | assets/picture-10.png |" in card
    assert job.segment_asset_card(2) == card


# ---------------------------------------------------------------------------
# 续跑：读回映射，不重算错号（AC5）
# ---------------------------------------------------------------------------

def _rewrite_plan_segment(tmp_path, position: int, mapping: list[tuple[int, int, str]]):
    rows = _plan_rows(tmp_path)
    rows[position - 1]["assets_in_segment"] = {
        "pictures": [{"new": new, "original": original, "name": name,
                      "path": _PATHS.get(original, "")}
                     for new, original, name in mapping],
        "subjects": [original for _new, original, _name in mapping],
    }
    (tmp_path / "segments" / "plan.json").write_text(
        json.dumps(rows, ensure_ascii=False), encoding="utf-8")


def test_resume_reuses_the_plan_mapping_instead_of_recomputing(tmp_path):
    """AC5：续跑按 plan.json 的映射改写本段，不按 Shot 原文重算。

    故意把段 2 的映射改成「原 12 → 新 2」（按 Shot 原文重算绝不会得到这个结果）：
    产物里出现 ``<Picture 2>``/``<Subject 12>`` 才能证明读的是 plan.json。
    """
    _prepare(tmp_path)
    _rewrite_plan_segment(tmp_path, 2, [(1, 10, "素材10"), (2, 12, "素材12")])
    job = load_ref_segments_for_resume(tmp_path, total_s=30.0, llm=None,
                                       ref_meta=_REGISTRY)
    assert job.plans[1].assets_in_segment is not None
    text, _issues = job.write_segment(2)
    defs = split_ref_sections(text)["subject_definitions"]
    assert "<Picture 2>" in defs and "<Subject 12>" in defs
    assert "<Picture 12>" not in defs


def test_old_plan_json_without_assets_in_segment_keeps_whole_definitions(tmp_path):
    """AC5 兼容：旧 plan.json 没有该字段 → 整片定义块原样下发，不重编号。"""
    _prepare(tmp_path)
    rows = _plan_rows(tmp_path)
    for row in rows:
        row.pop("assets_in_segment", None)
    (tmp_path / "segments" / "plan.json").write_text(
        json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    job = load_ref_segments_for_resume(tmp_path, total_s=30.0, llm=None,
                                       ref_meta=_REGISTRY)
    assert job.plans[1].assets_in_segment is None
    text, _issues = job.write_segment(2)
    defs = split_ref_sections(text)["subject_definitions"]
    assert "<Picture 10>" in defs and "<Subject 12>" in defs   # 原号，未裁剪
    assert (tmp_path / "segments" / "injection-card-02.md").exists() is False

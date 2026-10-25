"""多参专属四列分镜表（issue #89 / spec #84 D10）。

接缝（票面「Testing Decisions」）：**产物文本**——``segments/ref-shot-table.md`` 的
四列与行数、参考锚点与 ``plan.json`` 的 ``assets_in_segment`` 及段接线卡**逐项相等**、
base 路线不产出本表。全部走机械路径或打桩，不发网络。

判据为什么钉「同源」而不是「表里有图号」（票面变异检验）：分镜表若自己重算一遍锚点
（按 Shot 原文再收一次料），就会写出「表上 3 张、卡上 2 张」这种读起来都合理、却对
不上号的产物——用户照表备图、照卡接线，两边各说一套。所以锚点必须从 plan 的
``assets_in_segment`` 读，而不是第二套计算。
"""
from __future__ import annotations

import json
import re

from minimax_h3_prompt.ref_segments import (
    SHOT_TABLE_FILENAME,
    prepare_ref_segmentation,
    render_ref_shot_table,
)
from minimax_h3_prompt.segment_planner import SegmentPlan

# 30s 整片：五个镜头，切点 0/5/12/18/22/30 → 机械打包 12/10/8 三段。
# 段 1（0-12s，Shot 1-2）用图 1/2；段 2（12-22s，Shot 3-4）用图 1/10；段 3 只用图 1。
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
    (tmp_path / "video-prompt.md").write_text(_WHOLE_30S, encoding="utf-8")
    return prepare_ref_segmentation(
        whole_prompt=_WHOLE_30S, total_s=30.0, out_dir=tmp_path, llm=llm,
        ref_meta=_REGISTRY, asset_paths=_PATHS)


def _table_rows(text: str) -> list[list[str]]:
    """取表格数据行（跳过表头与分隔行）；单元格里的竖线已转义，按未转义的 ``|`` 切。"""
    lines = [line for line in text.splitlines() if line.startswith("|")]
    rows: list[list[str]] = []
    for line in lines[2:]:
        cells = [cell.strip() for cell in re.split(r"(?<!\\)\|", line.strip().strip("|"))]
        rows.append(cells)
    return rows


def _plan_rows(tmp_path) -> list[dict]:
    return json.loads((tmp_path / "segments" / "plan.json").read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# 产物本身：四列、一行一段
# ---------------------------------------------------------------------------

def test_multi_ref_run_writes_a_four_column_table_with_one_row_per_segment(tmp_path):
    """AC1：多参运行产出分镜表，四列齐全，行数＝执行段数。"""
    _prepare(tmp_path)
    text = (tmp_path / "segments" / SHOT_TABLE_FILENAME).read_text(encoding="utf-8")
    assert "| 镜头/时长 | 参考锚点 | 连续性交接 | 镜头描述摘要 |" in text
    rows = _table_rows(text)
    assert len(rows) == 3, f"应当一行一个执行段：{rows}"
    assert {len(row) for row in rows} == {4}, rows


def test_row_carries_shots_duration_anchors_states_and_summary(tmp_path):
    """AC1：每行的四列各有其物——镜头/时长、本段锚点、起止状态、剧情摘要。"""
    _prepare(tmp_path)
    rows = _table_rows((tmp_path / "segments" / SHOT_TABLE_FILENAME).read_text(
        encoding="utf-8"))
    first, second = rows[0], rows[1]
    assert first[0] == "Shot 1–2 · 0–12s（12s）"
    assert first[1] == ("<Picture 1>（整片 <Picture 1>）素材1；"
                        "<Picture 2>（整片 <Picture 2>）素材2")
    # 连续性交接：plan 的 start_state → end_hook（机械路径取自本段首/末镜头句）
    assert "→" in first[2]
    head, tail = (part.strip() for part in first[2].split("→", 1))
    assert head.startswith("<Subject 1> walks in")
    assert tail.startswith("the shot cuts to a close-up of")
    assert first[3], "镜头描述摘要不能为空"
    # 段 2 的图号按段内重编号：原 10 → 新 2
    assert "<Picture 2>（整片 <Picture 10>）素材10" in second[1]


# ---------------------------------------------------------------------------
# 与 plan.json / 段接线卡同源
# ---------------------------------------------------------------------------

def test_anchors_equal_plan_mapping_and_the_segment_injection_card(tmp_path):
    """AC2：每段锚点与 ``assets_in_segment``、段接线卡逐项相等（同一份来源）。"""
    job = _prepare(tmp_path)
    table = _table_rows((tmp_path / "segments" / SHOT_TABLE_FILENAME).read_text(
        encoding="utf-8"))
    for position, (plan, row) in enumerate(zip(job.plans, table), 1):
        mapped = [(item["new"], item["original"], item["name"])
                  for item in plan.assets_in_segment["pictures"]]
        assert row[1] == "；".join(
            f"<Picture {new}>（整片 <Picture {original}>）{name}"
            for new, original, name in mapped), f"第 {position} 段与 plan.json 不一致"
        # 接线卡只在写段时落盘——写它一次，取同一段的「新号 | 原号 | 素材名」三列
        job.write_segment(position)
        card = (tmp_path / "segments" / f"injection-card-{position:02d}.md").read_text(
            encoding="utf-8")
        card_rows = [line for line in card.splitlines() if line.startswith("| <Picture")]
        from_card = [(int(re.search(r"\| <Picture (\d+)>", line).group(1)),
                      int(re.search(r"\| <Picture \d+> \| <Picture (\d+)>", line).group(1)),
                      line.split("|")[3].strip())
                     for line in card_rows]
        assert from_card == mapped, f"第 {position} 段接线卡与分镜表不是同一份锚点"


def test_anchors_are_read_from_the_plan_mapping_not_recomputed(tmp_path):
    """变异检验（票面硬要求）：把映射写成 Shot 原文重算**绝不会**得到的样子。

    按 Shot 原文重算的锚点只会是「原号 1,2」；这里故意写「原 12 → 新 1、原 10 → 新 2」，
    表若照抄 plan 映射就出现 12/10，一旦有人把来源拆开（自己再收一次料）本条必红。
    """
    plan = SegmentPlan(
        index=0, start_s=0, end_s=12, shots_in_segment=(1, 2),
        summary="进门", end_hook="the girl enters", start_state="the gate opens",
        assets_in_segment={
            "pictures": [
                {"new": 1, "original": 12, "name": "素材12", "path": ""},
                {"new": 2, "original": 10, "name": "素材10", "path": ""},
            ],
            "subjects": [12, 10],
        })
    row = _table_rows(render_ref_shot_table([plan], total_s=12))[0]
    assert row[1] == "<Picture 1>（整片 <Picture 12>）素材12；<Picture 2>（整片 <Picture 10>）素材10"


def test_cell_text_cannot_break_the_table(tmp_path):
    """状态句里带折行/竖线时压平并转义——否则整张表列数当场错位。"""
    plan = SegmentPlan(
        index=0, start_s=0, end_s=4, shots_in_segment=(1,),
        summary="第一行\n第二行 | 带竖线", end_hook="end | state",
        start_state="start\nstate",
        assets_in_segment={"pictures": [], "subjects": []})
    row = _table_rows(render_ref_shot_table([plan], total_s=4))[0]
    assert len(row) == 4
    assert "\n" not in row[3]
    assert "第一行 第二行 \\| 带竖线" == row[3]


# ---------------------------------------------------------------------------
# 路线隔离：首尾帧路线不产出，既有产物键不变
# ---------------------------------------------------------------------------

_FIXTURE_TEXT = (
    "For the target video, at 0.00 seconds into the target video, "
    "<Picture 1> (from [Shot 1]) is fully referenced.\n\n"
    "[Shot 1] The store interior is still.\n"
    "[Shot 2] At 00:06.000, the cat walks to the counter.\n\n"
    "overall_soundscape: a low hum.\n\n"
    "non_diegetic_music: N/A\n"
)


def _base_session(tmp_path):
    from minimax_h3_prompt import session_store
    from minimax_h3_prompt.brief_parser import Brief

    brief = Brief(mode="base", variant="I2VA", duration=12.0, style="写实",
                  plot="便利店橘猫")
    generation_dir = tmp_path / "GEN001"
    generation_dir.mkdir(parents=True, exist_ok=True)
    return session_store.SessionState(
        directory=generation_dir, brief=brief,
        stage_state={"shot_table": "[Shot 1] 深夜空店内……"},
        status=session_store.STATUS_SEGMENTED_RUNNING)


def test_base_route_never_writes_the_ref_shot_table(tmp_path, monkeypatch):
    """AC3：首尾帧路线（v2 陪跑）跑完一段也不产出 ``ref-shot-table.md``。"""
    from types import SimpleNamespace

    from minimax_h3_prompt import segment_prompts
    from minimax_h3_prompt.ui import wizard

    session = _base_session(tmp_path)
    plans = [SegmentPlan(index=0, start_s=0, end_s=4, shots_in_segment=(1,),
                         summary="守店", end_hook="第一段末态")]
    frame = tmp_path / "frame.png"
    frame.write_bytes(b"png")

    monkeypatch.setattr(segment_prompts, "write_segment_v2",
                        lambda *a, **k: _FIXTURE_TEXT)
    monkeypatch.setattr(
        wizard, "_capture_bridge_frame",
        lambda *a, **k: ([], "桥接帧画面", frame))
    monkeypatch.setattr(wizard, "_confirm", lambda *a, **k: True)
    monkeypatch.setattr(wizard, "_drain_stdin", lambda: None)
    monkeypatch.setattr(wizard, "_prompt", lambda *a, **k: "")

    wizard._run_segmented_flow_v2(session, plans, {"shot_table": "[Shot 1] x"},
                                  SimpleNamespace())

    seg_dir = session.directory / "segments"
    assert (seg_dir / "shot-01.md").is_file(), "base 陪跑应当照常写出段文件"
    assert not (seg_dir / SHOT_TABLE_FILENAME).exists(), "首尾帧路线不产出多参分镜表"
    # 既有产物键不变：段表仍是 base 的七个键，没有混进段内素材子集
    rows = json.loads((seg_dir / "plan.json").read_text(encoding="utf-8"))
    assert set(rows[0]) == {"index", "start_s", "end_s", "shots_in_segment",
                            "summary", "end_hook", "start_state"}

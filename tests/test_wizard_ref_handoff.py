"""向导·多参逐段陪跑（issue #55）：一次只出当前一段 → 交回本段视频 → 可证层硬阻断 → 落段产出账。

覆盖票面 AC：
- AC1 收尾只展示**当前一段**的提示词（含起止秒与本段时长），整片六段式降级为存档预览；
- AC2 一次只维持一个当前待办：交回并验收通过才出下一段；
- AC3 交回口收本段输出视频路径，空输入**停住**（不静默跳过、不静默降级）；
- AC4 可证层不合格**硬阻断**：不写下一段、不落进度，并当面打出错在哪；
- AC5 段产出账一段一行，含视频路径与 SHA256。

薄接线：走真实调用链（``run_wizard`` 的多参阶段 2），只打桩 LLM 与交互输入。
视频是**真写**的（可证层判的是帧数档，假文件量不出来就只剩 warning）。
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace

import cv2
import numpy as np

from minimax_h3_prompt import model_factory
from minimax_h3_prompt.brief_parser import Brief
from minimax_h3_prompt.ref_segments import (
    SEGMENT_LEDGER_FILENAME,
    read_segment_ledger,
)
from minimax_h3_prompt.tools import reference_auditor
from minimax_h3_prompt.tools.frame_match import video_meta
from minimax_h3_prompt.tools.preflight import EXPECTED_VIDEO_SIZE, H3_TIER_FRAMES
from minimax_h3_prompt.ui import wizard

TOPIC = "红裙女孩走进秋日庭院，把青铜钥匙交给蓝衫少年。"

# 20s 两镜（切点 0/10）→ 机械打包恰得两段各 10s，段正文互不相同：
# 段 1 只有 Shot 1（花园小径），段 2 只有 Shot 2（钥匙特写）。两句话是「当前展示的是
# 哪一段」的判据——出现次数与先后都靠它们认。
SEG1_BODY = "A medium shot frames the girl walking through the garden gate."
SEG2_BODY = "the shot cuts to a close-up of the bronze key in her hand"
# 整片 summary 那句：机械拆段的段 summary 是固定句（``_MECH_SUMMARY_BODY``），
# 所以这句话只会出现在**整片**正文里——它是「整片有没有被当交付物打印」的判据。
_WHOLE_SUMMARY = "The target video shows the girl handing over the key."

_SIX_SECTION_PROMPT = (
    "subject_definitions:\n"
    "<Picture 1> is 红裙女孩.\n<Picture 2> is 秋日庭院.\n"
    "<Picture 3> is 青铜钥匙.\n<Picture 4> is 蓝衫少年.\n\n"
    f"summary:\n[reference generation] {_WHOLE_SUMMARY}\n\n"
    "retention_analysis:\n<Picture 1>: fully_preserved - ok.\n\n"
    "detailed_description:\n"
    f"[Shot 1] {SEG1_BODY}\n"
    f"[Shot 2] At 00:10.000, {SEG2_BODY}.\n\n"
    "overall_soundscape:\nSoft outdoor ambience continues.\n\n"
    "non_diegetic_music:\nN/A"
)


def _silence(monkeypatch) -> None:
    monkeypatch.setattr(wizard, "_drain_stdin", lambda: None)
    monkeypatch.setattr(wizard.sys, "stdin", SimpleNamespace(isatty=lambda: True))
    # 读图是**网络调用**（qwen3.7-plus），必须打桩（同「会真建 LLM 客户端的用例一律打桩」
    # 的纪律）：不打桩时本机没 key 会逐帧等到超时（#56 实测这个文件从 80s 涨到 145s，
    # 全量里看着像卡住），有 key 时又把用例结论绑到真模型上。语义层缺读图结果的诚实
    # 降级另有专门用例（test_ref_segment_frames.py）。
    monkeypatch.setattr(reference_auditor, "describe_image",
                        lambda *a, **k: "桩：本段首/末帧的实际画面")


def _fake_inputs(monkeypatch, inputs: list[str]) -> list[str]:
    prompts = iter(inputs)
    asked: list[str] = []

    def fake_prompt(text: str) -> str:
        asked.append(text)
        return next(prompts)

    monkeypatch.setattr(wizard, "_prompt", fake_prompt)
    return asked


def _confirm_continue_only(text: str, default: bool = True) -> bool:
    """确认桩：只有「继续出提示词」答是，其余（素材视频/音频追加等）一律跳过。

    **语义层那句（问「达到」）答是**（issue #56 起每段交回后多一道人判）：本文件测的是
    **可证层**与段产出账（#55 的 AC），语义层的五档出路与改写轮次在
    ``test_wizard_ref_semantics.py`` 里测——这里一律判「达到」，让可证层的用例只受
    可证层影响（否则每条都要多喂一个菜单输入，测的东西就混了）。
    """
    if "达到" in text:
        return True
    return "继续" in text


def _ref_stage1_state() -> dict:
    return {
        "script": TOPIC,
        "character_image_prompts": [
            {"name": "蓝衫少年", "positive_prompt": "人物·蓝衫少年专属生图提示词"},
            {"name": "红裙女孩", "positive_prompt": "人物·红裙女孩专属生图提示词"},
        ],
        "prop_image_prompts": [
            {"name": "青铜钥匙", "positive_prompt": "道具·青铜钥匙专属生图提示词"},
        ],
        "scene_image_prompts": [
            {"name": "秋日庭院", "positive_prompt": "场景·秋日庭院专属生图提示词"},
        ],
    }


def _cfg(tmp_path, **overrides):
    base = dict(
        default_duration=5.0, default_language="Chinese",
        sessions_root=tmp_path / "sessions", max_clarification_rounds=0,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _make_video(path: Path, seconds: float, *, fps: int = 24) -> Path:
    """写一段真视频：帧数按 H3 实测档位取（「4s」档真产物是 107 帧，不是 96 帧）。

    这一条不是装饰：可证层按**帧数表**判档，96 帧的假 4s 视频落不进任何档位、只报
    「无法确证档位」的 warning——用错帧数的话「用错时长档被拦下」这条根本测不出来。
    """
    frames = H3_TIER_FRAMES.get(int(round(seconds)), int(round(seconds * fps)))
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"MJPG"), fps,
                             EXPECTED_VIDEO_SIZE)
    assert writer.isOpened(), "测试环境缺 MJPG 编码器"
    image = np.full((EXPECTED_VIDEO_SIZE[1], EXPECTED_VIDEO_SIZE[0], 3), 128, np.uint8)
    for i in range(frames):
        image[:] = 128 + (i % 8)
        writer.write(image)
    writer.release()
    actual = video_meta(path).get("duration_s", 0.0)
    assert abs(actual - frames / fps) < 0.05, f"测试视频时长不可控：{actual}"
    return path


def _run(tmp_path, monkeypatch, handbacks: list[str]) -> tuple[list[str], int]:
    """全程驱动：阶段 1 → 素材循环（四张全放弃）→ 阶段 2 → 逐段陪跑。"""
    _silence(monkeypatch)
    monkeypatch.setattr(wizard, "_choose_canvases", lambda: {})
    monkeypatch.setattr(
        wizard, "run_stage1",
        lambda brief, config, **kw: (_ref_stage1_state(), object(),
                                     {"prompt_engineer": object()}))
    monkeypatch.setattr(
        wizard, "run_stage2",
        lambda state, brief, config, **kw: (dict(state), _SIX_SECTION_PROMPT))
    # 假模型没有 invoke：规划与回退重写都失败 → 走机械拆段（确定性）
    monkeypatch.setattr(model_factory, "build_chat_model", lambda: object())
    monkeypatch.setattr(wizard, "_confirm", _confirm_continue_only)
    tail = [TOPIC, "20", "", "4", "", "4", "", "4", "", "4", "4", *handbacks]
    asked = _fake_inputs(monkeypatch, tail)
    return asked, wizard.run_wizard(_cfg(tmp_path))


def _generation_dir(tmp_path) -> Path:
    """本次生成的 GEN 目录（``read_segment_ledger`` 收的是它，不是 segments/ 那层）。"""
    return next((tmp_path / "sessions").rglob("session-state.json")).parent


def _output_rows(tmp_path) -> list[dict]:
    """账上的**通过行**（``event=output``）。

    #56 起同一本账还装 encounter/rewrite 两类行（语义层遭遇与改写轮次），所以「哪几段
    过了」必须按 event 取数——不判别就会把「改了一轮词」读成「这段过了」。
    """
    from minimax_h3_prompt.ref_segments import SEGMENT_EVENT_OUTPUT

    return read_segment_ledger(_generation_dir(tmp_path), event=SEGMENT_EVENT_OUTPUT)


def _whole_prompt_text(tmp_path) -> str:
    return next((tmp_path / "sessions").rglob("video-prompt.md")).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# AC1 / AC2：只展示当前一段；通过才出下一段
# ---------------------------------------------------------------------------

def test_only_current_segment_is_shown_and_next_comes_after_accept(tmp_path, monkeypatch, capsys):
    """段 1 的词摆出来、整片不再当交付物；段 2 的词在段 1 交回通过之后才出现。"""
    first = _make_video(tmp_path / "seg1.avi", 10.0)
    second = _make_video(tmp_path / "seg2.avi", 10.0)
    asked, code = _run(tmp_path, monkeypatch, [str(first), str(second)])
    assert code == 0
    out = capsys.readouterr().out

    # 交回口收的是**本段输出视频路径**（AC3），问句里带段号
    assert any("第 1/2 段输出视频路径" in text for text in asked), f"交回口问句不对：{asked[-3:]}"

    # 交付主体换位：整片六段式只落盘存档，不再逐字打在屏幕上。
    # 判据取整片 summary 那句——机械拆段的段 summary 是固定句（_MECH_SUMMARY_BODY），
    # 所以「整片那句出现了」只可能是整片正文被打印（原先正是这样）。
    assert _WHOLE_SUMMARY in _whole_prompt_text(tmp_path), "整片存档本身不许撤"
    assert _WHOLE_SUMMARY not in out, "整片六段式仍在当交付物展示"
    assert "存档预览" in out

    # 当前一段的抬头：段号 / 起止秒 / 本段时长 / 提示词文件
    assert "第 1/2 段 · 起止 0s-10s（本段时长 10s）" in out
    assert "segment-01.md" in out
    assert "第 2/2 段 · 起止 10s-20s（本段时长 10s）" in out

    # 一次只维持一个当前待办：段 2 的词出现在段 1 通过之后
    assert out.index(SEG2_BODY) > out.index("第 1/2 段已交回并通过可证层与语义层校验")
    assert out.index(SEG1_BODY) < out.index(SEG2_BODY)


# ---------------------------------------------------------------------------
# AC5：段产出账一段一行（含视频路径与 SHA256）
# ---------------------------------------------------------------------------

def test_ledger_records_each_segment_with_path_and_sha(tmp_path, monkeypatch, capsys):
    first = _make_video(tmp_path / "seg1.avi", 10.0)
    second = _make_video(tmp_path / "seg2.avi", 10.0)
    _, code = _run(tmp_path, monkeypatch, [str(first), str(second)])
    assert code == 0

    rows = _output_rows(tmp_path)
    assert [r["segment"] for r in rows] == [1, 2]
    assert [r["start_s"] for r in rows] == [0, 10]
    assert [r["end_s"] for r in rows] == [10, 20]
    assert rows[0]["video"] == str(first.resolve())
    assert rows[1]["video"] == str(second.resolve())
    assert rows[0]["sha256"] == hashlib.sha256(first.read_bytes()).hexdigest()
    assert rows[1]["sha256"] == hashlib.sha256(second.read_bytes()).hexdigest()
    assert {r["verdict"] for r in rows} == {"pass"}
    assert all(r["at"] for r in rows)
    assert list((tmp_path / "sessions").rglob(SEGMENT_LEDGER_FILENAME))


# ---------------------------------------------------------------------------
# AC4：可证层不合格硬阻断（不写下一段、不落进度），并当面打出错在哪
# ---------------------------------------------------------------------------

def test_wrong_duration_hard_blocks_and_logs_nothing_for_that_segment(tmp_path, monkeypatch, capsys):
    """段 1 计划 10s 却交回 4s 的视频（用错时长档）→ 拦下、重问，账上只留合规那份。"""
    wrong = _make_video(tmp_path / "wrong.avi", 4.0)
    right = _make_video(tmp_path / "right.avi", 10.0)
    last = _make_video(tmp_path / "seg2.avi", 10.0)
    _, code = _run(tmp_path, monkeypatch, [str(wrong), str(right), str(last)])
    assert code == 0
    out = capsys.readouterr().out

    assert "segment_video_duration_mismatch" in out, "可证错误必须当面报出，绝不静默"
    assert "交错了段，或用错了时长档" in out, "必须说清错在哪（AC4）"
    # 硬阻断发生在段 2 出词之前：被拦下的那一次不进入下一段
    assert out.index(SEG2_BODY) > out.index("segment_video_duration_mismatch")

    rows = _output_rows(tmp_path)
    assert len(rows) == 2, f"被拦下的那一次不许落账：{rows}"
    assert rows[0]["video"] == str(right.resolve())
    assert rows[0]["sha256"] == hashlib.sha256(right.read_bytes()).hexdigest(), \
        "账上记的必须是**通过验收的那一份**，不是先交上来的错份"


def test_missing_video_path_is_refused_not_silently_accepted(tmp_path, monkeypatch, capsys):
    """路径不存在＝可证事实：当面拦住重问，绝不当成「量不出来」放行。"""
    absent = tmp_path / "not-there.avi"
    assert not absent.exists()
    right = _make_video(tmp_path / "right.avi", 10.0)
    last = _make_video(tmp_path / "seg2.avi", 10.0)
    _, code = _run(tmp_path, monkeypatch, [str(absent), str(right), str(last)])
    assert code == 0
    out = capsys.readouterr().out
    assert "读不到这个文件" in out
    rows = _output_rows(tmp_path)
    assert [r["segment"] for r in rows] == [1, 2]
    assert rows[0]["video"] == str(right.resolve())


# ---------------------------------------------------------------------------
# AC3：空输入停住（不静默跳过、不静默降级）
# ---------------------------------------------------------------------------

def test_empty_handback_stops_the_run(tmp_path, monkeypatch, capsys):
    """回车＝停住：这一段不落账、下一段不出词，已通过的段留在账上。"""
    _, code = _run(tmp_path, monkeypatch, [""])
    assert code == 0
    out = capsys.readouterr().out
    assert "没有交回输出视频" in out
    assert "停在这里" in out
    assert SEG2_BODY not in out, "停住之后不许再出下一段"
    assert not list((tmp_path / "sessions").rglob(SEGMENT_LEDGER_FILENAME)), \
        "一段都没通过时账文件不该存在（不是「存在但为空」）"
    assert _output_rows(tmp_path) == []


def test_empty_handback_after_one_pass_keeps_that_row(tmp_path, monkeypatch, capsys):
    """段 1 通过后停住：账上留着段 1 那一行（进度不丢），段 2 那一行不落。"""
    first = _make_video(tmp_path / "seg1.avi", 10.0)
    _, code = _run(tmp_path, monkeypatch, [str(first), ""])
    assert code == 0
    out = capsys.readouterr().out
    # 段 2 的词**出**了（段 1 通过了，这就是「一次只维持一个当前待办」的正常推进），
    # 但它没有交回 → 不落账
    assert out.index(SEG2_BODY) > out.index("第 1/2 段已交回并通过可证层与语义层校验")
    rows = _output_rows(tmp_path)
    assert [r["segment"] for r in rows] == [1]
    assert rows[0]["video"] == str(first.resolve())

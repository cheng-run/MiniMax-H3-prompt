"""向导·续跑点（issue #51）：中断后续跑只补真正缺的部分。

五条 AC 的牙齿在这里：
- AC1 答过的答案不重答（素材路径、裁决）；
- AC2 已入库素材与已裁决项续跑跳过，从缺的那张继续；
- AC3 阶段 2 组装开跑前，入参（视频输出三元组 / 帧图读图结果）已在会话状态里；
- AC4 续跑点是可证的落盘记录（会话状态增量），保存/恢复有单元测试；
- AC5 中断-续跑全程走真实调用链（``run_wizard``），只打桩 LLM 与交互输入。

模拟中断的手法：输入队列走完就抛 ``KeyboardInterrupt``——那正是用户在终端按 Ctrl-C
的现场；``main`` 顶层也按它收尾。打桩 ``run_stage2`` 抛中断时同理。
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import minimax_h3_prompt.asset_ledger as asset_ledger_module
from minimax_h3_prompt import model_factory
from minimax_h3_prompt.asset_ledger import load_asset_ledger
from minimax_h3_prompt.asset_proofread import progress_rows
from minimax_h3_prompt.brief_parser import Brief
from minimax_h3_prompt.resume_point import ANSWERS_KEY
from minimax_h3_prompt.session_store import (
    STATUS_AWAITING_FRAMES,
    load_session,
    save_session,
)
from minimax_h3_prompt.tools import frame_auditor, reference_auditor
from minimax_h3_prompt.ui import wizard

TOPIC = "红裙女孩走进秋日庭院，把青铜钥匙交给蓝衫少年。"

_SIX_SECTION_PROMPT = (
    "subject_definitions:\n<Subject 1> is 红裙女孩 in <Picture 1>.\n\n"
    "summary:\n[reference generation] The target video shows the girl handing over the key.\n\n"
    "retention_analysis:\n<Subject 1>: fully_preserved - ok.\n\n"
    "detailed_description:\n[Shot 1] A medium shot frames the girl in the garden.\n\n"
    "overall_soundscape:\nSoft outdoor ambience continues.\n\n"
    "non_diegetic_music:\nN/A"
)

GIRL_PROMPT = "人物·红裙女孩专属生图提示词：红裙、黑发、银项链"


# ---------------------------------------------------------------------------
# 桩
# ---------------------------------------------------------------------------

def _silence(monkeypatch) -> None:
    monkeypatch.setattr(wizard, "_drain_stdin", lambda: None)
    monkeypatch.setattr(wizard.sys, "stdin", SimpleNamespace(isatty=lambda: True))


def _fake_inputs(monkeypatch, inputs: list[str], asked: list[str] | None = None) -> list[str]:
    """按序喂输入；喂完＝用户按了 Ctrl-C（续跑点的中断就是它）。

    ``asked`` 传列表即共用一个 sink：本文件要在**一次测试里跑两轮向导**，
    两轮的提问要落在同一份账上才能断言「这轮没再问那道题」。
    """
    prompts = iter(inputs)
    asked = asked if asked is not None else []

    def fake_prompt(text: str) -> str:
        asked.append(text)
        try:
            return next(prompts)
        except StopIteration:
            # 逐段陪跑的交回口（issue #57 起 ≤15 秒直出也走这一套收尾）：跑到底的那
            # 一轮喂完输入就停在这里。本文件用「喂完＝Ctrl-C」表示中断，不在这儿收住
            # 会把「跑完」误当成「又断了一次」。
            if "输出视频路径" in text:
                return ""
            raise KeyboardInterrupt from None

    monkeypatch.setattr(wizard, "_prompt", fake_prompt)
    return asked


def _fake_confirms(monkeypatch, rules: list[tuple[str, bool]]) -> list[str]:
    queue = list(rules)
    asked: list[str] = []

    def fake_confirm(text: str, default: bool = True) -> bool:
        asked.append(text)
        for i, (needle, answer) in enumerate(queue):
            if needle in text:
                queue.pop(i)
                return answer
        return default

    monkeypatch.setattr(wizard, "_confirm", fake_confirm)
    return asked


def _ref_stage1_state() -> dict:
    return {
        "script": TOPIC,
        "character_image_prompts": [
            {"name": "蓝衫少年", "positive_prompt": "人物·蓝衫少年专属生图提示词"},
            {"name": "红裙女孩", "positive_prompt": GIRL_PROMPT},
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
        vision_model=SimpleNamespace(model="stub-vision"),
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _stub_stage1(monkeypatch) -> None:
    monkeypatch.setattr(
        wizard, "run_stage1",
        lambda brief, config, **kw: (_ref_stage1_state(), object(),
                                     {"prompt_engineer": object()}),
    )


def _stub_stage2(monkeypatch, captured: dict | None = None) -> None:
    def fake_run_stage2(state, brief, config, **kwargs):
        if captured is not None:
            captured["state"] = dict(state)
        return dict(state), _SIX_SECTION_PROMPT

    monkeypatch.setattr(wizard, "run_stage2", fake_run_stage2)


def _stub_describe(monkeypatch, mapping: dict[str, str], calls: list[str] | None = None) -> list[str]:
    seen = calls if calls is not None else []

    def fake_describe(path, instruction=None):
        seen.append(str(path))
        for key, raw in mapping.items():
            if key in str(path):
                return raw
        return '{"claims": []}'

    monkeypatch.setattr(reference_auditor, "describe_image", fake_describe)
    return seen


def _claims(deviations=(), omissions=()) -> str:
    items = [{"aspect": a, "prompt_quote": q, "observed": o} for a, q, o in deviations]
    items += [{"aspect": a, "prompt_quote": "", "observed": o} for a, o in omissions]
    return json.dumps({"claims": items}, ensure_ascii=False)


def _write_source(tmp_path: Path, name: str, content: bytes) -> Path:
    source = tmp_path / "returned" / name
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(content)
    return source


def _session_dir(tmp_path: Path) -> Path:
    return next((tmp_path / "sessions").rglob("session-state.json")).parent


def _stub_alignment(monkeypatch) -> None:
    """桩掉回灌（``_align_asset_with_settled``）——它会真建 LLM 客户端。

    本文件测的是**续跑点**，回灌结果不在任何断言里；不打桩的话，有 ``.env`` 的机器上
    它会真发网络请求（2026-10-21 实测：本文件 6 条用例 236s→54s，单条 3 次调用共 105s），
    而没有 ``.env`` 的干净检出上同一路径又秒失败——两种机器跑的分明不是同一条路径。
    本仓纪律「会真建 LLM 客户端的用例一律打桩」正是为这个：抛错让回灌走它自己的
    「失败只提示、词保持原样」出口，与干净检出上的行为一致且毫秒级。
    """
    def boom(*_args, **_kwargs):
        raise RuntimeError("回灌调用未打桩（issue #59 全量跑时发现的真网络请求）")

    monkeypatch.setattr(model_factory, "build_chat_model", boom)


def _start_ref_run(tmp_path, monkeypatch, *, vision: dict[str, str] | None = None,
                   stage2=None, asked: list[str] | None = None) -> list[str]:
    """装好一整套桩（不跑）。返回共用的 ``asked`` sink——两轮向导的提问都落在它上面。"""
    asked = asked if asked is not None else []
    _silence(monkeypatch)
    _stub_alignment(monkeypatch)
    monkeypatch.setattr(wizard, "_choose_canvases", lambda: {})
    _stub_stage1(monkeypatch)
    if stage2 is None:
        _stub_stage2(monkeypatch)
    else:
        monkeypatch.setattr(wizard, "run_stage2", stage2)
    _stub_describe(monkeypatch, vision or {})
    _fake_confirms(monkeypatch, [("视频/音频", False)])
    return asked


def _run(tmp_path, monkeypatch, inputs: list[str], asked: list[str] | None = None) -> int:
    """跑一次向导（输入原样喂）；续跑时没有阶段 1 那四问，故由调用方给全。"""
    _fake_inputs(monkeypatch, list(inputs), asked)
    return wizard.run_wizard(_cfg(tmp_path))


def _fresh(tail: list[str]) -> list[str]:
    """首轮向导：主题/时长/风格/方式=多参 四问固定，其余由 tail 决定。"""
    return [TOPIC, "", "", "4"] + list(tail)


# ---------------------------------------------------------------------------
# AC1：素材路径答过就不重答
# ---------------------------------------------------------------------------

def test_answered_asset_path_survives_interrupt_before_intake(tmp_path, monkeypatch):
    """填了路径、正入库时被 Ctrl-C：续跑按**已答的路径**接着走，不再问一遍。"""
    asked = _start_ref_run(tmp_path, monkeypatch)
    girl = _write_source(tmp_path, "girl.png", b"girl")

    def boom(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(asset_ledger_module, "intake_assets", boom)
    with pytest.raises(KeyboardInterrupt):
        _run(tmp_path, monkeypatch, _fresh(["", "1", str(girl)]), asked)

    # 落盘证据：答案账里已有这条路径（AC4「可证的落盘记录」）
    session = load_session(_session_dir(tmp_path))
    assert session.stage_state[ANSWERS_KEY]["asset_path:红裙女孩"] == str(girl)

    # 续跑：审查菜单照旧（产物还没落定），路径问题回车＝沿用上次的答案
    monkeypatch.undo()
    asked = _start_ref_run(tmp_path, monkeypatch, asked=asked)
    monkeypatch.setattr(wizard, "_offer_resume", lambda config: load_session(_session_dir(tmp_path)))
    tail = ["1", "", "", "4", "", "4", "4"]  # 满意 → 路径回车=沿用 → 其余三张放弃
    code = _run(tmp_path, monkeypatch, tail, asked)

    assert code == 0
    path_questions = [text for text in asked if "素材图路径" in text]
    assert any("沿用上次" in text for text in path_questions), \
        f"续跑没给出已答的路径（用户得重新敲一遍）：{path_questions}"
    ledger = load_asset_ledger(_session_dir(tmp_path))
    assert ledger.get("红裙女孩", "picture").source == str(girl), "沿用的路径没真的用来入库"


def test_remembered_path_can_be_cleared_with_dash(tmp_path, monkeypatch):
    """回车被「沿用上次」占了之后，还要留一个空出口：输 ``-`` 即清空（回审查/跳过）。"""
    _silence(monkeypatch)
    girl = _write_source(tmp_path, "girl.png", b"girl")
    asked = _fake_inputs(monkeypatch, ["-"])

    assert wizard._ask_asset_path("红裙女孩 素材图路径", remembered=str(girl)) == ""
    assert "沿用上次" in asked[0] and "输 - 改填" in asked[0], \
        f"有已答路径时没把「怎么清空」说给用户：{asked[0]}"


def test_answered_asset_path_is_forgotten_once_intaken(tmp_path, monkeypatch):
    """入库落定后答案账作废：产物接它的班，答案不再冒充事实（AC1 的另一半）。"""
    asked = _start_ref_run(tmp_path, monkeypatch)
    girl = _write_source(tmp_path, "girl.png", b"girl")
    code = _run(tmp_path, monkeypatch,
                _fresh(["", "1", str(girl), "", "4", "", "4", "4"]), asked)

    assert code == 0
    session = load_session(_session_dir(tmp_path))
    assert "asset_path:红裙女孩" not in (session.stage_state.get(ANSWERS_KEY) or {})


# ---------------------------------------------------------------------------
# AC2：已入库素材续跑跳过审查与路径问答
# ---------------------------------------------------------------------------

def test_returned_asset_resumes_at_proofread_without_asking_path(tmp_path, monkeypatch):
    """图已入库、读图时中断：续跑从**这张的校对**接上，不问审查、不问路径。"""
    asked = _start_ref_run(tmp_path, monkeypatch)
    girl = _write_source(tmp_path, "girl.png", b"girl")

    def boom(path, instruction=None):
        raise KeyboardInterrupt

    monkeypatch.setattr(reference_auditor, "describe_image", boom)
    with pytest.raises(KeyboardInterrupt):
        _run(tmp_path, monkeypatch, _fresh(["", "1", str(girl)]), asked)

    ledger_dir = _session_dir(tmp_path)
    assert load_asset_ledger(ledger_dir).get("红裙女孩", "picture") is not None, "图没入库，续跑无从跳过"

    monkeypatch.undo()
    boundary = len(asked)
    asked = _start_ref_run(tmp_path, monkeypatch, asked=asked)
    monkeypatch.setattr(wizard, "_offer_resume", lambda config: load_session(ledger_dir))
    tail = ["1", "", "4", "", "4", "4"]  # 接着校对 → 其余三张放弃
    code = _run(tmp_path, monkeypatch, tail, asked)

    assert code == 0
    resumed = asked[boundary:]
    assert any("接着校对这一版" in text for text in resumed), "续跑没认出这张已入库"
    assert not any("素材图路径" in text for text in resumed), "已入库的图又被要求填一遍路径"
    record = load_session(ledger_dir).stage_state["asset_proofreads"]["红裙女孩"]
    assert record["status"] == "done"


def test_returned_asset_can_be_replaced_from_resume_menu(tmp_path, monkeypatch):
    """续跑菜单留「换一张图」出口：已入库不等于锁死，换了照样重新入库校对。"""
    asked = _start_ref_run(tmp_path, monkeypatch)
    girl = _write_source(tmp_path, "girl.png", b"girl")

    def boom(path, instruction=None):
        raise KeyboardInterrupt

    monkeypatch.setattr(reference_auditor, "describe_image", boom)
    with pytest.raises(KeyboardInterrupt):
        _run(tmp_path, monkeypatch, _fresh(["", "1", str(girl)]), asked)

    monkeypatch.undo()
    asked = _start_ref_run(tmp_path, monkeypatch, asked=asked)
    girl2 = _write_source(tmp_path, "girl2.png", b"girl-v2")
    ledger_dir = _session_dir(tmp_path)
    monkeypatch.setattr(wizard, "_offer_resume", lambda config: load_session(ledger_dir))
    _fake_confirms(monkeypatch, [("视频/音频", False), ("用新版替换登记", True)])
    # 2=换一张图重新交回 → 审查满意 → 新路径 → 其余三张放弃
    tail = ["2", "1", str(girl2), "", "4", "", "4", "4"]
    code = _run(tmp_path, monkeypatch, tail, asked)

    assert code == 0
    assert any("换一张图重新交回" in text for text in asked)
    assert load_asset_ledger(ledger_dir).get("红裙女孩", "picture").source == str(girl2)


# ---------------------------------------------------------------------------
# AC1：裁决答过就不重答（读图结果也不重付）
# ---------------------------------------------------------------------------

def test_decided_deviations_are_not_asked_again_on_resume(tmp_path, monkeypatch):
    """两条偏差裁完一条就中断：续跑只问剩下的那条，且不重付一次读图。"""
    vision = {"红裙女孩":_claims(deviations=[("裙色", "红裙", "宝蓝色长裙"),
                                                ("发型", "黑发", "短发")])}
    asked = _start_ref_run(tmp_path, monkeypatch, vision=vision)
    girl = _write_source(tmp_path, "girl.png", b"girl")

    with pytest.raises(KeyboardInterrupt):
        _run(tmp_path, monkeypatch, _fresh(["", "1", str(girl), "", ""]), asked)  # 裁第 1 条后断

    ledger_dir = _session_dir(tmp_path)
    progress = progress_rows(load_session(ledger_dir).stage_state)["红裙女孩"]
    assert len(progress["decisions"]) == 1, "已裁的那条没落盘"
    assert len(progress["claims"]) == 2, "读图结果没落盘（续跑要重付一次视觉调用）"

    monkeypatch.undo()
    boundary = len(asked)
    asked = _start_ref_run(tmp_path, monkeypatch, vision=vision, asked=asked)
    vision_calls = _stub_describe(monkeypatch, vision)  # 续跑段独立计读图次数
    monkeypatch.setattr(wizard, "_offer_resume", lambda config: load_session(ledger_dir))
    # 续跑：接着校对 → 裁第 2 条 → 其余三张放弃（出下一张前还有一次队列菜单）
    tail = ["1", "", "", "", "4", "", "4", "4"]
    code = _run(tmp_path, monkeypatch, tail, asked)

    assert code == 0
    resumed = asked[boundary:]
    assert sum(1 for text in resumed if "按图改文案" in text) == 1, \
        f"续跑把已裁的条目重问了一遍：{resumed}"
    assert not vision_calls, f"续跑又付了一次读图（读图结果本该沿用）：{vision_calls}"
    record = load_session(ledger_dir).stage_state["asset_proofreads"]["红裙女孩"]
    assert record["description"] == "人物·红裙女孩专属生图提示词：宝蓝色长裙、短发、银项链"


# ---------------------------------------------------------------------------
# AC3：组装开跑前入参已在会话状态里
# ---------------------------------------------------------------------------

def test_video_output_is_on_disk_before_assembly(tmp_path, monkeypatch):
    """组装（最贵一步）中途中断：视频输出三元组已在会话里，续跑不再重算/重问。"""
    def boom(state, brief, config, **kwargs):
        raise KeyboardInterrupt

    asked = _start_ref_run(tmp_path, monkeypatch, stage2=boom)
    with pytest.raises(KeyboardInterrupt):
        _run(tmp_path, monkeypatch, _fresh(["", "4", "", "4", "", "4", "4"]), asked)

    session = load_session(_session_dir(tmp_path))
    params = session.brief.video_output
    assert params is not None, "组装前没把视频输出参数写进会话（中断即丢）"
    assert params.size[0] > 0 and params.size[1] > 0


def test_frame_inputs_are_on_disk_before_assembly(tmp_path, monkeypatch):
    """首尾帧路线的同一道口：帧图路径与读图结果必须在 run_stage2 之前落盘。"""
    brief = Brief(mode="base", variant="I2VA", duration=18.0, style="写实", plot="便利店橘猫")
    generation_dir = tmp_path / "GEN001"
    save_session(generation_dir, brief, {"shot_table": "[Shot 1] 深夜空店内……"},
                 status=STATUS_AWAITING_FRAMES)
    session = load_session(generation_dir)
    image = tmp_path / "first.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\nfake-first-frame")

    _silence(monkeypatch)
    monkeypatch.setattr(wizard, "_collect_frame_paths", lambda variant: (str(image), ""))
    monkeypatch.setattr(frame_auditor, "audit_frame_images", lambda refs, variant, **kw: [
        frame_auditor.FrameAudit(picture=1, role="first", path=str(image), description="一帧画面")])
    monkeypatch.setattr(wizard, "run_stage2", lambda *a, **k: (_ for _ in ()).throw(KeyboardInterrupt()))

    with pytest.raises(KeyboardInterrupt):
        wizard._phase2_collect_and_finish(_cfg(tmp_path), session)

    reloaded = load_session(generation_dir)
    assert reloaded.stage_state["fl2va_frame_descriptions"], "帧读图结果没在组装前落盘"
    assert reloaded.stage_state["frame_images"], "帧图入库记录没在组装前落盘"


def test_frame_paths_and_descriptions_are_reused_after_assembly_interrupt(tmp_path, monkeypatch):
    """续跑不再问帧图路径、也不再重付读图：同一批帧图（sha 未变）直接沿用。"""
    brief = Brief(mode="base", variant="I2VA", duration=6.0, style="写实", plot="便利店橘猫")
    generation_dir = tmp_path / "GEN001"
    save_session(generation_dir, brief, {"shot_table": "[Shot 1] 深夜空店内……"},
                 status=STATUS_AWAITING_FRAMES)
    image = tmp_path / "first.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\nfake-first-frame")

    _silence(monkeypatch)
    asked_paths: list[str] = []
    monkeypatch.setattr(wizard, "_collect_frame_paths",
                        lambda variant: asked_paths.append(variant) or (str(image), ""))
    audits: list[int] = []
    monkeypatch.setattr(frame_auditor, "audit_frame_images",
                        lambda refs, variant, **kw: audits.append(1) or [
                            frame_auditor.FrameAudit(picture=1, role="first",
                                                     path=str(image), description="一帧画面")])
    monkeypatch.setattr(wizard, "run_stage2", lambda *a, **k: (_ for _ in ()).throw(KeyboardInterrupt()))

    with pytest.raises(KeyboardInterrupt):
        wizard._phase2_collect_and_finish(_cfg(tmp_path), load_session(generation_dir))

    # 续跑：第一次的落盘已经够用
    monkeypatch.setattr(wizard, "run_stage2",
                        lambda state, brief, config, **kw: (dict(state), "PROMPT"))
    monkeypatch.setattr(wizard, "_load_or_make_summary", lambda *a, **k: None)
    assert wizard._phase2_collect_and_finish(_cfg(tmp_path), load_session(generation_dir)) == 0

    assert len(asked_paths) == 1, "续跑又问了一遍帧图路径"
    assert len(audits) == 1, "续跑又付了一次读图（帧图没换，结果本该沿用）"


def test_pending_regen_resume_asks_for_the_new_image(tmp_path, monkeypatch):
    """裁了「重生成」就断线：续跑直接要新图（回审查菜单），不再重问重掷菜单。"""
    vision = {"红裙女孩":_claims(deviations=[("裙色", "红裙", "宝蓝色长裙")])}
    asked = _start_ref_run(tmp_path, monkeypatch, vision=vision)
    girl = _write_source(tmp_path, "girl.png", b"girl")

    with pytest.raises(KeyboardInterrupt):  # 裁「重生成」后断在重掷菜单
        _run(tmp_path, monkeypatch, _fresh(["", "1", str(girl), "2"]), asked)

    ledger_dir = _session_dir(tmp_path)
    record = load_session(ledger_dir).stage_state["asset_proofreads"]["红裙女孩"]
    assert record["status"] == "pending_regen"

    monkeypatch.undo()
    boundary = len(asked)
    asked = _start_ref_run(tmp_path, monkeypatch, vision=vision, asked=asked)
    girl2 = _write_source(tmp_path, "girl2.png", b"girl-v2")
    monkeypatch.setattr(wizard, "_offer_resume", lambda config: load_session(ledger_dir))
    _fake_confirms(monkeypatch, [("视频/音频", False), ("用新版替换登记", True)])
    # 审查满意 → 交回重掷后的新图 → 新版裁决「明知故犯」 → 其余三张放弃
    tail = ["1", str(girl2), "3", "", "4", "", "4", "4"]
    code = _run(tmp_path, monkeypatch, tail, asked)

    assert code == 0
    resumed = "\n".join(asked[boundary:])
    assert "原词再掷" not in resumed, "续跑把已经做过的重掷选择又问了一遍"
    assert "审查·红裙女孩" in resumed, "续跑没把「交回新图」这条路交出来"
    assert load_asset_ledger(ledger_dir).get("红裙女孩", "picture").source == str(girl2)
    assert load_session(ledger_dir).stage_state["asset_proofreads"]["红裙女孩"]["status"] == "done"


def test_intaken_promptless_asset_is_not_asked_again(tmp_path, monkeypatch):
    """无生图提示词的登记项已入库 → 续跑不再问路径（AC2 的另一条素材入口）。"""
    from minimax_h3_prompt.asset_ledger import AssetSubmission, intake_assets
    from minimax_h3_prompt.brief_parser import RefItem

    generation_dir = tmp_path / "GEN001"
    generation_dir.mkdir()
    cam = _write_source(tmp_path, "cam.png", b"cam")
    intake_assets(generation_dir, [AssetSubmission("运镜参考", "picture", str(cam))])
    brief = Brief(mode="ref", variant="T2VA", duration=5.0, plot=TOPIC,
                  refs=[RefItem(picture=1, name="运镜参考", description="")])

    _silence(monkeypatch)
    monkeypatch.setattr(wizard, "_confirm", lambda *a, **k: False)
    asked = _fake_inputs(monkeypatch, [])
    wizard._collect_extra_media({}, brief, generation_dir)

    assert not any("素材图路径" in text for text in asked), "已入库的登记项又被问了一遍路径"

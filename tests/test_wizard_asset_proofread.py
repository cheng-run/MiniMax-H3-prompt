"""向导·读图校对接线（issue #44）：对照器清单、偏差三选、裁决并入素材画面事实。

薄接线：走真实调用链（``run_wizard`` / ``_phase2_ref_finish``），只打桩视觉读图、
LLM 与交互输入。对照器纯函数层在 ``tests/test_asset_proofread.py``。

票面 AC2 的牙齿在这里：**偏差必问**（不静默放行）、**「重生成」不进片**（run_stage2
不跑、会话停在等交回）——生图跑偏时不静默把废图钉进成片。
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from minimax_h3_prompt import model_factory
from minimax_h3_prompt.asset_ledger import load_asset_ledger
from minimax_h3_prompt.brief_parser import Brief
from minimax_h3_prompt.session_store import (
    find_awaiting_sessions,
    load_session,
    save_session,
    STATUS_AWAITING_FRAMES,
)
from minimax_h3_prompt.tools import reference_auditor
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

# 生图提示词里的可锚原文（对照器引文必须逐字命中它，才能「只改那一处」）
GIRL_PROMPT = "人物·红裙女孩专属生图提示词：红裙、黑发、银项链"


@pytest.fixture(autouse=True)
def _isolate_injection_card(monkeypatch):
    """注入卡（issue #45）不属本文件考题：读图校对→注入卡在 `_phase2_ref_finish`
    里前后脚，打成空操作免得校对考题捎带卡落盘/推导断言——「推导→卡」由
    test_wizard_injection_card 专测钉住（issue #49 后它不再吃交互输入）。
    """
    monkeypatch.setattr(wizard, "_deliver_injection_card", lambda *a, **k: None)


def _silence(monkeypatch) -> None:
    monkeypatch.setattr(wizard, "_drain_stdin", lambda: None)
    monkeypatch.setattr(wizard.sys, "stdin", SimpleNamespace(isatty=lambda: True))


def _fake_inputs(monkeypatch, inputs: list[str]) -> list[str]:
    prompts = iter(inputs)
    asked: list[str] = []

    def fake_prompt(text: str) -> str:
        asked.append(text)
        try:
            return next(prompts)
        except StopIteration:
            # 逐段陪跑的交回口（issue #57 起 ≤15 秒直出也走这一套收尾）：喂完输入就
            # 停在这里。本文件钉的不是收尾，让 StopIteration 冒出去只会冒充失败。
            if "输出视频路径" in text:
                return ""
            raise

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
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _stub_stage2(monkeypatch, captured: dict | None = None) -> None:
    def fake_run_stage2(state, brief, config, **kwargs):
        if captured is not None:
            captured["state"] = dict(state)
            captured["brief"] = brief
        return dict(state), _SIX_SECTION_PROMPT

    monkeypatch.setattr(wizard, "run_stage2", fake_run_stage2)


def _stub_stage1(monkeypatch) -> None:
    monkeypatch.setattr(
        wizard, "run_stage1",
        lambda brief, config, **kw: (_ref_stage1_state(), object(),
                                     {"prompt_engineer": object()}),
    )


def _stub_describe(monkeypatch, mapping: dict[str, str],
                   calls: list[str] | None = None) -> list[str]:
    """按正本文件名（picture-NN）路由读图结果；记录调用过的路径。"""
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


def _run_one_picture_flow(tmp_path, monkeypatch, reading: str,
                          extra_inputs: list[str], *,
                          confirms: list[tuple[str, bool]] | None = None,
                          run2_captured: dict | None = None):
    """全程：素材循环里只交回红裙女孩一张图（picture-01，队首），其余三张显式放弃。

    ``extra_inputs`` 落在「交回之后、校对裁决之间」——三选/补锚等答问按序消耗。
    返回 ``(asked, code, calls)``。
    """
    _silence(monkeypatch)
    monkeypatch.setattr(wizard, "_choose_canvases", lambda: {})
    _stub_stage1(monkeypatch)
    _stub_stage2(monkeypatch, run2_captured)
    monkeypatch.setattr(model_factory, "build_chat_model", lambda: object())
    calls = _stub_describe(monkeypatch, {"picture-01": reading})
    girl = _write_source(tmp_path, "girl.png", b"girl")
    # 主题/时长/风格/方式 → 素材循环：女孩（出词/审查/路径）→ 裁决答问 → 其余三张放弃
    loop = ["", "1", str(girl)] + list(extra_inputs) + ["", "4", "", "4", "4"]
    asked = _fake_inputs(monkeypatch, [TOPIC, "", "", "4"] + loop + ["8", "1"])
    _fake_confirms(monkeypatch, confirms or [("视频/音频", False)])
    code = wizard.run_wizard(_cfg(tmp_path))
    return asked, code, calls


# ---------------------------------------------------------------------------
# AC2：偏差必问三选，默认建议「按图改文案」；默认路径并入素材画面事实
# ---------------------------------------------------------------------------

def test_deviation_three_way_defaults_to_rewrite_and_merges(tmp_path, monkeypatch):
    """回车=按图改文案：只改引文那一处 + 补漏并入，其余细节逐字保留（AC3/AC4）。"""
    reading = _claims(deviations=[("裙色", "红裙", "宝蓝色长裙")],
                      omissions=[("腕表", "左腕银色腕表")])
    captured: dict = {}
    asked, code, _calls = _run_one_picture_flow(
        tmp_path, monkeypatch, reading,
        extra_inputs=["", ""],  # 裁决回车=按图改文案；替换文案回车=用图中实见
        run2_captured=captured)

    assert code == 0
    assert any("按图改文案" in text for text in asked), "三选没问（默认建议缺席）"
    assert any("重生成这张素材图" in text for text in asked), "三选没给全"
    assert any("明知故犯" in text for text in asked), "三选没给全"

    record = captured["state"]["asset_proofreads"]["红裙女孩"]
    # 纠偏差只改那一处：正文「红裙」→「宝蓝色长裙」，语义名与其余细节逐字在场
    assert record["description"] == (
        "人物·红裙女孩专属生图提示词：宝蓝色长裙、黑发、银项链；左腕银色腕表"
    )
    assert record["deviations"][0]["decision"] == "rewrite"
    assert record["status"] == "done"


def test_rewrite_spares_semantic_name_when_quote_hits_both(tmp_path, monkeypatch):
    """引文「红裙」在语义名与正文各一处：只改正文那处、名字不动——语义名是标识符
    （registry 的 name 键）不是偏差处，改它就与登记名失同步（评审点名的缺陷场景）。"""
    reading = _claims(deviations=[("裙子", "红裙", "宝蓝色长裙")])
    captured: dict = {}
    _asked, code, _ = _run_one_picture_flow(
        tmp_path, monkeypatch, reading, extra_inputs=["", ""], run2_captured=captured)
    desc = captured["state"]["asset_proofreads"]["红裙女孩"]["description"]
    assert code == 0
    assert desc == "人物·红裙女孩专属生图提示词：宝蓝色长裙、黑发、银项链"


# ---------------------------------------------------------------------------
# 失锚补锚：三选必须走得到底（评审——失锚「按图改文案」路径名不副实）
# ---------------------------------------------------------------------------

def test_unanchored_quote_prompts_reanchor_and_rewrite_lands(tmp_path, monkeypatch):
    """读图改写了措辞（引文对不上原文）→ 引导补锚而不是崩溃；补锚后定点改落得下。"""
    reading = _claims(deviations=[("裙色", "红色的长裙", "宝蓝色长裙")])
    captured: dict = {}
    asked, code, _ = _run_one_picture_flow(
        tmp_path, monkeypatch, reading,
        extra_inputs=["", "红裙", ""],  # 裁决=改文案；补锚引文=红裙；替换回车=图中实见
        run2_captured=captured)
    assert code == 0
    assert any("补锚" in text for text in asked), "失锚没引导补锚——三选走不到底"
    desc = captured["state"]["asset_proofreads"]["红裙女孩"]["description"]
    assert desc == "人物·红裙女孩专属生图提示词：宝蓝色长裙、黑发、银项链"
    assert captured["state"]["asset_proofreads"]["红裙女孩"]["deviations"][0]["prompt_quote"] == "红裙"


def test_reanchor_abandon_returns_to_three_way(tmp_path, monkeypatch):
    """补锚回车＝放弃改文案、回三选改选——人始终能完成裁决，系统不替人硬改也不崩。"""
    reading = _claims(deviations=[("裙色", "红色的长裙", "宝蓝色长裙")])
    captured: dict = {}
    asked, code, _ = _run_one_picture_flow(
        tmp_path, monkeypatch, reading,
        extra_inputs=["", "", "3"],  # 裁决=改文案；补锚回车=放弃；重问裁决=明知故犯
        run2_captured=captured)
    assert code == 0
    assert sum(1 for t in asked if "按图改文案" in t) == 2, "放弃补锚后没回三选"
    record = captured["state"]["asset_proofreads"]["红裙女孩"]
    assert record["description"] == GIRL_PROMPT
    assert record["deviations"][0]["decision"] == "keep"


# ---------------------------------------------------------------------------
# 逐张落盘：中途崩溃不丢前几张已裁决的账（评审——失锚崩溃连带丢裁决）
# ---------------------------------------------------------------------------

def test_mid_adjudication_crash_keeps_earlier_records(tmp_path, monkeypatch):
    """前一张已裁决完、后一张裁决中途炸——前一张的记录必须已在会话里，
    不许随 traceback 一起丢（裁决逐张落盘）。"""
    _silence(monkeypatch)
    monkeypatch.setattr(wizard, "_choose_canvases", lambda: {})
    _stub_stage1(monkeypatch)
    ran = {"ok": False}
    monkeypatch.setattr(wizard, "run_stage2",
                        lambda *a, **k: ran.__setitem__("ok", True) or (dict(a[0]), ""))
    monkeypatch.setattr(model_factory, "build_chat_model", lambda: object())
    # picture-01 干净读数（直接出记录）；picture-03 有偏差（进三选时炸）
    _stub_describe(monkeypatch, {
        "picture-01": '{"claims": []}',
        "picture-03": _claims(deviations=[("裙色", "红裙", "宝蓝色长裙")]),
    })
    girl = _write_source(tmp_path, "girl.png", b"girl")
    key = _write_source(tmp_path, "key.png", b"key")
    # 素材循环（issue #66 纯 Picture 序）：红裙女孩→girl（picture-01，干净落账）；
    # 秋日庭院放弃；青铜钥匙→key（picture-03）→ 三选时炸
    wire = iter([TOPIC, "", "", "4", "", "1", str(girl),
                 "", "4", "1", "1", str(key)])

    def boom_on_adjudication(text: str) -> str:
        if "裁决" in text:
            raise RuntimeError("模拟中途崩溃")
        return next(wire)

    monkeypatch.setattr(wizard, "_prompt", boom_on_adjudication)
    _fake_confirms(monkeypatch, [("视频/音频", False)])

    with pytest.raises(RuntimeError):
        wizard.run_wizard(_cfg(tmp_path))

    assert not ran["ok"]
    sessions = find_awaiting_sessions(tmp_path / "sessions")
    assert sessions, "裁决逐张没落盘——前一张的账随崩溃丢了"
    record = sessions[0].stage_state["asset_proofreads"]["红裙女孩"]
    assert record["status"] == "done"


def test_keep_decision_preserves_original_description(tmp_path, monkeypatch):
    reading = _claims(deviations=[("裙色", "红裙", "宝蓝色长裙")])
    captured: dict = {}
    _asked, code, _ = _run_one_picture_flow(
        tmp_path, monkeypatch, reading, extra_inputs=["3"], run2_captured=captured)
    record = captured["state"]["asset_proofreads"]["红裙女孩"]
    assert record["description"] == GIRL_PROMPT  # 明知故犯：一字不动
    assert record["deviations"][0]["decision"] == "keep"
    assert code == 0


def test_clean_reading_asks_no_adjudication_and_proceeds(tmp_path, monkeypatch):
    """无偏差无补漏：不问裁决、直接出提示词——干净图不添无谓一问。"""
    asked, code, _ = _run_one_picture_flow(
        tmp_path, monkeypatch, _claims(), extra_inputs=[])
    assert code == 0
    assert not any("按图改文案" in text for text in asked)
    assert list((tmp_path / "sessions").rglob("video-prompt.md"))


# ---------------------------------------------------------------------------
# AC2 牙齿：「重生成这张素材图」＝不进片
# ---------------------------------------------------------------------------

def test_regenerate_choice_blocks_stage2_and_stays_resumable(tmp_path, monkeypatch, capsys):
    """选「重生成」后先到这里（q）→ run_stage2 不跑、video-prompt.md 不写、会话可续接。"""
    reading = _claims(deviations=[("裙色", "红裙", "宝蓝色长裙")])
    captured: dict = {"ran": False}

    def fake_run_stage2(state, brief, config, **kwargs):
        captured["ran"] = True
        return dict(state), _SIX_SECTION_PROMPT

    _silence(monkeypatch)
    monkeypatch.setattr(wizard, "_choose_canvases", lambda: {})
    _stub_stage1(monkeypatch)
    monkeypatch.setattr(wizard, "run_stage2", fake_run_stage2)
    monkeypatch.setattr(model_factory, "build_chat_model", lambda: object())
    _stub_describe(monkeypatch, {"picture-01": reading})
    girl = _write_source(tmp_path, "girl.png", b"girl")
    # 素材循环：女孩交回 → 裁决=重生成 → 重生成菜单 q=先到这里（issue #50）
    asked = _fake_inputs(monkeypatch, [TOPIC, "", "", "4", "", "1", str(girl), "2", "q"])
    _fake_confirms(monkeypatch, [("视频/音频", False)])

    code = wizard.run_wizard(_cfg(tmp_path))

    assert code == 0
    assert not captured["ran"], "「重生成」被绕过——废图钉进成片了"
    assert not list((tmp_path / "sessions").rglob("video-prompt.md"))
    assert any("重生成" in text for text in asked), "重生成裁决没有可见交代"
    capsys.readouterr()
    assert find_awaiting_sessions(tmp_path / "sessions"), "会话必须停在可续接态"
    # 裁决记录在场：重跑续接时按 sha 复用/重问，不丢
    session = find_awaiting_sessions(tmp_path / "sessions")[0]
    record = session.stage_state["asset_proofreads"]["红裙女孩"]
    assert record["status"] == "pending_regen"
    assert record["deviations"][0]["decision"] == "regenerate"


def test_regenerate_skips_omission_merge_for_that_image(tmp_path, monkeypatch):
    """图要重画，本轮补漏不并入（那是旧图的事实）——新图交回后重新校对。"""
    reading = _claims(deviations=[("裙色", "红裙", "宝蓝色长裙")],
                      omissions=[("腕表", "左腕银色腕表")])
    # 裁决=重生成（2）→ 重生成菜单 q=先到这里收尾
    _asked, _code, _ = _run_one_picture_flow(
        tmp_path, monkeypatch, reading, extra_inputs=["2", "q"])
    # 走到这里即证明补漏确认没问（重掷的图标废，旧图的补漏不该并入）
    session = find_awaiting_sessions(tmp_path / "sessions")[0]
    record = session.stage_state["asset_proofreads"]["红裙女孩"]
    assert record["status"] == "pending_regen"
    assert not record["omissions"], "重生成图中途仍并入了旧图的补漏事实"


# ---------------------------------------------------------------------------
# AC3：裁决并入素材画面事实，下游提示词写作消费它
# ---------------------------------------------------------------------------

def test_merged_description_reaches_prompt_engineer_request(tmp_path, monkeypatch):
    """真过一遍图：合并后的素材画面事实必须出现在【参考资产】请求里。

    删掉 ``PipelineState`` 的 ``asset_proofreads`` 声明，本用例必须红——
    LangGraph 只搬运声明过的键（issue #28 同款陷阱）。
    """
    from minimax_h3_prompt.graph import nodes, pipeline, roundtable

    merged = "宝蓝色长裙、黑发的素材画面事实；左腕银色腕表"
    brief = Brief(mode="ref", variant="T2VA", duration=5.0, plot=TOPIC)
    state = _ref_stage1_state()
    state["asset_proofreads"] = {
        "红裙女孩": {"sha256": "ab" * 32, "status": "done", "description": merged,
                    "deviations": [], "omissions": []},
    }
    requests: list[str] = []

    def fake_run_agent(agent, message):
        requests.append(message)
        if message.startswith("请按官方规范组装最终 H3 提示词"):
            return _SIX_SECTION_PROMPT
        return "上游产物正文"

    monkeypatch.setattr(nodes, "run_agent", fake_run_agent)
    monkeypatch.setattr(pipeline, "run_agent", fake_run_agent)
    monkeypatch.setattr(roundtable, "_invoke", lambda role, model, msgs: "已锁定")
    monkeypatch.setattr(model_factory, "build_chat_model", lambda: object())
    monkeypatch.setattr(pipeline.stage_saver, "base", tmp_path / "stages")
    config = SimpleNamespace(roundtable_max_rounds=1, common_sense_qa=False,
                             max_qa_iterations=2)

    _final_state, prompt = pipeline.run_stage2(state, brief, config, model=object())

    assert prompt
    ref_request = next(r for r in requests if r.startswith("请按官方规范组装最终 H3 提示词"))
    assert merged in ref_request, "素材画面事实没进下游请求——PipelineState 没声明这个键？"


def test_registry_description_prefers_merged_over_prompt():
    """取材口单测在 test_generation；这里钉向导写入的记录形状能被它吃。"""
    from minimax_h3_prompt.generation import ref_asset_registry

    state = _ref_stage1_state()
    state["asset_proofreads"] = {
        "红裙女孩": {"sha256": "cd" * 32, "status": "done",
                    "description": "合并后的素材画面事实", "deviations": [], "omissions": []},
    }
    registry = ref_asset_registry(state)
    by_name = {r.name: r.description for r in registry}
    assert by_name["红裙女孩"] == "合并后的素材画面事实"
    assert by_name["青铜钥匙"] == "道具·青铜钥匙专属生图提示词"


# ---------------------------------------------------------------------------
# 续接：已裁决同版不重问；换版（sha 变）重新校对
# ---------------------------------------------------------------------------

def test_done_record_at_same_sha_skips_reread(tmp_path, monkeypatch):
    """同 sha 已裁决 → 续接不再出这张的词、不再读图（防重跑把裁决再问一遍）。"""
    _silence(monkeypatch)
    girl = _write_source(tmp_path, "girl.png", b"girl")
    monkeypatch.setattr(wizard, "_choose_canvases", lambda: {})
    _stub_stage1(monkeypatch)
    _stub_stage2(monkeypatch)
    monkeypatch.setattr(model_factory, "build_chat_model", lambda: object())
    calls = _stub_describe(monkeypatch, {"picture-01": '{"claims": []}'})
    # 首跑：女孩交回落账，其余三张放弃
    _fake_inputs(monkeypatch, [TOPIC, "", "", "4", "", "1", str(girl),
                               "", "4", "", "4", "4", "8", "1"])
    _fake_confirms(monkeypatch, [("视频/音频", False)])
    wizard.run_wizard(_cfg(tmp_path))
    assert len(calls) == 1

    # 手工把会话改回「可续接」态重进，验证跳过逻辑
    ledger_dir = next((tmp_path / "sessions").rglob("asset-ledger.json")).parent
    entry = load_asset_ledger(ledger_dir).entries[0]
    session = load_session(ledger_dir)
    assert session is not None
    save_session(ledger_dir, session.brief, session.stage_state, status=STATUS_AWAITING_FRAMES)
    session = load_session(ledger_dir)
    monkeypatch.setattr(wizard, "_offer_resume", lambda config: session)
    calls.clear()
    # 已落账的红裙女孩不再进队列——续接只剩三张待办，全部放弃收尾
    _fake_inputs(monkeypatch, ["", "4", "", "4", "4", "8", "1"])
    _fake_confirms(monkeypatch, [("视频/音频", False)])
    wizard.run_wizard(_cfg(tmp_path))
    assert not calls, "同 sha 已裁决还重读图——裁决被重复劳动/重问"
    assert entry.sha256


def test_new_sha_rereads_and_readjudicates(tmp_path, monkeypatch):
    """重生成后换版交回（正本 sha 变）→ 重新读图裁决；旧裁决不冒充新版的。"""
    _silence(monkeypatch)
    girl = _write_source(tmp_path, "girl.png", b"girl")
    girl2 = _write_source(tmp_path, "girl2.png", b"girl-v2")
    monkeypatch.setattr(wizard, "_choose_canvases", lambda: {})
    _stub_stage1(monkeypatch)
    captured: dict = {}
    _stub_stage2(monkeypatch, captured)
    monkeypatch.setattr(model_factory, "build_chat_model", lambda: object())
    # 两轮都读出同一条偏差：第一轮裁重生成，第二轮裁明知故犯收尾
    calls = _stub_describe(monkeypatch, {"picture-01": _claims(
        deviations=[("裙色", "红裙", "宝蓝色长裙")])})
    _fake_inputs(monkeypatch, [TOPIC, "", "", "4",
                               "", "1", str(girl),   # 交回第一版
                               "2", "",              # 裁决重生成 → 原词再掷
                               "1", str(girl2),      # 换版交回新版（sha 变）
                               "3",                  # 明知故犯保住原描述
                               "", "4", "", "4", "4", "8", "1"])
    _fake_confirms(monkeypatch, [("换版", True), ("视频/音频", False)])
    wizard.run_wizard(_cfg(tmp_path))
    assert len(calls) == 2, "换版后没重新读图——旧裁决冒充了新版"
    record = captured["state"]["asset_proofreads"]["红裙女孩"]
    assert record["status"] == "done"
    ledger_dir = next((tmp_path / "sessions").rglob("asset-ledger.json")).parent
    entry = load_asset_ledger(ledger_dir).get("红裙女孩", "picture")
    assert record["sha256"] == entry.sha256, "裁决钉的不是换版后的正本"


# ---------------------------------------------------------------------------
# 读图失败要响，不静默跳过（红线：图跑偏无人纠）
# ---------------------------------------------------------------------------

def test_reading_failure_pauses_instead_of_skipping(tmp_path, monkeypatch, capsys):
    def boom(path, instruction=None):
        raise RuntimeError("视觉模型不可用")

    _silence(monkeypatch)
    monkeypatch.setattr(wizard, "_choose_canvases", lambda: {})
    _stub_stage1(monkeypatch)
    ran = {"ok": False}
    monkeypatch.setattr(wizard, "run_stage2",
                        lambda *a, **k: ran.__setitem__("ok", True) or (dict(a[0]), ""))
    monkeypatch.setattr(model_factory, "build_chat_model", lambda: object())
    monkeypatch.setattr(reference_auditor, "describe_image", boom)
    girl = _write_source(tmp_path, "girl.png", b"girl")
    # 素材循环：女孩交回后读图失败 → 停在可续接态（后续答问不再消耗）
    _fake_inputs(monkeypatch, [TOPIC, "", "", "4", "", "1", str(girl)])
    _fake_confirms(monkeypatch, [("视频/音频", False)])

    wizard.run_wizard(_cfg(tmp_path))

    assert not ran["ok"], "读图失败却继续出提示词——跳过了校对"
    assert "读图" in capsys.readouterr().out

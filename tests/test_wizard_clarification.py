"""起步前置澄清：问得到、跳过得了、结论真的进了生成上下文（issue #29 / T7）。

票面五条验收标准：提问数量不超过上限 / 一句话跳过即直入原流程 / 上限改了立即生效
（默认 3）/ 结论进入后续生成的上下文且**不**拼进主题字段 / 跳过与不跳过都能跑完阶段 1。

本文件走**真实调用链**（``_phase1_new`` + 真节点），只打桩 LLM、交互输入与阶段 1 管线，
断言全落在**外部产物**上：发给模型的请求文本、落盘的会话与 brief。
纯函数层在 ``tests/test_clarification.py``。
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Callable

import pytest

from minimax_h3_prompt import clarification, config as config_module, model_factory
from minimax_h3_prompt.brief_parser import Brief
from minimax_h3_prompt.graph import nodes
from minimax_h3_prompt.session_store import load_session
from minimax_h3_prompt.ui import wizard

TOPIC = "秋日庭院里的橘猫"
QUESTIONS = ["庭院是哪个朝代？", "人物服装按哪个年代？"]
ANSWER_1 = "北宋"
ANSWER_2 = "参考现代汉服审美，颜值要高"
CLARIFY_LABEL = clarification.BLOCK_LABEL
FAKE_LLM_ERROR = "网关 500：上游服务不可用"


# ---------------------------------------------------------------------------
# 打桩件
# ---------------------------------------------------------------------------

class _QuestionLLM:
    """假的问答模型：``content`` 是它「生成」的问题清单，``error`` 非空则抛异常。"""

    def __init__(self, content: str = "", error: Exception | None = None) -> None:
        self.content = content
        self.error = error
        self.requests: list[str] = []

    def invoke(self, request: str):
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return SimpleNamespace(content=self.content)


def _bundle_payload(label: str = "初版") -> dict:
    """I2VA 单帧产物（与 test_wizard_revision_loop 同形）。"""
    return {
        "scene_anchor": "秋日庭院",
        "first": {"zimage": {
            "positive_prompt": f"秋日庭院里的橘猫（{label}）",
            "instructions": ["paste to node 187"],
        }},
        "continuity_constraints": ["保持同一只橘猫与同一庭院"],
    }


def _run_phase1(tmp_path, monkeypatch, *, inputs: list[str],
                questions: tuple[str, ...] = tuple(QUESTIONS),
                llm: _QuestionLLM | None = None,
                config: object | None = None) -> tuple[object, dict, _QuestionLLM, list[str]]:
    """跑一遍 ``_phase1_new``。返回 (session, 阶段 1 收到的 brief 快照, 问答模型, 问过的提示语)。"""
    prompts = iter(inputs)
    asked: list[str] = []

    def fake_prompt(text: str) -> str:
        asked.append(text)
        return next(prompts)

    monkeypatch.setattr(wizard, "_prompt", fake_prompt)
    monkeypatch.setattr(wizard, "_confirm", lambda *a, **k: False)
    monkeypatch.setattr(wizard, "_drain_stdin", lambda: None)
    stub_llm = llm or _QuestionLLM("\n".join(questions))
    monkeypatch.setattr(model_factory, "build_chat_model", lambda: stub_llm)

    captured: dict = {}

    def fake_run_stage1(brief, config, **kwargs):
        captured["brief"] = brief
        return (
            {"fl2va_prompt_bundle": _bundle_payload(), "shot_table": "[Shot 1] 庭院空镜"},
            object(),
            {"frame_prompt_engineer": object()},
        )

    monkeypatch.setattr(wizard, "run_stage1", fake_run_stage1)
    cfg = config or SimpleNamespace(
        default_duration=5.0, default_language="Chinese",
        sessions_root=tmp_path / "sessions", max_clarification_rounds=3,
    )
    return wizard._phase1_new(cfg), captured, stub_llm, asked


def _answer_path_inputs() -> list[str]:
    # 主题 → 两条澄清回答 → 时长（默认）→ 风格（AI 定）→ 生成方式（1 首帧）
    return [TOPIC, ANSWER_1, ANSWER_2, "", "", "1"]


def _skip_path_inputs() -> list[str]:
    return [TOPIC, "跳过", "", "", "1"]


# ---------------------------------------------------------------------------
# 验收：提问 / 跳过 / 上限
# ---------------------------------------------------------------------------

def test_clarification_questions_are_asked_before_generating(tmp_path, monkeypatch):
    """起步处出现澄清提问，数量不超过配置上限（上限 3，模型给 2 个）。"""
    session, _, llm, asked = _run_phase1(tmp_path, monkeypatch, inputs=_answer_path_inputs())

    clarify_prompts = [text for text in asked if "[澄清" in text]
    assert len(clarify_prompts) == len(QUESTIONS)
    assert QUESTIONS[0] in clarify_prompts[0]
    assert QUESTIONS[1] in clarify_prompts[1]
    # 澄清发生在生成之前：第一句提问就是主题，接着就是澄清，之后才是时长
    assert asked[0].startswith("请输入视频主题")
    assert asked[1].startswith("[澄清 1/2]")
    assert "视频时长" in asked[3]
    assert llm.requests, "上限与主题没有进澄清提问的请求——模型无从依主题提问"


def test_answers_land_in_clarifications_and_never_in_plot(tmp_path, monkeypatch):
    """结论进的是独立字段：主题字段仍然只装主题（它会喂两条闸门的判据）。"""
    session, captured, _, _ = _run_phase1(tmp_path, monkeypatch, inputs=_answer_path_inputs())

    assert session is not None
    brief = captured["brief"]
    block = brief.clarifications
    assert ANSWER_1 in block and ANSWER_2 in block
    assert QUESTIONS[0] in block, "澄清块要带上问题，否则结论没有上下文"
    assert brief.plot == TOPIC
    assert ANSWER_1 not in brief.plot and ANSWER_2 not in brief.plot

    raw = json.loads((session.directory / "session-state.json").read_text(encoding="utf-8"))
    assert raw["brief"]["plot"] == TOPIC, "落盘的 brief 里主题被澄清污染了"
    assert ANSWER_1 in raw["brief"]["clarifications"], \
        "澄清没进 brief 的持久化字段（重启后用户要重新答一遍）"


def test_skip_word_ends_questioning_and_goes_straight_to_original_flow(tmp_path, monkeypatch):
    """输入跳过即直入原流程，问题不再出现。"""
    session, captured, _, asked = _run_phase1(tmp_path, monkeypatch, inputs=_skip_path_inputs())

    assert session is not None
    clarify_prompts = [text for text in asked if "[澄清" in text]
    assert len(clarify_prompts) == 1, "按了跳过之后不该再问第二个问题"
    assert QUESTIONS[1] not in "".join(asked)
    assert captured["brief"].clarifications == ""
    # 「直入原流程」= 后续三个参数问句照旧出现
    assert any("视频时长" in text for text in asked)
    assert any("视觉风格" in text for text in asked)
    assert any("1/2/3" in text for text in asked)


def test_skip_after_one_answer_keeps_what_was_already_answered(tmp_path, monkeypatch):
    """答了一条再跳过：已答的那条照样生效（跳过只结束「还没问的」）。"""
    session, captured, _, _ = _run_phase1(
        tmp_path, monkeypatch, inputs=[TOPIC, ANSWER_1, "跳过", "", "", "1"])

    assert session is not None
    assert ANSWER_1 in captured["brief"].clarifications
    assert ANSWER_2 not in captured["brief"].clarifications


def test_unanswered_question_leaves_no_entry(tmp_path, monkeypatch):
    """回车=不回答这条：不该凭空造出一条「问 → （空）」的澄清。"""
    session, captured, _, _ = _run_phase1(
        tmp_path, monkeypatch, inputs=[TOPIC, "", ANSWER_2, "", "", "1"])

    assert session is not None
    assert QUESTIONS[0] not in captured["brief"].clarifications
    assert ANSWER_2 in captured["brief"].clarifications


def test_both_paths_finish_phase1(tmp_path, monkeypatch):
    """跳过路径与不跳过路径都能跑完阶段 1（都落到 awaiting_frames）。"""
    answered, _, _, _ = _run_phase1(tmp_path, monkeypatch, inputs=_answer_path_inputs())
    skipped, _, _, _ = _run_phase1(tmp_path / "second", monkeypatch, inputs=_skip_path_inputs())

    for session in (answered, skipped):
        assert session is not None, "阶段 1 没跑完，返回了 None"
        assert session.awaiting_frames, "阶段 1 没有走到 awaiting_frames"


def test_zero_limit_disables_clarification(tmp_path, monkeypatch):
    """上限 0 = 关闭：连模型都不调，用户的 token 不该被这一步花掉。"""
    config = SimpleNamespace(
        default_duration=5.0, default_language="Chinese",
        sessions_root=tmp_path / "sessions", max_clarification_rounds=0,
    )
    session, captured, llm, asked = _run_phase1(
        tmp_path, monkeypatch, inputs=[TOPIC, "", "", "1"], config=config)

    assert session is not None
    assert not [text for text in asked if "[澄清" in text]
    assert llm.requests == []
    assert captured["brief"].clarifications == ""


def test_cap_follows_config_file(tmp_path, monkeypatch):
    """上限配置项改了立即生效：换成配置里的 1，模型给两条也只问一条。"""
    yaml_path = tmp_path / "agent.yaml"
    yaml_path.write_text("pipeline:\n  max_clarification_rounds: 1\n", encoding="utf-8")
    monkeypatch.setattr(config_module, "CONFIG_PATH", yaml_path)
    monkeypatch.setattr(config_module, "load_dotenv", lambda *a, **k: None)
    cfg = config_module.Config()
    cfg.sessions_root = tmp_path / "sessions"  # 别写到项目 output/ 里去
    assert cfg.max_clarification_rounds == 1

    session, captured, _, asked = _run_phase1(
        tmp_path, monkeypatch, inputs=[TOPIC, ANSWER_1, "", "", "1"], config=cfg)

    assert session is not None
    assert len([text for text in asked if "[澄清" in text]) == 1
    assert ANSWER_2 not in captured["brief"].clarifications


def test_default_cap_is_three_and_shipped_config_carries_the_key(tmp_path, monkeypatch):
    """默认值 3，且出货配置里确实写着这一项（与质检轮数上限并列）。"""
    yaml_path = tmp_path / "agent.yaml"
    yaml_path.write_text("pipeline:\n  max_qa_iterations: 2\n", encoding="utf-8")
    monkeypatch.setattr(config_module, "CONFIG_PATH", yaml_path)
    monkeypatch.setattr(config_module, "load_dotenv", lambda *a, **k: None)
    assert config_module.Config().max_clarification_rounds == 3

    pipeline = config_module.config.raw.get("pipeline", {})
    assert "max_clarification_rounds" in pipeline, "出货的 agent.yaml 没写这一项，用户改不到"


def test_question_generation_failure_is_reported_not_silent(tmp_path, monkeypatch, capsys):
    """生成失败要报出来再跳过：可选前置不该炸掉整条流程，也不该静默。"""
    llm = _QuestionLLM(error=RuntimeError(FAKE_LLM_ERROR))
    session, captured, _, asked = _run_phase1(
        tmp_path, monkeypatch, inputs=[TOPIC, "", "", "1"], llm=llm)

    assert session is not None, "澄清生成失败把阶段 1 一起炸了"
    assert captured["brief"].clarifications == ""
    assert not [text for text in asked if "[澄清" in text], "没有问句却还是问了"
    out = capsys.readouterr().out
    assert FAKE_LLM_ERROR in out, "失败原因没报给用户（静默失败）"


def test_model_saying_no_questions_skips_without_asking(tmp_path, monkeypatch, capsys):
    """模型答「无需澄清」时不该把它当成一个问题去问用户。"""
    llm = _QuestionLLM("这个主题已经很清楚了，无需澄清。")
    session, captured, _, asked = _run_phase1(
        tmp_path, monkeypatch, inputs=[TOPIC, "", "", "1"], llm=llm)

    assert session is not None
    assert not [text for text in asked if "[澄清" in text]
    assert captured["brief"].clarifications == ""
    assert "直接进入原流程" in capsys.readouterr().out


def test_clarifications_survive_session_round_trip(tmp_path, monkeypatch):
    """会话 save/load 往返：brief 的字段是逐个显式列出的，漏一个就是静默丢弃。"""
    session, _, _, _ = _run_phase1(tmp_path, monkeypatch, inputs=_answer_path_inputs())
    assert session is not None

    loaded = load_session(session.directory)
    assert loaded is not None
    assert ANSWER_1 in loaded.brief.clarifications
    assert loaded.brief.clarifications == session.brief.clarifications


# ---------------------------------------------------------------------------
# 验收：结论进入后续生成的上下文（不只是打印）
# ---------------------------------------------------------------------------

_AGENTS = {
    key: object() for key in (
        "producer", "director", "screenwriter", "character_designer",
        "image_prompt_engineer", "art_director", "storyboard", "cinematographer",
        "frame_prompt_engineer", "prompt_engineer",
    )
}

_IMAGE_JSON = json.dumps({"zimage": {"positive_prompt": "庭院橘猫"},
                          "flux2": {"positive_prompt": "庭院橘猫 cinematic"}})

# (节点名, 造节点, 该节点要的 state 附加项, 假模型回复)
_TOPIC_NODE_CASES: list[tuple[str, Callable[[], Callable], dict, str]] = [
    ("producer", lambda: nodes._make_producer_node(_AGENTS), {}, "制作计划正文"),
    ("director", lambda: nodes._make_director_node(_AGENTS), {}, "导演阐述正文"),
    ("screenwriter", lambda: nodes._make_screenwriter_node(_AGENTS), {}, "剧本正文"),
    ("character_designer",
     lambda: nodes._make_design_node(_AGENTS, "character_designer", "character_design", "人物形象设计"),
     {}, "人物设计正文"),
    ("image_prompt_character",
     lambda: nodes._make_image_prompt_node(_AGENTS, "人物", "character_design", "character_image_prompts"),
     {}, _IMAGE_JSON),
    ("art_director", lambda: nodes._make_art_director_node(_AGENTS), {}, "美术统筹正文"),
    ("storyboard", lambda: nodes._make_storyboard_node(_AGENTS), {}, "[Shot 1] 庭院空镜，橘猫入画\n"),
    ("cinematographer", lambda: nodes._make_cinematographer_node(_AGENTS), {}, "画面细化正文"),
    ("fl2va_frame_prompts", lambda: nodes._make_fl2va_frame_prompt_node(_AGENTS),
     {}, json.dumps(_bundle_payload())),
    ("prompt_engineer", lambda: nodes._make_prompt_engineer_node(_AGENTS), {}, "最终提示词正文"),
]


def _state_for(brief: Brief) -> dict:
    return {"brief": brief, "variant": "I2VA", "duration": 5.0,
            "ref_meta": (), "shot_table": "[Shot 1] 庭院空镜"}


@pytest.mark.parametrize("case", _TOPIC_NODE_CASES, ids=[c[0] for c in _TOPIC_NODE_CASES])
def test_every_topic_node_carries_the_clarification_block(monkeypatch, case):
    """凡注入主题处都注入澄清（与主题并列成两块）——这是「不只是打印」的物证。"""
    name, factory, extra, reply = case
    seen: list[str] = []
    monkeypatch.setattr(nodes, "run_agent",
                        lambda agent, message: seen.append(message) or reply)
    block = clarification.render_clarification_block([(QUESTIONS[0], ANSWER_1)])
    brief = Brief(variant="I2VA", duration=5.0, plot=TOPIC, clarifications=block)

    factory()({**_state_for(brief), **extra})

    assert seen, f"{name} 没有调用模型"
    request = seen[0]
    assert CLARIFY_LABEL in request, f"{name} 的请求里没有澄清块"
    assert ANSWER_1 in request, f"{name} 的请求里澄清块是空的"
    assert "原始" in request or "剧情" in request or "主题" in request, \
        f"{name} 的请求里澄清块没有与主题并列"


def test_roundtable_contexts_carry_the_clarification_block():
    """两场圆桌（创意 / 镜头评审）同样拿主题，也必须有澄清块。"""
    block = clarification.render_clarification_block([(QUESTIONS[0], ANSWER_1)])
    brief = Brief(variant="I2VA", duration=5.0, plot=TOPIC, clarifications=block)
    captured: dict = {}

    class _StubRoundtable:
        def invoke(self, payload):
            captured.update(payload)
            return {"lock": "已锁定"}

    for maker in (nodes.make_creative_rt_node, nodes.make_shot_rt_node):
        captured.clear()
        maker(_StubRoundtable(), object(), brief)(_state_for(brief))
        context = captured["context"]
        assert CLARIFY_LABEL in context and ANSWER_1 in context, \
            f"{maker.__name__} 的圆桌上下文没有澄清块"


def test_requests_are_unchanged_without_clarifications(monkeypatch):
    """没有澄清时请求逐字不变：块是空值，``_ctx`` 整个跳掉。"""
    seen: list[str] = []
    monkeypatch.setattr(nodes, "run_agent",
                        lambda agent, message: seen.append(message) or "正文")
    brief = Brief(variant="I2VA", duration=5.0, plot=TOPIC)  # clarifications 默认空

    nodes._make_producer_node(_AGENTS)(_state_for(brief))
    nodes._make_director_node(_AGENTS)(_state_for(brief))

    assert all(CLARIFY_LABEL not in message for message in seen)


def test_repair_retry_of_the_frame_node_also_carries_the_clarification(monkeypatch):
    """首帧节点 JSON 解析失败后的**重试**请求同样带澄清。

    这条路只在首轮产物过不了 bundle 校验时走，正常路径走不到——它是本票最容易漏的
    一处接线。首轮回复用合法 JSON 的数组（顶层不是对象），才能落到**外层**修复分支；
    用坏 JSON 会落到内层的「转成 JSON」重试，那条只管格式转换，不问主题。
    """
    replies = iter(["[]", json.dumps(_bundle_payload())])
    seen: list[str] = []
    monkeypatch.setattr(nodes, "run_agent",
                        lambda agent, message: seen.append(message) or next(replies))
    block = clarification.render_clarification_block([(QUESTIONS[0], ANSWER_1)])
    brief = Brief(variant="I2VA", duration=5.0, plot=TOPIC, clarifications=block)

    nodes._make_fl2va_frame_prompt_node(_AGENTS)(_state_for(brief))

    assert len(seen) == 2, "首轮坏 JSON 没有触发重试，这条用例没测到重试路径"
    assert CLARIFY_LABEL in seen[1] and ANSWER_1 in seen[1], "重试请求里没有澄清"


def test_injection_headers_agree_across_call_sites():
    """三处注入的标题必须同形：节点的 ``_ctx`` 标签、判官与修复指令用的 ``as_block``。

    两处措辞分叉过（一处写「属于合法来源」一处写「必须保留」）——同一个概念在请求里
    以两个名字出现，正是 domain.md 要求术语与 CONTEXT.md 一致要挡的事。
    """
    text = clarification.render_clarification_block([(QUESTIONS[0], ANSWER_1)])
    assert clarification.as_block(text) == nodes._ctx(
        **{clarification.BLOCK_LABEL: text}), "as_block 与 _ctx 的标题不同形"
    assert clarification.BLOCK_LABEL == "起步澄清", \
        "块标题必须逐字等于 CONTEXT.md 的术语（与「用户修订」并排出现）"


def test_common_sense_judge_sees_clarifications_as_a_legitimate_source(monkeypatch):
    """常识判官也要看到澄清，并把它算作合法来源。

    判官的「禁止无中生有」原本只认主题/剧本/镜头表/关键帧；澄清按设计就是**主题里没有、
    用户另外说出口**的要求。少了这一块，澄清产出的细节会被判成 UNSOURCED_DETAIL error，
    有界质检循环随即把它重出掉——用户的要求被机器静默撤回。
    """
    from minimax_h3_prompt.tools import frame_sanity

    seen: list[str] = []
    monkeypatch.setattr(frame_sanity, "run_agent",
                        lambda agent, message: seen.append(message) or '{"issues": []}')
    block = clarification.render_clarification_block([(QUESTIONS[0], ANSWER_1)])
    brief = Brief(variant="I2VA", duration=5.0, plot=TOPIC, clarifications=block)

    frame_sanity.prompt_common_sense_issues("秋日庭院里的橘猫", brief, {"qa": object()})

    assert seen
    assert ANSWER_1 in seen[0], "判官看不到澄清 → 澄清细节会被判成无中生有"
    assert clarification.BLOCK_LABEL in seen[0].split("【待审内容】")[0].split("禁止无中生有")[1], \
        "规则不让判官把澄清当来源"


def test_theme_repair_injection_keeps_clarifications():
    """阶段 2 质检精修的修复指令同样不得把澄清当成「无中生有」。"""
    from minimax_h3_prompt.graph import pipeline

    block = clarification.render_clarification_block([(QUESTIONS[0], ANSWER_1)])
    brief = Brief(variant="I2VA", duration=5.0, plot=TOPIC, clarifications=block)

    injection = pipeline._theme_repair_injection({"fl2va_prompt_bundle": {}}, brief)

    assert ANSWER_1 in injection
    assert clarification.BLOCK_LABEL in injection.split("【禁止无中生有】")[1], \
        "修复指令的来源清单里没有澄清 → 修复轮会把用户的澄清删掉"


def test_clarification_reaches_the_generation_request_end_to_end(tmp_path, monkeypatch):
    """端到端：用户答的澄清，真的出现在阶段 1 节点的请求文本里。

    两个打桩点之间的接缝（向导写 brief → 节点读 brief）正是「写了没人读」那一族
    缺陷的形态，所以这里用真节点跑一遍，而不是只断言 brief 字段。
    """
    _, captured, _, _ = _run_phase1(tmp_path, monkeypatch, inputs=_answer_path_inputs())
    brief = captured["brief"]

    seen: list[str] = []
    monkeypatch.setattr(nodes, "run_agent",
                        lambda agent, message: seen.append(message) or "制作计划正文")
    nodes._make_producer_node(_AGENTS)(_state_for(brief))

    request = seen[0]
    assert ANSWER_1 in request, "用户答过的澄清没有进入生成请求"
    assert ANSWER_2 in request
    assert TOPIC in request and ANSWER_1 not in TOPIC

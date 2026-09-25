"""设定级修订：声明层次、回灌设定、重跑下游（issue #28 / T6）。

票面缺陷：设定级意见在架构上**没有任何落点**——人物设计／背景／道具／美术统筹／分镜／
镜头评审／身份锁定／画面细化全部在首帧提示词节点**之前**定稿，而重出只跑那一个节点。
用户提「汉服太朴素了，参考现代的汉服」是**人物设计级**的要求，最终只有首帧图变好看。

本文件两层：

- **节点层（纯）**：本轮那条设定级修订到底有没有进该层节点的请求文本，且**只进该进的那个**
  （重跑起点是节点组：设计节点组一次跑三个设计师，不过滤的话「改人物」会连背景道具一起改写）；
- **循环层（真调用链 ``_phase1_new`` + 真落盘）**：确认门念了什么、从哪个节点截断、
  哪些产物被替换、成功后标已落地、失败或拒绝时不标也不留痕。
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from minimax_h3_prompt import model_factory
from minimax_h3_prompt.brief_parser import Brief
from minimax_h3_prompt.graph import nodes, pipeline, roundtable
from minimax_h3_prompt.graph.pipeline import (
    _NODE_ARTIFACTS,
    _STAGE1_CHAIN,
    build_pipeline_graph,
    run_stage1,
    stage1_rerun_plan,
)
from minimax_h3_prompt.segment_prompts import render_revision_context
from minimax_h3_prompt.session_store import load_session
from minimax_h3_prompt.ui import wizard
from minimax_h3_prompt.user_revisions import (
    FRAME_ROUNDS_FILENAME,
    LAYER_ART,
    LAYER_CHARACTER,
    LAYER_FRAME,
    LAYER_STORY,
    SETTING_LAYERS,
    SETTING_REVISION_KEY,
)

TOPIC = "秋日庭院里的橘猫"
# 票面场景原话：这是**人物设计级**的要求，不该只让首帧图好看。
SETTING_TEXT = "汉服太朴素了，参考现代的汉服，颜值高"
FRAME_TEXT = "天空改成黄昏，要暖金色"

# 设计节点组（重跑起点）之后到链尾的节点数：确认门念的「将重跑 N 步」。
_STEPS_FROM_DESIGNERS = len(_STAGE1_CHAIN) - _STAGE1_CHAIN.index("parallel_designers")


def _bundle_payload(label: str) -> dict:
    """I2VA 单帧产物。主题无地点关键词 → 无主题词约束，校验只要求连续性与非空。"""
    return {
        "scene_anchor": "秋日庭院",
        "first": {"zimage": {
            "positive_prompt": f"秋日庭院里的橘猫（{label}）",
            "instructions": ["paste to node 187"],
        }},
        "continuity_constraints": ["保持同一只橘猫与同一庭院"],
    }


# ---------------------------------------------------------------------------
# 节点层：设定级修订进请求文本，且只进该层的节点
# ---------------------------------------------------------------------------

def _brief() -> Brief:
    return Brief(mode="base", variant="I2VA", duration=5.0, style="写实", plot=TOPIC)


def _run_node(monkeypatch, node_name: str, state: dict, requests: list[str],
              reply: str = "产物") -> None:
    """用假 agent 跑一个真节点，把真正发给模型的请求文本记下来。

    agent 对象本身被忽略（``run_agent`` 被打桩），所以这里只需要一个能按下标取到东西的 dict：
    本文件考的是**请求文本**，不是角色提示词。
    """
    brief = _brief()
    agents = {name: object() for name in (
        "character_designer", "background_designer", "prop_designer", "screenwriter",
        "storyboard", "art_director", "cinematographer", "reference_consistency",
        "frame_prompt_engineer", "image_prompt_engineer", "sound_designer", "composer",
    )}

    monkeypatch.setattr(nodes, "run_agent",
                        lambda agent, message: requests.append(message) or reply)
    built = nodes.make_nodes(agents, object(), brief, SimpleNamespace(max_qa_iterations=0))
    built[node_name]({**state, "brief": brief})


def _design_state(*, layer: str, text: str) -> dict:
    """重跑现场的最小 state：只有本轮那条设定级修订（其余产物由重跑链逐节点现产）。

    键的形状与 ``_rerun_setting_layer`` 落的那个一致：``{layer, text}``。
    """
    return {SETTING_REVISION_KEY: {"layer": layer, "text": text}}


def test_character_layer_reaches_the_character_design_node(monkeypatch):
    """人物设定级修订必须出现在**人物形象设计节点**的请求里——那才是它的落点。

    只进首帧提示词的请求是今天的行为（票面根因 2）；这条断言的就是「产物本身被改写」。
    """
    requests: list[str] = []
    _run_node(monkeypatch, "character_designer", _design_state(layer=LAYER_CHARACTER, text=SETTING_TEXT), requests)

    assert len(requests) == 1
    assert SETTING_TEXT in requests[0], "人物设计节点没拿到本轮这条设定级修订"
    assert "人物设定" in requests[0], "请求里没写明这条属于哪一层（模型无从知道要落实进产物）"


def test_character_layer_does_not_leak_to_sibling_designers(monkeypatch):
    """层次过滤：改人物的意见不得改写背景与道具设计。

    重跑起点是**节点组**（``parallel_designers`` 一次跑三个设计师），不过滤的话一条
    「汉服改成现代审美」会把背景与道具一起重写——用户没要求的产物被替换掉。
    """
    for node_name in ("background_designer", "prop_designer"):
        requests: list[str] = []
        _run_node(monkeypatch, node_name, _design_state(layer=LAYER_CHARACTER, text=SETTING_TEXT), requests)
        assert SETTING_TEXT not in requests[0], f"{node_name} 吃到了别的层次的修订"


def test_art_layer_reaches_scene_and_prop_designers_only(monkeypatch):
    """美术/场景/道具层＝背景设计 ＋ 道具设计两个节点，人物设计不吃。"""
    for node_name, expected in (("background_designer", True), ("prop_designer", True),
                                ("character_designer", False)):
        requests: list[str] = []
        _run_node(monkeypatch, node_name, _design_state(layer=LAYER_ART, text=SETTING_TEXT), requests)
        assert (SETTING_TEXT in requests[0]) is expected, \
            f"{node_name} 对美术层的修订处理错误（应{'进' if expected else '不进'}请求）"


def test_story_layer_reaches_screenwriter_and_storyboard(monkeypatch):
    """剧情/分镜层＝分场剧本 ＋ 分镜表：两个节点都要拿到（票面「回灌对应产物」两处都列了）。"""
    for node_name in ("screenwriter", "storyboard"):
        requests: list[str] = []
        _run_node(monkeypatch, node_name, _design_state(layer=LAYER_STORY, text=SETTING_TEXT), requests)
        assert SETTING_TEXT in requests[0], f"{node_name} 没拿到剧情层的修订"

    requests = []
    _run_node(monkeypatch, "character_designer", _design_state(layer=LAYER_STORY, text=SETTING_TEXT), requests)
    assert SETTING_TEXT not in requests[0], "剧情层的修订漏进了人物设计节点"


def test_no_setting_revision_key_leaves_requests_unchanged(monkeypatch):
    """没有这个键（自动质检循环、续接重启后的普通重跑）时，请求里一个字都不该多出来。"""
    for node_name in ("character_designer", "background_designer", "prop_designer",
                      "screenwriter", "storyboard"):
        requests: list[str] = []
        _run_node(monkeypatch, node_name, {}, requests)
        assert "设定级修订" not in requests[0], f"{node_name} 凭空注入了设定级修订块"


def test_frame_node_skips_revisions_that_already_landed(monkeypatch):
    """已落地的修订不再进首帧节点的请求（票面 AC-4）：它的要求已写进设定产物本身。"""
    requests: list[str] = []
    state = {
        "user_revisions": [
            {"round": 1, "layer": LAYER_CHARACTER, "text": SETTING_TEXT, "applied": True},
            {"round": 2, "layer": LAYER_FRAME, "text": FRAME_TEXT},
        ],
    }
    _run_node(monkeypatch, "fl2va_frame_prompts", state, requests, reply=json.dumps(_bundle_payload("新版")))

    assert FRAME_TEXT in requests[0], "未落地的修订必须照旧注入"
    assert SETTING_TEXT not in requests[0], "已落地的修订又被当成新要求注入了（越滚越重）"


def test_setting_revision_survives_the_graph_state_channel(monkeypatch):
    """真过一遍图：LangGraph **只搬运 ``PipelineState`` 里声明过的键**，未声明的静默丢弃。

    这条是被实际咬过才写的。第一版把 ``setting_revision`` 放进 ``run_stage1`` 的
    initial_state 就以为接线完成——而重跑是**走图跑的**，三个设计师节点一个都没拿到它，
    设定级回灌成了空动作；偏偏所有打桩 ``run_stage1`` 的用例照样全绿（它们绕过了图）。
    所以这条必须跑真图、真状态 schema，不能打桩。
    """
    brief = _brief()
    requests: list[str] = []
    monkeypatch.setattr(nodes, "run_agent",
                        lambda agent, message: requests.append(message) or "设计正文")
    graph, _ = build_pipeline_graph(object(), brief, SimpleNamespace(roundtable_max_rounds=1),
                                    stage=1, chain=["parallel_designers"])

    graph.invoke({
        "brief": brief, "mode": "base", "variant": "I2VA", "duration": brief.duration,
        SETTING_REVISION_KEY: {"layer": LAYER_CHARACTER, "text": SETTING_TEXT},
    })

    assert requests, "设计节点根本没跑"
    assert any(SETTING_TEXT in request for request in requests), \
        "设定级修订经图之后没了——多半是 PipelineState 没声明这个键（LangGraph 静默丢弃）"


def test_rerun_really_truncates_the_chain_from_the_layer_node(tmp_path, monkeypatch):
    """缝三（真跑）：设定级重跑**真的**从该层起点截断到链尾，而不是只算了个计划。

    循环层测试把 ``run_stage1`` 打了桩，桩上只核对 ``resume_from_node``——「截断」这件事
    没被真跑验证过。这里放行真的 ``run_stage1``（真图、真截断链、真状态合并）：断言①该层
    节点跑到了并拿到本轮修订，②起点之前的节点（编剧及其上游）一个都没跑。
    """
    requests: list[str] = []

    def fake_run_agent(agent, message):
        requests.append(message)
        # 链尾的关键帧节点要求 JSON；其余节点一句非空文本即可
        if "请为这一段" in message or "请只生成首帧" in message or "请只生成尾帧" in message:
            return json.dumps(_bundle_payload("重跑版"))
        return "产物"

    monkeypatch.setattr(nodes, "run_agent", fake_run_agent)
    # 圆桌节点自己也调模型（roundtable._invoke 不走 nodes.run_agent）；给出锁定的结论即可
    monkeypatch.setattr(roundtable, "_invoke", lambda role, model, msgs: "已锁定")
    monkeypatch.setattr(model_factory, "build_chat_model", lambda: object())
    # 节点落盘的证据目录不在本票的题目里，改到 tmp_path——别往仓库的 output/ 里写东西
    monkeypatch.setattr(pipeline.stage_saver, "base", tmp_path / "stages")
    config = SimpleNamespace(roundtable_max_rounds=1, common_sense_qa=False)

    state, _, _ = run_stage1(
        _brief(), config,
        resume_from_node="parallel_designers",
        initial_state={"shot_table": "[Shot 1] 旧分镜", "character_design": "旧版人物形象设计",
                       SETTING_REVISION_KEY: {"layer": LAYER_CHARACTER, "text": SETTING_TEXT}},
    )

    assert any(SETTING_TEXT in request for request in requests), "该层节点没拿到本轮修订"
    assert not any("请写出分场剧本" in request for request in requests), \
        "重跑越过了起点，把编剧也重跑了（截断没生效）"
    assert not any("请给出制作计划" in request for request in requests), "重跑跑到了制片节点"
    assert state["character_design"] == "产物", "该层产物没有被重跑链写回"


def test_gate_defaults_to_not_rerunning(monkeypatch, capsys):
    """确认门默认**否**：回车不该触发那个不可逆动作（替换用户已看过的产物）。"""
    monkeypatch.setattr(wizard, "_prompt", lambda text, *a, **k: "")  # 直接回车
    monkeypatch.setattr(wizard, "_drain_stdin", lambda: None)

    assert wizard._confirm_setting_rerun(stage1_rerun_plan(LAYER_CHARACTER)) is False
    assert "按默认 否 处理" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# 分段注入通道：已落地的修订同样不再带
# ---------------------------------------------------------------------------

def test_segment_channel_skips_revisions_that_already_landed():
    """分段写段请求与分段规划取的是同一块：已落地的条目在**每一条**注入通道里都要消失。"""
    landed = {"round": 1, "layer": LAYER_CHARACTER, "text": SETTING_TEXT, "applied": True}
    pending = {"round": 2, "layer": LAYER_FRAME, "text": FRAME_TEXT}

    both = render_revision_context({"user_revisions": [landed, pending]})
    assert FRAME_TEXT in both
    assert SETTING_TEXT not in both, "已落地的修订仍在分段请求里"

    assert render_revision_context({"user_revisions": [landed]}) == "", \
        "全部已落地时还渲染出了修订块（等于把改好的设定又说一遍）"


# ---------------------------------------------------------------------------
# 重跑计划：链上现推，不手写
# ---------------------------------------------------------------------------

def test_every_rerun_plan_node_has_an_artifact_label():
    """链上可能出现于重跑计划的节点都得有产物标签，反之标签表里不该有链外的节点。

    确认门那句「下列产物会被替换」就取这张表：链上加了节点而表没跟着加，门会**少报**
    一件被替换的产物——用户凭这份清单决定要不要跑，少报等于骗他做决定。
    """
    planned = {name for spec in SETTING_LAYERS
               for name in stage1_rerun_plan(spec.value).nodes}
    assert planned == set(_NODE_ARTIFACTS), \
        f"重跑计划的节点与产物标签表对不上：{planned ^ set(_NODE_ARTIFACTS)}"


def test_rerun_plan_starts_at_the_layer_node_and_runs_to_the_chain_end():
    """层次＝重跑起点，截断到链尾；「N 步」与「哪些产物」都从链上现推。"""
    plan = stage1_rerun_plan(LAYER_CHARACTER)

    assert plan.start == "parallel_designers"
    assert plan.nodes == tuple(_STAGE1_CHAIN[_STAGE1_CHAIN.index("parallel_designers"):])
    assert "人物形象设计" in plan.artifacts, "本层产物不在「会被替换」清单里"
    assert "关键帧生图提示词（首/尾帧）" in plan.artifacts, "下游到链尾的产物没被列出来"


def test_rerun_plan_does_not_promise_ref_only_products_in_base_mode():
    """base 模式下身份评审与参考一致性节点直接返回空串：列它们等于承诺两份不存在的产物。"""
    base = stage1_rerun_plan(LAYER_STORY, mode="base")
    ref = stage1_rerun_plan(LAYER_STORY, mode="ref")

    assert "身份一致性锁定" not in base.artifacts
    assert "参考一致性素材" not in base.artifacts
    assert "身份一致性锁定" in ref.artifacts and "参考一致性素材" in ref.artifacts


def test_rerun_plan_rejects_a_non_setting_layer():
    """画面级不是设定级：它不走截断重跑（走了就与今天的行为不一致了）。"""
    with pytest.raises(ValueError):
        stage1_rerun_plan(LAYER_FRAME)


# ---------------------------------------------------------------------------
# 循环层：确认门 / 截断重跑 / 已落地标记 / 台账
# ---------------------------------------------------------------------------

def _run_loop(tmp_path, monkeypatch, *, prompts: list[str], confirms: list[bool],
              rerun_fails: bool = False):
    """跑 ``_phase1_new`` 的修改循环。返回 ``(session, 每轮发给 frame agent 的请求, run_stage1 调用)``。

    ``prompts`` 是 ``_prompt`` 按序吐出的输入：起步四问，之后每轮是**菜单号 → 意见正文 →
    层次号**（回车＝只改画面）。
    ``confirms`` 是 ``_confirm`` 按序吐出的答案（每轮「有修改意见？」＋设定级的「确认重跑？」）。

    ``run_stage1`` 被打桩成两副面孔：起步那次给出初版产物，**带 resume_from_node 的那次**
    就是设定级重跑——它必须把 initial_state 里的旧产物带上、只换掉重跑该换的那几件
    （真实现场由 ``run_stage1`` 的截断链完成，这里只如实模拟它的语义）。
    """
    pending = list(prompts)
    answers = list(confirms)

    def fake_prompt(text, *a, **k):
        print(str(text), end="")
        return pending.pop(0)

    monkeypatch.setattr(wizard, "_prompt", fake_prompt)
    monkeypatch.setattr(wizard, "_confirm", lambda *a, **k: answers.pop(0))
    monkeypatch.setattr(wizard, "_drain_stdin", lambda: None)

    requests: list[str] = []
    counter = {"n": 0}

    def fake_run_agent(agent, message):
        requests.append(message)
        counter["n"] += 1
        return json.dumps(_bundle_payload(f"第{counter['n']}版"))

    calls: list[dict[str, Any]] = []

    def fake_run_stage1(brief, config, **kwargs):
        calls.append(kwargs)
        if "resume_from_node" not in kwargs:
            return ({"shot_table": "[Shot 1] 庭院空镜",
                     "character_design": "旧版人物形象设计",
                     "fl2va_prompt_bundle": _bundle_payload("初版")},
                    object(), {"frame_prompt_engineer": object()})
        if rerun_fails:
            raise RuntimeError("重跑炸了")
        merged = dict(kwargs.get("initial_state") or {})
        merged.update({"character_design": "新版人物形象设计",
                       "shot_table": "[Shot 1] 新版分镜",
                       "fl2va_prompt_bundle": _bundle_payload("重跑版")})
        return merged, object(), {"frame_prompt_engineer": object()}

    monkeypatch.setattr(nodes, "run_agent", fake_run_agent)
    monkeypatch.setattr(wizard, "run_stage1", fake_run_stage1)
    # max_clarification_rounds=0：起步澄清会多消耗一次交互输入（issue #29 的题目在别处）
    config = SimpleNamespace(default_duration=5.0, default_language="Chinese",
                             sessions_root=tmp_path / "sessions",
                             max_clarification_rounds=0)
    session = wizard._phase1_new(config)
    return session, requests, calls


def _startup() -> list[str]:
    """起步四问：主题 → 时长（默认）→ 风格（默认）→ 生成方式（1 首帧）。"""
    return [TOPIC, "", "", "1"]


def _ledger_rows(session) -> list[dict]:
    path = session.directory / FRAME_ROUNDS_FILENAME
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_gate_names_the_steps_and_the_products_it_will_replace(tmp_path, monkeypatch, capsys):
    """票面 AC-3：重跑前展示「将重跑 N 步 + 哪些产物会被替换」，且**在跑之前**。

    替换不可逆（LLM 生成不可复现同一版本），用户凭这份清单决定值不值得跑。
    """
    session, _, calls = _run_loop(
        tmp_path, monkeypatch,
        prompts=[*_startup(), "1", SETTING_TEXT, "2"], confirms=[True, True, False])

    out = capsys.readouterr().out
    assert f"将重跑 {_STEPS_FROM_DESIGNERS} 步" in out, "确认门没报「将重跑几步」"
    for label in ("人物形象设计", "背景设计", "道具设计", "美术统筹", "分镜表",
                  "关键帧生图提示词（首/尾帧）"):
        assert label in out, f"确认门没列出会被替换的产物：{label}"
    assert len(calls) == 2, "确认之后没有真的重跑阶段 1 的链"
    assert calls[1]["resume_from_node"] == "parallel_designers"
    assert session is not None


def test_setting_rerun_rewrites_the_artifact_and_marks_the_revision_applied(tmp_path, monkeypatch):
    """票面 AC-2/AC-4/AC-5：该层产物本身被改写、从该层起点截断、成功后标已落地并留台账。"""
    session, _, calls = _run_loop(
        tmp_path, monkeypatch,
        prompts=[*_startup(), "1", SETTING_TEXT, "2"], confirms=[True, True, False])

    assert session is not None
    # ① 该层产物本身被改写（不是一个只挂着看的注记）
    assert session.stage_state["character_design"] == "新版人物形象设计", "人物设计没有被回灌重写"
    # ② 从该层起点截断到链尾，且旧产物随 initial_state 进了重跑
    initial = calls[1]["initial_state"]
    assert initial["character_design"] == "旧版人物形象设计", "重跑没带上原有产物（会从空白重造）"
    assert initial["shot_table"] == "[Shot 1] 庭院空镜"
    # ③ 本轮那条修订经**独立入参键**进了重跑（节点层测试断言它最终真的出现在请求里）
    assert initial[SETTING_REVISION_KEY]["layer"] == LAYER_CHARACTER
    assert initial[SETTING_REVISION_KEY]["text"] == SETTING_TEXT
    # ④ 该键是**一轮的入参**：不得顺着 state 流到阶段 2 或续接路径上去
    raw = json.loads((session.directory / "session-state.json").read_text(encoding="utf-8"))
    assert SETTING_REVISION_KEY not in raw["stage_state"]
    assert "_progress" not in raw["stage_state"], "重跑的断点元数据留在会话里了"
    # ⑤ 标已落地（含白名单往返：这一眼是从磁盘 load 回来的）
    entry = session.stage_state["user_revisions"][0]
    assert (entry["round"], entry["layer"], entry["text"]) == (1, LAYER_CHARACTER, SETTING_TEXT)
    assert entry["applied"] is True, "重跑成功却没有标已落地（清单会一直重复注入这条）"
    # ⑥ 台账：一层一行，记的是这一轮真正发生了什么
    rows = _ledger_rows(session)
    assert len(rows) == 1
    assert rows[0]["layer"] == LAYER_CHARACTER and rows[0]["layer_label"] == "人物设定"
    assert rows[0]["feedback_this_round"] == SETTING_TEXT
    assert rows[0]["active_revisions"][0]["applied"] is True, "台账快照看不出这条已落地"
    assert rows[0]["base_used"] is False, "设定级重跑没有上修订基线，台账要如实记"


def test_declining_the_rerun_records_nothing(tmp_path, monkeypatch, capsys):
    """票面 AC-3 后半：用户拒绝则不跑、不留痕（不进清单、不写台账、不动产物）。"""
    session, requests, calls = _run_loop(
        tmp_path, monkeypatch,
        prompts=[*_startup(), "1", SETTING_TEXT, "2"], confirms=[True, False, False])

    assert session is not None
    assert len(calls) == 1, "用户拒绝了重跑，链还是被重跑了"
    assert requests == [], "用户拒绝了重跑，却已经重出了提示词"
    assert session.stage_state.get("user_revisions") in (None, []), "被拒绝的意见进了累积清单"
    assert _ledger_rows(session) == [], "被拒绝的轮次留下了台账行"
    out = capsys.readouterr().out
    assert "没有记录" in out, "拒绝之后没告诉用户这条意见没有被记下"


def test_failed_rerun_records_the_revision_but_does_not_mark_it_applied(tmp_path, monkeypatch):
    """票面 AC-6：重跑失败或中断时**不**标已落地——清单不得谎报。

    意见本身要留在清单里（用户说了的话不能因为一次崩溃就消失），只是它还没落地。
    """
    with pytest.raises(RuntimeError):
        _run_loop(tmp_path, monkeypatch,
                  prompts=[*_startup(), "1", SETTING_TEXT, "2"], confirms=[True, True],
                  rerun_fails=True)

    gen = tmp_path / "sessions" / wizard._topic_slug(TOPIC) / "GEN001"
    session = load_session(gen)
    assert session is not None
    entry = session.stage_state["user_revisions"][0]
    assert entry["text"] == SETTING_TEXT
    assert entry.get("applied", False) is False, "重跑失败了却把这条标成了已落地"
    assert not (gen / FRAME_ROUNDS_FILENAME).exists(), "没跑成的轮次写进了台账"


def test_frame_layer_stays_exactly_what_it_was(tmp_path, monkeypatch):
    """票面 AC-1：选「只改画面」（回车默认）时行为与今天完全一致——不重跑链、不标已落地。"""
    session, requests, calls = _run_loop(
        tmp_path, monkeypatch,
        prompts=[*_startup(), "1", FRAME_TEXT, ""], confirms=[True, False])

    assert session is not None
    assert len(calls) == 1, "画面级修订却重跑了阶段 1 的链"
    assert len(requests) == 1 and FRAME_TEXT in requests[0], "画面级重出没带上这条意见"
    entry = session.stage_state["user_revisions"][0]
    assert entry["layer"] == LAYER_FRAME
    assert entry.get("applied", False) is False, "画面级修订被标成了已落地（它每轮都要注入）"
    rows = _ledger_rows(session)
    assert [row["layer"] for row in rows] == [LAYER_FRAME]
    assert rows[0]["base_used"] is True, "画面级重出仍以上一版为基线，台账要如实记"


def test_applied_revision_is_not_injected_again_in_the_next_round(tmp_path, monkeypatch, capsys):
    """票面 AC-4 的接线：落地之后的下一次重出，请求里不再有它；屏幕把它单列成「已落地」。"""
    session, requests, _ = _run_loop(
        tmp_path, monkeypatch,
        prompts=[*_startup(), "1", SETTING_TEXT, "2", "1", FRAME_TEXT, ""],
        confirms=[True, True, True, False])

    assert session is not None
    assert len(requests) == 1, "第 2 轮只该有一次画面级重出"
    assert FRAME_TEXT in requests[0], "第 2 轮的新意见没进请求"
    assert f"[人物设定] {SETTING_TEXT}" not in requests[0], \
        "已落地的那条仍作为要求注入了下一轮（会与改好的设定产物叠加）"
    out = capsys.readouterr().out
    assert "已落地" in out, "屏幕没告诉用户这条已经落地"
    assert SETTING_TEXT in out, "清单里看不到已落地那条（用户无从确认自己的要求去了哪）"


def test_revocation_screen_numbering_excludes_landed_revisions(tmp_path, monkeypatch, capsys):
    """撤销屏幕清单与本轮撤销编号必须同源：已落地的条目**不占号**。

    占号的后果是撤错人：用户照屏幕念「1」撤的是画面级那条，而屏幕上 1 号显示的是别的话。
    （已落地那条撤不掉是有意的：它的改动已经写进设定产物，撤掉文本也撤不掉产物。）
    """
    session, _, _ = _run_loop(
        tmp_path, monkeypatch,
        prompts=[*_startup(), "1", SETTING_TEXT, "2", "1", FRAME_TEXT, "", "3", "1", ""],
        confirms=[True, True, True, True, False])

    assert session is not None
    texts = [(item["text"], item.get("applied")) for item in session.stage_state["user_revisions"]]
    assert texts == [(SETTING_TEXT, True)], f"撤错了条目或把已落地那条也撤了：{texts}"

    out = capsys.readouterr().out
    assert f"1. [人物设定] {SETTING_TEXT}" not in out, \
        "已落地的条目在带编号的清单里占了号（照屏幕念号会撤错人）"
    assert f"1. [只改画面] {FRAME_TEXT}" in out, "待落地那条没有被编进撤销清单"


def test_all_four_layers_are_offered(tmp_path, monkeypatch, capsys):
    """票面 AC-1：四档层次可选，且「只改画面」是回车默认（代价最短的那条）。"""
    _run_loop(tmp_path, monkeypatch, prompts=[*_startup(), "1", FRAME_TEXT, ""],
              confirms=[True, False])

    out = capsys.readouterr().out
    for label in ("只改画面", "人物设定", "美术场景道具", "剧情分镜"):
        assert label in out, f"屏幕上没有可选层次：{label}"
    assert SETTING_LAYERS, "设定级层次表是空的"


def test_every_chain_node_has_a_screen_label():
    """确认门念的节点名要有人读的中文标签：链上加了节点而标签表没加，屏幕上就会
    冒出一个 ``parallel_foo`` 这样的内部名（``_NODE_LABELS.get(name, name)`` 静默降级）。

    与产物标签表（``pipeline._NODE_ARTIFACTS``）的区别：那张表管「哪些产物会被替换」，
    这张管「重跑哪几步」的念法，两张都随链走，都得有闸门盯着。
    """
    missing = set(_STAGE1_CHAIN) - set(wizard._NODE_LABELS)
    assert not missing, f"阶段 1 链上这些节点没有屏幕标签：{sorted(missing)}"

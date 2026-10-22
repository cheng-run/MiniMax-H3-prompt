"""成本账（issue #52）：token 计价跨中断续跑累计、读图计入、快照随续跑点落盘。

四条 AC 的牙齿在这里（AC4 的台账外部成本在 ``tests/test_asset_ledger.py``）：

- AC1 中断重启后累计不归零：续跑入口把上一次的成本表接回来，阶段入口不清零；
- AC2 读图（视觉调用）计入计价表——一次读图就是一次真实计费调用；
- AC3 快照随续跑点持久化（``save_session`` 落盘时自动带上活账）；
- 新开一次生成＝新账（上一份生成的账不串到这一份里）。

接缝：计量表（``TokenMeter``）、会话持久化（``save_session/load_session``）、
读图口（``reference_auditor.describe_image``）、阶段入口（``run_stage1``）与向导续跑入口
（``run_wizard``）——全部是既有公开接口，本票零新接缝（spec #47 Testing Decisions）。

模拟重启的手法：``token_meter.reset()``——那正是新进程里计量表的初始状态。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from PIL import Image

from minimax_h3_prompt import model_factory
from minimax_h3_prompt.brief_parser import Brief
from minimax_h3_prompt.observability import (
    COST_TABLE_KEY,
    VISION_ROLE,
    token_meter,
)
from minimax_h3_prompt.session_store import STATUS_AWAITING_FRAMES, load_session, save_session
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


@pytest.fixture(autouse=True)
def _clean_meter():
    """计量表是模块级单例：用例前后都清空（每条用例自己写自己的账）。"""
    token_meter.reset()
    yield
    token_meter.reset()


def _cfg(tmp_path, **overrides):
    base = dict(
        default_duration=5.0, default_language="Chinese",
        sessions_root=tmp_path / "sessions", max_clarification_rounds=0,
        common_sense_qa=False, max_qa_iterations=1, roundtable_max_rounds=1,
        vision_model=SimpleNamespace(model="stub-vision"),
    )
    base.update(overrides)
    return SimpleNamespace(**base)


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


def _ref_stage1_state() -> dict:
    return {
        "script": TOPIC,
        "character_image_prompts": [
            {"name": "红裙女孩", "positive_prompt": "人物·红裙女孩专属生图提示词"},
        ],
    }


# ---------------------------------------------------------------------------
# AC1：中断重启后累计不归零
# ---------------------------------------------------------------------------

def test_stage_entry_does_not_zero_the_account(tmp_path, monkeypatch):
    """阶段入口不再清零累计账（issue #52 AC1）：进阶段前花的钱，跑完还在。

    用一张假图替代真实管线：图里的节点跑完顺手花掉一笔——真实管线里那就是 LLM 调用。
    断言落在外部行为：计量表的总账、以及写回 state 的那份成本表。
    """
    from minimax_h3_prompt.graph import pipeline as pipeline_module

    token_meter.add("role_director", 1000, 200)  # 进阶段之前已经花掉的

    class FakeGraph:
        def stream(self, initial, stream_mode=None):
            token_meter.add("role_director", 500, 100)  # 阶段里花的
            yield {"producer": {"production_plan": "计划"}}

    monkeypatch.setattr(model_factory, "build_chat_model", lambda: object())
    monkeypatch.setattr(pipeline_module, "build_pipeline_graph",
                        lambda *a, **k: (FakeGraph(), {}))
    monkeypatch.setattr(pipeline_module.stage_saver, "base", tmp_path / "stages")

    brief = Brief(mode="ref", variant="T2VA", duration=5.0, plot=TOPIC)
    state, _, _ = pipeline_module.run_stage1(brief, _cfg(tmp_path))

    assert token_meter.totals()["input"] == 1500, "阶段入口把累计账清零了"
    # 成本表不进 state（复审按 ADR 0005 接线纪律核过）：它的产出物是**会话文件**，
    # 由 `save_session` 取活账写入；state 里再放一份是「写了没人读」的第二份真相。
    assert COST_TABLE_KEY not in state


def test_resumed_session_continues_the_cost_account(tmp_path, monkeypatch):
    """中断重启后续跑：成本表接上上一次的账，新花的钱往上加（AC1 + AC3）。

    上一轮进程在会话里留下 1200/300 的账；这一轮进程从空表起（``token_meter.reset()``），
    续跑组装时又花了 800/200。续跑结束后的总账必须是 2000/500——不是 800/200。
    """
    generation_dir = tmp_path / "sessions" / "topic" / "GEN001"
    brief = Brief(mode="ref", variant="T2VA", duration=5.0, plot=TOPIC)
    token_meter.add("role_director", 1200, 300)  # 上一轮进程里已经花掉的（一次调用）
    save_session(generation_dir, brief, _ref_stage1_state(), status=STATUS_AWAITING_FRAMES)

    _silence(monkeypatch)
    monkeypatch.setattr(wizard, "_confirm", lambda *a, **k: False)  # 视频/音频一律不挂账
    monkeypatch.setattr(wizard, "_offer_resume", lambda config: load_session(generation_dir))

    def fake_stage2(state, brief, config, **kwargs):
        token_meter.add("role_prompt_engineer", 800, 200)
        return dict(state), _SIX_SECTION_PROMPT

    monkeypatch.setattr(wizard, "run_stage2", fake_stage2)

    token_meter.reset()  # ＝重启后的新进程：空表
    _fake_inputs(monkeypatch, ["4"])  # 唯一一张资产：显式放弃
    assert wizard.run_wizard(_cfg(tmp_path)) == 0

    totals = token_meter.totals()
    assert (totals["input"], totals["output"]) == (2000, 500), \
        f"续跑没接上一次的账（或把老账丢了）：{totals}"
    assert load_session(generation_dir).stage_state[COST_TABLE_KEY] == {
        "role_director": {"calls": 1, "input": 1200, "output": 300},
        "role_prompt_engineer": {"calls": 1, "input": 800, "output": 200},
    }, "续跑落盘的账对不上（老账 + 新账）"


def test_new_generation_starts_a_fresh_account(tmp_path, monkeypatch):
    """新开一次生成＝新账（反向护栏）：上一份生成的成本不许串进这一份。

    这不是「阶段入口清零」，而是**账本的归属**：成本表跟着一份生成走，新会话另开。
    """
    _silence(monkeypatch)
    monkeypatch.setattr(wizard, "_choose_canvases", lambda: {})
    monkeypatch.setattr(
        wizard, "run_stage1",
        lambda brief, config, **kw: (_ref_stage1_state(), object(), {}),
    )
    token_meter.add("上一份生成", 5000, 1000)

    _fake_inputs(monkeypatch, [TOPIC, "", "", "4"])  # 主题/时长/风格/方式=多参
    wizard._phase1_new(_cfg(tmp_path))

    assert token_meter.totals()["calls"] == 0, "上一份生成的成本串进了新生成"


# ---------------------------------------------------------------------------
# AC2：读图计入计价表
# ---------------------------------------------------------------------------

def test_vision_reading_is_counted_in_the_account(tmp_path, monkeypatch):
    """读图（视觉调用）进成本表（issue #52 AC2）——读 8 张素材就是 8 次真实计费。

    期望值取模型现成的 ``usage_metadata``（独立来源），不从实现反推。
    """
    from langchain_core.messages import AIMessage

    image = tmp_path / "ref.png"
    Image.new("RGB", (16, 16), (10, 20, 30)).save(image)

    reply = AIMessage(
        content="画面里有一名红裙女孩。",
        usage_metadata={"input_tokens": 1234, "output_tokens": 56, "total_tokens": 1290},
    )

    class _StreamingVision:
        """读图主路走流式（issue #71）：假模型把首字节喂给 config 里的回调。"""

        def invoke(self, messages, config=None):
            callbacks = (config or {}).get("callbacks") or []
            for handler in callbacks:
                handler.on_llm_new_token("画面")
            return reply

    monkeypatch.setattr(reference_auditor, "_build_vision_model",
                        lambda streaming=True: _StreamingVision())

    text = reference_auditor.describe_image(image)

    assert text == "画面里有一名红裙女孩。"
    totals = token_meter.totals()
    assert (totals["calls"], totals["input"], totals["output"]) == (1, 1234, 56)
    assert VISION_ROLE in token_meter.snapshot(), "读图没单独记一行（看不出贵在哪）"


# ---------------------------------------------------------------------------
# 阶段 2 断点也要带着成本表（issue #52 AC1 的最贵那一块）
# ---------------------------------------------------------------------------

def test_stage2_checkpoint_carries_the_cost_account(tmp_path):
    """组装完成时落的断点里带着那一刻的成本表。

    组装是最贵的一步（十几分钟一次调用），而它跑完只落 ``stage2-checkpoint.json``、
    不落会话；成本表不跟着断点走，续跑（断点会跳过组装）之后这笔钱就再也补不回来
    ——正是「跑完的成本蒸发」里最大的一块。
    """
    from minimax_h3_prompt.stage2_checkpoint import load_checkpoint, save_checkpoint

    token_meter.add("role_prompt_engineer", 3000, 700)
    save_checkpoint(tmp_path, prompt_draft="初稿", cost_table=token_meter.snapshot())

    data = load_checkpoint(tmp_path)
    assert data is not None
    assert data["cost_table"]["role_prompt_engineer"] == {
        "calls": 1, "input": 3000, "output": 700}


def test_stage2_resume_adopts_the_checkpoint_account(tmp_path, monkeypatch):
    """断点续跑（跳过组装）时把断点里的账接回来：组装那笔钱不再凭空消失。"""
    from minimax_h3_prompt.graph import pipeline as pipeline_module
    from minimax_h3_prompt.stage2_checkpoint import save_checkpoint

    save_checkpoint(tmp_path, prompt_draft="初稿", cost_table={
        "role_prompt_engineer": {"calls": 1, "input": 3000, "output": 700}})
    token_meter.reset()  # ＝新进程：空表
    # 质检循环不是本用例的对象：校验器一律无问题，跑完就返回（不建真 LLM 客户端）
    monkeypatch.setattr(pipeline_module, "build_role_agents", lambda *a, **k: {})
    monkeypatch.setattr(pipeline_module, "validate_prompt", lambda *a, **k: [])
    monkeypatch.setattr(pipeline_module, "theme_fidelity_issues", lambda *a, **k: [])

    brief = Brief(mode="ref", variant="T2VA", duration=5.0, plot=TOPIC)
    state, _ = pipeline_module.run_stage2(
        {"script": TOPIC}, brief, _cfg(tmp_path), model=object(), checkpoint_dir=tmp_path)

    assert token_meter.totals()["input"] == 3000, "断点里的账没接回来（组装那笔钱丢了）"
    assert COST_TABLE_KEY not in state, "成本表不该由阶段写进 state（产出物是会话文件）"
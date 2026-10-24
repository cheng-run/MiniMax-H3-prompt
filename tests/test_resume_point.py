"""续跑点（issue #51）：答案账与读图校对进度的落盘/恢复。

CONTEXT.md「续跑点」：每答一问、每定稿一个计价产物即写盘；中断后从最后一个续跑点
继续，已定稿的不重跑不重付。本文件只测**纯件**（两张记录 + 会话往返），问答节奏
（什么时候问、什么时候建续跑点）在 ``test_wizard_resume_points.py``。

AC4 的牙齿在这里：两张记录都是**可证的落盘记录**——它们在 ``session_store`` 的
持久化白名单里，save/load 往返后逐字回来；形状坏了要响（本仓「falsy 错形不许被
`or {}` 洗成空表」的统一口径）。
"""
from __future__ import annotations

import pytest

from minimax_h3_prompt.asset_proofread import (
    Adjudication,
    Claim,
    Deviation,
    ProofreadProgress,
    PROGRESS_KEY,
    clear_progress,
    load_progress,
    pop_decision,
    progress_rows,
    store_progress,
)
from minimax_h3_prompt.brief_parser import Brief
from minimax_h3_prompt.resume_point import (
    ANSWERS_KEY,
    answer_book,
    forget,
    recall,
    remember,
)
from minimax_h3_prompt.session_store import load_session, save_session

GIRL_PROMPT = "人物·红裙女孩专属生图提示词：红裙、黑发、银项链"
SHA = "a" * 64


# ---------------------------------------------------------------------------
# 答案账：答过就记得，坏了就响
# ---------------------------------------------------------------------------

def test_answer_is_remembered_recalled_and_forgotten():
    """一条答案三件事：记住 → 读回 → 作废（产物落定后答案不再冒充事实）。"""
    state: dict = {}
    assert recall(state, "asset_path:红裙女孩") is None
    remember(state, "asset_path:红裙女孩", "D:/returns/girl.png")
    assert recall(state, "asset_path:红裙女孩") == "D:/returns/girl.png"

    forget(state, "asset_path:红裙女孩")
    assert recall(state, "asset_path:红裙女孩") is None, "作废后还读得到＝旧答案冒充新事实"


def test_wrong_shaped_answers_are_loud():
    """错形**响**：falsy 错形不许被 ``or {}`` 洗成空表（画布/校对记录同一口径）。"""
    with pytest.raises(ValueError, match="答案账"):
        answer_book({"resume_answers": ["红裙女孩"]})       # 应为对象
    with pytest.raises(ValueError, match="resume_answers"):
        answer_book({"resume_answers": {"k": 1}})           # 值应为字符串

    # 键缺失＝新会话：补出空账并**落回 state**（调用方改的那个就是它）
    state: dict = {}
    assert answer_book(state) == {}
    remember(state, "asset_path:红裙女孩", "a.png")
    assert state[ANSWERS_KEY]["asset_path:红裙女孩"] == "a.png"


# ---------------------------------------------------------------------------
# 读图校对进度：读图结果 + 已裁决条目 + 补漏回答
# ---------------------------------------------------------------------------

def _progress() -> ProofreadProgress:
    return ProofreadProgress(
        name="红裙女孩", sha256=SHA, base=GIRL_PROMPT,
        claims=(Claim("裙色", "红裙", "宝蓝色长裙"), Claim("腕表", "", "左腕银色腕表")),
    )


def test_progress_keeps_claims_and_decisions_round_trip():
    """进度记录往返：读图结果（claims）、已裁决条目、补漏回答逐字回来。"""
    adj = Adjudication(Deviation("裙色", "红裙", "宝蓝色长裙"), "rewrite", "宝蓝色长裙")
    progress = _progress().with_decision(adj).with_omissions(True)
    restored = ProofreadProgress.from_dict("红裙女孩", progress.to_dict())

    assert restored == progress
    assert restored.reading().claims[0].aspect == "裙色"
    assert restored.decisions[0].decision == "rewrite"
    assert restored.omissions is True


def test_unknown_field_in_progress_is_loud():
    """未知字段响错不吞（校对记录同口径）：账本被外人加列时不许装看不见。"""
    raw = _progress().to_dict()
    raw["mystery"] = 1
    with pytest.raises(ValueError, match="未知字段"):
        ProofreadProgress.from_dict("红裙女孩", raw)


def test_progress_is_reused_only_for_same_sha_and_same_base():
    """换版（sha 变）或改词（base 变）→ 旧进度作废：它记的是**另一张图/另一版词**。"""
    state: dict = {}
    store_progress(state, _progress())

    assert load_progress(state, "红裙女孩", sha256=SHA, base=GIRL_PROMPT) is not None
    assert load_progress(state, "红裙女孩", sha256="b" * 64, base=GIRL_PROMPT) is None
    assert load_progress(state, "红裙女孩", sha256=SHA, base=GIRL_PROMPT + "！") is None
    assert load_progress(state, "蓝衫少年", sha256=SHA, base=GIRL_PROMPT) is None


def test_progress_rows_are_loud_on_wrong_shape():
    with pytest.raises(ValueError, match="校对进度"):
        progress_rows({"asset_proofread_progress": []})


def test_clear_progress_removes_only_that_name():
    state: dict = {}
    store_progress(state, _progress())
    store_progress(state, ProofreadProgress(name="蓝衫少年", sha256=SHA, base="其他"))
    clear_progress(state, "红裙女孩")
    assert "红裙女孩" not in progress_rows(state)
    assert "蓝衫少年" in progress_rows(state)


def test_pop_decision_matches_three_fields_and_consumes_once():
    """按（aspect, prompt_quote, observed）三字段认领上次的裁决；领过的不再发第二次。"""
    dev = Deviation("裙色", "红裙", "宝蓝色长裙")
    other = Deviation("发型", "黑发", "短发")
    pool = [
        Adjudication(other, "keep"),
        Adjudication(dev, "rewrite", "宝蓝色长裙"),
    ]
    got = pop_decision(pool, dev)
    assert got is not None and got.decision == "rewrite"
    assert pop_decision(pool, dev) is None, "同一条裁决被认领了两次"
    assert pop_decision(pool, other).decision == "keep"


# ---------------------------------------------------------------------------
# AC4：两张记录都要活过重启（会话持久化白名单）
# ---------------------------------------------------------------------------

def _brief() -> Brief:
    return Brief(mode="ref", variant="T2VA", duration=6.0, plot="红裙女孩")


def test_resume_records_survive_session_round_trip(tmp_path):
    """续跑点必须是**可证的落盘记录**：save → load 后答案与裁决进度都还在。

    不在 ``_STATE_KEYS`` 白名单里的键会在 save 时静默丢弃——本仓见过两次
    （frame_descriptions、user_revisions），故这条测试盯的是白名单本身。
    """
    state: dict = {}
    remember(state, "asset_path:红裙女孩", "D:/returns/girl.png")
    store_progress(state, _progress().with_decision(
        Adjudication(Deviation("裙色", "红裙", "宝蓝色长裙"), "keep")))

    generation_dir = tmp_path / "GEN001"
    save_session(generation_dir, _brief(), state)
    loaded = load_session(generation_dir)

    assert loaded is not None
    assert recall(loaded.stage_state, "asset_path:红裙女孩") == "D:/returns/girl.png"
    progress = load_progress(loaded.stage_state, "红裙女孩", sha256=SHA, base=GIRL_PROMPT)
    assert progress is not None and progress.decisions[0].decision == "keep"
    assert PROGRESS_KEY in loaded.stage_state

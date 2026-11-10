"""_session_dir 续接/新建判定测试（真实验收流程：同 brief 可开新 GEN）。"""
from minimax_h3_prompt.brief_parser import Brief
from minimax_h3_prompt.session_store import (
    STATUS_AWAITING_FRAMES,
    STATUS_COMPLETED,
    save_session,
)
from minimax_h3_prompt.ui.wizard import _session_dir


def _brief(topic: str = "深夜便利店") -> Brief:
    return Brief(mode="base", variant="I2VA", duration=18.0, style="写实", plot=topic)


def test_new_topic_creates_gen001(tmp_path):
    d = _session_dir(type("C", (), {"sessions_root": str(tmp_path)})(), "全新主题")
    assert d.name == "GEN001"


def test_resumable_session_reused(tmp_path):
    """中断/等帧图会话（非 completed）→ 续接复用。"""
    cfg = type("C", (), {"sessions_root": str(tmp_path)})()
    first = _session_dir(cfg, "某主题")
    save_session(first, _brief(), {"k": "v"}, status=STATUS_AWAITING_FRAMES)
    again = _session_dir(cfg, "某主题")
    assert again == first


def test_completed_session_opens_next_gen(tmp_path):
    """已完成会话 → 自动开新 GEN002（真实验收：同 brief 重新生成）。"""
    cfg = type("C", (), {"sessions_root": str(tmp_path)})()
    first = _session_dir(cfg, "某主题")
    save_session(first, _brief(), {"k": "v"}, status=STATUS_COMPLETED)
    second = _session_dir(cfg, "某主题")
    assert second.name == "GEN002"
    assert second.parent == first.parent


def test_completed_then_resumable_chain(tmp_path):
    """GEN001 完成、GEN002 中断 → 第三次运行续接 GEN002（不跳号）。"""
    cfg = type("C", (), {"sessions_root": str(tmp_path)})()
    g1 = _session_dir(cfg, "某主题")
    save_session(g1, _brief(), {}, status=STATUS_COMPLETED)
    g2 = _session_dir(cfg, "某主题")
    save_session(g2, _brief(), {}, status=STATUS_AWAITING_FRAMES)
    assert _session_dir(cfg, "某主题") == g2


def test_no_session_file_opens_next_gen(tmp_path):
    """目录存在但无 session-state.json（如纯手工产物）→ 视为已完成，开新 GEN。"""
    cfg = type("C", (), {"sessions_root": str(tmp_path)})()
    first = _session_dir(cfg, "某主题")
    (first / "segments").mkdir()
    assert _session_dir(cfg, "某主题") != first


def test_workbench_session_is_not_reused_or_overwritten(tmp_path):
    """API 建的 workbench 会话（created）绝不被向导续接，原文件逐字不变（#215）。

    两者同用 ``sessions_root/<slug>/GEN00N`` 布局；旧的「非 completed 就复用」判据
    会让 ``_phase1_new`` 重跑阶段 1 并把 API 会话整份 save 覆盖——静默数据丢失。
    """
    cfg = type("C", (), {"sessions_root": str(tmp_path)})()
    seed = _session_dir(cfg, "某主题")
    save_session(seed, _brief(), {"k": "v"}, status="created",
                 session_id=f"{seed.parent.name}/GEN001")
    before = (seed / "session-state.json").read_bytes()

    again = _session_dir(cfg, "某主题")

    assert again.name == "GEN002", "向导复用了 API 建的工作台会话目录"
    assert (seed / "session-state.json").read_bytes() == before, "API 会话文件被向导改动"


def test_awaiting_frames_is_still_reused(tmp_path):
    """对照：新白名单没有把正常续接关掉（awaiting_frames 仍复用）。"""
    cfg = type("C", (), {"sessions_root": str(tmp_path)})()
    first = _session_dir(cfg, "某主题")
    save_session(first, _brief(), {"k": "v"}, status=STATUS_AWAITING_FRAMES)

    assert _session_dir(cfg, "某主题") == first


def test_resumable_statuses_is_the_single_guard_list():
    """可续接清单只有一份，且不收录工作台状态（两处共用同一常量）。"""
    from minimax_h3_prompt import session_store

    assert session_store.RESUMABLE_STATUSES == (
        session_store.STATUS_AWAITING_FRAMES,
        session_store.STATUS_STAGE1_RUNNING,
        session_store.STATUS_SEGMENTED_RUNNING,
        session_store.STATUS_REF_SEGMENTS_RUNNING,
        session_store.STATUS_DIRECTOR_SEGMENTS_RUNNING,
    )
    for status in ("created", "running", "awaiting_human", "failed",
                   "cancelled", "interrupted"):
        assert status not in session_store.RESUMABLE_STATUSES

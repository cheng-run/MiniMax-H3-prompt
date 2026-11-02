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

"""会话持久化测试：阶段 1 → 关终端 → 阶段 2 续跑时哪些 state 必须活下来。

bug（2026-09-22）：``fl2va_frame_descriptions``（首帧实际画面的读图结果）不在
``_STATE_KEYS`` 白名单里，save 时被静默丢弃 → 续跑后的分段流程不再锚定首帧图片。
"""
from minimax_h3_prompt.brief_parser import Brief
from minimax_h3_prompt.session_store import load_session, save_session

FRAME_DESC = "室内灯光明亮，收银台后站着一名年轻男性店员；前景下方偏左处一只橘色虎斑猫站在门口地砖上。"


def _brief() -> Brief:
    return Brief(mode="base", variant="I2VA", duration=18.0, style="写实", plot="便利店橘猫")


def test_frame_descriptions_survive_resume(tmp_path):
    """断点续跑后，首帧实际画面必须还在 state 里（否则分段又回去照分镜表写）。"""
    state = {
        "shot_table": "[Shot 1] 深夜空店内……",
        "fl2va_frame_descriptions": [
            {"picture": 1, "role": "first", "path": "frames/first.png", "description": FRAME_DESC},
        ],
    }
    generation_dir = tmp_path / "GEN001"
    save_session(generation_dir, _brief(), state)
    loaded = load_session(generation_dir)
    assert loaded is not None
    descriptions = loaded.stage_state.get("fl2va_frame_descriptions")
    assert descriptions, "续跑后首帧实际画面丢失 → 分段流程拿不到锚"
    assert descriptions[0]["description"] == FRAME_DESC


def test_brief_and_status_survive_resume(tmp_path):
    """基本往返：brief 字段与 status 原样回来（回归护栏）。"""
    generation_dir = tmp_path / "GEN001"
    save_session(generation_dir, _brief(), {"shot_table": "表"})
    loaded = load_session(generation_dir)
    assert loaded is not None
    assert loaded.brief.variant == "I2VA"
    assert loaded.brief.duration == 18.0
    assert loaded.awaiting_frames is True

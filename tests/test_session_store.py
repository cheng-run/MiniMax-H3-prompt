"""会话持久化测试：阶段 1 → 关终端 → 阶段 2 续跑时哪些 state 必须活下来。

bug（2026-09-22）：``fl2va_frame_descriptions``（首帧实际画面的读图结果）不在
``_STATE_KEYS`` 白名单里，save 时被静默丢弃 → 续跑后的分段流程不再锚定首帧图片。
"""
import pytest

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


def test_canvases_survive_resume(tmp_path):
    """用户选定的画布必须活过重启（issue #42）。

    brief 的持久化字段是**逐个显式列出**的（见 ``_brief_to_dict`` 的注释）——
    新字段漏登记就静默丢，重启后画布悄悄回落默认表，用户看不出差别直到图出完。
    """
    brief = Brief(mode="base", variant="I2VA", duration=18.0, style="写实", plot="便利店橘猫",
                  canvases={"zimage": (700, 1300), "flux2": (1024, 1024)})
    generation_dir = tmp_path / "GEN001"
    save_session(generation_dir, brief, {"shot_table": "表"})
    loaded = load_session(generation_dir)
    assert loaded is not None
    assert loaded.brief.canvases == {"zimage": (700, 1300), "flux2": (1024, 1024)}


def _corrupt_canvases(tmp_path, replacement: str) -> None:
    generation_dir = tmp_path / "GEN001"
    save_session(generation_dir, _brief(), {"shot_table": "表"})
    session_file = generation_dir / "session-state.json"
    raw = session_file.read_text(encoding="utf-8").replace('"canvases": {}', replacement)
    session_file.write_text(raw, encoding="utf-8")


@pytest.mark.parametrize("replacement", [
    '"canvases": {"zimage": "700x1300"}',   # 条目是字符串（错形）
    '"canvases": {"zimage": ["700", "1300"]}',  # 字符串数字——静默强转=数据被改
    '"canvases": {"zimage": [700.9, 1300.1]}',  # 浮点——静默截断=数据被改
    '"canvases": []',                        # 整字段错形（falsy 不许被 or {} 洗掉）
    '"canvases": 5',                         # 整字段标量
])
def test_malformed_canvases_in_session_file_is_loud(tmp_path, replacement):
    """会话文件里的错形画布必须报错，不许静默丢/静默改（issue #42 复审）。

    静默丢＝重启后画布悄悄回落默认表；静默强转/截断＝数据被改而不报错，比丢弃更糟。
    会话文件是系统自己写的，错形只可能是损坏。键缺失/null 仍合法（旧会话没有该键）。
    """
    _corrupt_canvases(tmp_path, replacement)

    with pytest.raises(ValueError):
        load_session(tmp_path / "GEN001")


def test_missing_canvases_key_still_reads(tmp_path):
    """键缺失/JSON null 是旧会话的既定形态——回落空表（默认兜底），不报错。"""
    _corrupt_canvases(tmp_path, '"canvases": null')
    assert load_session(tmp_path / "GEN001") is not None


def test_video_output_survives_resume(tmp_path):
    """用户选定的视频输出三元组必须活过重启（issue #45，画布同族：漏登记即静默丢）。"""
    from minimax_h3_prompt.injection_card import VideoOutputParams

    brief = Brief(mode="ref", variant="T2VA", duration=6.0, plot="主题",
                  video_output=VideoOutputParams(ratio="3:2 (Photo)", megapixels=1.0, multiple=16))
    generation_dir = tmp_path / "GEN001"
    save_session(generation_dir, brief, {"shot_table": "表"})
    loaded = load_session(generation_dir)
    assert loaded is not None
    assert (loaded.brief.video_output.ratio,
            loaded.brief.video_output.megapixels,
            loaded.brief.video_output.multiple) == ("3:2 (Photo)", 1.0, 16)


def _corrupt_video_output(tmp_path, replacement: str) -> None:
    generation_dir = tmp_path / "GEN001"
    save_session(generation_dir, _brief(), {"shot_table": "表"})
    session_file = generation_dir / "session-state.json"
    raw = session_file.read_text(encoding="utf-8").replace('"video_output": null', replacement)
    session_file.write_text(raw, encoding="utf-8")


@pytest.mark.parametrize("replacement", [
    '"video_output": ["9:16 (Portrait Widescreen)", 0.4, 32]',  # 整字段错形
    '"video_output": {"ratio": "5:6", "megapixels": 0.4, "multiple": 32}',   # 档外比例
    '"video_output": {"ratio": "9:16 (Portrait Widescreen)", "megapixels": "1.0", "multiple": 32}',
    '"video_output": {"ratio": "9:16 (Portrait Widescreen)", "megapixels": 0.4, "multiple": 32.9}',
])
def test_malformed_video_output_in_session_file_is_loud(tmp_path, replacement):
    """错形三元组必须报错，不许静默强转/截断（画布同纪律，issue #45 复审）。

    `"1.0"` 被 float() 强转、32.9 被 int() 截断都是「数据被改而不报错」——
    会话文件是系统自己写的，错形只可能是损坏或外人改写。
    """
    _corrupt_video_output(tmp_path, replacement)
    with pytest.raises(ValueError):
        load_session(tmp_path / "GEN001")


def test_missing_video_output_key_still_reads(tmp_path):
    """键缺失/null ＝ 未定案（旧会话没有该键）：消费方按画布自动推导（issue #49），
    不报错。"""
    _corrupt_video_output(tmp_path, '"video_output": null')
    loaded = load_session(tmp_path / "GEN001")
    assert loaded is not None and loaded.brief.video_output is None


def test_find_awaiting_sessions_skips_damaged_file_with_warning(tmp_path, capsys):
    """恢复扫描隔离单文件损坏：警告 + 跳过，不炸掉整个扫描（复审：此容错曾零守卫）。"""
    from minimax_h3_prompt.session_store import STATUS_AWAITING_FRAMES, find_awaiting_sessions

    root = tmp_path / "sessions"
    bad_dir = root / "topic" / "GEN001"
    good_dir = root / "topic" / "GEN002"
    save_session(bad_dir, _brief(), {"shot_table": "表"}, status=STATUS_AWAITING_FRAMES)
    save_session(good_dir, _brief(), {"shot_table": "表"}, status=STATUS_AWAITING_FRAMES)
    bad_file = bad_dir / "session-state.json"
    bad_file.write_text(bad_file.read_text(encoding="utf-8").replace(
        '"canvases": {}', '"canvases": []'), encoding="utf-8")

    found = find_awaiting_sessions(root)
    assert [s.directory.name for s in found] == ["GEN002"], "损坏会话没被跳过（或好会话被误伤）"
    assert "GEN001" in capsys.readouterr().out, "跳过损坏会话没有可见警告——静默消失也不行"


def test_user_revisions_survive_resume(tmp_path):
    """累积的用户修订与轮次号必须活过重启（issue #23）。

    与上面那条同族：真源不在 ``_STATE_KEYS`` 白名单里，save 时静默丢弃 →
    用户重启后要说的话得再说一遍（这正是本票要消灭的行为）。
    """
    state = {
        "user_revisions": [
            {"round": 1, "layer": "frame", "text": "天空改成黄昏"},
            {"round": 2, "layer": "frame", "text": "院中加红枫"},
        ],
        "frame_round": 2,
    }
    generation_dir = tmp_path / "GEN001"
    save_session(generation_dir, _brief(), state)
    loaded = load_session(generation_dir)
    assert loaded is not None
    assert [item["text"] for item in loaded.stage_state["user_revisions"]] == [
        "天空改成黄昏", "院中加红枫"]
    assert loaded.stage_state["frame_round"] == 2

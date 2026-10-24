"""会话持久化测试：阶段 1 → 关终端 → 阶段 2 续跑时哪些 state 必须活下来。

bug（2026-09-22）：``fl2va_frame_descriptions``（首帧实际画面的读图结果）不在
``_STATE_KEYS`` 白名单里，save 时被静默丢弃 → 续跑后的分段流程不再锚定首帧图片。
"""
import json
import os
from pathlib import Path

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
    canvases 里的 flux2 键是旧会话残留（ADR 0013 全链退役）：**原样往返、无人读**，
    载入不校验不报错——历史数据按弃读惯例处理（issue #48）。
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


# ---------------------------------------------------------------------------
# 成本表随续跑点落盘（issue #52）
# ---------------------------------------------------------------------------

@pytest.fixture
def _clean_meter():
    """计量表是模块级单例：用例前后都清空，免得别的用例的账混进这里的断言。"""
    from minimax_h3_prompt.observability import token_meter

    token_meter.reset()
    yield token_meter
    token_meter.reset()


def test_cost_table_rides_every_session_save(tmp_path, _clean_meter):
    """会话一存，成本表的当前快照就在里面（issue #52 AC3：随续跑点持久化）。

    快照不是产品给的另一份数据，而是计量表当下的全量账——落盘时机就是续跑点，
    所以它在 ``save_session`` 里自动带上，任何一条保存路径都不必自己记得写。
    """
    from minimax_h3_prompt.observability import COST_TABLE_KEY

    _clean_meter.add("导演", 1200, 300)
    generation_dir = tmp_path / "GEN001"
    save_session(generation_dir, _brief(), {"shot_table": "表"})

    loaded = load_session(generation_dir)
    assert loaded is not None
    assert loaded.stage_state[COST_TABLE_KEY]["导演"] == {
        "calls": 1, "input": 1200, "output": 300}


def test_saved_cost_table_is_the_live_account_not_the_stale_copy(tmp_path, _clean_meter):
    """state 里那份旧快照不许覆盖活账：落盘取的是计量表**此刻**的数。

    这条守的是「落盘 ≈ 续跑点」这个承诺的成色——续跑窗口里 LLM/读图还在花钱，
    state 里带的快照却是上一次保存时的旧数；照抄它就等于把这段时间的成本抹掉。
    """
    from minimax_h3_prompt.observability import COST_TABLE_KEY

    stale = {COST_TABLE_KEY: {"导演": {"calls": 9, "input": 900, "output": 900}}}
    _clean_meter.add("导演", 100, 20)
    generation_dir = tmp_path / "GEN001"
    save_session(generation_dir, _brief(), {"shot_table": "表", **stale})

    loaded = load_session(generation_dir)
    assert loaded is not None
    assert loaded.stage_state[COST_TABLE_KEY]["导演"] == {
        "calls": 1, "input": 100, "output": 20}, "旧快照盖过了活账（成本被抹掉）"


# ---------------------------------------------------------------------------
# 路线过滤：资产图提示词三键只属于多参路线（issue #80 / 父 spec #77）
# ---------------------------------------------------------------------------

_ASSET_KEYS = ("character_image_prompts", "prop_image_prompts", "scene_image_prompts")

# 真实旧会话样本（仓库外、`output/` 已 gitignore）：有则验真、无则 SKIP。
# 与 tests/conftest.py 的机器专属素材同一口径——换机器请设 H3_SESSION_SAMPLES。
_REAL_SAMPLES = Path(os.environ.get("H3_SESSION_SAMPLES")
                     or r"D:/项目/python/MiniMax-H3-prompt/output/sessions")


def _asset_prompt_state() -> dict:
    """修复前首尾帧会话的形状：三键齐全（真实旧样本 10 份里有 3 份就是这形态）。"""
    return {
        "shot_table": "[Shot 1] 深夜空店内……",
        "fl2va_frame_descriptions": [
            {"picture": 1, "role": "first", "description": FRAME_DESC},
        ],
        "character_image_prompts": [{"name": "店员", "positive_prompt": "年轻男性店员，白衬衫"}],
        "prop_image_prompts": [{"name": "收银台", "positive_prompt": "不锈钢收银台"}],
        "scene_image_prompts": [{"name": "便利店", "positive_prompt": "深夜便利店，冷白灯"}],
    }


def test_base_session_drops_asset_prompt_keys_on_load(tmp_path):
    """首尾帧会话读回后，内存里不许再有资产图提示词三键（issue #80）。

    修复前的首尾帧会话把多参的三份提示词写进了 stage_state；续跑时原样读回，就等于
    把另一条路线的产物带回来了（2026-10-23 用户现场把它看成「首帧路线变成了多参」）。
    路线过滤在**读回**这一个入口做掉。

    同时钉住「别顺手多删」：首帧读图结果（fl2va_frame_descriptions）是阶段 2 分段
    唯一的画面事实源，误删它比残留三键更糟。
    """
    generation_dir = tmp_path / "GEN001"
    save_session(generation_dir, _brief(), _asset_prompt_state())

    loaded = load_session(generation_dir)
    assert loaded is not None
    assert not [k for k in _ASSET_KEYS if k in loaded.stage_state], \
        "首尾帧会话读回仍带着资产图提示词"
    assert loaded.stage_state["fl2va_frame_descriptions"][0]["description"] == FRAME_DESC
    assert loaded.stage_state["shot_table"].startswith("[Shot 1]")


def test_base_session_save_after_load_drops_asset_prompt_keys_from_file(tmp_path):
    """续跑一次再保存，会话文件里也不该再有那三键（issue #80 AC1 的文件侧）。

    读入口本身**不改写文件**（扫描续接会话时 load 是纯读，回写会波及用户目录里每一个
    会话），所以文件侧靠「过滤后的 state 再落盘」自然收敛；这条用例钉住这个收敛，
    别让将来的保存路径从别处又把三键捞回去。
    """
    generation_dir = tmp_path / "GEN001"
    save_session(generation_dir, _brief(), _asset_prompt_state())
    session_file = generation_dir / "session-state.json"
    before = json.loads(session_file.read_text(encoding="utf-8"))["stage_state"]
    assert [k for k in _ASSET_KEYS if k in before], "夹具没把三键写进文件，这条用例会空转"

    loaded = load_session(generation_dir)
    assert loaded is not None
    loaded.save()

    on_disk = json.loads(session_file.read_text(encoding="utf-8"))["stage_state"]
    assert not [k for k in _ASSET_KEYS if k in on_disk], "续跑保存后文件里仍带着资产图提示词"


def test_ref_session_keeps_asset_prompt_keys_and_proofreads(tmp_path):
    """多参会话读回时三键与读图校对记录原样保留（issue #80 AC2）。

    多参每件资产要单独出图，三键是素材台账与素材循环的输入；读回丢键等于中断续跑后
    素材全丢、读图校对白做。**夹具是自造的多参会话**——仓库里没有修复前的真实多参
    会话样本（`output/sessions/` 十份全是 base/I2VA）。
    """
    brief = Brief(mode="ref", variant="T2VA", duration=8.0, plot="雨夜出租车")
    state = _asset_prompt_state()
    state["asset_proofreads"] = {
        "店员": {"sha256": "abc123", "status": "approved", "description": "短寸头，白衬衫"},
    }
    generation_dir = tmp_path / "GEN002"
    save_session(generation_dir, brief, state)

    loaded = load_session(generation_dir)
    assert loaded is not None
    assert loaded.brief.mode == "ref"
    for key in _ASSET_KEYS:
        assert loaded.stage_state[key] == state[key], f"多参会话读回丢了 {key}"
    assert loaded.stage_state["asset_proofreads"] == state["asset_proofreads"]


def test_real_old_sessions_filter_by_route():
    """用真实旧会话验路线过滤（issue #80 AC4）：首尾帧样本读回无三键。

    逐份比对「文件里的三键」与「读回后的三键」：首尾帧应为空，多参应原样。这些是
    用户跑到一半的真实产物，**只读**，不复制也不写回。样本目录不在（清洁检出、换
    机器）就 SKIP；样本里连一份带三键的旧首尾帧会话都没有时同样 SKIP——那种情况下
    这条用例已经退化成空转，报绿就是骗人。
    """
    if not _REAL_SAMPLES.is_dir():
        pytest.skip(f"真实会话样本目录不存在：{_REAL_SAMPLES}")
    files = sorted(_REAL_SAMPLES.glob("**/session-state.json"))
    snapshots = []
    for path in files:
        raw = json.loads(path.read_text(encoding="utf-8"))
        before = [k for k in _ASSET_KEYS if k in raw.get("stage_state", {})]
        mode = raw.get("brief", {}).get("mode", "base")
        snapshots.append((path, mode, before))
    if not [s for s in snapshots if s[1] != "ref" and s[2]]:
        pytest.skip(f"样本里没有带三键的旧首尾帧会话：{_REAL_SAMPLES}")

    for path, mode, before in snapshots:
        loaded = load_session(path.parent)
        assert loaded is not None, f"真实样本读不回来：{path}"
        after = [k for k in _ASSET_KEYS if k in loaded.stage_state]
        # 期望值取**文件里**的路线，不取解析后的 brief.mode：解析若把路线读错，
        # 用它当期望就把这条用例变成自证（路线读错时两边一起变，永远绿）。
        expected = before if mode == "ref" else []
        assert after == expected, f"{path}：读回后三键 {after} 与路线不符（期望 {expected}）"

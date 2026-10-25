"""工作流配置读取口（issue #65 / issue #86）：登记名 + 三型参考槽位数。

判据全部是**外部行为**：给一个 YAML 文件，读出来的登记名与槽位数是什么；缺文件/
缺字段时是否回落默认值。V2 登记名与 9/3/3 这三个默认值来自票面 #86（独立来源），
不从实现里抄——实现若把默认值改掉，这里当场红。

2026-10-25（issue #86 / spec #84 D5）：官方参考上限是 ≤9 图，登记的 24 槽 V3 从一开始
就不该是默认路径（24 张全喂在 16GB 卡上实测 OOM）。登记回 V2 的 9 图槽版本。
"""
from __future__ import annotations

import pytest

from minimax_h3_prompt import workflow_config
from minimax_h3_prompt.asset_ledger import KIND_AUDIO, KIND_PICTURE, KIND_VIDEO
from minimax_h3_prompt.workflow_config import (
    DEFAULT_REF_WORKFLOW_NAME,
    DEFAULT_SLOT_COUNTS,
    load_workflow_settings,
    workflow_settings,
)

# 票面 #86 钉死的字面量：V2 是登记的 9 图槽版本（V3/24 槽已退役，见票面 D6）。
V2_WORKFLOW_NAME = "59-(多Lora改)MiniMax-H3 ref2va 多参生视频 V2"


def test_missing_config_file_falls_back_to_defaults(tmp_path):
    """配置缺失不崩溃：图 9 / 视频 3 / 音频 3，登记名仍是 V2（票面 AC1）。"""
    settings = load_workflow_settings(tmp_path / "absent.yaml")
    assert settings.slot_counts == {KIND_PICTURE: 9, KIND_VIDEO: 3, KIND_AUDIO: 3}
    assert settings.ref_workflow_name == V2_WORKFLOW_NAME


def test_missing_fields_fall_back_per_field(tmp_path):
    """只缺一项就只回落那一项：写了的槽位数必须生效，别整份配置被默认值盖掉。"""
    path = tmp_path / "workflow.yaml"
    path.write_text("slots:\n  picture: 6\n", encoding="utf-8")
    settings = load_workflow_settings(path)
    assert settings.slot_counts == {KIND_PICTURE: 6, KIND_VIDEO: 3, KIND_AUDIO: 3}
    assert settings.ref_workflow_name == V2_WORKFLOW_NAME


def test_config_file_drives_the_name_and_counts(tmp_path):
    """配置写什么读什么：这是「换工作流只改配置」的唯一入口。"""
    path = tmp_path / "workflow.yaml"
    path.write_text(
        'ref_workflow_name: "某个新工作流 V9"\n'
        "slots:\n"
        "  picture: 12\n"
        "  video: 2\n"
        "  audio: 1\n",
        encoding="utf-8",
    )
    settings = load_workflow_settings(path)
    assert settings.ref_workflow_name == "某个新工作流 V9"
    assert settings.slot_counts == {KIND_PICTURE: 12, KIND_VIDEO: 2, KIND_AUDIO: 1}


@pytest.mark.parametrize("body,needle", [
    ("slots:\n  picture: 0\n", "picture"),     # 0 槽＝没有端口，写进配置必是笔误
    ("slots:\n  picture: 2.5\n", "picture"),   # 槽位是端口个数，只能是正整数
    ("slots:\n  picture: yes\n", "picture"),   # YAML 的 yes 会解析成 bool（isinstance int 的坑）
    ("slots:\n  pictures: 24\n", "pictures"),  # 拼错的键若静默略过，用户会以为边界改了
    ("slots: []\n", "slots"),                  # 写了但写错：不是「没写」，不能回落默认
])
def test_invalid_slot_counts_are_loud(tmp_path, body, needle):
    """写了非法值不许静默回默认（那会把笔误藏起来）：报错点名是哪一型/哪个键。"""
    path = tmp_path / "workflow.yaml"
    path.write_text(body, encoding="utf-8")
    with pytest.raises(ValueError) as excinfo:
        load_workflow_settings(path)
    assert needle in str(excinfo.value)


def test_active_loader_reads_the_configured_path(tmp_path, monkeypatch):
    """生效路径必须真的读盘：把路径换成临时配置，读出来的边界就得跟着变。

    守卫「把来源改坏会不会报红」：若实现退回写死常量、或忽略 WORKFLOW_CONFIG_PATH，
    这条立刻红——这是「改配置即改边界」的最小证据。
    """
    path = tmp_path / "workflow.yaml"
    path.write_text("slots:\n  picture: 2\n", encoding="utf-8")
    monkeypatch.setattr(workflow_config, "WORKFLOW_CONFIG_PATH", path)
    assert workflow_config.workflow_settings().slot_counts[KIND_PICTURE] == 2


def test_repo_config_registers_v2_with_nine_picture_slots():
    """仓库里的 config/workflow.yaml 就是生效的那份：V2 名 + 9 图槽（票面 AC5）。

    「不再出现 24 槽或 V3 字样」在**用户可见的登记口**上判：卡与菜单都从这份配置取。
    """
    settings = workflow_settings()
    assert settings.ref_workflow_name == V2_WORKFLOW_NAME
    assert "V3" not in settings.ref_workflow_name and "24" not in settings.ref_workflow_name
    assert settings.slot_counts == {KIND_PICTURE: 9, KIND_VIDEO: 3, KIND_AUDIO: 3}
    # 默认值与票面同源（缺文件时才有意义，但两处必须一致——否则「缺配置」是另一种口径）
    assert DEFAULT_REF_WORKFLOW_NAME == V2_WORKFLOW_NAME
    assert DEFAULT_SLOT_COUNTS == {KIND_PICTURE: 9, KIND_VIDEO: 3, KIND_AUDIO: 3}


def test_repo_config_file_itself_names_v2_and_nine_slots():
    """直接读盘上那份 YAML：登记值不许再写 V3/24（防止只改常量、配置文件被漏掉）。

    只判 ``ref_workflow_name:`` 这一行与 ``picture:`` 这个值——注释里提历史是允许的
    （解释「为什么退役」的注释比沉默有用），但**生效值**必须已是 V2/9。
    """
    from minimax_h3_prompt.workflow_config import WORKFLOW_CONFIG_PATH

    lines = WORKFLOW_CONFIG_PATH.read_text(encoding="utf-8").splitlines()
    registered = next(line for line in lines if line.startswith("ref_workflow_name:"))
    assert V2_WORKFLOW_NAME in registered
    assert "V3" not in registered
    assert "  picture: 9" in "\n".join(lines)


# --- 每段参考图上限（issue #75）------------------------------------------------
# 这是**另一个**边界：槽位数是工作流端口数（登记 V2 后同为 9），每段上限是单次生成
# 真正喂几张。24 张全喂会 OOM（ADR 0017「代价」），所以默认 9 是票面钉死的字面量。

def test_missing_config_uses_nine_refs_per_segment(tmp_path):
    """缺文件 → 每段 9 张（issue #75 的默认值）。"""
    assert load_workflow_settings(tmp_path / "absent.yaml").refs_per_segment == 9


def test_config_file_drives_refs_per_segment(tmp_path):
    """写什么读什么：换显存/换工作流只改这一处。"""
    path = tmp_path / "workflow.yaml"
    path.write_text("refs_per_segment: 6\n", encoding="utf-8")
    assert load_workflow_settings(path).refs_per_segment == 6


@pytest.mark.parametrize("body", [
    "refs_per_segment: 0\n",      # 0 张＝段内无参照，必是笔误
    "refs_per_segment: 2.5\n",    # 张数只能是整数
    "refs_per_segment: yes\n",    # YAML 的 yes 是 bool（isinstance int 的坑）
    'refs_per_segment: "9"\n',    # 字符串数字不静默转换
])
def test_invalid_refs_per_segment_is_loud(tmp_path, body):
    """写了非法值要报（静默回默认会把笔误藏起来）。"""
    path = tmp_path / "workflow.yaml"
    path.write_text(body, encoding="utf-8")
    with pytest.raises(ValueError) as excinfo:
        load_workflow_settings(path)
    assert "refs_per_segment" in str(excinfo.value)


def test_repo_config_caps_each_segment_at_nine_pictures():
    """仓库生效配置：每段 9 张（与 ADR 0017 的「每段默认 9」同源）。"""
    assert workflow_settings().refs_per_segment == 9


def test_registered_name_constant_derives_from_the_config():
    """菜单/向导/产物共用的 `REF_WORKFLOW_NAME` 必须从配置派生（票面 AC5 单一真源）。

    若这里还留着旧的 V2 字面量，菜单与注入卡就会指两个工作流——用户按菜单进
    ComfyUI 找不到对应那张。
    """
    from minimax_h3_prompt import generation

    assert generation.REF_WORKFLOW_NAME == workflow_settings().ref_workflow_name
    assert generation.REF_WORKFLOW_NAME == V2_WORKFLOW_NAME


def test_user_facing_exits_read_the_registered_name_at_call_time(tmp_path, monkeypatch):
    """登记名在**调用时**取，不是 import 时求值（复审 S4）。

    原先 ``generation.REF_WORKFLOW_NAME = workflow_settings().ref_workflow_name`` 在
    import 那一刻定值：换一份配置后，注入卡会用新名字，菜单/登记行却还指着旧名字——
    用户按菜单进 ComfyUI 找不到那张工作流，而且**不报错**。判据：把生效路径换到
    临时 YAML 后，别名与注入卡都必须当场跟着变（monkeypatch 退出即恢复）。
    """
    from minimax_h3_prompt import generation, injection_card, workflow_config
    from minimax_h3_prompt.asset_ledger import AssetLedger
    from minimax_h3_prompt.injection_card import VideoOutputParams

    path = tmp_path / "workflow.yaml"
    path.write_text('ref_workflow_name: "临时工作流 V9"\n', encoding="utf-8")
    monkeypatch.setattr(workflow_config, "WORKFLOW_CONFIG_PATH", path)

    assert generation.REF_WORKFLOW_NAME == "临时工作流 V9", (
        "兼容别名仍停在 import 时的旧值——改配置不会改用户可见的登记名")
    text = injection_card.render_injection_card(AssetLedger(), VideoOutputParams())
    assert "临时工作流 V9" in text and V2_WORKFLOW_NAME not in text

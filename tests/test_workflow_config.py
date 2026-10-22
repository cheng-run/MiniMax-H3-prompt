"""工作流配置读取口（issue #65）：登记名 + 三型参考槽位数。

判据全部是**外部行为**：给一个 YAML 文件，读出来的登记名与槽位数是什么；缺文件/
缺字段时是否回落默认值。V3 登记名与 24/3/3 这三个默认值来自票面 #65（独立来源），
不从实现里抄——实现若把默认值改掉，这里当场红。
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

# 票面 #65 钉死的字面量：V3 是「24 图槽」那个工作流，与 V2 并存（V2 留在本机作退路）。
V3_WORKFLOW_NAME = "59-(多Lora改)MiniMax-H3 ref2va 多参生视频 V3（24图槽）"


def test_missing_config_file_falls_back_to_defaults(tmp_path):
    """配置缺失不崩溃：图 24 / 视频 3 / 音频 3，登记名仍是 V3（票面 AC1）。"""
    settings = load_workflow_settings(tmp_path / "absent.yaml")
    assert settings.slot_counts == {KIND_PICTURE: 24, KIND_VIDEO: 3, KIND_AUDIO: 3}
    assert settings.ref_workflow_name == V3_WORKFLOW_NAME


def test_missing_fields_fall_back_per_field(tmp_path):
    """只缺一项就只回落那一项：写了的槽位数必须生效，别整份配置被默认值盖掉。"""
    path = tmp_path / "workflow.yaml"
    path.write_text("slots:\n  picture: 6\n", encoding="utf-8")
    settings = load_workflow_settings(path)
    assert settings.slot_counts == {KIND_PICTURE: 6, KIND_VIDEO: 3, KIND_AUDIO: 3}
    assert settings.ref_workflow_name == V3_WORKFLOW_NAME


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


def test_repo_config_registers_v3_with_24_picture_slots():
    """仓库里的 config/workflow.yaml 就是生效的那份：V3 名 + 24 图槽（票面 AC5）。"""
    settings = workflow_settings()
    assert settings.ref_workflow_name == V3_WORKFLOW_NAME
    assert settings.slot_counts == {KIND_PICTURE: 24, KIND_VIDEO: 3, KIND_AUDIO: 3}
    # 默认值与票面同源（缺文件时才有意义，但两处必须一致——否则「缺配置」是另一种口径）
    assert DEFAULT_REF_WORKFLOW_NAME == V3_WORKFLOW_NAME
    assert DEFAULT_SLOT_COUNTS == {KIND_PICTURE: 24, KIND_VIDEO: 3, KIND_AUDIO: 3}


def test_registered_name_constant_derives_from_the_config():
    """菜单/向导/产物共用的 `REF_WORKFLOW_NAME` 必须从配置派生（票面 AC5 单一真源）。

    若这里还留着旧的 V2 字面量，菜单与注入卡就会指两个工作流——用户按菜单进
    ComfyUI 找不到对应那张。
    """
    from minimax_h3_prompt import generation

    assert generation.REF_WORKFLOW_NAME == workflow_settings().ref_workflow_name
    assert generation.REF_WORKFLOW_NAME == V3_WORKFLOW_NAME

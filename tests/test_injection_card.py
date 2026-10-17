"""注入卡（injection card）与视频侧尺寸契约（issue #45）。

注入卡是纯文本接线清单：素材台账逐条映射到 59- 工作流参考槽（`ref_image_N` 等，
编号按剧本出场顺序）＋视频输出参数三元组＋登记工作流名；用户照卡进 ComfyUI
手动接线，系统不自动投喂。本文件钉住四件外部行为：

1. **尺寸推导**：「比例档 + megapixels + 对齐倍数 → 最终 W×H」与工作流节点
   （JZL_HailuoH3VideoParamsPro）的官方 ResolutionSelector 公式逐档一致——
   金值独立来源见 DERIVE_CASES 注释，不是从实现抄来的；
2. **10 档预设比例**：标签＝节点 widget 值（可照抄），无任意比例；
3. **槽位映射**：素材名→槽位编号按剧本出场顺序；
4. **卡文本**：四条验收点（映射/三元组/登记名/不进提示词正文）。
"""
from __future__ import annotations

import pytest

from minimax_h3_prompt.asset_ledger import (
    KIND_AUDIO,
    KIND_PICTURE,
    KIND_VIDEO,
    AssetLedger,
    AssetRecord,
)
from minimax_h3_prompt.injection_card import (
    CARD_FILENAME,
    VIDEO_RATIO_PRESETS,
    VideoOutputParams,
    render_injection_card,
    slot_name,
    write_injection_card,
)

# ---------------------------------------------------------------------------
# 1. 尺寸推导：与 59- 工作流节点同口径（AC2）
# ---------------------------------------------------------------------------

# (比例档, megapixels, 对齐倍数, 期望宽, 期望高)。金值**独立于实现**：
# - 公式出处＝JZL_HailuoH3VideoParamsPro（ComfyUI-JZL-MiniMax-H3/nodes_hailuo_video.py）
#   的官方 ResolutionSelector 复刻：total=MP×1024²，scale=√(total/(w·h))，
#   宽高各 round(ratio×scale/倍数)×倍数；
# - 9:16 · 0.4 · 32 → 480×864 是 **59- 工作流 JSON 存档 widget 显示值**（「480 × 864」）
#   的独立对账——实现若抄错公式，这条当场红；
# - 其余三行按同一公式手工演算：1:1·1.0·32 → scale=1024，1024/32 恰 32 档 → 1024×1024；
#   16:9·1.0·32 → scale≈85.3333，42.667→43 档、24→24 档 → 1376×768；
#   21:9·0.4·32 → scale≈47.1085，30.915→31 档、13.249→13 档 → 992×416。
DERIVE_CASES = (
    ("9:16 (Portrait Widescreen)", 0.4, 32, 480, 864),
    ("1:1 (Square)", 1.0, 32, 1024, 1024),
    ("16:9 (Widescreen)", 1.0, 32, 1376, 768),
    ("21:9 (Ultrawide)", 0.4, 32, 992, 416),
)


@pytest.mark.parametrize("ratio,megapixels,multiple,width,height", DERIVE_CASES)
def test_video_size_derivation_matches_the_workflow_node(ratio, megapixels, multiple,
                                                         width, height):
    """「比例档 + megapixels + 对齐倍数 → 最终 W×H」逐档与节点推导口径一致。"""
    params = VideoOutputParams(ratio=ratio, megapixels=megapixels, multiple=multiple)
    assert params.size == (width, height)


# 三元组的字段就是节点 widget 名（aspect_ratio / megapixels / multiple），
# 最终 W×H 只由推导得出——「不支持任意宽高直填」在类型面上就没有入口。
def test_video_output_params_carry_the_widget_triple_only():
    """三元组＋推导：W×H 不是输入字段（不支持任意宽高直填）。"""
    params = VideoOutputParams(ratio="9:16 (Portrait Widescreen)", megapixels=0.4)
    assert (params.ratio, params.megapixels, params.multiple) == (
        "9:16 (Portrait Widescreen)", 0.4, 32)  # 对齐倍数默认 32（票面）
    assert set(vars(params)) == {"ratio", "megapixels", "multiple"}
    assert params.size == (480, 864)


# 坏值大声报（parse_canvas_size 同纪律）：静默回落默认＝「用户以为选了、实际没选」。
# needle 是报错消息里的稳定词（全中文消息，字段名只在 megapixels/对齐倍数口径里保留英文）。
REJECT_CASES = (
    ("比例", {"ratio": "5:6", "megapixels": 0.4}),           # 档外比例：无任意比例
    ("比例", {"ratio": "480x864", "megapixels": 0.4}),       # 宽高直填：节点不收
    ("megapixels", {"ratio": "1:1 (Square)", "megapixels": 0.0}),   # 节点 min=0.1
    ("megapixels", {"ratio": "1:1 (Square)", "megapixels": 17.0}),  # 节点 max=16.0
    ("对齐倍数", {"ratio": "1:1 (Square)", "megapixels": 1.0, "multiple": 4}),    # min=8
    ("对齐倍数", {"ratio": "1:1 (Square)", "megapixels": 1.0, "multiple": 256}),  # max=128
)


@pytest.mark.parametrize("needle,kwargs", REJECT_CASES)
def test_out_of_contract_values_are_rejected_loudly(needle, kwargs):
    """档外比例/越界 megapixels/越界对齐倍数当场报错，不静默回落。"""
    with pytest.raises(ValueError) as excinfo:
        VideoOutputParams(**kwargs)
    assert needle in str(excinfo.value)


# ---------------------------------------------------------------------------
# 2. 槽位映射：素材名→槽位，编号按剧本出场顺序（AC1 的映射口径）
# ---------------------------------------------------------------------------

SLOT_CASES = (
    ("picture", 1, "ref_image_0"),   # <Picture 1> 进第一个图槽：编号即叙事顺序
    ("picture", 9, "ref_image_8"),
    ("video", 1, "ref_video_0"),
    ("audio", 1, "ref_audio_0"),
    ("audio", 3, "ref_audio_2"),
)


@pytest.mark.parametrize("kind,number,expected", SLOT_CASES)
def test_slot_name_maps_ledger_number_to_workflow_slot(kind, number, expected):
    """台账编号 N → 工作流槽位（0 起）：`ref_image_N` 等与节点端口名逐字一致。"""
    assert slot_name(kind, number) == expected


def test_slot_name_rejects_numbers_beyond_the_physical_ports():
    """槽位是真实端口名：越界编号不造名（图 9 / 视频 3 / 音频 3 是硬端口数）。

    第 10 张图若渲成 `ref_image_9`，照卡接线会接到不存在的端口——接线清单的
    最低义务是每行都能接得上（复审 (a)1）。
    """
    with pytest.raises(ValueError) as excinfo:
        slot_name("picture", 10)
    assert "9" in str(excinfo.value), "报错要给出端口数（可赢信息）"
    with pytest.raises(ValueError):
        slot_name("video", 4)
    with pytest.raises(ValueError):
        slot_name("audio", 4)


def test_preset_table_carries_exactly_the_ten_node_widget_labels():
    """10 档预设比例：标签＝节点 ASPECT_RATIOS 的 widget 值（照抄口径），无任意比例。"""
    labels = [preset.label for preset in VIDEO_RATIO_PRESETS]
    assert labels == [
        "1:1 (Square)",
        "2:3 (Portrait Photo)",
        "3:2 (Photo)",
        "3:4 (Portrait Standard)",
        "4:5 (Portrait Tall)",
        "4:3 (Standard)",
        "5:4 (Landscape Tall)",
        "9:16 (Portrait Widescreen)",
        "16:9 (Widescreen)",
        "21:9 (Ultrawide)",
    ]


# ---------------------------------------------------------------------------
# 3. 卡文本：四条验收点（AC1/AC2/AC3）＋缺号/音轨附注
# ---------------------------------------------------------------------------

def _entry(name: str, kind: str, number: int, path: str) -> AssetRecord:
    return AssetRecord(name=name, kind=kind, number=number, path=path,
                       sha256="0" * 64)


def _ledger_with(*entries: AssetRecord) -> AssetLedger:
    return AssetLedger(entries=tuple(entries))


def test_card_maps_each_asset_to_its_slot_in_script_appearance_order():
    """AC1：素材名逐条映射到参考槽，编号按剧本出场顺序；纯文本产出。"""
    ledger = _ledger_with(
        _entry("红裙女孩", KIND_PICTURE, 1, "assets/picture-01.png"),
        _entry("秋日庭院", KIND_PICTURE, 2, "assets/picture-02.png"),
        _entry("运镜参考", KIND_VIDEO, 1, "assets/video-01.mp4"),
        _entry("红裙女孩音色", KIND_AUDIO, 1, "assets/audio-01.wav"),
    )
    text = render_injection_card(ledger, VideoOutputParams())
    assert isinstance(text, str)
    # 素材名 → 槽位逐条对上号；行序＝台账序（图按出场序、视频/音频型内序）
    for line in (
        "ref_image_0", "红裙女孩", "assets/picture-01.png",
        "ref_image_1", "秋日庭院", "assets/picture-02.png",
        "ref_video_0", "运镜参考", "assets/video-01.mp4",
        "ref_audio_0", "红裙女孩音色", "assets/audio-01.wav",
    ):
        assert line in text, f"注入卡缺接线信息：{line}"
    assert text.index("ref_image_0") < text.index("ref_image_1") < text.index("ref_video_0")
    # 官方标签随行（提示词与注入卡的指称口径同源）
    assert "<Picture 1>" in text and "<Picture 2>" in text


def test_card_records_the_widget_triple_and_final_size():
    """AC2：视频输出参数＝「比例档 + megapixels + 对齐倍数 → 最终 W×H」三元组。"""
    ledger = _ledger_with(_entry("红裙女孩", KIND_PICTURE, 1, "assets/picture-01.png"))
    params = VideoOutputParams()  # 默认三元组＝登记工作流现值（9:16 · 0.4 · 32）
    text = render_injection_card(ledger, params)
    assert "9:16 (Portrait Widescreen)" in text      # 比例档（widget 值逐字）
    assert "0.4" in text                             # megapixels
    assert "32" in text                              # 对齐倍数
    assert "480" in text and "864" in text           # 最终 W×H（480 × 864）


def test_card_carries_the_registered_workflow_name():
    """AC3：注入卡含登记工作流名（与产物/菜单同一指称，REF_WORKFLOW_NAME 单一来源）。"""
    from minimax_h3_prompt.generation import REF_WORKFLOW_NAME

    text = render_injection_card(_ledger_with(), VideoOutputParams())
    assert REF_WORKFLOW_NAME in text


def test_card_warns_on_number_gaps_that_shift_model_labels():
    """缺号要报：空槽会让模型 <Picture N> 编号前移错位（绝不静默）。"""
    ledger = _ledger_with(
        _entry("红裙女孩", KIND_PICTURE, 1, "assets/picture-01.png"),
        _entry("青铜钥匙", KIND_PICTURE, 3, "assets/picture-03.png"),
    )
    text = render_injection_card(ledger, VideoOutputParams())
    assert "ref_image_1" in text, "缺号槽位要点名"
    assert "错位" in text or "前移" in text, "缺号后果要写明"
    # 无缺号的台账不该喊狼来了
    full = render_injection_card(
        _ledger_with(_entry("红裙女孩", KIND_PICTURE, 1, "assets/picture-01.png")),
        VideoOutputParams())
    assert "错位" not in full and "前移" not in full


def test_write_card_persists_text_next_to_the_ledger(tmp_path):
    """落盘口（向导与结构化出口共用）：生成目录根上出 injection-card.md，正文同返回值。

    与 asset-ledger.json 同级：生成目录自包含（搬走/拷给别人仍对得上账，#43 同款）。
    """
    from minimax_h3_prompt.asset_ledger import save_asset_ledger

    save_asset_ledger(tmp_path, _ledger_with(
        _entry("红裙女孩", KIND_PICTURE, 1, "assets/picture-01.png")))
    path, text = write_injection_card(tmp_path, VideoOutputParams())
    assert path == tmp_path / CARD_FILENAME
    assert path.read_text(encoding="utf-8") == text
    assert "红裙女孩" in text and "ref_image_0" in text


# ---------------------------------------------------------------------------
# 4. 结构化产物出口（既有缝）：brief 路径同落注入卡，接线不丢参
# ---------------------------------------------------------------------------

def _stub_pipeline(monkeypatch):
    """打桩 LLM 层（历史教训：暗读真 key 的用例只在本机能过）。"""
    from pathlib import Path

    from minimax_h3_prompt import topic_generation

    monkeypatch.setattr(topic_generation, "run_pipeline_structured",
                        lambda *a, **k: (object(), object()))
    monkeypatch.setattr(topic_generation, "save_generation",
                        lambda result, directory, overwrite=False: Path(directory))


def _cfg(tmp_path):
    from types import SimpleNamespace

    return SimpleNamespace(
        default_duration=5.0, default_language="Chinese", default_style="",
        sessions_root=tmp_path / "sessions", max_clarification_rounds=0,
    )


def test_brief_path_writes_card_with_declared_triple(tmp_path, monkeypatch):
    """ref brief 带路径与视频输出三元组 → 注入卡落盘（映射 + 声明值，非默认值）。"""
    from minimax_h3_prompt.brief_parser import RefItem
    from minimax_h3_prompt.topic_generation import create_video_from_topic

    _stub_pipeline(monkeypatch)
    girl = tmp_path / "girl.png"
    girl.write_bytes(b"girl-bytes")
    refs = [RefItem(picture=1, name="红裙女孩", description="红裙", path=str(girl))]
    _result, directory = create_video_from_topic(
        "测试主题", _cfg(tmp_path), mode="ref", refs=refs,
        video_output=VideoOutputParams(ratio="1:1 (Square)", megapixels=1.0, multiple=16))
    text = (directory / CARD_FILENAME).read_text(encoding="utf-8")
    assert "ref_image_0" in text and "红裙女孩" in text
    assert "1:1 (Square)" in text and "megapixels：1" in text and "对齐倍数：16" in text
    assert "1024" in text  # 1:1 · 1.0MP · 对齐 16 → 1024 × 1024


def test_base_mode_never_writes_the_card(tmp_path, monkeypatch):
    """base（首尾帧链）没有参考槽可接线——注入卡是多参路线的产物，不越界。"""
    from minimax_h3_prompt.brief_parser import RefItem
    from minimax_h3_prompt.topic_generation import create_video_from_topic

    _stub_pipeline(monkeypatch)
    frame = tmp_path / "first.png"
    frame.write_bytes(b"frame-bytes")
    refs = [RefItem(picture=1, name="首帧", description="", path=str(frame))]
    _result, directory = create_video_from_topic(
        "测试主题", _cfg(tmp_path), mode="base", variant="I2VA", refs=refs)
    assert not (directory / CARD_FILENAME).exists()


def test_create_video_from_topic_puts_video_output_into_the_brief(tmp_path, monkeypatch):
    """topic 入口自建 Brief——video_output 不递进来就是「规则在、接线不在」（画布同款）。"""
    from minimax_h3_prompt import topic_generation

    captured: dict = {}

    def fake_run(brief, config, **kwargs):
        captured["video_output"] = brief.video_output
        raise RuntimeError("到此为止")  # 不真跑管线

    monkeypatch.setattr(topic_generation, "run_pipeline_structured", fake_run)
    params = VideoOutputParams(ratio="21:9 (Ultrawide)", megapixels=0.4)
    with pytest.raises(RuntimeError):
        topic_generation.create_video_from_topic(
            "主题", _cfg(tmp_path), mode="ref", video_output=params)
    assert captured["video_output"] is params


def test_cli_brief_path_forwards_video_output_to_generation(tmp_path, monkeypatch):
    """CLI --brief 路径把解析出的三元组**递到**生成入口（画布丢参的复审现场不得复刻）。"""
    import argparse

    from minimax_h3_prompt.main import _run_generate_prompts

    brief_file = tmp_path / "brief.md"
    brief_file.write_text("输入: 多参\n视频比例: 3:2\n## 剧情\n主题", encoding="utf-8")
    forwarded: dict = {}

    def fake_create(topic, config, **kwargs):
        forwarded.update(kwargs)
        raise RuntimeError("到此为止")

    monkeypatch.setattr("minimax_h3_prompt.topic_generation.create_video_from_topic", fake_create)
    args = argparse.Namespace(brief=str(brief_file), variant=None, dry_run=False,
                              topic=None, duration=None, style=None, language=None)
    with pytest.raises(RuntimeError):
        _run_generate_prompts(args)
    assert forwarded.get("video_output") is not None
    assert forwarded["video_output"].ratio == "3:2 (Photo)"


def test_brief_snapshot_records_the_video_output_triple():
    """generation.json 的 brief 快照记三元组（逐字段显式清单；漏登记＝事后对不上账）。"""
    from minimax_h3_prompt.brief_parser import Brief
    from minimax_h3_prompt.generation import brief_snapshot

    brief = Brief(mode="ref", plot="主题",
                  video_output=VideoOutputParams(megapixels=1.0, multiple=16))
    snapshot = brief_snapshot(brief)
    assert snapshot["video_output"] == {"ratio": "9:16 (Portrait Widescreen)",
                                        "megapixels": 1.0, "multiple": 16}


def test_card_never_maps_assets_to_nonexistent_ports():
    """溢出台账不渲假端口：标「无对应槽位」＋点名告警，缺号警告只扫真实端口范围。"""
    ten_pictures = _ledger_with(*[
        _entry(f"人物{i}", KIND_PICTURE, i, f"assets/picture-{i:02d}.png")
        for i in range(1, 11)  # 10 张图：第 10 张无端口（图槽只有 0..8）
    ])
    text = render_injection_card(ten_pictures, VideoOutputParams())
    assert "ref_image_9" not in text, "越界编号不得渲出不存在的端口名"
    assert "无对应槽位" in text and "槽位越界" in text
    assert "人物10" in text            # 溢出条目仍列名（不静默丢）
    assert "缺号" not in text          # 1..9 齐全，不得误报缺号


def test_card_warns_when_two_entries_claim_the_same_port():
    """同号重复＝多行抢一个端口，只有一行接得上——告警点名两行（每行都要能接）。"""
    ledger = _ledger_with(
        _entry("红裙女孩", KIND_PICTURE, 1, "assets/picture-01.png"),
        _entry("神秘来客", KIND_PICTURE, 1, "assets/picture-02.png"),
    )
    text = render_injection_card(ledger, VideoOutputParams())
    assert "同号重复" in text and "红裙女孩" in text and "神秘来客" in text


def test_card_notes_video_soundtrack_pairing_and_audio_label_hazard():
    """素材视频的音轨接同号 ref_video_audio_N；自带音轨会占 <Audio> 号，独立音频
    的标签可能与台账编号错位——接线以槽位为准，这条要说破。"""
    ledger = _ledger_with(
        _entry("运镜参考", KIND_VIDEO, 1, "assets/video-01.mp4"),
        _entry("红裙女孩音色", KIND_AUDIO, 1, "assets/audio-01.wav"),
    )
    text = render_injection_card(ledger, VideoOutputParams())
    assert "ref_video_audio_0" in text
    assert "<Audio>" in text and "以本卡槽位为准" in text
    # 只有图的项目不提视频音轨（不相关的附注＝刷屏）
    pics_only = render_injection_card(
        _ledger_with(_entry("红裙女孩", KIND_PICTURE, 1, "assets/picture-01.png")),
        VideoOutputParams())
    assert "ref_video_audio" not in pics_only

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
    derive_video_output,
    render_injection_card,
    slot_name,
    write_injection_card,
)
from minimax_h3_prompt.workflow_config import workflow_settings

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


# ---------------------------------------------------------------------------
# 1b. 画布 → 视频输出参数推导（issue #49：比例继承画布、面积反推取档）
# ---------------------------------------------------------------------------

def test_derive_video_output_reproduces_the_manual_pick():
    """当日手选值可复现（issue #49 AC2 金值，独立来源＝票面记录的实测现场）：
    画布 1280×720 → 16:9 · 0.9MP · 32 → 1280×736。"""
    derivation = derive_video_output(1280, 720)
    params = derivation.params
    assert (params.ratio, params.megapixels, params.multiple) == (
        "16:9 (Widescreen)", 0.9, 32)
    assert params.size == (1280, 736)  # 与 2026-10-18 手选落卡的最终 W×H 逐字一致
    assert derivation.notes == ()  # 逐项直取：无回退/夹档可说破


def test_derive_inherits_ratio_from_custom_canvas_by_reduction():
    """自定义画布按约分继承比例档（issue #49 AC5）：尺寸不在菜单上，比例对得上就继承。
    1600×900 约分 16:9 → 同档；面积 1.373MP 取 0.1 档 → 1.4。"""
    derivation = derive_video_output(1600, 900)
    params = derivation.params
    assert params.ratio == "16:9 (Widescreen)"
    assert params.megapixels == 1.4
    assert params.multiple == 32  # 对齐倍数取默认（票面）
    assert derivation.notes == ()


def test_derive_extreme_ratio_falls_back_to_the_nearest_preset():
    """极端/档外比例回退最近档（issue #49 AC5）：540×1260（9:21，向导自定义入口的
    票面样例）最近的是 9:16（0.429 对 0.5625 距 0.134，对 2:3 距 0.238）；
    极宽 2560×720（32:9）回退 21:9——回退不是无脑塞 9:16，仍要贴画布形状。
    回退必须说破（notes 非空），绝不静默。"""
    portrait = derive_video_output(540, 1260)
    assert portrait.params.ratio == "9:16 (Portrait Widescreen)"
    assert any("回退" in note for note in portrait.notes)
    wide = derive_video_output(2560, 720)
    assert wide.params.ratio == "21:9 (Ultrawide)"
    assert any("回退" in note for note in wide.notes)


def test_derive_exact_match_reduces_both_sides_so_21x9_inherits_silently():
    """21:9 档存的是未约分份数 (21, 9)（节点 widget 值的原样拼写）——精确对档必须
    两边同约分（issue #49 复审硬伤）：正 21:9 画布 2520×1080 约分成 7:3，若只约
    画布不约档，档内画布会错走回退分支、打印假说破（「无对应比例档」）。
    """
    derivation = derive_video_output(2520, 1080)
    assert derivation.params.ratio == "21:9 (Ultrawide)"
    assert derivation.notes == (), "档内画布不得报「回退」——那是假说破"


def test_derive_megapixels_tier_rounds_half_up_and_clamps_to_node_bounds():
    """面积反推取档：0.1 档四舍五入、半值向上（预算不缩），越界夹到节点契约并说破。
    512×512＝0.25MP 半值 → 0.3；64×64＝0.004MP → 夹下限 0.1；8192×8192＝64MP → 夹上限 16。"""
    assert derive_video_output(512, 512).params.megapixels == 0.3
    low = derive_video_output(64, 64)
    assert low.params.megapixels == 0.1
    assert any("夹" in note or "节点" in note for note in low.notes)
    high = derive_video_output(8192, 8192)
    assert high.params.megapixels == 16.0
    assert any("夹" in note or "节点" in note for note in high.notes)


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

# 槽位映射的样例覆盖：首尾两种边界都取，图槽上限从工作流配置派生（issue #65：
# 不再把 9 当边界——配置一改，这里跟着走）。
_PICTURE_CAP = workflow_settings().slot_count(KIND_PICTURE)
SLOT_CASES = (
    ("picture", 1, "ref_image_0"),   # <Picture 1> 进第一个图槽：编号即叙事顺序
    ("picture", _PICTURE_CAP, f"ref_image_{_PICTURE_CAP - 1}"),
    ("video", 1, "ref_video_0"),
    ("audio", 1, "ref_audio_0"),
    ("audio", 3, "ref_audio_2"),
)


@pytest.mark.parametrize("kind,number,expected", SLOT_CASES)
def test_slot_name_maps_ledger_number_to_workflow_slot(kind, number, expected):
    """台账编号 N → 工作流槽位（0 起）：`ref_image_N` 等与节点端口名逐字一致。"""
    assert slot_name(kind, number) == expected


def test_slot_name_rejects_numbers_beyond_the_physical_ports():
    """槽位是真实端口名：越界编号不造名（端口数从工作流配置派生，不钉死 9）。

    超过端口数若仍渲出 `ref_image_N`，照卡接线会接到不存在的端口——接线清单的
    最低义务是每行都能接得上（复审 (a)1）。
    """

    settings = workflow_settings()
    picture_cap = settings.slot_count(KIND_PICTURE)
    with pytest.raises(ValueError) as excinfo:
        slot_name(KIND_PICTURE, picture_cap + 1)
    assert str(picture_cap) in str(excinfo.value), "报错要给出端口数（可赢信息）"
    with pytest.raises(ValueError):
        slot_name(KIND_VIDEO, settings.slot_count(KIND_VIDEO) + 1)
    with pytest.raises(ValueError):
        slot_name(KIND_AUDIO, settings.slot_count(KIND_AUDIO) + 1)


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
    """AC3：注入卡含登记工作流名（与产物/菜单同一指称，workflow.yaml 单一来源）。"""

    text = render_injection_card(_ledger_with(), VideoOutputParams())
    assert workflow_settings().ref_workflow_name in text


def test_card_shows_the_official_capacity_budget_and_current_usage():
    """卡面把容量账摆出来（AC3 的可见面）：用户照卡接线前就知道还剩几个位置。

    只报「超限」不报「剩多少」，用户要自己数——而这张卡的全部意义就是他不用数。
    """
    ledger = _ledger_with(
        _entry("红裙女孩", KIND_PICTURE, 1, "assets/picture-01.png"),
        _entry("秋日庭院", KIND_PICTURE, 2, "assets/picture-02.png"),
        _entry("运镜参考", KIND_VIDEO, 1, "assets/video-01.mp4"),
        _entry("红裙女孩音色", KIND_AUDIO, 1, "assets/audio-01.wav"),
    )
    text = render_injection_card(ledger, VideoOutputParams())
    # 现行用量与官方预算同屏（图 2 / 视频 1 / 音频 1，上限 9/3/3/12）
    assert "图 2" in text and "视频 1" in text and "音频 1" in text
    assert "9" in text and "12" in text


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
    result = write_injection_card(tmp_path, VideoOutputParams())
    assert result.path == tmp_path / CARD_FILENAME
    assert result.written is True
    assert result.path.read_text(encoding="utf-8") == result.text
    assert "红裙女孩" in result.text and "ref_image_0" in result.text


def test_write_card_never_overwrites_hand_edits(tmp_path):
    """卡是手改出口（issue #49 AC4）：用户手改不被系统回写覆盖。

    第二次写卡（重跑交付/续接）时卡已存在——可能是用户改过的正本：覆盖＝把手改
    抹掉。已存在一律保留原文并报告 ``written=False``；要重出新卡，用户删文件后重跑
    （删除是显式动作，与「不许静默覆盖」同一口径）。
    """
    first = write_injection_card(tmp_path, VideoOutputParams())
    assert first.written is True
    edited = first.text.replace("megapixels：0.4", "megapixels：2.5")
    first.path.write_text(edited, encoding="utf-8")
    second = write_injection_card(
        tmp_path, VideoOutputParams(ratio="21:9 (Ultrawide)", megapixels=8.0))
    assert second.written is False
    assert second.text == edited, "手改后的卡不得被系统回写覆盖"
    assert "21:9" not in second.text  # 新渲染稿不得混进盘上正文


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


def test_brief_path_derives_from_canvas_when_video_output_absent(tmp_path, monkeypatch):
    """brief 未声明视频参数 → 按画布自动推导（issue #49）：画布 1280×720 → 16:9 · 0.9MP · 32。

    推导值回写进 Brief——卡与 brief 快照必须同源（复审「两处对不上账」的教训）。
    """
    from minimax_h3_prompt.topic_generation import create_video_from_topic

    _stub_pipeline(monkeypatch)
    _result, directory = create_video_from_topic(
        "测试主题", _cfg(tmp_path), mode="ref", canvases={"zimage": (1280, 720)})
    text = (directory / CARD_FILENAME).read_text(encoding="utf-8")
    assert "16:9 (Widescreen)" in text
    assert "megapixels：0.9" in text and "对齐倍数：32" in text
    assert "1280 × 736" in text


def test_brief_declaration_wins_over_canvas_derivation(tmp_path, monkeypatch):
    """brief 显式声明优先于推导（issue #49 AC3）：声明 1:1·1.0MP·16，画布 1280×720
    推得出 16:9·0.9MP 也得让路——显式声明是用户的决定，推导只是缺省。"""
    from minimax_h3_prompt.topic_generation import create_video_from_topic

    _stub_pipeline(monkeypatch)
    _result, directory = create_video_from_topic(
        "测试主题", _cfg(tmp_path), mode="ref", canvases={"zimage": (1280, 720)},
        video_output=VideoOutputParams(ratio="1:1 (Square)", megapixels=1.0, multiple=16))
    text = (directory / CARD_FILENAME).read_text(encoding="utf-8")
    assert "1:1 (Square)" in text and "megapixels：1" in text and "对齐倍数：16" in text
    assert "16:9" not in text  # 推导值不得混进声明值的卡


def test_card_renders_derivation_notes_next_to_the_triple():
    """推导注记（比例回退/面积夹档）落卡说破（issue #49，绝不静默）；
    无注记＝注记行不出现（老卡排版逐字保留）。"""
    text = render_injection_card(_ledger_with(), VideoOutputParams(),
                                 notes=("画布比例 9:21 无对应比例档，回退最近档 9:16",))
    assert "推导注记" in text and "回退最近档" in text
    plain = render_injection_card(_ledger_with(), VideoOutputParams())
    assert "推导注记" not in plain


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


def test_cli_fast_path_judges_pictures_only_when_the_film_is_one_request(capsys):
    """--brief 快路径的容量判据按路线挑（issue #86 AC4 / spec #84 D5）。

    ≤15 秒：整片即一次请求 → 10 张图停下并点名。
    >15 秒：要拆段，图数按段判 → 快路径**不许**在这里拒（24 张素材、每段 ≤9 的片子
    正是这条规格要救的）。
    """
    from minimax_h3_prompt.brief_parser import Brief, RefItem
    from minimax_h3_prompt.main import _check_brief_ref_capacity

    def _brief(duration: float, count: int) -> Brief:
        return Brief(mode="ref", plot="主题", duration=duration,
                     refs=[RefItem(picture=n, name=f"素材{n}", description="")
                           for n in range(1, count + 1)])

    assert _check_brief_ref_capacity(_brief(8.0, 9)) is True
    assert _check_brief_ref_capacity(_brief(8.0, 10)) is False
    output = capsys.readouterr().out
    assert "超出官方容量上限" in output and "<Picture 10>" in output
    # >15 秒：同一份 10 张的 brief 放行（按段判）
    assert _check_brief_ref_capacity(_brief(30.0, 10)) is True


def test_cli_brief_path_stops_readably_when_over_capacity(tmp_path, monkeypatch, capsys):
    """CLI --brief 路径超限要**停下并给可读报错**（issue #86 AC3）。

    ``write_injection_card`` 抛的是设计边界（素材太多），不是崩溃。产线上这条路径是
    ``main._run_generate_prompts`` → ``create_video_from_topic``：不在那里收口，用户
    拿到的是 traceback，而不是「哪几张超了 + 接下来怎么做」。
    """
    import argparse

    from minimax_h3_prompt.main import _run_generate_prompts
    from minimax_h3_prompt.ref_capacity import ReferenceCapacityError

    brief_file = tmp_path / "brief.md"
    brief_file.write_text("输入: 多参\n## 剧情\n主题", encoding="utf-8")

    def fake_create(topic, config, **kwargs):
        raise ReferenceCapacityError("图 10 个 > 9 个：<Picture 10>「素材10」")

    monkeypatch.setattr("minimax_h3_prompt.topic_generation.create_video_from_topic",
                        fake_create)
    args = argparse.Namespace(brief=str(brief_file), variant=None, dry_run=False,
                              topic=None, duration=None, style=None, language=None)
    assert _run_generate_prompts(args) == 1
    output = capsys.readouterr().out
    assert "超出官方容量上限" in output
    assert "<Picture 10>" in output, "点名超出的素材必须传到用户眼前"


def test_cli_run_command_stops_before_the_pipeline_on_a_single_request_film(tmp_path,
                                                                          monkeypatch,
                                                                          capsys):
    """`run` 快路径 ≤15 秒超 9 图：在烧管线之前停下（AC4 的**接线**判据）。

    变异判据：删掉 ``_run_cli`` 里的 ``if not _check_brief_ref_capacity(brief)``，
    本条红——单独测判定口盖不住「有没有人调用它」（本仓「规则在、接线不在」的老形态）。
    """
    import argparse

    from minimax_h3_prompt import main as main_module

    lines = "\n".join(f"- Picture {n}: 素材{n} — 描述{n}" for n in range(1, 11))
    brief_file = tmp_path / "brief.md"
    brief_file.write_text(
        "输入: 多参\n时长: 6\n\n## 剧情\n主题\n\n## 角色与参考图\n" + lines,
        encoding="utf-8")

    monkeypatch.setattr("minimax_h3_prompt.graph.pipeline.run_pipeline",
                        lambda *a, **k: pytest.fail("超限不许跑管线"))
    args = argparse.Namespace(brief=str(brief_file), variant=None, dry_run=False,
                              model=None, mode=None, polish=False, output=None)
    assert main_module._run_cli(args) == 1
    assert "超出官方容量上限" in capsys.readouterr().out


def test_cli_segment_delivery_stops_readably_on_a_capacity_error(tmp_path, monkeypatch,
                                                                 capsys):
    """`run` 快路径 >15 秒：逐段容量闸抛错要当面说清，不是 traceback。

    变异判据：删掉 ``_deliver_ref_segments_cli`` 的 ``except ReferenceCapacityError``，
    本条红。
    """
    from minimax_h3_prompt import main as main_module
    from minimax_h3_prompt.brief_parser import Brief
    from minimax_h3_prompt.ref_capacity import ReferenceCapacityError

    def boom(**kwargs):
        raise ReferenceCapacityError("第 1 段接线卡：总文件 13 个 > 12 个")

    monkeypatch.setattr("minimax_h3_prompt.ref_segments.deliver_ref_segments", boom)
    main_module._deliver_ref_segments_cli(
        Brief(mode="ref", plot="主题", duration=30.0), "整片提示词", tmp_path)
    assert "无法按段交付" in capsys.readouterr().out


def test_brief_path_refuses_more_than_nine_pictures_for_a_single_request_film(
        tmp_path, monkeypatch):
    """`generate-prompts --brief`（≤15 秒）超 9 图：写卡之前就停（AC3/AC4）。

    变异判据：删掉 ``topic_generation`` 里的 ``check_film_capacity`` 调用，本条红。
    """
    from minimax_h3_prompt.brief_parser import RefItem
    from minimax_h3_prompt.ref_capacity import ReferenceCapacityError
    from minimax_h3_prompt.topic_generation import create_video_from_topic

    _stub_pipeline(monkeypatch)
    refs = []
    for number in range(1, 11):
        path = tmp_path / f"picture-{number}.png"
        path.write_bytes(b"img")
        refs.append(RefItem(picture=number, name=f"素材{number}", description="",
                            path=str(path)))
    with pytest.raises(ReferenceCapacityError) as excinfo:
        create_video_from_topic("主题", _cfg(tmp_path), mode="ref", refs=refs)
    assert "<Picture 10>" in str(excinfo.value)


def test_brief_snapshot_records_the_video_output_triple():
    """generation.json 的 brief 快照记三元组（逐字段显式清单；漏登记＝事后对不上账）。"""
    from minimax_h3_prompt.brief_parser import Brief
    from minimax_h3_prompt.generation import brief_snapshot

    brief = Brief(mode="ref", plot="主题",
                  video_output=VideoOutputParams(megapixels=1.0, multiple=16))
    snapshot = brief_snapshot(brief)
    assert snapshot["video_output"] == {"ratio": "9:16 (Portrait Widescreen)",
                                        "megapixels": 1.0, "multiple": 16}


def test_card_maps_all_nine_picture_slots_from_config():
    """AC2（issue #86）：9 张素材逐条给出 `ref_image_0..ref_image_8`，一个都不越界。

    登记工作流回到 V2 的 9 图槽版本（2026-10-25）：槽位数读 ``config/workflow.yaml``，
    9 是**官方单次请求上限**而不是节点 UI 上限（spec #84 D4/D5）。
    """

    cap = workflow_settings().slot_count(KIND_PICTURE)
    assert cap == 9, "登记工作流应是 9 图槽的 V2"
    ledger = _ledger_with(*[
        _entry(f"素材{i}", KIND_PICTURE, i, f"assets/picture-{i:02d}.png")
        for i in range(1, cap + 1)
    ])
    text = render_injection_card(ledger, VideoOutputParams())
    for i in range(1, cap + 1):
        assert f"ref_image_{i - 1}" in text, f"第 {i} 张没有槽位"
    assert "无对应槽位" not in text and "槽位越界" not in text, "9 张不该有越界告警"
    assert "缺号" not in text, "1..9 齐全，不得误报缺号"
    # 卡面不再出现退役的 24 槽/V3（AC1）
    assert "V3" not in text and "24 槽" not in text


def test_card_never_maps_assets_to_nonexistent_ports():
    """溢出台账不渲假端口：标「无对应槽位」＋点名告警，缺号警告只扫真实端口范围。

    边界从工作流配置派生（issue #65：不钉死数字）——这里把配置压到 3 槽来触发溢出分支。
    默认 9 槽时「10 张」不会走到这里：官方容量校验更早一步拒绝（见
    ``test_card_refuses_more_than_the_official_picture_cap``）。
    """
    from minimax_h3_prompt import workflow_config

    settings = workflow_config.WorkflowSettings(
        ref_workflow_name="三槽测试工作流",
        slot_counts={KIND_PICTURE: 3, KIND_VIDEO: 3, KIND_AUDIO: 3})
    overflowed = _ledger_with(*[
        _entry(f"人物{i}", KIND_PICTURE, i, f"assets/picture-{i:02d}.png")
        for i in range(1, 5)  # 4 张图：最后一张无端口
    ])
    text = render_injection_card(overflowed, VideoOutputParams(), settings=settings)
    assert "ref_image_3" not in text, "越界编号不得渲出不存在的端口名"
    assert "无对应槽位" in text and "槽位越界" in text
    assert "人物4" in text              # 溢出条目仍列名（不静默丢）
    assert "缺号" not in text            # 1..3 齐全，不得误报缺号


def test_card_renders_even_when_the_film_has_more_pictures_than_one_request_allows():
    """整片卡是**库存清单**，不是一次请求（issue #86 / spec #84 D5）。

    24 张素材的片子每段只喂 ≤9 张本来就合法（ADR 0017 的真实运行就是这个规模），
    拿整片图数当请求量去拒，会把这种片子整片拦死——正好废掉「按段取用」。所以卡面
    照出全部条目（越界编号仍标「无对应槽位」），容量闸住在一次请求那两处。
    """
    ledger = _ledger_with(*[
        _entry(f"素材{i}", KIND_PICTURE, i, f"assets/picture-{i:02d}.png")
        for i in range(1, 13)
    ])
    text = render_injection_card(ledger, VideoOutputParams())
    assert "<Picture 12>" in text and "素材12" in text, "超限素材也要列出来（不静默丢）"
    assert "无对应槽位" in text, "9 槽工作流下第 10 张起确实无端口，要说破"


def test_card_boundary_follows_the_workflow_config(tmp_path, monkeypatch):
    """AC4（issue #65）：改配置即改边界——不重载模块、不重建卡函数。

    把生效配置换成图 3 槽：3 张全有槽、第 4 张越界。若实现里还留着写死的 24/9，
    这条立刻红（守卫的变异检验）。
    """
    from minimax_h3_prompt import workflow_config

    path = tmp_path / "workflow.yaml"
    path.write_text("slots:\n  picture: 3\n", encoding="utf-8")
    monkeypatch.setattr(workflow_config, "WORKFLOW_CONFIG_PATH", path)
    ledger = _ledger_with(*[
        _entry(f"素材{i}", KIND_PICTURE, i, f"assets/picture-{i:02d}.png")
        for i in range(1, 5)
    ])
    text = render_injection_card(ledger, VideoOutputParams())
    assert "ref_image_0" in text and "ref_image_2" in text
    assert "ref_image_3" not in text
    assert "无对应槽位" in text and "素材4" in text


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

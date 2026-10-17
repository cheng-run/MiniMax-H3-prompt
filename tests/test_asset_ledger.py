"""素材交回与素材台账（issue #43）：三型入库、五字段落盘、换版可发现、编号按剧本出场序。

接缝（spec #39 Testing Decisions 预议）：

1. **素材台账层（本票唯一新缝）**：交回入库的登记结果（语义名/类型/官方编号/路径/
   SHA256）落盘为持久化产物；同名换版（SHA256 变化）可发现并报出、不静默（LoRA 可变
   文件名教训的正面解）；编号与剧本出场序一致。
2. **向导接线**（薄）在 ``tests/test_wizard_asset_intake.py``；**结构化出口**（既有缝）
   的 brief 路径建账在本文件末节。全部断言落在外部行为：台账文件内容、入库报告、
   落盘副本。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from minimax_h3_prompt.asset_ledger import (
    KIND_AUDIO,
    KIND_PICTURE,
    KIND_VIDEO,
    AssetSubmission,
    intake_assets,
    load_asset_ledger,
    render_ledger,
    render_report,
    verify_asset_ledger,
)
from minimax_h3_prompt.brief_parser import RefItem


def _registry(*pairs: tuple[int, str]) -> list[RefItem]:
    return [RefItem(picture=n, name=name, description=f"{name}的生图提示词") for n, name in pairs]


def _cfg(tmp_path):
    from types import SimpleNamespace
    return SimpleNamespace(
        sessions_root=tmp_path / "sessions",
        default_duration=5.0, default_style="Cinematic", default_language="Chinese",
    )


def _write_source(tmp_path: Path, name: str, content: bytes) -> Path:
    source = tmp_path / "returned" / name
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(content)
    return source


# ---------------------------------------------------------------------------
# 交回入库：五字段登记 + 落盘持久化产物（AC2）
# ---------------------------------------------------------------------------

def test_intake_registers_picture_with_five_fields(tmp_path):
    """交回一张素材图 → 台账落盘：语义名/类型/<Picture N>/路径/SHA256（AC2）。

    五字段的期望值**逐项手写**，不从实现反推：语义名与编号来自参考素材登记（剧本出场序），
    SHA256 对交回文件内容现算，路径指向会话目录内正本（复制入库，旧产物才可复测）。
    """
    directory = tmp_path / "GEN001"
    source = _write_source(tmp_path, "girl.png", b"fake-image-bytes-v1")
    registry = _registry((1, "红裙女孩"))

    report = intake_assets(directory, [
        AssetSubmission(name="红裙女孩", kind=KIND_PICTURE, source=str(source)),
    ], registry=registry)

    ledger = load_asset_ledger(directory)
    assert len(ledger.entries) == 1
    entry = ledger.entries[0]
    assert entry.name == "红裙女孩"
    assert entry.kind == KIND_PICTURE
    assert entry.number == 1  # <Picture 1>：编号取自参考素材登记（剧本出场序）
    expected_sha = hashlib.sha256(b"fake-image-bytes-v1").hexdigest()
    assert entry.sha256 == expected_sha
    # 正本在会话目录内（只复制不移动，同 frames/ 入库先例）；记录的是目录内正本路径
    assert (directory / entry.path).read_bytes() == b"fake-image-bytes-v1"
    assert entry.path.startswith("assets/")
    assert not Path(entry.path).is_absolute()
    assert report.registered == (entry,)
    assert report.missing == ()


def test_ledger_persists_as_json_artifact_and_roundtrips(tmp_path):
    """台账是持久化产物：写盘后重新加载，五字段与换版记录逐项还原（AC2）。"""
    directory = tmp_path / "GEN001"
    source = _write_source(tmp_path, "key.webp", b"key-bytes")
    intake_assets(directory, [
        AssetSubmission(name="青铜钥匙", kind=KIND_PICTURE, source=str(source)),
    ], registry=_registry((1, "青铜钥匙")))

    raw = json.loads((directory / "asset-ledger.json").read_text(encoding="utf-8"))
    assert raw["schema_version"] == "asset-ledger.v1"
    assert len(raw["entries"]) == 1
    assert set(raw["entries"][0]) == {"name", "kind", "number", "path", "sha256", "source"}

    ledger = load_asset_ledger(directory)
    assert ledger.entries[0].name == "青铜钥匙"
    assert ledger.entries[0].sha256 == hashlib.sha256(b"key-bytes").hexdigest()


def test_three_kinds_can_be_registered(tmp_path):
    """三型参考素材都可交回入库：图为主，视频/音频按需（AC1）。"""
    directory = tmp_path / "GEN001"
    sources = {
        KIND_PICTURE: _write_source(tmp_path, "girl.png", b"img"),
        KIND_VIDEO: _write_source(tmp_path, "camera.mp4", b"vid"),
        KIND_AUDIO: _write_source(tmp_path, "voice.wav", b"aud"),
    }
    intake_assets(directory, [
        AssetSubmission(name="红裙女孩", kind=KIND_PICTURE, source=str(sources[KIND_PICTURE])),
        AssetSubmission(name="运镜参考", kind=KIND_VIDEO, source=str(sources[KIND_VIDEO])),
        AssetSubmission(name="红裙女孩音色", kind=KIND_AUDIO, source=str(sources[KIND_AUDIO])),
    ], registry=_registry((1, "红裙女孩")))

    ledger = load_asset_ledger(directory)
    assert {(e.name, e.kind) for e in ledger.entries} == {
        ("红裙女孩", KIND_PICTURE),
        ("运镜参考", KIND_VIDEO),
        ("红裙女孩音色", KIND_AUDIO),
    }


# ---------------------------------------------------------------------------
# 同名换版：SHA256 变化可发现并报出，不静默（AC3，LoRA 可变文件名教训）
# ---------------------------------------------------------------------------

def test_reintake_changed_content_is_reported_not_silent(tmp_path):
    """同名同型换版（SHA256 变化）：默认拒收并报出新旧哈希——不静默（AC3）。

    LoRA 教训（issue #18）是「同名文件换版后旧对照静默作废」；这里必须反过来：
    换版被发现、被报出（新旧 SHA256 都在报告里），且未经同意不许替换登记。
    """
    directory = tmp_path / "GEN001"
    registry = _registry((1, "红裙女孩"))
    v1 = _write_source(tmp_path, "girl-v1.png", b"content-v1")
    intake_assets(directory, [AssetSubmission("红裙女孩", KIND_PICTURE, str(v1))],
                  registry=registry)
    old_sha = load_asset_ledger(directory).entries[0].sha256

    v2 = _write_source(tmp_path, "girl-v2.png", b"content-v2-different")
    report = intake_assets(
        directory,
        [AssetSubmission("红裙女孩", KIND_PICTURE, str(v2))],
        registry=registry,
        on_replace=lambda existing, incoming: False,  # 默认口径：人没点头就不换
    )

    # 报出：换版被发现且新旧哈希都在报告里（不许只有名字、查无对照）
    assert len(report.replaced) == 0
    assert [e.name for e in report.refused] == ["红裙女孩"]
    assert report.refused[0].old_sha256 == old_sha
    assert report.refused[0].new_sha256 == hashlib.sha256(b"content-v2-different").hexdigest()
    # 原登记保留，正本内容不动
    ledger = load_asset_ledger(directory)
    assert ledger.entries[0].sha256 == old_sha
    assert (directory / ledger.entries[0].path).read_bytes() == b"content-v1"


def test_reintake_replacement_keeps_old_sha_in_history(tmp_path):
    """点头替换：换版记录追加进台账，旧 SHA256 不消失（旧产物可复测，AC3）。"""
    directory = tmp_path / "GEN001"
    registry = _registry((1, "红裙女孩"))
    v1 = _write_source(tmp_path, "girl-v1.png", b"content-v1")
    intake_assets(directory, [AssetSubmission("红裙女孩", KIND_PICTURE, str(v1))],
                  registry=registry)
    old_sha = load_asset_ledger(directory).entries[0].sha256

    seen: list[tuple[str, str]] = []
    v2 = _write_source(tmp_path, "girl-v2.png", b"content-v2-different")
    report = intake_assets(
        directory,
        [AssetSubmission("红裙女孩", KIND_PICTURE, str(v2))],
        registry=registry,
        on_replace=lambda existing, incoming: seen.append((existing.sha256, incoming.sha256)) or True,
    )

    # on_replace 回调拿到新旧两侧（报出的材料），替换后台账留史
    assert seen == [(old_sha, hashlib.sha256(b"content-v2-different").hexdigest())]
    assert len(report.replaced) == 1
    assert report.replaced[0].old_sha256 == old_sha
    assert report.replaced[0].new_sha256 == hashlib.sha256(b"content-v2-different").hexdigest()
    ledger = load_asset_ledger(directory)
    assert ledger.entries[0].sha256 == hashlib.sha256(b"content-v2-different").hexdigest()
    assert ledger.replacements == report.replaced
    assert (directory / ledger.entries[0].path).read_bytes() == b"content-v2-different"


def test_reintake_same_content_is_idempotent(tmp_path):
    """同名同内容重交：不算换版、不加第二条（登记幂等，免得报告噪声淹没真换版）。"""
    directory = tmp_path / "GEN001"
    registry = _registry((1, "红裙女孩"))
    v1 = _write_source(tmp_path, "girl-v1.png", b"content-v1")
    intake_assets(directory, [AssetSubmission("红裙女孩", KIND_PICTURE, str(v1))],
                  registry=registry)
    again = _write_source(tmp_path, "girl-again.png", b"content-v1")
    report = intake_assets(directory, [AssetSubmission("红裙女孩", KIND_PICTURE, str(again))],
                           registry=registry)
    assert len(report.ledger.entries) == 1
    assert report.registered == () and report.replaced == () and report.refused == ()
    assert "红裙女孩" in report.unchanged[0]
    assert report.ledger.replacements == ()


def test_empty_intake_does_not_materialize_ledger(tmp_path):
    """什么都没交回 ＝ 不落空台账文件：产物要么有货要么不存在，不造「空壳权威清单」。"""
    directory = tmp_path / "GEN001"
    report = intake_assets(directory, [], registry=_registry((1, "红裙女孩")))
    assert report.registered == () and report.missing == ("红裙女孩",)
    assert not (directory / "asset-ledger.json").exists()
    assert load_asset_ledger(directory).entries == ()


# ---------------------------------------------------------------------------
# 编号：图按剧本出场序（AC4），视频/音频型内递增且不挤占已分配的号
# ---------------------------------------------------------------------------

def test_ledger_numbering_matches_registry_script_order(tmp_path):
    """台账语义名与 <Picture N> 与剧本出场序一致（AC4）：编号取自参考素材登记，交回
    次序打乱也不改号——编号即叙事顺序（2026-10-16 裁定）。"""
    directory = tmp_path / "GEN001"
    # 参考素材登记＝ref_asset_registry 的产物形状：按剧本出场序编号（红裙女孩先登场）
    registry = _registry((1, "红裙女孩"), (2, "秋日庭院"), (3, "青铜钥匙"), (4, "蓝衫少年"))
    # 交回次序故意与出场序相反
    for name, data in [("蓝衫少年", b"a"), ("青铜钥匙", b"b"), ("秋日庭院", b"c"), ("红裙女孩", b"d")]:
        source = _write_source(tmp_path, f"{name}.png", data)
        intake_assets(directory, [AssetSubmission(name, KIND_PICTURE, str(source))],
                      registry=registry)

    ledger = load_asset_ledger(directory)
    assert [(e.name, e.number) for e in ledger.entries] == [
        ("红裙女孩", 1), ("秋日庭院", 2), ("青铜钥匙", 3), ("蓝衫少年", 4),
    ]
    assert [e.tag for e in ledger.entries] == [
        "<Picture 1>", "<Picture 2>", "<Picture 3>", "<Picture 4>",
    ]


def test_unknown_picture_name_goes_tail_and_is_flagged(tmp_path):
    """不在参考素材登记里的语义名不许静默丢、也不许硬塞叙事位次：补型内末尾并报出。"""
    directory = tmp_path / "GEN001"
    registry = _registry((1, "红裙女孩"))
    source = _write_source(tmp_path, "extra.png", b"extra")
    report = intake_assets(directory, [AssetSubmission("神秘来客", KIND_PICTURE, str(source))],
                           registry=registry)
    assert report.missing == ("红裙女孩",)  # 参考素材登记里没交回的照样点名（不静默）
    assert report.unlisted == ("神秘来客",)  # 交回了却不在参考素材登记里的也点名
    entry = report.ledger.get("神秘来客", KIND_PICTURE)
    assert entry is not None and entry.number == 2  # 排在参考素材登记之后


def test_video_audio_numbers_increment_within_kind(tmp_path):
    """视频/音频按型内递增编号（官方标签各自独立序列），后续批次不挤占已分配的号。"""
    directory = tmp_path / "GEN001"
    v1 = _write_source(tmp_path, "cam.mp4", b"v1")
    a1 = _write_source(tmp_path, "voice.wav", b"a1")
    intake_assets(directory, [
        AssetSubmission("运镜参考", KIND_VIDEO, str(v1)),
        AssetSubmission("红裙女孩音色", KIND_AUDIO, str(a1)),
    ], registry=())
    v2 = _write_source(tmp_path, "cam2.mp4", b"v2")
    report = intake_assets(directory, [AssetSubmission("打斗运镜", KIND_VIDEO, str(v2))],
                           registry=())
    ledger = report.ledger
    assert ledger.get("运镜参考", KIND_VIDEO).tag == "<Video 1>"
    assert ledger.get("红裙女孩音色", KIND_AUDIO).tag == "<Audio 1>"
    assert ledger.get("打斗运镜", KIND_VIDEO).tag == "<Video 2>"  # 不从 1 重开
    # 已分配的号不因新批次改写（label continuity）
    assert ledger.get("运镜参考", KIND_VIDEO).number == 1


# ---------------------------------------------------------------------------
# 对账与严格读回：盘上换版可发现（LoRA 教训），坏了要响
# ---------------------------------------------------------------------------

def test_verify_detects_file_swapped_behind_ledger(tmp_path):
    """登记后正本被同名换内容 → 对账发现并给出新旧 SHA256（issue #18 LoRA 教训）。

    只记文件名的年代「换版后旧对照静默作废」；台账记了 SHA256，重算就能指认。
    """
    directory = tmp_path / "GEN001"
    source = _write_source(tmp_path, "girl.png", b"content-v1")
    intake_assets(directory, [AssetSubmission("红裙女孩", KIND_PICTURE, str(source))],
                  registry=_registry((1, "红裙女孩")))
    old_sha = load_asset_ledger(directory).entries[0].sha256

    # 换版不经交回入口：直接改会话正本（模拟别人动了文件）
    ledger = load_asset_ledger(directory)
    (directory / ledger.entries[0].path).write_bytes(b"swapped-content")
    drift = verify_asset_ledger(directory)
    assert len(drift) == 1
    assert drift[0].name == "红裙女孩"
    assert drift[0].recorded_sha256 == old_sha
    assert drift[0].actual_sha256 == hashlib.sha256(b"swapped-content").hexdigest()

    # 文件被删也是一类差异（actual=None），不是静默忽略
    (directory / ledger.entries[0].path).unlink()
    drift = verify_asset_ledger(directory)
    assert drift[0].actual_sha256 is None


def test_intake_reports_drift_of_already_registered_files(tmp_path):
    """交回入口先对账：正本被换过 → 差异进报告，不等到下游才发现。"""
    directory = tmp_path / "GEN001"
    registry = _registry((1, "红裙女孩"))
    source = _write_source(tmp_path, "girl.png", b"content-v1")
    intake_assets(directory, [AssetSubmission("红裙女孩", KIND_PICTURE, str(source))],
                  registry=registry)
    ledger = load_asset_ledger(directory)
    (directory / ledger.entries[0].path).write_bytes(b"swapped")

    extra = _write_source(tmp_path, "key.png", b"key")
    report = intake_assets(directory, [AssetSubmission("青铜钥匙", KIND_PICTURE, str(extra))],
                           registry=_registry((1, "红裙女孩"), (2, "青铜钥匙")))
    assert [d.name for d in report.drift] == ["红裙女孩"]


def test_ledger_corruption_is_loud_not_silent(tmp_path):
    """台账文件损坏/错形要响：它是精确数据，吞掉就是「台账悄悄不见了」。"""
    directory = tmp_path / "GEN001"
    directory.mkdir(parents=True)
    (directory / "asset-ledger.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError, match="损坏"):
        load_asset_ledger(directory)

    (directory / "asset-ledger.json").write_text(
        json.dumps({"schema_version": "asset-ledger.v1", "entries": [{"name": "", "kind": "picture",
                                                                      "number": 1, "path": "assets/x.png",
                                                                      "sha256": "0" * 64, "bogus": 1}],
                    "replacements": []}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="未知字段"):
        load_asset_ledger(directory)


# ---------------------------------------------------------------------------
# 出站渲染：素材台账权威清单 + 入库报告（向导/CLI 共用一份渲染）
# ---------------------------------------------------------------------------

def test_render_ledger_shows_five_fields(tmp_path):
    """台账出站（用户故事 12）：语义名/类型/官方编号/路径/SHA256 都在清单里可读。"""
    directory = tmp_path / "GEN001"
    source = _write_source(tmp_path, "girl.png", b"content-v1")
    intake_assets(directory, [AssetSubmission("红裙女孩", KIND_PICTURE, str(source))],
                  registry=_registry((1, "红裙女孩")))
    text = render_ledger(load_asset_ledger(directory))
    assert "<Picture 1>" in text          # 官方编号
    assert "红裙女孩" in text              # 语义名
    assert "图" in text                    # 类型（图/视频/音频）
    assert "assets/picture-01.png" in text # 路径
    entry = load_asset_ledger(directory).entries[0]
    assert entry.sha256[:16] in text      # SHA256（终端可读口径，全量在 JSON）


def test_render_report_surfaces_replacement_and_missing(tmp_path):
    """入库报告出站：换版新旧哈希对照、未交回点名、未登记点名——三条都不静默。"""
    directory = tmp_path / "GEN001"
    registry = _registry((1, "红裙女孩"), (2, "青铜钥匙"))
    v1 = _write_source(tmp_path, "girl-v1.png", b"content-v1")
    intake_assets(directory, [AssetSubmission("红裙女孩", KIND_PICTURE, str(v1))], registry=registry)
    v2 = _write_source(tmp_path, "girl-v2.png", b"content-v2-different")
    extra = _write_source(tmp_path, "extra.png", b"extra")
    report = intake_assets(
        directory,
        [AssetSubmission("红裙女孩", KIND_PICTURE, str(v2)),
         AssetSubmission("神秘来客", KIND_PICTURE, str(extra))],
        registry=registry,
        on_replace=lambda existing, incoming: True,
    )
    text = render_report(report)
    old_sha = hashlib.sha256(b"content-v1").hexdigest()
    new_sha = hashlib.sha256(b"content-v2-different").hexdigest()
    assert old_sha[:16] in text and new_sha[:16] in text  # 换版对照
    assert "青铜钥匙" in text                              # 未交回点名
    assert "神秘来客" in text                              # 未登记点名


# ---------------------------------------------------------------------------
# 结构化产物出口（既有缝，扩 mode="ref"）：brief 路径同步建账
# ---------------------------------------------------------------------------

def _stub_pipeline(monkeypatch):
    """打桩 LLM 层（历史教训：暗读真 key 的用例只在本机能过）。"""
    from minimax_h3_prompt import topic_generation

    monkeypatch.setattr(topic_generation, "run_pipeline_structured",
                        lambda *a, **k: (object(), object()))
    monkeypatch.setattr(topic_generation, "save_generation",
                        lambda result, directory, overwrite=False: Path(directory))


def test_brief_refs_with_paths_build_ledger(tmp_path, monkeypatch, capsys):
    """brief 声明的参考素材带路径 ＝ 交回入库：台账五字段 + 正本复制，编号按用户声明。"""
    from minimax_h3_prompt.topic_generation import create_video_from_topic

    _stub_pipeline(monkeypatch)
    girl = _write_source(tmp_path, "girl.png", b"girl-bytes")
    key = _write_source(tmp_path, "key.png", b"key-bytes")
    refs = [
        RefItem(picture=1, name="红裙女孩", description="红裙", path=str(girl)),
        RefItem(picture=2, name="青铜钥匙", description="钥匙", path=str(key)),
    ]
    result, directory = create_video_from_topic("测试主题", _cfg(tmp_path), mode="ref", refs=refs)

    ledger = load_asset_ledger(directory)
    assert [(e.name, e.number) for e in ledger.entries] == [("红裙女孩", 1), ("青铜钥匙", 2)]
    assert ledger.entries[0].sha256 == hashlib.sha256(b"girl-bytes").hexdigest()
    assert (directory / ledger.entries[0].path).is_file()


def test_brief_refs_without_paths_are_reported_missing(tmp_path, monkeypatch, capsys):
    """只声明语义名没交文件 ＝ 尚未交回：点名报告，不登记空壳条目（不静默）。"""
    from minimax_h3_prompt.topic_generation import create_video_from_topic

    _stub_pipeline(monkeypatch)
    refs = [RefItem(picture=1, name="红裙女孩", description="红裙")]
    result, directory = create_video_from_topic("测试主题", _cfg(tmp_path), mode="ref", refs=refs)
    assert load_asset_ledger(directory).entries == ()
    out = capsys.readouterr().out
    assert "红裙女孩" in out and "还没交回" in out


def test_base_mode_frame_refs_never_enter_ledger(tmp_path, monkeypatch):
    """base 模式的 refs 是首尾帧锚点，不是参考素材——绝不进素材台账（守卫）。"""
    from minimax_h3_prompt.topic_generation import create_video_from_topic

    _stub_pipeline(monkeypatch)
    frame = _write_source(tmp_path, "first.png", b"frame-bytes")
    refs = [RefItem(picture=1, name="首帧", description="", path=str(frame))]
    result, directory = create_video_from_topic("测试主题", _cfg(tmp_path), mode="base",
                                                variant="I2VA", refs=refs)
    assert not (directory / "asset-ledger.json").exists()
    assert load_asset_ledger(directory).entries == ()


def test_brief_reintake_swapped_file_replaces_and_reports(tmp_path, monkeypatch, capsys):
    """brief 路径重跑遇同名文件换版（同路径内容变了）：非交互口径自动替换，但照报不静默（AC3）。

    正是 issue #18 LoRA 教训的原型场景——同一个文件名底下的内容被换掉；CLI 没有人
    在场点头（on_replace=None），替换是重跑的本意，但新旧哈希必须两头都报出来。
    """
    from minimax_h3_prompt.topic_generation import create_video_from_topic

    _stub_pipeline(monkeypatch)
    girl = _write_source(tmp_path, "girl.png", b"content-v1")
    refs = [RefItem(picture=1, name="红裙女孩", description="红裙", path=str(girl))]
    create_video_from_topic("测试主题", _cfg(tmp_path), mode="ref", refs=refs)
    old_sha = load_asset_ledger(_gen_dir(tmp_path)).entries[0].sha256

    girl.write_bytes(b"content-v2-different")  # 同名文件换版：路径不变、内容变
    create_video_from_topic("测试主题", _cfg(tmp_path), mode="ref", refs=refs)

    out = capsys.readouterr().out
    new_sha = hashlib.sha256(b"content-v2-different").hexdigest()
    assert "换版" in out
    assert old_sha[:16] in out and new_sha[:16] in out  # 新旧哈希两头都报
    ledger = load_asset_ledger(_gen_dir(tmp_path))
    assert ledger.entries[0].sha256 == new_sha           # 已换成新版
    assert ledger.replacements[0].old_sha256 == old_sha  # 旧哈希留史（旧产物可复测）
    copied = _gen_dir(tmp_path) / ledger.entries[0].path
    assert copied.read_bytes() == b"content-v2-different"  # 正本同步更新


def _gen_dir(tmp_path: Path) -> Path:
    return next((tmp_path / "sessions").rglob("asset-ledger.json")).parent

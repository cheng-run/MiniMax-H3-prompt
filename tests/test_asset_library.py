"""素材库落点与台账路径解析（issue #85 / spec #84 D1–D3）。

接缝（spec #84「Testing Decisions」预议的 ①②）：

1. ``intake_assets`` 的复制目标与命名（外部行为：库里出现什么文件、生成目录还剩什么）；
2. 台账解析口 ``resolve_asset_path``（外部行为：读图/对账/撤下能不能找到那份正本）。

全部断言落在**盘上的文件与台账 JSON**上，不碰内部实现。素材库是仓库外的长期目录
（默认 ``D:\\素材库``），所以每条用例都把库根指到临时目录——测试绝不许写进真实素材库。
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
    remove_asset,
    resolve_asset_path,
    verify_asset_ledger,
)
from minimax_h3_prompt.brief_parser import RefItem
from minimax_h3_prompt.library_config import (
    DEFAULT_LIBRARY_ROOT,
    load_library_settings,
)


def _registry(*pairs: tuple[int, str]) -> list[RefItem]:
    return [RefItem(picture=n, name=name, description=f"{name}的生图提示词")
            for n, name in pairs]


def _write_source(tmp_path: Path, name: str, content: bytes) -> Path:
    source = tmp_path / "returned" / name
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(content)
    return source


def _gen_dir(tmp_path: Path, slug: str = "一个汽水广告-a77dca42") -> Path:
    """一次生成运行的目录形状：``<sessions>/<slug>/GEN001``（与向导/CLI 一致）。"""
    directory = tmp_path / "sessions" / slug / "GEN001"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


# ---------------------------------------------------------------------------
# 配置读取口：库根可配置（D1），写错了要响、没写才回落默认
# ---------------------------------------------------------------------------

def test_library_root_comes_from_config_file(tmp_path):
    """库根从 ``config/library.yaml`` 读，换机器只改配置、不改代码。"""
    config = tmp_path / "library.yaml"
    config.write_text("root: 'E:/别的素材库'\n", encoding="utf-8")

    assert load_library_settings(config).root == Path("E:/别的素材库")


def test_missing_library_config_falls_back_to_default_root(tmp_path):
    """没有配置文件＝用户没自定义，回落默认 ``D:\\素材库``（流程不能因缺配置走不下去）。"""
    assert load_library_settings(tmp_path / "absent.yaml").root == DEFAULT_LIBRARY_ROOT


@pytest.mark.parametrize("body, match", [
    ("roooot: 'D:/素材库'\n", "未知字段"),          # 键打错：静默回落默认＝用户以为改了其实没改
    ("root: '素材库'\n", "绝对路径"),                # 相对库根会随 CWD 漂，落点不可复现
    ("root: 42\n", "字符串"),                        # 类型写错同样是「写了但写错」
])
def test_written_but_broken_library_config_is_loud(tmp_path, body, match):
    """写了的非法配置大声报（同 workflow.yaml 纪律）：静默回默认会把笔误藏起来。"""
    config = tmp_path / "library.yaml"
    config.write_text(body, encoding="utf-8")
    with pytest.raises(ValueError, match=match):
        load_library_settings(config)


# ---------------------------------------------------------------------------
# 入库落点与命名（D1/D2/D3）：编号-语义名，生成目录不留图
# ---------------------------------------------------------------------------

def test_intake_copies_into_library_with_numbered_semantic_name(tmp_path):
    """交回一张图 → 库里出现 ``<slug>/1-便利店冰柜.png``，生成目录不再保存副本。"""
    library = tmp_path / "library"
    directory = _gen_dir(tmp_path)
    source = _write_source(tmp_path, "冰柜.png", b"fridge-bytes")

    report = intake_assets(
        directory,
        [AssetSubmission(name="便利店冰柜", kind=KIND_PICTURE, source=str(source))],
        registry=_registry((1, "便利店冰柜")),
        library_root=library,
    )

    entry = report.registered[0]
    assert entry.path == "一个汽水广告-a77dca42/1-便利店冰柜.png"
    assert (library / entry.path).read_bytes() == b"fridge-bytes"
    # 生成目录里没有图片副本，也不再建 assets/ 空壳（盘点时那会被当成「这轮存了图」）
    assert not list(directory.rglob("*.png"))
    assert not (directory / "assets").exists()
    assert not (directory / "便利店冰柜.png").exists()


def test_ledger_records_library_root_beside_relative_path(tmp_path):
    """台账记库根 + 相对路径（AC2）：换机器把库整盘搬走后，改库根就能重新对上账。"""
    library = tmp_path / "library"
    directory = _gen_dir(tmp_path)
    source = _write_source(tmp_path, "冰柜.png", b"x")

    intake_assets(directory, [AssetSubmission("便利店冰柜", KIND_PICTURE, str(source))],
                  registry=_registry((1, "便利店冰柜")), library_root=library)

    raw = json.loads((directory / "asset-ledger.json").read_text(encoding="utf-8"))
    assert raw["library_root"] == str(library)
    assert raw["entries"][0]["path"] == "一个汽水广告-a77dca42/1-便利店冰柜.png"


def test_revision_keeps_old_file_and_appends_r2(tmp_path):
    """换版：新文件 ``-r2``、旧文件仍在（D3 可回退），台账换版史两头都记。"""
    library = tmp_path / "library"
    directory = _gen_dir(tmp_path)
    registry = _registry((1, "便利店冰柜"))
    first = _write_source(tmp_path, "v1.png", b"v1-bytes")
    intake_assets(directory, [AssetSubmission("便利店冰柜", KIND_PICTURE, str(first))],
                  registry=registry, library_root=library)
    old = load_asset_ledger(directory).entries[0]

    second = _write_source(tmp_path, "v2.png", b"v2-bytes")
    report = intake_assets(directory, [AssetSubmission("便利店冰柜", KIND_PICTURE, str(second))],
                           registry=registry)  # 非交互口径（CLI 重跑）：接受替换
    new = report.ledger.entries[0]

    assert old.path.endswith("/1-便利店冰柜.png")
    assert new.path.endswith("/1-便利店冰柜-r2.png")
    assert (library / new.path).read_bytes() == b"v2-bytes"
    assert (library / old.path).read_bytes() == b"v1-bytes", "换版把上一版文件删了（不可回退）"
    assert report.replaced[0].old_path == old.path
    assert report.replaced[0].new_path == new.path

    third = _write_source(tmp_path, "v3.png", b"v3-bytes")
    intake_assets(directory, [AssetSubmission("便利店冰柜", KIND_PICTURE, str(third))],
                  registry=registry)
    newest = load_asset_ledger(directory).entries[0]
    assert newest.path.endswith("/1-便利店冰柜-r3.png"), "换版号没跟着上一版走"
    assert (library / old.path).is_file() and (library / new.path).is_file()


def test_video_and_audio_carry_a_kind_prefix_so_per_kind_numbers_never_collide(tmp_path):
    """三型各自独立编号：图 1 与视频 1 同名时，视频**首版**不许被认成「换版 -r2」。

    换版扫描按文件名 stem 认上一版；不带词头时 ``1-红裙女孩.png`` 与视频的
    ``1-红裙女孩.mp4`` 同 stem，首版视频会拿到 ``-r2`` 而台账里没有任何换版记录
    （issue #85 复审实测）。词头按 spec D3：视频 ``Video N-``、音频 ``Audio N-``，
    图不带词头（数据布局示例就是 ``1-便利店冰柜.png``）。
    """
    library = tmp_path / "library"
    directory = _gen_dir(tmp_path)
    picture = _write_source(tmp_path, "p.png", b"p")
    video = _write_source(tmp_path, "v.mp4", b"v")
    audio = _write_source(tmp_path, "a.wav", b"a")

    report = intake_assets(directory, [
        AssetSubmission("红裙女孩", KIND_PICTURE, str(picture)),
        AssetSubmission("红裙女孩", KIND_VIDEO, str(video)),
        AssetSubmission("红裙女孩", KIND_AUDIO, str(audio)),
    ], registry=_registry((1, "红裙女孩")), library_root=library)

    paths = {e.kind: e.path for e in report.ledger.entries}
    assert paths[KIND_PICTURE] == "一个汽水广告-a77dca42/1-红裙女孩.png"
    assert paths[KIND_VIDEO] == "一个汽水广告-a77dca42/Video 1-红裙女孩.mp4"
    assert paths[KIND_AUDIO] == "一个汽水广告-a77dca42/Audio 1-红裙女孩.wav"
    assert report.replaced == () and report.ledger.replacements == (), \
        "首版视频/音频被记成了换版（无来源的 -r2）"

    # 视频换版照常 -r2，且只动视频那一份（图的 1- 不被连坐）
    video2 = _write_source(tmp_path, "v2.mp4", b"v2")
    report2 = intake_assets(directory, [AssetSubmission("红裙女孩", KIND_VIDEO, str(video2))],
                            registry=_registry((1, "红裙女孩")))
    assert report2.ledger.get("红裙女孩", KIND_VIDEO).path \
        .endswith("/Video 1-红裙女孩-r2.mp4")
    assert (library / paths[KIND_PICTURE]).read_bytes() == b"p"
    assert (library / paths[KIND_VIDEO]).read_bytes() == b"v"


def test_same_topic_rerun_uses_minus_2_subdir_and_keeps_previous_round(tmp_path):
    """同一主题重跑 → 另开 ``-2`` 子目录：上一轮素材原样保留，不被改写。"""
    library = tmp_path / "library"
    slug = "一个汽水广告-a77dca42"
    registry = _registry((1, "便利店冰柜"))
    first_run = _gen_dir(tmp_path, slug)
    source = _write_source(tmp_path, "v1.png", b"round-1")
    intake_assets(first_run, [AssetSubmission("便利店冰柜", KIND_PICTURE, str(source))],
                  registry=registry, library_root=library)
    round1 = load_asset_ledger(first_run).entries[0]

    # 新的一轮＝同主题下的新 GEN 目录（向导/CLI 的续接规则：完成的会话另开 GEN002）
    second_run = tmp_path / "sessions" / slug / "GEN002"
    second_run.mkdir(parents=True)
    source2 = _write_source(tmp_path, "v2.png", b"round-2")
    report = intake_assets(
        second_run, [AssetSubmission("便利店冰柜", KIND_PICTURE, str(source2))],
        registry=registry, library_root=library)
    round2 = report.registered[0]

    assert round2.path == f"{slug}-2/1-便利店冰柜.png"
    assert (library / round2.path).read_bytes() == b"round-2"
    assert (library / round1.path).read_bytes() == b"round-1", "重跑改写了上一轮的文件"


def test_second_asset_in_the_same_run_reuses_the_same_subdir(tmp_path):
    """同一轮逐张交回沿用同一个子目录：不许每交一张另开一个 ``-2``。"""
    library = tmp_path / "library"
    directory = _gen_dir(tmp_path)
    first = _write_source(tmp_path, "a.png", b"a")
    second = _write_source(tmp_path, "b.png", b"b")

    intake_assets(directory, [AssetSubmission("便利店冰柜", KIND_PICTURE, str(first))],
                  registry=_registry((1, "便利店冰柜")), library_root=library)
    intake_assets(directory, [AssetSubmission("橘猫", KIND_PICTURE, str(second))],
                  registry=_registry((1, "便利店冰柜"), (2, "橘猫")))

    paths = [e.path for e in load_asset_ledger(directory).entries]
    assert paths == ["一个汽水广告-a77dca42/1-便利店冰柜.png",
                     "一个汽水广告-a77dca42/2-橘猫.png"]


# ---------------------------------------------------------------------------
# 统一解析口（D2）：对账/撤下/旧台账都从 resolve_asset_path 取文件
# ---------------------------------------------------------------------------

def test_verify_finds_assets_in_the_library_not_the_generation_dir(tmp_path):
    """对账按库根找正本：改成 ``<生成目录>/<path>`` 就会把「文件好好的」报成丢了。"""
    library = tmp_path / "library"
    directory = _gen_dir(tmp_path)
    source = _write_source(tmp_path, "v1.png", b"v1-bytes")
    intake_assets(directory, [AssetSubmission("便利店冰柜", KIND_PICTURE, str(source))],
                  registry=_registry((1, "便利店冰柜")), library_root=library)

    ledger = load_asset_ledger(directory)
    entry = ledger.entries[0]
    assert resolve_asset_path(ledger, entry, session_dir=directory) == library / entry.path
    assert verify_asset_ledger(directory) == ()  # 对账能读到库里的正本 → 无差异

    # 正反两面：库里那份被换掉，对账照样发现（解析口是双向的，不是只换个前缀）
    (library / entry.path).write_bytes(b"swapped")
    drift = verify_asset_ledger(directory)
    assert [d.name for d in drift] == ["便利店冰柜"]


def test_remove_asset_drops_the_registration_but_keeps_the_library_file(tmp_path):
    """撤下只撤登记（废图不进片），库里的图不动——那是这件素材唯一的长期落点。

    向导问的是「从素材台账撤下」，人点头的不是「删掉图库里的图」；删文件是不可逆的，
    而登记才是「进不进片」的判据（注入卡与段接线都按台账取素材）。
    """
    library = tmp_path / "library"
    directory = _gen_dir(tmp_path)
    source = _write_source(tmp_path, "v1.png", b"v1-bytes")
    intake_assets(directory, [AssetSubmission("便利店冰柜", KIND_PICTURE, str(source))],
                  registry=_registry((1, "便利店冰柜")), library_root=library)
    entry = load_asset_ledger(directory).entries[0]

    removed = remove_asset(directory, "便利店冰柜", KIND_PICTURE)

    assert removed is not None
    assert not load_asset_ledger(directory).entries
    assert (library / entry.path).read_bytes() == b"v1-bytes", "撤登记把库里的图删了"


def test_legacy_ledger_without_library_root_reads_old_layout_and_stays_legacy(tmp_path):
    """旧台账（没有 library_root）不崩、按旧路径解析；后续交回留在旧布局（不劈两半）。"""
    directory = _gen_dir(tmp_path)
    legacy_file = directory / "assets" / "picture-01.png"
    legacy_file.parent.mkdir(parents=True)
    legacy_file.write_bytes(b"legacy-bytes")
    digest = hashlib.sha256(b"legacy-bytes").hexdigest()
    (directory / "asset-ledger.json").write_text(json.dumps({
        "schema_version": "asset-ledger.v1",
        "entries": [{
            "name": "红裙女孩", "kind": "picture", "number": 1,
            "path": "assets/picture-01.png", "sha256": digest, "source": "",
        }],
        "replacements": [],
    }, ensure_ascii=False), encoding="utf-8")

    ledger = load_asset_ledger(directory)
    entry = ledger.entries[0]
    assert ledger.library_root == ""
    assert resolve_asset_path(ledger, entry, session_dir=directory) == legacy_file
    assert verify_asset_ledger(directory) == ()

    # 旧会话续跑再交一张：库根参数在场也仍走旧布局——一本账只认一个解析口径
    extra = _write_source(tmp_path, "cat.png", b"cat")
    report = intake_assets(
        directory, [AssetSubmission("橘猫", KIND_PICTURE, str(extra))],
        registry=_registry((1, "红裙女孩"), (2, "橘猫")),
        library_root=tmp_path / "library")
    assert report.registered[0].path.startswith("assets/")
    assert (directory / report.registered[0].path).is_file()

    # 旧布局撤下仍连副本一起删（那份是交回时的副产品，原件在用户手里）：旧行为不变
    legacy = load_asset_ledger(directory)
    first = legacy.get("红裙女孩", KIND_PICTURE)
    remove_asset(directory, "红裙女孩", KIND_PICTURE)
    assert not resolve_asset_path(legacy, first, session_dir=directory).is_file()


def test_segment_plan_carries_the_resolved_library_path(tmp_path, monkeypatch):
    """段内素材子集落进 ``plan.json`` 的路径也过统一解析口（库内可直达的绝对路径）。

    接线卡与段表都按这张表给文件位置：这里退回 ``entry.path``（台账相对路径），
    用户照卡找不到图，而且不会有任何报错（issue #85 复审：卡/段两条消费线原本
    没有被变异检验咬住）。
    """
    from minimax_h3_prompt import model_factory
    from minimax_h3_prompt.brief_parser import Brief
    from minimax_h3_prompt.ref_segments import prepare_ref_segments

    # 机械拼装路径：不构造真实模型（历史教训：暗读真 key 的用例只在本机能过）
    monkeypatch.setattr(model_factory, "build_chat_model",
                        lambda: (_ for _ in ()).throw(RuntimeError("no llm in tests")))

    library = tmp_path / "library"
    directory = _gen_dir(tmp_path, "片子-abc12345")
    source = _write_source(tmp_path, "v1.png", b"x")
    intake_assets(directory, [AssetSubmission("红裙女孩", KIND_PICTURE, str(source))],
                  registry=_registry((1, "红裙女孩")), library_root=library)

    prompt = """subject_definitions:
<Subject 1> is 红裙女孩 (the girl in <Picture 1>), with a red dress.

summary:
[reference generation] The target video follows her through the garden.

retention_analysis:
<Subject 1>: fully_preserved - look retained.

detailed_description:
[Shot 1] A medium shot frames <Subject 1> in the garden.
[Shot 2] At 00:08.000, the camera pushes in on <Picture 1>.

overall_soundscape:
Soft outdoor ambience continues.

non_diegetic_music:
N/A
"""
    (directory / "video-prompt.md").write_text(prompt, encoding="utf-8")
    brief = Brief(mode="ref", variant="T2VA", duration=20.0, plot="红裙女孩",
                  refs=_registry((1, "红裙女孩")))

    prepare_ref_segments(whole_prompt=prompt, total_s=20.0, out_dir=directory,
                         state={}, brief=brief)

    rows = json.loads((directory / "segments" / "plan.json").read_text(encoding="utf-8"))
    paths = [picture["path"] for row in rows
             for picture in row["assets_in_segment"]["pictures"]]
    assert paths, "段内没有素材子集，本用例前提不成立"
    for path in paths:
        assert Path(library) in Path(path).parents, f"段表给的不是库内路径：{path}"
        assert Path(path).is_file(), f"段表里给的不是可直达的路径：{path}"


@pytest.mark.parametrize("bad_root", ["relative/library", "D:/素材库/../别处"])
def test_bad_library_roots_are_rejected_through_one_validator(tmp_path, bad_root):
    """库根的绝对路径/``..`` 校验三处入口共用一份：参数这条路也不许更宽松。"""
    directory = _gen_dir(tmp_path)
    source = _write_source(tmp_path, "v1.png", b"x")
    with pytest.raises(ValueError, match="绝对路径|不含 ..|不许含"):
        intake_assets(directory, [AssetSubmission("便利店冰柜", KIND_PICTURE, str(source))],
                      registry=_registry((1, "便利店冰柜")), library_root=bad_root)


def test_ledger_with_relative_library_root_is_loud(tmp_path):
    """台账里的库根坏了要响（它是「去哪找图」的唯一线索），不静默按 CWD 解析。"""
    directory = _gen_dir(tmp_path)
    (directory / "asset-ledger.json").write_text(json.dumps({
        "schema_version": "asset-ledger.v1",
        "library_root": "library",
        "entries": [],
        "replacements": [],
    }, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError, match="绝对路径"):
        load_asset_ledger(directory)


def test_illegal_characters_in_semantic_name_are_replaced_not_escaped(tmp_path):
    """语义名去非法字符：``/`` ``?`` 之类不许把文件写到别的目录或变成通配符。"""
    library = tmp_path / "library"
    directory = _gen_dir(tmp_path)
    source = _write_source(tmp_path, "shot.png", b"x")

    report = intake_assets(
        directory,
        [AssetSubmission(name="枪/刀?特写", kind=KIND_PICTURE, source=str(source))],
        registry=_registry((1, "枪/刀?特写")),
        library_root=library,
    )

    entry = report.registered[0]
    assert entry.path == "一个汽水广告-a77dca42/1-枪_刀_特写.png"
    assert (library / entry.path).is_file()

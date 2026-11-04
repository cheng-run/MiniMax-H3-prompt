"""sidecar 写口：merge 累积、write 重置（#157 复审采纳，2026-11-04）。

为什么单独立用例：``write_frame_meta`` 是整份覆盖，``merge_frame_meta`` 是累积合并。
两者并存时，调用方很容易选错——#158 的首帧图路径就是被 #160 的首帧词重写整份覆盖
抹掉的，只有复审抓到。这里把两个写口的契约钉死，调用方选错时至少有一处会红。
"""
from __future__ import annotations

from pathlib import Path

from minimax_h3_prompt.director.frame_meta import (
    merge_frame_meta,
    read_frame_meta,
    write_frame_meta,
)


def _prompt(tmp_path: Path) -> Path:
    path = tmp_path / "firstframe-02.md"
    path.write_text("prompt\n", encoding="utf-8")
    return path


def test_merge_keeps_unknown_fields_and_overwrites_named_keys(tmp_path):
    """另一个写口的字段（首帧图路径）与未知字段都要留下；本次字段覆盖同名键。"""
    path = _prompt(tmp_path)
    write_frame_meta(path, {
        "kind": "firstframe",
        "first_frame_image": "D:/frames/cut-2.png",
        "stale": 1,
    })

    merge_frame_meta(path, {"model": "qwen"})

    meta = read_frame_meta(path)
    assert meta is not None
    assert meta["first_frame_image"] == "D:/frames/cut-2.png"
    assert meta["stale"] == 1
    assert meta["model"] == "qwen"
    assert meta["kind"] == "firstframe"


def test_merge_defaults_only_fill_missing_keys(tmp_path):
    """``defaults`` 只补缺，不许覆盖既有值（kind 从 firstframe 被改回是回归）。"""
    path = _prompt(tmp_path)
    merge_frame_meta(
        path, {"model": "qwen"}, defaults={"kind": "firstframe", "segment": 2})
    assert read_frame_meta(path) == {
        "kind": "firstframe", "segment": 2, "model": "qwen"}

    merge_frame_meta(path, {"model": "moss"}, defaults={"kind": "tailframe"})
    meta = read_frame_meta(path)
    assert meta is not None
    assert meta["kind"] == "firstframe", "defaults 不许覆盖既有值"
    assert meta["model"] == "moss"


def test_merge_on_missing_or_corrupt_sidecar_starts_fresh(tmp_path):
    """没有 sidecar 与坏 sidecar 都按「从空开始」处理，不读崩。"""
    path = _prompt(tmp_path)
    merge_frame_meta(path, {"model": "qwen"})
    assert read_frame_meta(path) == {"model": "qwen"}

    (tmp_path / "firstframe-02.meta.json").write_text("{not json", encoding="utf-8")
    merge_frame_meta(path, {"model": "moss"})
    assert read_frame_meta(path) == {"model": "moss"}


def test_write_frame_meta_still_replaces(tmp_path):
    """整份重置仍是显式选项（全片尾帧换图后重读描述用它）。"""
    path = _prompt(tmp_path)
    write_frame_meta(path, {"kind": "film_end", "description": "旧描述"})
    write_frame_meta(path, {"kind": "film_end", "description": "新描述"})
    assert read_frame_meta(path) == {"kind": "film_end", "description": "新描述"}

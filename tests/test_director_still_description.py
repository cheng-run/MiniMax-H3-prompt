"""共享读图实现的唯一单测口（#318）：取描述、按图指纹复用 sidecar、失败抛错。

缝（与票面 AC 一一对应，均为**外部可观察行为**）：

1. ``director.still_description.get_still_description``——读图一次、落 sidecar、
   按图指纹复用；差异只走参数（sidecar 的 ``kind``、失败消息里的用途说法）；
2. ``director.still_description.read_cached_description``——离线导出用的只读口：
   只读 sidecar，绝不读图、绝不写盘；
3. 缓存命中契约三条（``kind`` 相符 ＋ 描述非空 ＋ 图指纹一致）共享同一份实现，
   任何一条不满足就重读并覆盖。

全片尾帧改用这份实现后，``tests/test_director_film_end.py`` 一字不改仍全绿——
那是「行为与 sidecar 输出逐字节不变」的回归证据，所以这里不重复测全片尾帧的
字段取值，只测共享实现自己的契约。视觉调用一律打桩（出网守卫会拦真实请求）。
"""
from __future__ import annotations

import json

import pytest

# 断言用的描述：与全片尾帧用例同一份形态，便于对照两边行为一致。
_DESCRIPTION = "一个男人背对镜头，沿积水的小路走向院门，没有回头。"
_KIND = "film_end"
_FAILURE_LABEL = "全片尾帧读图失败"


def _image(tmp_path):
    path = tmp_path / "still.png"
    path.write_bytes(b"first-version-of-the-still")
    return path


def _sidecar(image):
    return image.with_name(image.stem + ".meta.json")


def _patch_describe(monkeypatch, behaviour):
    """打桩唯一视觉调用口；返回记录每次调用的列表（照抄既有全片尾帧用例的手法）。"""
    from minimax_h3_prompt.tools import reference_auditor

    calls: list = []

    def fake_describe(path, instruction=None):
        calls.append(path)
        return behaviour(len(calls), path)

    monkeypatch.setattr(reference_auditor, "describe_image", fake_describe)
    return calls


def _get(image, *, kind=_KIND, label=_FAILURE_LABEL):
    from minimax_h3_prompt.director.still_description import get_still_description

    return get_still_description(image, kind=kind, failure_label=label)


# ---------------------------------------------------------------------------
# 取描述：读一次、落 sidecar、指纹复用
# ---------------------------------------------------------------------------

def test_first_read_writes_the_sidecar_next_to_the_image(tmp_path, monkeypatch):
    """首次取描述：读图一次，sidecar 与图同目录，字段沿用既有形态。"""
    from minimax_h3_prompt.config import config
    from minimax_h3_prompt.director.frame_meta import sha256_file

    image = _image(tmp_path)
    calls = _patch_describe(monkeypatch, lambda _n, _p: _DESCRIPTION)

    assert _get(image) == _DESCRIPTION
    assert calls == [image], "首次取描述应当读图一次，且读的就是盘上那张图"

    meta_path = _sidecar(image)
    assert meta_path.is_file(), "sidecar 必须与图同目录落 <stem>.meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    assert meta["kind"] == _KIND, "sidecar 的用途标记取参数，不是写死的字面量"
    assert meta["description"] == _DESCRIPTION
    assert meta["vision_model"] == config.vision_model.model, \
        "视觉模型名必须从配置现取并落盘（事后可追溯是哪个模型读的）"
    assert meta["image_sha256"] == sha256_file(image), "sidecar 要带图指纹"
    assert meta["at"], "落盘时间不能空"


def test_matching_fingerprint_reuses_the_sidecar_without_reading_again(
        tmp_path, monkeypatch):
    """图没换：第二次取描述不重读图（续跑不重复付费）。"""
    image = _image(tmp_path)
    calls = _patch_describe(monkeypatch, lambda _n, _p: _DESCRIPTION)
    assert _get(image) == _DESCRIPTION
    assert len(calls) == 1, "首次取描述只读一次图"

    def _boom(_n, _p):
        raise AssertionError("图没换又读了图：续跑重复付费")

    monkeypatch.setattr(
        "minimax_h3_prompt.tools.reference_auditor.describe_image", _boom)
    assert _get(image) == _DESCRIPTION


def test_changed_fingerprint_rereads_and_overwrites_the_sidecar(
        tmp_path, monkeypatch):
    """图换了：指纹变 → 重读并覆盖 sidecar（旧描述不许继续约束段词）。"""
    from minimax_h3_prompt.director.frame_meta import sha256_file

    image = _image(tmp_path)
    _patch_describe(monkeypatch, lambda _n, _p: _DESCRIPTION)
    assert _get(image) == _DESCRIPTION

    image.write_bytes(b"second-version-of-the-still")
    calls = _patch_describe(monkeypatch, lambda _n, _p: "新画面：男人已经站在院门里。")
    assert _get(image) == "新画面：男人已经站在院门里。"
    assert len(calls) == 1, "指纹变了必须重读"

    meta = json.loads(_sidecar(image).read_text(encoding="utf-8"))
    assert meta["description"] == "新画面：男人已经站在院门里。"
    assert meta["image_sha256"] == sha256_file(image), "覆盖后的指纹要跟上新图"


def test_corrupt_sidecar_is_treated_as_missing_and_reread(tmp_path, monkeypatch):
    """坏 sidecar 按缺失处理：重读并修好，不把续跑拦死。"""
    image = _image(tmp_path)
    _sidecar(image).write_text("{not json", encoding="utf-8")
    calls = _patch_describe(monkeypatch, lambda _n, _p: _DESCRIPTION)

    assert _get(image) == _DESCRIPTION
    assert len(calls) == 1
    meta = json.loads(_sidecar(image).read_text(encoding="utf-8"))
    assert meta["description"] == _DESCRIPTION


# ---------------------------------------------------------------------------
# 差异参数据化：kind 与失败说法都来自参数，不靠调用方各写一套分支
# ---------------------------------------------------------------------------

def test_sidecar_written_for_another_kind_is_not_reused(tmp_path, monkeypatch):
    """用途标记不符就是不命中：同一张图在不同用途下各自读、各自落 sidecar。

    这条同时钉住「kind 由参数决定」——写死成某个字面量时，另一个用途的 sidecar
    会被误认成自己的（后续开场首帧读图正是第二个用途）。
    """
    image = _image(tmp_path)
    _patch_describe(monkeypatch, lambda _n, _p: _DESCRIPTION)
    assert _get(image, kind="opening_still") == _DESCRIPTION

    _patch_describe(monkeypatch, lambda _n, _p: "另一用途的描述")
    assert _get(image, kind=_KIND) == "另一用途的描述", \
        "别的用途留下的 sidecar 不许被当成自己的缓存"

    meta = json.loads(_sidecar(image).read_text(encoding="utf-8"))
    assert meta["kind"] == _KIND, "重读后用途标记要跟着参数走"


def test_failure_message_carries_the_parameterized_label_and_the_path(
        tmp_path, monkeypatch):
    """失败说法来自参数：同一个共享实现，不同用途各说自己的那句话。"""
    image = _image(tmp_path)

    def fail(_path, instruction=None):
        raise RuntimeError("网关 502")

    monkeypatch.setattr(
        "minimax_h3_prompt.tools.reference_auditor.describe_image", fail)

    with pytest.raises(RuntimeError) as excinfo:
        _get(image, kind="opening_still", label="开场首帧读图失败")
    message = str(excinfo.value)
    assert message.startswith("开场首帧读图失败（图："), \
        "用途说法要按参数出现在消息开头（调用方不必再包一层）"
    assert str(image) in message, "错误里要点名是哪张图"
    assert "网关 502" in message, "原始错误不能吞掉"
    assert not _sidecar(image).exists(), "读图失败不许留下 sidecar"


def test_empty_vision_output_raises_and_leaves_no_sidecar(tmp_path, monkeypatch):
    """视觉模型返回空白：同样抛错且不落盘（空描述不是「没有终点画面」）。"""
    image = _image(tmp_path)
    calls = _patch_describe(monkeypatch, lambda _n, _p: "   ")

    with pytest.raises(RuntimeError) as excinfo:
        _get(image)
    assert "没有返回任何内容" in str(excinfo.value)
    assert len(calls) == 1
    assert not _sidecar(image).exists(), "空描述不许落成 sidecar"


# ---------------------------------------------------------------------------
# 只读口：离线导出复现同一份缓存判据，但绝不读图、绝不写盘
# ---------------------------------------------------------------------------

def test_read_cached_description_returns_none_without_reading_or_writing(
        tmp_path, monkeypatch):
    """没有 sidecar：只读口返回 None，不调视觉模型、不落盘（导出脚本能安全询问）。"""
    from minimax_h3_prompt.director.still_description import read_cached_description

    image = _image(tmp_path)
    calls = _patch_describe(monkeypatch, lambda _n, _p: _DESCRIPTION)

    assert read_cached_description(image, kind=_KIND) is None
    assert calls == [], "只读口绝不许现场读图"
    assert not _sidecar(image).exists(), "只读口绝不许写盘"


def test_read_cached_description_matches_the_cache_hit_contract(
        tmp_path, monkeypatch):
    """只读口与取描述走同一份命中判据：写进去的那份，只读口原样认得。"""
    from minimax_h3_prompt.director.still_description import read_cached_description

    image = _image(tmp_path)
    _patch_describe(monkeypatch, lambda _n, _p: _DESCRIPTION)
    assert _get(image) == _DESCRIPTION

    assert read_cached_description(image, kind=_KIND) == _DESCRIPTION
    assert read_cached_description(image, kind="opening_still") is None, \
        "用途标记不符还是不命中"

    image.write_bytes(b"changed-bytes")
    assert read_cached_description(image, kind=_KIND) is None, \
        "图换了指纹对不上，只读口也要判不命中"

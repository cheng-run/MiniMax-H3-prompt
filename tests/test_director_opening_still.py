"""开场首帧读图与事实块渲染（#321）：两个交付物，本票不接调用方。

缝（与票面 AC 一一对应，均为**外部可观察行为**）：

1. ``director.opening_still.get_opening_still_description``——盘上有图才读、落 sidecar、
   按图指纹复用；读图失败抛错且不落盘；
2. ``director.opening_still.render_opening_still_block``——块标题、描述正文、
   一致性纪律句（真源常量）、「冲突时以画面为准」一句；规划期与写段期只差半句。

视觉调用一律打桩（照抄 ``tests/test_director_film_end.py`` 的 ``_patch_describe``）。
"""
from __future__ import annotations

import json

import pytest

# 断言用的描述：与全片尾帧/共享读图用例同一份形态，便于对照三条路径一致。
_DESCRIPTION = "一个男人背对镜头，沿积水的小路走向院门，没有回头。"


def _session_dir(tmp_path):
    """开场首帧图所在目录：``director/assets/01/start.png``（打包的契约落点）。"""
    directory = tmp_path / "GEN001"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _image(session_dir):
    path = session_dir / "director" / "assets" / "01" / "start.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"first-version-of-the-opening-still")
    return path


def _sidecar(image):
    return image.with_name(image.stem + ".meta.json")


def _patch_describe(monkeypatch, behaviour):
    """打桩唯一视觉调用口；返回记录每次调用的列表。"""
    from minimax_h3_prompt.tools import reference_auditor

    calls: list = []

    def fake_describe(path, instruction=None):
        calls.append(path)
        return behaviour(len(calls), path)

    monkeypatch.setattr(reference_auditor, "describe_image", fake_describe)
    return calls


# ---------------------------------------------------------------------------
# 读图口：没有图就不读、不落盘
# ---------------------------------------------------------------------------

def test_no_image_returns_none_without_reading_or_writing(tmp_path, monkeypatch):
    """盘上没有开场首帧图：返回 None，不调视觉模型、不产生 sidecar。"""
    from minimax_h3_prompt.director.opening_still import (
        get_opening_still_description,
    )

    session_dir = _session_dir(tmp_path)
    calls = _patch_describe(monkeypatch, lambda _n, _p: _DESCRIPTION)

    assert get_opening_still_description(session_dir) is None
    assert calls == [], "没有图就不许读图"
    assert not (session_dir / "director").exists(), "没有图不许凭空建目录/落盘"


# ---------------------------------------------------------------------------
# 读图口：有图读一次、落 sidecar（用途标记 / 描述 / 图指纹 / 读图模型）
# ---------------------------------------------------------------------------

def test_first_read_writes_a_sidecar_next_to_the_image(tmp_path, monkeypatch):
    """首次取描述：读图一次，sidecar 落在图旁边，字段齐全。"""
    from minimax_h3_prompt.config import config
    from minimax_h3_prompt.director.frame_meta import sha256_file
    from minimax_h3_prompt.director.opening_still import (
        OPENING_STILL_KIND,
        get_opening_still_description,
    )

    session_dir = _session_dir(tmp_path)
    image = _image(session_dir)
    calls = _patch_describe(monkeypatch, lambda _n, _p: _DESCRIPTION)

    assert get_opening_still_description(session_dir) == _DESCRIPTION
    assert calls == [image], "首次取描述应当读图一次，且读的就是盘上那张开场首帧图"

    meta_path = _sidecar(image)
    assert meta_path.is_file(), "sidecar 必须与图同目录落 start.meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    assert meta["kind"] == OPENING_STILL_KIND == "opening_still", \
        "用途标记要能认出这是开场首帧的读图结果（与全片尾帧的 sidecar 区分开）"
    assert meta["description"] == _DESCRIPTION
    assert meta["vision_model"] == config.vision_model.model, \
        "视觉模型名必须从配置现取并落盘（事后可追溯是哪个模型读的）"
    assert meta["image_sha256"] == sha256_file(image), "sidecar 要带图指纹"
    assert meta["at"], "落盘时间不能空"


def test_matching_fingerprint_reuses_the_sidecar_without_reading_again(
        tmp_path, monkeypatch):
    """图没换：第二次取描述不重读图（续跑与改词不重复付费）。"""
    from minimax_h3_prompt.director.opening_still import (
        get_opening_still_description,
    )

    session_dir = _session_dir(tmp_path)
    _image(session_dir)
    calls = _patch_describe(monkeypatch, lambda _n, _p: _DESCRIPTION)
    assert get_opening_still_description(session_dir) == _DESCRIPTION
    assert len(calls) == 1, "首次取描述只读一次图"

    def _boom(_n, _p):
        raise AssertionError("图没换又读了图：续跑重复付费")

    monkeypatch.setattr(
        "minimax_h3_prompt.tools.reference_auditor.describe_image", _boom)
    assert get_opening_still_description(session_dir) == _DESCRIPTION


def test_changed_fingerprint_rereads_and_overwrites_the_sidecar(
        tmp_path, monkeypatch):
    """图换了：指纹变 → 重读并覆盖 sidecar（旧描述不许继续约束段词）。"""
    from minimax_h3_prompt.director.frame_meta import sha256_file
    from minimax_h3_prompt.director.opening_still import (
        get_opening_still_description,
    )

    session_dir = _session_dir(tmp_path)
    image = _image(session_dir)
    _patch_describe(monkeypatch, lambda _n, _p: _DESCRIPTION)
    assert get_opening_still_description(session_dir) == _DESCRIPTION

    image.write_bytes(b"second-version-of-the-opening-still")
    calls = _patch_describe(monkeypatch, lambda _n, _p: "新画面：男人已经站在院门里。")
    assert get_opening_still_description(session_dir) == "新画面：男人已经站在院门里。"
    assert len(calls) == 1, "指纹变了必须重读"

    meta = json.loads(_sidecar(image).read_text(encoding="utf-8"))
    assert meta["description"] == "新画面：男人已经站在院门里。"
    assert meta["image_sha256"] == sha256_file(image), "覆盖后的指纹要跟上新图"


def test_corrupt_sidecar_is_treated_as_missing_and_reread(tmp_path, monkeypatch):
    """坏 sidecar 按缺失处理：重读并修好，不把续跑拦死。"""
    from minimax_h3_prompt.director.opening_still import (
        get_opening_still_description,
    )

    session_dir = _session_dir(tmp_path)
    image = _image(session_dir)
    _sidecar(image).write_text("{not json", encoding="utf-8")
    calls = _patch_describe(monkeypatch, lambda _n, _p: _DESCRIPTION)

    assert get_opening_still_description(session_dir) == _DESCRIPTION
    assert len(calls) == 1, "坏 sidecar 要重读"
    meta = json.loads(_sidecar(image).read_text(encoding="utf-8"))
    assert meta["description"] == _DESCRIPTION


@pytest.mark.parametrize("missing", ["kind", "description", "image_sha256"])
def test_incomplete_sidecar_is_treated_as_missing_and_reread(
        tmp_path, monkeypatch, missing):
    """字段不齐的 sidecar 按「没有」处理并重读（任何一条命中判据不满足都不复用）。"""
    from minimax_h3_prompt.director.frame_meta import sha256_file
    from minimax_h3_prompt.director.opening_still import (
        OPENING_STILL_KIND,
        get_opening_still_description,
    )

    session_dir = _session_dir(tmp_path)
    image = _image(session_dir)
    complete = {
        "kind": OPENING_STILL_KIND,
        "description": "旧画面：男人背对镜头。",
        "vision_model": "some-vision-model",
        "image_sha256": sha256_file(image),
        "at": "2026-11-01T00:00:00+00:00",
    }
    del complete[missing]
    _sidecar(image).write_text(json.dumps(complete), encoding="utf-8")
    calls = _patch_describe(monkeypatch, lambda _n, _p: _DESCRIPTION)

    assert get_opening_still_description(session_dir) == _DESCRIPTION
    assert len(calls) == 1, f"缺 {missing} 的 sidecar 不许被当成命中"
    meta = json.loads(_sidecar(image).read_text(encoding="utf-8"))
    assert meta["description"] == _DESCRIPTION, "重读后要补齐字段"


def test_sidecar_from_another_purpose_is_not_reused(tmp_path, monkeypatch):
    """别的用途留下的 sidecar（kind 不符）不许被当成开场首帧的缓存。"""
    from minimax_h3_prompt.director.frame_meta import sha256_file
    from minimax_h3_prompt.director.opening_still import (
        get_opening_still_description,
    )

    session_dir = _session_dir(tmp_path)
    image = _image(session_dir)
    _sidecar(image).write_text(json.dumps({
        "kind": "film_end",
        "description": "全片尾帧的画面",
        "image_sha256": sha256_file(image),
    }), encoding="utf-8")
    calls = _patch_describe(monkeypatch, lambda _n, _p: _DESCRIPTION)

    assert get_opening_still_description(session_dir) == _DESCRIPTION
    assert len(calls) == 1, "用途标记不符就是不命中"


def test_visual_call_failure_raises_and_leaves_no_sidecar(tmp_path, monkeypatch):
    """视觉调用失败：抛错（带图路径与病因），且**不落** sidecar。"""
    from minimax_h3_prompt.director.opening_still import (
        get_opening_still_description,
    )

    session_dir = _session_dir(tmp_path)
    image = _image(session_dir)

    def fail(_path, instruction=None):
        raise RuntimeError("网关 502")

    monkeypatch.setattr(
        "minimax_h3_prompt.tools.reference_auditor.describe_image", fail)

    with pytest.raises(RuntimeError) as excinfo:
        get_opening_still_description(session_dir)
    message = str(excinfo.value)
    assert "开场首帧图读图失败" in message, "要说是哪张图的读图失败"
    assert str(image) in message, "错误里要点名是哪张图"
    assert "网关 502" in message, "原始错误不能吞掉"
    assert not _sidecar(image).exists(), "读图失败不许留下 sidecar"


# ---------------------------------------------------------------------------
# 事实块渲染：标题、描述正文、纪律句、「冲突时以画面为准」
# ---------------------------------------------------------------------------

def test_block_carries_the_head_the_description_the_discipline_and_the_image_wins_rule():
    """渲染出的块包含四件东西：块标题、描述正文、一致性纪律句、「以画面为准」。"""
    from minimax_h3_prompt.director.opening_still import (
        OPENING_STILL_BLOCK_HEAD,
        render_opening_still_block,
    )
    from minimax_h3_prompt.writing_disciplines import FRAME_CONSISTENCY_DISCIPLINE

    # 先断言真源非空再 ``in``，否则「清空真源」的变异会被 ``"" in text`` 恒真骗过。
    assert FRAME_CONSISTENCY_DISCIPLINE.strip()
    block = render_opening_still_block(_DESCRIPTION)

    assert OPENING_STILL_BLOCK_HEAD in block, "块标题必须出现"
    assert OPENING_STILL_BLOCK_HEAD.startswith("【") and \
        OPENING_STILL_BLOCK_HEAD.endswith("】"), "块标题要与全片尾帧块同形【…】"
    assert _DESCRIPTION in block, "描述正文必须出现"
    assert FRAME_CONSISTENCY_DISCIPLINE in block, "纪律句必须取自真源常量"
    assert "冲突时以画面为准" in block, "必须明写与分镜表/剧情概述冲突时以画面为准"
    assert "分镜表" in block and "剧情概述" in block, "冲突的一方要点名分镜表与剧情概述"


def test_block_head_is_on_its_own_line():
    """块标题独占一行（与 ``FILM_END_BLOCK_HEAD`` 同形），正文另起。"""
    from minimax_h3_prompt.director.opening_still import (
        OPENING_STILL_BLOCK_HEAD,
        render_opening_still_block,
    )

    lines = render_opening_still_block(_DESCRIPTION).splitlines()
    head_lines = [line for line in lines if line.strip() == OPENING_STILL_BLOCK_HEAD]
    assert len(head_lines) == 1, "块标题要独占一行出现且只出现一次"


def test_empty_description_produces_nothing():
    """空描述不产生块、不产生空行（统一走「空值不占位」的既有口径）。"""
    from minimax_h3_prompt.director.opening_still import render_opening_still_block

    assert render_opening_still_block("") == ""
    assert render_opening_still_block("   \n") == ""


def test_planning_and_writing_differ_only_in_what_to_do_with_it():
    """规划期与写段期共用同一份渲染：只差「拿它来做什么」那半句。"""
    from minimax_h3_prompt.director.opening_still import (
        OPENING_STILL_BLOCK_HEAD,
        render_opening_still_block,
    )
    from minimax_h3_prompt.writing_disciplines import FRAME_CONSISTENCY_DISCIPLINE

    planner = render_opening_still_block(_DESCRIPTION, for_planning=True)
    writer = render_opening_still_block(_DESCRIPTION)

    for block in (planner, writer):
        assert OPENING_STILL_BLOCK_HEAD in block
        assert _DESCRIPTION in block
        assert FRAME_CONSISTENCY_DISCIPLINE in block
        assert "冲突时以画面为准" in block
    assert "规划" in planner, "规划期要说清是拿来定本段边界与落定画面的"
    assert "写本段正文" in writer
    assert "规划" not in writer, "写段期的措辞不该自称在规划"

    # 「只差半句」要有可核的证据：两份渲染逐行只允许一行不同，且那一行是「拿它来
    # 做什么」的差别。
    planner_lines = planner.splitlines()
    writer_lines = writer.splitlines()
    assert len(planner_lines) == len(writer_lines), "两份渲染的行数必须一样"
    differing = [(a, b) for a, b in zip(planner_lines, writer_lines) if a != b]
    assert len(differing) == 1, f"除「拿它来做什么」外还有别的差异：{differing}"
    assert "规划" in differing[0][0] and "写本段正文" in differing[0][1]

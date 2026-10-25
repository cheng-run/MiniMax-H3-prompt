"""段内参考素材子集 + 重编号（issue #75 / ADR 0017「代价」）。

接缝（票面「Testing Decisions」）：纯函数——输入本段 Shot 原文、整片
``subject_definitions`` 与素材登记，输出「保留的 Picture 原号 → 段内新号」与
「保留的 Subject 原号」；产物是下一次组装/校验/接线卡的输入。

为什么必须有这一层：2026-10-22 的真实多参运行一次喂 24 张参考图，Qwen3-VL 文本
编码序列涨到约 5.4 万 token，本机只有朴素注意力 kernel 可用，``einsum`` 直接要
232 GiB（``torch.OutOfMemoryError``）。16GB 卡上「每段 ≤9 张」不是优化项，是能否
出片的前置条件（ADR 0017「代价」：每张约 1008 packed token，注意力超线性）。
"""
from __future__ import annotations

import pytest

from minimax_h3_prompt.segment_refs import (
    SegmentAssetOverflowError,
    compute_segment_scope,
    render_segment_injection_card,
    rewrite_picture_labels,
    scope_ref_sections,
)

# 整片定义：三个 Picture、四个 Subject（Subject 4 引用了 Subject 1，用来钉闭包）
_DEFS = """<Subject 1> is the young woman in <Picture 1>, with long dark hair and a red dress.
<Subject 2> is the brass key in <Picture 2>, with an old green patina.
<Subject 3> is the foyer bench in <Picture 3>, with dark lacquered wood.
<Subject 4> is the same woman as <Subject 1>, now wearing a grey coat.
<Picture 3> is the first frame of [Shot 3], showing the foyer at dusk.
"""

_REGISTRY = [
    (1, "红裙女孩", "人物·红裙女孩专属生图提示词"),
    (2, "青铜钥匙", "道具·青铜钥匙专属生图提示词"),
    (3, "门厅", "场景·门厅专属生图提示词"),
]


def _scope(shot_text: str, *, refs_per_segment: int = 9, paths=None):
    return compute_segment_scope(
        shot_text=shot_text, subject_definitions=_DEFS, registry=_REGISTRY,
        refs_per_segment=refs_per_segment, paths=paths)


def test_scope_keeps_only_pictures_used_by_this_segment_and_renumbers_from_one():
    """本段只引用第三张图 → 段内只保留它，并重编号为 <Picture 1>（AC1）。"""
    scope = _scope("[Shot 1] At 00:00.000, the camera rests on <Picture 3>.")
    assert [(a.new_picture, a.original_picture, a.name) for a in scope.pictures] == [
        (1, 3, "门厅")]
    assert scope.picture_map == {3: 1}
    # 这张图**有独立定义行**：它自己把画面讲清楚了，不再反向拉引用它的 Subject 3
    # （2026-10-22 实测反例：无条件反向拉取会把「全片公寓系统」Subject 拉进只拍卧室的
    # 段，连带拖入 4 张本段用不到的图）。反向拉取只服务「没有独立定义行、只内联在
    # Subject 里」的官方写法。
    assert scope.subject_numbers == ()


def test_scope_seeds_from_semantic_names_because_the_body_uses_names():
    """正文走**语义名**（CONTEXT「参考素材」指称体系）：Shot 写「红裙女孩」也要带上她的图。

    这是真实产物的形态：官方示例里 ``<Picture N>`` 只在 subject_definitions 被引，
    正文按语义名叙述。只收标签的话，一整段会一张图都不留——段内正文就成了无参照描述。
    """
    scope = _scope("[Shot 1] 红裙女孩 steps into the garden, alone.")
    assert [(a.new_picture, a.original_picture) for a in scope.pictures] == [(1, 1)]
    assert scope.subject_numbers == (1,)


def test_scope_prefers_the_longest_semantic_name_so_prefixes_do_not_leak():
    """语义名互相包含时按**最长优先**匹配（「素材1」是「素材10」的前缀）。"""
    registry = [(1, "素材1", ""), (10, "素材10", "")]
    defs = ("<Subject 1> is 素材1 in <Picture 1>.\n"
            "<Subject 10> is 素材10 in <Picture 10>.\n")
    scope = compute_segment_scope(
        shot_text="[Shot 1] 素材10 sits on the table.",
        subject_definitions=defs, registry=registry, refs_per_segment=9)
    assert [(a.new_picture, a.original_picture) for a in scope.pictures] == [(1, 10)]


def test_scope_keeps_everything_when_the_shot_text_mentions_nothing():
    """分不清本段用哪几张时**保留全部**（宁可超限报错，也不静默让段内无参照）。"""
    scope = _scope("[Shot 1] A wide shot of the empty street.")
    assert [a.original_picture for a in scope.pictures] == [1, 2, 3]
    # 保留全部＝图与 Subject 都保留：只留图会让段内定义块半缺，正文引用的实体没定义。
    assert scope.subject_numbers == (1, 2, 3, 4)


def test_scope_follows_subject_definitions_into_the_pictures_they_cite():
    """Shot 用 <Subject 2> → 它的定义引用了 <Picture 2>，于是 Picture 2 必须保留。"""
    scope = _scope("[Shot 1] <Subject 2> is turned over in her hand.")
    assert [(a.new_picture, a.original_picture) for a in scope.pictures] == [(1, 2)]
    assert scope.subject_numbers == (2,)


def test_scope_is_transitively_closed_over_subject_to_subject_references():
    """<Subject 4> 的定义引用 <Subject 1>：两者与 1 的图一起进入本段（闭包）。"""
    scope = _scope("[Shot 1] <Subject 4> steps into the foyer.")
    assert scope.subject_numbers == (1, 4)
    assert [(a.new_picture, a.original_picture) for a in scope.pictures] == [(1, 1)]


def test_scope_renumbers_in_original_picture_order():
    """多张图按**原号升序**映射到 1..k——顺序即接线顺序（新号 N → ref_image_{N-1}）。"""
    scope = _scope("[Shot 1] <Subject 3> stands beside <Subject 1>.")
    assert [(a.new_picture, a.original_picture) for a in scope.pictures] == [
        (1, 1), (2, 3)]
    assert scope.picture_map == {1: 1, 3: 2}


def test_scope_refuses_more_pictures_than_the_limit_and_names_them():
    """超过 refs_per_segment 时**报错并点名**，绝不静默截断（AC3）。"""
    ten = " ".join(f"<Picture {n}>" for n in range(1, 11))
    two_defs = "".join(f"<Subject {n}> is item in <Picture {n}>.\n" for n in range(1, 11))
    with pytest.raises(SegmentAssetOverflowError) as excinfo:
        compute_segment_scope(
            shot_text=ten, subject_definitions=two_defs,
            registry=[(n, f"素材{n}", "") for n in range(1, 11)], refs_per_segment=9)
    message = str(excinfo.value)
    assert "素材10" in message and "素材1" in message


def test_rewrite_picture_labels_maps_every_occurrence():
    """重编号是全文改写：同一原号出现几次都要换成同一个新号。"""
    text = "<Picture 7> is the anchor; later <Picture 7> is cited again."
    assert rewrite_picture_labels(text, {7: 2}) == (
        "<Picture 2> is the anchor; later <Picture 2> is cited again.")


def test_scoped_sections_keep_subject_numbers_but_filter_and_renumber_pictures():
    """段内定义块：Subject 原号不动、未用到的 Subject/Picture 行被裁掉、图号换新号。"""
    # 本段用 Subject 4（→ Subject 1 → Picture 1）与 Subject 3（→ Picture 3）：
    # 原号 [1, 3] 重编号为新 [1, 2]，summary 里的 <Picture 3> 必须跟着变成 <Picture 2>
    scope = _scope("[Shot 1] <Subject 4> steps into the foyer beside <Subject 3>.")
    assert scope.picture_map == {1: 1, 3: 2}
    sections = {
        "subject_definitions": _DEFS,
        "summary": "[reference generation] <Picture 3> anchors the foyer beat.",
        "retention_analysis": (
            "<Subject 1> (appears in [Shot 3]): fully_preserved - red dress retained.\n"
            "<Subject 2> (appears in [Shot 3]): fully_preserved - patina retained.\n"
            "<Subject 4> (appears in [Shot 3]): fully_preserved - grey coat retained.\n"
            "<Picture 3> (appears in [Shot 3]): fully_copy - foyer frame reused."),
        "detailed_description": "[Shot 1] untouched body.",
        "overall_soundscape": "Rain ticks on the awning.",
        "non_diegetic_music": "N/A",
    }
    scoped = scope_ref_sections(sections, scope)
    defs = scoped["subject_definitions"]
    assert "<Subject 4>" in defs and "<Subject 1>" in defs and "<Subject 3>" in defs
    assert "<Subject 2>" not in defs
    # 原 <Picture 3> 的独立定义行保留，但已重编号为新 <Picture 2>
    assert "<Picture 1>" in defs and "<Picture 2>" in defs and "<Picture 3>" not in defs
    assert scoped["summary"] == (
        "[reference generation] <Picture 2> anchors the foyer beat.")
    retention = scoped["retention_analysis"]
    assert "<Subject 4>" in retention and "<Subject 2>" not in retention
    assert "<Picture 2> (appears in [Shot 3])" in retention
    assert "<Picture 3>" not in retention


def test_injection_card_lists_new_original_name_and_file_in_one_table():
    """每段接线卡：新号 ↔ 原号 ↔ 素材名 ↔ 正本文件（AC4 的四列）。"""
    scope = compute_segment_scope(
        shot_text="<Subject 3> stands beside <Subject 1>.",
        subject_definitions=_DEFS, registry=_REGISTRY, refs_per_segment=9,
        paths={1: "assets/picture-01.png", 3: "assets/picture-03.png"})
    card = render_segment_injection_card(scope, position=2, total=5)
    assert "段 2/5" in card
    assert "| <Picture 1> | <Picture 1> | 红裙女孩 | assets/picture-01.png |" in card
    assert "| <Picture 2> | <Picture 3> | 门厅 | assets/picture-03.png |" in card


def test_official_nine_picture_cap_wins_over_a_larger_configured_value():
    """把 ``refs_per_segment`` 调到 9 以上也不放行（issue #86）：官方上限是硬契约。

    配置是「本机想喂几张」，官方上限是「一次请求收得下几张」——后者更硬。调大前者
    只会让卡与工作流对不上，不会让请求变合法，所以取两者较小值。
    """
    registry = [(n, f"素材{n}", "") for n in range(1, 11)]
    defs = "\n".join(f"<Picture {n}> is 素材{n}." for n in range(1, 11))
    shot = " ".join(f"<Picture {n}>" for n in range(1, 11))
    with pytest.raises(SegmentAssetOverflowError) as excinfo:
        compute_segment_scope(shot_text=shot, subject_definitions=defs,
                              registry=registry, refs_per_segment=12)
    message = str(excinfo.value)
    assert "9" in message and "<Picture 10>" in message


def test_card_lists_only_this_segments_pictures_and_names_the_unused_slots():
    """AC2（issue #86）：卡上只列本段 k 张（k ≤ 9），并写明未用槽留空/bypass。

    为什么必须写这一句：段内只有 k 张图，工作流却有 9 个图槽。不写清楚，用户面对
    7 个空槽会怀疑自己漏接了素材，或把上一段的图接到本段空槽上（静默串图）。
    """
    scope = _scope("The camera rests on <Picture 3>.")
    card = render_segment_injection_card(scope, position=1, total=4)
    rows = [line for line in card.splitlines() if line.startswith("| <Picture")]
    assert len(rows) == 1, "本段只有 1 张图，卡上不许出现别的素材行"
    assert "留空" in card and "bypass" in card
    # 未用槽点名到具体端口：ref_image_1 … ref_image_8（本段用掉 ref_image_0）
    assert "ref_image_1" in card and "ref_image_8" in card
    assert "ref_image_9" not in card, "9 槽工作流没有 ref_image_9"
    # 卡上点名登记工作流（AC1）：用户照卡进 ComfyUI 要能一眼找到那张；且不许再出现
    # 退役的 24 槽/V3 字样
    from minimax_h3_prompt.workflow_config import workflow_settings
    card_workflow = workflow_settings().ref_workflow_name
    assert card_workflow in card
    assert "V3" not in card and "24 槽" not in card


def test_card_says_so_when_every_slot_is_used():
    """用满 9 张时也要说清楚（「没有未用槽」≠「这一句可以省」）。"""
    registry = [(n, f"素材{n}", "") for n in range(1, 10)]
    defs = "\n".join(f"<Picture {n}> is 素材{n}." for n in range(1, 10))
    shot = " ".join(f"<Picture {n}>" for n in range(1, 10))
    scope = compute_segment_scope(shot_text=shot, subject_definitions=defs,
                                  registry=registry, refs_per_segment=9)
    card = render_segment_injection_card(scope, position=3, total=3)
    assert len([l for l in card.splitlines() if l.startswith("| <Picture")]) == 9
    assert "无未用槽" in card or "用满" in card


# ---------------------------------------------------------------------------
# 反向闭包的边界（issue #75 实测：段 1 被拉成 8 张，其中 4 张本段根本不用）
# ---------------------------------------------------------------------------

def test_a_picture_with_its_own_definition_does_not_pull_in_every_subject_that_cites_it():
    """独立 Picture 定义已经讲清楚这张图，就不该反向拉「引用它的全部 Subject」。

    反例（2026-10-22 实测）：段 1 只直接引用 <Picture 1>，反向闭包把「公寓材质与
    空间系统」Subject 6 拉进来（它引用 Picture 1/4/9/10/24），于是夜间卧室、卸妆棉、
    窗边起居区全被拖进本段——8 张里有 4 张本段根本拍不到。反向拉取只该服务
    「图没有独立定义行、只内联在 Subject 里」的官方写法。
    """
    defs = (
        "<Subject 1> is the room system: anchored by <Picture 1> and <Picture 9>.\n"
        "<Subject 2> is the woman in <Picture 2>.\n"
        "<Picture 1> is the bedroom anchor for Shot 01.\n"
        "<Picture 2> is the identity anchor.\n"
        "<Picture 9> is the nighttime bedroom.\n"
    )
    scope = compute_segment_scope(
        shot_text="Wide shot of the bedroom (<Picture 1>).",
        subject_definitions=defs,
        registry=[(1, "卧室", ""), (2, "她", ""), (9, "夜间卧室", "")],
    )
    assert [p.original_picture for p in scope.pictures] == [1]


def test_picture_definition_references_to_other_pictures_are_followed():
    """一张图自己的定义里引用了别的图（夜灯/充电器），那些图也必须进本段子集。

    否则段内会留下 `<Picture N>` 悬空引用：模型能看到标签、却拿不到对应参考图，
    校验器也会报 PICTURE_UNDEFINED（2026-10-22 实测）。
    """
    defs = (
        "<Picture 1> is the bedroom anchor, with the night-light shown in <Picture 3>.\n"
        "<Picture 2> is the identity anchor.\n"
        "<Picture 3> is the warm bedside night-light.\n"
    )
    scope = compute_segment_scope(
        shot_text="Wide shot of the bedroom (<Picture 1>).",
        subject_definitions=defs,
        registry=[(1, "卧室", ""), (2, "她", ""), (3, "夜灯", "")],
    )
    assert [p.original_picture for p in scope.pictures] == [1, 3]

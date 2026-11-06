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
    SegmentAssetScope,
    compute_asset_closure,
    compute_segment_scope,
    render_segment_injection_card,
    rewrite_anchor_picture_labels,
    rewrite_picture_labels,
    scope_ref_sections,
    scope_retention_to_shots,
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


def test_retention_keeps_only_the_shots_of_this_segment():
    """``Shot N, ...`` 形态：别的段的条目整行删，不带镜头号的全局说明原样留。

    样本取 2026-10-26 真实产物的形态——段 2 的 retention 里还列着段 1 的
    ``backward glance``，模型据此重演了段 1 的构图（首帧 MAD 27.68）。
    """
    text = (
        "Shot 1, 00:00-00:04: <Subject 2> attribute_transfer into a backward glance.\n"
        "Shot 2, 00:04-00:10: <Subject 2> and <Subject 3> attribute_transfer into a half-step.\n"
        "Shot 3, 00:10-00:16: <Subject 3> attribute_transfer into a lowered gaze.\n"
        "<Subject 6> stays fully_preserved in every shot so the look never slips.\n"
        "No audio reference labels exist in this batch."
    )
    scoped = scope_retention_to_shots(text, [2])
    assert "Shot 2," in scoped and "half-step" in scoped
    assert "Shot 1," not in scoped and "backward glance" not in scoped
    assert "Shot 3," not in scoped
    # 全局说明（无镜头号）不是「别的段的内容」，删掉会让本段失去全部素材约束
    assert "stays fully_preserved in every shot" in scoped
    assert "No audio reference labels exist" in scoped


def test_retention_trims_shot_list_inside_appears_in_entries():
    """官方形态一行跨多镜：裁的是括号里的镜头列表，不是整行删。"""
    text = (
        "<Subject 1> (appears in [Shot 1], [Shot 2], [Shot 3]): fully_preserved - red dress.\n"
        "<Subject 2> (appears in [Shot 1]): fully_preserved - patina."
    )
    scoped = scope_retention_to_shots(text, [2, 3])
    assert "<Subject 1> (appears in [Shot 2], [Shot 3])" in scoped
    # 只出现在别的段的条目整行消失（它在本段没有留存关系可讲）
    assert "<Subject 2>" not in scoped


def test_retention_returns_original_when_no_entry_is_recognised():
    """格式认不出就整块清空，比重复更坏——宁可原样交回给校验器报。"""
    text = "The red dress and the patina both survive the scene unscathed."
    assert scope_retention_to_shots(text, [2]) == text


def test_retention_drops_fragments_about_subjects_missing_from_this_segment():
    """素材子集裁掉的角色，留存分析里也不许再点名（2026-10-26 段 4 实测）。

    段 4 只剩 A（``<Subject 3>``→段内 ``<Subject 3>``），缩到 Shot 4 的那条却仍写着
    ``<Subject 1>``/``<Subject 4>``/``<Subject 5>``——本段定义块里没有它们，就是悬空
    标签；校验器查不到这一层，0 条问题静默放过。
    """
    text = (
        "Shot 4, 00:16-00:22: <Subject 1> partially_preserved as the interior is "
        "pressed darker, <Subject 4> and <Subject 5> fully_preserved and motionless, "
        "<Subject 3> attribute_transfer into a near-silent breath, a lowered look "
        "and a lifted gaze, <Subject 2> staying off screen, <Subject 6> fully_preserved.")
    scoped = scope_retention_to_shots(text, [4], defined_subjects=[3])
    assert "<Subject 3>" in scoped and "near-silent breath" in scoped
    assert "a lowered look and a lifted gaze" in scoped  # 无标签的延续跟着前一片段
    for gone in ("<Subject 1>", "<Subject 2>", "<Subject 4>", "<Subject 5>", "<Subject 6>"):
        assert gone not in scoped


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


# ---------------------------------------------------------------------------
# 闭包本身（#181）：规划期与写段期必须调同一份实现，且每条边带得出溯源
# ---------------------------------------------------------------------------

def test_closure_reports_the_definition_that_pulled_each_node_in():
    """种子 Subject 2 → 它的定义点了 <Picture 2>：边要指出「哪条定义、哪句话」。"""
    closure = compute_asset_closure(
        seed_subjects=[2], subject_definitions=_DEFS, registry=_REGISTRY)
    assert closure.pictures == (2,)
    assert closure.subjects == (2,)
    (edge,) = closure.edges_into("picture", 2)
    assert edge.definition == ("subject", 2)
    assert edge.evidence == (
        "<Subject 2> is the brass key in <Picture 2>, with an old green patina.")
    assert edge.anchor is False


def test_closure_marks_edges_that_hit_the_consistency_anchor_sentence():
    """一致性锚是白名单句式（父 spec #180 用户故事 9）：默认算出镜，命中才标锚。

    三种句式各来一条；``comes from <Picture N>`` 是**身份来源**，不是一致性锚——
    把它误标成锚，降级时会把人物的身份来源图撤掉（用户故事 9 的原话）。
    """
    defs = (
        "<Subject 21> is the entrance hall that must agree with <Subject 22>.\n"
        "<Subject 22> is the front yard whose layout comes from <Picture 22>.\n"
        "<Subject 23> is the corridor whose floor stays consistent with <Picture 24>.\n"
        "<Subject 24> is the doorway whose colour 与 <Picture 25> 一致。\n"
    )
    closure = compute_asset_closure(
        seed_subjects=[21, 23, 24], subject_definitions=defs)

    def _anchored(kind: str, number: int) -> bool:
        # 一个节点可能有多条边进来（Subject 22 自己的定义也点了它），锚是**这条边**
        # 的属性：只要有一条是锚，这个节点就是靠锚勾进来的。
        return any(edge.anchor for edge in closure.edges_into(kind, number))

    assert _anchored("subject", 22) is True
    assert _anchored("picture", 24) is True
    assert _anchored("picture", 25) is True
    assert _anchored("picture", 22) is False


def test_closure_records_the_reverse_edge_for_a_picture_without_its_own_definition():
    """图只内联在 Subject 定义里（官方写法）：反向拉引用它的 Subject，边指向那条定义。

    ``via`` 是触发它的图、``definition`` 是点了它的 Subject 定义——两者不同正是反向
    边的特征（正向边里两者相同）。少了这一支，段内 ``<Picture N>`` 会悬空。
    """
    defs = (
        "<Subject 1> is the young woman whose appearance comes from <Picture 1>.\n"
        "<Subject 2> is the brass key in <Picture 2>.\n"
    )
    closure = compute_asset_closure(
        seed_pictures=[1], subject_definitions=defs)
    assert closure.pictures == (1,)
    assert closure.subjects == (1,)
    (edge,) = closure.edges_into("subject", 1)
    assert edge.via == ("picture", 1)
    assert edge.definition == ("subject", 1)
    assert edge.evidence == (
        "<Subject 1> is the young woman whose appearance comes from <Picture 1>.")
    assert edge.anchor is False


def test_closure_falls_back_to_the_whole_registry_when_there_are_no_seeds():
    """种子为空＝分不清本段要用什么：保留全部登记图与全部 Subject（规则 ③）。

    宁可超限报错也不静默让段内无参照；只留图不留 Subject 会让段内定义块半缺。
    """
    closure = compute_asset_closure(
        subject_definitions=_DEFS, registry=_REGISTRY)
    assert closure.pictures == (1, 2, 3)
    assert closure.subjects == (1, 2, 3, 4)


def test_closure_reports_only_the_sentence_that_cites_the_label():
    """一条定义可以有好几句：溯源要的是点了标签的**那一句**，不是整条。

    整条交出去，锚句式就会被别的句子带进来——「这句到底算不算锚」将无从分辨。
    """
    defs = (
        "<Subject 5> is the lobby of the building. Its stone floor pattern must agree "
        "with <Picture 7> for continuity.\n"
    )
    closure = compute_asset_closure(
        seed_subjects=[5], subject_definitions=defs)
    (edge,) = closure.edges_into("picture", 7)
    assert edge.evidence == (
        "Its stone floor pattern must agree with <Picture 7> for continuity.")
    assert edge.anchor is True


# ---------------------------------------------------------------------------
# 锚降级（#183）：只被一致性锚句式勾进来的图不占槽
#
# 真实现场（2026-11-06，父 spec #180）：第 5 段正文点名 9 张，第 10 张 <Picture 22>
# 是 Subject 21 定义里「must agree with <Subject 22>」把 Subject 22（前院）勾进来、
# 再沿 Subject 22 自己的定义落到 Picture 22 的——镜头全程在室内，前院根本没出镜。
# 这张图不占槽，但 Subject 22 的定义必须留着（否则段词里 <Subject 22> 悬空）。
# ---------------------------------------------------------------------------

_ANCHOR_REGISTRY = (
    *[(n, f"素材{n}", "") for n in range(1, 10)],
    (22, "前院与车道", ""),
)


def _anchor_defs(count: int = 9, extra: str = "") -> str:
    return "\n".join([
        *(f"<Subject {n}> is 素材{n} in <Picture {n}>." for n in range(1, count + 1)),
        "<Subject 21> is the entrance hall that must agree with <Subject 22>.",
        "<Subject 22> is the front yard whose layout comes from <Picture 22>.",
        extra,
    ]).strip()


def _anchor_shot(count: int = 9, extra: str = "") -> str:
    return (
        "[Shot 5] At 00:25.917, "
        + " ".join(f"<Picture {n}>" for n in range(1, count + 1))
        + " stay in frame while <Subject 21> holds still." + extra)


def _anchor_scope(*, defs: str | None = None, shot: str | None = None,
                  refs_per_segment: int = 9):
    return compute_segment_scope(
        shot_text=shot or _anchor_shot(), subject_definitions=defs or _anchor_defs(),
        registry=_ANCHOR_REGISTRY, refs_per_segment=refs_per_segment)


def test_scope_demotes_a_picture_pulled_in_only_by_an_anchor_sentence():
    """AC1：闭包 10 张 → 出镜 9 张且不含 <Picture 22>，锚记 via 溯源。

    ``refs_per_segment=9`` 本身就是判据：不降级就会在这里抛超限。
    """
    scope = _anchor_scope()
    assert [item.original_picture for item in scope.pictures] == list(range(1, 10))
    assert 22 not in scope.picture_map
    assert [(a.original_picture, a.name, a.via) for a in scope.anchors] == [
        (22, "前院与车道", "Subject 21")]
    assert scope.anchors[0].evidence == (
        "<Subject 21> is the entrance hall that must agree with <Subject 22>.")
    # 锚的 Subject 定义条目仍在本段范围内（否则段词里 <Subject 22> 悬空）
    assert 21 in scope.subject_numbers and 22 in scope.subject_numbers


def test_scope_keeps_a_picture_that_also_has_a_depicted_source_edge():
    """AC3：同一张图另有非锚边（``comes from``）→ 仍算出镜，不被降级。

    这是「身份来源图」的形态：把它误降级会让人物/场景相似度悄悄变差。
    """
    defs = _anchor_defs(8, "<Subject 23> is the doorway whose colour comes from "
                           "<Picture 22>.")
    shot = _anchor_shot(8, " <Subject 23> frames the doorway.")
    scope = _anchor_scope(defs=defs, shot=shot)
    assert [item.original_picture for item in scope.pictures] == [*range(1, 9), 22]
    assert scope.anchors == ()


def test_scope_never_demotes_a_picture_the_segment_itself_names():
    """种子永远算出镜：正文点了 <Picture 22> 就是本段要拍它。"""
    shot = _anchor_shot(8, " The locked gate of <Picture 22> stays shut.")
    scope = _anchor_scope(shot=shot)
    assert 22 in scope.picture_map
    assert scope.anchors == ()


def test_scoped_sections_keep_the_anchor_subject_definition_without_the_label():
    """AC2：锚的 Subject 定义留在段内定义块，但不再含指向被撤图的 <Picture N>。

    只改那半句，同一条定义里的其它字句原样——这是确定性文本改写，不交给模型。
    """
    scope = _anchor_scope()
    sections = {
        "subject_definitions": _anchor_defs(),
        "retention_analysis": (
            "<Subject 22> (appears in [Shot 5]): fully_preserved - the yard behind "
            "matches <Picture 22>.\n"
            "<Picture 22>: not_in_frame."),
    }
    scoped = scope_ref_sections(sections, scope)
    defs = scoped["subject_definitions"]
    assert "<Subject 22>" in defs, "锚的 Subject 定义必须保留：删掉会让段词里 <Subject 22> 悬空"
    assert "<Picture 22>" not in defs, "被撤下的图不能在定义块里留下标签"
    assert "<Subject 21> is the entrance hall that must agree with <Subject 22>." in defs, \
        "锚边自己那句一字不改（它没有指向被撤图）"
    assert "<Subject 22> is the front yard whose layout comes from" in defs, \
        "只改写「来源图」半句，其余字句原样"
    assert "<Picture 9>" in defs, "其余条目照常保留并重编号"
    retention = scoped["retention_analysis"]
    assert "<Subject 22>" in retention and "<Picture 22>" not in retention


def test_anchor_records_survive_the_plan_json_roundtrip():
    """锚跟着 plan.json 走：续跑照同一份改写，不重算也不丢锚。

    旧 plan.json 没有 ``anchors`` 键（本次改动之前落的盘）：照旧读得回来，锚为空。
    """
    scope = _anchor_scope()
    restored = SegmentAssetScope.from_dict(scope.to_dict())
    assert restored.anchors == scope.anchors
    old = SegmentAssetScope.from_dict({
        "pictures": [{"new": 1, "original": 3, "name": "门厅"}], "subjects": [3]})
    assert old.anchors == ()


def test_rewrite_anchor_picture_labels_keeps_everything_but_the_tag():
    """确定性改写：只换标签本身，句子其余部分一字不动；不命中的号原样保留。"""
    text = "<Subject 22> is the front yard whose layout comes from <Picture 22>."
    assert rewrite_anchor_picture_labels(text, {22}) == (
        "<Subject 22> is the front yard whose layout comes from "
        "the corresponding reference material.")
    assert rewrite_anchor_picture_labels(text, {7}) == text
    zh = "<Subject 24> is the doorway whose colour 与 <Picture 25> 一致。"
    rewritten = rewrite_anchor_picture_labels(zh, {25})
    assert "<Picture" not in rewritten and "与" in rewritten and "一致" in rewritten

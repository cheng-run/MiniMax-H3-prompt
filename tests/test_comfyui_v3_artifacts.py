"""本机件守卫：24 图槽节点包与 V3 工作流在仓库内的副本必须自洽。

**为什么需要它**：节点包与 V3 工作流是**仓库外**的产物（写在 ComfyUI 目录），
换机不可复现；本仓 ``docs/comfyui/`` 留一份源码副本、工作流 JSON 与生成脚本。
这些文件在运行时没有任何消费者——坏掉（槽位少一个、连线指向不存在的 link、
参数与 V2 漂移）不会被其它测试发现，只会在用户打开 ComfyUI 时报错或静默少喂图
（issue #69、ADR 0017）。本文件把「换机可复现」变成会响的判据。

**判据只认结构事实**：24 个 LoadImage 是否都真的经过总线到达编码节点、每条
link 的两端是否都存在、H3 节点的参数是否仍等于 V2——不数「文件有几行」，
也不断言实现细节（例如节点内部怎么调用上游）。
"""
from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
DOCS = ROOT / "docs" / "comfyui"
NODE_DIR = DOCS / "MiniMaxH3-Refs24"
INIT_PATH = NODE_DIR / "__init__.py"
NODES_PATH = NODE_DIR / "nodes_refs24.py"
WORKFLOW_PATH = DOCS / "59-(多Lora改)MiniMax-H3 ref2va 多参生视频 V3（24图槽）.json"
GENERATOR_PATH = DOCS / "generate_workflow_v3.py"
README_PATH = DOCS / "README.md"

# 本机真实路径只用于「副本 vs 安装件」交叉核对；别处不依赖它（换机时 skip）。
INSTALLED_NODE_DIR = Path("D:/Comfyui/ComfyUI/custom_nodes/MiniMaxH3-Refs24")
V2_WORKFLOW = Path(
    "D:/Comfyui/ComfyUI/user/default/workflows/001-JZL基础工作流/15-MiniMax-H3/"
    "59-(多Lora改)MiniMax-H3 ref2va 多参生视频 V2 .json"
)

IMAGE_SLOTS = tuple(f"ref_image_{i}" for i in range(24))
VIDEO_SLOTS = tuple(f"ref_video_{i}" for i in range(3))
VIDEO_AUDIO_SLOTS = tuple(f"ref_video_audio_{i}" for i in range(3))
AUDIO_SLOTS = tuple(f"ref_audio_{i}" for i in range(3))
ALL_SLOTS = IMAGE_SLOTS + VIDEO_SLOTS + VIDEO_AUDIO_SLOTS + AUDIO_SLOTS

BUS_TYPE = "MINIMAX_H3_REFS24_BUS"
BUS_OUT_TYPE = "MiniMaxH3Refs24BusOut"
BUS_IN_TYPE = "MiniMaxH3Refs24BusIn"
ENCODE_TYPE = "MiniMaxH3Refs24ReferenceToVideo"


def _load_package():
    """按 ComfyUI 的加载方式 exec 仓库内副本（不启动服务、不 import torch）。"""
    name = "refs24_docs_under_test"
    spec = importlib.util.spec_from_file_location(
        name, INIT_PATH, submodule_search_locations=[str(NODE_DIR)]
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(name, None)
    return module


def _workflow() -> dict:
    return json.loads(WORKFLOW_PATH.read_text(encoding="utf-8"))


def _subgraph(workflow: dict, prefix: str) -> dict:
    matches = [sg for sg in workflow["definitions"]["subgraphs"] if sg["name"].startswith(prefix)]
    assert len(matches) == 1, f"子图 {prefix} 应恰好有一个，实际 {len(matches)}"
    return matches[0]


def _node(graph: dict, number: int) -> dict:
    matches = [n for n in graph["nodes"] if n["id"] == number]
    assert len(matches) == 1, f"节点 {number} 应恰好有一个，实际 {len(matches)}"
    return matches[0]


def _links(graph: dict) -> dict[int, dict]:
    """把兼容数组/字典两种序列化的 links 统一成 {id: 记录}。"""
    result: dict[int, dict] = {}
    for raw in graph["links"]:
        if isinstance(raw, list):
            link = {
                "id": raw[0],
                "origin_id": raw[1],
                "origin_slot": raw[2],
                "target_id": raw[3],
                "target_slot": raw[4],
                "type": raw[5],
            }
        else:
            link = raw
        assert link["id"] not in result, f"link {link['id']} 重复"
        result[link["id"]] = link
    return result


def _assert_graph_links_consistent(graph: dict, label: str) -> None:
    """每条 link 必须恰好挂在一个输出与一个输入上，且槽位下标存在。"""
    links = _links(graph)
    nodes = {n["id"]: n for n in graph["nodes"]}

    for link in links.values():
        origin_id, target_id = link["origin_id"], link["target_id"]
        if origin_id != -10:  # -10 是子图输入边界
            origin = nodes.get(origin_id)
            assert origin is not None, f"{label}: link {link['id']} 的起点节点 {origin_id} 不存在"
            outputs = origin.get("outputs") or []
            assert link["origin_slot"] < len(outputs), (
                f"{label}: link {link['id']} 起点槽位 {link['origin_slot']} 越界"
                f"（{origin['type']} 只有 {len(outputs)} 个输出）"
            )
            assert link["id"] in (outputs[link["origin_slot"]].get("links") or []), (
                f"{label}: {origin['type']}#{origin_id} 输出槽 {link['origin_slot']}"
                f" 没有登记 link {link['id']}"
            )
        if target_id != -20:  # -20 是子图输出边界
            target = nodes.get(target_id)
            assert target is not None, f"{label}: link {link['id']} 的终点节点 {target_id} 不存在"
            inputs = target.get("inputs") or []
            assert link["target_slot"] < len(inputs), (
                f"{label}: link {link['id']} 终点槽位 {link['target_slot']} 越界"
                f"（{target['type']} 只有 {len(inputs)} 个输入）"
            )
            assert inputs[link["target_slot"]].get("link") == link["id"], (
                f"{label}: {target['type']}#{target_id} 输入槽 {link['target_slot']}"
                f" 登记的不是 link {link['id']}"
            )

    # 反向：节点上登记的每个 link 都必须存在于 links 表。
    for node in graph["nodes"]:
        for slot, item in enumerate(node.get("inputs") or []):
            link_id = item.get("link")
            if link_id is None:
                continue
            assert link_id in links, (
                f"{label}: {node['type']}#{node['id']} 输入槽 {slot} 指向不存在的 link {link_id}"
            )
            assert links[link_id]["target_id"] == node["id"], (
                f"{label}: link {link_id} 的终点不是 {node['id']}"
            )
        for slot, item in enumerate(node.get("outputs") or []):
            for link_id in item.get("links") or []:
                assert link_id in links, (
                    f"{label}: {node['type']}#{node['id']} 输出槽 {slot}"
                    f" 指向不存在的 link {link_id}"
                )
                assert links[link_id]["origin_id"] == node["id"], (
                    f"{label}: link {link_id} 的起点不是 {node['id']}"
                )


# ── 节点包：注册与槽位 ──────────────────────────────────────────────


def test_node_package_registers_three_nodes() -> None:
    module = _load_package()
    assert set(module.NODE_CLASS_MAPPINGS) == {BUS_OUT_TYPE, BUS_IN_TYPE, ENCODE_TYPE}
    assert set(module.NODE_DISPLAY_NAME_MAPPINGS) == set(module.NODE_CLASS_MAPPINGS)


def test_bus_slots_are_exactly_twenty_four_images() -> None:
    module = _load_package()
    bus_out = module.NODE_CLASS_MAPPINGS[BUS_OUT_TYPE]
    bus_in = module.NODE_CLASS_MAPPINGS[BUS_IN_TYPE]

    inputs = bus_out.INPUT_TYPES()
    assert tuple(inputs["optional"]) == ALL_SLOTS
    assert all(inputs["optional"][name] == ("IMAGE",) for name in IMAGE_SLOTS)
    assert all(inputs["optional"][name] == ("AUDIO",) for name in VIDEO_AUDIO_SLOTS + AUDIO_SLOTS)

    assert tuple(bus_in.RETURN_NAMES) == ALL_SLOTS
    assert bus_in.RETURN_TYPES[0] == "IMAGE"
    assert bus_in.RETURN_TYPES[-1] == "AUDIO"


def test_encode_node_has_twenty_four_image_inputs() -> None:
    module = _load_package()
    node = module.NODE_CLASS_MAPPINGS[ENCODE_TYPE]
    inputs = node.INPUT_TYPES()
    required = inputs["required"]
    optional = inputs["optional"]

    assert tuple(optional) == ALL_SLOTS
    for name in ("clip", "vae", "audio_vae", "prompt", "width", "height", "length"):
        assert name in required
    assert required["ref_image_size"][0] == ["match", "max"]
    assert node.RETURN_TYPES == ("CONDITIONING", "LATENT")


def test_encode_node_reuses_upstream_instead_of_copying() -> None:
    """编码实现只许借用上游，不许抄一份——抄了就会随上游更新静默漂移。"""
    source = NODES_PATH.read_text(encoding="utf-8")
    assert '"JZL_MiniMaxH3ReferenceToVideo"' in source, "必须调用上游注册的公开节点类"
    for copied in ("vae.encode(", "clip.tokenize(", "_encode_ref_audio", "conditioning_set_values"):
        assert copied not in source, f"疑似复制了上游编码实现：{copied}"
    assert 'BUS_TYPE = "MINIMAX_H3_REFS24_BUS"' in source


def _load_slot_module():
    """直接加载节点实现（不经包入口），只为取三个槽位常量。

    目录名带连字符，不能走普通 import；``spec_from_file_location`` 指向
    ``nodes_refs24.py`` 即可——该文件模块级没有相对导入。
    """
    name = "refs24_slots_under_test"
    spec = importlib.util.spec_from_file_location(name, NODES_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(name, None)
    return module


def test_slot_counts_agree_across_config_workflow_and_node_package() -> None:
    """V3 工件内部一致：节点包常量 == V3 工作流端口数（换机可复现那半判据）。

    2026-10-25（issue #86 / T2）：登记工作流已回到 **V2 的 9 图槽**，官方单次请求上限
    也是 9 图——``DEFAULT_SLOT_COUNTS`` 因此不再等于 V3 的 24 槽，原来把「配置默认值」
    和 V3 绑在一起的那条断言随之作废（它不是被放宽，而是绑错了对象：配置现在跟 V2
    走）。V3 这套工件本身随 T6（#90）整体退役，在那之前仍要求它自洽——否则换机复现
    时坏掉不会被任何测试发现。
    """
    from minimax_h3_prompt.asset_ledger import KIND_AUDIO, KIND_PICTURE, KIND_VIDEO

    slots = _load_slot_module()
    node_counts = {
        KIND_PICTURE: slots.REF_IMAGE_SLOTS,
        KIND_VIDEO: slots.REF_VIDEO_SLOTS,
        KIND_AUDIO: slots.REF_AUDIO_SLOTS,
    }

    workflow = _workflow()
    ref_names = [item["name"] for item in
                 next(n for n in _subgraph(workflow, "②")["nodes"] if n["type"] == BUS_OUT_TYPE)["inputs"]]
    param_names = [item["name"] for item in
                   next(n for n in _subgraph(workflow, "③")["nodes"] if n["type"] == ENCODE_TYPE)["inputs"]]

    def count(names: list[str], prefix: str) -> int:
        # 精确匹配 ``prefix_<数字>``：``ref_video_audio_N`` 不能被算进 ``ref_video``
        return sum(1 for name in names if re.fullmatch(rf"{prefix}_\d+", name))

    for kind, prefix in ((KIND_PICTURE, "ref_image"),
                         (KIND_VIDEO, "ref_video"),
                         (KIND_AUDIO, "ref_audio")):
        assert count(ref_names, prefix) == node_counts[kind], (
            f"V3 参考资产子图 {prefix}_* 端口数 {count(ref_names, prefix)} ≠ 节点包 {node_counts[kind]}")
        assert count(param_names, prefix) == node_counts[kind], (
            f"V3 编码节点 {prefix}_* 端口数 {count(param_names, prefix)} ≠ 节点包 {node_counts[kind]}")


# ── 工作流 V3：24 槽真的连到了编码节点 ─────────────────────────────


def test_workflow_v3_has_24_loadimage_into_bus() -> None:
    workflow = _workflow()
    ref_graph = _subgraph(workflow, "②")
    bus_out = next(n for n in ref_graph["nodes"] if n["type"] == BUS_OUT_TYPE)
    links = _links(ref_graph)

    inputs = bus_out["inputs"]
    assert [item["name"] for item in inputs] == list(ALL_SLOTS)
    image_links = [inputs[i]["link"] for i in range(len(IMAGE_SLOTS))]
    assert all(link is not None for link in image_links), "24 个图槽必须全部连线"

    # 每条图槽连线 → Reroute → LoadImage；24 个来源必须互不相同。
    load_images: list[int] = []
    for link_id in image_links:
        link = links[link_id]
        reroute = next(n for n in ref_graph["nodes"] if n["id"] == link["origin_id"])
        assert reroute["type"] == "Reroute"
        upstream_link = reroute["inputs"][0]["link"]
        assert upstream_link is not None
        source = next(n for n in ref_graph["nodes"] if n["id"] == links[upstream_link]["origin_id"])
        assert source["type"] == "LoadImage"
        load_images.append(source["id"])
    assert len(set(load_images)) == 24, "24 条图槽必须来自 24 个不同的 LoadImage"

    all_load_images = [n for n in ref_graph["nodes"] if n["type"] == "LoadImage"]
    assert len(all_load_images) == 24, f"参考资产子图应有 24 个 LoadImage，实际 {len(all_load_images)}"


def test_workflow_v3_bus_reaches_encode_node_in_order() -> None:
    workflow = _workflow()
    param_graph = _subgraph(workflow, "③")
    bus_in = next(n for n in param_graph["nodes"] if n["type"] == BUS_IN_TYPE)
    encoder = next(n for n in param_graph["nodes"] if n["type"] == ENCODE_TYPE)
    links = _links(param_graph)

    assert [item["name"] for item in bus_in["outputs"]] == list(ALL_SLOTS)
    encoder_images = [item for item in encoder["inputs"] if item["name"] in IMAGE_SLOTS]
    assert len(encoder_images) == 24
    for index, item in enumerate(encoder_images):
        link = links[item["link"]]
        assert link["origin_id"] == bus_in["id"], "图槽必须直接来自新总线解包节点"
        assert link["origin_slot"] == index, "第 N 个图槽必须接总线第 N 个输出"


def test_workflow_v3_links_are_self_consistent() -> None:
    workflow = _workflow()
    _assert_graph_links_consistent(workflow, "顶层")
    for graph in workflow["definitions"]["subgraphs"]:
        _assert_graph_links_consistent(graph, graph["name"])


def test_workflow_v3_bus_type_is_unique_to_this_package() -> None:
    """不能复用上游类型名：18 槽总线与 24 槽总线互插会静默少喂图。"""
    workflow = _workflow()
    ref_graph = _subgraph(workflow, "②")
    param_graph = _subgraph(workflow, "③")
    bus_out = next(n for n in ref_graph["nodes"] if n["type"] == BUS_OUT_TYPE)
    bus_in = next(n for n in param_graph["nodes"] if n["type"] == BUS_IN_TYPE)
    assert bus_out["outputs"][0]["type"] == BUS_TYPE
    assert bus_in["inputs"][0]["type"] == BUS_TYPE
    assert BUS_TYPE != "JZL_REF2VA_BUS"

    top = _workflow()
    bus_links = [raw for raw in top["links"] if raw[5] == BUS_TYPE]
    assert len(bus_links) == 2, "顶层应有两段总线 link（子图②→Reroute、Reroute→子图③）"
    # Reroute 的中间段在 ComfyUI 里序列化为 '*',但两端的端口类型也必须是新总线类型，
    # 否则用户可以把旧 18 槽总线接到新线路上（静默少喂图）。
    for node in top["nodes"]:
        if node["id"] in (2586, 2587):
            assert node["type"] == "Reroute"
            for item in (node.get("inputs") or []) + (node.get("outputs") or []):
                assert item["type"] in (BUS_TYPE, "*")
    assert "JZL_REF2VA_BUS" not in json.dumps(top, ensure_ascii=False), (
        "V3 里不应再残留上游总线类型名"
    )


def test_workflow_v3_keeps_v2_generation_parameters() -> None:
    workflow = _workflow()
    param_graph = _subgraph(workflow, "③")
    encoder = next(n for n in param_graph["nodes"] if n["type"] == ENCODE_TYPE)

    assert encoder["widgets_values"] == ["", 1344, 768, 124, "match", 1]
    assert encoder["widgets_values_named"] == {
        "prompt": "",
        "width": 1344,
        "height": 768,
        "length": 124,
        "ref_image_size": "match",
        "ref_scale": 1,
    }

    instance = next(n for n in workflow["nodes"] if n["id"] == 2580)
    assert instance["widgets_values"] == [1344, 768, 124, 1, ""]

    # 模型、LoRA、采样、尺寸这些与图槽无关的参数必须原样保留（已知good 字面量）。
    model_graph = _subgraph(workflow, "①")
    assert _node(model_graph, 129)["widgets_values"] == [
        "MiniMax-H3\\minimax_h3_ref2va_pruned_int8_convrot.safetensors",
        "default",
    ]
    assert _node(model_graph, 181)["widgets_values"] == [
        "MiniMax-H3\\minimax_h3_fl2v_turbo_4step_v1.1_768p_comfyui_bf16.safetensors",
        1,
    ]
    assert _node(model_graph, 2606)["widgets_values"] == [
        "MiniMax-H3\\动态修复\\Motion_Repair_V2.safetensors",
        0.3,
    ]
    assert _node(workflow, 2601)["widgets_values"] == ["simple", 8, 1]
    assert _node(workflow, 2602)["widgets_values"] == ["euler"]
    assert _node(workflow, 311)["widgets_values"]["frame_rate"] == 24
    assert _node(workflow, 311)["widgets_values"]["crf"] == 19


def test_workflow_v3_is_labelled_as_v3() -> None:
    workflow = _workflow()
    label = _node(workflow, 165)
    assert "V3（24图槽）" in label["title"]
    ref_graph = _subgraph(workflow, "②")
    assert any("图片9-23" in group["title"] for group in ref_graph.get("groups", [])), (
        "新增槽位应有自己的组，用户才能找到要填的图"
    )


# ── 生成脚本：可复现、无本机绝对路径 ───────────────────────────────


def test_generator_has_no_machine_specific_paths() -> None:
    source = GENERATOR_PATH.read_text(encoding="utf-8")
    assert "D:/Comfyui" not in source and "D:\\Comfyui" not in source
    assert "--v2" in source and "--out" in source


@pytest.mark.skipif(not V2_WORKFLOW.exists(), reason="本机没有 V2 工作流，换机时跳过")
def test_generator_reproduces_committed_workflow() -> None:
    """生成脚本必须能从 V2 精确重建仓库里那份 V3（否则副本无法换机复现）。"""
    name = "comfyui_v3_generator_under_test"
    spec = importlib.util.spec_from_file_location(name, GENERATOR_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
        v2 = json.loads(V2_WORKFLOW.read_text(encoding="utf-8"))
        rebuilt = module.build_v3(v2)
    finally:
        sys.modules.pop(name, None)

    committed = _workflow()
    assert json.dumps(rebuilt, ensure_ascii=False, sort_keys=True) == json.dumps(
        committed, ensure_ascii=False, sort_keys=True
    )


@pytest.mark.skipif(not V2_WORKFLOW.exists(), reason="本机没有 V2 工作流，换机时跳过")
def test_v3_differs_from_v2_only_where_expected() -> None:
    """除图槽相关件外，V3 与 V2 必须逐节点一致（参数对齐的强判据）。"""
    v2 = json.loads(V2_WORKFLOW.read_text(encoding="utf-8"))
    v3 = _workflow()

    assert v2["definitions"]["subgraphs"][0] == v3["definitions"]["subgraphs"][0], (
        "①模型选择子图必须原样保留"
    )
    assert v2["extra"] == v3["extra"]
    assert v2["groups"] == v3["groups"]

    def by_id(graph: dict) -> dict[int, dict]:
        return {n["id"]: n for n in graph["nodes"]}

    v2_top, v3_top = by_id(v2), by_id(v3)
    assert set(v2_top) == set(v3_top), "顶层节点集合不应增删（只改已有节点）"
    affected = {165, 2527, 2580, 2586, 2587}  # 画布标题、子图实例接口、两个 Reroute 端口类型
    for node_id in set(v2_top) - affected:
        assert v2_top[node_id] == v3_top[node_id], f"顶层节点 {node_id} 不应漂移"

    v2_ref = _subgraph(v2, "②")
    v3_ref = _subgraph(v3, "②")
    v2_ref_nodes, v3_ref_nodes = by_id(v2_ref), by_id(v3_ref)
    # V2 原有的节点（9 张图 + 视频/音频 + 选择器）原样不动；2575 是唯一要换的总线节点。
    for node_id, node in v2_ref_nodes.items():
        if node_id == 2575:
            continue
        assert v3_ref_nodes[node_id] == node, f"参考资产子图原有节点 {node_id} 不应漂移"

    v2_param = _subgraph(v2, "③")
    v3_param = _subgraph(v3, "③")
    for node_id in (2578, 2579):
        old = by_id(v2_param)[node_id]
        new = by_id(v3_param)[node_id]
        assert old.get("widgets_values") == new.get("widgets_values")
        assert old.get("widgets_values_named") == new.get("widgets_values_named")
        assert old["pos"] == new["pos"]


# ── 仓库副本 vs 本机安装件 ────────────────────────────────────────


@pytest.mark.skipif(not (INSTALLED_NODE_DIR / "nodes_refs24.py").exists(), reason="本机未安装节点包")
def test_installed_package_matches_repo_copy() -> None:
    for name in ("__init__.py", "nodes_refs24.py"):
        installed = (INSTALLED_NODE_DIR / name).read_text(encoding="utf-8")
        copy = (NODE_DIR / name).read_text(encoding="utf-8")
        assert installed == copy, f"本机安装件 {name} 与仓库副本不一致"


# ── 文档：接线步骤与 V2 退路 ──────────────────────────────────────


def test_docs_explain_wiring_and_v2_fallback() -> None:
    text = README_PATH.read_text(encoding="utf-8")
    assert "V2" in text and "退路" in text
    assert "MiniMaxH3-Refs24" in text
    assert "verify_nodes_import.py" in text
    assert "generate_workflow_v3.py" in text

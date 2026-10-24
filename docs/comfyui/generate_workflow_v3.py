"""从 V2 工作流生成 V3（24 图槽）——换机复现用。

**为什么用脚本而不是手改 JSON**：V3 相对 V2 要动 24 个图槽的连线、两组子图边界
和编码节点的槽位下标（图槽从 9 扩到 24 后，视频/音频/提示词/尺寸的 target_slot
全部平移）。手改一次能过，但换机或上游工作流更新后无法重来；脚本把「改哪里」
写成可执行、可测试的规则（``tests/test_comfyui_v3_artifacts.py`` 会核对脚本
重建结果与仓库副本逐字段一致）。

**不改 V2**：输入只读，输出另存 V3；V2 必须保持原样作退路（ADR 0017）。

用法（在仓库根目录，路径按本机实际填写）::

    python docs/comfyui/generate_workflow_v3.py \
        --v2 "<ComfyUI>/user/default/workflows/001-JZL基础工作流/15-MiniMax-H3/59-(多Lora改)MiniMax-H3 ref2va 多参生视频 V2 .json" \
        --out "<同目录>/59-(多Lora改)MiniMax-H3 ref2va 多参生视频 V3（24图槽）.json"
"""

from __future__ import annotations

import argparse
import copy
import importlib.util
import json
from pathlib import Path


def _load_slot_constants():
    """槽位数从节点包**动态取**，不在这里另抄一份（复审 T1）。

    24/3/3 是节点端口的**物理事实**，真源在 ``MiniMaxH3-Refs24/nodes_refs24.py``。
    生成脚本、校验脚本、配置默认值各抄一份时，改一处而漏另一处不会报错——脚本会
    按旧数重建工作流，而节点包已是新数（ADR 0017 的原始事故：接线接进空气）。
    目录名带连字符不能普通 import，故用 ``spec_from_file_location`` 指向模块文件。
    """
    path = Path(__file__).resolve().parent / "MiniMaxH3-Refs24" / "nodes_refs24.py"
    spec = importlib.util.spec_from_file_location("refs24_slots_for_generator", path)
    if spec is None or spec.loader is None:  # pragma: no cover - 仓库副本缺失才发生
        raise SystemExit(f"[fail] 无法加载节点槽位常量：{path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_SLOTS = _load_slot_constants()
IMAGE_SLOTS = _SLOTS.REF_IMAGE_SLOTS
VIDEO_SLOTS = _SLOTS.REF_VIDEO_SLOTS
VIDEO_AUDIO_SLOTS = _SLOTS.VIDEO_AUDIO_SLOTS
AUDIO_SLOTS = _SLOTS.REF_AUDIO_SLOTS

# 类型名故意不复用上游的 JZL_REF2VA_BUS，防止 18 槽总线与 24 槽总线互插后静默少喂图。
BUS_TYPE = "MINIMAX_H3_REFS24_BUS"
BUS_OUT_TYPE = "MiniMaxH3Refs24BusOut"
BUS_IN_TYPE = "MiniMaxH3Refs24BusIn"
ENCODE_TYPE = "MiniMaxH3Refs24ReferenceToVideo"

V2_BUS_OUT_TYPE = "JZL_MiniMaxH3Ref2vaBusOut"
V2_BUS_IN_TYPE = "JZL_MiniMaxH3Ref2vaBusIn"
V2_ENCODE_TYPE = "JZL_MiniMaxH3ReferenceToVideo2"

# 新工作流用固定 UUID：脚本重复运行必须逐字节可复现（测试据此比对副本）。
V3_WORKFLOW_ID = "4f0d8a92-7c1e-4a3b-9d6e-2b8f5c1a7e40"

# 占位图必须真实存在于 ComfyUI/input，否则工作流一载入就报缺图；
# 用户在注入卡里按 Picture N 替换全部 24 张。
PLACEHOLDER_IMAGE = "1.png"


def _subgraph(workflow: dict, prefix: str) -> dict:
    matches = [sg for sg in workflow["definitions"]["subgraphs"] if sg["name"].startswith(prefix)]
    if len(matches) != 1:
        raise ValueError(f"子图 {prefix} 应恰好有一个，实际 {len(matches)}")
    return matches[0]


def _node(graph: dict, node_id: int) -> dict:
    matches = [n for n in graph["nodes"] if n["id"] == node_id]
    if len(matches) != 1:
        raise ValueError(f"节点 {node_id} 应恰好有一个，实际 {len(matches)}")
    return matches[0]


def _node_by_type(graph: dict, node_type: str) -> dict:
    matches = [n for n in graph["nodes"] if n["type"] == node_type]
    if len(matches) != 1:
        raise ValueError(f"类型 {node_type} 应恰好有一个，实际 {len(matches)}")
    return matches[0]


def _link_map(graph: dict) -> dict[int, dict]:
    return {link["id"]: link for link in graph["links"]}


def _next_node_id(graph: dict) -> int:
    current = max([graph["state"].get("lastNodeId", 0)] + [n["id"] for n in graph["nodes"]])
    graph["state"]["lastNodeId"] = current + 1
    return current + 1


def _next_link_id(graph: dict) -> int:
    current = max([graph["state"].get("lastLinkId", 0)] + [link["id"] for link in graph["links"]])
    graph["state"]["lastLinkId"] = current + 1
    return current + 1


def _add_link(
    graph: dict,
    origin_id: int,
    origin_slot: int,
    target_id: int,
    target_slot: int,
    link_type: str,
) -> int:
    link_id = _next_link_id(graph)
    graph["links"].append(
        {
            "id": link_id,
            "origin_id": origin_id,
            "origin_slot": origin_slot,
            "target_id": target_id,
            "target_slot": target_slot,
            "type": link_type,
        }
    )
    return link_id


def _update_link(graph: dict, link_id: int, **changes) -> None:
    link = _link_map(graph)[link_id]
    link.update(changes)


def _expand_reference_graph(graph: dict) -> None:
    """②参考资产管理：9 个图槽扩到 24，并换成 24 槽总线。"""
    bus_out = _node_by_type(graph, V2_BUS_OUT_TYPE)
    links = _link_map(graph)

    # 先按图槽顺序记录 V2 的 9 条链路：LoadImage → Reroute → 总线。
    keep_chains: list[tuple[int, int, int]] = []  # (load_id, reroute_id, bus_link_id)
    for slot in range(9):
        bus_link = links[bus_out["inputs"][slot]["link"]]
        reroute = _node(graph, bus_link["origin_id"])
        upstream_link = links[reroute["inputs"][0]["link"]]
        keep_chains.append((upstream_link["origin_id"], reroute["id"], bus_link["id"]))

    _, reroute_template_id, _ = keep_chains[0]
    load_template = _node(graph, keep_chains[0][0])
    reroute_template = _node(graph, reroute_template_id)
    max_order = max(n.get("order", 0) for n in graph["nodes"])

    # 新槽位排在视频组下方（原图组 3 列 × 5 行），不覆盖任何现有节点。
    added_chains: dict[int, int] = {}
    for offset, slot in enumerate(range(9, IMAGE_SLOTS)):
        row, column = divmod(offset, 3)
        load = copy.deepcopy(load_template)
        load["id"] = _next_node_id(graph)
        load["pos"] = [-1180 + column * 300, 2990 + row * 420]
        load["title"] = f"参考图{slot}（ref_image_{slot}）"
        load["widgets_values"] = [PLACEHOLDER_IMAGE, "image"]
        load["order"] = max_order + 1 + offset * 2
        load["outputs"][0]["links"] = []

        reroute = copy.deepcopy(reroute_template)
        reroute["id"] = _next_node_id(graph)
        reroute["pos"] = [-160, 1490 + offset * 20]
        reroute["order"] = max_order + 2 + offset * 2
        reroute["inputs"][0]["link"] = None
        reroute["outputs"][0]["links"] = []

        graph["nodes"].extend([load, reroute])
        load_to_reroute = _add_link(graph, load["id"], 0, reroute["id"], 0, "IMAGE")
        reroute_to_bus = _add_link(graph, reroute["id"], 0, bus_out["id"], slot, "IMAGE")
        load["outputs"][0]["links"] = [load_to_reroute]
        reroute["inputs"][0]["link"] = load_to_reroute
        reroute["outputs"][0]["links"] = [reroute_to_bus]
        added_chains[slot] = reroute_to_bus

    # 重建总线输入：前 24 个是图槽，其余视频/音轨/音频原样保留。
    rest_inputs = bus_out["inputs"][9:]
    video_links = [item["link"] for item in rest_inputs[0:VIDEO_SLOTS]]
    video_audio_links = [
        item["link"] for item in rest_inputs[VIDEO_SLOTS : VIDEO_SLOTS + VIDEO_AUDIO_SLOTS]
    ]
    audio_links = [
        item["link"]
        for item in rest_inputs[VIDEO_SLOTS + VIDEO_AUDIO_SLOTS : VIDEO_SLOTS + VIDEO_AUDIO_SLOTS + AUDIO_SLOTS]
    ]
    image_inputs = []
    for slot in range(IMAGE_SLOTS):
        link_id = keep_chains[slot][2] if slot < 9 else added_chains[slot]
        image_inputs.append(
            {
                "localized_name": f"🖼️ 参考图{slot}",
                "name": f"ref_image_{slot}",
                "shape": 7,
                "type": "IMAGE",
                "link": link_id,
            }
        )
    bus_out["inputs"] = image_inputs + rest_inputs
    # 视频/音轨/音频的输入槽位随图槽数整体后移，旧连线的 target_slot 必须同步。
    for slot, link_id in enumerate(video_links):
        _update_link(graph, link_id, target_slot=IMAGE_SLOTS + slot)
    for slot, link_id in enumerate(video_audio_links):
        _update_link(graph, link_id, target_slot=IMAGE_SLOTS + VIDEO_SLOTS + slot)
    for slot, link_id in enumerate(audio_links):
        _update_link(
            graph, link_id,
            target_slot=IMAGE_SLOTS + VIDEO_SLOTS + VIDEO_AUDIO_SLOTS + slot)
    bus_out["type"] = BUS_OUT_TYPE
    bus_out["properties"] = {"Node name for S&R": BUS_OUT_TYPE}
    bus_out["size"] = [340, 780]
    bus_out["outputs"][0]["name"] = "refs24参考总线"
    bus_out["outputs"][0]["localized_name"] = "🔗 refs24参考总线"
    bus_out["outputs"][0]["type"] = BUS_TYPE

    boundary_link = bus_out["outputs"][0]["links"][0]
    _update_link(graph, boundary_link, type=BUS_TYPE)
    graph["outputs"][0]["name"] = "refs24参考总线"
    graph["outputs"][0]["type"] = BUS_TYPE

    # 给新增槽位单独分组：用户需要在画布上找到这 15 个 LoadImage。
    group_ids = [group["id"] for group in graph.get("groups", [])]
    graph.setdefault("groups", []).append(
        {
            "id": max(group_ids + [0]) + 1,
            "title": "🖼️启用图片9-23（V3 新增）",
            "bounding": [-1190, 2940, 900, 2240],
            "color": "#3f789e",
            "flags": {"pinned": True},
        }
    )


def _rewire_parameter_graph(graph: dict) -> None:
    """③生成参数配置：换 24 槽总线解包节点，并平移编码节点所有槽位下标。"""
    bus_in = _node_by_type(graph, V2_BUS_IN_TYPE)
    encoder = _node_by_type(graph, V2_ENCODE_TYPE)
    links = _link_map(graph)

    old_inputs = {item["name"]: item for item in encoder["inputs"]}
    image_links = [old_inputs[f"ref_image_{i}"]["link"] for i in range(9)]
    video_links = [old_inputs[f"ref_video_{i}"]["link"] for i in range(VIDEO_SLOTS)]
    video_audio_links = [
        old_inputs[f"ref_video_audio_{i}"]["link"] for i in range(VIDEO_AUDIO_SLOTS)
    ]
    audio_links = [old_inputs[f"ref_audio_{i}"]["link"] for i in range(AUDIO_SLOTS)]
    clip_link = old_inputs["clip"]["link"]
    vae_link = old_inputs["vae"]["link"]
    audio_vae_link = old_inputs["audio_vae"]["link"]
    prompt_link = old_inputs["prompt"]["link"]
    width_link = old_inputs["width"]["link"]
    height_link = old_inputs["height"]["link"]
    length_link = old_inputs["length"]["link"]
    ref_scale_link = old_inputs["ref_scale"]["link"]

    # 图槽 9..23 需要新的 总线输出 → 编码节点 连线。
    for slot in range(9, IMAGE_SLOTS):
        image_links.append(_add_link(graph, bus_in["id"], slot, encoder["id"], 3 + slot, "IMAGE"))

    # 串行化顺序沿用 V2：链接型输入在前，widget 型输入在后。
    encoder_inputs = [
        {"localized_name": "CLIP文本编码器", "name": "clip", "type": "CLIP", "link": clip_link},
        {"localized_name": "VAE", "name": "vae", "type": "VAE", "link": vae_link},
        {"localized_name": "音频VAE", "name": "audio_vae", "type": "VAE", "link": audio_vae_link},
    ]
    for slot in range(IMAGE_SLOTS):
        encoder_inputs.append(
            {
                "localized_name": f"ref_image_{slot}",
                "name": f"ref_image_{slot}",
                "shape": 7,
                "type": "IMAGE",
                "link": image_links[slot],
            }
        )
    for slot in range(VIDEO_SLOTS):
        encoder_inputs.append(
            {
                "localized_name": f"ref_video_{slot}",
                "name": f"ref_video_{slot}",
                "shape": 7,
                "type": "IMAGE",
                "link": video_links[slot],
            }
        )
    for slot in range(VIDEO_AUDIO_SLOTS):
        encoder_inputs.append(
            {
                "localized_name": f"ref_video_audio_{slot}",
                "name": f"ref_video_audio_{slot}",
                "shape": 7,
                "type": "AUDIO",
                "link": video_audio_links[slot],
            }
        )
    for slot in range(AUDIO_SLOTS):
        encoder_inputs.append(
            {
                "localized_name": f"ref_audio_{slot}",
                "name": f"ref_audio_{slot}",
                "shape": 7,
                "type": "AUDIO",
                "link": audio_links[slot],
            }
        )
    for name, localized, link_id, widget_kind in (
        ("prompt", "提示词", prompt_link, "STRING"),
        ("width", "宽度", width_link, "INT"),
        ("height", "高度", height_link, "INT"),
        ("length", "时长(帧)", length_link, "INT"),
        ("ref_image_size", "参考图尺寸", None, "COMBO"),
        ("ref_scale", "参考值放大", ref_scale_link, "FLOAT"),
    ):
        encoder_inputs.append(
            {
                "localized_name": localized,
                "name": name,
                "type": widget_kind,
                "widget": {"name": name},
                "link": link_id,
            }
        )

    encoder["inputs"] = encoder_inputs
    encoder["type"] = ENCODE_TYPE
    encoder["properties"] = {"Node name for S&R": ENCODE_TYPE}
    encoder["size"] = [400, 1000]

    # 原有连线只改槽位下标，不换 id：V2 里已有的 9 张图仍是同一条线。
    for slot in range(9):
        _update_link(graph, image_links[slot], target_slot=3 + slot)
    # 编码节点前 3 个输入是固定的 CLIP/VAE/音频VAE；参考槽从这里往后排。
    fixed_encoder_inputs = 3
    for slot in range(VIDEO_SLOTS):
        _update_link(
            graph, video_links[slot],
            origin_slot=IMAGE_SLOTS + slot,
            target_slot=fixed_encoder_inputs + IMAGE_SLOTS + slot)
    for slot in range(VIDEO_AUDIO_SLOTS):
        _update_link(
            graph,
            video_audio_links[slot],
            origin_slot=IMAGE_SLOTS + VIDEO_SLOTS + slot,
            target_slot=fixed_encoder_inputs + IMAGE_SLOTS + VIDEO_SLOTS + slot,
        )
    for slot in range(AUDIO_SLOTS):
        _update_link(
            graph, audio_links[slot],
            origin_slot=IMAGE_SLOTS + VIDEO_SLOTS + VIDEO_AUDIO_SLOTS + slot,
            target_slot=(fixed_encoder_inputs + IMAGE_SLOTS + VIDEO_SLOTS
                         + VIDEO_AUDIO_SLOTS + slot))
    # widget 型输入排在全部参考槽之后：prompt / width / height / length / ref_image_size / ref_scale。
    first_widget_input = (fixed_encoder_inputs + IMAGE_SLOTS + VIDEO_SLOTS
                          + VIDEO_AUDIO_SLOTS + AUDIO_SLOTS)
    _update_link(graph, prompt_link, target_slot=first_widget_input)
    _update_link(graph, width_link, target_slot=first_widget_input + 1)
    _update_link(graph, height_link, target_slot=first_widget_input + 2)
    _update_link(graph, length_link, target_slot=first_widget_input + 3)
    _update_link(graph, ref_scale_link, target_slot=first_widget_input + 5)

    # 解包节点：24 图 + 3 视频 + 3 音轨 + 3 音频。
    bus_outputs = []
    for slot in range(IMAGE_SLOTS):
        bus_outputs.append(
            {
                "localized_name": f"🖼️ 参考图{slot}",
                "name": f"ref_image_{slot}",
                "type": "IMAGE",
                "links": [image_links[slot]],
            }
        )
    for slot in range(VIDEO_SLOTS):
        bus_outputs.append(
            {
                "localized_name": f"🎬 参考视频{slot}",
                "name": f"ref_video_{slot}",
                "type": "IMAGE",
                "links": [video_links[slot]],
            }
        )
    for slot in range(VIDEO_AUDIO_SLOTS):
        bus_outputs.append(
            {
                "localized_name": f"🎧 视频音轨{slot}",
                "name": f"ref_video_audio_{slot}",
                "type": "AUDIO",
                "links": [video_audio_links[slot]],
            }
        )
    for slot in range(AUDIO_SLOTS):
        bus_outputs.append(
            {
                "localized_name": f"🎧 音频{slot}",
                "name": f"ref_audio_{slot}",
                "type": "AUDIO",
                "links": [audio_links[slot]],
            }
        )
    bus_in["outputs"] = bus_outputs
    bus_in["type"] = BUS_IN_TYPE
    bus_in["properties"] = {"Node name for S&R": BUS_IN_TYPE}
    bus_in["size"] = [340, 780]
    bus_in["inputs"][0]["type"] = BUS_TYPE
    bus_in["inputs"][0]["localized_name"] = "🔗 refs24参考总线"

    bus_link = bus_in["inputs"][0]["link"]
    _update_link(graph, bus_link, type=BUS_TYPE)
    graph["inputs"][0]["type"] = BUS_TYPE
    graph["inputs"][0]["localized_name"] = "🔗 refs24参考总线"


def _update_top_level(workflow: dict) -> None:
    ref_instance = _node(workflow, 2527)
    ref_instance["outputs"][0]["name"] = "refs24参考总线"
    ref_instance["outputs"][0]["type"] = BUS_TYPE

    param_instance = _node(workflow, 2580)
    param_instance["inputs"][0]["type"] = BUS_TYPE
    param_instance["inputs"][0]["localized_name"] = "🔗 refs24参考总线"

    for raw in workflow["links"]:
        # 顶层 link 是数组序列化：[id, origin, origin_slot, target, target_slot, type]
        if raw[5] == "JZL_REF2VA_BUS":
            raw[5] = BUS_TYPE

    # Reroute 节点自己的端口类型也要换：类型留在端口上，用户仍能接错。
    for node in workflow["nodes"]:
        if node["type"] != "Reroute":
            continue
        for item in (node.get("inputs") or []) + (node.get("outputs") or []):
            if item.get("type") == "JZL_REF2VA_BUS":
                item["type"] = BUS_TYPE

    _node(workflow, 165)["title"] = "MiniMax-H3 ref2va 多参生视频 V3（24图槽）工作流"


def build_v3(v2: dict) -> dict:
    """返回 V3 工作流字典；不修改传入的 V2（便于测试里两臂对照）。"""
    workflow = copy.deepcopy(v2)
    workflow["id"] = V3_WORKFLOW_ID
    workflow["revision"] = 0

    _expand_reference_graph(_subgraph(workflow, "②"))
    _rewire_parameter_graph(_subgraph(workflow, "③"))
    _update_top_level(workflow)
    return workflow


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="从 V2 工作流生成 24 图槽 V3")
    parser.add_argument("--v2", required=True, help="V2 工作流 JSON 路径（只读）")
    parser.add_argument("--out", required=True, help="V3 工作流输出路径")
    args = parser.parse_args(argv)

    v2_path = Path(args.v2)
    out_path = Path(args.out)
    if not v2_path.exists():
        raise SystemExit(f"找不到 V2 工作流：{v2_path}")
    if v2_path.resolve() == out_path.resolve():
        raise SystemExit("输出路径不能与 V2 相同：V2 必须原样保留作退路（ADR 0017）")

    v2 = json.loads(v2_path.read_text(encoding="utf-8"))
    v3 = build_v3(v2)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(v3, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"已生成 V3：{out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

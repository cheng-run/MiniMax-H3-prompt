from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path

ROOT = Path(r"D:\Comfyui\ComfyUI\user\default\workflows")
OUT = Path(__file__).with_name("workflow-inventory.json")
TARGET = ("z-image", "z_image", "flux", "minimax", "h3", "56-", "57-", "58-", "59-", "62-", "63-", "64-", "1141")

def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()

def classify(relative: str, name: str) -> tuple[str, str]:
    text = f"{relative} {name}".lower()
    if "minimax" in text or "h3" in text:
        if "1141" in text or "latent" in text:
            return "h3_fl2va_latent_upscale", "candidate"
        if "ref2va" in text and ("放大" in text or "upscale" in text):
            return "h3_ref2va_upscale", "candidate"
        if "ref2va" in text:
            return "h3_ref2va", "candidate"
        if "59-" in text or "切换" in text:
            return "h3_multimodal_switch", "inventory_only"
        if "fl2va" in text or "首尾帧" in text:
            return "h3_fl2va", "candidate"
        if "t2va" in text:
            return "h3_t2va", "candidate"
        return "h3_unspecified", "inventory_only"
    if "flux" in text:
        if "文生图" in text or "t2i" in text:
            return "flux2_t2i", "candidate"
        if "单图" in text:
            return "flux2_single_edit", "candidate"
        if "双图" in text:
            return "flux2_dual_edit", "candidate"
        if "三图" in text:
            return "flux2_triple_edit", "inventory_only"
        if "局部" in text:
            return "flux2_inpaint", "inventory_only"
        if "扩展" in text:
            return "flux2_outpaint", "inventory_only"
        if "背景" in text:
            return "flux2_background", "inventory_only"
        return "flux2_unspecified", "inventory_only"
    if "z-image" in text or "z_image" in text:
        if "三视图" in text:
            return "zimage_character_sheet", "candidate"
        if "图生图" in text:
            return "zimage_i2i", "candidate"
        if "多模态" in text or "控制" in text or "姿态" in text:
            return "zimage_control_image", "candidate"
        if "文生图" in text:
            return "zimage_t2i", "candidate"
        return "zimage_unspecified", "inventory_only"
    return "other", "inventory_only"

def summarize(path: Path) -> dict:
    raw_bytes = path.read_bytes()
    item = {
        "name": path.name,
        "relative_path": path.relative_to(ROOT).as_posix(),
        "size": len(raw_bytes),
        "sha256": sha256(path),
        "modified_at": datetime.fromtimestamp(path.stat().st_mtime).astimezone().isoformat(),
    }
    capability, classification = classify(item["relative_path"], path.name)
    item["capability"] = capability
    item["classification"] = classification
    try:
        data = json.loads(raw_bytes.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        item["format"] = "invalid"
        item["errors"] = [str(exc)]
        return item
    if isinstance(data, dict) and data and all(isinstance(v, dict) and "class_type" in v and "inputs" in v for v in data.values()):
        item["format"] = "api"
        item["node_count"] = len(data)
        item["node_types"] = sorted({str(v.get("class_type", "")) for v in data.values()})
        item["node_ids"] = sorted(str(k) for k in data)
        item["link_count"] = sum(1 for v in data.values() for x in (v.get("inputs", {}) or {}).values() if isinstance(x, list) and len(x) >= 2 and isinstance(x[0], (str, int)))
        item["model_hints"] = sorted({str(value) for node in data.values() for key, value in (node.get("inputs", {}) or {}).items() if "name" in key.lower() and isinstance(value, str) and not value.startswith("$")})
    elif isinstance(data, dict) and isinstance(data.get("nodes"), list):
        item["format"] = "ui"
        nodes = [node for node in data["nodes"] if isinstance(node, dict)]
        item["node_count"] = len(nodes)
        item["node_types"] = sorted({str(node.get("type", "")) for node in nodes})
        item["node_ids"] = sorted(str(node.get("id", "")) for node in nodes)
        item["link_count"] = len(data.get("links", []) or [])
        item["subgraph_count"] = len((data.get("definitions", {}) or {}).get("subgraphs", []) or [])
        item["model_hints"] = sorted({str(value) for node in nodes for value in (node.get("widgets_values", []) or []) if isinstance(value, str) and (".safetensors" in value or ".gguf" in value or ".ckpt" in value)})
    else:
        item["format"] = "unknown"
        item["errors"] = ["顶层结构不是 ComfyUI UI 或 API 工作流"]
    return item

files = sorted(ROOT.rglob("*.json"), key=lambda path: path.as_posix().lower())
items = [summarize(path) for path in files]
out = {
    "schema_version": "2",
    "inventory_type": "comfyui_workflows",
    "scanned_root": str(ROOT),
    "scanned_at": datetime.now().astimezone().isoformat(),
    "read_only": True,
    "recursive": True,
    "file_count": len(items),
    "target_file_count": sum(item["capability"] != "other" for item in items),
    "files": items,
    "rules": {
        "raw_sha256": "对原始 JSON 字节计算，不能重新序列化后计算",
        "profile_status": "扫描结果最多生成 candidate；verified 需要完整静态契约核验；approved 需要实际运行和人工验收",
        "source_boundary": "本清单不修改工作流、不运行 ComfyUI、不导入 output",
        "classification_boundary": "文件名和目录只用于初步分类，最终 Profile 能力必须以节点、连接和人工确认核对",
    },
}
OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps({"file_count": len(items), "target_file_count": out["target_file_count"], "output": str(OUT)}, ensure_ascii=False))

"""参考图审核：用 DashScope qwen3.7-plus（多模态）读参考图，生成准确描述草稿。

图片先压缩（长边 ≤1024px JPEG）再 base64 发送，避免 4K 大图超限 / 烧 token。
产出的描述草稿在交互菜单里由用户确认/修改后才进管线，杜绝模型臆测。
"""
from __future__ import annotations

import base64
import io
from pathlib import Path

from langchain_core.messages import HumanMessage
from langchain_openai import ChatOpenAI
from PIL import Image

from ..config import config


def _build_vision_model() -> ChatOpenAI:
    settings = config.vision_model
    try:
        key = config.resolve_api_key(settings)
    except ValueError as exc:
        raise RuntimeError(
            f"缺失 {settings.api_key_env}：参考图审核需要 {settings.provider} 视觉模型"
        ) from exc
    return ChatOpenAI(model=settings.model, api_key=key, base_url=settings.base_url)


def _downscale_to_jpeg(path: Path, max_side: int = 1024) -> str:
    """压缩图片为 JPEG base64（长边 ≤ max_side），返回 data URI。"""
    img = Image.open(path)
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    w, h = img.size
    scale = max_side / max(w, h)
    if scale < 1:
        img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


def describe_image(path: str | Path) -> str:
    """用视觉模型描述一张参考图（角色/场景外观），供人工确认。"""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"参考图不存在：{p}")
    model = _build_vision_model()
    msg = HumanMessage(content=[
        {"type": "text", "text": (
            "请详细描述这张参考图的内容，用于 AI 视频的角色一致性绑定。\n"
            "重点：人物（性别/年龄/发型/服装颜色与款式/配饰/姿态）、场景（环境/光线/色调）、画面构图。\n"
            "只描述图中真实可见的内容，不要臆测或脑补不存在的细节。用中文输出，2-4 句话。"
        )},
        {"type": "image_url", "image_url": {"url": _downscale_to_jpeg(p)}},
    ])
    resp = model.invoke([msg])
    return resp.content if isinstance(resp.content, str) else str(resp.content)


def audit_refs(refs) -> list[dict]:
    """审核全部带路径的参考图，返回 [{picture, path, name, draft}]。"""
    results = []
    for r in refs:
        if not r.path:
            continue
        draft = describe_image(r.path)
        results.append({"picture": r.picture, "path": r.path, "name": r.name, "draft": draft})
    return results

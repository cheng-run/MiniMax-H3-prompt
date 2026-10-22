"""参考图审核：用 DashScope qwen3.7-plus（多模态）读参考图，生成准确描述草稿。

图片先压缩（长边 ≤1024px JPEG）再 base64 发送，避免 4K 大图超限 / 烧 token。
产出的描述草稿在交互菜单里由用户确认/修改后才进管线，杜绝模型臆测。

等待判据与角色调用**同源**（issue #71 / ADR 0019）：读图是一次可能几分钟的直调，
原先非流式 + ``timeout=900`` 让「卡住」与「慢慢生成」完全不可分——``[读图校对]``
之后用户只能白等。现在读图客户端走流式（首字节线 30 秒，与主档同一份常量），
首字节前超时判卡住并重试；**网关不支持流式**时明确报错，回退为「只报已等时长」的
非流式路径（那条路分不出卡住，所以只能诚实报已等多久，不许装成有进度）。
"""
from __future__ import annotations

import base64
import io
import threading
import time
from pathlib import Path

import httpx
from langchain_core.messages import HumanMessage
from langchain_openai import ChatOpenAI
from PIL import Image

from .. import model_factory
from ..config import config
from ..observability import VISION_ROLE, record_vision_call

# 非流式回退的请求超时：那条路看不到首字节，只能等完整响应，故沿用原来的 900 秒
# （DashScope 长输出可能超 600s）。**不要拿它的一个数当流式路径的读超时**——那正是
# issue #59 的病根（见 model_factory 的常量注释）。
_FALLBACK_TIMEOUT_SECONDS = 900

# 非流式回退期间的心跳间隔。回退路径没有首字节事件可订阅，只能由本模块定期打一行
# 「已等 Xs」（ADR 0019：回退为「只报已等时长」）——不打这一行，用户面对的又是一次
# 无声的几分钟。
_FALLBACK_HEARTBEAT_SECONDS = 10.0


def _build_vision_model(*, streaming: bool = True) -> ChatOpenAI:
    """视觉客户端：流式档接与主档同源的等待判据；非流式档只用于网关不支持流式的回退。

    为什么视觉侧要单独一份构造而不是复用 ``model_factory.build_chat_model``：视觉模型
    是另一份配置（``config.vision_model``），base_url/model 都不同；但**超时常量必须
    同源**——各写一个数就是两处会各自漂移的口径（改主档首字节线时读图不动，用户就会
    在两道不同的线之间猜）。
    """
    settings = config.vision_model
    try:
        key = config.resolve_api_key(settings)
    except ValueError as exc:
        raise RuntimeError(
            f"缺失 {settings.api_key_env}：参考图审核需要 {settings.provider} 视觉模型"
        ) from exc
    if streaming:
        timeout: object = httpx.Timeout(
            connect=model_factory._LLM_CONNECT_TIMEOUT_SECONDS,
            read=model_factory._LLM_READ_TIMEOUT_SECONDS,
            write=model_factory._LLM_WRITE_TIMEOUT_SECONDS,
            pool=model_factory._LLM_POOL_TIMEOUT_SECONDS,
        )
    else:
        timeout = _FALLBACK_TIMEOUT_SECONDS
    return ChatOpenAI(
        model=settings.model,
        api_key=key,
        base_url=settings.base_url,
        timeout=timeout,
        max_retries=0,    # 重试由看门狗控制（openai 自动重试会翻倍烧 token）
        # 读流式才有「首字节」这个活体信号；stream_usage 是它的伴生必要条件——
        # 只开 streaming 不开它，usage_metadata 是 None，成本表「看着还在、数是空的」
        # （2026-10-21 实测，见 model_factory 同处注释）。
        streaming=streaming,
        stream_usage=streaming,
    )


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


def _read_without_stream(messages: list, cause: str) -> str:
    """非流式回退读图：**只能报已等时长**，其余判据一概没有（ADR 0019）。

    为什么要一个线程：``invoke`` 是阻塞的，主线程不轮询就一行「已等」都打不出来。
    为什么回退失败要把两条病因都写出来：用户需要知道是「流式这一路不行」还是
    「这张图/这个网关整个不行」，只报后半句会让人以为换回主档就好了。
    """
    model = _build_vision_model(streaming=False)
    box: dict[str, object] = {}

    def worker() -> None:
        try:
            box["response"] = model.invoke(messages)
        except BaseException as exc:  # noqa: BLE001 - 病因原样交回调用方
            box["error"] = exc

    started = time.monotonic()
    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    while True:
        thread.join(_FALLBACK_HEARTBEAT_SECONDS)
        if not thread.is_alive():
            break
        # 间隔取模块常量（测试可压到毫秒级）；秒数取本次等待的墙钟
        print(f"  ⏳ 读图仍在等待（已等 {time.monotonic() - started:.0f}s；"
              "非流式回退，只能报已等时长）", flush=True)
    if "error" in box:
        raise RuntimeError(f"读图失败（流式：{cause}；非流式：{box['error']}）")
    response = box.get("response")
    content = getattr(response, "content", response)
    text = content if isinstance(content, str) else str(content)
    if not text.strip():
        raise RuntimeError(f"读图结果为空（流式：{cause}；非流式同样没有内容）")
    # 回退也是一次真实计费调用：不进成本表，「跑完的成本」就少一块（issue #52）。
    record_vision_call(response, text, time.monotonic() - started)
    return text


def describe_image(path: str | Path, instruction: str | None = None) -> str:
    """用视觉模型描述一张参考图（角色/场景外观），供人工确认。

    instruction 可覆盖默认读图提示词（frame_auditor 用它定制首帧/尾帧描述）。

    流式是**唯一主路**：走 ``run_direct_call`` 拿与角色调用同源的等待判据（首字节
    事件、卡住重试、预算、失败诊断）。两类失败要分开处理：

    - **超时（卡住 / 中流断流）**：照实抛错，**不回退**。回退非流式等于把「判出
      卡住」重新埋回 900 秒白等里，正是本票要治的病；
    - **非超时失败或空流**：最可能是网关不吃 ``stream``（400 拒掉，或干脆忽略参数
      返回一坨 JSON → 零个块）。这时明确报出来，回退非流式，只报已等时长。
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"参考图不存在：{p}")
    text = instruction or (
        "请详细描述这张参考图的内容，用于 AI 视频的角色一致性绑定。\n"
        "重点：人物（性别/年龄/发型/服装颜色与款式/配饰/姿态）、场景（环境/光线/色调）、画面构图。\n"
        "只描述图中真实可见的内容，不要臆测或脑补不存在的细节。用中文输出，2-4 句话。"
    )
    messages = [HumanMessage(content=[
        {"type": "text", "text": text},
        {"type": "image_url", "image_url": {"url": _downscale_to_jpeg(p)}},
    ])]
    # 延迟导入：agents 会拉起整条角色装配栈，而读图口也被 --brief 快路径直接 import
    # （同 segment_planner 的同一处置）。
    from ..agents import run_direct_call

    result = run_direct_call(_build_vision_model(), messages, role=VISION_ROLE)
    if result.ok and result.text.strip():
        return result.text
    if result.timed_out:
        # 卡住不是「不支持流式」：不许回退（见 docstring）
        raise RuntimeError(result.error)
    cause = result.error if not result.ok else "流式响应没有任何内容（网关可能忽略了 stream 参数）"
    print(f"[读图] 流式读图失败：{cause}", flush=True)
    print("[读图] 网关可能不支持流式：已回退为非流式读图——这条路上没有首字节可判，"
          "等待期间只能报告已等时长（ADR 0019）。", flush=True)
    return _read_without_stream(messages, cause)


def audit_refs(refs) -> list[dict]:
    """审核全部带路径的参考图，返回 [{picture, path, name, draft}]。"""
    results = []
    for r in refs:
        if not r.path:
            continue
        draft = describe_image(r.path)
        results.append({"picture": r.picture, "path": r.path, "name": r.name, "draft": draft})
    return results

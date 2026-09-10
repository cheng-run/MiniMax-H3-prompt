"""关键帧图片读图审核：用 qwen3.7-plus 读真实首帧/尾帧，产出画面描述供管线锚定。

与 reference_auditor（参考图身份绑定）不同：这里读的是**视频第一帧/最后一帧的
实际画面**，描述将作为 prompt_engineer 组装正文时的最高优先级锚定文本。
单张读图失败只降级跳过该帧（回退生图提示词锚定），不阻塞整条管线。
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..brief_parser import Brief, RefItem
from ..task_package import resolve_input_path

# 变体 → 所需 (Picture 编号, 帧位)；T2VA 无关键帧
_FRAME_REQUIREMENTS: dict[str, tuple[tuple[int, str], ...]] = {
    "FL2VA": ((1, "first"), (2, "last")),
    "I2VA": ((1, "first"),),
    "L2VA": ((1, "last"),),
}

_ROLE_LABELS = {"first": "视频的第一帧", "last": "视频的最后一帧"}


def required_frames(variant: str) -> tuple[tuple[int, str], ...]:
    """返回该变体需要的关键帧位；未知变体按 T2VA 处理（无帧）。"""
    return _FRAME_REQUIREMENTS.get(str(variant).upper(), ())


@dataclass(frozen=True)
class FrameAudit:
    """一帧真实图片的读图结果。"""

    picture: int
    role: str  # "first" | "last"
    path: str
    description: str

    def to_dict(self) -> dict[str, object]:
        return {
            "picture": self.picture,
            "role": self.role,
            "path": self.path,
            "description": self.description,
        }


def _frame_prompt(role: str) -> str:
    return (
        f"这张图是一段 AI 视频的{_ROLE_LABELS[role]}静态画面。请精确描述画面内容："
        "地点与环境（室内/室外、具体场所）、人物（性别/年龄/发型/服装颜色款式/姿态）、"
        "关键道具、光线方向与色调、构图（景别与主体位置）。"
        "只描述图中真实可见的内容，不要臆测或补充不存在的细节。用中文输出，3-5 句话。"
    )


def audit_frame_images(
    refs: list[RefItem],
    variant: str,
    *,
    describe=None,
    on_frame=None,
) -> list[FrameAudit]:
    """读取该变体所需的关键帧图片。

    describe 可注入 mock 测试；默认用 reference_auditor.describe_image（qwen3.7-plus）。
    on_frame：可选回调，每张帧图开始读前以帧位标签（如"视频的第一帧"）调用，供 UI 报进度。
    - 对应帧位的 ref 没有 path：跳过该帧（回退生图提示词锚定）；
    - path 指向不存在的文件：FileNotFoundError（显式失败）；
    - 单张读图异常：捕获并跳过该帧，其余继续。
    """
    describe_fn = describe
    if describe_fn is None:
        from .reference_auditor import describe_image as describe_fn
    by_picture = {ref.picture: ref for ref in refs}
    audits: list[FrameAudit] = []
    for picture, role in required_frames(variant):
        ref = by_picture.get(picture)
        if ref is None or not ref.path:
            continue
        path = resolve_input_path(ref.path)
        if not path.is_file():
            raise FileNotFoundError(f"{_ROLE_LABELS[role]}图片不存在：{path}")
        try:
            if on_frame is not None:
                on_frame(_ROLE_LABELS[role])
            description = str(describe_fn(path, _frame_prompt(role))).strip()
        except Exception:  # noqa: BLE001 - 单帧失败降级，不阻塞管线
            continue
        if description:
            audits.append(FrameAudit(
                picture=picture, role=role, path=str(path), description=description,
            ))
    return audits


__all__ = ["FrameAudit", "required_frames", "audit_frame_images", "extract_last_frame"]


# ---------------------------------------------------------------------------
# 桥接帧：从执行输出剥尾帧（cv2，无外部 ffmpeg 依赖）
# ---------------------------------------------------------------------------

def extract_last_frame(video_path: str | Path, output_path: str | Path | None = None) -> Path:
    """用 OpenCV 剥出视频最后一帧并写为 PNG。

    - cv2 懒加载：只为段桥接流程引入解码依赖，不拖慢主流程 import。
    - 先按总帧数 seek；seek 失败（部分编码不允许随机访问）则顺序遍历兜底。
    - Windows 非 ASCII 路径用 ``imencode + tofile`` 落盘，绕开 ``cv2.imwrite`` 的编码坑。
    """
    import cv2  # noqa: PLC0415 - 懒加载重依赖

    source = Path(video_path)
    if not source.is_file():
        raise FileNotFoundError(f"视频文件不存在：{source}")
    capture = cv2.VideoCapture(str(source))
    if not capture.isOpened():
        raise ValueError(f"无法打开视频（编码不被支持或文件损坏）：{source}")
    try:
        total = int(capture.get(cv2.CAP_PROP_FRAME_COUNT))
        frame = None
        if total > 0:
            capture.set(cv2.CAP_PROP_POS_FRAMES, total - 1)
            ok, candidate = capture.read()
            if ok and candidate is not None:
                frame = candidate
        if frame is None:  # seek 失败：顺序读到最后一帧
            capture.set(cv2.CAP_PROP_POS_FRAMES, 0)
            while True:
                ok, candidate = capture.read()
                if not ok or candidate is None:
                    break
                frame = candidate
    finally:
        capture.release()
    if frame is None:
        raise ValueError(f"未能从视频中读取任何帧：{source}")

    target = Path(output_path) if output_path else source.with_suffix(".last-frame.png")
    target.parent.mkdir(parents=True, exist_ok=True)
    ok, buffer = cv2.imencode(".png", frame)
    if not ok:
        raise ValueError(f"尾帧编码为 PNG 失败：{source}")
    buffer.tofile(str(target))
    return target

"""关键帧图片读图审核：用 qwen3.7-plus 读真实首帧/尾帧，产出画面描述供管线锚定。

与 reference_auditor（参考图身份绑定）不同：这里读的是**视频第一帧/最后一帧的
实际画面**，描述将作为 prompt_engineer 组装正文时的最高优先级锚定文本。
单张读图失败只降级跳过该帧（回退生图提示词锚定），不阻塞整条管线。
"""
from __future__ import annotations

from contextlib import contextmanager
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


__all__ = [
    "FrameAudit", "required_frames", "audit_frame_images",
    "describe_bridge_frame", "describe_segment_frame",
    "extract_first_frame", "extract_last_frame", "segment_frame_prompt",
]


# ---------------------------------------------------------------------------
# 桥接帧读图（issue #12）：剥出的桥接帧必须在写下一段提示词前被读出来
# ---------------------------------------------------------------------------

def bridge_frame_prompt() -> str:
    """桥接帧读图提示词：描述的是**下一段的 0.00s 开场状态**，措辞对齐关键帧读图。"""
    return (
        "这张图是一段 AI 视频某个执行段的开场静态画面（上一段视频的末尾，剥出作下一段首帧）。"
        "请精确描述画面内容：地点与环境、人物（性别/年龄/发型/服装颜色款式/姿态）、"
        "关键动物与道具、光线方向与色调、构图（景别与主体位置）。"
        "只描述图中真实可见的内容，不要臆测或补充不存在的细节。用中文输出，3-5 句话。"
    )


def _read_frame_or_none(path: str | Path, prompt: str, *,
                        label: str, on_failure: str) -> str | None:
    """读一张帧图 → 画面描述；失败**显式警告**并返回 None（诚实降级，绝不伪造读图记录）。

    读图走 ``reference_auditor.describe_image`` **唯一口**（关键帧/桥接帧/段首末帧同源）。
    ``label`` 用于「XX图片不存在」「XX读图失败」两句话术，``on_failure`` 是失败后的
    去向说明（两种帧位的去向不同：桥接帧退回图片锚定，段首末帧则是「本次没有可对照的
    画面描述」）。文件不存在同样走警告降级——刚剥出帧却读不了图，不该炸掉陪跑流程。
    """
    from .reference_auditor import describe_image

    p = Path(path)
    try:
        if not p.is_file():
            raise FileNotFoundError(f"{label}图片不存在：{p}")
        description = str(describe_image(p, prompt)).strip()
    except Exception as exc:  # noqa: BLE001 - 单帧失败降级，不阻塞陪跑流程
        print(f"[警告] {label}读图失败（{exc}）：{on_failure}")
        return None
    return description or None


def describe_bridge_frame(path: str | Path) -> str | None:
    """读一张桥接帧图，返回画面描述；失败返回 None（调用方诚实降级，不阻塞人工流程）。"""
    return _read_frame_or_none(
        path, bridge_frame_prompt(), label="桥接帧",
        on_failure="下一段提示词将退回仅按图片锚定（无读图结果）。")


# 段首/末帧读图的帧位标签（issue #56）：读的是**本段视频自己的**开场/收尾画面，
# 不是上一段的尾帧——所以措辞里点明时刻，让读图模型把注意力放在「这一瞬间」上。
_SEGMENT_FRAME_LABELS = {
    "first": ("首帧", "本段 0.00s 的开场画面"),
    "last": ("末帧", "本段的收尾画面"),
}


def segment_frame_prompt(role: str) -> str:
    """段首/末帧读图提示词（措辞对齐桥接帧与关键帧读图，只换「是哪一帧」）。"""
    try:
        label, moment = _SEGMENT_FRAME_LABELS[str(role)]
    except KeyError as exc:
        raise ValueError(f"未知的段帧位：{role!r}（应为 first/last）") from exc
    return (
        f"这张图是一段 AI 视频执行段的{label}静态画面（{moment}）。"
        "请精确描述画面内容：地点与环境、人物（性别/年龄/发型/服装颜色款式/姿态）、"
        "关键动物与道具、光线方向与色调、构图（景别与主体位置）。"
        "只描述图中真实可见的内容，不要臆测或补充不存在的细节。用中文输出，3-5 句话。"
    )


def describe_segment_frame(path: str | Path, role: str) -> str | None:
    """读一张段首/末帧图（issue #56 AC1），返回画面描述；失败返回 None。

    失败**显式警告**并返回 None——语义层照旧把「读图失败」并排摆出来由人判，绝不伪造
    读图记录、也不因此跳过这一层（缺一半的并排看起来像「没问题」）。

    ``role`` 取值错**响**（ValueError）而不是降级：那是编程错误，不是「量不出来」。
    """
    label = _SEGMENT_FRAME_LABELS.get(str(role))
    if label is None:
        raise ValueError(f"未知的段帧位：{role!r}（应为 first/last）")
    return _read_frame_or_none(
        path, segment_frame_prompt(role), label=f"段{label[0]}",
        on_failure="本次没有可对照的画面描述。")


# ---------------------------------------------------------------------------
# 桥接帧：从执行输出剥尾帧（cv2，无外部 ffmpeg 依赖）
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# 执行段首/末帧：剥帧（cv2，无外部 ffmpeg 依赖）与读图（issue #56）
#
# 与桥接链的 ``extract_last_frame`` 共用同一套 Windows 解码/落盘规避（非 ASCII 路径
# 先复制到临时目录、imencode+tofile 落盘），差别在**取哪一帧**：桥接帧要锐利
# （那张图当下一段的 first_frame 参考、不进成片时间线），语义核验要**真实的首帧**
# ——挑一张更锐利的邻近帧就是另一个时刻的画面，判据当场错位。
# ---------------------------------------------------------------------------

@contextmanager
def _open_capture(video_path: str | Path):
    """打开视频供解码；含非 ASCII 路径时先复制到系统临时目录（cv2 打不开那种路径）。

    ``yield`` 出来的是已打开的 ``cv2.VideoCapture``，退出时释放句柄并清掉临时副本。
    """
    import cv2  # noqa: PLC0415 - 懒加载重依赖
    import shutil  # noqa: PLC0415
    import tempfile  # noqa: PLC0415

    source = Path(video_path)
    if not source.is_file():
        raise FileNotFoundError(
            f"视频文件不存在：{source}"
            "（提示：粘贴路径时不要带首尾引号；含空格的路径也无需加引号）"
        )
    decode_source = source
    temp_dir = None
    try:
        str(source).encode("ascii")
    except UnicodeEncodeError:
        temp_dir = Path(tempfile.mkdtemp(prefix="h3-bridge-"))
        temp_copy = temp_dir / ("source" + source.suffix.lower())
        shutil.copy2(source, temp_copy)
        decode_source = temp_copy

    try:
        capture = cv2.VideoCapture(str(decode_source))
        if not capture.isOpened():
            raise ValueError(f"无法打开视频（编码不被支持或文件损坏）：{source}")
        try:
            yield capture
        finally:
            capture.release()
    finally:
        if temp_dir is not None:
            shutil.rmtree(temp_dir, ignore_errors=True)


def _write_png(frame, target: Path, source: Path) -> Path:
    """把一帧写成 PNG（Windows 非 ASCII 路径走 imencode + tofile，绕开 cv2.imwrite 的编码坑）。"""
    import cv2  # noqa: PLC0415

    target.parent.mkdir(parents=True, exist_ok=True)
    ok, buffer = cv2.imencode(".png", frame)
    if not ok:
        raise ValueError(f"帧编码为 PNG 失败：{source}")
    buffer.tofile(str(target))
    return target


def extract_first_frame(video_path: str | Path, output_path: str | Path | None = None) -> Path:
    """剥出视频的**第一帧**（0.00s）并写为 PNG（issue #56 AC1：本段开场画面）。

    **不做锐度优选**（与 ``extract_last_frame`` 的区别，也是它存在的理由）：语义层
    核验判的是「本段开场瞬间的画面是否达到声明的 start_state」，挑一张更锐利的邻近帧
    就是另一个时刻的画面——判据会错位，而且错得看不出来。
    """
    source = Path(video_path)
    with _open_capture(source) as capture:
        ok, frame = capture.read()
    if not ok or frame is None:
        raise ValueError(f"能打开但一帧都没读到（可能文件损坏）：{source}")
    target = Path(output_path) if output_path else source.with_suffix(".first-frame.png")
    return _write_png(frame, target, source)


def extract_last_frame(video_path: str | Path, output_path: str | Path | None = None, *,
                       sharpest: bool = True) -> Path:
    """剥出视频尾部的一帧并写为 PNG，两种取帧口径（issue #98，父 spec #92）。

    - ``sharpest=True``（默认，**旧口径一个字不改**）：取最后 5 帧候选里按 Laplacian
      方差最锐利的一张；全部低于阈值时回退最后一帧并打印警告。多参语义层的段末帧
      用它（``wizard._capture_ref_segment_frames``），判的是「接近尾部的画面」，挑一张
      更锐利的邻近帧不改判据。
    - ``sharpest=False``（首尾帧链的桥接帧）：**恒取末帧**，锐度只降级为**告警**。

    为什么首尾帧链不能用「最锐利」选帧（spec #92 实测 100.9/100.8/100.0/99.8/101.9）：
    段尾落定时最后五帧的锐度是一条平盘，``max()`` 等于随机。桥接帧是**下一段的第一帧
    输入**，成片要在接缝处裁掉下一段的首帧——取到倒数第几帧直接决定「回退了几帧」，
    也决定「裁多少帧」；随机取帧会让这两件事都不可确定（2026-10-26 实测四道接缝里
    三道退回 4 帧 = 0.167s，肉眼不可辨只因段尾被冻住）。

    其余行为两条口径一致：
    - cv2 懒加载：只为段桥接流程引入解码依赖，不拖慢主流程 import。
    - 先按总帧数 seek；seek 失败（部分编码不允许随机访问）则顺序遍历兜底。
    - Windows 非 ASCII 路径用 ``imencode + tofile`` 落盘，绕开 ``cv2.imwrite`` 的编码坑。
    - Windows 下 cv2.VideoCapture 打不开非 ASCII 路径：含非 ASCII 字符时先复制到
      系统临时目录（纯 ASCII 路径）再解码，剥完删除临时文件。
    """
    source = Path(video_path)
    with _open_capture(source) as capture:
        if sharpest:
            frame = _read_sharpest_tail_frame(capture)
        else:
            frame, sharpness = _read_exact_tail_frame(capture)
            _warn_if_blurred_tail(sharpness)
    if frame is None:
        raise ValueError(f"能打开但一帧都没读到（可能文件损坏）：{source}")
    target = Path(output_path) if output_path else source.with_suffix(".last-frame.png")
    return _write_png(frame, target, source)


def _read_last_frame(capture):
    """从已打开的 VideoCapture 取最后一帧；seek 失败时顺序遍历兜底。"""
    import cv2  # noqa: PLC0415

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
    return frame


# 尾帧锐度优选：生成末帧常有编码压缩伪影/边缘抖动，剥作下一段首帧参考时
# 会导致人物识别失败。剥最后 N 帧选最锐利的；全低于阈值则回退最后一帧并告警。
_TAIL_FRAME_CANDIDATES = 5
_MIN_SHARPNESS = 20.0  # Laplacian 方差阈值（低于视为运动模糊/压缩糊）


def _frame_sharpness(frame) -> float:
    """Laplacian 方差：值越高边缘越锐利。"""
    import cv2  # noqa: PLC0415

    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    return float(cv2.Laplacian(gray, cv2.CV_64F).var())


def _read_exact_tail_frame(capture):
    """从已打开的 VideoCapture 取**最后一帧**，连同它的锐度一起返回（issue #98）。

    锐度不参与选帧，只供调用方决定要不要告警——这就是首尾帧链要的「恒取末帧」。
    """
    frame = _read_last_frame(capture)
    if frame is None:
        return None, 0.0
    return frame, _frame_sharpness(frame)


def _warn_if_blurred_tail(sharpness: float) -> None:
    """末帧锐度低于阈值只说一句、**不换帧**（spec #92）。

    旧口径在这里会退回「最锐利的那张」，但那等于偷偷换掉了下一段的首帧输入；本口径
    把「这一帧可能不可靠」当信息交给用户（他可以选择重跑本段），而不是替他换一帧。
    """
    if sharpness >= _MIN_SHARPNESS:
        return
    print(
        f"[警告] 本段末可能有运动模糊（末帧锐度 {sharpness:.1f}，低于阈值 "
        f"{_MIN_SHARPNESS:g}），下一段识别可能不稳；桥接图照旧用末帧——"
        "若下一段人物识别失败，请重跑本段"
    )


def _read_sharpest_tail_frame(capture):
    """从已打开的 VideoCapture 取最后 N 帧中最锐利的一帧（**默认口径**）。

    读者是多参语义层的段末帧（以及任何「接近尾部的画面就行」的场合）；首尾帧链的
    桥接帧**不再走这里**（issue #98 起改走 ``_read_exact_tail_frame``）——那一路要的
    是确定的那一帧，因为成片要在接缝处裁掉它。seek 失败或总帧数未知时回退单帧策略
    （``_read_last_frame``）。
    """
    import cv2  # noqa: PLC0415

    total = int(capture.get(cv2.CAP_PROP_FRAME_COUNT))
    if total <= 1:
        return _read_last_frame(capture)
    start = max(0, total - _TAIL_FRAME_CANDIDATES)
    candidates: list[tuple[float, object]] = []  # (sharpness, frame)
    capture.set(cv2.CAP_PROP_POS_FRAMES, start)
    while True:
        ok, candidate = capture.read()
        if not ok or candidate is None:
            break
        candidates.append((_frame_sharpness(candidate), candidate))
    if not candidates:
        return _read_last_frame(capture)
    best_sharpness, best_frame = max(candidates, key=lambda item: item[0])
    if best_sharpness < _MIN_SHARPNESS:
        last_frame = candidates[-1][1]
        print(
            f"[警告] 尾帧候选（最后 {len(candidates)} 帧）锐度均低于阈值 "
            f"{_MIN_SHARPNESS:g}（最高 {best_sharpness:.1f}），可能整体处于运动模糊；"
            "已回退取最后一帧，若下一段人物识别失败请重生成"
        )
        return last_frame
    return best_frame

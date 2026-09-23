"""产物载荷清单：从产物 ``.png`` 的 ``prompt`` chunk 提出「实际跑了什么仪器」，并校验仪器还在。

**为什么需要它。** 产物元数据里模型只记**文件名**，而文件名是可变引用：2026-09-23
17:25–17:46 用户把这套 LoRA 全部重下（``8step_v1.0_comfyui`` → ``8step_v1.0_768p_comfyui``；
``4step_v1.1_768p`` → ``4step_v1.2_768p``），当天 16:10 之前所有跑次引用的文件在本机
**已解析不到任何东西，而没有任何地方会报错或警告**。后果：``00003``（2.78）/ ``00005``
（0.596）那对质检最严的对照（同 seed、同图、同文本、同步数，载荷只差 ``lora_name``）
**永久不可复测**，那个 4.7 倍差异再也无法收口。

同型事故当天还踩了两次，都靠人肉核对元数据才发现：``00007`` 实际用的是 ``ref2v_8step``
LoRA（用户以为是与 ``00006`` 并列的重复跑），``00012`` 的 seed 是跑完随机化留下的值、
不是工作流里钉死的那个——「只动步数」的对照根本没做成。

这与项目里「规则在、接线不在」同型：纪律（只信**同仪器**上的 >2× 效应，ADR 0002）
在，仪器身份校验不在。本工具就是那根接线。

**为什么哈希只算模型文件。** 模型动辄 1–2 GB，输入图只有几百 KB 且元数据里记的是
可解析的名字；给每张输入图算哈希只会让工具慢到没人愿意跑。输入图用大小 + mtime 指认。

用法（仓库根，venv python）::

    ./.venv/Scripts/python.exe tools/loadout_manifest.py <产物.png> [<产物.png> ...]

ComfyUI 根目录按 ``--comfy-root`` → ``H3_COMFY_ROOT`` → 本机默认值的顺序解析。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from PIL import Image

# 本机便利默认值（这台机器上 ComfyUI 的根）。换机器用 --comfy-root 或 H3_COMFY_ROOT。
# 与 tests/conftest.py 的 H3_COMFY_OUTPUT 同一套约定：机器专属路径给默认值 + 环境变量覆盖。
DEFAULT_COMFY_ROOT = Path(os.environ.get("H3_COMFY_ROOT") or r"D:/Comfyui/ComfyUI")

# 模型引用：元数据里的字段名 → (种类, ComfyUI 下的子目录)。
# 字段名逐字取自 2026-09-23 真产物的 prompt chunk，不是猜的。
MODEL_DIRS: dict[str, tuple[str, str]] = {
    "lora_name": ("lora", "models/loras"),
    "unet_name": ("unet", "models/diffusion_models"),
    "clip_name": ("clip", "models/text_encoders"),
    "vae_name": ("vae", "models/vae"),
}
IMAGE_DIRS: dict[str, tuple[str, str]] = {"image": ("image", "input")}

# 认不出的加载器（如 MiniMaxH3PDDAccApply 那类加速节点）按后缀兜底：只要值是模型文件名
# 就必须进清单——「认不出」不能变成「不校验」。
MODEL_SUFFIXES: tuple[str, ...] = (
    ".safetensors", ".sft", ".ckpt", ".pt", ".pth", ".gguf", ".bin",
)

AB_DISCIPLINE = (
    "拿历史产物做 A/B 之前先跑本工具。引用已消失时，那批历史数字只能当孤立观测，"
    "不得与新数字拼进同一张表——仪器都不是同一个，「差」就不是效应。"
)


@dataclass(frozen=True)
class Sampling:
    """采样配置。``strength_model`` 收成元组：一个载荷可以挂多个 LoRA 节点。"""

    steps: int | None = None
    scheduler: str | None = None
    denoise: float | None = None
    sampler_name: str | None = None
    noise_seed: int | None = None
    strength_model: tuple[float, ...] = ()


@dataclass(frozen=True)
class ModelRef:
    """产物元数据里的一个文件引用。``name`` 保持原样（含 ``MiniMax-H3\\`` 前缀与反斜杠）。"""

    kind: str
    name: str
    node: str


@dataclass
class Loadout:
    """一份产物的载荷清单。``error`` 非空表示这份产物根本读不出载荷。"""

    path: Path
    sampling: Sampling = field(default_factory=Sampling)
    refs: list[ModelRef] = field(default_factory=list)
    error: str | None = None


@dataclass
class ResolvedRef:
    """一条引用在本机解析的结果。``path is None`` 即解析不到。"""

    kind: str
    name: str
    node: str
    path: Path | None = None
    size: int = 0
    mtime: str | None = None
    sha256: str | None = None
    note: str | None = None

    @property
    def resolved(self) -> bool:
        return self.path is not None


@dataclass
class Report:
    """一次多产物校验的结果汇总。"""

    root: Path
    loadouts: list[Loadout] = field(default_factory=list)
    resolved: list[ResolvedRef] = field(default_factory=list)
    missing: list[ResolvedRef] = field(default_factory=list)
    root_missing: bool = False

    @property
    def unreadable(self) -> list[Loadout]:
        return [item for item in self.loadouts if item.error]

    @property
    def ok(self) -> bool:
        return not (self.root_missing or self.missing or self.unreadable)


def extract_loadout(path: Path | str) -> Loadout:
    """读一份产物 PNG 的 ``prompt`` chunk，提出采样配置与全部文件引用。

    读不到 chunk 时返回带 ``error`` 的 Loadout，**不返回空清单**——空清单会被下游
    当成「这份产物没有任何引用」，那是静默失败，正是本票要消灭的东西。
    """
    path = Path(path)
    try:
        with Image.open(path) as image:
            raw = image.info.get("prompt")
    except OSError as exc:
        return Loadout(path=path, error=f"打不开产物：{exc}")
    if not raw:
        return Loadout(path=path, error="产物里没有 prompt chunk（不是 ComfyUI 产物？）")
    try:
        payload = json.loads(raw) if isinstance(raw, str) else raw
    except json.JSONDecodeError as exc:
        return Loadout(path=path, error=f"prompt chunk 不是合法 JSON：{exc}")
    if not isinstance(payload, dict):
        return Loadout(path=path, error="prompt chunk 不是节点字典")

    steps = scheduler = denoise = sampler = seed = None
    strengths: list[float] = []
    refs: list[ModelRef] = []

    for node in payload.values():
        if not isinstance(node, dict):
            continue
        class_type = str(node.get("class_type") or "")
        inputs = node.get("inputs")
        if not isinstance(inputs, dict):
            continue
        if class_type == "BasicScheduler":
            steps = _as_int(inputs.get("steps"))
            scheduler = _as_str(inputs.get("scheduler"))
            denoise = _as_float(inputs.get("denoise"))
        elif class_type == "KSamplerSelect":
            sampler = _as_str(inputs.get("sampler_name"))
        elif class_type == "RandomNoise":
            seed = _as_int(inputs.get("noise_seed"))
        if class_type == "LoraLoaderModelOnly":
            strength = _as_float(inputs.get("strength_model"))
            if strength is not None:
                strengths.append(strength)
        for key, value in inputs.items():
            if not isinstance(value, str) or not value:
                continue
            if key in IMAGE_DIRS:
                refs.append(ModelRef(kind=IMAGE_DIRS[key][0], name=value, node=class_type))
            elif key in MODEL_DIRS:
                refs.append(ModelRef(kind=MODEL_DIRS[key][0], name=value, node=class_type))
            elif key.endswith("_name") or value.lower().endswith(MODEL_SUFFIXES):
                if value.lower().endswith(MODEL_SUFFIXES):
                    refs.append(ModelRef(kind="其它模型", name=value, node=class_type))

    return Loadout(
        path=path,
        sampling=Sampling(
            steps=steps, scheduler=scheduler, denoise=denoise,
            sampler_name=sampler, noise_seed=seed, strength_model=tuple(strengths),
        ),
        refs=refs,
    )


def resolve_refs(loadouts: list[Loadout], root: Path | str,
                 want_hash: bool = True, progress=None) -> Report:
    """把每份载荷里的引用解析到 ComfyUI 的实际目录，给出大小/时间/（模型文件的）SHA256。

    ``progress`` 是可选回调：哈希是按 GB 计的（本机 UNET 19.5GB、CLIP 13.9GB），
    必须能看见在动，否则就是一次静默的几十分钟卡死——那正是本工具要消灭的形态。
    """
    root = Path(root)
    report = Report(root=root, loadouts=list(loadouts), root_missing=not root.is_dir())
    for loadout in loadouts:
        if loadout.error:
            continue
        for ref in loadout.refs:
            item = _resolve_one(ref, root, want_hash=want_hash and ref.kind != "image",
                                progress=progress)
            (report.resolved if item.resolved else report.missing).append(item)
    return report


def render_report(report: Report) -> str:
    """把报告渲染成给人看的文本。解析不到的引用显式列在末尾，不藏在行间。"""
    lines: list[str] = []
    lines.append("=" * 72)
    lines.append("产物载荷清单（issue #18）")
    lines.append("=" * 72)
    state = "不存在" if report.root_missing else "存在"
    lines.append(f"ComfyUI 根：{report.root}  （{state}）")
    if report.root_missing:
        lines.append("  ⚠ 根目录不存在——这**不等于**文件被删。先确认根目录对不对，"
                     "再谈引用失效。")

    for index, loadout in enumerate(report.loadouts, start=1):
        lines.append("")
        lines.append(f"#{index} {loadout.path}")
        if loadout.error:
            lines.append(f"    ✗ 读不出载荷：{loadout.error}")
            continue
        lines.append("    采样  " + _render_sampling(loadout.sampling))
        mine = [r for r in report.resolved + report.missing if _belongs(r, loadout)]
        bad = [r for r in mine if not r.resolved]
        lines.append(f"    引用  {len(mine)} 个，解析 {len(mine) - len(bad)} 个"
                     + (f"，**{len(bad)} 个解析不到**" if bad else "，全部解析"))
        for item in mine:
            lines.append("      " + _render_ref(item))

    if report.missing:
        lines.append("")
        lines.append("!" * 72)
        lines.append(f"[引用失效] {len(report.missing)} 个文件在本机解析不到：")
        for item in report.missing:
            lines.append(f"  ✗ {item.kind:6s} {item.name}   （引用它的节点：{item.node}）")
        lines.append("  这批产物所依赖的仪器已不在本机——它们的数字只能当孤立观测。")
        lines.append("!" * 72)

    if report.unreadable:
        lines.append("")
        lines.append(f"[读不出载荷] {len(report.unreadable)} 份产物：")
        for loadout in report.unreadable:
            lines.append(f"  ✗ {loadout.path}：{loadout.error}")

    lines.append("")
    lines.append("-" * 72)
    lines.append(f"结论：{'全部引用解析成功' if report.ok else '有引用解析不到（见上）'}")
    lines.append(f"纪律：{AB_DISCIPLINE}")
    lines.append("-" * 72)
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="loadout_manifest.py",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "产物载荷清单：从产物 .png 的 prompt chunk 提出实际执行的采样配置与模型文件\n"
            "引用，逐个解析到 ComfyUI 的实际目录，给出大小与 SHA256；解析不到的文件名\n"
            "显式报红并以非零退出码结束。\n\n"
            "为什么要跑它：产物元数据里的模型只是一个**文件名**，即一个可变引用。文件被\n"
            "换版或删除后，元数据记录的名字在本机解析不到任何东西，而没有任何地方会报错。\n"
            "2026-09-23 用户把整套 LoRA 重下，导致 00003/00005 那对最好的对照永久作废。\n\n"
            "注意：模型文件才默认算哈希（1–2 GB × N 会慢）；输入图只记大小 + mtime。"
        ),
        epilog=(
            f"纪律：{AB_DISCIPLINE}\n"
            "本条也写在输出的尾注里——它必须跟着工具走，不能只活在文档里。"
        ),
    )
    parser.add_argument("artifacts", nargs="+", metavar="产物.png",
                        help="一个或多个 ComfyUI 产物 PNG")
    parser.add_argument("--comfy-root", default=None,
                        help=f"ComfyUI 根目录（默认 {DEFAULT_COMFY_ROOT}；也可用环境变量 H3_COMFY_ROOT）")
    parser.add_argument("--no-hash", action="store_true",
                        help="跳过模型文件哈希（只解析路径与大小，快得多）")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = Path(args.comfy_root) if args.comfy_root else DEFAULT_COMFY_ROOT
    loadouts = [extract_loadout(Path(p)) for p in args.artifacts]
    report = resolve_refs(loadouts, root, want_hash=not args.no_hash,
                          progress=_progress_to_stderr)
    print(render_report(report))
    return 0 if report.ok else 1


def _progress_to_stderr(message: str) -> None:
    """哈希进度走 stderr，让 stdout 保持是一份干净、可重定向的报告。"""
    print(message, file=sys.stderr, flush=True)


# --- 内部工具 -----------------------------------------------------------

def _belongs(item: ResolvedRef, loadout: Loadout) -> bool:
    return any(r.name == item.name and r.node == item.node for r in loadout.refs)


def _resolve_one(ref: ModelRef, root: Path, *, want_hash: bool,
                 progress=None) -> ResolvedRef:
    candidates: list[Path] = []
    if ref.kind == "image":
        candidates.append(root / IMAGE_DIRS["image"][1] / _relative(ref.name))
    else:
        known = next((sub for kind, sub in MODEL_DIRS.values() if kind == ref.kind), None)
        if known:
            candidates.append(root / known / _relative(ref.name))
        else:
            # 认不出种类的模型（PDDAcc 等）：在四个模型目录里找，找到就记下在哪
            candidates.extend(root / sub / _relative(ref.name)
                              for _, sub in MODEL_DIRS.values())
    for candidate in candidates:
        if candidate.is_file():
            stat = candidate.stat()
            if want_hash and progress is not None:
                progress(f"  哈希 {candidate.name}（{_human(stat.st_size)}）…")
            return ResolvedRef(
                kind=ref.kind, name=ref.name, node=ref.node, path=candidate,
                size=stat.st_size,
                mtime=datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
                sha256=_sha256(candidate) if want_hash else None,
                note=_note(candidate, root),
            )
    return ResolvedRef(kind=ref.kind, name=ref.name, node=ref.node)


def _relative(name: str) -> Path:
    """元数据里的名字用反斜杠（``MiniMax-H3\\foo.safetensors``），归一成相对路径。"""
    return Path(name.replace("\\", "/"))


def _note(path: Path, root: Path) -> str | None:
    """认不出种类时说明是在哪个目录找到的——不让人猜。"""
    try:
        rel = path.relative_to(root)
    except ValueError:
        return None
    return str(rel.parent).replace("\\", "/")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _human(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f}{unit}" if unit != "B" else f"{int(value)}B"
        value /= 1024
    return f"{value:.1f}TB"


def _as_int(value: object) -> int | None:
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _as_float(value: object) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _as_str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _render_sampling(sampling: Sampling) -> str:
    parts = [
        f"steps={sampling.steps}",
        f"scheduler={sampling.scheduler}",
        f"denoise={sampling.denoise}",
        f"sampler={sampling.sampler_name}",
        f"seed={sampling.noise_seed}",
    ]
    if sampling.strength_model:
        parts.append("strength=" + ",".join(f"{s:g}" for s in sampling.strength_model))
    return "  ".join(parts)


def _render_ref(item: ResolvedRef) -> str:
    if not item.resolved:
        return f"✗ [{item.kind:6s}] {item.name}  → 解析不到（节点 {item.node}）"
    digest = f"sha256={item.sha256[:16]}…" if item.sha256 else "sha256=跳过"
    where = f"（{item.note}）" if item.note and item.kind == "其它模型" else ""
    return (f"✓ [{item.kind:6s}] {item.name}  → {item.path}{where}  "
            f"{_human(item.size)}  mtime={item.mtime}  {digest}")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())

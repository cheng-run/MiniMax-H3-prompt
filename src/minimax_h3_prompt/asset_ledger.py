"""素材台账（asset ledger）：素材交回入库与登记清单（issue #43）。

用户把生成好的素材图（及按需的素材视频/音频）交回系统入库，这里登记**素材台账**：
语义名、类型（图/视频/音频）、官方编号（`<Picture N>` 等）、文件路径与 SHA256，
落盘为持久化产物 ``asset-ledger.json``。记 SHA256 是为了对抗**文件名可变引用**——
只记文件名的话，素材换版后旧产物永久不可复测且无处报错（LoRA 同款教训，issue #18）；
同名素材换版（SHA256 变化）必须**可发现并报出、不静默**。

与 ``tools/ledger.py`` 的审计台账（镜头链路账本）**不是一回事**，勿混用——本模块
名与产物名都带 asset 前缀就是为了不撞那个词（CONTEXT.md「闸门遭遇记录」条目同款警惕）。

三条既定规则落在这里：

1. **正本复制入库**：交回的文件复制进 ``<目录>/assets/``（只复制不移动，同
   ``frames/`` 先例），台账记**目录内正本**的相对路径（POSIX 斜杠）。生成目录因此
   自包含——把整个目录搬走、拷给别人，台账与文件仍然对得上账，这才叫「旧产物可复测」。
2. **编号一经分配不再变**（官方 label continuity：「Once a reference label is
   assigned… it keeps the same number」）：图的编号取自参考素材登记（``ref_asset_registry``，
   剧本出场序，编号即叙事顺序）；视频/音频按型内递增，批内按剧本出场序、无出场排尾。
   后续批次的新名字补号不许挤占已分配的号。
3. **换版要人点头**（向导）：同名同型再交回而 SHA256 变了＝换版，默认**保留原登记**并
   报出新旧哈希，调用方显式同意才替换；替换记录追加进 ``replacements``（旧 SHA256
   不消失，旧产物才可复测）。
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable, Sequence

from .brief_parser import RefItem
from .task_package import resolve_input_path

LEDGER_FILENAME = "asset-ledger.json"
# 版本号**不因可选字段而升**（issue #52 的外部成本）：读回是宽松的（缺键＝0）、写回是
# 完整的，升版会让盘上所有旧台账在读到的那一刻报「不支持的 schema_version」——为了
# 两个可选字段把旧产物的可复测性砸掉，正好违背台账存在的理由。
_SCHEMA_VERSION = "asset-ledger.v1"

# 参考素材三型：对齐官方标签（Picture / Video / Audio），值是机器口径，渲染再转中文。
KIND_PICTURE = "picture"
KIND_VIDEO = "video"
KIND_AUDIO = "audio"
KINDS = (KIND_PICTURE, KIND_VIDEO, KIND_AUDIO)
KIND_LABELS = {KIND_PICTURE: "图", KIND_VIDEO: "视频", KIND_AUDIO: "音频"}
_OFFICIAL_LABEL = {KIND_PICTURE: "Picture", KIND_VIDEO: "Video", KIND_AUDIO: "Audio"}

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def format_tag(kind: str, number: int) -> str:
    """官方参考标签，如 ``<Picture 1>``——提示词与注入卡的指称口径。

    **唯一可写来源**（ADR 0008 同款纪律）：台账条目、换版事件、对账差异与向导问句
    都从它取形，几处各拼一遍就是第二份可写来源（issue #40 先例）。
    """
    return f"<{_OFFICIAL_LABEL[kind]} {number}>"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _require_relative_asset_path(value: str, label: str) -> str:
    """台账 ``path`` 只收相对正本路径：绝对路径/``..`` 逃逸会让「目录自包含」落空，
    还会让换版清理误删目录外文件——精确数据坏了要响（session_store 画布同口径）。"""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} 不能为空")
    pure = PurePosixPath(value)
    if pure.is_absolute() or ".." in pure.parts:
        raise ValueError(f"{label} 必须是相对路径且不含 ..：{value!r}")
    return str(pure)


def _require_external_cost(seconds: Any, attempts: Any) -> tuple[float, int]:
    """校验外部生成成本（ComfyUI 出图耗时秒数/次数），issue #52。

    两个字段都是**可选**：没登记就是 0（缺省不填不报错）。登记了就得是像样的数：
    负数、字符串数字、bool、小数次数一律报错——静默强转/截断＝数据被改而不报错，
    与画布、视频输出三元组的严格读回同一条纪律。
    """
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or seconds < 0:
        raise ValueError(f"外部生成耗时必须是 >=0 的秒数：{seconds!r}")
    if isinstance(attempts, bool) or not isinstance(attempts, int) or attempts < 0:
        raise ValueError(f"外部生成次数必须是 >=0 的整数：{attempts!r}")
    return float(seconds), attempts


@dataclass(frozen=True)
class AssetRecord:
    """台账一条登记：五字段是票面契约，``source`` 是交回时的原始路径（溯源）。

    ``external_seconds`` / ``external_attempts`` 是**可选**的外部生成成本（issue #52）：
    ComfyUI 出图耗时（秒）与出图次数。系统看不到外部生成，这两个数只能由人登记；
    0 ＝ 没登记（缺省不填不报错）。账要能回答「这一版花了多少」，否则「跑完的成本」
    只算得清 token、算不清图。
    """

    name: str
    kind: str
    number: int
    path: str
    sha256: str
    source: str = ""
    external_seconds: float = 0.0
    external_attempts: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("语义名不能为空")
        if self.kind not in KINDS:
            raise ValueError(f"参考素材类型无效：{self.kind!r}（只认 {', '.join(KINDS)}）")
        if isinstance(self.number, bool) or not isinstance(self.number, int) or self.number < 1:
            raise ValueError(f"官方编号必须是正整数：{self.number!r}")
        object.__setattr__(self, "path", _require_relative_asset_path(self.path, "path"))
        if not _SHA256_RE.fullmatch(str(self.sha256)):
            raise ValueError(f"sha256 必须是 64 位小写 SHA-256：{self.sha256!r}")
        seconds, attempts = _require_external_cost(self.external_seconds, self.external_attempts)
        object.__setattr__(self, "external_seconds", seconds)
        object.__setattr__(self, "external_attempts", attempts)

    @property
    def tag(self) -> str:
        """官方标签，如 ``<Picture 1>``（取形自 ``format_tag``，唯一可写来源）。"""
        return format_tag(self.kind, self.number)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> AssetRecord:
        if not isinstance(raw, dict):
            raise ValueError("台账条目必须是对象")
        unknown = set(raw) - {
            "name", "kind", "number", "path", "sha256", "source",
            "external_seconds", "external_attempts",
        }
        if unknown:
            raise ValueError(f"台账条目包含未知字段：{', '.join(sorted(unknown))}")
        return cls(
            name=str(raw.get("name", "")),
            kind=str(raw.get("kind", "")),
            number=raw.get("number", 0),
            path=str(raw.get("path", "")),
            sha256=str(raw.get("sha256", "")),
            source=str(raw.get("source", "")),
            # 旧台账没有这两个键 ＝ 没登记（缺省 0），不报错
            external_seconds=raw.get("external_seconds", 0),
            external_attempts=raw.get("external_attempts", 0),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind,
            "number": self.number,
            "path": self.path,
            "sha256": self.sha256,
            "source": self.source,
            "external_seconds": self.external_seconds,
            "external_attempts": self.external_attempts,
        }


@dataclass(frozen=True)
class Replacement:
    """换版事件：同名同型参考素材交回了不同内容（SHA256 变化）。已替换的追加进台账
    ``replacements`` 留史，旧 SHA256 不消失（旧产物可复测）；被拒的也带全对照材料。

    old_*/new_* 四字段**平铺是落盘 schema 的形状**（同 ``AssetInput``、帧入库清单的
    平铺口径）——拆成 old/new 嵌套对会把 JSON 契约改丑，人对账时也不便逐行 diff。
    """

    name: str
    kind: str
    number: int
    old_path: str
    old_sha256: str
    new_path: str
    new_sha256: str
    detected_at: str = ""

    @property
    def tag(self) -> str:
        return format_tag(self.kind, self.number)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Replacement:
        if not isinstance(raw, dict):
            raise ValueError("换版记录必须是对象")
        unknown = set(raw) - {
            "name", "kind", "number", "old_path", "old_sha256",
            "new_path", "new_sha256", "detected_at",
        }
        if unknown:
            raise ValueError(f"换版记录包含未知字段：{', '.join(sorted(unknown))}")
        return cls(
            name=str(raw.get("name", "")),
            kind=str(raw.get("kind", "")),
            number=raw.get("number", 0),
            old_path=str(raw.get("old_path", "")),
            old_sha256=str(raw.get("old_sha256", "")),
            new_path=str(raw.get("new_path", "")),
            new_sha256=str(raw.get("new_sha256", "")),
            detected_at=str(raw.get("detected_at", "")),
        )

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass(frozen=True)
class Drift:
    """对账差异：已登记文件与记录的 SHA256 不符（``actual`` 为 None ＝ 文件已不在）。"""

    name: str
    kind: str
    number: int
    path: str
    recorded_sha256: str
    actual_sha256: str | None

    @property
    def tag(self) -> str:
        return format_tag(self.kind, self.number)


@dataclass(frozen=True)
class AssetLedger:
    """素材台账：当前登记 + 换版史。条目按（类型, 编号）规范排序——台账本身就是
    「编号即叙事顺序」的可核对呈现（AC4）。"""

    entries: tuple[AssetRecord, ...] = ()
    replacements: tuple[Replacement, ...] = ()

    def __post_init__(self) -> None:
        ordered = tuple(sorted(self.entries, key=lambda e: (KINDS.index(e.kind), e.number)))
        object.__setattr__(self, "entries", ordered)

    def get(self, name: str, kind: str) -> AssetRecord | None:
        for item in self.entries:
            if item.name == name and item.kind == kind:
                return item
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": _SCHEMA_VERSION,
            "entries": [item.to_dict() for item in self.entries],
            "replacements": [item.to_dict() for item in self.replacements],
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> AssetLedger:
        if not isinstance(raw, dict):
            raise ValueError("素材台账顶层必须是对象")
        unknown = set(raw) - {"schema_version", "entries", "replacements"}
        if unknown:
            raise ValueError(f"素材台账包含未知字段：{', '.join(sorted(unknown))}")
        if raw.get("schema_version") != _SCHEMA_VERSION:
            raise ValueError(f"不支持的素材台账 schema_version：{raw.get('schema_version')!r}")
        entries = raw.get("entries")
        replacements = raw.get("replacements")
        if not isinstance(entries, list) or not isinstance(replacements, list):
            raise ValueError("素材台账的 entries/replacements 必须是数组")
        return cls(
            entries=tuple(AssetRecord.from_dict(item) for item in entries),
            replacements=tuple(Replacement.from_dict(item) for item in replacements),
        )


def load_asset_ledger(directory: str | Path) -> AssetLedger:
    """读回台账；文件不存在 ＝ 空台账。文件存在但坏了要**响**（它是精确数据）。"""
    target = Path(directory) / LEDGER_FILENAME
    if not target.is_file():
        return AssetLedger()
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"素材台账文件损坏：{target}（{exc}）") from exc
    return AssetLedger.from_dict(raw)


def save_asset_ledger(directory: str | Path, ledger: AssetLedger) -> Path:
    target = Path(directory) / LEDGER_FILENAME
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(ledger.to_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )
    return target


def record_external_cost(directory: str | Path, name: str, kind: str, *,
                         seconds: float = 0, attempts: int = 0) -> AssetRecord:
    """给**已登记**的那一版挂外部生成成本（ComfyUI 出图耗时/次数，issue #52）。

    这是事后补登记的口（向导审查菜单第 5 项走它）：图已经在盘上、成本才想起来记。
    没有这条登记就报错、不造空壳行——成本必须挂在某个正本上才有意义（「产物要么有货
    要么不存在」）。只改这一条的这两个字段，其余字段与换版史原样保留。
    """
    base = Path(directory)
    ledger = load_asset_ledger(base)
    entry = ledger.get(name, kind)
    if entry is None:
        raise ValueError(f"素材台账里没有这条登记，无法登记外部成本：{kind}/{name!r}")
    seconds, attempts = _require_external_cost(seconds, attempts)
    updated = replace(entry, external_seconds=seconds, external_attempts=attempts)
    save_asset_ledger(base, AssetLedger(
        entries=tuple(
            updated if (e.name, e.kind) == (name, kind) else e for e in ledger.entries
        ),
        replacements=ledger.replacements,
    ))
    return updated


def remove_asset(directory: str | Path, name: str, kind: str) -> AssetRecord | None:
    """撤下一条登记（连同会话目录里的正本复制）；没有该登记返回 None。

    台账的增删只由本模块做——调用方拿 ``entries`` 自己重建是「第二处可写来源」，
    换版记录（``replacements``）也会跟着丢。撤下是**人的显式决定**（issue #50
    「显式放弃」问过人才撤），换版史照旧留着。
    """
    base = Path(directory)
    ledger = load_asset_ledger(base)
    entry = ledger.get(name, kind)
    if entry is None:
        return None
    kept = tuple(e for e in ledger.entries if (e.name, e.kind) != (name, kind))
    save_asset_ledger(base, AssetLedger(entries=kept, replacements=ledger.replacements))
    copied = base / entry.path
    if copied.is_file():
        copied.unlink()
    return entry


@dataclass(frozen=True)
class AssetSubmission:
    """一次交回：语义名 + 类型 + 用户交回的文件路径。

    ``external_seconds`` / ``external_attempts`` 是这次交回**顺带登记**的外部生成成本
    （issue #52，可选，0 ＝ 没登记）。放在交回里而不是另开一次写台账的动作：成本说的
    就是「这一版图是怎么来的」，与正本、SHA256 同一次落盘才不会被劈成两本账。
    """

    name: str
    kind: str
    source: str
    external_seconds: float = 0.0
    external_attempts: int = 0

    def __post_init__(self) -> None:
        seconds, attempts = _require_external_cost(self.external_seconds, self.external_attempts)
        object.__setattr__(self, "external_seconds", seconds)
        object.__setattr__(self, "external_attempts", attempts)

    @property
    def external_cost_given(self) -> bool:
        """这次交回有没有带着成本（0 是「未登记」，不是「登记成 0」）。"""
        return bool(self.external_seconds or self.external_attempts)


@dataclass(frozen=True)
class IntakeReport:
    """入库结果的外部行为全集：登记了什么、换版了什么、对账差异、还缺什么。

    ``replaced`` 与 ``refused`` 都是**换版事件**（新旧路径与新旧 SHA256 齐全）：
    「报出」必须给出对照材料，只有名字的清单查无对照（AC3 的「不静默」）。前者已
    替换进台账，后者保留原登记。``missing`` 只对**参考素材登记**（图）算：视频/音频
    是按需附加，没有「应交未交」；``unlisted`` 是交回了却不在参考素材登记里的语义名
    （编号补尾，同 #41 复审 F5 的点名口径）。
    """

    ledger: AssetLedger
    registered: tuple[AssetRecord, ...] = ()
    unchanged: tuple[str, ...] = ()
    replaced: tuple[Replacement, ...] = ()
    refused: tuple[Replacement, ...] = ()
    drift: tuple[Drift, ...] = ()
    missing: tuple[str, ...] = ()
    unlisted: tuple[str, ...] = ()


def verify_asset_ledger(directory: str | Path) -> tuple[Drift, ...]:
    """对账：逐条重算正本 SHA256，与记录不符（或文件已不在）即差异——LoRA 教训
    的正面解：文件名一样内容换了，这里看得见。"""
    base = Path(directory)
    ledger = load_asset_ledger(base)
    drift: list[Drift] = []
    for entry in ledger.entries:
        target = base / entry.path
        if not target.is_file():
            drift.append(Drift(entry.name, entry.kind, entry.number, entry.path,
                               entry.sha256, None))
            continue
        actual = _sha256_file(target)
        if actual != entry.sha256:
            drift.append(Drift(entry.name, entry.kind, entry.number, entry.path,
                               entry.sha256, actual))
    return tuple(drift)


def _registry_numbers(registry: Sequence[RefItem] | None) -> dict[str, int]:
    """参考素材登记：语义名 → <Picture N>。同名重复会让编号对不上号，当场报。"""
    numbers: dict[str, int] = {}
    for item in registry or ():
        if item.name in numbers and numbers[item.name] != item.picture:
            raise ValueError(f"参考素材登记语义名重复且编号不一致：{item.name!r}")
        numbers[item.name] = item.picture
    return numbers


def _next_number(taken: set[int]) -> int:
    return max(taken, default=0) + 1


def intake_assets(
    directory: str | Path,
    submissions: Iterable[AssetSubmission],
    *,
    registry: Sequence[RefItem] | None = None,
    script: str = "",
    on_replace: Callable[[AssetRecord, AssetRecord], bool] | None = None,
) -> IntakeReport:
    """交回入库：复制正本 → 算 SHA256 → 登记/换版 → 落盘台账。

    编号规则（编号即叙事顺序，label continuity 保证已分配的号不改）：

    - **图**：取参考素材登记的编号（``ref_asset_registry``，剧本出场序）；
      不在参考素材登记里的语义名补到型内末尾并**点名报告**（同 #41 复审 F5：
      不静默丢弃，也不硬塞叙事位次）。
    - **视频/音频**：型内递增；同批多个时按剧本出场序（``script`` 里语义名首次出现
      位置），无出场的排尾（稳定按交回次序）。

    同名同型再交回：内容没变 ＝ 重报不变；SHA256 变了 ＝ **换版**，默认保留原登记
    （``on_replace`` 不给或返回 False），返回 True 才替换并追加换版记录。``on_replace``
    为 None ＝ 非交互场景（CLI 重跑）接受替换，但报告里照报不误。
    """
    base = Path(directory)
    assets_dir = base / "assets"
    assets_dir.mkdir(parents=True, exist_ok=True)
    ledger = load_asset_ledger(base)
    numbers = _registry_numbers(registry)
    drift = verify_asset_ledger(base)

    items = list(submissions)
    # 视频/音频的批内次序：剧本出场序、无出场排尾（稳定＝交回次序）。
    def _batch_key(pair: tuple[int, AssetSubmission]) -> tuple[int, int]:
        at = script.find(pair[1].name) if pair[1].name else -1
        return (0, at) if at >= 0 else (1, pair[0])

    ordered_video_audio = sorted(
        ((i, s) for i, s in enumerate(items) if s.kind != KIND_PICTURE), key=_batch_key,
    )

    registered: list[AssetRecord] = []
    unchanged: list[str] = []
    replaced: list[Replacement] = []
    refused: list[Replacement] = []
    ledger_mut = ledger

    def _target_name(number: int, kind: str, source: Path) -> str:
        suffix = source.suffix.lower() or ".bin"
        return str(PurePosixPath("assets") / f"{kind}-{number:02d}{suffix}")

    def _apply(sub: AssetSubmission) -> None:
        nonlocal ledger_mut
        # brief 解析把 Windows 路径归一成 /mnt/<盘>/ 写法，交回路径要过同一解析口
        # （同帧图入库），否则 CLI 上带路径的参考素材一律「文件不存在」。
        source = resolve_input_path(sub.source)
        if not source.is_file():
            raise FileNotFoundError(f"交回的文件不存在：{source}")
        digest = _sha256_file(source)
        existing = ledger_mut.get(sub.name, sub.kind)

        def _cost(prev: AssetRecord | None) -> tuple[float, int]:
            """这一版的外部成本：这次交回带了就用它，没带就沿用上一版那笔。

            沿用而不是清零（issue #52）：换版大多是「重掷一张」，成本是这张图的知识；
            清零会让「已经登记过」这件事静默消失，而账本里最难发现的就是少了一笔。
            """
            if sub.external_cost_given:
                return sub.external_seconds, sub.external_attempts
            if prev is not None:
                return prev.external_seconds, prev.external_attempts
            return 0.0, 0

        if existing is not None:
            if existing.sha256 == digest:
                seconds, attempts = _cost(existing)
                if (seconds, attempts) != (existing.external_seconds, existing.external_attempts):
                    updated = replace(existing, external_seconds=seconds,
                                      external_attempts=attempts)
                    ledger_mut = AssetLedger(
                        entries=tuple(
                            updated if (e.name, e.kind) == (sub.name, sub.kind) else e
                            for e in ledger_mut.entries
                        ),
                        replacements=ledger_mut.replacements,
                    )
                    unchanged.append(f"{sub.name}（{existing.tag}，外部成本已更新）")
                    return
                unchanged.append(f"{sub.name}（{existing.tag}）")
                return
            # 换版：默认保留原登记（不静默接受），调用方点头才替换；两头都进报告。
            seconds, attempts = _cost(existing)
            candidate = AssetRecord(
                name=sub.name, kind=sub.kind, number=existing.number,
                path=_target_name(existing.number, sub.kind, source),
                sha256=digest, source=str(source),
                external_seconds=seconds, external_attempts=attempts,
            )
            event = Replacement(
                name=sub.name, kind=sub.kind, number=existing.number,
                old_path=existing.path, old_sha256=existing.sha256,
                new_path=candidate.path, new_sha256=digest, detected_at=_now(),
            )
            accept = True if on_replace is None else on_replace(existing, candidate)
            if not accept:
                refused.append(event)
                return
            ledger_mut = AssetLedger(
                entries=tuple(
                    candidate if (e.name, e.kind) == (sub.name, sub.kind) else e
                    for e in ledger_mut.entries
                ),
                replacements=ledger_mut.replacements + (event,),
            )
            replaced.append(event)
            if existing.path != candidate.path:
                old_file = base / existing.path
                if old_file.is_file():
                    old_file.unlink()
            shutil.copy2(source, base / candidate.path)
            return
        # 新登记：图优先要参考素材登记的编号（剧本出场序），视频/音频型内递增。
        if sub.kind == KIND_PICTURE and sub.name in numbers:
            number = numbers[sub.name]
        else:
            taken = {e.number for e in ledger_mut.entries if e.kind == sub.kind}
            if sub.kind == KIND_PICTURE:
                taken |= set(numbers.values())
            number = _next_number(taken)
        entry = AssetRecord(
            name=sub.name, kind=sub.kind, number=number,
            path=_target_name(number, sub.kind, source),
            sha256=digest, source=str(source),
            external_seconds=sub.external_seconds,
            external_attempts=sub.external_attempts,
        )
        ledger_mut = AssetLedger(
            entries=ledger_mut.entries + (entry,),
            replacements=ledger_mut.replacements,
        )
        registered.append(entry)
        shutil.copy2(source, base / entry.path)

    # 图先入（编号来自参考素材登记、与批内次序无关），视频/音频按批内出场序补号。
    for sub in items:
        if sub.kind == KIND_PICTURE:
            _apply(sub)
    for _i, sub in ordered_video_audio:
        _apply(sub)

    present = {(e.name) for e in ledger_mut.entries if e.kind == KIND_PICTURE}
    missing = tuple(
        item.name for item in registry or ()
        if item.name not in present
    )
    # 交回了却不在参考素材登记里的图语义名：编号补尾（上面），这里点名（同 #41 复审 F5 口径）。
    unlisted = tuple(dict.fromkeys(
        sub.name for sub in items
        if sub.kind == KIND_PICTURE and sub.name not in numbers
    ))
    report = IntakeReport(
        ledger=ledger_mut,
        registered=tuple(registered),
        unchanged=tuple(unchanged),
        replaced=tuple(replaced),
        refused=tuple(refused),
        drift=drift,
        missing=missing,
        unlisted=unlisted,
    )
    # 空手不落账：什么都没登记、又没有旧账 ＝ 不造空壳台账文件（产物要么有货要么不存在）。
    if registered or replaced or (base / LEDGER_FILENAME).is_file():
        save_asset_ledger(base, ledger_mut)
    return report


# ---------------------------------------------------------------------------
# 出站渲染：向导与 CLI 共用（两处各写一份渲染就是第二份可写来源，issue #40 先例）
# ---------------------------------------------------------------------------

def render_ledger(ledger: AssetLedger) -> str:
    """素材台账权威清单（用户故事 12）：五字段 + 换版史 + 登记过的外部成本。

    终端 SHA256 取前 16 位（同 loadout_manifest 口径），**全量在 JSON**——显示层
    不是对照物，要逐位核对读 ``asset-ledger.json``。外部生成成本只在登记过时出现
    （0 ＝ 没登记，见 issue #52）。
    """
    counts = {kind: sum(1 for e in ledger.entries if e.kind == kind) for kind in KINDS}
    head = (f"[素材台账] {len(ledger.entries)} 条"
            f"（图 {counts[KIND_PICTURE]} · 视频 {counts[KIND_VIDEO]} · 音频 {counts[KIND_AUDIO]}）")
    lines = [head]
    if not ledger.entries:
        lines.append("  （空）")
    for entry in ledger.entries:
        line = (f"  {entry.tag} {KIND_LABELS[entry.kind]} · {entry.name} · "
                f"{entry.path} · sha256={entry.sha256[:16]}…")
        # 外部生成成本只在登记过时才占版面（0 ＝ 没登记）：没登记的行不该多一截噪音
        cost = []
        if entry.external_seconds:
            cost.append(f"{entry.external_seconds:g}s")
        if entry.external_attempts:
            cost.append(f"{entry.external_attempts} 次")
        if cost:
            line += f" · 外部成本 {'/'.join(cost)}"
        lines.append(line)
    for record in ledger.replacements:
        lines.append(f"  [换版史] {record.name} {record.tag}："
                     f"{record.old_sha256[:16]}… → {record.new_sha256[:16]}…"
                     + (f"（{record.detected_at}）" if record.detected_at else ""))
    return "\n".join(lines)


def render_report(report: IntakeReport) -> str:
    """入库报告出站：这次交回发生了什么。三条「不静默」都在这里被看见——
    换版对照（新旧哈希）、未交回点名、未登记点名。"""
    lines: list[str] = []
    if report.registered:
        lines.append(f"[入库] 新登记 {len(report.registered)} 条：")
        lines.extend(f"  {e.tag} {KIND_LABELS[e.kind]} · {e.name} · sha256={e.sha256[:16]}…"
                     for e in report.registered)
    if report.unchanged:
        lines.append(f"[入库] 内容未变（重交免重复登记）：{'、'.join(report.unchanged)}")
    for record in report.replaced:
        lines.append(f"[换版] {record.name} {record.tag}："
                     f"旧 sha256={record.old_sha256[:16]}… → 新 sha256={record.new_sha256[:16]}…（已替换）")
    for record in report.refused:
        lines.append(f"[换版] {record.name} {record.tag}："
                     f"旧 sha256={record.old_sha256[:16]}… → 新 sha256={record.new_sha256[:16]}…"
                     f"（未替换，保留原登记）")
    for item in report.drift:
        actual = f"{item.actual_sha256[:16]}…" if item.actual_sha256 else "文件已不在"
        lines.append(f"[对账] {item.name} {item.tag} 正本与记录不符："
                     f"记录 {item.recorded_sha256[:16]}…，盘上 {actual}")
    if report.missing:
        lines.append(f"[待交回] 参考素材登记里还没交回：{'、'.join(report.missing)}")
    if report.unlisted:
        lines.append(f"[提示] 以下语义名不在参考素材登记中，编号排在型内最后（参考素材照常登记）："
                     f"{'、'.join(report.unlisted)}")
    return "\n".join(lines)


__all__ = [
    "KINDS", "KIND_PICTURE", "KIND_VIDEO", "KIND_AUDIO", "KIND_LABELS",
    "LEDGER_FILENAME", "format_tag",
    "AssetRecord", "AssetLedger", "AssetSubmission", "Replacement", "Drift",
    "IntakeReport",
    "intake_assets", "load_asset_ledger", "remove_asset", "record_external_cost",
    "save_asset_ledger",
    "verify_asset_ledger",
    "render_ledger", "render_report",
]

"""导演台交付（issue #108，设计 §3.9）：成片记录、最终包归档、改动比对。

全部段通过可证层之后，用户在导演台里合并成片、导出**最终包**，把两者交给项目：

- 成片只记路径与帧数（``director/final-film.json``）——片子是用户导出的，项目不拼；
- 最终包归档成 ``director/final.mmxpack.zip`` 并**现算** ``final.sha256``；
- 读最终包的 ``timeline.json`` 逐段比对提示词，把用户在 UI 里改过的段补记成
  ``event=rewrite`` 账行（写口仍只有 ``ref_segments.log_segment_rewrite_round``）。

三条容易写错的契约（都来自设计 §3.2 / §3.9 与插件勘察 §8）：

1. **成片帧数取 Σ 导出帧**，不是时间线的 Σ 声明帧。两套坐标差
   ``12×有引导段数``：拿声明坐标记成片，片子长度与账永远对不上，且没有任何一处会报错。
2. **join 键是 ``segments[].id``**，不是数组下标。UI 里可以删段、重排，下标一挪，
   改过的词就会被记到别的段上（账上看起来完全正常）。
3. **``fl2v`` 还要比 ``shots[].prompt``**：UI 以 shots 为准、后端读 segments，只比一处
   会漏掉「用户在 fl2v 界面里改的词」。**帧数不比**——最终包的 ``frameCount`` 会被 UI
   归一化改过（研究笔记第 10 条），改过不代表用户手改过提示词。

失败一律 ``ValueError`` 点名（票面 AC4）：包缺失 / 不是 zip / ``timeline.json`` 损坏 /
段数与初始包不一致 / 成片不存在。**先校验再落盘**——落半份交付比没落更坏：用户会以为
已经交付过。
"""
from __future__ import annotations

import json
import shutil
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..asset_ledger import sha256_file
from ..ref_segments import log_segment_rewrite_round
from .frames import DirectorPlan
from .pack import TIMELINE_FILENAME, pack_dir_for
from .planning import DIRECTOR_DIRNAME

# ``GENxxx/director/`` 下的三个交付产物（设计 §3.9）。
FINAL_FILM_FILENAME = "final-film.json"
FINAL_PACK_FILENAME = "final.mmxpack.zip"
FINAL_SHA_FILENAME = "final.sha256"

# ``rewrite`` 行的 ``feedback``：说明这次改动**发生在哪里**。与多参那本账同形
# （``ref_segments.log_segment_rewrite_round``），事后按这一句就能分清「项目里改的」
# 与「导演台 UI 里改的」。
REWRITE_FEEDBACK = "导演台 UI 内修改"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def director_dir(session_dir: str | Path) -> Path:
    """``GENxxx/director/``——导演台所有产物的落点（与段表同一个目录常量）。"""
    return Path(session_dir) / DIRECTOR_DIRNAME


def final_film_path(session_dir: str | Path) -> Path:
    return director_dir(session_dir) / FINAL_FILM_FILENAME


def final_pack_path(session_dir: str | Path) -> Path:
    return director_dir(session_dir) / FINAL_PACK_FILENAME


def final_sha256_path(session_dir: str | Path) -> Path:
    return director_dir(session_dir) / FINAL_SHA_FILENAME


def initial_timeline_path(session_dir: str | Path) -> Path:
    """初始包的时间线：``director/pack/timeline.json``（打包时落盘，``pack.py`` 写）。"""
    return pack_dir_for(session_dir) / TIMELINE_FILENAME


# ---------------------------------------------------------------------------
# 读包
# ---------------------------------------------------------------------------

def read_initial_timeline(session_dir: str | Path) -> dict[str, Any]:
    """读初始包的 ``timeline.json``。读不回来就抛——比对的「改前」词只住在这里。"""
    path = initial_timeline_path(session_dir)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"初始包的 timeline.json 读不出：{exc}（{path}）") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"初始包的 timeline.json 不是合法 JSON：{exc}（{path}）") from exc
    if not isinstance(data, dict):
        raise ValueError(f"初始包的 timeline.json 顶层必须是对象（{path}）")
    return data


def read_pack_timeline(pack_zip: str | Path) -> dict[str, Any]:
    """读导演台导出的最终包里的 ``timeline.json``（包根，插件 ``pack.py:1033``）。

    三种坏法各点名一次：文件不在／不是 zip／包里没有时间线或它不是合法 JSON。
    合成一句「最终包不可用」会把用户唯一能动手的线索（到底哪一步坏了）抹掉。
    """
    path = Path(pack_zip)
    if not path.is_file():
        raise ValueError(f"最终包不存在：{path}")
    if not zipfile.is_zipfile(path):
        raise ValueError(
            f"最终包不是 zip：{path}（导演台「导出最终包」产出的应是 .mmxpack.zip）")
    try:
        with zipfile.ZipFile(path) as archive:
            try:
                raw = archive.read(TIMELINE_FILENAME)
            except KeyError:
                raise ValueError(
                    f"最终包缺 {TIMELINE_FILENAME}：{path}（不是导演台导出的包？）") from None
    except zipfile.BadZipFile as exc:
        raise ValueError(f"最终包读不开：{exc}（{path}）") from exc
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(
            f"最终包的 {TIMELINE_FILENAME} 读不出：{exc}（{path}）") from exc
    if not isinstance(data, dict):
        raise ValueError(f"最终包的 {TIMELINE_FILENAME} 顶层必须是对象（{path}）")
    return data


# ---------------------------------------------------------------------------
# 比对提示词（设计 §3.9 / 插件勘察 §8 的键路径表）
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PromptChange:
    """一段在导演台 UI 里被改过的词：``segment`` 是**初始包里的段号**（1 基）。"""

    segment: int
    segment_id: str
    prompt_before: str
    prompt_after: str
    source: str  # "segments"（后端真读的那份）／"shots"（fl2v 界面那份）


def _segment_rows(timeline: dict[str, Any], where: str) -> list[dict]:
    rows = timeline.get("segments")
    if not isinstance(rows, list) or not rows:
        raise ValueError(f"{where}的 timeline.json 缺 segments 数组（或为空）")
    for position, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            raise ValueError(f"{where}的 timeline.json 第 {position} 段不是对象")
    return rows


def _prompt_by_id(rows: list[dict], where: str) -> dict[str, tuple[int, str]]:
    """``{段 id: (数组位置, 提示词)}``——join 键只用 id（研究笔记 §8「段身份」）。

    查重是硬要求：同 id 两行说明这份包不该被当成段表用（重复 id 时「改的是哪一段」
    没有答案），此时静默取最后一个等于替用户猜。
    """
    prompts: dict[str, tuple[int, str]] = {}
    for position, row in enumerate(rows, 1):
        segment_id = row.get("id")
        if not isinstance(segment_id, str) or not segment_id.strip():
            raise ValueError(
                f"{where}的 timeline.json 第 {position} 段没有 id：导演台比对以 "
                "segments[].id 为 join 键（插件勘察 §8），缺了就无法判断哪个差异属于"
                "哪一段——按数组下标比会在 UI 删段/重排时把词记到别的段上")
        if segment_id in prompts:
            raise ValueError(
                f"{where}的 timeline.json 里 id {segment_id!r} 重复：join 键必须唯一")
        prompt = row.get("prompt")
        if prompt is None:
            prompt = ""
        elif not isinstance(prompt, str):
            raise ValueError(
                f"{where}的 timeline.json 第 {position} 段的 prompt 不是字符串："
                f"{prompt!r}")
        prompts[segment_id] = (position, prompt)
    return prompts


def _require_same_ids(initial: dict, final: dict, what: str) -> None:
    missing = sorted(set(initial) - set(final))
    unknown = sorted(set(final) - set(initial))
    if missing or unknown:
        details = []
        if missing:
            details.append(f"最终包缺 {', '.join(missing)}")
        if unknown:
            details.append(f"最终包多出初始包没有的 {', '.join(unknown)}")
        raise ValueError(
            f"最终包与初始包的{what} id 对不上（{'；'.join(details)}）："
            "两份包不是同一次打包/同一条时间线，比对结果不能记进这本账")


def compare_prompts(initial: dict[str, Any],
                    final: dict[str, Any]) -> list[PromptChange]:
    """逐段比对两份 ``timeline.json``，返回在 UI 里**改过的段**（段序 = 初始包顺序）。

    - 段数、id 集合必须一致：不一致抛错，不静默跳过（票面 AC4）。
    - **不读 ``group.json``**：插件只在 ``timeline.json`` 该段词为空时才拿它补
      （``pack.py:980-1016``，插件勘察 §8），拿它当比对源会造出用户没做过的改动。
    - 每段最多一行：``segments`` 先比（后端真正会跑的那份词）；segments 没变而
      ``shots`` 变了（fl2v 界面以 shots 为准）才记 shots 的值。**一处编辑只记一行**——
      两处都记会让一本账里同一段同一次改动出现两轮。
    """
    initial_rows = _segment_rows(initial, "初始包")
    final_rows = _segment_rows(final, "最终包")
    if len(final_rows) != len(initial_rows):
        raise ValueError(
            f"最终包段数 {len(final_rows)} 与初始包 {len(initial_rows)} 不一致："
            "段数变了说明用户在 UI 里增删过段，比对的「改前」已经不存在")
    initial_prompts = _prompt_by_id(initial_rows, "初始包")
    final_prompts = _prompt_by_id(final_rows, "最终包")
    _require_same_ids(initial_prompts, final_prompts, "段")

    initial_shots: dict[str, tuple[int, str]] = {}
    final_shots: dict[str, tuple[int, str]] = {}
    initial_shot_rows = initial.get("shots")
    if isinstance(initial_shot_rows, list) and initial_shot_rows:
        final_shot_rows = final.get("shots")
        if not isinstance(final_shot_rows, list) or not final_shot_rows:
            raise ValueError(
                "初始包是 fl2v（timeline.shots 非空），最终包却没有 shots 数组："
                "UI 以 shots 为准，缺了它就分不清用户改的是哪一份词")
        if len(final_shot_rows) != len(initial_shot_rows):
            raise ValueError(
                f"最终包的 shots 段数 {len(final_shot_rows)} 与初始包 "
                f"{len(initial_shot_rows)} 不一致")
        initial_shots = _prompt_by_id(initial_shot_rows, "初始包的 shots")
        final_shots = _prompt_by_id(final_shot_rows, "最终包的 shots")
        _require_same_ids(initial_shots, final_shots, " shots ")

    changes: list[PromptChange] = []
    for segment_id, (position, before) in initial_prompts.items():
        after = final_prompts[segment_id][1]
        if after != before:
            changes.append(PromptChange(
                segment=position, segment_id=segment_id,
                prompt_before=before, prompt_after=after, source="segments"))
            continue
        if segment_id in initial_shots:
            shot_before = initial_shots[segment_id][1]
            shot_after = final_shots[segment_id][1]
            if shot_after != shot_before:
                changes.append(PromptChange(
                    segment=position, segment_id=segment_id,
                    prompt_before=shot_before, prompt_after=shot_after,
                    source="shots"))
    return changes


# ---------------------------------------------------------------------------
# 落盘：成片记录与最终包归档
# ---------------------------------------------------------------------------

def record_final_film(session_dir: str | Path, *, plan: DirectorPlan,
                      film_path: str | Path) -> Path:
    """写 ``director/final-film.json``：路径 + **成片坐标**帧数 + 段数 + 时间。"""
    film = Path(film_path)
    if not film.is_file():
        raise ValueError(f"成片 mp4 不存在：{film}（导演台合并导出之后再交付）")
    # 成片坐标 ＝ Σ 导出帧（含每段有引导的 12 帧）；**不是**时间线的 Σ 声明帧
    # （设计 §3.2：两套坐标差 12×有引导段数）。
    total_frames = sum(segment.export_frames for segment in plan.segments)
    record = {
        "path": str(film.resolve()),
        "total_frames": total_frames,
        "segments": len(plan.segments),
        "at": _now(),
    }
    path = final_film_path(session_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2),
                    encoding="utf-8")
    return path


def archive_final_pack(session_dir: str | Path,
                       *, pack_zip: str | Path) -> tuple[Path, str]:
    """把最终包复制成 ``director/final.mmxpack.zip`` 并写 ``final.sha256``。

    校验值**复制之后现算**、不收参数（设计 §3.9）：收参数就多一个「传错了也没人知道」
    的口子；算复制后的那一份，顺带证明复制本身没坏。文件形如 ``sha256sum`` 的一行。
    """
    source = Path(pack_zip)
    if not source.is_file():
        raise ValueError(f"最终包不存在：{source}")
    if not zipfile.is_zipfile(source):
        raise ValueError(
            f"最终包不是 zip：{source}（导演台「导出最终包」产出的应是 .mmxpack.zip）")
    target = final_pack_path(session_dir)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    digest = sha256_file(target)
    final_sha256_path(session_dir).write_text(
        f"{digest}  {target.name}\n", encoding="utf-8")
    return target, digest


# ---------------------------------------------------------------------------
# 编排
# ---------------------------------------------------------------------------

@dataclass
class DeliveryReport:
    """一次交付的结果：写到哪里、成片多长、补了几行改写账。"""

    film_path: Path
    total_frames: int
    segments: int
    film_record: Path
    pack_path: Path
    pack_sha256: str
    sha_path: Path
    changes: tuple[PromptChange, ...] = field(default_factory=tuple)

    def render(self) -> str:
        lines = [
            "",
            "交付完成：",
            f"  成片：{self.film_path}（{self.total_frames} 帧 / {self.segments} 段）"
            f" → 记录 {self.film_record}",
            f"  最终包：{self.pack_path}",
            f"  校验值：{self.pack_sha256} → {self.sha_path}",
        ]
        if self.changes:
            lines.append(f"  改写账：补记 {len(self.changes)} 行（{REWRITE_FEEDBACK}）")
            for change in self.changes:
                lines.append(
                    f"    - 第 {change.segment} 段（{change.segment_id}，"
                    f"{change.source}）")
        else:
            lines.append("  改写账：最终包的段词与初始包一致，没有补记任何改写行。")
        return "\n".join(lines)


def deliver(session_dir: str | Path, *, plan: DirectorPlan,
            film_path: str | Path, pack_zip: str | Path) -> DeliveryReport:
    """交付全流程：校验 → 归档 → 记成片 → 补改写行。

    **先校验完再落盘**：任何一项不合格都不该在会话目录里留下半份交付产物
    （用户下次重跑看到 final-film.json 会以为已经交付过）。
    """
    film = Path(film_path)
    if not film.is_file():
        raise ValueError(f"成片 mp4 不存在：{film}（导演台合并导出之后再交付）")

    initial = read_initial_timeline(session_dir)
    final = read_pack_timeline(pack_zip)

    initial_count = len(_segment_rows(initial, "初始包"))
    if initial_count != len(plan.segments):
        raise ValueError(
            f"初始包段数 {initial_count} 与段表 {len(plan.segments)} 段不一致："
            f"{initial_timeline_path(session_dir)} 不是这一次交付打的包，"
            "拿它当「改前」会算出假的改写行")

    changes = compare_prompts(initial, final)

    pack_path, digest = archive_final_pack(session_dir, pack_zip=pack_zip)
    record_path = record_final_film(session_dir, plan=plan, film_path=film)
    for change in changes:
        # 唯一写口：不在这里另写一份追加逻辑（账行形态只住 ref_segments）。
        log_segment_rewrite_round(
            session_dir, segment=change.segment, feedback=REWRITE_FEEDBACK,
            prompt_before=change.prompt_before, prompt_after=change.prompt_after)

    record = json.loads(record_path.read_text(encoding="utf-8"))
    return DeliveryReport(
        film_path=film.resolve(),
        total_frames=int(record["total_frames"]),
        segments=int(record["segments"]),
        film_record=record_path,
        pack_path=pack_path,
        pack_sha256=digest,
        sha_path=final_sha256_path(session_dir),
        changes=tuple(changes),
    )

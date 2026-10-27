"""首尾帧链段产出账（issue #94）：``segments/shot-ledger.jsonl``。

**它解决什么**：逐段陪跑原来只留 ``segments/progress.json`` 的 ``done=N``——那个数
不记段视频路径，于是「哪几段过了」可答、「每段的视频在哪」不可答，成片拼接（#96）
拼不出来；重跑本段换了新视频后也无从取最新那份。本账一段一行（段号／起止秒／视频
绝对路径／SHA256／写入时间），因此成为**「第几段过了」的唯一真源**。

**与多参段产出账的关系**：同形不同路——多参那本（``ref_segments.SEGMENT_LEDGER_FILENAME``）
一本账装三类行、按 ``event`` 取数；首尾帧链这本只有一类「这段过了」的行，另有
``segments/gate-log.jsonl`` 记锚帧人裁的每次遭遇。文件名沿用既有 ``shot-NN`` 命名体系
（``shot-NN.md`` 是段提示词），不制造新旧两套。

**旧会话**：``progress.json`` 停写、降级为兼容读口——旧会话没有新账时由向导回退读它，
并当面说破「旧账没记段视频，拼接不可用」（见 ``ui.wizard._segment_progress``）。

纯件：不碰终端、不做流程决策，问答节奏住在 ``ui.wizard``。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from .asset_ledger import sha256_file

# 段产出账文件名。与 ``segments/shot-NN.md`` 同一套编号（execution segment，不是镜头），
# 放在 ``segments/`` 下与 plan.json / gate-log.jsonl 同址。
SHOT_LEDGER_FILENAME = "shot-ledger.jsonl"


def _now() -> str:
    """UTC ISO 时间戳（与多参段产出账同形，便于两条路线的账用同一套解析）。"""
    return datetime.now(timezone.utc).isoformat()


def shot_ledger_path(directory: str | Path) -> Path:
    return Path(directory) / "segments" / SHOT_LEDGER_FILENAME


def append_shot_output(
    directory: str | Path,
    *,
    segment: int,
    start_s: float | None,
    end_s: float | None,
    video_path: str | Path,
) -> Path:
    """追加一行：一段通过**可证层交接校验**与**锚帧语义校验**之后的产出。

    调用方只在流程真的越过这一段时调用——被拦下、判重跑、停下的轮次都不落这一行，
    「行在＝这段过了」这条判据才有意义（与多参 ``log_segment_output`` 同一条纪律）。

    记 **SHA256** 是为了对抗**文件名可变引用**：只记文件名的话，段视频换版/改名后
    旧产物永久不可复测且无处报错（素材台账同款理由）。摘要**在写入时现算**，不另收
    参数——由调用方传 sha 就多一个「传错了也没人知道」的口子。

    ``start_s`` / ``end_s`` 允许为 None：机械回退路径没有 plan 层，段的起止秒是从
    整片提示词的时间戳反推的，缺失时如实记 null，不编一个数（编出来的时间会比缺失
    更坏：它看起来像可证事实）。
    """
    path = shot_ledger_path(directory)
    record = {
        "segment": int(segment),
        # 段边界按契约是整数秒（规划器与机械拆分都取整），落盘统一成整数便于对账。
        "start_s": None if start_s is None else int(round(float(start_s))),
        "end_s": None if end_s is None else int(round(float(end_s))),
        "video": str(Path(video_path).resolve()),
        "sha256": sha256_file(Path(video_path)),
        "at": _now(),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    return path


def read_shot_ledger(directory: str | Path) -> list[dict]:
    """逐行读账；文件不在＝空账，坏行**响**（ValueError）。

    坏行响不吞：追加账是「这段到底交回过什么」的凭据，静默跳过坏行等于进度缺页无人知
    （与 ``ref_segments.read_segment_ledger`` 同一条口径）。
    """
    path = shot_ledger_path(directory)
    if not path.is_file():
        return []
    rows: list[dict] = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"段产出账第 {lineno} 行不是合法 JSON：{path}") from exc
        if not isinstance(record, dict):
            raise ValueError(f"段产出账第 {lineno} 行不是对象：{path}")
        rows.append(record)
    return rows


def passed_shot_rows(directory: str | Path) -> dict[int, dict]:
    """账里通过的段 → ``{段号: 行}``；**同一段多行取最后一行**。

    最后一行＝最近一次通过的那份视频：重跑本段换了新视频后，成片要用**新**的，
    不是第一次通过时的那份（多参 ``passed_segment_rows`` 同一条读取语义）。

    行**写全了**才算「这段过了」：缺视频/摘要/时间或段号读不出的行不能拿来跳段——
    拿它续跑等于放行一段没人生成过的视频（2026-10-21 多参复审实测过剥掉 video 后
    续跑照跳的形态）。
    """
    rows: dict[int, dict] = {}
    path = shot_ledger_path(directory)
    for index, record in enumerate(read_shot_ledger(directory), 1):
        try:
            number = int(record.get("segment"))
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"段产出账的行读不出段号（第 {index} 行，{path}）：{record.get('segment')!r}"
            ) from exc
        missing = [key for key in ("video", "sha256", "at")
                   if not str(record.get(key) or "").strip()]
        if missing:
            raise ValueError(
                f"段产出账第 {number} 段的通过行缺字段 {missing}（{path}）："
                "行在＝这段过了这条判据建立在行写全了之上，残缺的行不能拿来跳段")
        video = str(record.get("video"))
        if not Path(video).is_absolute():
            # 字段定义是「视频**绝对路径**」：相对路径换个工作目录就指到另一个文件，
            # 拼接时会把一条错片子悄悄拼出来（#96 的 SHA256 核对也救不了路径错）。
            raise ValueError(
                f"段产出账第 {number} 段的视频不是绝对路径（{video}）：{path}")
        rows[number] = record
    return rows


def validate_shot_progress(passed: dict[int, dict], *, total_segments: int) -> None:
    """账与段表必须对得上：段号越界、通过行跳号 → 抛 ``ValueError``。

    为什么不静默从零开始：按错的账续跑会**跳过从没做过的段**（跳号时），或者把
    另一次拆段的产物当成这一次的进度（越界时）。两种情况都会让用户拿到一条没人
    生成过中间段的片子，而且全程不报错。
    """
    total = int(total_segments)
    extra = sorted(number for number in passed if not 1 <= number <= total)
    if extra:
        raise ValueError(
            f"段产出账里有段表之外的段号 {extra}（段表只有 {total} 段）："
            "账与 segments/plan.json 不是同一次拆段的产物，不能拿它续跑")
    numbers = sorted(passed)
    if numbers != list(range(1, len(numbers) + 1)):
        raise ValueError(
            f"段产出账的通过行跳号了：{numbers}——陪跑是严格顺序的，中间缺段说明"
            "账被删过或改过；按它续跑会跳过从没做过的段")


def read_shot_progress(directory: str | Path, *, total_segments: int) -> dict[int, dict]:
    """续跑要的「已通过段」→ ``{段号: 行}``；账与段表**对不上就抛**。

    ``total_segments`` 是段表的段数（调用方手上就有 ``len(plans)``）。账文件**不在**
    不算错：追加账在「一段都没通过」时本来就不存在，那种情况返回空表、从第 1 段起。
    """
    passed = passed_shot_rows(directory)
    validate_shot_progress(passed, total_segments=total_segments)
    return passed


# 旧进度文件（只读兼容口）。**新交付口要把它和账一起挪开**：留着它，新一轮会被
# ``_segment_progress`` 判成「旧会话」——于是新账永远不写、成片永远拼不出来。
LEGACY_PROGRESS_FILENAME = "progress.json"


def legacy_progress_path(directory: str | Path) -> Path:
    return Path(directory) / "segments" / LEGACY_PROGRESS_FILENAME


def rotate_shot_progress(directory: str | Path) -> list[Path]:
    """把上一次陪跑的进度落盘物（段产出账 ＋ 旧 progress.json）挪成 ``.bak``。

    为什么需要它：``ui.wizard._session_dir`` 只要会话不是 completed 就复用同一个 GEN，
    而用户可以**拒绝续接**、重走一遍流程。那时 ``plan.json``/``shot-NN.md`` 都会被
    新一轮交付整份覆盖，唯独这本账是追加式的、从不清理——不挪开的话，新一次的陪跑会
    读到**上一次**的通过行，把「第 1 段已通过」当成这一轮的进度照跳（多参侧
    2026-10-21 复审实测过同形缺陷，issue #58）。

    ``progress.json`` 一起挪是同一件事的另一半：它是旧会话的判据（无账 + 有它＝旧会话），
    留着它，新一轮交付会被永久当成旧会话。

    挪而不删：旧账是那一次交付的凭据，留着可查；返回被挪走的路径供调用方当面说破。
    """
    moved: list[Path] = []
    for path in (shot_ledger_path(directory), legacy_progress_path(directory)):
        if not path.is_file():
            continue
        target = path.with_name(path.name + ".bak")
        index = 1
        while target.exists():
            index += 1
            target = path.with_name(f"{path.name}.bak{index}")
        path.rename(target)
        moved.append(target)
    return moved

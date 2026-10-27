"""与路线无关的成片拼接核心（issue #93，父 spec #92）。

多参路线（ref2va）与首尾帧链都要把**已验收通过**的各段接成 ``final.mp4``，两边的差别
只有一处：拼接口径。段表覆盖判据、SHA256 核对、成片实测时长的「只报短」核对这三件事
必须**同一份代码**——两条路各写一遍，迟早出现「一份改了口径另一份没改」，而这类漂移
不会让任何用例变红（issue #57 的「各段之和 ＝ 总时长」判据就是这么咬到人的：规划器
契约是「末段可略超 total」，写死成等式的那个口径把全段通过的片子判成了伪产物）。

本模块只装**与路线无关**的编排；找段表、读账、算落点这类「从盘上取输入」的事归各路的
薄包装（``ref_segments.assemble_final_film`` 是多参那一层）。共用件的入参全是数据：
段表、账行、起步总时长、拼接策略、成片落点。
"""
from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Mapping, Sequence

from .asset_ledger import sha256_file
from .segment_planner import SegmentPlan

# 「只报短」容差的兜底帧率：读不到输入段 fps 时才用它。容差本身按**实测 fps** 折算
# 一帧/段（``len(sources) / fps``）——写死 1/24 时，60fps 素材上一帧只有 1/60 秒，
# 每个接缝白得多给 1.5 帧的余量，真短掉几帧会被静默吞掉（2026-10-27 复审实测）。
_FALLBACK_FPS = 24.0

# 帧率一致性的实测容差：同一个帧率在不同容器里读回来的小数位会差一点
# （23.976023976… vs 23.976），而「24 vs 30」这种真不一致差的是整数级。
_FPS_TOLERANCE = 0.01

# 兜底档（裁首帧重编码失败 → 退回朴素拼接）的拼接方式取值。它**不能**与多参的
# ``stream-copy`` 同值：两者产出的片子接缝不一样（一个留重复首帧），同值时用户与
# 事后复测都分不清手里这条是哪一档。
_FALLBACK_HOW = "stream-copy-no-trim"

# 兜底档必须当面说破（spec #92「不许静默降级」）。
_DUPLICATE_SEAM_NOTICE = "接缝保留了重复首帧（裁首帧重编码失败，已退回朴素拼接）。"


class ConcatStrategy(str, Enum):
    """拼接口径。**必须是入参、不许写死在共用件里**（issue #93）。

    两条路线在这件事上本来就不同：多参是「优先无损 stream-copy、失败才重编码」，
    首尾帧链是「裁掉第 2..N 段的首帧再重编码」（接缝处重一帧，见 spec #92）。抽共用件
    时「顺手统一」等于悄悄改变多参的成片行为——所以两档各是一个**可辨识取值**，
    而不是共用件里的一个 if 分支。

    ``TRIM_FIRST_FRAME`` 由 #98 实现（``_produce_trimmed``）：裁掉第 2..N 段的首帧、
    单次 filter 图重编码；重编码那一路挂掉时退回朴素拼接，但结果里必须说破「接缝保留
    了重复首帧」——静默降级等于用户拿到一条接缝抽帧的成片却以为问题在别处。
    """

    STREAM_COPY_FIRST = "stream-copy-first"   # 多参现状：先 stream copy，失败转重编码
    TRIM_FIRST_FRAME = "trim-first-frame"     # 首尾帧链：裁第 2..N 段首帧 + 重编码


@dataclass
class FinalFilm:
    """一次成片拼接的结果。

    ``path`` 为 None ＝ **没产出成片**，``reason`` 说清卡在哪（缺段／账上视频对不上／
    拼接失败）——缺一段的成片时长对不上总时长，是伪产物，比没有更坏（spec #54）。
    """

    path: Path | None
    how: str            # "copy"（段数=1 退化为拷贝改名）/ "stream-copy" / "re-encode"
    total_s: int        # 起步输入的总时长（用户的目标成片时长）
    # 各段计划时长之和。**可以大于 total_s**：规划器允许末段 end 略超 total 留余量
    # （``segment_planner.parse_segment_plan``），所以成片比起步总时长长一点是设计使然，
    # 不是缺陷——判「段表对不对」要用覆盖式判据（``_coverage_problem``），不是相等。
    planned_s: int
    measured_s: float | None
    reason: str = ""
    warnings: list[str] = field(default_factory=list)


def _coverage_problem(plans: Sequence[SegmentPlan], total_s: int) -> str:
    """段表必须无缝覆盖 ``[0, 起步总时长]``；不覆盖就返回一句「哪儿不对」（空串＝没问题）。

    **末段 end 允许略超 total**（规划器留余量：``segment_planner.parse_segment_plan``
    写着「最后一段 end 可以略超 total（留余量）但不许欠」），所以判据是「≥」不是「＝」。
    拿「各段计划时长之和 ＝ 起步总时长」当判据，末段一留余量就会把**全段通过**的片子
    判成「段表与本次生成不是一套」而拒绝出片——同一份契约两处口径，本仓已犯过（issue
    #56 的假 ``SEGMENT_TIMELINE_MISSING`` 就是这个形态；issue #57 复审实测末段超 total
    的规划产物可达）。这正是本票要抽共用件的理由：判据只能有一份。
    """
    if not plans:
        return "段表是空的（segments/plan.json 里没有段）"
    if plans[0].start_s != 0:
        return f"段表不从 0 起（首段从 {plans[0].start_s}s 起）"
    for previous, following in zip(plans, plans[1:]):
        if previous.end_s != following.start_s:
            return (f"段表有缺口/重叠：{previous.start_s}-{previous.end_s}s 之后是 "
                    f"{following.start_s}-{following.end_s}s")
    if plans[-1].end_s < total_s:
        return f"段表只覆盖到 {plans[-1].end_s}s，欠起步总时长 {total_s}s"
    return ""


def assemble_film(*, plans: Sequence[SegmentPlan],
                  rows: Mapping[int, Mapping],
                  total_s: int,
                  out_path: str | Path,
                  strategy: ConcatStrategy,
                  reencode_crf: int,
                  remedy_hint: str | None = None) -> FinalFilm:
    """把**已通过验收**的各段按段序接成成片，返回结果对象（不抛异常）。

    ``rows`` 是段产出账里的**通过行**（多参：``event=output``）；「哪几段过了」取数是
    调用方的事，但**拼不拼由这里说了算**——「未通过即不拼」这条规矩必须住在被拼接的
    那一处，否则它就是一个可以被绕过的调用方约定（本仓「规则在、接线不在」已犯多次）。

    段序取 ``plans``（不是 ``rows`` 的插入顺序、也不是交回顺序）：plan 是段序的唯一真源。

    ``strategy`` 与 ``reencode_crf`` **都必填、都不给默认值**：两个都是「这条路线要什么
    样的成片」的取值（拼接口径 / 重编码画质档）。留默认值等于把「多参用的是哪一档」
    藏进签名——将来谁改默认值，多参的成片行为就静默变了（多参 18 是已交付过的历史值，
    首尾帧链按 spec #92 取 16，由 #98 传）。

    ``remedy_hint`` 是**换版之后的出路**，默认值＝多参今天那句（含 ``ref-concat
    --replace`` 指引，逐字不变）。它必须可改：出路是路线的事——多参有换段拼接命令，
    首尾帧链不提供（spec #92），共用件替它讲一句对自己不存在的命令就是误导。
    """
    planned_s = sum(int(plan.duration_s) for plan in plans)

    # 局部 import：成片这一步才需要 cv2（``video_meta``）与 ffmpeg 封装，模块导入期
    # 不拉进来（与 ``ref_segments`` 既有写法一致——分段主流程不该被这两个重依赖拖慢）。
    from .tools.frame_match import video_meta

    def _refuse(reason: str) -> FinalFilm:
        return FinalFilm(path=None, how="", total_s=int(total_s), planned_s=planned_s,
                         measured_s=None, reason=reason)

    missing = [plan.index + 1 for plan in plans if (plan.index + 1) not in rows]
    if missing:
        return _refuse(
            "第 " + "、".join(str(n) for n in missing)
            + f" 段尚未通过验收：缺一段的成片时长对不上起步总时长 {int(total_s)}s，"
            "是伪产物——不产出成片。")
    problem = _coverage_problem(plans, int(total_s))
    if problem:
        return _refuse(f"{problem}：段表与本次生成不是一套，不产出成片。")

    sources: list[Path] = []
    for plan in plans:
        number = plan.index + 1
        row = rows[number]
        video = Path(str(row.get("video", "")))
        if not video.is_file():
            return _refuse(f"第 {number} 段账上记的视频读不到：{video}"
                           "（改名/搬走了？）——不产出成片。")
        recorded = str(row.get("sha256") or "")
        if recorded and sha256_file(video) != recorded:
            # 只记文件名挡不住「同名换版」：账上记的 SHA256 就是「通过验收的那一份」的
            # 身份，对不上说明它已经不是了（素材台账同款理由，见 CONTEXT「文件名可变引用」）。
            return _refuse(
                f"第 {number} 段的文件与验收时记下的 SHA256 不一致：{video}"
                "（换版/被覆盖？）——它已经不是通过验收的那一份，不产出成片。"
                + _sha_remedy_hint(remedy_hint, number))
        sources.append(video)

    out = Path(out_path)
    ok, how, extra_warnings = _produce(sources, out, strategy, reencode_crf)
    if not ok:
        return _refuse(how)

    meta = video_meta(out)
    measured = float(meta.get("duration_s") or 0.0) or None
    warnings: list[str] = list(extra_warnings)
    if measured is None:
        warnings.append(f"读不到成片的时长元数据：{out}（跳过时长核对）")
    else:
        # 「只报短」的容差与预期值都按**输入段实测 fps** 折算（spec #92 复审 2026-10-27）：
        # H3 产出从不是整数秒，档位量化误差是帧级的，所以容差按一帧/段给；而「一帧」
        # 多长随 fps 变——写死 1/24 在 60fps 素材上每段多给 1.5 帧余量，真短掉几帧会被
        # 吞掉。fps 取**输入段**的、不取成品的：cv2 的 ``CAP_PROP_FPS`` 在重编码产物上
        # 是估出来的——容器 duration 不含末帧时长，212 帧会被读成 24.1137fps（实测），
        # 拿它折算会把该扣掉的量算小、反而制造假告警。
        fps = float(video_meta(sources[0]).get("fps") or 0.0) or _FALLBACK_FPS
        expected_s: float = planned_s
        if strategy == ConcatStrategy.TRIM_FIRST_FRAME:
            # 裁首帧那一档的预期值要扣掉裁掉的那几帧；多参那一档不扣、口径一字不动
            # ——两条路线在「成片该多长」上本来就不一样，硬统一会把多参的「只报短」改坏。
            expected_s = planned_s - (len(sources) - 1) / fps
        if measured + len(sources) / fps < expected_s:
            if strategy == ConcatStrategy.TRIM_FIRST_FRAME:
                # 这一档比的是「扣掉接缝裁帧后的预期」，说成「各段计划时长之和」就是
                # 两套口径（复审 2026-10-27）：8.92s 从来不是哪一段的和，它是 9s 减两帧。
                warnings.append(
                    f"成片实测 {measured:.2f}s，短于扣除接缝裁帧后的预期 "
                    f"{_seconds_text(expected_s)}s（各段计划时长之和 "
                    f"{_seconds_text(planned_s)}s，{len(sources) - 1} 个接缝各裁 1 帧）——"
                    "可能有段没接上，或某段交回的不是计划时长档的产出")
            else:
                # 多参那一档的用户可见文案逐字不变。
                warnings.append(
                    f"成片实测 {measured:.2f}s，短于各段计划时长之和 {_seconds_text(expected_s)}s——"
                    "可能有段没接上，或某段交回的不是计划时长档的产出")
    return FinalFilm(path=out, how=how, total_s=int(total_s), planned_s=planned_s,
                     measured_s=measured, warnings=warnings)


def _seconds_text(seconds: float) -> str:
    """秒数的当面写法：整数就不带小数点（多参那条路的历史文案逐字不变）。"""
    return f"{seconds:.2f}" if abs(seconds - round(seconds)) > 1e-9 else str(int(round(seconds)))


def _sha_remedy_hint(hint: str | None, segment: int) -> str:
    """换版之后的补救出路：**路线**给什么就用什么，没给就用多参今天这句（逐字不变）。

    默认值里带段号是必要的——「用哪一段的新视频顶上」是这个动作的全部信息，而段号只有
    在拒绝发生时才知道（是它把这条账行取出来的）。
    """
    if hint is not None:
        return hint
    return ("重跑该段后请用 `ref-concat <生成目录> --replace "
            f"{segment} <新视频>` 显式指定。")


def _copy_single_source(source: Path, out: Path) -> tuple[bool, str, list[str]]:
    """段数=1：拼接退化为拷贝改名（两档共用，2026-10-27 复审抽出一份）。

    段数=1 时两档都没有接缝可处理（≤15 秒直出的片子走这一档，不劳 ffmpeg 一趟）；
    逐字重复两遍的结果是「一份改了口径另一份没改」——本仓「同一契约两处口径」已经
    咬过两次（#56/#57）。

    **源与目标同一路径时跳过拷贝**：``ref-concat`` 允许用户把段视频放在生成目录里，
    此时 ``shutil.copy2`` 会抛 ``SameFileError``（自己拷自己）。
    """
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.resolve() != source.resolve():
        shutil.copy2(source, out)
    return True, "copy", []


def _produce(sources: list[Path], out: Path, strategy: ConcatStrategy,
             reencode_crf: int) -> tuple[bool, str, list[str]]:
    """按**拼接口径**把各段接成 ``out``；返回 (成功?, 成功时的拼接方式／失败时的原因, 附带提醒)。

    未知档位一律显式失败（spec #92「不许静默降级」）：猜一个最像的去拼，用户拿到的
    片子接缝抽帧、却以为问题出在别处。

    ``reencode_crf`` 只在这一档真正要重编码时才有意义，但它是**调用方给路线属性**，
    共用件不做任何取值判断（判了就等于替路线定了画质）。
    """
    if strategy == ConcatStrategy.TRIM_FIRST_FRAME:
        return _produce_trimmed(sources, out, reencode_crf)
    if strategy != ConcatStrategy.STREAM_COPY_FIRST:
        return False, f"未知的拼接策略：{strategy!r}——不产出成片。", []

    from .tools.seam_audit import merge_videos

    if len(sources) == 1:
        return _copy_single_source(sources[0], out)

    try:
        moved, how = merge_videos(sources, out, reencode_crf=reencode_crf)
    except Exception as exc:  # noqa: BLE001——见下
        # ``find_ffmpeg`` 找不到时抛 RuntimeError（它是给「调用方自己报错」用的）。
        # 但走到这里，用户已经跑完整条链、全部段都验收过了：一个缺依赖不该把收尾炸成
        # 异常栈，而该跟「拼接失败」走同一条路（返回结果对象、会话不标完成、可续拼）。
        return False, f"拼接失败（找不到可用的 ffmpeg 或调用失败）：{exc}", []
    if not moved:
        # 失败线索是 ffmpeg 的 stderr 尾巴，原样带出——用户在成片这一层只看得见这条。
        return False, f"拼接失败（先 stream copy、失败转重编码都没成）：{how}", []
    return True, how, []


def _media_problem(metas: Sequence[Mapping]) -> str:
    """各段尺寸与帧率必须一致；不一致（或读不出）就返回一句点名（空串＝没问题）。

    这既是**产品判断**（尺寸/帧率不同说明有人把别的片子的产出交了回来），也是
    concat filter 的**技术前提**——ffmpeg 不会替人拦：它会把 128×72 与 736×1280
    按第一段的尺寸硬接，或者按各自节奏接出一条时长悄悄不对的片子，全程不报错。
    所以必须在拼之前核、并且**点名是哪几段**（让人直接去换那段，而不是读 stderr）。
    """
    unreadable = [index + 1 for index, meta in enumerate(metas)
                  if not meta or not meta.get("fps")
                  or not meta.get("width") or not meta.get("height")]
    if unreadable:
        return ("第 " + "、".join(str(n) for n in unreadable) + " 段读不出尺寸/帧率："
                "无从核对各段是不是一套输出，不产出成片。")
    base = metas[0]
    mismatched = [
        number for number, meta in enumerate(metas[1:], start=2)
        if (meta["width"], meta["height"]) != (base["width"], base["height"])
        or abs(float(meta["fps"]) - float(base["fps"])) > _FPS_TOLERANCE
    ]
    if mismatched:
        return ("第 " + "、".join(str(n) for n in mismatched)
                + f" 段的尺寸或帧率与第 1 段不一致（第 1 段 {base['width']}×"
                f"{base['height']}@{float(base['fps']):g}fps）：段间硬接不上，"
                "不产出成片——请确认这几段是不是本片的输出。")
    return ""


def _produce_trimmed(sources: list[Path], out: Path,
                     reencode_crf: int) -> tuple[bool, str, list[str]]:
    """首尾帧链那一档：裁掉第 2..N 段的首帧后重编码接起来（spec #92）。

    接缝处重一帧是这个流程的结构性产物：第 N+1 段的第一帧**就是**第 N 段的末帧
    （尾帧接力），原样接起来成片会在每个接缝抽一下。裁掉之后接缝两侧是「段 N 末帧｜
    段 N+1 第二帧」——画面往前走，而不是停一帧。

    **兜底不是静默的**：重编码这一路挂掉（编码器缺失、filter 图被拒等）时退回朴素
    拼接，但那意味着接缝又保留了重复首帧——结果对象里必须说破，拼接方式也得能辨识，
    否则用户手里是一条「看起来接好了」的坏片子。
    """
    from .tools.frame_match import video_meta
    from .tools.seam_audit import merge_videos_trimming_first_frames, probe_streams

    if len(sources) == 1:
        # 没有接缝可裁（与 stream-copy 档同一形态：≤10 秒不分段的片子不走到这里，
        # 但共用件不为「本路线最少两段」这条路线属性开洞）。两档共用一份退化逻辑。
        return _copy_single_source(sources[0], out)

    metas = [video_meta(path) for path in sources]
    problem = _media_problem(metas)
    if problem:
        return False, problem, []

    fps = float(metas[0]["fps"])
    try:
        streams = [probe_streams(path) for path in sources]
        with_audio = all("audio" in kinds for kinds in streams)
        if any("audio" in kinds for kinds in streams) and not with_audio:
            mixed = [index + 1 for index, kinds in enumerate(streams) if "audio" not in kinds]
            return False, ("第 " + "、".join(str(n) for n in mixed)
                           + " 段没有音轨，而其它段有：音轨不一致的几段接起来会音画错位，"
                           "不产出成片。"), []
        ok, detail = merge_videos_trimming_first_frames(
            sources, out, reencode_crf=reencode_crf, fps=fps, with_audio=with_audio)
    except Exception as exc:  # noqa: BLE001——与 stream-copy 档同款：缺依赖不炸收尾
        ok, detail = False, f"找不到可用的 ffmpeg 或调用失败：{exc}"
    if ok:
        return True, ConcatStrategy.TRIM_FIRST_FRAME.value, []

    from .tools.seam_audit import merge_videos

    try:
        moved, how = merge_videos(sources, out, reencode_crf=reencode_crf)
    except Exception as exc:  # noqa: BLE001
        return False, (f"拼接失败（先裁首帧重编码、再退回朴素拼接都没成）："
                       f"{detail}；{exc}"), []
    if not moved:
        return False, (f"拼接失败（先裁首帧重编码、再退回朴素拼接都没成）："
                       f"{detail}；{how}"), []
    return True, _FALLBACK_HOW, [_DUPLICATE_SEAM_NOTICE]


def format_final_film(film: FinalFilm) -> str:
    """成片结果的当面文案：落点、时长、拼接方式，以及卡在哪一段。

    它跟着结果对象走、不跟着路线走：两条路线接的是同一条成片，文案口径不一致会让
    「成片到底是什么」在两种场合说成两件事（spec #92「两条路线成片的同与不同写在一处」）。

    起步总时长与各段计划时长之和**分别给**：末段留余量时两者不等（成片会比目标长一点），
    只报一个数会让人以为成片超了是缺陷——那是规划器的余量，不是错。
    """
    if film.path is None:
        return "\n[成片] 未产出成片：" + film.reason
    planned = (f"起步总时长 {film.total_s}s"
               if film.planned_s == film.total_s
               else f"起步总时长 {film.total_s}s（各段计划时长之和 {film.planned_s}s，"
                    "末段留余量）")
    lines = [
        f"\n[成片] 已接成成片：{film.path}",
        f"  段间硬切、用各段自身帧；{planned}，"
        + (f"实测 {film.measured_s:.2f}s" if film.measured_s else "实测时长读不出")
        + f"（拼接方式：{'拷贝改名' if film.how == 'copy' else film.how}）。",
    ]
    lines += [f"  [提醒] {note}" for note in film.warnings]
    return "\n".join(lines)

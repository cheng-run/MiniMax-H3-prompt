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

# 成片短于各段计划时长之和的告警容差：H3 产出**从不是整数秒**（4s 档恒 4.458s，见
# ``preflight.H3_TIER_FRAMES``），所以「等于总时长」的可证形态是**结构**的（段表覆盖
# ``[0, 起步总时长]``），实测时长只做**只报短**的核对。容差按**一帧/段**给：档位量化
# 误差是帧级的（±2 帧 ≈ 0.08s），跨段累积不会超过这个量级；真短掉一档（如 10s 段交回
# 7s 档）差的是好几秒，照样咬得住。
_FINAL_FILM_FRAME_S = 1.0 / 24.0


class ConcatStrategy(str, Enum):
    """拼接口径。**必须是入参、不许写死在共用件里**（issue #93）。

    两条路线在这件事上本来就不同：多参是「优先无损 stream-copy、失败才重编码」，
    首尾帧链是「裁掉第 2..N 段的首帧再重编码」（接缝处重一帧，见 spec #92）。抽共用件
    时「顺手统一」等于悄悄改变多参的成片行为——所以两档各是一个**可辨识取值**，
    而不是共用件里的一个 if 分支。

    ``TRIM_FIRST_FRAME`` 本票只定义取值，实现留给 #96/#98：未实现的那一档必须**显式
    返回失败原因**，绝不静默退回 stream-copy（静默降级＝用户拿到一条接缝抽帧的成片，
    却以为问题出在别处）。
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
    ok, how = _produce(sources, out, strategy, reencode_crf)
    if not ok:
        return _refuse(how)

    measured = float(video_meta(out).get("duration_s") or 0.0) or None
    warnings: list[str] = []
    if measured is None:
        warnings.append(f"读不到成片的时长元数据：{out}（跳过时长核对）")
    elif measured + len(sources) * _FINAL_FILM_FRAME_S < planned_s:
        warnings.append(
            f"成片实测 {measured:.2f}s，短于各段计划时长之和 {planned_s}s——"
            "可能有段没接上，或某段交回的不是计划时长档的产出")
    return FinalFilm(path=out, how=how, total_s=int(total_s), planned_s=planned_s,
                     measured_s=measured, warnings=warnings)


def _sha_remedy_hint(hint: str | None, segment: int) -> str:
    """换版之后的补救出路：**路线**给什么就用什么，没给就用多参今天这句（逐字不变）。

    默认值里带段号是必要的——「用哪一段的新视频顶上」是这个动作的全部信息，而段号只有
    在拒绝发生时才知道（是它把这条账行取出来的）。
    """
    if hint is not None:
        return hint
    return ("重跑该段后请用 `ref-concat <生成目录> --replace "
            f"{segment} <新视频>` 显式指定。")


def _produce(sources: list[Path], out: Path, strategy: ConcatStrategy,
             reencode_crf: int) -> tuple[bool, str]:
    """按**拼接口径**把各段接成 ``out``；返回 (成功?, 成功时的拼接方式／失败时的原因)。

    策略分派**先于**「段数=1 退化为拷贝」：``TRIM_FIRST_FRAME`` 那一档还没实现时，若先
    撞上拷贝分支，就会静默产出一条**没裁接缝首帧**的成片——用户拿到的片子抽帧，原因却
    不在任何提示里。所以未实现的档位一律显式失败（spec #92「不许静默降级」）。

    ``reencode_crf`` 只在这一档真正要重编码时才有意义，但它是**调用方给路线属性**，
    共用件不做任何取值判断（判了就等于替路线定了画质）。
    """
    if strategy == ConcatStrategy.TRIM_FIRST_FRAME:
        return False, ("拼接策略「裁首帧 + 重编码」尚未实现（#96/#98）："
                       "不产出成片，也不退回 stream-copy——退回会静默留下接缝的重复首帧。")
    if strategy != ConcatStrategy.STREAM_COPY_FIRST:
        return False, f"未知的拼接策略：{strategy!r}——不产出成片。"

    from .tools.seam_audit import merge_videos

    if len(sources) == 1:
        # 段数=1：拼接退化为拷贝改名（≤15 秒直出走这一档，不劳 ffmpeg 一趟）。
        # 源就是成片名时跳过拷贝（``ref-concat`` 允许用户把段视频放在生成目录里）。
        out.parent.mkdir(parents=True, exist_ok=True)
        if out.resolve() != sources[0].resolve():
            shutil.copy2(sources[0], out)
        return True, "copy"

    try:
        moved, how = merge_videos(sources, out, reencode_crf=reencode_crf)
    except Exception as exc:  # noqa: BLE001——见下
        # ``find_ffmpeg`` 找不到时抛 RuntimeError（它是给「调用方自己报错」用的）。
        # 但走到这里，用户已经跑完整条链、全部段都验收过了：一个缺依赖不该把收尾炸成
        # 异常栈，而该跟「拼接失败」走同一条路（返回结果对象、会话不标完成、可续拼）。
        return False, f"拼接失败（找不到可用的 ffmpeg 或调用失败）：{exc}"
    if not moved:
        # 失败线索是 ffmpeg 的 stderr 尾巴，原样带出——用户在成片这一层只看得见这条。
        return False, f"拼接失败（先 stream copy、失败转重编码都没成）：{how}"
    return True, how


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

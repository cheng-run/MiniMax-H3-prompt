"""级联重跑（issue #106，设计 §3.8）：改一段之后，让项目侧的「已通过」真正失效。

**为什么必须由项目侧做**：插件的缓存指纹**不含父段身份**（插件勘察 §5.2、§四 第 22
条），改了上游段，下游缓存照样命中——不删就是「重跑」什么都没发生，而界面上一切都
正常。这条是本模块存在的全部理由。

三段口径，各自只住一处（设计 §1 第 3 条）：

- 删哪几个文件 → ``cache.delete_segment_cache``（清单在 ``cache.py``）；
- 作废行怎么写 → ``ledger.log_director_invalidate``（行格式在 ``ledger.py``）；
- 「作废谁、删谁、前 N-1 段不动」→ 本模块。

**第 N 段自己也要作废＋删**（设计只写了「第 N+1..」，这里按票面口径扩到含 N）：

- 不作废 N 的通过行：陪跑按账跳过它，用户输入的「重跑第 N 段」被静默吞掉；
- 不删 N 的缓存：陪跑下一次回车时读到**旧**帧文件，照样判通过（更坏——假绿）。
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from . import cache, ledger


@dataclass(frozen=True)
class CascadeResult:
    """一次级联做了什么。``deleted`` / ``derived_deleted`` 都是**实际**删掉的路径。

    分两栏：「缓存」是插件的段缓存（在 ComfyUI output 下），「派生件」是项目侧由旧末帧
    写出的尾帧文档与末帧导出图（在会话目录下）。两处根不同，删失败时能直接看出是哪边。
    """

    from_segment: int
    total_segments: int
    invalidated: tuple[int, ...]
    deleted: tuple[Path, ...]
    derived_deleted: tuple[Path, ...] = ()


def _validate_range(from_segment: int, total_segments: int) -> None:
    """段号与段数的合法性（作废、删缓存、删派生件三处共用同一判据）。"""
    if (isinstance(total_segments, bool) or not isinstance(total_segments, int)
            or total_segments < 1):
        raise ValueError(f"段数必须是正整数，实际 {total_segments!r}")
    if (isinstance(from_segment, bool) or not isinstance(from_segment, int)
            or not 1 <= from_segment <= total_segments):
        raise ValueError(
            f"要重跑的段号必须在 1..{total_segments} 之间，实际 {from_segment!r}"
            "（越界会删到段表之外的段，或让整条时间线看起来像没跑过）")


def affected_segments(from_segment: int, total_segments: int) -> tuple[int, ...]:
    """这次级联波及的段号：第 N..末段（纯函数）。

    作废与删除必须走**同一个区间**：两处各写一份 range，将来改一处就会长出
    「作废了但没删」这类半失效状态，而它看起来一切正常。
    """
    return tuple(range(from_segment, total_segments + 1))


def cascade_derived_artifacts(directory: str | Path, *, from_segment: int,
                              total_segments: int) -> tuple[Path, ...]:
    """这次级联要一起删的**项目侧派生件**（纯函数：只算路径，不碰磁盘）。

    两类，都是「由旧末帧派生出来的东西」：

    - ``director/tailframe-NN.md``，N ∈ [N, 末段]。它写的是「怎么把第 N-1 段的末帧变成
      第 N 段终点画面」；末帧变了，这份词就对不上了。不删，#105 的「已在盘上就沿用、
      不重付」会让陈旧词一直生效——用户看不出问题，因为文件在、词也在。
    - ``director/frame-KK-last.png``，KK ∈ [N-1, 末段-1]（N=1 时从 1 起）。重跑第 N..末段
      会改变这些锚的像素。**KK=N-1 也在内**：第 N-1 段自己的缓存与通过行不动，但它的
      末帧导出图是给第 N 段用的派生件，必须重生成。末段那张不在内——没有下一段用它。

    文件名一律经 ``tailframe.tailframe_path`` / ``last_frame_png_path`` 取：编号口径
    只住一处，级联与写词的落点必须永远指同一个文件。
    """
    from .tailframe import last_frame_png_path, tailframe_path

    _validate_range(from_segment, total_segments)
    documents = [tailframe_path(directory, segment)
                 for segment in affected_segments(from_segment, total_segments)]
    # 末帧图从「第 N-1 段」起（N=1 时没有第 0 段，从第 1 段起），到末段的前一段为止。
    first_anchor = max(1, from_segment - 1)
    images = [last_frame_png_path(directory, segment)
              for segment in range(first_anchor, total_segments)]
    return tuple(documents + images)


def cascade_rerun(directory: str | Path, *, from_segment: int, total_segments: int,
                  cache_directory: str | Path, reason: str | None = None) -> CascadeResult:
    """从第 ``from_segment`` 段起重跑：作废通过行 ＋ 删该段及其后的缓存与派生件。

    ``directory`` 是会话目录（账与派生件在那里），``cache_directory`` 是插件缓存目录
    （帧文件在那里）——两者分开传，因为账跟着会话走、缓存跟着 ComfyUI 的 output 走。

    **先校验、再删、最后写作废行**：校验（越界/符号链接）必须在任何删除之前全部跑完，
    否则「删了一半抛错」既没法回滚、也没法告诉用户该重跑哪一段。删除失败则一行作废
    都不写：反过来（先作废再删）账上已是「这块没通过」、缓存却还在——下一次陪跑会读到
    旧帧数直接判通过，比什么都不做更难查。

    只对**现在算通过**的段写作废行：从没跑过的段本来就没有通过行可作废，凭空写一行
    会让账上的「作废」与真实发生过的通过对不上（账是证据，不是日志）。

    派生件（尾帧文档＋末帧导出图）与段缓存走**同一个区间**（第 N..末段；末帧图从第
    N-1 段起）：它们都是按旧末帧算出来的东西，缓存重跑而派生件留着，等于给下一段喂
    旧锚点。
    """
    _validate_range(from_segment, total_segments)
    affected = affected_segments(from_segment, total_segments)
    cache_targets = [path for segment in affected
                     for path in cache.segment_cache_files(cache_directory, segment)]
    derived_targets = cascade_derived_artifacts(
        directory, from_segment=from_segment, total_segments=total_segments)

    # 两个根各校验一遍，都在**删第一个文件之前**完成（见 docstring 第二段）。
    cache.assert_files_inside(cache_directory, cache_targets, what="缓存目录")
    cache.assert_files_inside(directory, derived_targets, what="会话目录")

    deleted = cache.delete_files_under(cache_directory, cache_targets, what="缓存目录")
    derived_deleted = cache.delete_files_under(
        directory, derived_targets, what="会话目录")

    passed = ledger.passed_director_rows(directory)
    invalidated = tuple(segment for segment in affected if segment in passed)
    note = reason or f"级联重跑：用户要求从第 {from_segment} 段起重跑（#106）"
    for segment in invalidated:
        ledger.log_director_invalidate(directory, segment=segment, reason=note)

    return CascadeResult(
        from_segment=from_segment, total_segments=total_segments,
        invalidated=invalidated, deleted=deleted, derived_deleted=derived_deleted)


def render_cascade_notice(result: CascadeResult) -> str:
    """把级联结果念给用户，**并说清两条操作路径的差别**（票面 AC3）。

    为什么要在出站里分开念：两条路径对项目侧完全一样（都是「从第 N 段重跑」），差别在
    用户手上——不改词是直接回导演台点运行；改词必须**先在导演台 UI 里**改，项目侧的
    ``seg-NN.md`` 不会反向同步进导演台（导演包是快照，插件读的是时间线里的词）。
    不说明白，用户改完项目里的文件再重跑，会得到一段「提示词没变」的旧视频。
    """
    first, total = result.from_segment, result.total_segments
    span = f"第 {first} 段" if first == total else f"第 {first}-{total} 段"
    invalidated = ("、".join(f"第 {segment} 段" for segment in result.invalidated)
                   if result.invalidated else "无（这几段本来就没通过）")
    lines = [
        f"[级联] 从{span}起重跑：作废通过行 {invalidated}；"
        f"删掉缓存文件 {len(result.deleted)} 个"
        + ("" if result.deleted else "（本来就不在，幂等跳过）") + "；"
        f"删掉项目侧派生件 {len(result.derived_deleted)} 个"
        + ("" if result.derived_deleted else "（还没有，幂等跳过）") + "。",
    ]
    if result.derived_deleted:
        # 点名到文件：用户可能正开着那份 tailframe-NN.md 在改，删了要当场知道。
        lines.append("  派生件（旧末帧写出来的词与图，重跑后要重写）："
                     + "、".join(path.name for path in result.derived_deleted))
    if first > 1:
        earlier = "第 1 段" if first == 2 else f"第 1-{first - 1} 段"
        lines.append(f"  {earlier}的通过行与缓存没动（它们仍是这次重跑的输入）。")
    lines.extend([
        f"  · 不改词重跑：回到导演台，用 runSelection 只勾第 {first} 段点运行，"
        "跑完把本段成片视频路径贴回来。",
        f"  · 改词重跑：在**导演台 UI 里**改第 {first} 段的提示词"
        f"（项目侧的 seg-{first:02d}.md 不会自动同步进导演台），或在陪跑里输入 e；"
        f"改完回到第 {first} 段重跑，跑完把本段成片视频路径贴回来。",
    ])
    return "\n".join(lines)

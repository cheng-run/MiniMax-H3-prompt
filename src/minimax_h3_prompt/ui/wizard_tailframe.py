"""导演台陪跑的按需帧词：切镜段首帧词与终点画面（尾帧）词（#305 从 wizard 搬出）。

``tests/test_wizard_size.py`` 把 ``ui/wizard.py`` 钉在体量基线上：涨行数必须用搬迁换。
这两个函数自成一格——轮到某一段时产一份词，只认 plan／segment／session／llm，出站
文本与落盘位置都不变。问答入口由调用方注入（``ask``／``confirm``），所以本模块不
反向 import ``wizard``、也不会形成环（与 ``wizard_director.py`` 同一分工）。
"""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # 只用于注解
    from ..director.frames import DirectorPlan, DirectorSegment
    from ..session_store import SessionState


class HandoffStopped(Exception):
    """用户在按需词环节输入 ``q``：干净停下（退出码 0），不是错误。

    用异常而不是布尔返回值：这一问嵌在「取起点画面 → 写尾帧词」的中间，返回
    ``None`` 会被下游当成「没有这张图」继续兜底——而这里要的是**停住**，两者在
    类型上分开，调用方就不可能把「用户主动停下」误当成「缺图」（#149）。
    """


def frame_meta_rewrite_requested(
    prompt_path: Path, label: str, *, confirm: Callable[..., bool],
) -> bool:
    """盘上按需词的模型档与当前 ``frame:`` 不一致时告警并问「重写？」；默认 N。

    #153：首帧/尾帧词是**按某一档的写词纪律与画布**产出的。用户换了 ``config/image.yaml``
    的 ``frame:`` 之后续跑，旧词会被当作「已在盘上，不重付」直接沿用——产物照常生成，
    只是按旧档的口径写、按新档的工作流出图，两边都不对，而且一声不响。
    默认 N：这份词可能已经拿去生过图，重写会让「挂上去的那张图」与文件里的词错开。
    """
    from .. import image_config
    from ..director.frame_meta import read_frame_meta

    meta = read_frame_meta(prompt_path)
    used = str((meta or {}).get("model") or "")
    if not used:
        return False  # 旧产物没有 sidecar：不知道按哪档写的，不凭空报警
    current = image_config.load_frame_image_model()
    if used == current:
        return False
    label_of = image_config.IMAGE_MODEL_LABELS
    print(f"[警告] {label}是按 {label_of.get(used, used)}（{used}）写的，"
          f"当前 config/image.yaml 的 frame: 是 {label_of.get(current, current)}"
          f"（{current}）：沿用旧词会按旧档的纪律与画布出图，可能与当前工作流不匹配。")
    return confirm(f"重写{label}？", default=False)


def prompt_required_image(
    question: str, consequence: str, *, ask: Callable[[str], str],
) -> Path:
    """问一张**必填**图片路径：空输入讲清后果后重问；``q`` 干净停下。

    切镜段的尾帧词以**本段首帧**为参考图（``tailframe_writer_director.md``）。
    以前这里回车＝没有、改用上一段终点图兜底，于是「切镜段起点在别处」被静默忽略
    （#137 段2 实测）。没有兜底出口以后，缺图只能停，不能写出一份从错误起点出发的词。
    """
    while True:
        raw = ask(question).strip().strip('"').strip("'")
        if raw.lower() in ("q", "quit"):
            raise HandoffStopped
        if not raw:
            print(f"[提示] 这一问是必填：{consequence}")
            continue
        path = Path(raw)
        if not path.is_file():
            print(f"[错误] 文件不存在：{path}")
            print("  请确认路径后重输（q＝停下，已通过的段留在账上）。")
            continue
        return path


def prompt_optional_image(question: str, *, ask: Callable[[str], str]) -> Path | None:
    """问一张**可选**图片路径：回车＝没有；路径不存在当场报错重问（不猜目录）。

    与 ``wizard_director.prompt_existing_file`` 的差别只在「空串」的语义：这里是「没有这张图」，
    不是失败——下一段会回退到视频末帧。用户多半从资源管理器「复制文件地址」粘过来，
    会带一对引号，所以先剥引号再判。
    """
    while True:
        raw = ask(question).strip().strip('"').strip("'")
        if not raw:
            return None
        path = Path(raw)
        if not path.is_file():
            print(f"[错误] 文件不存在：{path}")
            print("  请确认路径后重输（回车＝没有，下一段改用本段视频末帧兜底）。")
            continue
        return path


def cut_first_frame_prompt(
    plan: DirectorPlan, segment: DirectorSegment, session: SessionState, *,
    llm, confirm: Callable[..., bool],
) -> str | None:
    """切镜段的段首帧词：轮到该段时先给词，再写尾帧词（尾帧要拿这张图当参考）。

    不重付：文件已在盘上就沿用（与尾帧词同一条纪律——用户可能已经拿它生成过图，
    重写一份会让「挂上去的那张图」与文件里的词不再对应）。
    """
    from ..director.firstframe import firstframe_path, write_firstframe_prompt
    from ..image_config import load_frame_image_model
    from .wizard_display import print_firstframe_prompt

    path = firstframe_path(session.directory, segment.index)
    if path.is_file() and not frame_meta_rewrite_requested(
            path, f"第 {segment.index} 段段首帧词", confirm=confirm):
        text = path.read_text(encoding="utf-8").strip()
        print_firstframe_prompt(text, index=segment.index, path=path,
                                note="已在盘上，沿用，不重付")
        return text
    print(f"\n[进度] 轮到第 {segment.index}/{len(plan.segments)} 段（{segment.label}）："
          "切镜段要一张自己的首帧，正在写段首帧提示词（需要调用模型，稍等）…")
    shot_table = str(session.stage_state.get("shot_table", ""))
    try:
        written = write_firstframe_prompt(
            segment, llm=llm, shot_table=shot_table,
            brief=session.brief, session_dir=session.directory,
            # 档名由写词处落进 sidecar，终端从同一份读：新旧词只有这一个档名来源。
            model=load_frame_image_model(),
            # 设计上下文只在 stage_state 里（分场剧本/人物/道具/背景/美术/镜头评审/
            # 画面细化/身份锁定）；不传进来，请求就只剩剧情摘要（#152）。
            state=session.stage_state)
    except Exception as exc:  # noqa: BLE001 - 病因原样交回，别伪装成别的问题
        print(f"[错误] 第 {segment.index} 段的段首帧提示词写不出来：{exc}")
        return None
    if written is None:
        print(f"[错误] 第 {segment.index} 段的段首帧提示词写不出来"
              "（调用失败或回复里没有 first 帧正文）。")
        return None
    print_firstframe_prompt(written, index=segment.index, path=path)
    return written


def write_director_tailframe(
    plan: DirectorPlan, segment: DirectorSegment, session: SessionState, *,
    llm, ask: Callable[[str], str], confirm: Callable[..., bool],
) -> bool:
    """轮到该段时产出它的终点画面提示词（#107 的三段式，本票负责接线）。

    起点画面取**账内上一段成片视频的末帧**（#114）：这一段自己还没跑，拿不到自己的帧；
    而「本段第一帧」就是上一段的终点，这才是尾帧变换的输入。为什么不取段缓存：
    #111 的现场里插件默认写 ``raw`` 编码的 ``.pt``，本项目读不了，旧实现因此整轮
    陪跑中止；而且用户实际跑出来的是哪一版视频，提示词就该描述哪一版。

    账是**现读**的，不用进入循环时的 ``passed`` 快照：轮到第 N 段时，第 N-1 段正是
    本轮刚在这个循环里通过的——快照是在循环之前读的，看不到它。

    只传**这一段**给 ``write_tailframe_prompts``（用 ``replace`` 造单段 plan）：
    它会对 plan 里所有要终点画面的段产出，而陪跑此刻只该为轮到的那一段写——
    其它段的起点画面还没产生，用这一张图写出来的会是假词。
    """
    from dataclasses import replace

    from ..director import cache, ledger
    from ..director import tailframe as tailframe_module
    from ..director.frames import needs_first_frame_image
    from ..director.pack import film_end_still
    from ..director.tailframe import (
        needs_tailframe, tailframe_path, write_tailframe_prompts,
    )
    from .wizard_display import print_tailframe_document

    existing = tailframe_path(session.directory, segment.index)
    # 全片尾帧优先（#149）：`ending` 的尾帧槽同时被「作者给的全片终点图」和「尾帧词」
    # 指向，作者指定的终点必须赢。盘上有 film-end 就不写词、不花那次模型调用；旧词
    # 可能已生成过图，明确说一句「不采用」，别让它在盘上冒充本次的终点。
    if segment.type == "ending":
        film_end = film_end_still(session.directory)
        if film_end is not None:
            print(f"\n[尾帧] 第 {segment.index} 段（{segment.label}）用**全片尾帧**：{film_end}")
            print(f"       把它挂到导演台第 {segment.index} 段的尾帧槽；"
                  "这一段不再产出尾帧词（作者给的终点优先）。")
            if existing.is_file():
                print(f"       旧尾帧词不采用：{existing}（全片尾帧优先，#149）。")
            return True
    if existing.is_file() and not frame_meta_rewrite_requested(
            existing, f"第 {segment.index} 段尾帧词", confirm=confirm):
        # 续跑不重付、也不许覆盖（#58 的同一条教训）：这一段的词可能已经拿去生成过
        # 终点画面了，重写一份新词会让「挂上去的那张图」与文件里的词不再对应。
        text = existing.read_text(encoding="utf-8")
        print_tailframe_document(text, index=segment.index, path=existing,
                                 note="已在盘上，沿用，不重付")
        # 与本段视频词一起复核：背景冻结 vs 机位在动的判据只接在写词路径上时，
        # 续跑沿用的旧词可以带着一对互斥要求而终端一声不响（半接线）。静态分支
        # 也要按本节段重新判，否则静态旧词会被按运镜段挂假位移错误（#150）。
        for issue in tailframe_module.validate_written_tailframe(
                text, segment, session.directory):
            print(f"   [{issue.severity.upper()}] {issue.code}: {issue.message}")
        return True

    previous = segment.index - 1
    if previous < 1:
        print(f"[错误] 第 {segment.index} 段（{segment.label}）不该要终点画面："
              "首段没有起点画面可用（opening 不产尾帧变换）。")
        return False
    # 跳过已通过的段之后，这里要取上一段的起点画面再调用模型，可能等几秒到几十秒；
    # 没有这行时用户只看到「跳过」就静默了（2026-10-31 验收反馈）。
    print(f"\n[进度] 轮到第 {segment.index}/{len(plan.segments)} 段（{segment.label}）："
          "正在取上一段起点画面并写终点画面提示词（需要调用模型，稍等）…")
    # 起点画面＝**本段起点**（``tailframe_writer_director.md``）。两种段分开取：
    # 切镜段起点在别处，参考图只能是用户挂在该段首帧槽的那张；引导段的起点＝上一段
    # 终点，下面那段「上一段终点图／视频末帧」才是本段起点。2026-11-02 验收
    # （#137 段2）之前这里对两类一视同仁，切镜段的尾帧词因此从错误起点写起。
    image = None
    if needs_first_frame_image(segment.type, plan.mode):
        # 必填、给了 q 就停：这里没有「回车＝兜底」的出口（#149）。兜底会让尾帧词
        # 从上一段终点写起，而切镜段的起点恰恰不在这里。
        image = prompt_required_image(
            f"第 {segment.index} 段（{segment.label}）的首帧路径"
            "（本段起点画面；尾帧词以它为参考图；q＝停下）：",
            "切镜段的尾帧词必须以本段首帧为参考图，没有它只能从上一段终点写起"
            "（2026-11-02 验收里尾帧词写错起点的成因）。",
            ask=ask)
        print(f"  起点画面：本段首帧 {image}")
        # #158：这一段贴回成片时，画幅核对要把这张图当锚点。路径只在这里出现过，
        # 不记进 sidecar，续跑那一轮就只剩「元信息缺失」，核对等于没做。
        from ..director.firstframe import record_first_frame_image

        record_first_frame_image(session.directory, segment.index, image)
    if image is None:
        # 起点画面**优先用上一段的终点图**（用户生成并挂在导演台尾帧槽的那张）。
        # #114 原先只认账内上一段成片视频的末帧；2026-10-31 实跑证明视频末帧每段都会漂
        # （第 3 段起墨镜、第 4 段胡子），拿它当参考会把漂移逐段放大。终点图是干净的
        # 目标准图；旧账没有这个字段、或用户没存，才回退到视频末帧。
        try:
            rows = ledger.passed_director_rows(session.directory)
        except (OSError, ValueError) as exc:
            print(f"[错误] 段产出账读不回来，第 {segment.index} 段的终点画面提示词写不了：{exc}")
            print("  修好后重跑向导会从这一段接着走（已通过的段留在段产出账里）。")
            return False
        row = rows.get(previous)
        end_image = row.get("end_image") if row else None
        # 旧账（本功能上线前通过的段）没有 ``end_image``：这里补问一次，免得用户为了
        # 拿到干净参考图而重跑上一段。上一段本身不产终点画面（opening/continue）时不问。
        previous_segment = next(
            (item for item in plan.segments if item.index == previous), None)
        if (not end_image and row is not None and previous_segment is not None
                and needs_tailframe(previous_segment.type, plan.mode)):
            end_image = prompt_optional_image(
                f"第 {previous} 段的终点图路径（账里没记；它带起点地标，是距离感的"
                "锚点；回车=没有，改用上一段视频末帧）：",
                ask=ask)
        if end_image:
            candidate = Path(end_image)
            if candidate.is_file():
                image = candidate
                print(f"  起点画面：上一段终点图 {image}")
            else:
                print(f"  [警告] 账里记的上一段终点图不存在：{candidate}"
                      "——改用上一段视频末帧")
        video = row.get("video") if row else None
        if image is None:
            if not video:
                # 终点图与成片视频都没有（旧账或根本没通过）：不猜、不拿缓存顶替。
                print(f"[错误] 第 {segment.index} 段要终点画面，但账里没有上一段"
                      f"（第 {previous} 段）可用的起点画面（终点图与成片视频都没有）。")
                print("  修好后重跑向导会从这一段接着走（已通过的段留在段产出账里）。")
                return False
            try:
                # 落点由 tailframe.last_frame_png_path 给：级联重跑按同一个名字删（#106），
                # 在这里再拼一份字面就是两处会各自漂的口径。
                image = cache.export_last_frame(
                    video, tailframe_module.last_frame_png_path(session.directory, previous))
            except ValueError as exc:
                print(f"[错误] 第 {segment.index} 段要终点画面，但取不到上一段视频的末帧：{exc}")
                print("  修好后重跑向导会从这一段接着走（已通过的段留在段产出账里）。")
                return False
            print(f"  起点画面：上一段视频末帧 {image}")
    single = replace(plan, segments=(segment,))
    try:
        written = write_tailframe_prompts(
            single, llm=llm, session_dir=session.directory, start_image=image,
            identity_baseline=session.stage_state.get("character_design"))
    except (RuntimeError, ValueError) as exc:
        print(f"[错误] 第 {segment.index} 段的终点画面提示词写不出来：{exc}")
        return False
    for item in written:
        print_tailframe_document(item.text, index=item.index, path=item.path, model=item.model)
        for issue in item.issues:
            print(f"   [{issue.severity.upper()}] {issue.code}: {issue.message}")
        print("   用它生成终点画面，然后挂到导演台这一段的尾帧槽上再点运行。")
    return True

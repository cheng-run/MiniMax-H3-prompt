"""向导的展示与资产提示词读写（2026-11-02 体检 P5：拆 ``wizard.py`` 第一刀）。

为什么单独成文件：``ui/wizard.py`` 6400 行、149 个顶层定义，是「读代码不全」的
最大一处。这一刀只搬**无问答、不改会话状态**的纯函数——移出去不会改变任何
monkeypatch 点（用例从不替这些名字打桩），因此行为可按原样对照。2026-11-03 #149
起，「打印可复制正文 / 点名全片尾帧落点」这类纯输出也搬到这里（只是 print，
不读输入、不写盘）。

留在 ``wizard.py`` 的是编排（谁在什么时候问、什么时候存）；这里只回答
「这句话怎么说」「这张素材的模型判定是什么」。新代码请加在本文件，别再把
纯展示逻辑塞回向导。
"""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # 只用于注解（运行时经函数级导入取，防环）
    from collections.abc import Sequence

    from ..asset_loop import QueueItem
    from ..brief_parser import Brief


def render_director_segment_briefing(plan, segment, session_dir: Path,
                                      *, single_shot: bool = False,
                                      cache_directory: Path | None = None,
                                      cache_name: str = "") -> str:
    """一段的执行说明：段号、两种计划帧、给图清单、段文件、**勾段方式与证据入口**。

    「用 runSelection 只勾第 N 段」这句是**硬要求**（设计 §3.7 最后一条）：插件对
    选中段一律重采样，全片模式下点运行会把整条时间线重跑一遍——用户会以为「只补跑
    一段」，实际是几十分钟的整片重烧。

    #112 起证据统一为**成片视频**：简报不能说「数缓存帧数」，那会让用户以为缓存仍是
    判定依据（`raw` 编码现场就是这样被卡在第 1 段的）。但#112 复审（2026-10-31）
    要求多段仍印一行**段缓存目录**并当场标注它**只供定位、不是判定依据**——用户去
    找插件现场产物时需要这个路径，删掉只会让他自己翻 `output/`。
    单段计划（i2v）不印这一行：插件根本不为单段写缓存（2026-10-30 现场目录都不存在），
    印一个永远不存在的路径只会误导。
    """
    from ..director.frames import FIRST_SEGMENT_TYPE, needs_first_frame_image
    from ..director.pack import film_end_still
    from ..director.planning import render_give_images
    from ..director.prompts import segment_path
    from ..director.tailframe import needs_tailframe

    # 给图行与段表清单、打包报告同一口径：单段 fl2v 的全片尾帧会被挂成这一段的
    # 终点画面，多段时它属于最后一段（#147 AC2）。`pack` 在函数级导入——模块级
    # 会经 `pack -> prompts` 绕回来形成环。
    film_end = plan.mode == "fl2v" and film_end_still(session_dir) is not None
    give_images = render_give_images(segment, plan.mode,
                                     single_segment=len(plan.segments) == 1,
                                     film_end=film_end)

    lines = [
        "",
        "=" * 60,
        f"第 {segment.index}/{len(plan.segments)} 段（{segment.label}）  "
        f"计划：声明 {segment.declared_frames} 帧 / 导出 {segment.export_frames} 帧  "
        f"给图：{give_images}",
        f"段提示词：{segment_path(session_dir, segment.index)}",
    ]
    # 提示词正文**直接打在终端**（P6，2026-11-02 验收）：只给路径时用户要一边终端
    # 一边资源管理器翻文件，既打断节奏也容易复制错版本。
    prompt_path = segment_path(session_dir, segment.index)
    if prompt_path.is_file():
        lines += [
            "",
            "本段提示词（整段复制到导演台的提示词框）：",
            "-" * 60,
            prompt_path.read_text(encoding="utf-8").strip(),
            "-" * 60,
        ]
    if segment.type != FIRST_SEGMENT_TYPE and needs_first_frame_image(
            segment.type, plan.mode):
        # 判据与给图契约同源（#145），再排除全片开场：opening 的首帧由阶段 1 全片
        # 首帧承担，段号也没有上一段可关引导。fl2v 的 camera_cut 有首帧槽，r2v 的
        # 同一类型没有——照类型名判会让 r2v 用户去找一张不存在、也挂不上的首帧图。
        # P4（2026-11-02 验收）：切镜段关引导。包内 continuityFromPrev=false，
        # 但 ComfyUI 里若被勾成「引用上段」，这一段会多出 12 帧「保完整」尾，
        # 帧数判定必挂（#137 段2 实测 136 帧 vs 应出 124）。
        lines.append(
            "本段是切镜段：挂**首帧**（本段起点画面），并把「引用上段」**关掉**——"
            "包内 continuityFromPrev=false；被勾上会当成续接，多出 12 帧「保完整」尾。")
    if needs_tailframe(segment.type, plan.mode):
        lines.append(
            "本段有终点画面：生成后把图片留着，跑完贴视频时会问它的路径"
            "（下一段拿它当参考图）。")
    if single_shot:
        lines.append(
            "→ 在导演台里直接点运行（时间线只有这一段），跑完回来贴路径。")
    else:
        lines += [
            f"→ 在导演台里用 runSelection **只勾第 {segment.index} 段**，然后点运行。",
            "  （全片模式下点运行会把整条时间线重跑一遍：插件对选中段一律重采样，",
            "    缓存命中也不会跳。）",
            # 2026-10-31 现场：导演包固定 exportMode="all"，跑完导出的是「前几段的
            # 合并视频」，用户贴回来的是 226 帧而不是本段的 187 帧。先说清，免得
            # 他以为自己跑错、或者去手动裁视频。
            "  导出是「全部导出」：跑完的视频含前面已有帧的段（未勾的段从缓存填），",
            "  把那条路径贴回来即可，不用自己裁。",
        ]
        if cache_directory is not None:
            # 只印路径、不印「数帧」：判定证据是用户贴回的成片视频（#112），缓存帧只
            # 用来定位插件现场产物。标注写在同一行，免得用户只看到路径就以为要核对它。
            lines.append(
                f"段缓存目录：{cache_directory}（只供定位，不是判定依据："
                "判定按你贴回的成片视频）")
        if cache_name:
            # P5（2026-11-02 验收）：包内的缓存名与节点性能组的字段是两处输入；
            # 节点留空时缓存落到以 node id 命名的目录，账行路径与现场对不上。
            lines.append(
                f"节点性能组的 cache_name 也填：{cache_name}"
                "（留空会让段缓存目录名与账行对不上）")
    lines.append("跑完把本段成片视频路径贴回来（我按这条视频数帧判定）。")
    lines.append("=" * 60)
    return "\n".join(lines)


def asset_assignment(state: dict, item: QueueItem) -> dict[str, str]:
    """这一张当前的模型判定：条目里记的那一份，旧条目（issue #88 之前）回落到配置默认。

    回落**走同一个判定函数**（``image_routing.assignment``，不给文本＝不命中）：手搓一个
    ``{model, source, keyword}`` 字面量就是同一份记录的第二个写点，形状一改就漂。
    """
    from ..image_routing import assignment, assignment_of

    value = assignment_of(find_prompt_entry(state, item))
    if value is not None:
        return value
    return assignment(item.kind, texts=())


def render_asset_model_line(state: dict, item: QueueItem) -> str:
    """出词横幅里那两行：模型与理由（＋ 受限模型的许可提示）。**唯一写源**（issue #88）。

    内容从条目里记的判定现读，不重新判一遍：重新判会在提示词被改写后给出与当初不同的
    答案（同 ``image_routing`` 模块注释里那条理由）。

    许可提示按模型查 ``image_config.LICENSE_NOTES``：写死 ``== "qwen"`` 的话，加第二档
    受限模型时横幅会静默不再提示。
    """
    from ..image_config import LICENSE_NOTES
    from ..image_routing import described

    value = asset_assignment(state, item)
    lines = [f"[生图模型] {item.name}：{described(value)}"]
    note = LICENSE_NOTES.get(value["model"])
    if note:
        lines.append(f"[许可] {note}")
    return "\n".join(lines)


def find_prompt_entry(state: dict, item: QueueItem) -> dict:
    """按语义名定位 state 里的资产条目（读/写两处共用这一遍走法）；找不到响错不吞。"""
    rows = state.get(f"{item.kind}_image_prompts")
    if isinstance(rows, list):
        for entry in rows:
            if isinstance(entry, dict) and entry.get("name") == item.name:
                return entry
    raise ValueError(f"找不到资产提示词条目：{item.kind}/{item.name!r}")


def current_prompt_text(state: dict, item: QueueItem) -> str:
    """取该资产当前的 positive_prompt（改写后即最新版）。"""
    return str(find_prompt_entry(state, item).get("positive_prompt") or "")


def update_prompt_text(state: dict, item: QueueItem, new_text: str) -> None:
    """把改写后的提示词写回 state 的资产条目（下游对照底本/登记描述同源最新版）。"""
    find_prompt_entry(state, item)["positive_prompt"] = new_text


def locked_features(state: dict, brief: Brief, name: str) -> Sequence[str]:
    """取一张素材的锁死清单（issue #53）：判偏差的唯一判据。

    抽成一处是因为它有两个消费者（AI 改词、回灌对齐）——两处各写一遍 ``next(...)``
    就是第二份会漂的取法（复审 T5）：将来 ``image_prompt_variants`` 的字段改名或
    清单来源改口径，漏改一处就会静默退回空清单（＝不收窄），而空清单是合法值，
    没有任何东西会报红。旧条目没有这一键时返回空元组（不收窄），与对照器同口径。
    """
    from ..generation import image_prompt_variants

    return next((v.locked_features for v in image_prompt_variants(state, brief.canvases)
                 if v.name == name), ())


def clean_path_input(raw: str) -> str:
    """清洗交互输入的文件路径：剥空白与首尾成对引号。

    Windows 资源管理器"复制为路径"自带双引号，直接 Path(raw) 会得到
    不存在的路径。只剥"首尾成对"的引号，路径中间内容原样保留——
    无引号输入的行为与清洗前完全一致。
    """
    text = raw.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in ("'", '"'):
        text = text[1:-1].strip()
    return text


def announce_film_end_still(directory: Path, plan) -> None:
    """点名全片尾帧该挂到**哪一段**（多段 fl2v 的唯一用途）。

    它在打包时**不进包**（设计 §3.4.1 第 3 条：非开场段的终点画面由用户逐段挂），
    所以用户必须在这里被告知落点——否则这张图在盘上，却没人知道该挂哪一段。
    单段 fl2v 例外：打包器会把它挂成那一段的终点画面（``pack._plan_media``）。
    """
    from ..director.frames import segment_type_label
    from ..director.pack import film_end_still

    if plan.mode != "fl2v":
        return
    path = film_end_still(directory)
    if path is None:
        return
    ending = next((seg for seg in plan.segments if seg.type == "ending"), plan.segments[-1])
    print(f"\n[尾帧] 全片终点画面：{path}")
    if len(plan.segments) == 1:
        print(f"       单段片子：打包时已挂成第 {ending.index} 段的终点画面。")
    else:
        print(f"       多段片子：请在导演台把它挂到第 {ending.index} 段"
              f"（{segment_type_label(ending.type)}）的尾帧槽——中间段的终点画面"
              "由尾帧变换提示词逐段产出。")


def print_tailframe_document(text: str, *, index: int, path: Path,
                             note: str = "", model: str | None = None) -> None:
    """打印**可直接复制给生图模型**的正文（2026-10-31 用户裁定，同日修订）。

    锁死清单是生成之后的核对表，不是生成指令：它逐字摘自正文，粘给模型是冗余，
    还可能让模型把清单当成额外约束逐条照做。用户明确要求终端只展示正文；
    完整三段式仍写盘（``tailframe-NN.md``），核对时打开文件即可。

    **拼成一段而不是两节**：Qwen-Image-2.1 的单图编辑口径就是一段 prompt
    （官方示例 ``Create a photorealistic photograph ... from the input sketch ...``），
    带两个 ``###`` 段头粘过去等于把指令拆成文档。段头只留在文件里给校验器用。
    正文用分隔线包住，方便整段复制；路径留在标题里，事后仍可定位文件。

    ``model`` 是这份词**写时**的档（#150 AC2）：用户据此知道该开哪个工作流，也免得
    把 Qwen 的词拿到 MOSS 工作流里出图。沿用旧词时传 sidecar 里的档，不是当前配置。
    """
    from ..director.tailframe import (
        PRESERVE_HEAD, TRANSFORM_HEAD, split_tailframe_sections,
    )

    header = f"[尾帧] 第 {index} 段可直接复制给生图模型"
    if note:
        header += f"（{note}）"
    print(f"{header}（{path}）：")
    if model is None:
        # 沿用盘上旧词时没人在调用点知道它按哪档写的：sidecar 才是**写时**的档。
        # 现读配置会把旧词标成新档（用户中途换过 ``frame:`` 时，#150）。
        from ..director.frame_meta import read_frame_meta

        model = str((read_frame_meta(path) or {}).get("model") or "") or None
    if model:
        # 展示名取配置层的对照表（与 #88 的出词横幅同源），不另写一份中文名。
        from ..image_config import IMAGE_MODEL_LABELS

        print(f"本次按 {IMAGE_MODEL_LABELS.get(model, model)}（{model}）档出词："
              "角色声明与保留句按这一档的图生图编辑纪律写。")
    print("-" * 60)
    sections = split_tailframe_sections(text)
    blocks = [sections.get(head, "").strip()
              for head in (TRANSFORM_HEAD, PRESERVE_HEAD)
              if sections.get(head, "").strip()]
    if blocks:
        print("\n\n".join(blocks))
    else:
        # 极端情况下两节都没解析出来（模型产出了坏文档）：**不要**原样打印全文——
        # 全文可能带着锁死清单，而用户要求终端不展示它。完整内容在文件里，
        # 下方的校验问题会点名缺了哪一节。
        print("（没有解析出「变换指令 / 保留项声明」两节；完整内容在文件里，"
              "请看下方校验问题。）")
    print("-" * 60)
    print("锁死清单已留在文件里，生成后核对用（不粘给模型）。")
    print("生成后把这张终点图的路径留着：跑完这一段会问，下一段拿它当参考图"
          "（带起点地标，才有距离感）。")


def print_firstframe_prompt(text: str, *, index: int, path: Path,
                            note: str = "", model: str | None = None) -> None:
    """打印切镜段**段首帧**的可复制正文（用户要粘给生图模型）。

    与尾帧文档分开：首帧词是文生图的一段正文（没有三段式），粘过去就是一整段；
    加分隔线只是方便整段选中。

    ``model`` 是这份词**写时**的档（#150 AC2，与 ``print_tailframe_document`` 同一口径）：
    用户据此知道该开哪个工作流。没传时读 sidecar（写词处落的**写时**档，旧词也给对档），
    旧产物没有 sidecar 才回落当前 ``frame:``——**不能**直接现读配置，那会把旧词标成新档。
    """
    header = f"[首帧] 第 {index} 段可直接复制给生图模型"
    if note:
        header += f"（{note}）"
    print(f"{header}（{path}）：")
    if model is None:
        from ..director.frame_meta import read_frame_meta
        from ..image_config import load_frame_image_model

        model = str((read_frame_meta(path) or {}).get("model") or "") \
            or load_frame_image_model()
    if model:
        # 展示名取配置层的对照表（与出词横幅同源），不另写一份中文名。
        from ..image_config import IMAGE_MODEL_LABELS

        print(f"本次按 {IMAGE_MODEL_LABELS.get(model, model)}（{model}）档出词："
              "首帧词按这一档的写词纪律与画布出。")
    print("-" * 60)
    print(text.strip())
    print("-" * 60)
    print("用它先生成这一段的**首帧**，挂到导演台该段的「首帧」槽；"
          "接着写的尾帧词会以这张图为参考。")


def semantic_reference(previous_text: str | None) -> str | None:
    """从一段提示词正文取「该达到的末态」参照：收尾句。

    v2 规划路径用 ``plan.end_hook``（规划层显式声明的末态）；机械拆分不产出它，
    所以回退路径退而取上一段正文的最后一行——官方格式下它就是该段最后一个动作节拍。
    展示时会标明是降级参照，不假装它是 end_hook。
    """
    if not previous_text:
        return None
    lines = [line.strip() for line in previous_text.strip().splitlines() if line.strip()]
    return lines[-1] if lines else None

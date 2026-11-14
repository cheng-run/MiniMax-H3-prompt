"""确定性桩管线（D4 spec §11）：没有真实模型也能跑完整闭环。

只为 Playwright CI 与本地调试存在：开关**只认环境变量** ``MINIMAX_FAKE_PIPELINE``
（默认关、无 HTTP 入口），``JobRunner`` 在桩模式下根本不调用 ``run_stage1`` /
``run_stage2``——真实与桩互斥，不是「真实路径里多一个 if 换掉回复」。

桩产出的状态与产物仍走**真实落盘路径**（``store.write_text_artifact``），所以浏览器
看到的是和真实跑一样的产物形状；唯一的差别是内容固定。修订会把用户意见拼进固定稿，
让「重跑后文件同步更新」在桩上也可观察。
"""
from __future__ import annotations

import math
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..segment_planner import SegmentPlan

# 开关名是启动日志里要被点名的那一个，调用方从常量取，不重抄字符串。
FAKE_PIPELINE_ENV = "MINIMAX_FAKE_PIPELINE"
# H3 产物是 736×1280@24fps（帧数表与尺寸同住 ``tools.preflight``）；桩造的视频要与
# 真实产物同档，否则段校验判它不合格——那时只剩「放宽闸门」一条路，而那是 CI 假绿。
H3_VIDEO_FPS = 24
_TRUTHY = frozenset({"1", "true", "yes", "on"})


def fake_pipeline_enabled() -> bool:
    """环境变量开着吗：只认明确真值，其它取值（含空串）一律当关。"""
    return os.environ.get(FAKE_PIPELINE_ENV, "").strip().lower() in _TRUTHY


def segment_video_frames(seconds: float) -> int:
    """这一段该有多少帧：只认 H3 实测档位（4/5/6 秒），档外直接报错。

    桩造件是「用户从 ComfyUI 跑完回交」的替身，而 H3 只有这几档可生成。档外秒数要么是
    规划错了、要么是测试写错了；静默按帧率折算会造出一段只拿得到「无法确证档位」warning
    的视频，CI 会误以为档位已被真实闸门验证（那是假绿）。
    """
    from ..tools.preflight import H3_TIER_FRAMES

    tier = int(round(seconds))
    if tier not in H3_TIER_FRAMES or abs(seconds - tier) > 1e-6:
        raise ValueError(
            f"桩管线只造 H3 实测档位的视频（{sorted(H3_TIER_FRAMES)} 整秒），"
            f"收到 {seconds:g} 秒")
    return H3_TIER_FRAMES[tier]


def ref_segment_video_frames(seconds: float) -> int:
    """多参执行段的帧数：段长是 H3 单体 4–15 秒的整数档，按 24fps 折算。

    与 ``segment_video_frames`` 有意分开：帧数表只校准到 6 秒，7–15 秒档没有实测样本。
    桩按帧率折算造件，真实闸门会给「无法确证档位」的 warning——这是**诚实**的，不冒充
    「档位已核对」；把 7–15 秒也塞进首尾帧链那张表反而是假绿。
    """
    from ..tools.preflight import SEGMENT_MAX_S, SEGMENT_MIN_S

    tier = int(round(seconds))
    if abs(seconds - tier) > 1e-6 or not (SEGMENT_MIN_S <= tier <= SEGMENT_MAX_S):
        raise ValueError(
            f"桩管线只造 {SEGMENT_MIN_S:g}–{SEGMENT_MAX_S:g} 整数秒的多参执行段，"
            f"收到 {seconds:g} 秒")
    return tier * H3_VIDEO_FPS


def _encode_stub_video(path: Path, frames: int) -> Path:
    """用 ffmpeg 现造一段合规 mp4：帧数由调用方按路线算好。

    编码器用 ffmpeg 内置的 ``mpeg4``——不赌发行版带没带 x264，容器仍是真 mp4
    （``ftyp``），读图剥尾帧与拼接都读得动。产物要过**真实**的
    ``preflight.check_segment_video``，桩不为它开后门。
    """
    import subprocess  # noqa: PLC0415 - 只有桩造件这一处需要拉子进程

    from ..tools.preflight import EXPECTED_VIDEO_SIZE
    from ..tools.seam_audit import find_ffmpeg

    width, height = EXPECTED_VIDEO_SIZE
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        [str(find_ffmpeg()), "-y", "-hide_banner", "-loglevel", "error",
         "-f", "lavfi", "-i",
         f"testsrc=size={width}x{height}:rate={H3_VIDEO_FPS}",
         "-frames:v", str(frames), "-c:v", "mpeg4", "-q:v", "3",
         "-pix_fmt", "yuv420p", str(target)],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        raise RuntimeError(
            f"桩管线造段视频失败（ffmpeg 退出 {proc.returncode}）：{proc.stderr[-800:]}")
    return target


def write_segment_video(path: Path, seconds: float) -> Path:
    """首尾帧链的桩段视频（D6-7）：帧数只认 H3 实测档位。"""
    return _encode_stub_video(path, segment_video_frames(seconds))


def write_ref_segment_video(path: Path, seconds: float) -> Path:
    """多参执行段的桩段视频（D7-6）：4–15 整数秒档，帧数按 24fps 折算。"""
    return _encode_stub_video(path, ref_segment_video_frames(seconds))


def write_director_segment_video(path: Path, frames: int) -> Path:
    """导演台段视频的桩件（#264）：帧数由调用方**直传**。

    为什么不复用 ``segment_video_frames``：那张表只认 H3 的 4/5/6 秒档，而导演台的
    执行契约是 17k+5 网格（有引导段导出恒为 17 的倍数）。为让桩也能造件而去放宽档位
    校验，等于把 CI 的档位判据换成桩自己的口径（假绿）。这里只借真实的编码封装。
    """
    if isinstance(frames, bool) or not isinstance(frames, int) or frames <= 0:
        raise ValueError(f"桩导演台视频需要正整数帧数，收到 {frames!r}")
    return _encode_stub_video(path, frames)


def _frame_roles(variant: str) -> tuple[tuple[int, str], ...]:
    # 变体 → 需要的帧位那份知识只住 ``frame_auditor.required_frames``（真实读图也用它）；
    # 桩不另立一张表，否则插进新变体时两边会各说各话。
    from ..tools.frame_auditor import required_frames

    return required_frames(variant)


def _frame_bundle(brief: Any, round_no: int) -> dict[str, Any] | None:
    """按变体要的帧位造一份合法 bundle；T2VA 没有帧，返回 None。"""
    roles = [role for _picture, role in _frame_roles(brief.variant)]
    if not roles:
        return None
    anchor = f"{brief.plot}，雨夜老城街口，霓虹灯在积水里反光"
    frames: dict[str, list[dict[str, Any]]] = {"first": [], "last": []}
    for role in roles:
        label = "开场" if role == "first" else "收束"
        frames[role].append({
            "frame": role,
            "model_family": "z_image",
            "positive_prompt": (
                f"{brief.plot}，桩管线第 {round_no} 稿的{label}画面，"
                "人物居中，中景，冷蓝霓虹侧逆光，写实电影感"),
            "composition": "中景，人物居中",
            "lighting": "冷蓝霓虹侧逆光",
            "scene_anchor": anchor,
        })
    return {
        "prompt_node_id": "187",
        "scene_anchor": anchor,
        "continuity_constraints": ["人物服装与道具在画面内保持一致"],
        "first": frames["first"],
        "last": frames["last"],
    }


def asset_prompt_text(name: str) -> str:
    """桩资产图词：按语义名固定成一句可直接复制的画面描述（D7-6）。

    词由 ``stage1_state`` 按台账语义名写进 state，出词 job 再经真实的
    ``generation.asset_prompt_for`` 取出来落盘——桩只换「词是谁写的」，取数与落盘
    仍是真实那一份，所以产物的形状与真实跑一致。
    """
    return (f"{name}：成年女性全身正面，米色风衣，黑色长柄伞，雨夜老城街口，"
            "中景，冷蓝霓虹侧逆光，写实电影感（桩管线固定词）")


def asset_proofread_verdict(
    number: int, first_number: int, round_no: int,
) -> tuple[bool, str]:
    """桩校对结论：**第 1 件的第一轮**不通过，其余一律通过（D7-6）。

    必须确定性：随机或按调用次数会在重跑/续跑时换结论，E2E 就时红时绿。判据用台账
    轮次（``round == 0`` 才是第一轮，与 ``_settle_asset`` 的转移规则同源），不数调用。
    差异清单要提到「词里是什么、图里是什么」，否则卡片上的红框没有内容可读。
    """
    if number != first_number or round_no > 0:
        return True, ""
    return False, (
        "差异清单（桩管线）：词里写的是「米色风衣、黑色长柄伞」，"
        "图里人物穿深灰外套、撑透明塑料伞——外套颜色与伞的材质不一致。"
        "按差异改词后重新生成一张，再交回这一件。"
    )


def rewrite_asset_prompt_text(current: str) -> str:
    """桩改写：按差异把修订句追加到原词尾部（不建模型、确定性）。

    与真实 ``rewrite_asset_prompt`` 的区别只在「谁写」：产物仍是同一份
    ``assets/picture-NN.md`` 的覆盖写，用户手里的下一版词看得到这次改了什么。
    """
    return (str(current).rstrip()
            + "\n\n修订（按校对差异）：外套改为米色，伞改为黑色长柄伞。")


def stage1_state(brief: Any, previous: dict[str, Any] | None = None) -> dict[str, Any]:
    """固定阶段 1 的 state；轮次与用户意见从 ``previous`` 里现读。

    轮次取「累积修订条数 + 1」而不是 ``_progress``：后者是续跑书签、不保证存在，
    而修订清单是 ``record_user_revision`` 真写进 state 的事实。
    """
    state = dict(previous or {})
    revisions = [
        str(entry.get("text", "")).strip()
        for entry in state.get("user_revisions") or []
        if isinstance(entry, dict) and str(entry.get("text", "")).strip()
    ]
    round_no = len(revisions) + 1
    refs = sorted(getattr(brief, "refs", ()) or (), key=lambda item: item.picture)
    script_lines = [
        f"# 分场剧本（桩管线第 {round_no} 稿）",
        "",
        f"主题：{brief.plot}",
        "",
        "分场 1：雨夜街口，人物撑伞走过积水，霓虹在积水里反光。",
        "分场 2：人物停在旧楼门前，收起伞，抬头看向窗内的灯。",
    ]
    if refs:
        script_lines += ["", "参考素材：", *[
            f"- <Picture {ref.picture}> {ref.name}" for ref in refs]]
    if revisions:
        script_lines += ["", "本轮修订：", *[f"- {text}" for text in revisions]]
    shot_lines = [f"# 分镜镜头表（桩管线第 {round_no} 稿）", ""]
    if refs:
        # 多参分镜表按台账语义名与 `<Picture N>` 写（D7 §10）：素材循环与段内子集
        # 都靠这个名字对号，桩不另编一套指称。
        shot_lines += [
            f"[Shot {index}] At 00:{2 * index:02d}.000, a medium shot featuring "
            f"{ref.name} <Picture {ref.picture}>, neon reflecting in the puddles."
            for index, ref in enumerate(refs, 1)]
    else:
        shot_lines += [
            "[Shot 1] At 00:00.000, a medium shot of the woman under a black umbrella "
            "in the rainy street.",
            "[Shot 2] At 00:02.000, a closer shot as she lowers the umbrella and looks up.",
        ]
    update: dict[str, Any] = {
        "production_plan": "制作计划：4 秒竖屏短视频，双场景呈现（桩管线）。",
        "director_brief": f"导演阐述：围绕「{brief.plot}」的写实电影感，低饱和冷调（桩管线）。",
        "creative_lock": "创意锁定：雨夜旧街，人物与旧信（桩管线）。",
        "script": "\n".join(script_lines),
        "character_design": "人物设计：一位成年女性，米色风衣，黑伞（桩管线）。",
        "background_design": "背景设计：雨夜老城街口，霓虹与积水（桩管线）。",
        "prop_design": "道具设计：一把黑伞、一封旧信（桩管线）。",
        "art_design": "美术统筹：冷蓝主调，霓虹点缀，湿反光（桩管线）。",
        "shot_table": "\n".join(shot_lines),
        "shot_review_lock": "镜头评审锁定：两镜，中景到近景（桩管线）。",
        "visual_design": "画面细化：手持跟随，冷蓝侧逆光，浅景深（桩管线）。",
        "sound_design": "对白表：无对白。环境声：雨声与脚步声（桩管线）。",
        "music": "non_diegetic_music：低频弦乐铺底（桩管线）。",
    }
    if refs:
        # 资产图词一件一份，语义名是台账与阶段 1 之间的唯一对号键（D7 §7）：出词
        # job 用真实的 ``asset_prompt_for`` 按名字取，取不到就硬失败而不是拿邻件顶替。
        update["character_image_prompts"] = [
            {"name": ref.name, "positive_prompt": asset_prompt_text(ref.name),
             "negative_prompt": "", "locked_features": []}
            for ref in refs]
    bundle = _frame_bundle(brief, round_no)
    if bundle is not None:
        update["fl2va_prompt_bundle"] = bundle
    state.update(update)
    return state


def frame_descriptions(
    brief: Any, variant: str, roles: tuple[str, ...],
) -> list[dict[str, Any]]:
    """假读图：只给**用户真的提交了、且有效变体要的**帧位，形状与
    ``FrameAudit.to_dict()`` 一致。

    D4-2 起它读的是用户提交的帧位（``first``/``last`` 由调用方从会话里取出）；
    桩不替用户脑补一帧——那会让「双空＝显式跳过」在 E2E 里看起来像交过帧。
    """
    slots = {role: picture for picture, role in _frame_roles(variant)}
    descriptions: list[dict[str, Any]] = []
    for role in roles:
        if role not in slots:
            continue
        label = "开场" if role == "first" else "收束"
        descriptions.append({
            "picture": slots[role],
            "role": role,
            "path": f"fake://{role}.png",
            "description": (
                f"桩读图：{brief.plot}的{label}画面，人物位于画面中部，"
                "冷蓝霓虹光线，浅景深。"),
        })
    return descriptions


def stage2_state(state: dict[str, Any], brief: Any,
                 descriptions: list[dict[str, Any]]) -> tuple[dict[str, Any], str]:
    """固定阶段 2：注入**调用方给的**读图结果、给一份固定视频提示词。

    ref 走官方六段式（D7-6）：分段规划按 ``detailed_description`` 里的镜头块切段、
    段内子集按 ``<Picture N>`` 标签收种子。桩若仍写 base 的三段式，分段器会退回
    「整片一个窗口」，段内参考素材那一行永远是空的——CI 就证明不了 D7 的接线。
    """
    updated = dict(state)
    updated["fl2va_frame_descriptions"] = descriptions
    refs = sorted(getattr(brief, "refs", ()) or (), key=lambda item: item.picture)
    if refs:
        shots = []
        for index, ref in enumerate(refs):
            seconds = 0 if index == 0 else int(round(brief.duration * index / len(refs)))
            shots.append(
                f"[Shot {index + 1}] At 00:{seconds:02d}.000, a medium shot of "
                f"{ref.name} <Picture {ref.picture}> in the rainy street, "
                "neon reflecting in the puddles.")
        prompt = "\n".join([
            "subject_definitions:",
            *[f"<Subject {ref.picture}>: the {ref.name} reference asset, shown as "
              f"<Picture {ref.picture}>." for ref in refs],
            "",
            "summary:",
            f"target_video: {brief.duration:g}s，桩管线多参整片提示词。",
            "",
            "retention_analysis:",
            "all subjects fully_preserved.",
            "",
            "detailed_description:",
            *shots,
            "",
            "overall_soundscape:",
            "雨声与脚步声（桩管线）。",
            "",
            "non_diegetic_music:",
            "低频弦乐铺底（桩管线）。",
        ])
    else:
        prompt = "\n".join([
            "# H3 视频提示词（桩管线）",
            "",
            f"主题：{brief.plot}　风格：{brief.style}　时长：{brief.duration:g}s",
            f"模式：{brief.mode}　变体：{brief.variant}",
            "",
            "[Shot 1] At 00:00.000, a medium shot of the woman under a black umbrella "
            "in the rainy street, neon reflecting in the puddles.",
            "[Shot 2] At 00:02.000, a closer shot as she lowers the umbrella and looks up.",
        ])
    updated["final_prompt"] = prompt
    return updated, prompt


def _tier_durations(total_s: int) -> list[int] | None:
    """把总时长拆成 H3 实测档位（``H3_TIER_FRAMES`` 的 4/5/6 秒），段数取最少。

    DP 而不是贪心：贪心在 13 秒会先拿 6 再卡在 7（拆不出 4/5/6），DP 得 5+4+4。
    总时长 ≤60，这个规模可以忽略。拆不出（如 7 秒）返回 None，由调用方退回等分。
    """
    from ..tools.preflight import H3_TIER_FRAMES

    tiers = tuple(sorted(H3_TIER_FRAMES))
    best: list[list[int] | None] = [None] * (total_s + 1)
    best[0] = []
    for part in range(1, total_s + 1):
        for tier in tiers:
            if tier > part:
                continue
            # 取到局部变量再收窄：mypy 不对下标表达式做 None 收窄
            head = best[part - tier]
            if head is None:
                continue
            candidate = [*head, tier]
            current = best[part]
            if current is None or len(candidate) < len(current):
                best[part] = candidate
    return best[total_s]


def director_plan(mode: str, total_s: float) -> Any:
    """桩导演台规划：帧网格自洽、Σ 导出＝总帧，读回不炸（#262 / #266）。

    长片 ``fl2v``（>10 秒）走**三段**（开场 / 切镜 / 收尾）：E2E 要覆盖切镜段的
    段首图交接与两种按需词，单段表根本走不到那些分支。短 ``fl2v`` 与其余模式仍是
    单段开场——既有用例只验证「有段表」，多造段只会让它们多等几个后台 job。

    帧数取 107/107/119：首两段无引导（声明＝导出 107，17×6+5），收尾有引导（声明
    107 → 导出 119＝17×7），合计 333。目标总帧常不可达（96 帧不在网格上），单段分支
    仍按 ``align_frames`` 向上吸附。
    """
    from ..director.frames import (
        FPS, DirectorPlan, DirectorSegment, align_frames, give_images_for, is_guided)

    def _segment(index: int, segment_type: str, start: int,
                 declared: int, export: int) -> Any:
        return DirectorSegment(
            index=index, type=segment_type, guided=is_guided(segment_type),
            start_frame=start, end_frame=start + export,
            declared_frames=declared, export_frames=export,
            summary=f"桩管线导演台第 {index} 段：雨夜街口",
            end_hook=f"桩管线：第 {index} 段结束时画面落定",
            give_images=give_images_for(segment_type, mode),
        )

    if mode == "fl2v" and float(total_s) > 10:
        segments = (
            _segment(1, "opening", 0, 107, 107),
            _segment(2, "camera_cut", 107, 107, 107),
            _segment(3, "ending", 214, 107, 119),
        )
        return DirectorPlan(mode=mode, total_frames=333, segments=segments)
    landed = align_frames(round(float(total_s) * FPS))
    segment = _segment(1, "opening", 0, landed, landed)
    return DirectorPlan(mode=mode, total_frames=landed, segments=(segment,))


def director_segment_prompt(plan: Any, segment: Any) -> str:
    """桩导演台段词（#262）：三字段外壳齐全的固定正文，不建模型。

    桩不与真实写段抢道（桩模式 ``build_chat_model`` 一调就抛）：落盘仍走工作台
    执行器那一份真实路径，形状与浏览器看到的完全一致。
    """
    return (
        "integrated_multimodal_description:\n"
        f"[Shot 1] At 00:00.000, a fixed stub establishing shot for director "
        f"segment {segment.index}/{len(plan.segments)}.\n\n"
        "overall_soundscape:\nRain and footsteps (stub).\n\n"
        "non_diegetic_music:\nLow sustained strings (stub)."
    )


def director_firstframe_prompt(segment: Any) -> str:
    """桩段首帧词（#264）：不建模型，正文形状与真实产物一致（可直接复制给生图模型）。

    真实写词要走 ``frame_prompt_engineer`` 角色与读图；桩只替「谁写这句话」，落盘与
    产物登记仍走执行器那一份真实路径。
    """
    return (
        f"第 {segment.index} 段段首帧（桩管线）：雨夜老城街口，撑黑伞的成年女性，"
        "中景，冷蓝霓虹侧逆光，写实电影感")


def director_tailframe_prompt(segment: Any, start_image: str | Path) -> str:
    """桩尾帧词（#264）：三段式外壳齐全，起点画面路径原样点出。

    真实路径会先用视觉模型读起点画面再写变换；桩把这一步换成固定文案，避免在 CI
    里建模型或读图。``start_image`` 仍由执行器按真实规则选（本段首帧/上一段末帧）。
    """
    return "\n".join([
        f"### 变换指令（第 {segment.index} 段，桩管线）",
        f"以起点画面 {Path(start_image).name} 为参考，把人物转为回望姿态。",
        "",
        "### 保留项声明",
        "人物服装、道具与场景地标保持不变。",
        "",
        "### 锁死清单",
        "米色风衣；黑色长柄伞。",
    ])


def director_ref_sections(registry: Any) -> Any:
    """桩 r2v 全片定义块（D8-8 / #268）：确定性英文两节，每张登记图都被定义。

    真实路径要走一次模型调用把阶段 1 的中文 ``subject_defs`` 改写成英文；桩只替
    「谁写这句话」，产物形态与 ``refdefs.RefSections`` 完全一致（续跑缓存、登记表
    比对与写段拼装都走真实代码）。语言按校验器口径写成英文，避免 ``BODY_NOT_ENGLISH``。
    """
    from ..director.refdefs import RefSections

    rows = [(int(item[0]), str(item[1])) for item in registry]
    definitions = "\n".join(
        f"<Picture {number}> is a registered reference image for the scene."
        for number, _name in rows)
    retention = "\n".join(
        f"<Picture {number}> fully_preserved." for number, _name in rows)
    return RefSections(
        subject_definitions=definitions,
        retention_analysis=retention,
        registry=tuple(rows),
    )


def director_segment_reply() -> str:
    """桩 r2v 写段回复：只给模型写的那几节，定义块与时间轴注记由真实组装器注入。

    ``<Picture 1>`` 是随手引用的登记图——单段桩片子的登记表至少有一张（起跑闸门
    保证）；段内子集因此由真实的 ``compute_segment_scope`` 算出来，而不是桩另编。
    """
    return (
        "summary:\n"
        "target_video: 4 秒雨夜街口（桩管线）。\n\n"
        "detailed_description:\n"
        "[Shot 1] At 00:00.000, a medium shot of the subject <Picture 1> in the "
        "rainy street, neon reflecting in the puddles.\n\n"
        "overall_soundscape:\n"
        "Rain on the pavement and steady footsteps (stub).\n\n"
        "non_diegetic_music:\n"
        "Low sustained strings (stub)."
    )


class StubChatModel:
    """写段直调用的最小桩：``invoke`` 返回固定正文，不建客户端、不走网络。

    形状按 ``run_direct_call`` 的唯一要求（``invoke(request, config=…)`` →
    带 ``content`` 的对象）裁剪；用量字段缺省即 0（桩不该产生成本数字）。
    """

    def __init__(self, reply: str) -> None:
        self._reply = reply

    def invoke(self, request: Any, config: Any = None) -> Any:
        from langchain_core.messages import AIMessage

        return AIMessage(content=self._reply)


def segment_plan(total_s: int) -> list[SegmentPlan]:
    """桩分段规划（D6-1）：把总时长拆成实测帧数表里的档位（4/5/6 秒）。

    真实规划允许 4–10 秒，H3 节点本身支持 4–15 秒整数档；桩只用**实测帧数表**里的档位
    造件，是为了让 CI 能精确判定档位合规，不是生成能力的上限。桩若仍按 10 秒上限等分，
    18 秒会拆成 2×9、24 秒拆成 3×8，这些段在真实闸门上只能拿到「无法确证档位」的
    warning，CI 就证明不了档位合规。拆不出档位组合的时长（如 7 秒）退回等分，让真实
    闸门照常判定——桩不假装合规。``shots_in_segment`` 留空：桩不编造分镜归属。
    """
    from ..segment_planner import BASE_MAX_SEG_S, SegmentPlan

    durations = _tier_durations(int(total_s))
    if not durations:
        count = max(1, math.ceil(total_s / BASE_MAX_SEG_S))
        base = total_s // count
        durations = [base] * count
        durations[-1] += total_s - base * count
    plans = []
    start = 0
    for index, duration in enumerate(durations):
        plans.append(SegmentPlan(
            index=index,
            start_s=start,
            end_s=start + duration,
            shots_in_segment=(),
            summary=f"桩管线第 {index + 1} 段",
            end_hook=f"桩管线：第 {index + 1} 段结束时画面落定",
        ))
        start += duration
    return plans


def segment_prompt(
    plan: SegmentPlan, all_plans: list[SegmentPlan], state: dict[str, Any],
) -> str:
    """桩写段（D6 #246）：不建模型，给一段形状合法、可展示的固定提示词。

    桩不与真实写段抢道：真实路径 ``write_segment_v2`` 会调模型，桩模式
    ``build_chat_model`` 一调就抛。段号与末态声明从规划里现读，让每段正文不同、
    段尾钩子可核对，但不做语义闸门的真判（那一步由 ``judge_segment_end_hook`` 桩掉）。
    """
    number = int(plan.index) + 1
    total = len(all_plans)
    lines = [
        f"[Shot 1] At 00:00.000, a fixed stub establishing shot for segment {number}/{total}.",
        f"[Shot 1] At 00:02.000, the stub action settles toward: {plan.end_hook}",
    ]
    return "\n".join(lines)


def segment_tail_description(plan: Any) -> str:
    """桩读图（D6 #246）：尾帧画面固定落在本段末态声明上——供桩管线走通「通过」这一档。"""
    return f"桩读图：第 {int(plan.index) + 1} 段尾帧，画面落在「{plan.end_hook}」。"


def judge_segment_end_hook(end_hook: str, description: str) -> bool:
    """桩语义判据（D6 #246）：桩管线里恒判「达到」，不建模型、不联网。

    被拒/失败两档是**真实闸门**才可能出现的结局；桩管线只保证闭环能走完（E2E/Playwright），
    不靠它证明闸门有牙齿——那两条由真实路径的用例（打桩读图/判据）咬。
    """
    return True


def stage2_summary(brief: Any) -> Any:
    """桩阶段 2 的固定中文摘要：整体一句、无分镜条目。

    桩不与真实模型抢道：这里直接造 ``PromptSummary`` 而不建模型——桩模式下
    ``build_chat_model`` 本来就是「一调就抛」。渲染与落盘仍走真实路径的同一份代码。
    """
    from ..summary import PromptSummary

    return PromptSummary(
        overall=(f"《{brief.plot}》的桩管线摘要：雨夜街口的短视频，"
                 "两镜推进到人物收伞回望（固定文案，非模型产出）。"),
        shots=[],
    )


__all__ = [
    "FAKE_PIPELINE_ENV",
    "H3_VIDEO_FPS",
    "StubChatModel",
    "asset_prompt_text",
    "asset_proofread_verdict",
    "director_firstframe_prompt",
    "director_ref_sections",
    "director_segment_reply",
    "director_tailframe_prompt",
    "fake_pipeline_enabled",
    "frame_descriptions",
    "judge_segment_end_hook",
    "ref_segment_video_frames",
    "rewrite_asset_prompt_text",
    "segment_plan",
    "segment_prompt",
    "segment_tail_description",
    "segment_video_frames",
    "stage1_state",
    "stage2_state",
    "stage2_summary",
    "write_director_segment_video",
    "write_ref_segment_video",
    "write_segment_video",
]

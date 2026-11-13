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


def write_segment_video(path: Path, seconds: float) -> Path:
    """用 ffmpeg 现造一段「用户去 ComfyUI 跑完回交」的替身视频（D6-7）。

    造件参数（帧数 / 尺寸 / 帧率）全部从仓内常量派生，不手抄字面量；编码器用 ffmpeg
    内置的 ``mpeg4``——不赌发行版带没带 x264，容器仍是真 mp4（``ftyp``），读图剥尾帧与
    拼接都读得动。产物要过**真实**的 ``preflight.check_segment_video``，桩不为它开后门。
    """
    import subprocess  # noqa: PLC0415 - 只有桩造件这一处需要拉子进程

    from ..tools.preflight import EXPECTED_VIDEO_SIZE
    from ..tools.seam_audit import find_ffmpeg

    frames = segment_video_frames(seconds)
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
    script_lines = [
        f"# 分场剧本（桩管线第 {round_no} 稿）",
        "",
        f"主题：{brief.plot}",
        "",
        "分场 1：雨夜街口，人物撑伞走过积水，霓虹在积水里反光。",
        "分场 2：人物停在旧楼门前，收起伞，抬头看向窗内的灯。",
    ]
    if revisions:
        script_lines += ["", "本轮修订：", *[f"- {text}" for text in revisions]]
    update: dict[str, Any] = {
        "production_plan": "制作计划：4 秒竖屏短视频，双场景呈现（桩管线）。",
        "director_brief": f"导演阐述：围绕「{brief.plot}」的写实电影感，低饱和冷调（桩管线）。",
        "creative_lock": "创意锁定：雨夜旧街，人物与旧信（桩管线）。",
        "script": "\n".join(script_lines),
        "character_design": "人物设计：一位成年女性，米色风衣，黑伞（桩管线）。",
        "background_design": "背景设计：雨夜老城街口，霓虹与积水（桩管线）。",
        "prop_design": "道具设计：一把黑伞、一封旧信（桩管线）。",
        "art_design": "美术统筹：冷蓝主调，霓虹点缀，湿反光（桩管线）。",
        "shot_table": "\n".join([
            f"# 分镜镜头表（桩管线第 {round_no} 稿）",
            "",
            "[Shot 1] At 00:00.000, a medium shot of the woman under a black umbrella "
            "in the rainy street.",
            "[Shot 2] At 00:02.000, a closer shot as she lowers the umbrella and looks up.",
        ]),
        "shot_review_lock": "镜头评审锁定：两镜，中景到近景（桩管线）。",
        "visual_design": "画面细化：手持跟随，冷蓝侧逆光，浅景深（桩管线）。",
        "sound_design": "对白表：无对白。环境声：雨声与脚步声（桩管线）。",
        "music": "non_diegetic_music：低频弦乐铺底（桩管线）。",
    }
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
    """固定阶段 2：注入**调用方给的**读图结果、给一份固定视频提示词。"""
    updated = dict(state)
    updated["fl2va_frame_descriptions"] = descriptions
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
            if tier > part or best[part - tier] is None:
                continue
            candidate = [*best[part - tier], tier]
            if best[part] is None or len(candidate) < len(best[part]):
                best[part] = candidate
    return best[total_s]


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
    "fake_pipeline_enabled",
    "frame_descriptions",
    "judge_segment_end_hook",
    "segment_plan",
    "segment_prompt",
    "segment_tail_description",
    "segment_video_frames",
    "stage1_state",
    "stage2_state",
    "stage2_summary",
    "write_segment_video",
]

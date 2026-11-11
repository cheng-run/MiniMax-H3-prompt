"""确定性桩管线（D4 spec §11）：没有真实模型也能跑完整闭环。

只为 Playwright CI 与本地调试存在：开关**只认环境变量** ``MINIMAX_FAKE_PIPELINE``
（默认关、无 HTTP 入口），``JobRunner`` 在桩模式下根本不调用 ``run_stage1`` /
``run_stage2``——真实与桩互斥，不是「真实路径里多一个 if 换掉回复」。

桩产出的状态与产物仍走**真实落盘路径**（``store.write_text_artifact``），所以浏览器
看到的是和真实跑一样的产物形状；唯一的差别是内容固定。修订会把用户意见拼进固定稿，
让「重跑后文件同步更新」在桩上也可观察。
"""
from __future__ import annotations

import os
from typing import Any

# 开关名是启动日志里要被点名的那一个，调用方从常量取，不重抄字符串。
FAKE_PIPELINE_ENV = "MINIMAX_FAKE_PIPELINE"
_TRUTHY = frozenset({"1", "true", "yes", "on"})


def fake_pipeline_enabled() -> bool:
    """环境变量开着吗：只认明确真值，其它取值（含空串）一律当关。"""
    return os.environ.get(FAKE_PIPELINE_ENV, "").strip().lower() in _TRUTHY


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


__all__ = [
    "FAKE_PIPELINE_ENV",
    "fake_pipeline_enabled",
    "frame_descriptions",
    "stage1_state",
    "stage2_state",
]

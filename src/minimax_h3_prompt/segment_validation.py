"""段交接的两道判据（D6 #246；#270 把语义判据降级为提示）。

技术档判据不在本模块重写——它直接复用 ``tools/preflight.check_segment_video``
（吃文件路径、按 H3 实测帧数表判档）。本模块只补两件 CLI 侧靠人做的事：

- **同步提交自检**：段号是不是当前段、文件在不在、可证层有没有 error（``segment_submission_problems``）。
- **语义提示**：尾帧读图描述与本段 ``end_hook`` 的**关键事实**差异说明（CLI 侧是并排摆给
  人判，``ui/wizard._judge_bridge_semantics``；工作台无人可问，自动生成一句提示）。
- **交回图尺寸提示**（#365）：用户交回的关键帧图与当前画布的宽高不符时给一句提示
  （``frame_canvas_warning_text``）；与上面两道同族，只提示、不阻断。

判据本身是纯函数：事实进、结论出。读图与模型调用在调用方（``api/jobs.py``），
桩管线把「读图 + 判据」一起桩掉（``api/fake_pipeline.py``）。

**为什么不再返回布尔**（#270 / ADR 0039）：``end_hook`` 是规划期写下的**意图**，不是成片
的验收判据；真正保证段间衔接的是剥出来的实际尾帧图片。计划与成片有偏差是质量问题，
应该让人看见并选择，不该结构性地阻断流程。模型调用入口因此只返回「差异说明」字符串
（空串＝无提示），调用方没有任何分支能因它把段判为不合格。
"""
from __future__ import annotations

import logging
from collections.abc import Mapping
from pathlib import Path

from .tools.preflight import check_segment_video

_hint_logger = logging.getLogger(__name__)

# 语义判据直调的角色名（ADR 0019：成本表与进度面板按角色分账）。与写段那几个角色
# 并列，成本表上能看出钱花在「判段」而不是「写段」。
SEGMENT_END_HOOK_JUDGE_ROLE = "role_segment_end_hook_judge"


def segment_submission_problems(
    *, current_index: int, number: int, video_path: Path | str,
    expected_seconds: float,
) -> list[str]:
    """resolve 时的同步判定：段号必须是当前段，且技术档合格。返回原因列表（空＝放行）。

    「文件读不到」必须当**不合格**：``check_segment_video`` 对读不到的文件只报 warning
    （``tools.preflight.check_segment_video``），照直调会让「文件不在」被当成通过。
    CLI 的补法是把 ``is_file`` 判在调用之前（``ui.wizard._gate_segment_handoff``），
    这里照同一口径补。
    """
    problems: list[str] = []
    if int(number) != int(current_index):
        problems.append(
            f"待办段号 {number} 与当前段 {current_index} 不一致——可能拿到了旧段的待办")
    path = Path(video_path)
    if not path.is_file():
        problems.append(f"读不到这段输出视频：{path}（文件不在，或路径打错了）")
        return problems
    issues = check_segment_video(path, expected_seconds)
    problems.extend(
        f"{issue.code}: {issue.message}"
        for issue in issues if issue.severity == "error")
    return problems


def build_end_hook_prompt(end_hook: str, description: str) -> str:
    """语义判据的模型请求：只核**关键事实**冲突，不核细节与未入画部位（#270）。

    旧口径把「达到/未达到」当闸门，读图模型把人物自身左右说反、或要求核一个根本没入画
    的部位，都会让整条长片卡死。新口径只判四类事实：场景/地点、主体是否在场、本段大动作
    是否发生、关键道具落在哪；其余一律不算不符。回复两行：第一行「一致/冲突」，
    第二行差异说明（一致时写「无」）。

    #360 在**既有两类上各补一项**，不新开第五类：场景/地点含「人物所坐或所靠的支撑面」，
    关键道具原拟补「道具的形态或类型」。支撑面是粗粒度事实，纳入；**道具的形态或类型已
    按 #360／#362 实测撤回**——在既有真实产物上实测，同一只单肩包在「放在身旁」时肩带被
    遮挡，读图模型反复把它改判成「手提包／旅行包／圆筒状旅行包」（同一物体换了 4 种类型
    标签），且顺带把「挂左肩还是右肩」也判成冲突（那本在排除清单内）。道具形态项观测到的
    冲突**全部是误报、无一真阳性**，故只保留支撑面。排除清单仍只留原来四项（人物自身左右／
    具体哪只手／视线朝向／未入画部位）；提示词另起一句点明支撑面**不在**排除清单内，因为
    「只靠模型领会这条区分」等于没有区分。
    """
    return (
        "你是视频分段接力的检查员。下面并排给出本段计划的末态声明与实际尾帧画面描述，"
        "只判断**关键事实**是否冲突：场景/地点（含人物所坐或所靠的支撑面，"
        "如堤顶还是矮墙、桥面还是台阶）、主体是否在场、本段的大动作是否发生、"
        "关键道具落在哪。\n"
        "以下情况不算不符：人物自身的左右、具体是哪只手、视线朝向、描述里没有覆盖到的内容、"
        "镜头没有拍到的未入画部位。\n"
        "注意：人物所坐或所靠的支撑面不在排除清单内——"
        "它属于上面要核的关键事实，与那几项细节不同，冲突时要判。\n"
        f"本段末态声明：{end_hook}\n"
        f"尾帧实际画面描述：{description}\n"
        "回答两行：第一行只写「一致」或「冲突」；第二行写一句差异说明，一致时第二行写「无」。"
    )


# 「无冲突」类先判：``无冲突`` 里含 ``冲突``、``不一致`` 里含 ``一致``，顺序反了会
# 把结论判反（旧布尔解析的同一个坑）。
_NO_CONFLICT_MARKERS = ("无冲突", "没有冲突", "无差异", "没有差异", "no conflict", "match")
_CONFLICT_MARKERS = (
    "不一致", "不符", "冲突", "差异", "不同", "mismatch", "conflict", "not consistent")
_CONSISTENT_MARKERS = ("一致", "相同", "相符", "consistent")


def parse_end_hook_reply(text: str) -> str | None:
    """把判据模型的回复归一成「差异说明」：空串＝无提示，非空＝提示文本。

    回复第一行判关键事实是否冲突，第二行是差异说明。认不出第一行返回 ``None``——那是
    判据服务坏了，调用方按「语义检查未完成」处理，**不判失败**。
    """
    lines = [line.strip() for line in str(text or "").strip().splitlines()
             if line.strip()]
    if not lines:
        return None
    head = lines[0].lower()
    detail = lines[1] if len(lines) > 1 else ""
    if any(marker in head for marker in _NO_CONFLICT_MARKERS):
        return ""
    if any(marker in head for marker in _CONFLICT_MARKERS):
        return detail or lines[0]
    if any(marker in head for marker in _CONSISTENT_MARKERS):
        return ""
    return None


def judge_end_hook(end_hook: str, description: str, llm) -> str:
    """尾帧与本段 ``end_hook`` 的差异说明：空串＝无提示，非空＝给用户看的提示正文。

    旧签名返回 ``True/False`` 并由收尾据此落 ``passed``/``not_reached``（#246）；#270 起
    返回值只是一句提示，**没有任何调用方可因它阻断流程**。回复不可解析或调用失败时抛错，
    由调用方降级成「语义检查未完成」提示。``end_hook`` 为空视为无末态可判——不提示也不
    阻断（规划器保证非空；旧 plan.json 缺这一键时按无约束处理，与 CLI 的诚实降级同向）。

    调用走统一看门狗（ADR 0019：不许裸 ``llm.invoke``）：判据同样是一次慢速模型调用，
    卡住要有首字节心跳与重试预算，失败要把病因交回调用方。
    """
    if not str(end_hook or "").strip():
        return ""
    from .agents import run_direct_call  # noqa: PLC0415 - agents 会拉起角色装配栈

    result = run_direct_call(
        llm, build_end_hook_prompt(end_hook, description),
        role=SEGMENT_END_HOOK_JUDGE_ROLE)
    if not result.ok:
        raise RuntimeError(
            f"语义判据调用失败（已试 {result.attempts} 次）：{result.error}")
    warning = parse_end_hook_reply(result.text)
    if warning is None:
        raise ValueError(f"语义判据回复不可解析：{result.text!r}")
    return warning


def semantic_warning_text(difference: str, end_hook: str, description: str) -> str:
    """段通过后的语义提示正文：差异说明 + 计划末态 + 实际尾帧（供证据复核）。

    三个事实缺一省略，但差异说明本身是提示的起点——没有它就不该生成提示。
    """
    parts = [f"语义检查提示：{difference.strip()}"]
    if str(end_hook or "").strip():
        parts.append(f"计划末态：{end_hook.strip()}")
    if str(description or "").strip():
        parts.append(f"尾帧画面：{description.strip()}")
    return "｜".join(parts)


def validation_incomplete_text(reason: str) -> str:
    """读图/判据不可用时的提示：段照常通过，只说明这一项没做成。"""
    return f"语义检查未完成：{str(reason or '').strip()}"


FRAME_CANVAS_HINT_PREFIX = "关键帧尺寸提示"


def frame_canvas_warning_text(
    image_path: Path | str,
    expected: tuple[int, int],
    *,
    label: str = "关键帧",
) -> str:
    """交回的关键帧图与实际画布是否对得上：空串＝无提示，非空＝给用户看的一句提示。

    与 :func:`semantic_warning_text` 同族——**只产出提示**，不参与任何 ``passed`` /
    拒绝判定。用户有时会有意用小尺寸图做快速试跑，不符、读不出都不该拦住他交图。

    三种结论分开（票面 #365）：

    - 尺寸不符 → 一句含**两边数值**的提示（「我该改哪一边」要靠两边数字）；
    - 尺寸相符 → 空串（没有差异就没有声音）；
    - 读不出宽高（损坏 / 不是图片）→ 一句「量不出来」的说明。**它不是尺寸不符**：
      把坏图说成「尺寸错了」会把人的排查方向带偏。

    读图复用 ``director.aspect.image_size``（唯一一份实现）；期望画布由调用方取好
    再传进来，本函数不读配置。
    """
    from .director.aspect import image_size  # noqa: PLC0415 - 只有这一处要 PIL

    expected_width, expected_height = int(expected[0]), int(expected[1])
    actual = image_size(image_path)
    if actual is None:
        return (f"{FRAME_CANVAS_HINT_PREFIX}：读不出{label}图的宽高"
                f"（文件损坏或不是图片），量不出来；{label}图照常收下。")
    actual_width, actual_height = actual
    if (actual_width, actual_height) == (expected_width, expected_height):
        return ""
    return (
        f"{FRAME_CANVAS_HINT_PREFIX}：{label}图 {actual_width}×{actual_height} 与"
        f"当前画布 {expected_width}×{expected_height} 不一致——"
        f"生成时会按画布尺寸出图、构图词也按它的画幅族落笔，"
        f"图是别的尺寸通常意味着用的不是这一档的画布；不阻断，{label}图照常收下。")


FRAME_CANVAS_LABELS: tuple[tuple[str, str], ...] = (("first", "首帧"), ("last", "尾帧"))
"""帧槽角色 → 给人看的标签。CLI 与工作台共用同一份顺序与称呼（先首后尾）。"""


def frame_canvas_hints_for(
    frames: Mapping[str, Path | str | None],
    expected: tuple[int, int] | None,
) -> str:
    """逐帧算尺寸提示，合成一段（空串＝无提示）；**降级只在这一处**（#363 复审 A3）。

    为什么要有这个收口：CLI（``ui.wizard_display.announce_frame_canvas_hints``）与
    工作台（``api.jobs._frame_canvas_hint``）都要「取期望画布 → 逐帧算 → 收非空」，
    原先各写一遍，且降级口径分家——工作台两处 ``except`` 把失败吞成空提示，CLI
    侧一处都没有，``frame_canvas`` 读坏 ``config/image.yaml``（``ConfigError``）会
    炸穿阶段 2。提示是**附言**，附言算不出来就该一声不响，绝不能把主流程带停。

    ``expected`` 为 ``None``（调用方自己取画布时出错）时直接返回空串——没有可核的
    期望值，就没有提示。每张图一条、换行分隔：首尾两帧可能各自不符，合成一句会丢掉
    另一张的数值。
    """
    if expected is None or not frames:
        return ""
    notes: list[str] = []
    for role, label in FRAME_CANVAS_LABELS:
        path = frames.get(role)
        if path is None:
            continue
        try:
            note = frame_canvas_warning_text(path, expected, label=label)
        except Exception as exc:  # noqa: BLE001 - 量不出尺寸不该拦住交图
            _hint_logger.warning("交回 %s 图的尺寸提示算不出来，跳过：%s", label, exc)
            continue
        if note:
            notes.append(note)
    return "\n".join(notes)


__all__ = [
    "FRAME_CANVAS_HINT_PREFIX",
    "FRAME_CANVAS_LABELS",
    "SEGMENT_END_HOOK_JUDGE_ROLE",
    "build_end_hook_prompt",
    "frame_canvas_hints_for",
    "frame_canvas_warning_text",
    "judge_end_hook",
    "parse_end_hook_reply",
    "segment_submission_problems",
    "semantic_warning_text",
    "validation_incomplete_text",
]

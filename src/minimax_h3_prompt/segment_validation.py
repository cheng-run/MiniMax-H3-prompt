"""段交接的两道判据（D6 #246；#270 把语义判据降级为提示）。

技术档判据不在本模块重写——它直接复用 ``tools/preflight.check_segment_video``
（吃文件路径、按 H3 实测帧数表判档）。本模块只补两件 CLI 侧靠人做的事：

- **同步提交自检**：段号是不是当前段、文件在不在、可证层有没有 error（``segment_submission_problems``）。
- **语义提示**：尾帧读图描述与本段 ``end_hook`` 的**关键事实**差异说明（CLI 侧是并排摆给
  人判，``ui/wizard._judge_bridge_semantics``；工作台无人可问，自动生成一句提示）。

判据本身是纯函数：事实进、结论出。读图与模型调用在调用方（``api/jobs.py``），
桩管线把「读图 + 判据」一起桩掉（``api/fake_pipeline.py``）。

**为什么不再返回布尔**（#270 / ADR 0039）：``end_hook`` 是规划期写下的**意图**，不是成片
的验收判据；真正保证段间衔接的是剥出来的实际尾帧图片。计划与成片有偏差是质量问题，
应该让人看见并选择，不该结构性地阻断流程。模型调用入口因此只返回「差异说明」字符串
（空串＝无提示），调用方没有任何分支能因它把段判为不合格。
"""
from __future__ import annotations

from pathlib import Path

from .tools.preflight import check_segment_video

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
    """
    return (
        "你是视频分段接力的检查员。下面并排给出本段计划的末态声明与实际尾帧画面描述，"
        "只判断**关键事实**是否冲突：场景/地点、主体是否在场、本段的大动作是否发生、"
        "关键道具落在哪。\n"
        "以下情况不算不符：人物自身的左右、具体是哪只手、视线朝向、描述里没有覆盖到的内容、"
        "镜头没有拍到的未入画部位。\n"
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


__all__ = [
    "SEGMENT_END_HOOK_JUDGE_ROLE",
    "build_end_hook_prompt",
    "judge_end_hook",
    "parse_end_hook_reply",
    "segment_submission_problems",
    "semantic_warning_text",
    "validation_incomplete_text",
]

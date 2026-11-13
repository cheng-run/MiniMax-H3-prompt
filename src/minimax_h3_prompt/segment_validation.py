"""段交接的两道判据（D6 #246）：同步技术档 + 异步语义闸门。

技术档判据不在本模块重写——它直接复用 ``tools/preflight.check_segment_video``
（吃文件路径、按 H3 实测帧数表判档）。本模块只补两件 CLI 侧靠人做的事：

- **同步提交自检**：段号是不是当前段、文件在不在、可证层有没有 error（``segment_submission_problems``）。
- **语义自动判据**：尾帧读图描述有没有达到本段的 ``end_hook``（CLI 侧是并排摆给人判，
  ``ui/wizard._judge_bridge_semantics``；工作台无人可问，需要一条可自动执行、可桩的判据）。

判据本身是纯函数：事实进、结论出。读图与模型调用在调用方（``api/jobs.py``），
桩管线把「读图 + 判据」一起桩掉（``api/fake_pipeline.py``）。
"""
from __future__ import annotations

from pathlib import Path

from .tools.preflight import check_segment_video

# 语义判据直调的角色名（ADR 0019：成本表与进度面板按角色分账）。与写段那几个角色
# 并列，成本表上能看出钱花在「判段」而不是「写段」。
SEGMENT_END_HOOK_JUDGE_ROLE = "role_segment_end_hook_judge"

# 语义判据的两个结论。空串/不可解析既不是「达到」也不是「未达到」——那是**系统故障**
# （读图或判据模型给了没法用的回复），调用方按 ``failed`` 收尾，不落段状态。
END_REACHED = "reached"
END_NOT_REACHED = "not_reached"


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
    """语义判据的模型请求：把「该达到的末态」与「实际尾帧画面」并排摆给模型判。"""
    return (
        "你是视频分段接力的检查员。判断下面这张视频尾帧的实际画面描述，"
        "是否达到了本段要求的末态声明。\n"
        f"本段末态声明：{end_hook}\n"
        f"尾帧实际画面描述：{description}\n"
        "只回答一行：达到请回「达到」，未达到请回「未达到」，不要解释。"
    )


_NEGATIVE_MARKERS = ("未达到", "没达到", "没有达到", "not reached", "no", "false")
_AFFIRMATIVE_MARKERS = ("达到", "reached", "yes", "true")


def parse_end_hook_verdict(text: str) -> bool | None:
    """把判据模型的回复归一成 ``True/False``；认不出返回 ``None``（调用方当系统故障）。

    否定词先判：``未达到`` 里含 ``达到``，顺序反了会把不合格判成合格。
    """
    low = str(text or "").strip().lower()
    if not low:
        return None
    if any(marker in low for marker in _NEGATIVE_MARKERS):
        return False
    if any(marker in low for marker in _AFFIRMATIVE_MARKERS):
        return True
    return None


def judge_end_hook(end_hook: str, description: str, llm) -> bool:
    """尾帧画面是否达到 ``end_hook``：``True`` 达到、``False`` 未达到。

    回复不可解析时**抛错**（不是「未达到」）：那是判据服务坏了，调用方要落 ``failed``
    让用户重试，而不是让用户白换一段视频。``end_hook`` 为空视为无末态可判——不阻断
    （规划器保证非空；旧 plan.json 缺这一键时按无约束处理，与 CLI 的诚实降级同向）。

    调用走统一看门狗（ADR 0019：不许裸 ``llm.invoke``）：判据同样是一次慢速模型调用，
    卡住要有首字节心跳与重试预算，失败要把病因交回调用方落 ``failed``。
    """
    if not str(end_hook or "").strip():
        return True
    from .agents import run_direct_call  # noqa: PLC0415 - agents 会拉起角色装配栈

    result = run_direct_call(
        llm, build_end_hook_prompt(end_hook, description),
        role=SEGMENT_END_HOOK_JUDGE_ROLE)
    if not result.ok:
        raise RuntimeError(
            f"语义判据调用失败（已试 {result.attempts} 次）：{result.error}")
    verdict = parse_end_hook_verdict(result.text)
    if verdict is None:
        raise ValueError(f"语义判据回复不可解析：{result.text!r}")
    return verdict


__all__ = [
    "END_NOT_REACHED",
    "END_REACHED",
    "SEGMENT_END_HOOK_JUDGE_ROLE",
    "build_end_hook_prompt",
    "judge_end_hook",
    "parse_end_hook_verdict",
    "segment_submission_problems",
]

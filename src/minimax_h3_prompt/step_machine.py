"""生成会话的显式步骤机（D2 §4 / issue #216）。

它只回答「下一步该干什么」：阶段 1 → 人工闸门（``stage1_review``）→ 阶段 2 → 交付。
不提问、不画界面、不读写会话文件——那些是调用方的事（今天是向导，D2 之后还有执行器）。

纯数据 + 纯函数：事实进、动作出。同一份声明接不同的执行器，这正是后续票能直接复用
它、而不必再抄一遍顺序的原因（``ui/wizard_base`` 就是今天那个执行器适配层）。
"""
from __future__ import annotations

from dataclasses import dataclass

from .user_revisions import LAYER_FRAME, LAYER_SPECS

# ---------------------------------------------------------------------------
# 会话状态（D2 spec §4.1）
# ---------------------------------------------------------------------------
# 与 ``session_store.STATUS_*`` **不是一套**：那边是向导今天落盘的状态名
# （``awaiting_frames`` / ``stage1_running`` / …），这里是 spec 的会话状态词汇。
# 两者的映射只住在适配层（``ui/wizard_base``），步骤机不认识落盘格式。
STATUS_CREATED = "created"
STATUS_RUNNING = "running"
STATUS_AWAITING_HUMAN = "awaiting_human"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"
STATUS_INTERRUPTED = "interrupted"

STATUSES = (
    STATUS_CREATED,
    STATUS_RUNNING,
    STATUS_AWAITING_HUMAN,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_CANCELLED,
    STATUS_INTERRUPTED,
)

# 只有「已交付」是终态：失败/取消/中断都能再推进一次（spec §4.1 的「可 advance」列）。
TERMINAL_STATUSES = (STATUS_COMPLETED,)

# 可以起推进的状态。``running``（有 job 在跑）与 ``awaiting_human``（须先 resolve）
# 不在其中——这是 spec 里那条 409 的出处。
ADVANCEABLE_STATUSES = (
    STATUS_CREATED,
    STATUS_FAILED,
    STATUS_CANCELLED,
    STATUS_INTERRUPTED,
)

# ---------------------------------------------------------------------------
# 人工待办与步骤（spec §4.3）
# ---------------------------------------------------------------------------
ACTION_STAGE1_REVIEW = "stage1_review"
# 第二个人工待办（D4 spec §9）：帧链会话通过审查后先收关键帧，再进阶段 2。
# 它不是新事件类型——仍走既有的 ``action_item.created``（十种闭合不变）。
ACTION_FRAME_SUBMISSION = "frame_submission"

STEP_STAGE1 = "stage1"
STEP_STAGE2 = "stage2"

# 需要人工提交关键帧的变体。只有帧链（FL2VA）走第二道待办；T2VA 通过即进阶段 2，
# I2VA/L2VA 是降级产物、不是创建时可选的变体。集中在这里，落盘与判定不各写一份。
FRAME_CHAIN_VARIANTS = frozenset({"FL2VA"})

# 触发词：合法转移表的键。裸字符串会被打错字且无处对账，按本仓 ``STATUS_*`` 的口径走常量。
TRIGGER_ADVANCE = "advance"
TRIGGER_STAGE1_DONE = "stage1_done"
TRIGGER_STAGE2_DONE = "stage2_done"
TRIGGER_RESOLVE_APPROVE = "resolve_approve"
TRIGGER_RESOLVE_REVISE = "resolve_revise"
TRIGGER_FAIL = "fail"
TRIGGER_CANCEL = "cancel"
TRIGGER_INTERRUPT = "interrupt"
TRIGGER_RETRY = "retry"

TRIGGERS = (
    TRIGGER_ADVANCE,
    TRIGGER_STAGE1_DONE,
    TRIGGER_STAGE2_DONE,
    TRIGGER_RESOLVE_APPROVE,
    TRIGGER_RESOLVE_REVISE,
    TRIGGER_FAIL,
    TRIGGER_CANCEL,
    TRIGGER_INTERRUPT,
    TRIGGER_RETRY,
)

# 状态/闸门的**单一声明**：``(状态, 触发) → 状态``。没写的组合就是非法转移。
LEGAL_TRANSITIONS: dict[tuple[str, str], str] = {
    (STATUS_CREATED, TRIGGER_ADVANCE): STATUS_RUNNING,
    (STATUS_CREATED, TRIGGER_FAIL): STATUS_FAILED,
    (STATUS_CREATED, TRIGGER_CANCEL): STATUS_CANCELLED,
    (STATUS_RUNNING, TRIGGER_STAGE1_DONE): STATUS_AWAITING_HUMAN,
    (STATUS_RUNNING, TRIGGER_STAGE2_DONE): STATUS_COMPLETED,
    (STATUS_RUNNING, TRIGGER_FAIL): STATUS_FAILED,
    (STATUS_RUNNING, TRIGGER_CANCEL): STATUS_CANCELLED,
    (STATUS_RUNNING, TRIGGER_INTERRUPT): STATUS_INTERRUPTED,
    (STATUS_AWAITING_HUMAN, TRIGGER_RESOLVE_APPROVE): STATUS_RUNNING,
    (STATUS_AWAITING_HUMAN, TRIGGER_RESOLVE_REVISE): STATUS_RUNNING,
    (STATUS_FAILED, TRIGGER_RETRY): STATUS_RUNNING,
    (STATUS_CANCELLED, TRIGGER_RETRY): STATUS_RUNNING,
    (STATUS_INTERRUPTED, TRIGGER_RETRY): STATUS_RUNNING,
}

# 阶段 1 链上的首帧生图节点：画面级修订只重跑它（设定级从 ``LAYER_SPECS`` 的起点截断）。
# 写入这里而不是散在调用方，是因为它同时出现在「修订重跑起点」与「续跑落点」两处语义里。
FRAME_PROMPT_NODE = "fl2va_frame_prompts"

# 人工决定（spec §7 的 resolve 载荷）。
DECISION_APPROVE = "approve"
DECISION_REVISE = "revise"
# 帧提交不是接口上的 ``decision`` 取值（请求体里靠 ``frames`` 分型），但决议事件与
# 待办回显需要一个词说明「这条待办是被交帧决议掉的」——落成同一套内部词汇。
DECISION_SUBMIT_FRAMES = "submit_frames"


class InvalidTransition(ValueError):
    """非法转移：状态 / 触发组合不在声明表里。"""


@dataclass(frozen=True)
class ResumeAction:
    """阶段 1 从哪一格续跑。

    ``reason`` 用常量而不是布尔：调用方要为不同原因说不同的话（「没有断点」与
    「断点不在链上」对用户是两件事），布尔会把它们压成一句。
    """

    reason: str
    node: str = ""


RESUME_NO_CHECKPOINT = "no_checkpoint"
RESUME_UNKNOWN_NODE = "unknown_node"
RESUME_EXHAUSTED = "exhausted"
RESUME_FROM_NODE = "from_node"


def stage1_resume(chain: tuple[str, ...] | list[str], last_completed_node: str) -> ResumeAction:
    """阶段 1 的续跑起点判定（唯一一份）。

    输入是**事实**（链与最后完成节点），不是会话对象：向导、执行器、测试都拿同一份
    判定，才不会各算一次、慢慢算出不同的落点。
    """
    if not last_completed_node:
        return ResumeAction(RESUME_NO_CHECKPOINT)
    if last_completed_node not in chain:
        return ResumeAction(RESUME_UNKNOWN_NODE)
    next_index = chain.index(last_completed_node) + 1
    if next_index >= len(chain):
        # 链上已无下一节点：只差收尾（常见于最后一个节点之后还有常识质检循环）。
        return ResumeAction(RESUME_EXHAUSTED)
    return ResumeAction(RESUME_FROM_NODE, chain[next_index])


@dataclass(frozen=True)
class Facts:
    """步骤机要的全部输入事实。没有会话对象、没有路径、没有文件。"""

    status: str
    last_completed_node: str = ""
    gate_resolved: bool = False
    stage1_chain: tuple[str, ...] = ()


@dataclass(frozen=True)
class Action:
    """「下一步」：执行某个步骤，或停在某个人工闸门，或已到终态。"""

    status: str
    step: str | None = None
    gate: str | None = None
    terminal: bool = False
    resume: ResumeAction | None = None


def can_advance(status: str) -> bool:
    return status in ADVANCEABLE_STATUSES


def needs_frame_submission(variant: str) -> bool:
    """这个变体通过审查后是否要先收关键帧（D4 spec §9 的分叉判据）。

    未知变体按非帧链处理：D4 的创建表单只给 T2VA/FL2VA 两档，把拼写错误默默当成
    帧链会让一个笔误多出一道待办。
    """
    return str(variant).upper() in FRAME_CHAIN_VARIANTS


def next_status(status: str, trigger: str) -> str:
    """按声明表取下一个状态；组合不在表里就报错，绝不默认放行。"""
    try:
        return LEGAL_TRANSITIONS[(status, trigger)]
    except KeyError:
        raise InvalidTransition(f"非法转移：{status!r} --{trigger}--> ?") from None


def next_action(facts: Facts) -> Action:
    """回答「下一步该干什么」。

    ``running`` 不在处理范围内：那个状态的含义是「有 job 正持有会话」，调用方不该在
    那时来问（spec §4.1 的 409）。其余状态各有一条明确的去路。
    """
    status = facts.status
    if status == STATUS_COMPLETED:
        return Action(status=status, terminal=True)
    if status == STATUS_CREATED:
        return Action(status=status, step=STEP_STAGE1,
                      resume=ResumeAction(RESUME_NO_CHECKPOINT))
    if status == STATUS_AWAITING_HUMAN:
        if not facts.gate_resolved:
            return Action(status=status, gate=ACTION_STAGE1_REVIEW)
        return Action(status=status, step=STEP_STAGE2)
    if status in (STATUS_INTERRUPTED, STATUS_FAILED, STATUS_CANCELLED):
        return Action(status=status, step=STEP_STAGE1,
                      resume=stage1_resume(facts.stage1_chain, facts.last_completed_node))
    raise ValueError(f"步骤机不接受的状态：{status!r}")


@dataclass(frozen=True)
class Decision:
    """resolve 之后要执行的动作。

    ``revise`` 不是「回到阶段 1 重来」：起点由用户声明的层次决定（画面级只重出首帧
    节点，设定级从该层起点截断到链尾），跑完再回到**同一个人工闸门**——所以
    ``gate`` 在这里显式写出来，而不是让调用方自己记得再停一次。
    """

    status: str
    step: str
    rerun_from: str = ""
    gate: str = ""
    layer: str = ""


def revision_rerun_from(layer: str) -> str:
    """用户声明的层次 → 阶段 1 的重跑起点。

    起点只有一张表（``user_revisions.LAYER_SPECS``）：这里不另写一份节点名，否则
    链一变就有两处不同步，且不会报错。
    """
    if not layer or layer == LAYER_FRAME:
        return FRAME_PROMPT_NODE
    spec = LAYER_SPECS.get(layer)
    if spec is None:
        raise ValueError(f"不是可重跑的修订层次：{layer!r}")
    return spec.resume_from


def resolve(status: str, decision: str, *, layer: str = "") -> Decision:
    """人工闸门上的两个决定：通过 → 阶段 2；修订 → 从声明层次重跑阶段 1 再回闸门。"""
    if decision == DECISION_APPROVE:
        target = next_status(status, TRIGGER_RESOLVE_APPROVE)
        return Decision(status=target, step=STEP_STAGE2)
    if decision == DECISION_REVISE:
        target = next_status(status, TRIGGER_RESOLVE_REVISE)
        return Decision(status=target, step=STEP_STAGE1,
                        rerun_from=revision_rerun_from(layer),
                        gate=ACTION_STAGE1_REVIEW, layer=layer)
    raise ValueError(f"未知的人工决定：{decision!r}")


__all__ = [
    "ACTION_FRAME_SUBMISSION",
    "ACTION_STAGE1_REVIEW",
    "ADVANCEABLE_STATUSES",
    "Action",
    "DECISION_APPROVE",
    "DECISION_REVISE",
    "DECISION_SUBMIT_FRAMES",
    "Decision",
    "FRAME_CHAIN_VARIANTS",
    "Facts",
    "FRAME_PROMPT_NODE",
    "InvalidTransition",
    "LEGAL_TRANSITIONS",
    "RESUME_EXHAUSTED",
    "RESUME_FROM_NODE",
    "RESUME_NO_CHECKPOINT",
    "RESUME_UNKNOWN_NODE",
    "ResumeAction",
    "STATUSES",
    "STATUS_AWAITING_HUMAN",
    "STATUS_CANCELLED",
    "STATUS_COMPLETED",
    "STATUS_CREATED",
    "STATUS_FAILED",
    "STATUS_INTERRUPTED",
    "STATUS_RUNNING",
    "STEP_STAGE1",
    "STEP_STAGE2",
    "TERMINAL_STATUSES",
    "TRIGGER_ADVANCE",
    "TRIGGER_CANCEL",
    "TRIGGER_FAIL",
    "TRIGGER_INTERRUPT",
    "TRIGGER_RESOLVE_APPROVE",
    "TRIGGER_RESOLVE_REVISE",
    "TRIGGER_RETRY",
    "TRIGGER_STAGE1_DONE",
    "TRIGGER_STAGE2_DONE",
    "TRIGGERS",
    "can_advance",
    "needs_frame_submission",
    "next_action",
    "next_status",
    "resolve",
    "revision_rerun_from",
    "stage1_resume",
]

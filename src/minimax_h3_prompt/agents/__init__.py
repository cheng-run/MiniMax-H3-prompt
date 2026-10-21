"""角色 agent 装配：每个角色 = langchain.agents.create_agent（模型 + 角色 system prompt + 工具）。

**为什么用 create_agent 而不是 deepagents.create_deep_agent**：
deepagents 的 base stack 无条件注入文件系统/execute/task 等内建工具（`ls`、`write_file`、
`execute` 等）。对本项目"单次产出文本"的角色这是负资产——模型会去调 `write_file`/`ls`
而不是直接返回文本，导致空产出。create_agent 是同一 LangChain 栈、无默认工具、轻量；
deepagents 的"有界圆桌/讨论"思想已用 LangGraph 子图实现（graph/roundtable.py）。
"""
from __future__ import annotations

import time
from pathlib import Path
from urllib.parse import urlparse

from langchain.agents import create_agent
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.language_models import BaseChatModel
from langchain_core.tools import tool

from .. import film_language, model_factory
from ..brief_parser import FRAME_VARIANTS, RefItem
from ..config import config
from ..observability import record_agent_call, reporter
from ..tools.h3_validator import (
    ALIGN_TEMPLATES,
    EDGE_STABILITY_SENTENCE,
    format_issues,
    validate_prompt,
)
from ..tools.ref_metadata import format_ref_meta, to_tuple

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"

# 要逐字符照抄的官方原文，按角色声明；由加载处注入该角色提示词文末（issue #35）。
#
# 为什么注入而不写进 .md：官方原文在**代码里**的唯一可写来源是 ``tools/h3_validator`` 的两个
# 常量（校验侧与两条写段模板都从那里取），角色 .md 里再抄一份就是又一处可写来源——改常量时
# 它不会跟着动，而返回值里两份都在场，谁也看不出哪份是旧的（本仓吃过「同一句原文 3 份硬拷贝」
# 的亏）。指针留在 .md 里，原文由这里送过去。
#
# 这是**搬位置，不是改内容**：改前那份 .md 的「I2VA / FL2VA / L2VA 首行」三条与「edge-stability
# 原句」那一条逐字写着同样的四段原文，注入块里的字与它们逐字节相同（块本身不改写常量，
# 见 ``_official_verbatim_block``；对照见 output/issue35-evidence/two_arm_check.log）。
#
# 白名单是显式的，与电影语言词表同一纪律：**不声明即拿不到**。除整片提示词工程师外没有
# 别的角色需要它——写段那两条路径是代码拼的请求（``segment_prompts`` 直接插值常量），
# 审查/质检角色是拿校验器工具去判，不是自己逐字符写。
OFFICIAL_VERBATIM_ROLES = frozenset({"prompt_engineer"})

# 注入块的标题，也是角色 .md 里指针句认的字。**只有这一处定义**：块标题、指针句、测试三处
# 各写一遍就是又一个会各自漂移的字面量——本票正是来消这个的。测试会逐字比对 .md 里的指针，
# 所以改标题时漏改某份 .md 会报红，改 .md 而不改这里也会。
OFFICIAL_VERBATIM_MARKER = "【官方逐字符原文】"

# 15 个角色，覆盖电影制作 + AIGC 双流程
ROLE_KEYS = [
    "producer",
    "director",
    "screenwriter",
    "art_director",
    "character_designer",
    "background_designer",
    "prop_designer",
    "image_prompt_engineer",
    "frame_prompt_engineer",
    "storyboard",
    "cinematographer",
    "sound_designer",
    "composer",
    "reference_consistency",
    "feasibility_reviewer",
    "prompt_engineer",
    "qa",
]

# 需要 h3_validator 校验工具的角色（审查/组装/质检）
TOOL_ROLES = {"feasibility_reviewer", "prompt_engineer", "qa"}
# 需要参考资产工具的角色（ref 模式下）
REF_TOOL_ROLES = {
    "producer",
    "art_director",
    "character_designer",
    "background_designer",
    "prop_designer",
    "reference_consistency",
    "feasibility_reviewer",
    "prompt_engineer",
    "qa",
}


def _role_wordlist(role: str) -> str:
    """该角色文末要附的词表切片；不在任何白名单里返回空串。

    **两份真源、两个读取口**（#31 视频侧 / #33 生图侧）：视频半管 H3 正文的选词与写法，
    生图半管静态画面。一个角色只属于其中一侧——同一个键同时出现在两张白名单里时，
    这里按「先视频后生图」取第一份非空的，视频侧会**静默**赢，生图半那份永远送不出去。
    两张白名单因此不许相交，由 ``tests/test_film_language.py`` 钉住。
    """
    return film_language.video_slice(role) or film_language.image_slice(role)


def _official_verbatim_block(role: str) -> str:
    """该角色文末要附的「官方逐字符原文」块；不在 ``OFFICIAL_VERBATIM_ROLES`` 里返回空串。

    文本逐字取自 ``h3_validator`` 的常量——本函数**只做搬运，不改写**（与电影语言切片
    同一纪律），所以块里的每个字都能回常量比对。
    """
    if role not in OFFICIAL_VERBATIM_ROLES:
        return ""
    align_lines = "\n".join(
        f"- {variant}：`{line}`" for variant, line in ALIGN_TEMPLATES.items()
    )
    return (
        f"{OFFICIAL_VERBATIM_MARKER}（产出里必须逐字符出现，不得改写、翻译或换行）\n"
        "- 帧变体首行对齐指令，按当前变体取对应那一行"
        "（`N`=最终镜头号，`S.SS`=时长两位小数）：\n"
        f"{align_lines}\n"
        "- 每个镜头块的**最后一句**（多镜段里每块各加一次）：\n"
        f"`{EDGE_STABILITY_SENTENCE}`"
    )


def load_role_prompt(role: str) -> str:
    """读角色提示词；命中的角色在文末附上电影语言词表切片（issue #31 / #33）与官方原文块。

    两种附加都发生在**加载处**而不是各份 .md 里：它们都是逐字读入的纯文本，抄进角色 .md
    就等于同一份内容有 N 份硬拷贝（本仓吃过「同一句官方原文 3 份硬拷贝」的亏）。
    哪些角色拿哪几节词表由 ``film_language.VIDEO_SLICES`` / ``IMAGE_SLICES`` 声明，
    不声明的角色拿到空串——分段规划师尤其不能拿到（段尾钩子要留作锚帧语义校验的
    可判定对照物），关键帧生图提示词工程师只能拿生图半（静态图不写运镜）。
    哪些角色拿官方原文块由 ``OFFICIAL_VERBATIM_ROLES`` 声明（issue #35）。
    """
    prompt = (PROMPTS_DIR / f"{role}.md").read_text(encoding="utf-8")
    wordlist = _role_wordlist(role)
    # 原文块排在最后：角色 .md 里的指针写的是「见文末【官方逐字符原文】」
    verbatim = _official_verbatim_block(role)
    # 空块不加空行：不声明词表/原文的角色，提示词逐字与改动前一致
    return "\n".join(part for part in (prompt, wordlist, verbatim) if part)


def _make_tools(refs: list[RefItem], mode: str, duration: float, variant: str, role: str) -> list:
    tools: list = []
    ref_meta = to_tuple(refs)

    if role in REF_TOOL_ROLES and (mode == "ref" or variant in FRAME_VARIANTS) and refs:

        @tool
        def list_reference_assets() -> str:
            """列出当前任务的参考资产（<Picture N> 映射）。ref/帧变体模式使用。"""
            return format_ref_meta(refs)

        tools.append(list_reference_assets)

    if role in TOOL_ROLES:
        run_mode = mode

        @tool
        def validate_h3_prompt(prompt: str, mode: str = "") -> str:
            """校验一段 H3 提示词是否符合官方格式，返回问题清单。mode 为 'ref' 或 'base'（留空取当前任务模式）。"""
            issues = validate_prompt(
                prompt, mode=mode or run_mode, duration=duration, variant=variant, ref_meta=ref_meta
            )
            return format_issues(issues)

        tools.append(validate_h3_prompt)

    return tools


def build_role_agents(
    model: BaseChatModel,
    refs: list[RefItem],
    mode: str,
    duration: float,
    variant: str,
) -> dict[str, object]:
    """构建 15 个角色 agent。返回 {role_key: agent}。"""
    agents: dict[str, object] = {}
    for role in ROLE_KEYS:
        tools = _make_tools(refs, mode, duration, variant, role)
        agents[role] = create_agent(
            model=model,
            system_prompt=load_role_prompt(role),
            tools=tools or None,
            name=f"role_{role}",
        )
    return agents


def _content_to_text(content: object) -> str:
    """把模型返回的 content（str 或 blocks 列表，如 DeepSeek 带 thinking）提取成纯文本。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    parts.append(block.get("text", ""))
                elif isinstance(block.get("text"), str):
                    parts.append(block["text"])
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(p for p in parts if p)
    return str(content)


# 「还在等」事件的节流间隔。它**不是超时**（别并进 model_factory 的超时常量区）：
# 流式下一个大请求能吐上千块，逐块上报会把事件总线和终端刷爆；而用户需要的只是
# 「它还活着」的节奏感。10 秒一次与首字节线的量级一致（issue #59 实测健康块间
# gap 最大 0.62 秒，所以 10 秒没消息本身就是强信号）。#62 按这个节奏渲染。
_WAIT_PROGRESS_INTERVAL_SECONDS = 10.0


class FirstByteHandler(BaseCallbackHandler):
    """记录「首字节到了」并按节流上报生成进度（issue #61）。

    为什么靠回调、不看返回值：流式下每个 token 到达就是「服务端在干活」的唯一
    活体信号，而 invoke 的返回值要等整段结束才有。也不重构既有的「取最后一个
    非空 AI 消息」逻辑——那段约定有大量既有用例依赖（spec #59 决定 6）。

    事件契约（#62 照着渲染，字段定死）：
    - first_byte: {type, role, state, elapsed}
    - generating: {type, role, state, elapsed, chunks, bytes}
      ``chunks`` 是块数；``bytes`` 是这些块的 **UTF-8 字节数**，不是字符数
      （复审实测 ``"你好世界"`` 4 个字符 = 12 字节）。渲染层按 kB 展示，单位必须
      真是字节，否则进度里的「kB」是假的。
    - stuck / retrying 由外层 ``run_agent`` 发（判定与动作都在那里）：
      stuck 带 ``elapsed`` 与刚失败的 ``attempt``；retrying 带 ``attempt``（即将
      发起的尝试序号）、``retry``（重试序号，1 起）与 ``remaining_budget``。
    ``elapsed`` 一律是「从本次调用开始到此刻的秒数」。
    """

    def __init__(self, role: str) -> None:
        self.role = role
        self._started_at = time.monotonic()
        # 读模块常量而不是写成默认参数：默认参数在 import 时就固定了，
        # 测试 monkeypatch 常量会失效（守卫会变成永不为红）。原先还有个
        # ``interval`` 形参，全仓无调用方——留着它等于给测试开一条绕过常量
        # 的私路，删掉。
        self._interval = _WAIT_PROGRESS_INTERVAL_SECONDS
        self._last_progress_at = self._started_at
        self.first_byte_at: float | None = None
        self.chunks = 0
        # 累计的是 UTF-8 字节数：流式块是 str，``len`` 只数码点，中文一块 3 字节
        # 会算成 1（实测「你好世界」4 字符 = 12 字节）。字段名与渲染层的 kB
        # 都按字节说话，口径必须一致。
        self.bytes = 0

    def seconds_since_start(self) -> float:
        return time.monotonic() - self._started_at

    def seconds_since_first_byte(self) -> float:
        if self.first_byte_at is None:
            return 0.0
        return time.monotonic() - self.first_byte_at

    def on_llm_new_token(self, token: str, **kwargs) -> None:
        now = time.monotonic()
        if self.first_byte_at is None:
            self.first_byte_at = now
            reporter.emit({
                "type": "agent_wait", "role": self.role, "state": "first_byte",
                "elapsed": now - self._started_at,
            })
        self.chunks += 1
        # UTF-8 字节数，不是 ``len(token)``（那数码点：中文一字 1、字节却是 3）。
        # 空 token 也要能过，所以先 ``or ""`` 再编码。
        self.bytes += len((token or "").encode("utf-8"))
        if now - self._last_progress_at >= self._interval:
            self._last_progress_at = now
            reporter.emit({
                "type": "agent_wait", "role": self.role, "state": "generating",
                "elapsed": now - self._started_at,
                "chunks": self.chunks, "bytes": self.bytes,
            })


def _is_timeout_error(exc: BaseException) -> bool:
    """异常是不是「超时」——httpx 与 openai 内嵌的 httpx2 两代都要咬得住。

    **不能** ``isinstance(exc, httpx.ReadTimeout)``：openai 自带的 http 栈是
    vendored 的 ``httpx2``。实测（openai 3.0.0 + langchain-openai 1.5.0）：
    - 首字节前卡住 → ``openai.APITimeoutError``；
    - 流到一半卡住 → ``httpx2.ReadTimeout``，它对 ``httpx.ReadTimeout``、
      内建 ``TimeoutError``、``APITimeoutError`` **全是 False**；
    - 两代栈唯一的交集是 MRO 里名为 ``TimeoutException`` 的基类，故按类名判。
      这样将来 openai 切回真 httpx 也不会静默漏判（漏判＝卡住不再止损）。
    """
    try:
        from openai import APITimeoutError
    except ImportError:  # pragma: no cover - openai 是硬依赖，留作防御
        APITimeoutError = None  # type: ignore[assignment]
    if APITimeoutError is not None and isinstance(exc, APITimeoutError):
        return True
    return any(cls.__name__ == "TimeoutException" for cls in type(exc).__mro__)


def _gateway_name() -> str:
    """报错文案里的网关名：优先 base_url 的 host，取不到退回 provider。

    为什么从配置派生：原文案写死 ``"DashScope"``，用户换过网关后它仍把排查方向
    指向 DashScope（spec #59 user story 15）。host 比 provider 更能指认实际节点
    （换端点正是本票的重试机制），所以 host 优先。
    """
    settings = config.primary_model
    if settings.base_url:
        host = urlparse(settings.base_url).hostname
        if host:
            return host
    return settings.provider or "未知网关"


def _call_failure(
    role: str,
    *,
    phase: str,
    elapsed: float,
    attempts: int,
    remaining: float,
) -> RuntimeError:
    """构造「这次调用为什么失败、下一步怎么办」的中文错误——**唯一一份**文案。

    两个分支共用同一段结尾：同一句话只写一处，否则改一处漏一处（本仓「同一契约
    不许两处口径」的老毛病）。网关名与所有数字都从配置/常量派生（issue #61）：
    常量一改文案跟着改，测试以 monkeypatch 常量来验这条接线。
    """
    gateway = _gateway_name()
    read_timeout = model_factory._LLM_READ_TIMEOUT_SECONDS
    budget = model_factory._LLM_CALL_BUDGET_SECONDS
    max_attempts = model_factory._LLM_MAX_ATTEMPTS
    if phase == "midstream":
        head = (
            f"{role} 调用失败：网关 {gateway} 已返回首字节、流了 {elapsed:.1f} 秒后断流。"
            f"\n首字节之后断流不自动重试——已经流了一半，重来一遍是纯浪费（已试 {attempts} 次）。"
        )
    else:
        # 两个「卡住」分支的前两句逐字相同，只写这一处再拼各自的尾句：复审抓到
        # 它们被复制成两份，改一处漏一处就是又一份会各自漂移的文案。拼出来与
        # 拆前逐字节一致（``stuck_head`` 以句号结尾，尾句自带 ``\n``）。
        stuck_head = (
            f"{role} 调用失败：网关 {gateway} 连续 {attempts} 次在 {read_timeout:.0f} 秒内"
            f"没有返回首字节（判定卡住），最后一次等了 {elapsed:.1f} 秒。"
        )
        if attempts >= max_attempts:
            head = stuck_head + f"\n已到尝试次数上限（{max_attempts} 次），不再重试。"
        else:
            head = (
                stuck_head
                + f"\n本次调用的 {budget:.0f} 秒预算只剩 {max(remaining, 0.0):.0f} 秒，"
                f"不够再等一次首字节线，不再重试。"
            )
    return RuntimeError(
        head
        + "\n这通常是网关节点卡住或请求被挂起，不是本地网络问题。"
        + "\n下一步：稍后重试；若反复卡在同一节点，换一个 base_url 节点或检查网关状态。"
    )


def run_agent(agent: object, message: str, retry_empty: bool = True) -> str:
    """invoke 一个角色 agent，返回最终回答的纯文本。

    判据分派（issue #61，三种病走三条路）：
    - **首字节前**超时 → 判**卡住** → 可重试。重试会新建连接、触发 DNS 重解析，
      实测换到过另一个网关节点（``101.132.45.96`` → ``139.196.165.158``），
      被卡住的那次重试确实救回来过，所以别照搬「卡住不重试」；
    - **首字节后**超时 → 不重试，直接报错（已流了一半，重来是纯浪费）；
    - **空产出** → 走既有的 ``retry_empty`` 递归重试，判据独立（它是内容病），
      但并入同一份预算；
    - 其他异常 → 原样上抛（``raise``，不改类型）。

    ``agent_done.duration`` 报「从本函数开始到出结果」的总耗时（含全部超时重试），
    不是最后一次尝试的耗时——US5 问的是「这次调用总共花了多久」。

    预算与次数的分工：``_LLM_CALL_BUDGET_SECONDS`` 是墙钟硬顶（含全部重试），
    ``_LLM_MAX_ATTEMPTS`` 是次数硬顶。重试还要求**剩余预算撑得起一次首字节线**
    ——连 30 秒都等不起的剩余预算没有意义。并行节点里每个角色各调一次本函数，
    各算各的 deadline，天然互不牵连（不引入模块级共享状态）。
    """
    role = getattr(agent, "name", "agent")
    # 「开始」每**次产出**只发一次：挪到重试循环之外（issue #62 裁定 1）。发在
    # ``_run_agent_inner`` 里时，每次超时重试都会再打一行「正在处理……」，既看不出
    # 这是第几次（重试已有专属的 retrying 事件），又刷屏；超时重试路径上 agent_done
    # 本来只在成功时发一次，一举一收才对称。空产出重试同样不再重发——那是同一次产出
    # 内部的补一次（注意：那条路径上 agent_done 会发两次，属既有行为，#62 未动）。
    reporter.emit({"type": "agent_start", "role": role})
    started = time.monotonic()
    deadline = started + model_factory._LLM_CALL_BUDGET_SECONDS
    timeout_attempt = 0
    # 每次真正 invoke 都把 handler 放进来，外层靠 handlers[-1] 判断「首字节到没到」。
    # 空产出重试会递归再 invoke 一次，所以它必须是列表而不是单个对象。
    handlers: list[FirstByteHandler] = []
    while True:
        timeout_attempt += 1
        handlers.clear()
        try:
            return _run_agent_inner(
                agent, message, retry_empty=retry_empty,
                role=role, deadline=deadline, handlers=handlers,
                started=started,
            )
        except Exception as exc:
            if not _is_timeout_error(exc):
                raise
            handler = handlers[-1] if handlers else None
            if handler is not None and handler.first_byte_at is not None:
                # 首字节到了才算「流了一半」；空产出重试里断掉的那次同样按此判。
                raise _call_failure(
                    role,
                    phase="midstream",
                    elapsed=handler.seconds_since_first_byte(),
                    attempts=timeout_attempt,
                    remaining=deadline - time.monotonic(),
                ) from exc
            elapsed = handler.seconds_since_start() if handler else time.monotonic() - started
            reporter.emit({
                "type": "agent_wait", "role": role, "state": "stuck",
                "elapsed": elapsed, "attempt": timeout_attempt,
            })
            remaining = deadline - time.monotonic()
            # attempt 事件语义：stuck.attempt = 刚失败的尝试序号；
            # retrying.attempt = 即将发起的尝试序号（后者＝已试次数 + 1）。
            # retry 是**重试序号**（1 表示第一次重试）＝ attempt - 1，由这里派生：
            # 渲染层再自己减一次就会与 attempt 错位（issue #62 裁定 2）。
            if (
                timeout_attempt < model_factory._LLM_MAX_ATTEMPTS
                and remaining >= model_factory._LLM_READ_TIMEOUT_SECONDS
            ):
                reporter.emit({
                    "type": "agent_wait", "role": role, "state": "retrying",
                    "attempt": timeout_attempt + 1,
                    "retry": timeout_attempt,
                    "remaining_budget": max(0.0, remaining),
                })
                continue
            raise _call_failure(
                role,
                phase="stuck",
                elapsed=elapsed,
                attempts=timeout_attempt,
                remaining=remaining,
            ) from exc


def _run_agent_inner(
    agent: object,
    message: str,
    retry_empty: bool = True,
    *,
    role: str,
    deadline: float,
    handlers: list[FirstByteHandler],
    started: float,
) -> str:
    """一次 LLM 调用本体；run_agent 的超时与预算层包裹在它外面。

    取文本逻辑原封不动搬自改动前：优先取「最后一个有非空文本的 AI 消息」，
    空产出且 retry_empty=True 时追加一句提示重试一次。空产出重试**也吃同一份
    预算**：``deadline`` 到了就不再来一轮（否则内容病能把调用预算撑穿）。

    ``started`` 是 ``run_agent`` 的起点（monotonic），**不是**本次尝试的起点：
    US5 要的「这次调用总共花了多久」含全部超时重试，每次尝试各自计时会让重试
    过的调用只报最后一次的耗时（复审实测）。空产出重试递归时把它原样传下去，
    于是内层那次 done 报的也是「从 run_agent 开始到现在」。
    """
    handler = FirstByteHandler(role)
    handlers.append(handler)
    result = agent.invoke(
        {"messages": [{"role": "user", "content": message}]},
        config={"callbacks": [handler]},
    )
    msgs = result.get("messages", [])

    out = ""
    for m in reversed(msgs):
        if getattr(m, "type", "") == "ai":
            txt = _content_to_text(m.content)
            if txt.strip():
                out = txt
                break
    if not out:
        for m in reversed(msgs):  # 兜底：任意最后一条非空消息
            txt = _content_to_text(m.content)
            if txt.strip():
                out = txt
                break
    if not out and retry_empty and time.monotonic() < deadline:
        out = _run_agent_inner(
            agent,
            f"{message}\n\n（你上一条回复是空的，请直接给出内容，不要空谈。）",
            retry_empty=False,
            role=role,
            deadline=deadline,
            handlers=handlers,
            started=started,
        )
    # duration 是「从 run_agent 开始到此刻」的总耗时（含已发生的超时重试），
    # token 计量口径不动（仍按本次返回的消息取 usage）。
    record_agent_call(role, msgs, out, time.monotonic() - started)
    return out

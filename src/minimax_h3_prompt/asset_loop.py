"""素材循环的领域件（issue #50）：队列排序、改写轮次留痕、单条提示词改写。

素材循环把多参路线的资产图生产从「一次性全量出词」改成**逐张闭环**
（CONTEXT.md「素材循环」）：出一张提示词 → 审查（填路径前就能打回）→ 交回 →
读图校对 → 裁决 → 下一张。本模块只放不碰终端的纯件；问答节奏住在
``ui.wizard`` 的 ``_asset_loop``。

改写留痕走 ``<generation_dir>/asset-rounds.jsonl`` **追加式轮次记录**——沿用
修订台账（frame-rounds.jsonl）的追加式快照思路：只追加不改写，历史可查。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, NamedTuple, Sequence

# 追加式轮次记录文件名（issue #50 AC2/AC3「留痕」）。放生成目录下，与
# frame-rounds.jsonl 同址；两者并存——帧的修订台账管首尾帧提示词重出，
# 这本管资产图提示词的改写轮次，混一本会让「一轮」指代两种节奏。
ASSET_ROUNDS_FILENAME = "asset-rounds.jsonl"

# 队列进度在 state 里的键（会话持久化白名单同此名，见 session_store._STATE_KEYS）。
ASSET_LOOP_KEY = "asset_loop"

# 资产类型白名单（只做入参校验，**不再参与排序**）。issue #66 起队列顺序纯按
# ``<Picture N>`` 升序：编号即剧本出场序，也正是注入卡与工作流槽位的接线序；
# 原先的「人物→场景→道具」分组（issue #50 AC4，2026-10-18 裁定）让用户按菜单
# 顺序填图时填错槽，故该分组在 issue #66 被推翻。
_ASSET_KINDS = ("character", "scene", "prop")

# 留痕触发口（每行 round 记录的 trigger）：
# - 审查打回「改写提示词」（AC2）；
# - 校对后重生成「改词再掷」（AC3，词图皆错时连词一起换）；
# - 校对后重生成「原词再掷」（AC3 默认口——词没动也记一笔，因为裁决
#   记录会被换版后的重新校对**覆盖**，重掷历史只活在追加账里）；
# - 校对后重生成「AI 按偏差改词」（issue #72 / ADR 0018：由模型按偏差清单把被画漏的
#   细节写显式。与「改词再掷」分得清——那一档的意见是人写的，这一档是模型写的，
#   事后要能分辨是谁要求的这一轮）。
TRIGGER_REVIEW_REWORD = "review_reword"
TRIGGER_REGEN_REWORD = "regen_reword"
TRIGGER_REGEN_REROLL = "regen_reroll"
TRIGGER_REGEN_AI_REWORD = "regen_ai_reword"

# 「重画轮次」只认因偏差重画的这三档（issue #72）：审查打回改写不算——那一档图还没交回，
# 把它算进来会把「本张已重画 N 轮」说得比事实多。清单保持单一定义：计数口按它过滤，
# 将来加触发口只改这里（写死一遍 in ("a","b") 就是第二份口径）。
REGEN_TRIGGERS = (TRIGGER_REGEN_REROLL, TRIGGER_REGEN_REWORD, TRIGGER_REGEN_AI_REWORD)

# 打回「外部重掷」**不留痕**（issue #50 明确「不登记」）：它不是这本账的触发口，
# 故此处不设常量——写进轮次账就等于给被拒图记了名。


def loop_state(state: dict) -> dict:
    """队列进度的**唯一读口**：``{released: [...], abandoned: [...], current?: str}``。

    坏形状**响**（ValueError）——本仓口径：falsy 错形不许被 ``or {}`` / ``or []`` 洗成
    空表悄悄起新账（同 ``asset_proofread.proofread_rows`` 与画布字段）。返回的是 state 里
    **那个活的 dict**（调用方改它即落回 state），所以这里的错形检查是唯一闸门。
    """
    raw = state.setdefault(ASSET_LOOP_KEY, {})
    if not isinstance(raw, dict):
        raise ValueError(f"素材循环进度字段错形：{raw!r}（应为队列进度对象）")
    for key in ("released", "abandoned"):
        value = raw.get(key)
        if value is None:
            raw[key] = []
        elif not isinstance(value, list):
            raise ValueError(f"素材循环进度 {key} 错形：{value!r}（应为语义名列表）")
    current = raw.get("current")
    if current is not None and not isinstance(current, str):
        raise ValueError(f"素材循环进度 current 错形：{current!r}（应为语义名）")
    return raw


def note_name(state: dict, key: str, name: str) -> list[str]:
    """往 ``released`` / ``abandoned`` 里记一个语义名（幂等，保序）。返回该列表。"""
    if key not in ("released", "abandoned"):
        raise ValueError(f"素材循环进度没有 {key!r} 这一列")
    names = loop_state(state)[key]
    text = str(name)
    if text not in names:
        names.append(text)
    return names


def note_released(state: dict, name: str) -> list[str]:
    """记「这张已出过词」（出站节奏与落盘的依据）。"""
    return note_name(state, "released", name)


def note_abandoned(state: dict, name: str) -> list[str]:
    """记「这张被显式放弃」（续接据此不再排它）。"""
    return note_name(state, "abandoned", name)


def set_current(state: dict, name: str | None) -> None:
    """记/清「当前待办」——系统始终只维持一张，续接从它接上。"""
    loop = loop_state(state)
    if name is None:
        loop.pop("current", None)
    else:
        loop["current"] = str(name)


class QueueItem(NamedTuple):
    """队列里的一张待办资产：登记号 + 类型 + 语义名。

    提示词正文**不进队列项**：出词时刻要拿的是 state 里的最新版（改写后就变了），
    队列只管「出谁」；渲染时按语义名回 state 取，天然带上最新改写。
    """

    picture: int
    kind: str
    name: str


def asset_queue(entries: Sequence[dict[str, Any]], *, pictures: dict[str, int] | None = None) -> tuple[QueueItem, ...]:
    """把资产条目排成素材循环的待办队列（issue #66 起纯 ``<Picture N>`` 升序）。

    编号来自 ``pictures``（语义名 → 剧本出场序，经 ``ref_asset_registry`` 派生），
    升序排列后菜单顺序＝注入卡顺序＝工作流槽位顺序，用户按菜单往槽位里填图不会
    填错槽（issue #64 问题 2 的实测现场）。**不再有类型分组**：类型只用于展示标签。

    ``pictures`` 不给（或名字不在映射里）的条目排尾部、保持传入序——与参考素材
    登记「找不到出场的排尾」同一口径；跳号（1、5、9）不影响升序，被跳过的号
    仍然留在队列里（待办与否由 ``ui.wizard._asset_loop`` 按落账/放弃判）。

    入参是 ``{kind: [条目]}`` 或 ``(kind, 条目)`` 序列；条目取四键契约
    （name/positive_prompt/negative_prompt/locked_features，见
    ``generation._normalize_asset_entries``）。
    """
    items: list[tuple[int, int, QueueItem]] = []
    seq = 0

    def _add(kind: str, raw: dict[str, Any]) -> None:
        nonlocal seq
        if kind not in _ASSET_KINDS:
            raise ValueError(f"未知资产类型：{kind!r}")
        name = str(raw.get("name", "")).strip()
        if not name:
            raise ValueError("资产条目缺语义名 name")
        number = (pictures or {}).get(name, 10**6 + seq)
        items.append((number, seq, QueueItem(number, kind, name)))
        seq += 1

    if isinstance(entries, dict):
        for kind, rows in entries.items():
            for raw in rows or []:
                _add(str(kind), raw)
    else:
        for kind, raw in entries:
            _add(str(kind), raw)

    return tuple(item for *_keys, item in sorted(items, key=lambda t: t[:2]))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def log_asset_round(
    directory: str | Path,
    *,
    name: str,
    kind: str,
    trigger: str,
    feedback: str,
    prompt_before: str,
    prompt_after: str,
    round_index: int | None = None,
) -> Path:
    """追加一行改写轮次（``<generation_dir>/asset-rounds.jsonl``）。

    追加式快照：一行就是一轮的完整前后对照（``prompt_before``/``prompt_after``），
    只追加不改写——「某轮改了什么、谁要求的」事后逐行可查。``round_index``
    缺省按该语义名已有行数自动续号（跨调用不依赖内存计数）。
    """
    path = Path(directory) / ASSET_ROUNDS_FILENAME
    existing = read_asset_rounds(directory, name=name)
    record = {
        "round": int(round_index) if round_index is not None else len(existing) + 1,
        "name": name,
        "kind": kind,
        "trigger": trigger,
        "feedback": feedback,
        "prompt_before": prompt_before,
        "prompt_after": prompt_after,
        "at": _now(),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    return path


def read_asset_rounds(directory: str | Path, *, name: str | None = None) -> list[dict[str, Any]]:
    """读轮次账（可按语义名过滤）；文件不在＝空账，坏行**响**（ValueError）。

    坏行响不吞：追加账是「历史可查」的凭据，静默跳过坏行等于历史缺页无人知。
    """
    path = Path(directory) / ASSET_ROUNDS_FILENAME
    if not path.is_file():
        return []
    out: list[dict[str, Any]] = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"轮次账第 {lineno} 行不是合法 JSON：{path}") from exc
        if not isinstance(record, dict):
            raise ValueError(f"轮次账第 {lineno} 行不是对象：{path}")
        if name is None or record.get("name") == name:
            out.append(record)
    return out


def count_regen_rounds(directory: str | Path, *, name: str) -> int:
    """这张资产因偏差被重画了几轮——**从轮次账现算**，不另存计数器（issue #72）。

    不存计数器的理由：计数器是第二份真相，改写轮次账一被追加（续跑、CLI 补记），
    两边就各自漂；而「重画过几轮」只用来提示，现算一次的成本是一行文件读。
    只认 ``REGEN_TRIGGERS`` 那几档：审查打回改写不是重画（见该常量的注释）。
    """
    return sum(
        1 for record in read_asset_rounds(directory, name=name)
        if record.get("trigger") in REGEN_TRIGGERS
    )


def render_regen_hint(rounds: int) -> str:
    """重画轮次提示的**唯一写源**（issue #72 的菜单与校对报告两处都取它）。

    两处各拼一遍就会各自漂（同 ``format_tag`` 的纪律）。0 轮返回空串：第一次进重生成
    菜单时「已重画 0 轮」是噪音，人要看的是「你已经在这张图上花了 N 轮」。
    """
    count = int(rounds)
    if count <= 0:
        return ""
    return f"本张已重画 {count} 轮；仍不过可按图改文案保留这一版"


# 两条单资产请求（改写 / 回灌）共用的块标题。**只有这一处定义**：模板、假模型与断言
# 三处各写一遍就是又一个会各自漂移的字面量（同 agents.OFFICIAL_VERBATIM_MARKER 的纪律，
# ADR 0008 裁定二）。测试从 ``asset_loop`` 导入它，不另抄一份。
CURRENT_PROMPT_HEAD = "【当前提示词】"


# 直调路径的角色名（issue #71 / ADR 0019）：素材改写与回灌都是直调，事件与
# token 记账都按这两个名字出现在终端与成本表里——各写一份字面量就会漂。
ASSET_REWRITE_ROLE = "role_asset_rewrite"
ASSET_ALIGN_ROLE = "role_asset_align"


def _call_direct(llm, request: str, *, role: str) -> str:
    """一次直调走统一看门狗（ADR 0019）：失败带病因抛出，不做静默降级。

    为什么这两个纯件也要接看门狗：它们原先直接 ``llm.invoke``，首字节/卡住/重试
    全是空白——素材循环里用户看到的就是「打完一行字之后干等」。判据与角色调用
    同源（``agents.run_direct_call``），这里只负责把失败翻成异常让调用方保持原词。
    """
    from .agents import run_direct_call  # 延迟导入：避免入口期拉起角色装配栈

    result = run_direct_call(llm, request, role=role)
    if not result.ok:
        # 保持两个调用方原有的异常契约（元组/字符串接口都按「调用失败」处理），
        # 只把病因换成统一入口给的那一段人话。
        raise ValueError(result.error)
    return result.text


def rewrite_asset_prompt(old_prompt: str, feedback: str, llm) -> str:
    """按用户意见改写一条资产生图提示词（一轮修订的产物）。

    ``llm`` 由调用方注入（带 ``invoke(str)`` 接口，同 ``rewrite_segment_prompt``
    的口径），测试可传 stub。只动被点名的问题——提示词是逐轮小步改的，
    全篇重写会把没提到的细节一并漂走（同修订基线「未提到的部分原样保留」的用意）。
    """
    request = (
        "请按用户意见改写下面这条 Z-Image 生图提示词。\n"
        "纪律：只落实意见点名的问题；没提到的部分逐字保留；"
        "仍然输出一条可直接复制进生图工具的提示词正文，不要解释、不要前后缀。\n\n"
        f"{CURRENT_PROMPT_HEAD}\n{old_prompt.strip()}\n\n"
        f"【改写意见】\n{feedback.strip()}"
    )
    new_prompt = _call_direct(llm, request, role=ASSET_REWRITE_ROLE).strip()
    if not new_prompt:
        raise ValueError("改写结果为空")
    return new_prompt


# 回灌请求里那块「已定稿素材画面事实」的标题。**只有这一处定义**：请求文本、假模型
# 与断言三处各写一遍就是又一个会各自漂移的字面量（同 agents.OFFICIAL_VERBATIM_MARKER
# 的纪律）。标题改这里，测试里认这个字面的地方一并跟着改。
SETTLED_FACTS_HEAD = "【已定稿素材画面事实】"


def build_settled_align_request(prompt: str, settled: Sequence[tuple[str, str]]) -> str:
    """回灌请求文本（纯函数，返回字符串即契约，issue #53 AC3）。

    ``settled`` 是**已定稿素材**的 ``(语义名, 画面事实)``——画面事实由调用方按
    素材画面事实的优先序取好（合并结果 ＞ 生图提示词 ＞ 用户登记描述，见
    ``generation._with_merged_descriptions``），本函数只负责组装，不猜来源。

    「其余部分逐字保留」这条纪律是硬要求，不是客套：提示词是逐轮小步改的，让模型
    顺手「优化」没提到的部分，跨素材一致性就换来了整篇漂移（同修订基线那句
    「未提到的部分原样保留」的用意）。
    """
    facts = "\n".join(f"- {name}：{description}" for name, description in settled)
    return (
        "下面是一条 Z-Image 生图提示词，以及本片**已定稿**的其他素材的画面事实。\n"
        "请让当前提示词与这些事实在**跨素材一致性**上对齐：同一个人物、同一件标志物"
        "在两张图里必须是同一副样子（同一发色/服装/纹样/材质）。\n"
        "纪律：只做一致性对齐；没被这些事实点到的部分**逐字保留**，"
        "不要新增或删除细节、不要重排版、不要解释。\n"
        "只输出一条可直接复制进生图工具的提示词正文，不要前后缀。\n\n"
        f"{CURRENT_PROMPT_HEAD}\n{str(prompt).strip()}\n\n"
        f"{SETTLED_FACTS_HEAD}\n{facts}"
    )


def align_asset_prompt(
    old_prompt: str,
    settled: Sequence[tuple[str, str]],
    llm,
    *,
    locked_features: Sequence[str] = (),
) -> str:
    """按已定稿素材的画面事实对齐一条资产生图提示词（回灌，一轮）。

    ``llm`` 由调用方注入（带 ``invoke(str)`` 接口，同 ``rewrite_asset_prompt`` 的口径），
    测试可传 stub。

    **锁死特征丢了就响**：对齐请求的纪律是「只做一致性对齐、其余逐字保留」，丢掉锁死
    特征就是越权改词——而锁死清单条目是读图校对判偏差的判据，条目在正文里找不到，
    这一张的偏差就再也判不出来（静默失效）。调用方据此不采用对齐结果、保持原词。
    """
    request = build_settled_align_request(old_prompt, settled)
    new_prompt = _call_direct(llm, request, role=ASSET_ALIGN_ROLE).strip()
    if not new_prompt:
        raise ValueError("回灌对齐结果为空")
    missing = [str(x) for x in locked_features if str(x).strip() and str(x) not in new_prompt]
    if missing:
        raise ValueError(
            f"回灌对齐结果丢了锁死特征：{'、'.join(missing)}——"
            "对齐只许做跨素材一致性，锁死特征是判偏差的判据，不许改掉")
    return new_prompt


# AI 改词请求（issue #72）三块标题。**只有这一处定义**：请求文本、假模型与断言三处
# 各写一遍就是又一个会各自漂移的字面量（同 CURRENT_PROMPT_HEAD 的纪律）。
AI_REWORD_DEVIATIONS_HEAD = "【偏差清单】"
AI_REWORD_LOCKED_HEAD = "【锁死清单】"
AI_REWORD_DISCIPLINE_HEAD = "【生图提示词纪律】"

# AI 改词的角色名：直调入口的事件（agent_start / agent_wait / agent_done）与成本表
# 都按它归类（issue #72 / ADR 0019）。单列一个角色名，成本表里才看得出「AI 改词花了多少」。
AI_REWORD_ROLE = "role_asset_ai_reword"


def build_ai_reword_request(
    prompt: str,
    deviations: Sequence[tuple[str, str, str]],
    locked_features: Sequence[str] = (),
    discipline: str = "",
) -> str:
    """AI 改词请求文本（纯函数，返回字符串即契约，issue #72）。

    ``deviations`` 是**本轮全部**偏差的三元组 ``(哪一处, 描述原文, 图中实见)``——
    不是「已裁过的那几条」：人在第一条上就选了「重生成」时，后面的偏差压根没轮到人裁，
    但它们照样是这张图要修的地方（票面「全部偏差条目」）。

    **方向是强化原意，不是迁就成图**（ADR 0018 末段）：把被画漏/画歪的那几处写显式，
    让下一轮更可能画对；照着图改等于让一张画歪的图反过来改设计，而且那是「按图改文案」
    的活（改素材画面事实），与这里改生图提示词是两件事。这条纪律必须写进请求——不给它，
    模型最省力的做法就是照 observed 抄一遍，图的问题被固化成设计。

    ``discipline`` 由调用方从 ``image_prompt_discipline.discipline_text()`` 现取（#68 的
    单一来源），本函数不复制正文：两份纪律文本必然各自漂。
    """
    items = [
        f"- {aspect}：描述「{quote}」→ 图中「{observed}」"
        for aspect, quote, observed in deviations
    ]
    lines = [
        "下面是一条 Z-Image 生图提示词，以及读图校对给出的偏差清单。",
        "请**按偏差清单改写这条提示词**：方向是**强化原意**——把被画漏、被画错的那几处"
        "写得更显式（具体、可看见、正向陈述），让下一轮出图更可能画对。",
        "纪律：",
        "1. 只改偏差涉及的那几处；没被点到的部分**逐字保留**，不要重排版、不要「顺手优化」。",
        "2. 锁死清单的条目必须在改写结果里**逐字保留**——它们是判偏差的判据，改掉就再也判不出来。",
        "3. **不得改成图里的样子**：图是画歪的现场证据，不是设计。照着图改等于用一张画歪的图"
        "反过来改设计；那属于「按图改文案」，不是这一步。",
        "只输出改写后的**完整提示词正文**，不要解释、不要前后缀。",
        "",
        f"{CURRENT_PROMPT_HEAD}\n{str(prompt).strip()}",
        "",
        f"{AI_REWORD_DEVIATIONS_HEAD}\n" + "\n".join(items),
    ]
    locked = [str(x).strip() for x in locked_features if str(x).strip()]
    if locked:
        lines.extend(["", f"{AI_REWORD_LOCKED_HEAD}\n" + "\n".join(f"- {x}" for x in locked)])
    if str(discipline).strip():
        lines.extend(["", f"{AI_REWORD_DISCIPLINE_HEAD}\n{str(discipline).strip()}"])
    return "\n".join(lines)


def ai_reword_asset_prompt(
    prompt: str,
    deviations: Sequence[tuple[str, str, str]],
    llm,
    *,
    locked_features: Sequence[str] = (),
) -> str:
    """按偏差清单改写一条资产生图提示词（AI 改词，一轮，issue #72）。

    ``llm`` 由调用方注入（带 ``invoke(request, config=...)`` 接口，测试可传 stub）。
    纪律文本**现取** ``discipline_text()``：改 .md 即改模型看到的那一份，
    与角色提示词不可能只改到一半（#68 的单一来源）。

    **调用走直调等待看门狗**（issue #67 / ADR 0019）：ADR 的裁定是「所有直接
    ``llm.invoke`` 改走它」，AI 改词是这条规则落地之后新增的直调点——裸调就没有首字节
    心跳、没有卡住重试、token 也不进成本表（``run_agent`` 之外的第二套等待体验）。
    失败照旧**抛错交给调用方**（词保持原样、留在菜单），病因与尝试次数在错误文案里。

    **锁死特征丢了就响**：同 ``align_asset_prompt`` 的理由——条目是判偏差的判据，
    丢了这一张的偏差就再也判不出来（静默失效）。调用方据此不采用改写结果、保持原词。
    结果是空串也响：空词写回等于把这张图的提示词删了。
    """
    from .tools.image_prompt_discipline import discipline_text

    request = build_ai_reword_request(prompt, deviations, locked_features, discipline_text())
    # 复用同文件的 ``_call_direct``（复审 T4）：它已经把「run_direct_call → 失败翻异常」
    # 这段接好了，这里再抄一遍就是第二份会各自漂移的接线。空串/锁死清单校验留在下面
    # ——那是 AI 改词特有的结果契约，与调用包装不是一回事。
    new_prompt = _call_direct(llm, request, role=AI_REWORD_ROLE).strip()
    if not new_prompt:
        raise ValueError("AI 改词结果为空")
    missing = [str(x) for x in locked_features if str(x).strip() and str(x) not in new_prompt]
    if missing:
        raise ValueError(
            f"AI 改词结果丢了锁死特征：{'、'.join(missing)}——"
            "锁死清单是判偏差的判据，AI 改词只许强化原意、不许把它改掉")
    return new_prompt


__all__ = [
    "AI_REWORD_DEVIATIONS_HEAD", "AI_REWORD_DISCIPLINE_HEAD", "AI_REWORD_LOCKED_HEAD",
    "AI_REWORD_ROLE",
    "ASSET_ALIGN_ROLE", "ASSET_LOOP_KEY", "ASSET_ROUNDS_FILENAME", "ASSET_REWRITE_ROLE",
    "CURRENT_PROMPT_HEAD", "QueueItem",
    "REGEN_TRIGGERS", "SETTLED_FACTS_HEAD",
    "TRIGGER_REGEN_AI_REWORD", "TRIGGER_REGEN_REROLL", "TRIGGER_REGEN_REWORD",
    "TRIGGER_REVIEW_REWORD",
    "ai_reword_asset_prompt", "align_asset_prompt", "asset_queue",
    "build_ai_reword_request", "build_settled_align_request", "count_regen_rounds",
    "log_asset_round", "loop_state", "note_abandoned", "note_released",
    "read_asset_rounds", "render_regen_hint", "rewrite_asset_prompt", "set_current",
]

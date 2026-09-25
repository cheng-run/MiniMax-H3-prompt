"""用户修订：阶段 1 首帧环节人提的意见真源，以及每轮重出的台账。

**为什么要有独立真源**：意见原本拼在 ``brief.plot`` 上走私，而 ``plot`` 同时喂给
主题地点守卫（**按子串**提取地点硬约束）与生图常识判官（「不得新增主题中不存在的
主体、地点或道具」的判据）——散文意见塞进去等于让意见文字随机改写两条闸门的判据，
且有否定句反噬（用户说「不要森林，改到庭院」会让守卫反而开始要求正文出现「森林」）。

**为什么按轮累积**（而不是像自动质检问题那样每轮替换）：人提的意见是一次性说出口的
**断言**，静默丢掉是用户看不见的信息丢失；机检问题是**对当前产物**的判断，产物一变
判断就该重算。两条同形循环的语义分叉见 ``docs/adr/0005``。术语见 ``CONTEXT.md``。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

# 修订台账文件名（``<generation_dir>/frame-rounds.jsonl``，CONTEXT.md「修订台账」）。
FRAME_ROUNDS_FILENAME = "frame-rounds.jsonl"

# 修订基线的**独立入参键**（issue #25）：人机修改循环每轮重出时，把上一轮完整产物渲染好的
# 「修订基线」块放进 state 的这个键，首帧节点读它注入请求。独立的理由是语义分叉：基线与
# 「人的修订累积」同属人机循环，而**自动质检循环不设它**（它是对当前产物重算的替换语义，
# 不能滑成「以用户上一版为基线」，见 ADR 0005）。块由渲染函数产出而非节点自己拼，于是
# 「有没有基线」这件事只有一个判据——台账的 base_used 取的就是「这个键在不在 node 的入参里」。
FRAME_BASELINE_KEY = "frame_revision_baseline"

# 本轮**被撤销**的修订块（issue #24 / T2）的独立入参键。同样是一轮的入参、跑完即摘：
# 撤销是「这一轮谁不再生效」这件事，不是产物。与基线**分开成键**的理由是不能共用——
# 台账的 base_used 取「基线的键在不在入参里」，若把撤销说明塞进基线块，撤销轮就会在
# 没有上一版产物时也记成「上了基线」，而它是事后判定累积生效与否的唯一分组变量。
FRAME_REVOKED_KEY = "frame_revision_revoked"

# 层次＝重跑起点（CONTEXT.md「用户修订」）。四档：只改画面 ＋ 三档设定级。
LAYER_FRAME = "frame"
LAYER_CHARACTER = "character"
LAYER_ART = "art"
LAYER_STORY = "story"
# 标签逐字取自 ``CONTEXT.md``「用户修订」与票面那句「只改画面／人物设定／美术场景道具／
# 剧情分镜」：这两处会被用户与后人对照着读，写得好看一点（如「美术/场景/道具」）就变成
# 第三种写法，谁也说不清屏幕上那个词对应术语表里的哪一档。
LAYER_LABELS = {
    LAYER_FRAME: "只改画面",
    LAYER_CHARACTER: "人物设定",
    LAYER_ART: "美术场景道具",
    LAYER_STORY: "剧情分镜",
}


@dataclass(frozen=True)
class LayerSpec:
    """一档**设定级**层次：回灌哪些产物、从阶段 1 链的哪个节点起跑。

    ``resume_from`` 就是 ``graph.pipeline._STAGE1_CHAIN`` 里的节点名：层次**直接等于**
    重跑起点，不另造一套映射（另造一套的话「重跑哪一段」要再判一次，两张表迟早对不上）。
    在这里写字面量而不是导入链：本模块是项目最底层的域概念（管线反过来导入它），
    导入会把依赖方向倒过来。有测试逐个核对起点确实在链上。

    ``note`` 是屏幕上那句「回灌什么、重跑什么」——用户凭它选层次。
    """

    value: str
    label: str
    note: str
    resume_from: str


# 顺序＝屏幕上的编号（1/2/3）。只改画面**不在**这张表里：它的重跑起点就是首帧提示词节点
# 本身，历史上是一次直接的节点调用（``wizard._user_revision_loop``），不走截断链——塞进来
# 只会多出一个没人读的节点名，还会诱导后人把画面级也改成重跑整条链。
SETTING_LAYERS: tuple[LayerSpec, ...] = (
    LayerSpec(LAYER_CHARACTER, LAYER_LABELS[LAYER_CHARACTER],
              "回灌人物形象设计，并重跑其下游", "parallel_designers"),
    LayerSpec(LAYER_ART, LAYER_LABELS[LAYER_ART],
              "回灌背景与道具设计，并重跑其下游", "parallel_designers"),
    LayerSpec(LAYER_STORY, LAYER_LABELS[LAYER_STORY],
              "回灌分场剧本与分镜表，并重跑其下游", "screenwriter"),
)
LAYER_SPECS = {spec.value: spec for spec in SETTING_LAYERS}

# 本轮那条设定级修订的**独立入参键**（issue #28 / T6）。值是一个字典 ``{"layer", "text"}``：
# 带来层次是为了让**节点自己**决定吃不吃它——重跑起点是
# 节点组（``parallel_designers`` 一次跑三个设计师），不按层次过滤的话，一条「汉服改成
# 现代审美」会连背景与道具设计一起改写。与基线、撤销同为「一轮的入参」：跑完即摘，
# 不进持久化白名单。
SETTING_REVISION_KEY = "setting_revision"


def _entry_round(entry: Any) -> int:
    """条目里的轮次号；缺失或不合法都报错——静默归零会让后续轮次与台账行号重复。"""
    if not isinstance(entry, dict) or "round" not in entry:
        raise ValueError(f"用户修订条目缺少轮次：{entry!r}")
    try:
        return int(entry["round"])
    except (TypeError, ValueError) as exc:
        raise ValueError(f"用户修订条目的轮次不是整数：{entry!r}") from exc


def _checked(entry: Any) -> dict[str, Any]:
    """校验清单条目（对象、有内容、有轮次）。

    不合法就报错而**不是静默跳过**：静默丢掉一条修订，正是本模块存在的理由所要消灭的
    那种故障（用户说了、系统没听见、用户看不见）。条目只由 ``record_user_revision``
    写入，真读到坏条目意味着状态被手工改过或损坏，此时声张比容忍安全。
    """
    if not isinstance(entry, dict):
        raise ValueError(f"用户修订条目必须是对象：{entry!r}")
    if not str(entry.get("text", "")).strip():
        raise ValueError(f"用户修订条目没有内容：{entry!r}")
    _entry_round(entry)
    return entry


def _layer_label(layer: Any) -> str:
    """层次的展示标签；未登记的取值原样透出（T6 新增档位时不必先改这里）。"""
    value = str(layer or "")
    return LAYER_LABELS.get(value, value) or "未标层次"


def next_round(state: dict[str, Any]) -> int:
    """下一轮重出的轮次号（**1 起**；初始产物不算一轮重出，它记在会话状态里）。

    只增不减：按编号回收任意一条（T2）之后也必须继续往上走——若按「清单长度 + 1」
    推，撤掉一条就会重发一个用过的轮次，台账里出现两行 ``round`` 相同，事后无法按轮
    对齐。状态里没有计数器时（旧会话、手工改过的 state）从清单里的最大轮次自愈。
    """
    recorded = max((_entry_round(entry) for entry in state.get("user_revisions") or []), default=0)
    return max(int(state.get("frame_round") or 0), recorded) + 1


def begin_round(state: dict[str, Any]) -> int:
    """开一个新轮次：返回本轮轮次号，并把计数器推上去（**只增不减**）。

    为什么单独成一个函数：**撤销轮没有新增条目**（T2），轮次号得自己推。不推的话连续
    两轮撤销会拿到同一个轮次号，台账里出现两行 ``round`` 相同——正是 ``next_round``
    的 docstring 要防的那种事后无法按轮对齐。
    """
    index = next_round(state)
    state["frame_round"] = index
    return index


def record_user_revision(state: dict[str, Any], *, layer: str, text: str) -> dict[str, Any]:
    """把一条意见追加进累积清单，返回写入的条目（就地改 state）。

    空意见是调用方的 bug：静默写入会让台账多出一条没有内容的轮次，所以直接报错。
    """
    content = str(text).strip()
    if not content:
        raise ValueError("用户修订不能为空")
    entry = {"round": begin_round(state), "layer": str(layer), "text": content}
    state["user_revisions"] = [*(state.get("user_revisions") or []), entry]
    return entry


def remove_user_revision(state: dict[str, Any], screen_number: Any) -> dict[str, Any]:
    """按**屏幕编号**撤掉清单里的一条，返回被撤掉的条目（就地改 state）。

    编号取**待落地清单里 1 起的位置**，与注入块、屏幕清单同源（见 ``render_revision_list``）：
    用户照着屏幕念编号即可，撤掉一条后其余条重排序，下一次念的就是重排后的编号。
    不取「提出时的轮次」——那个编号与屏幕上的序号对不上，人要撤第 3 条得先去查台账。

    取待落地清单而不是全量清单：**已落地**的条目不在带编号的清单里（它的改动已经写进
    设定产物，撤掉文本也撤不掉产物），若编号把它算进去，屏幕上念的号与注入块就各指一条。

    ``screen_number`` 收 ``Any`` 而不收 ``int``：调用方给的是用户原样敲进来的字符串，
    这里要的是**报错**而不是 TypeError——越界与非法编号一律声张、不静默忽略，否则用户
    以为撤掉了、实际没撤，随后「重出后被撤的改动还在」又变成一条不可证故障（本模块
    存在的理由）。
    """
    stored = [_checked(item) for item in state.get("user_revisions") or []]
    pending = [item for item in stored if not item.get("applied")]
    try:
        position = int(screen_number)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"撤销编号必须是数字：{screen_number!r}") from exc
    if not 1 <= position <= len(pending):
        raise ValueError(f"撤销编号超界：清单现有 {len(pending)} 条，收到 {position}")
    victim = pending[position - 1]
    # 按对象身份摘那一条：已落地的条目要原样留着（它们是「这条已经落实过」的唯一记录）
    state["user_revisions"] = [item for item in stored if item is not victim]
    return victim


def active_revisions(state: dict[str, Any]) -> list[dict[str, Any]]:
    """当前清单的**全量快照**（副本；台账取它，不递内部对象出去）。

    **注入通道不要直接用它**——用 ``pending_revisions``：已落地的条目在这份全量里，
    但它们不该再进请求。两者分开正是「台账要全量、注入要待落地」这条分叉的落点。
    """
    return [dict(_checked(item)) for item in state.get("user_revisions") or []]


def pending_revisions(state: dict[str, Any]) -> list[dict[str, Any]]:
    """**还没落地**的修订：所有注入通道的真源（首帧节点、分段写段、分段规划）。

    已落地的条目（设定级重跑成功那条）不再进去：它的要求已经写进设定产物本身，而那些
    产物正在同一个请求里作为「人物设计／背景设计／分镜表」被注入——再带一遍就是拿同一条
    要求叠加第二次，越滚越重（票面 AC-4）。
    """
    return [entry for entry in active_revisions(state) if not entry.get("applied")]


def applied_revisions(state: dict[str, Any]) -> list[dict[str, Any]]:
    """**已落地**的修订：屏幕上单列一段，让用户看得见自己的要求去了哪里。

    为什么不留在带编号的清单里：编号是「模型看到的第几条」与「撤销要念的号」共用的那一套
    （T2）。已落地的条目既然不再注入，留在编号里就会让同一个号指向两条不同的话——用户
    撤错人也发现不了。
    """
    return [entry for entry in active_revisions(state) if entry.get("applied")]


def mark_revision_applied(state: dict[str, Any], entry: dict[str, Any]) -> dict[str, Any]:
    """把一条修订标为**已落地**——只在设定级重跑**成功之后**调。

    按 ``(round, layer, text)`` 在 state 里重新定位那一条再改，而不是直接改传进来的对象：
    ``state`` 往往是从会话 JSON 载回来的，传进来的那份与 state 里存的那份早已不是同一个，
    改游离的副本等于没标——而「标没标」正是「清单不得谎报」的唯一凭据。

    定位不到就报错：state 与标记打架是 bug，静默返回会让这条修订永远重复注入。
    """
    target_round = _entry_round(entry)
    for stored in state.get("user_revisions") or []:
        _checked(stored)
        if (int(stored["round"]) == target_round
                and str(stored.get("layer") or "") == str(entry.get("layer") or "")
                and str(stored.get("text")).strip() == str(entry.get("text")).strip()
                and not stored.get("applied")):
            stored["applied"] = True
            return stored
    raise ValueError(f"要标为已落地的修订不在清单里：{entry!r}")


def render_applied_list(revisions: Iterable[dict[str, Any]] | None) -> str:
    """已落地修订的屏幕段落（**不编号**，故不会与撤销编号混起来）。

    用横线起头而不是数字：带编号的行是「可以照号撤掉的」，这一段的条目撤不掉（它的改动
    已经在设定产物里），长得一样会让人以为自己能撤它。
    """
    entries = [_checked(entry) for entry in revisions or []]
    if not entries:
        return ""
    return "\n".join(
        f"- [{_layer_label(entry.get('layer'))}] {str(entry['text']).strip()}（已落地，不再注入）"
        for entry in entries
    )


def setting_revision_block(state: dict[str, Any], layer: str) -> str:
    """本轮那条设定级修订的注入块；**层次不匹配就是一个字都不进**（返回空串）。

    按层次过滤的理由见 ``SETTING_REVISION_KEY``：重跑起点是节点组，三个设计师一次全跑，
    不过滤就会把「改人物」的意见写进背景与道具设计。节点侧只报自己的层，判断全在这里
    ——两处各判一次迟早有一处写成 ``in``。

    措辞必须写明「落实进**你这一层重新产出的产物本身**」：这句话是设定级与画面级的分界，
    少了它模型会把它当成画面口味，产出照旧只在画面里体现（票面根因 2）。
    """
    raw = state.get(SETTING_REVISION_KEY)
    if not isinstance(raw, dict) or str(raw.get("layer") or "") != str(layer):
        return ""
    text = str(raw.get("text") or "").strip()
    if not text:
        return ""
    spec = LAYER_SPECS.get(str(layer))
    return "\n".join([
        f"【本轮设定级修订（{spec.label if spec else layer}）】",
        text,
        "【落实要求】这条是**设定级**要求：请把它直接落实进你这一层负责重新产出的产物本身"
        "（不是只在画面里体现），下游的产物会据此一并重跑。",
    ])


def _numbered_lines(entries: list[dict[str, Any]]) -> list[str]:
    """清单的带编号行（**唯一**的编号规则：注入块与屏幕清单都从这里取）。

    编号取**当前活动清单**的位置而不是提出时的轮次：按编号回收任意一条时，编号与
    清单一一对应，回收后不必重排台账；两处共用同一份编号，「我撤的是模型看到的第 2 条
    吗」才有确定答案（T2 AC-1）。
    """
    return [
        f"{index}. [{_layer_label(entry.get('layer'))}] {str(entry['text']).strip()}"
        for index, entry in enumerate(entries, 1)
    ]


def render_revision_block(revisions: Iterable[dict[str, Any]] | None) -> str:
    """把累积清单渲染成**给模型**的注入块；空清单返回空串（``_ctx`` 空值自动跳过）。"""
    entries = [_checked(entry) for entry in revisions or []]
    if not entries:
        return ""
    lines = _numbered_lines(entries)
    lines.append("（以上为用户累积提出的修订：每一条都必须落实，不是只落实最后一条。）")
    return "\n".join(lines)


def render_revision_list(revisions: Iterable[dict[str, Any]] | None) -> str:
    """把累积清单渲染成**屏幕清单**（带编号，供人对编号撤销）；空清单返回空串。

    与注入块分开的是**话术**不是编号：注入块要多一句「每一条都必须落实」的累积语义声明
    （那是给模型的），屏幕清单要的是干净的行、好让人一眼对上编号。两处的行必须逐字同源，
    所以编号规则只写在 ``_numbered_lines`` 里。
    """
    return "\n".join(_numbered_lines([_checked(entry) for entry in revisions or []]))


def render_revocation_block(revoked: Iterable[dict[str, Any]] | None) -> str:
    """把本轮**被撤销的修订**渲染成注入块；没有撤销时返回空串。

    为什么撤销必须说给模型听（票面 AC-3「被删那条的改动消失」）：只把条目从注入清单里
    摘掉，模型手里还有**修订基线**那块——它明写「用户修订没提到的部分必须原样保留」，
    于是上一版里因该条而落地的改动（票面场景里的「秋日庭院」）反倒被保住，撤销成了空动作。
    所以撤销轮要显式告知「这几条不再生效、其造成的画面改动要撤回」。

    与「撤掉被后条覆盖的旧条」相容：旧条本来就被后条压着，撤回它是 no-op；末句把
    「其余修订仍在生效」写死，免得它与仍在清单里的要求打架时模型挑错边。
    """
    entries = [_checked(entry) for entry in revoked or []]
    if not entries:
        return ""
    lines = [
        "本轮用户**撤销**了以下修订，它们不再生效：",
        *[f"- {str(entry['text']).strip()}" for entry in entries],
        "请把上一版里**因这些修订而出现**的画面改动一并撤回；上一版中并非因它们出现的细节"
        "仍然原样保留。若本轮其余修订也要求了同一处改动，以其余修订为准（它们仍在生效）。",
    ]
    return "\n".join(lines)


def revocation_note(revoked: Iterable[dict[str, Any]] | None) -> str:
    """撤销轮记进台账 ``feedback_this_round`` 的那句话（没有撤销时返回空串）。

    放这里而不是放在 UI：它按条目抠 ``text``，与 ``_numbered_lines`` 是同一族的「条目
    → 文本」规则，两处各写一份迟早对不上。

    记文本而**不记编号**：编号是撤的时候现念的位置号，撤掉一条后其余条重排序，事后再看
    编号已经指不回原来那条。也**不留空串**——票面 AC-5 说不必为撤销单开一列，但本轮到底
    发生了什么必须可读，空串会被后来的人读成「漏记」而不是「这一轮只撤了条」。
    """
    entries = [_checked(entry) for entry in revoked or []]
    if not entries:
        return ""
    return "（本轮无新意见，撤销：" + "；".join(str(entry["text"]).strip() for entry in entries) + "）"


_FRAME_LABELS = (("first", "首帧"), ("last", "尾帧"))


def _text_list(value: Any) -> list[str]:
    """把「一条或多条文本」规整成清单。

    字符串按**单条**处理而不是逐字符迭代：``continuity_constraints`` 在产物里是列表，
    裸字符串一旦进来，按可迭代拆开就会往基线里塞一串单字——渲染出的块看着有内容，
    实际全是噪声，而且不报错。
    """
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _frame_entries(frames: Any) -> list[tuple[str, str]]:
    """一帧的 ``(模型名, 正向提示词)`` 清单；模型名不知道就留空（渲染时省略标签）。

    认的形态与 ``FL2VAPromptBundle.from_dict`` 一致：节点落盘的是 ``to_dict()`` 的列表
    （每项带 ``model_family``），模型原始输出是「模型名分组」的 dict——同一份状态在仓里
    本来就有这两种写法，认窄了会让渲染悄悄少认一种。

    不复用 ``FL2VAPromptBundle.from_dict``：它缺锚点／空帧就抛，而这里「没有上一版产物」
    是合法输入（票面 AC-5 要求不报错），容错边界不同。
    """
    if isinstance(frames, dict):
        rows: list[tuple[str, Any]] = (
            [("", frames)] if ("positive_prompt" in frames or "prompt" in frames)
            else list(frames.items())
        )
    elif isinstance(frames, list):
        rows = [("", item) for item in frames]
    else:
        return []
    entries: list[tuple[str, str]] = []
    for model, item in rows:
        if not isinstance(item, dict):
            continue
        text = str(item.get("positive_prompt") or item.get("prompt") or "").strip()
        if text:
            entries.append((str(item.get("model_family") or model or ""), text))
    return entries


def render_baseline_block(bundle: Any) -> str:
    """把**上一轮完整产物**渲染成「修订基线」块；没有可用的上一版产物时返回空串。

    为什么取完整产物而不是只喂正文文本：``scene_anchor`` 与 ``continuity_constraints``
    正是首尾帧连续性那几条 mismatch 闸门校验的字段，只喂正文会让基线在被校验的字段上
    失锚（issue #25）。返回空串就是「尚无上一版产物」，调用方据此不设键、台账记
    ``base_used=False``，都不报错——首轮重出走的就是这条。
    """
    raw = bundle if isinstance(bundle, dict) else None
    if not raw:
        return ""
    frames = [(label, _frame_entries(raw.get(name))) for name, label in _FRAME_LABELS]
    anchor = str(raw.get("scene_anchor") or "").strip()
    constraints = _text_list(raw.get("continuity_constraints"))
    if not anchor and not constraints and not any(entries for _, entries in frames):
        return ""
    lines = [
        "上一版（用户已看过的那一版）关键帧生图提示词如下。本次是在它基础上的定向修改，"
        "不是重新创作：",
        "- 用户修订没有提到的部分必须原样保留：人物、服装、道具、场景陈设、构图与光线"
        "细节都不许漂移；",
        "- 与用户修订冲突的地方，一律以用户修订为准；",
        "- 场景锚点与连续性约束是首尾帧连续性校验的判据，除用户修订明确要求外必须保留。",
    ]
    if anchor:
        lines.append(f"场景锚点：{anchor}")
    for label, entries in frames:
        if entries:
            lines.append(f"{label}提示词：")
            # 模型名不知道就只写提示词本身；不要往发给模型的提示词里塞「?」这种占位符
            lines.extend(f"- {model}：{text}" if model else f"- {text}"
                         for model, text in entries)
    if constraints:
        lines.append("连续性约束：")
        lines.extend(f"- {item}" for item in constraints)
    return "\n".join(lines)


def log_frame_round(directory: str | Path, *, round_index: int, layer: str,
                    feedback_this_round: str, active_revisions: list[dict[str, Any]],
                    bundle: dict[str, Any] | None, base_used: bool) -> Path:
    """追加一行修订台账（``<generation_dir>/frame-rounds.jsonl``）。

    ``active_revisions`` 取**当轮活动清单的全量快照**而非增量：于是「某条修订在第 N 轮
    被撤掉」可事后 diff 相邻两轮看出来，撤销本身不必单开一列。
    ``base_used`` 是日后判定「累积到底生效没有」的分组变量——缺它就无法把「上了上一版
    做基线」与「纯重掷」的轮次分开看（基线由人机修改循环经 ``FRAME_BASELINE_KEY``
    传入，见 issue #25；调用方按「键在不在传给节点的 state 里」如实记）。
    """
    record = {
        "round": int(round_index),
        "layer": str(layer),
        "layer_label": _layer_label(layer),
        "feedback_this_round": str(feedback_this_round),
        "active_revisions": [dict(_checked(item)) for item in active_revisions],
        "bundle": bundle,
        "base_used": bool(base_used),
    }
    path = Path(directory) / FRAME_ROUNDS_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    return path


__all__ = [
    "FRAME_BASELINE_KEY", "FRAME_REVOKED_KEY", "FRAME_ROUNDS_FILENAME",
    "LAYER_ART", "LAYER_CHARACTER", "LAYER_FRAME", "LAYER_LABELS", "LAYER_SPECS",
    "LAYER_STORY", "LayerSpec", "SETTING_LAYERS", "SETTING_REVISION_KEY",
    "active_revisions", "applied_revisions", "begin_round", "log_frame_round",
    "mark_revision_applied", "next_round", "pending_revisions", "record_user_revision",
    "remove_user_revision", "render_applied_list", "render_baseline_block",
    "render_revision_block", "render_revision_list", "render_revocation_block",
    "revocation_note", "setting_revision_block",
]

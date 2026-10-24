"""读图校对对照器（issue #44）：生图提示词 × 读图结果 → 偏差清单 + 补漏候选。

**设计红线（素材画面事实，CONTEXT.md）**：生图提示词是描述主体（细节密度高于读图复述），
读图只做两件事——纠偏差（与描述不符处列成清单交人裁决）与补漏（图里有而描述没有的
显著事实并入）。本模块**没有**任何「拿读图整体替换生图提示词」的路径：合并函数
``merge_asset_description`` 只做定点替换（偏差处）与尾部追加（补漏），主体文本原样保留。
「只改不符的那处」是结构性的：改写落点必须**唯一命中**且**不落在语义名内**（语义名是
标识符、设计意图，不是可纠偏的描述），由 ``locate_rewrite_span`` 统一定位，
按位置替换——引文命中多处或只命中语义名时响 ``ValueError``，绝不猜一处改了算。

对照器是**纯函数**（票面唯一新接缝）：不建模、不联网、不落盘，同输入必同输出，
故可独立测试；读图（视觉模型）与人裁三选（UI）都在它之外——前者喂进结构化
读图结果，后者消费它产出的清单。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace
from typing import Sequence


@dataclass(frozen=True)
class Claim:
    """读图结果里的一条事实：图里看到的（observed）对应提示词里的哪句话（prompt_quote）。

    ``prompt_quote`` 为空 ＝ 提示词没写这条（补漏候选的原料）；非空 ＝ 读图声称与该句
    不符（偏差候选的原料）。引文是**人裁「按图改文案」时的定位锚**——改动只落在引文
    那一处，故读图指令要求逐字摘录、且精确到在描述里只出现一次。

    与 ``Deviation`` 三字段同形是刻意分层：Claim 是**读图原话**（对照器的输入），
    Deviation 是**对照产物**（进人裁与合并）；两者各自演化，不是别名。
    """

    aspect: str
    prompt_quote: str
    observed: str

    def __post_init__(self) -> None:
        # 没有「图里实况」的条目无从对照——精确数据坏了要响（parse 侧同样会响），
        # 静默丢弃就是「图跑偏无人纠」的入口。
        if not str(self.observed).strip():
            raise ValueError(f"读图 claim 缺 observed（图里实况）：{self!r}")

    @classmethod
    def from_dict(cls, raw: object) -> "Claim":
        if not isinstance(raw, dict):
            raise ValueError(f"读图 claim 必须是对象：{raw!r}")
        return cls(
            aspect=str(raw.get("aspect", "")).strip(),
            prompt_quote=str(raw.get("prompt_quote", "")).strip(),
            observed=str(raw.get("observed", "")).strip(),
        )


@dataclass(frozen=True)
class Reading:
    """读图结果：视觉模型对照「生图提示词 × 实际画面」给出的事实清单。"""

    claims: tuple[Claim, ...] = ()

    @classmethod
    def from_dict(cls, raw: object) -> "Reading":
        if not isinstance(raw, dict):
            raise ValueError(f"读图结果必须是 JSON 对象：{raw!r}")
        claims = raw.get("claims")
        if not isinstance(claims, list):
            raise ValueError(f"读图结果缺少 claims 数组：{raw!r}")
        return cls(claims=tuple(Claim.from_dict(item) for item in claims))


_JSON_FENCE_RE = re.compile(r"^```[A-Za-z]*\s*|\s*```$", re.MULTILINE)


def _extract_json_object(raw: str) -> dict:
    """从模型输出里抠出 JSON 对象；抠不出抛 ValueError（不静默返回空清单）。"""
    cleaned = _JSON_FENCE_RE.sub("", str(raw)).strip()
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start == -1 or end <= start:
            raise ValueError(f"读图结果不是 JSON：{raw[:200]!r}")
        try:
            data = json.loads(cleaned[start:end + 1])
        except json.JSONDecodeError as exc:
            raise ValueError(f"读图结果 JSON 解析失败：{exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"读图结果必须是 JSON 对象：{raw[:200]!r}")
    return data


def parse_reading(raw: str) -> Reading:
    """解析读图结果（JSON 文本 → ``Reading``）。坏输入**响**（ValueError）——
    读图解析失败若降级成空清单，「图跑偏」就无人纠了（素材画面事实红线）。"""
    return Reading.from_dict(_extract_json_object(raw))


# ---------------------------------------------------------------------------
# 对照器输出：偏差清单 + 补漏候选
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Deviation:
    """偏差一条：提示词里 ``prompt_quote`` 那一处与图不符，图里实为 ``observed``。

    ``prompt_quote`` 是「按图改文案」的改写落点（人裁时可补锚改写它，见向导）；
    与 ``Claim`` 三字段同形是分层不是别名——Claim 是读图原话（对照器输入），
    Deviation 是对照产物（进人裁与合并），两者各自演化。
    """

    aspect: str
    prompt_quote: str
    observed: str


@dataclass(frozen=True)
class Omission:
    """补漏候选一条：图里有（``fact``）而描述没有的显著事实。"""

    aspect: str
    fact: str


@dataclass(frozen=True)
class ProofreadReport:
    """对照器输出：偏差清单（交人三选裁决）+ 补漏候选（并入显著事实）+ 被忽略的出入。

    ``exempted`` 是**被锁死清单豁免**的理想描述出入（issue #53）：不判偏差、不进人裁、
    也不并入描述，但**留痕**——它是「忽略了几条」的唯一可观测面。AC2 的卖点正是
    「39 条偏差收敛到个位数」，收敛了多少若代码产不出来，清单配歪（某条过宽导致全部
    命中）时人拿不到任何信号（本仓纪律：绝不静默失败）。
    """

    deviations: tuple[Deviation, ...] = ()
    omissions: tuple[Omission, ...] = ()
    exempted: tuple[Claim, ...] = ()

    def is_clean(self) -> bool:
        """没有要人裁、要并入的东西即为干净——被忽略的条目不算「不干净」（它们不需动作）。"""
        return not self.deviations and not self.omissions


def _locked_hit(quote: str, locked: Sequence[str]) -> bool:
    """引文是否命中锁死清单（字面判据）：与某条清单条目**互为子串**即算命中。

    条目是生图提示词正文的原文片段（``nodes._normalize_asset_entries`` 钉住这条不变式），
    所以读图逐字摘录的那句通常**包含**条目；反向（条目包含引文）也认——模型可能只摘了
    条目的一截（「青铜」之于「青铜钥匙」）。两个方向都是字面判据，不猜语义
    （同 theme_guard 的子串口径）；空条目先滤掉：空串是任何串的子串，留着判据就恒真。
    """
    text = str(quote).strip()
    return any(text in entry or entry in text for entry in locked)


def compare_descriptions(
    image_prompt: str,
    reading: Reading,
    locked_features: Sequence[str] = (),
) -> ProofreadReport:
    """对照器（纯函数）：生图提示词 × 读图结果 → 偏差清单 + 补漏候选。

    判据（确定性，可独立测试）：

    - ``prompt_quote`` 空 → 补漏候选（提示词没写的显著事实）；
    - ``prompt_quote`` 非空且**命中锁死清单** → 偏差（读图声称描述里这句与图不符）；
    - ``prompt_quote`` 非空但**不命中**清单 → **理想描述**的出入（issue #53 裁定）：
      不判偏差、不进人裁，**也不进补漏**——直接忽略。

    降级为什么是「忽略」而不是「转补漏」（2026-10-20 裁定）：引文非空＝读图说的是
    「描述里这句与图不符」，即**它是「不符」不是「没有」**；当补漏并进去会让描述
    自相矛盾（原说红裙、又补一句蓝裙）。补漏仍是「图里有而描述没有」那条空引文的路，
    机制不变。实测（真实现场 39 条偏差重放）：窄清单下 10 条进人裁、29 条忽略、
    补漏候选维持原有的 24 条。

    为什么要收窄（2026-10-18 实测）：拿整篇提示词当判据时，8 张图出了 39 条偏差、
    38 条被裁成「按图改文案」——大头是构图口味级出入（「罐体按三分法偏左」vs 实测
    「几乎正中」），图是既成事实、文案跟改，人裁被淹。锁死清单把判据收到少数身份特征
    （发色/服装/标志物）上，那些才是「这张图认不出是同一个人」的根因。

    ``locked_features`` **空 = 未声明锁死清单**（旧产物、旧会话）＝不收窄：非空引文仍
    一律判偏差。清单是「收窄判据」的声明，没声明就不收窄——否则旧会话的图跑偏无人纠。

    引文**是否逐字命中**原文不改分类（命中与否只由锁死清单决定）、只记进
    ``Deviation.prompt_quote`` 供「按图改文案」定点落锚：失锚引文若静默丢弃就回到
    「图跑偏无人纠」；向导会引导人补锚后再落笔。

    补漏再做一道**字面去重**：实况文字已逐字出现在描述里的不算补漏（描述已有的事实
    再并一遍是冗余，不是补漏）。字面判据不猜语义（同 theme_guard 的子串口径）——
    漏判只是少一条候选，人裁时看得见；错并一条冗余事实倒会污染描述主体。
    """
    prompt = str(image_prompt)
    locked = tuple(text for text in (str(x).strip() for x in locked_features) if text)
    deviations: list[Deviation] = []
    omissions: list[Omission] = []
    exempted: list[Claim] = []
    for claim in reading.claims:
        # Claim 保证 observed 非空（构造即校验），这里不再有「静默跳过」分支。
        if claim.prompt_quote:
            if locked and not _locked_hit(claim.prompt_quote, locked):
                # 理想描述出入：忽略（#53 AC2 裁定）。不转补漏——引文非空＝「不符」
                # 不是「没有」，并进描述就是红裙+蓝裙同现那种自相矛盾。留痕：忽略的
                # 条数与条目进 ``exempted``（收窄了多少必须看得见）。
                exempted.append(claim)
                continue
            deviations.append(Deviation(claim.aspect, claim.prompt_quote, claim.observed))
        elif claim.observed not in prompt:
            omissions.append(Omission(claim.aspect, claim.observed))
    return ProofreadReport(tuple(deviations), tuple(omissions), tuple(exempted))


# ---------------------------------------------------------------------------
# 人裁三选与素材画面事实合并
# ---------------------------------------------------------------------------

# 三选（票面口径）：按图改文案／重生成这张素材图／明知故犯保留原描述。
# 默认建议按图改文案——建议属 UI 接线（向导把 DEFAULT_DECISION 当回车默认）。
DECISION_REWRITE = "rewrite"
DECISION_REGENERATE = "regenerate"
DECISION_KEEP = "keep"
DECISIONS = (DECISION_REWRITE, DECISION_REGENERATE, DECISION_KEEP)
DEFAULT_DECISION = DECISION_REWRITE

# 三选的屏幕名（续跑时的「沿用上次裁决：…」要念得出话；裸常量名等于没念）。
# 与 ``ui.wizard._ask_deviation_choice`` 的菜单同源同序——两处各写一份必然漂。
DECISION_LABELS = {
    DECISION_REWRITE: "按图改文案",
    DECISION_REGENERATE: "重生成这张素材图",
    DECISION_KEEP: "明知故犯保留原描述",
}


@dataclass(frozen=True)
class Adjudication:
    """一条偏差的裁决结果：偏差本身 + 人选的出路。

    ``replacement`` 只对「按图改文案」有意义：替换文字默认取图中实见（``observed``）；
    人补锚换了引文后，替换文字要跟着新引文走（粘贴的原文比图中实见宽时，
    默认替换会吞掉引文里的其他细节），故留给人改写，空串 = 用 observed。
    """

    deviation: Deviation
    decision: str
    replacement: str = ""

    def __post_init__(self) -> None:
        if self.decision not in DECISIONS:
            raise ValueError(f"无效裁决：{self.decision!r}（只认 {', '.join(DECISIONS)}）")

    def to_dict(self) -> dict:
        return {
            "aspect": self.deviation.aspect, "prompt_quote": self.deviation.prompt_quote,
            "observed": self.deviation.observed, "decision": self.decision,
            "replacement": self.replacement,
        }

    @classmethod
    def from_dict(cls, raw: object) -> "Adjudication":
        if not isinstance(raw, dict):
            raise ValueError(f"裁决记录必须是对象：{raw!r}")
        unknown = set(raw) - {"aspect", "prompt_quote", "observed", "decision", "replacement"}
        if unknown:
            raise ValueError(f"裁决记录包含未知字段：{', '.join(sorted(unknown))}")
        return cls(
            Deviation(str(raw.get("aspect", "")), str(raw.get("prompt_quote", "")),
                      str(raw.get("observed", ""))),
            str(raw.get("decision", "")),
            str(raw.get("replacement", "")),
        )


def locate_rewrite_span(text: str, quote: str, name: str = "") -> int:
    """「只改不符的那处」的定位锚（纯函数）：引文在正文里、语义名之外的**唯一**命中。

    返回该命中在 ``text`` 中的起始下标。三条路都响 ``ValueError``，不猜一处改了算：

    - 引文空／正文零命中 ＝ 失锚（读图可能改写了措辞）——由人补锚后重来；
    - 命中多处 ＝ 「那一处」不唯一，改哪处说不清——请人粘贴更长的原文钉住；
    - 命中只落在**语义名**里（或与语义名相交）＝ 名字是标识符、设计意图
      （registry 的 name 键），不是可纠偏的描述——它永远不参与按图改文案，
      否则描述与登记名失同步（评审点名的缺陷场景）。
    """
    if not str(quote).strip():
        raise ValueError("引文为空，定位不了改写落点")
    hay, needle = str(text), str(quote)
    hits = [i for i in range(len(hay) - len(needle) + 1) if hay.startswith(needle, i)]
    label = str(name or "")
    if label:
        spans = [j for j in range(len(hay) - len(label) + 1) if hay.startswith(label, j)]
        hits = [
            i for i in hits
            if not any(j < i + len(needle) and i < j + len(label) for j in spans)
        ]
        if not hits:
            raise ValueError(
                f"引文 {quote!r} 在正文里没有落点（找不到，或只命中语义名「{label}」）——"
                "名字是标识符不参与改文案；请粘贴描述正文里的原文，或改选其他裁决")
    if not hits:
        raise ValueError(f"引文 {quote!r} 在描述里找不到——失锚，无法只改那一处")
    if len(hits) > 1:
        raise ValueError(
            f"引文 {quote!r} 在描述里命中 {len(hits)} 处，「那一处」不唯一——"
            "请粘贴更长的原文把落点钉到唯一一处")
    return hits[0]


def merge_asset_description(
    image_prompt: str,
    adjudications: Sequence[Adjudication],
    additions: Sequence[str] = (),
    *,
    name: str = "",
) -> str:
    """素材画面事实合并（纯函数）：生图提示词为底，裁决定点落笔、补漏尾部并入。

    三条出路怎么落：

    - **按图改文案**：把引文那一处原样换成替换文字（默认 ``observed``）。落点由
      ``locate_rewrite_span`` 定位——**按位置换一处**（不是全局 replace），
      语义名内的命中不算落点；失锚／多处命中／只命中语义名都**抛 ValueError**，
      由人补锚后重来（静默不动＝偏差没纠，静默多改＝越权，两头都踩红线）。
    - **重生成这张素材图**：文案不动（图要按描述重画，描述仍是主体）。
    - **明知故犯保留原描述**：文案不动。

    补漏以「；」为分隔尾部并入（只追加、不改写既有文字）。**没有整体替换的入参**：
    函数结构上不存在「用读图结果换掉描述」的路径（AC4）。
    """
    text = str(image_prompt)
    for adj in adjudications:
        if adj.decision == DECISION_KEEP or adj.decision == DECISION_REGENERATE:
            continue
        quote = adj.deviation.prompt_quote
        pos = locate_rewrite_span(text, quote, name)
        text = text[:pos] + (adj.replacement or adj.deviation.observed) + text[pos + len(quote):]
    facts = [str(f) for f in additions if str(f).strip()]
    if facts:
        text = f"{text}；{'；'.join(facts)}"
    return text


def build_proofread_request(image_prompt: str, locked_features: Sequence[str] = ()) -> str:
    """读图校对请求文本（纯函数，返回字符串即契约）。

    **引文必须逐字摘录**是整条纠偏链的锚点：「按图改文案」只改 ``prompt_quote`` 那一处，
    引文是模型复述而非原文，就没有「那一处」可改（合并侧会抛错）。故请求里把这条
    钉成硬要求，而不是指望视觉模型自觉。

    **锁死清单条目要逐条摆出来**（issue #53）：只写一句「偏差只判身份特征」在信息上
    不可赢——模型得按字面知道是哪几条，它摘的引文才落得到条目上（同 issue #37 的教训：
    请求里不给词表，那句「使其通过校验」模型赢不了）。清单缺席时请求逐字与改动前一致，
    旧产物照旧走老口径（对照器那边同样不收窄，两处口径一致）。
    """
    lines = [
        "这是一张 AI 视频生成的参考素材图。请把它与下面的生图提示词逐项对照，"
        "找出两类事实，只输出一个 JSON 对象（不要 markdown 围栏）：",
        '1. 偏差：图中实际画面与提示词描述**不符**的每一处。每条给出 prompt_quote'
        "（从生图提示词里**逐字摘录**那句不符的原文，一字不差——后续只改这一处）"
        "和 observed（图中实际看到的样子）。",
        '2. 补漏：图中有、而提示词**没有写**的显著事实（外观细节/道具/文字等）。'
        "这类 prompt_quote 留空字符串，observed 写该事实。",
        "只报确定可见的差异，不要臆测；与提示词一致的不要报。",
    ]
    locked = [str(x).strip() for x in locked_features if str(x).strip()]
    if locked:
        lines.append(
            "【锁死清单】只有下列身份特征与图不符才算**偏差**；其余出入（构图/光线/"
            "口味级）属理想描述，不必报为偏差：")
        lines.extend(f"- {entry}" for entry in locked)
    lines.append(
        '{"claims": [{"aspect": "哪一处（如 裙色/道具/文字）", '
        '"prompt_quote": "逐字摘录或空串", "observed": "图中实际所见"}]}')
    lines.append(f"【生图提示词】\n{image_prompt}")
    return "\n".join(lines)


def render_proofread_report(report: ProofreadReport) -> str:
    """对照报告出站渲染（单写源：渲染文案只在这里写，将来 CLI 接线直接复用——
    两处各写一份渲染就是第二份可写来源，0008 单写源精神）。"""
    lines: list[str] = []
    if report.deviations:
        lines.append(f"[偏差] {len(report.deviations)} 处（描述与图不符）：")
        for item in report.deviations:
            lines.append(f"  - {item.aspect}：描述「{item.prompt_quote}」→ 图中「{item.observed}」")
    if report.omissions:
        lines.append(f"[补漏] {len(report.omissions)} 条（图里有而描述没有）：")
        for item in report.omissions:
            lines.append(f"  - {item.aspect}：{item.fact}")
    if report.exempted:
        # 只列条目标签、不逐条铺开：收窄的成果要看得见，但不该把屏面重新淹一遍
        # （逐条细节在 ``exempted`` 里，要查查它）。
        aspects = "、".join(item.aspect or "未标" for item in report.exempted)
        lines.append(f"[忽略] {len(report.exempted)} 条理想描述出入（不判偏差、不并入）：{aspects}")
    if not lines:
        lines.append("[读图校对] 无偏差、无补漏。")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 素材画面事实取材口：下游提示词写作按语义名取合并后的描述
# ---------------------------------------------------------------------------

PROOFREADS_KEY = "asset_proofreads"

# 校对记录状态：done = 本轮裁决齐、description 即合并后的素材画面事实；
# pending_regen = 有人裁了「重生成这张素材图」，图未换版前不放行。
STATUS_DONE = "done"
STATUS_PENDING_REGEN = "pending_regen"


@dataclass(frozen=True)
class ProofreadRecord:
    """一个语义名的读图校对记录（写进 ``state[PROOFREADS_KEY]``，随会话持久化）。

    ``description`` 是**素材画面事实**：生图提示词为底 + 已裁决的定点纠偏 + 并入的补漏。
    ``sha256`` 钉住校对针对的那版正本——换版后旧裁决自动失效、重新校对（LoRA
    可变文件名教训的同款防线）。
    """

    name: str
    sha256: str
    status: str
    description: str
    adjudications: tuple[Adjudication, ...] = ()
    omissions: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not str(self.name).strip():
            raise ValueError("校对记录缺语义名")
        if not str(self.sha256).strip():
            raise ValueError(f"校对记录缺 sha256（{self.name!r}）")
        if self.status not in (STATUS_DONE, STATUS_PENDING_REGEN):
            raise ValueError(f"无效校对状态：{self.status!r}")

    def to_dict(self) -> dict:
        return {
            "sha256": self.sha256, "status": self.status, "description": self.description,
            "deviations": [a.to_dict() for a in self.adjudications],
            "omissions": list(self.omissions),
        }

    @classmethod
    def from_dict(cls, name: str, raw: object) -> "ProofreadRecord":
        if not isinstance(raw, dict):
            raise ValueError(f"校对记录必须是对象（{name!r}）：{raw!r}")
        unknown = set(raw) - {"sha256", "status", "description", "deviations", "omissions"}
        if unknown:
            raise ValueError(
                f"校对记录（{name!r}）包含未知字段：{', '.join(sorted(unknown))}")
        return cls(
            name=name,
            sha256=str(raw.get("sha256", "")),
            status=str(raw.get("status", "")),
            description=str(raw.get("description", "")),
            adjudications=tuple(Adjudication.from_dict(x) for x in raw.get("deviations") or ()),
            omissions=tuple(str(x) for x in raw.get("omissions") or ()),
        )


# ---------------------------------------------------------------------------
# 续跑点：读图校对的中途进度（issue #51）
# ---------------------------------------------------------------------------

# 中途进度在 state 里的键（会话持久化白名单同此名）。它与 `asset_proofreads`（**落定**
# 的记录）是两件事：记录是产物，进度是「这道题问到哪了」——落定时进度即作废。
PROGRESS_KEY = "asset_proofread_progress"


@dataclass(frozen=True)
class ProofreadProgress:
    """一张资产读图校对的**中途**进度（issue #51 续跑点）。

    为什么连读图结果（``claims``）一起存：人裁的条目是**对着那一份清单**选的。
    续跑时若重新读图，视觉模型交回的偏差清单会换个样子，于是「已裁决的 3 条」既
    认不回来、也说不定已不存在——只存裁决不存清单，「已裁决项不重答」是句空话。
    故 ``sha256``（图未换版）与 ``base``（词未改写）三者对上才复用，否则整份作废
    重读：它记的是另一张图或另一版词，复用等于拿旧清单判新图。
    """

    name: str
    sha256: str
    base: str
    claims: tuple[Claim, ...] = ()
    decisions: tuple[Adjudication, ...] = ()
    # 补漏并入这个回答：None ＝ 还没问过（与「答了不并入」区分开——那是有效答案，
    # 续跑不该再问一遍）。
    omissions: bool | None = None

    def __post_init__(self) -> None:
        if not str(self.name).strip():
            raise ValueError("校对进度缺语义名")
        if not str(self.sha256).strip():
            raise ValueError(f"校对进度缺 sha256（{self.name!r}）")

    def reading(self) -> Reading:
        """把存下来的 claims 还原成读图结果（对照器输入），供复用。"""
        return Reading(claims=self.claims)

    def with_decision(self, adjudication: Adjudication) -> "ProofreadProgress":
        """追加一条已裁决条目（frozen：返回新对象，调用方写回 state 再落盘）。"""
        return replace(self, decisions=(*self.decisions, adjudication))

    def with_omissions(self, take: bool) -> "ProofreadProgress":
        return replace(self, omissions=bool(take))

    def to_dict(self) -> dict:
        return {
            "sha256": self.sha256,
            "base": self.base,
            "claims": [
                {"aspect": c.aspect, "prompt_quote": c.prompt_quote, "observed": c.observed}
                for c in self.claims
            ],
            "decisions": [a.to_dict() for a in self.decisions],
            "omissions": self.omissions,
        }

    @classmethod
    def from_dict(cls, name: str, raw: object) -> "ProofreadProgress":
        if not isinstance(raw, dict):
            raise ValueError(f"校对进度必须是对象（{name!r}）：{raw!r}")
        unknown = set(raw) - {"sha256", "base", "claims", "decisions", "omissions"}
        if unknown:
            raise ValueError(f"校对进度（{name!r}）包含未知字段：{', '.join(sorted(unknown))}")
        omissions = raw.get("omissions")
        if omissions is not None and not isinstance(omissions, bool):
            raise ValueError(
                f"校对进度（{name!r}）的补漏回答错形：{omissions!r}（应为 true/false/null）")
        return cls(
            name=name,
            sha256=str(raw.get("sha256", "")),
            base=str(raw.get("base", "")),
            claims=tuple(Claim.from_dict(x) for x in raw.get("claims") or ()),
            decisions=tuple(Adjudication.from_dict(x) for x in raw.get("decisions") or ()),
            omissions=omissions,
        )


def progress_rows(state: dict) -> dict:
    """state 里的校对进度（``{语义名: 进度}``）；键缺失／null → 空表。

    坏形状**响**（ValueError），口径同 ``proofread_rows``：falsy 错形不许被
    ``or {}`` 洗成空表，那等于「已有的裁决凭空消失、重问一遍」。

    返回的是**浅拷贝**（读用），与 ``resume_point.answer_book`` 返回活 dict 不同：
    进度只经 ``store_progress`` / ``clear_progress`` 写入，读侧改它不该生效——两处
    读口都自称「唯一读口」，写路径的差别写在这里而不是留给下一个人去猜。
    """
    raw = (state or {}).get(PROGRESS_KEY)
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError(f"校对进度字段错形：{raw!r}（应为 语义名→进度 的对象）")
    return dict(raw)


def load_progress(state: dict, name: str, *, sha256: str, base: str) -> ProofreadProgress | None:
    """取这张资产**可复用**的中途进度；图换版／词改写／没记录时返回 None。"""
    raw = progress_rows(state).get(str(name))
    if raw is None:
        return None
    progress = ProofreadProgress.from_dict(str(name), raw)
    if progress.sha256 != str(sha256) or progress.base != str(base):
        return None
    return progress


def store_progress(state: dict, progress: ProofreadProgress) -> None:
    """写回一张资产的中途进度（调用方随后落盘＝一个续跑点）。"""
    rows = state.setdefault(PROGRESS_KEY, {})
    if not isinstance(rows, dict):
        raise ValueError(f"校对进度字段错形：{rows!r}（应为 语义名→进度 的对象）")
    rows[progress.name] = progress.to_dict()


def clear_progress(state: dict, name: str) -> None:
    """作废一张资产的中途进度（裁决落定或作废时调：产物接它的班）。"""
    rows = state.get(PROGRESS_KEY)
    if isinstance(rows, dict):
        rows.pop(str(name), None)


def pop_decision(pool: list[Adjudication], deviation: Deviation) -> Adjudication | None:
    """从「上次已裁决」池里**取走**这一条偏差对应的裁决；三字段全等才认。

    ``pool`` 是可变的副本：取走保证同一条裁决不会被两条一模一样的偏差重复认领。
    三字段全等是刻意的严格判据——只比 aspect 会把「同一条偏差被补锚改写过引文」
    的两轮混为一谈。
    """
    target = (deviation.aspect, deviation.prompt_quote, deviation.observed)
    for index, adjudication in enumerate(pool):
        found = adjudication.deviation
        if (found.aspect, found.prompt_quote, found.observed) == target:
            pool.pop(index)
            return adjudication
    return None


def proofread_rows(state: dict) -> dict:
    """state 里的校对记录原始行（写账侧与取材口共用的形状口）：键缺失／null → 空表。

    坏形状**响**（ValueError），口径同 session_store 画布字段（issue #42 复审）：
    falsy 错形（``[]``、``""``、0…）不许被 ``or {}`` 洗成空表——读侧响、写账侧
    洗白就是「悄悄起新账」（评审：向导曾以 ``dict(x or {})`` 与本口径反着写），
    响错口只此一处，两侧共用。
    """
    raw = (state or {}).get(PROOFREADS_KEY)
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError(f"校对记录字段错形：{raw!r}（应为 语义名→记录 的对象）")
    return dict(raw)


def proofread_descriptions(state: dict) -> dict[str, str]:
    """state 里的校对记录 → ``{语义名: 素材画面事实}``（下游唯一取材口）。

    每条记录都收，**不按 status 过滤**：``pending_regen`` 的 description 是未改动的
    生图提示词底本——「重生成」等的是图换版、不是文（文是设计意图，正该给下游写
    视频提示词用）；status 管**图**放不放行，是向导闸门的账，不是取材口的过滤器。
    形状纪律在 ``proofread_rows``（响错口一处）。
    """
    out: dict[str, str] = {}
    for name, rec in proofread_rows(state).items():
        record = ProofreadRecord.from_dict(str(name), rec)
        if record.description.strip():
            out[record.name] = record.description
    return out


__all__ = [
    "DECISIONS", "DECISION_KEEP", "DECISION_LABELS", "DECISION_REGENERATE",
    "DECISION_REWRITE", "DEFAULT_DECISION", "PROGRESS_KEY", "PROOFREADS_KEY",
    "STATUS_DONE", "STATUS_PENDING_REGEN",
    "Adjudication", "Claim", "Deviation", "Omission", "ProofreadProgress",
    "ProofreadRecord", "ProofreadReport", "Reading",
    "build_proofread_request", "clear_progress", "compare_descriptions",
    "load_progress", "locate_rewrite_span", "merge_asset_description",
    "parse_reading", "pop_decision", "progress_rows", "proofread_descriptions",
    "proofread_rows", "render_proofread_report", "store_progress",
]

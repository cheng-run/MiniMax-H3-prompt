"""生图提示词纪律的读取口与警告级确定性检查（issue #68 / ADR 0018 / issue #87）。

**规则文本只有一份可写来源**：``prompts/image_prompt_disciplines/<模型>.md``，一档一份
（``z_image`` / ``qwen`` / ``moss``，issue #87 / spec #84 D7）。本模块只读取它们
（``discipline_text``），不复制正文——AI 改词（#72）也从同一个口取。两份文本就会各自
漂移，本仓吃过「同一句原文 3 份硬拷贝」的亏（issue #35）。

**为什么按模型分文件**：同一个模型写错纪律不会报错，只会少出东西——Z-Image Turbo 要
中文短句、全正向；Qwen-Image 2.1 要英文长文、不写负面；MOSS-Image 要英文并填负面标签。
以前只有 Z-Image 一档，纪律就写在角色 .md 末尾；三档并存后必须各占一份文件，否则
「换模型」与「换写法」只能一起改，改漏一处就是静默掉一档质量。

**只警告、不阻断**：Z-Image Turbo 的失败模式是「细节没画出来」，不是「输出不合规」。
把判据做成 error 闸门，会被合法写法误伤、把一份本来能出图的提示词停掉
（ADR 0018「考虑过的备选」）。检查在节点出词后打印，不改产物、不改历史会话。

**检查按模型分口径**：五类检查里除了「质量套话」（本仓通用的写法纪律）之外，其余四类
都以 CFG=1 与 Turbo 的弱项为据（ADR 0018），对 Qwen/MOSS 没有依据——拿它们去判一份
英文长文，会给出成片的假警告，把真问题淹掉。故非 Z-Image 档只跑通用那一类。

**为什么规则文本不落在本模块的常量里**：ADR 0018 明写纪律写进角色提示词，且角色 .md
是被逐字读进模型请求的那份文本。常量再抄一份就是第二个可写来源；读取口从 .md 现取，
改 .md 即改模型看到的与 #72 复用的那一份，不可能只改到一半。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from ..image_config import DEFAULT_IMAGE_MODEL, IMAGE_MODEL_KEYS, image_model_label

# 可加载纪律的模型键。**真源是 ``image_config.IMAGE_MODEL_KEYS``**（值域与配置文件同一处），
# 这里只做转发：两处各写一份时，新增一个模型会只改到一边（配置认它、纪律文件找不到）。
# 默认档 ``DEFAULT_IMAGE_MODEL`` 同样是转发（它住在模型词表那一处）。
DISCIPLINE_MODELS = IMAGE_MODEL_KEYS

# 纪律文件目录与标题前缀。标题前缀是「文件确实是一节纪律」的守卫判据——纪律文件若被
# 覆写成别的东西（空文件、正文片段），读取口要大声报，而不是把半截文本送进系统提示词。
DISCIPLINES_DIR = (
    Path(__file__).resolve().parents[1] / "prompts" / "image_prompt_disciplines"
)
DISCIPLINE_HEADING_PREFIX = "## 生图提示词纪律"

# 「长度约 600 字内」的上限。按**非空白字符**计数：中文一字算一字，空格与换行不计
# （排版的换行不该把提示词判成超长）。超了只警告——见模块头「只警告不阻断」。
MAX_PROMPT_CHARS = 600


@dataclass(frozen=True)
class DisciplineWarning:
    """一条纪律警告。``code`` 是稳定契约，``message`` 是给用户看的可行动文案。"""

    code: str
    message: str


def discipline_path(model: str = DEFAULT_IMAGE_MODEL) -> Path:
    """某一档的纪律文件路径；未知模型报错（别猜文件、也别回落到默认档）。"""
    if model not in DISCIPLINE_MODELS:
        raise ValueError(
            f"生图模型无效：{model!r}（只认 {', '.join(DISCIPLINE_MODELS)}）")
    return DISCIPLINES_DIR / f"{model}.md"


def discipline_text(model: str = DEFAULT_IMAGE_MODEL) -> str:
    """返回某一档的纪律小节全文（含标题）。

    找不到文件、或文件不以「## 生图提示词纪律」起头，都大声报错——静默返回空串会让
    AI 改词与守卫一起「看起来正常、其实没规则」；把半截文本送进系统提示词同样是
    「规则在、接线不在」的变体。
    """
    path = discipline_path(model)
    if not path.exists():
        raise RuntimeError(f"缺少生图提示词纪律文件：{path}（{image_model_label(model)} 档）")
    text = path.read_text(encoding="utf-8").strip()
    if not text.startswith(DISCIPLINE_HEADING_PREFIX):
        raise RuntimeError(
            f"{path} 不是一节生图提示词纪律（应以「{DISCIPLINE_HEADING_PREFIX}」起头）")
    return text


# --- 五类检查 -----------------------------------------------------------------
#
# 每类只报**一条**警告：用户要的是「这条提示词犯了哪几类」，不是逐词清单。
# 一条里出现两次同类违规也只报第一条命中，避免刷屏把真正要改的类别淹掉。

# 负向表述。中文**不认裸「不」**：``不同深浅`` / ``不断`` 是合法描述，裸判会让几乎每条
# 中文提示词都报警（警告失去信息量）。只认明确的否定指令与否定状态。
_NEGATIVE_CN = re.compile(
    r"不要|不能|不得|不许|不准|禁止|严禁|避免|切勿|请勿|杜绝|去除|去掉|消除|"
    r"没有|不含|不带|而不是|不是|无需|无须|不必|"
    r"无(?:人|云|雨|雾|遮挡|表情|装饰|文字|水印|阴影|瑕疵|杂物)"
)
_NEGATIVE_EN = re.compile(
    r"\b(?:no|not|never|without|avoid|avoiding|exclude|excluding|free of|devoid of|"
    r"don't|doesn't|isn't|aren't|rather than|instead of)\b",
    re.IGNORECASE,
)

# 权重语法：``(词:1.2)`` / ``[词:0.9]``（含多层括号的内层）。ComfyUI 的 z_image 编码器
# ``disable_weights=True``，括号与权重不生效、原样进提示词（ADR 0018 第 3 条事实）。
_WEIGHT_PAREN = re.compile(r"[（(]\s*[^（）()\n]{1,40}?\s*[:：]\s*\d+(?:\.\d+)?\s*[)）]")
_WEIGHT_BRACKET = re.compile(r"[\[［]\s*[^\[\]【】\n]{1,40}?\s*[:：]\s*\d+(?:\.\d+)?\s*[\]］]")

# 质量套话：官方 PE 模板明令禁止的元标签。只收「画不出东西」的标签，不碰
# 「柔和侧光」这类真正描述画面的词。
_QUALITY_EN = re.compile(
    r"\b(?:masterpiece|best quality|highest quality|high quality|ultra[ -]?detailed|"
    r"highly detailed|ultra[ -]?high definition|uhd|hdr|highres|high resolution|"
    r"8k|4k|16k|award[ -]winning|trending on artstation|octane render|unreal engine)\b",
    re.IGNORECASE,
)
_QUALITY_CN = re.compile(
    r"杰作|大师作品|最佳画质|最高画质|最佳质量|超高清|高清|高分辨率|4K|8K|"
    r"极致细节|超精细|获奖作品|画质极佳"
)

# 方位词。**最长优先**匹配：``左上方`` 不能被拆成 ``左`` + ``上方`` 两次判。
_CN_DIRECTIONS = (
    "左上方", "右上方", "左下方", "右下方", "正上方", "正下方",
    "左上角", "右上角", "左下角", "右下角",
    "左侧", "右侧", "左边", "右边", "上方", "下方", "前面", "后面",
    "前方", "后方", "旁边", "中间", "中央", "角落", "前景", "背景",
    "顶部", "底部", "正面", "背面", "内侧", "外侧",
)
_CN_DIRECTION_RE = re.compile(
    "|".join(sorted((re.escape(word) for word in _CN_DIRECTIONS), key=len, reverse=True))
)

# 往方位词前面回溯时跳过的字：语法助词、介词、方位后缀与常见动词。
# 为什么跳过它们而不是直接看前一个字：``桌子的左侧`` 的锚点是「桌子」，中间隔着「的」；
# ``站在左侧`` 回溯到「站」仍**不是**锚点——站/坐/放这些是动作，不是参照物。
_CN_ANCHOR_SKIP = set(
    "的地得在于向往从到是有着了这那其与和及或而也都就还又则最很更再便才已"
    "将把被让给对为以时处里旁边侧面方上下前后左右中内外"
    "站坐躺放挂摆立停走来去看出现位于"
)

# 英文方位。两类分开判：
# - 带 the 的位置名词（``on the left``）要求后面跟 ``of + 名词`` 才算有锚点；
# - 介词式方位（``above`` / ``next to``）要求后面直接有介词宾语。
_EN_POSITIONAL = re.compile(
    r"\b(?:on|to|at|in)\s+the\s+(?:far\s+)?"
    r"(left|right|top|bottom|centre|center|foreground|background)\b",
    re.IGNORECASE,
)
_EN_POSITIONAL_ANCHOR = re.compile(r"\bof\b\s+\S", re.IGNORECASE)
_EN_PREPOSITIONAL = re.compile(
    r"\b(?:above|below|behind|beside|next\s+to|in\s+front\s+of|between)\b",
    re.IGNORECASE,
)
_EN_OBJECT = re.compile(
    r"^\s+(?:the\s+|a\s+|an\s+|his\s+|her\s+|their\s+|its\s+)?[A-Za-z]",
    re.IGNORECASE,
)


def _first(regex: re.Pattern[str], text: str) -> str | None:
    found = regex.search(text)
    return found.group(0).strip() if found else None


def _negative_hit(text: str) -> str | None:
    return _first(_NEGATIVE_CN, text) or _first(_NEGATIVE_EN, text)


def _weight_hit(text: str) -> str | None:
    return _first(_WEIGHT_PAREN, text) or _first(_WEIGHT_BRACKET, text)


def _cliche_hit(text: str) -> str | None:
    return _first(_QUALITY_EN, text) or _first(_QUALITY_CN, text)


def _cliche_warning(hit: str) -> DisciplineWarning:
    """质量套话警告的**唯一**构造处：Z-Image 档与其余档共用同一句文案。"""
    return DisciplineWarning(
        "QUALITY_CLICHE",
        f"出现质量套话「{hit}」——masterpiece／8K 这类元标签不描述画面，"
        "官方生图提示词模板也明令禁止；请删掉。",
    )


def _cn_direction_has_anchor(text: str, start: int) -> bool:
    """从方位词往前回溯：跳过助词/介词/动词后，遇到实词才算有锚点。

    这是**启发式**，不是句法分析：本仓纪律是警告级、可观察后再收紧
    （ADR 0018 拒绝把它做成 error 闸门）。边界写在 ``_CN_ANCHOR_SKIP`` 的注释里。
    """
    index = start - 1
    while index >= 0:
        char = text[index]
        if char.isspace() or char in "，。、；：！？,.;:!?（）()【】[]“”\"'":
            return False
        if char in _CN_ANCHOR_SKIP:
            index -= 1
            continue
        return True
    return False


def _unanchored_direction(text: str) -> str | None:
    """找第一个没有参照物的方位词；都在锚点上返回 None。"""
    for found in _CN_DIRECTION_RE.finditer(text):
        if not _cn_direction_has_anchor(text, found.start()):
            return found.group(0)

    for found in _EN_POSITIONAL.finditer(text):
        tail = text[found.end(): found.end() + 40]
        if not _EN_POSITIONAL_ANCHOR.search(tail):
            return found.group(0).strip()

    for found in _EN_PREPOSITIONAL.finditer(text):
        tail = text[found.end(): found.end() + 40]
        # 后面到句读之前必须有宾语；``above,`` / ``behind.`` 这种就是裸方位。
        object_text = re.split(r"[.;:,!?（(]", tail, maxsplit=1)[0]
        if not _EN_OBJECT.search(object_text):
            return found.group(0).strip()
    return None


def check_image_prompt(prompt: str, *, model: str = DEFAULT_IMAGE_MODEL
                       ) -> list[DisciplineWarning]:
    """对一条 ``positive_prompt`` 跑警告级检查（纯函数，不改入参）。

    返回顺序固定（负向、权重、套话、超长、方位），每类最多一条。空文本不报：
    空提示词由上游的 ``_normalize_asset_entries`` 报错，不归这里管。

    **只有 Z-Image 档跑满五类**：负向、权重、超长、方位四类的依据都是 Turbo 自己的
    事实（CFG=1、编码器不认权重、长文稀释注意力、空间关系是它的弱项，见 ADR 0018）；
    拿它们判 Qwen 的英文长文会冒出成片假警告。其余档只跑「质量套话」——那是本仓通用
    的写法纪律（「四条硬约束」第 2 条），与模型无关。
    """
    # 值域先验：放在空文本早退之前——否则 `check_image_prompt("", model="flux")` 会
    # 静默返回空表，看起来「这个模型没违规」，实际是从没认过这个模型。
    if model not in DISCIPLINE_MODELS:
        raise ValueError(
            f"生图模型无效：{model!r}（只认 {', '.join(DISCIPLINE_MODELS)}）")

    text = str(prompt or "")
    if not text.strip():
        return []

    warnings: list[DisciplineWarning] = []

    if model != DEFAULT_IMAGE_MODEL:
        # 这一档只有「质量套话」一类：文案与下面 Z-Image 档用的是**同一个构造函数**，
        # 两处各写一份时改一处漏一处，两个模型会得到不一样的措辞（本仓「同一契约不许
        # 两处口径」）。
        if hit := _cliche_hit(text):
            warnings.append(_cliche_warning(hit))
        return warnings

    if hit := _negative_hit(text):
        warnings.append(DisciplineWarning(
            "NEGATIVE_STATEMENT",
            f"出现负向表述「{hit}」——Z-Image Turbo 没有 CFG 通路，负向词是空动作，"
            "还会被当正向词读；请改写成正向可见的描述。",
        ))

    if hit := _weight_hit(text):
        warnings.append(DisciplineWarning(
            "WEIGHT_SYNTAX",
            f"出现权重语法「{hit}」——ComfyUI 链路不认权重，括号与数字会原样进提示词；"
            "请删掉括号与权重，只留词语本身。",
        ))

    if hit := _cliche_hit(text):
        warnings.append(_cliche_warning(hit))

    visible = len(re.sub(r"\s+", "", text))
    if visible > MAX_PROMPT_CHARS:
        warnings.append(DisciplineWarning(
            "PROMPT_TOO_LONG",
            f"正文共 {visible} 字，超过约 {MAX_PROMPT_CHARS} 字上限——ComfyUI 不截断，"
            "但长文会稀释注意力；请删掉次要细节。",
        ))

    if hit := _unanchored_direction(text):
        warnings.append(DisciplineWarning(
            "SPATIAL_NO_ANCHOR",
            f"方位词「{hit}」没有参照物——空间关系要写成「锚点＋方位＋距离」，"
            "如「桌子的左侧，距桌沿约一掌宽」。",
        ))

    return warnings

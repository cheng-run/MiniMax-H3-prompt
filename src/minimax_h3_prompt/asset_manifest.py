"""设计端资产清单的解析与名字集合比较（issue #173 / spec #166 D）。

三位设计师（人物／场景／道具）在回复末尾附一个机器可读块：

    ```asset-manifest
    {"assets": ["青年男人｜综合板", "老妇人｜综合板"]}
    ```

粒度是**张**——一个人两张板写两个名字，本类没有资产写 ``{"assets": []}``。正文里的
「母版清单」保留给人读，机器口径只认这个块（spec D 的原话）。

为什么单独成模块：解析与集合比较都是纯文本逻辑，住在 ``graph/nodes.py`` 里会与
「节点怎么取 agent、怎么打桩」纠缠在一起；这里只依赖标准库，节点与用例都从同一处取，
不会出现「设计端解析一份、出词端又按另一份口径比」的第二套真相。
"""
from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum

# 围栏语言标签：设计端提示词（``graph/nodes.py`` 的请求与重问）与解析器共用这一个
# 常量。两边各写一份字面量时，改一处只有一边跟着动——模型照新标签回、解析器还找旧
# 标签，清单块被当正文吞掉且不报错（CODING_STANDARDS §四：守卫形态从常量派生）。
MANIFEST_FENCE = "asset-manifest"

# 正则从常量派生而非重抄字面量：``re.escape`` 保证标签里的正则元字符（将来的 ``:``、``.``）
# 不会被当成通配；夹具同样引用 ``MANIFEST_FENCE``，改常量时提示、解析与用例一起动。
_BLOCK_RE = re.compile(
    r"```" + re.escape(MANIFEST_FENCE) + r"[^\n]*\n(.*?)```", re.DOTALL)


class ManifestProblem(str, Enum):
    """清单块不可解析的具体原因（issue #178）。

    为什么要分而不是统一返回 ``None``：调用方的补救方式不同——块缺失／不是 JSON／
    形状不对，重问一句「照格式重写」就够；空名与**重复名**必须点名，否则模型收到
    笼统提示，很可能把同一份坏清单原样再写一遍，一次重问白花。
    """

    BLOCK_MISSING = "block_missing"
    NOT_JSON = "not_json"
    SHAPE = "shape"
    EMPTY_NAME = "empty_name"
    DUPLICATE_NAME = "duplicate_name"


@dataclass(frozen=True)
class ManifestSplit:
    """``split_manifest_reply`` 的结果：``names`` 有值＝解析成功，否则看 ``problem``。

    ``duplicates`` 只在 ``DUPLICATE_NAME`` 时有值，且要由调用方**原样点名**出去
    （重问附言与最终报错都点名）——这是 #178 定案「重复名由解析器独占判定」的落点：
    合并处守卫只判跨类重名，类内重复不存在第二条判定路径。
    """

    body: str
    names: list[str] | None
    problem: ManifestProblem | None = None
    duplicates: tuple[str, ...] = ()


def split_manifest_reply(reply: str) -> ManifestSplit:
    """把设计师回复切成 ``(正文, 清单)``；拿不到合法清单时 ``names`` 为 ``None``。

    ``None`` 的几种来源见 ``ManifestProblem``；调用方据此**重问一次**，仍失败才报错
    （spec #166 D）。重复名字也算无效：清单是「张」的枚举，同一个名字出现两次就说不出
    这是几张，放着它过会让「一个人两张板」实际退化成一张。判定留在这里、名字带出去，
    是因为解析器一次只看见一份清单——类内重复只有这一处看得见（#178）。
    """
    match = _BLOCK_RE.search(reply)
    if match is None:
        return ManifestSplit(reply, None, ManifestProblem.BLOCK_MISSING)
    try:
        payload = json.loads(match.group(1))
    except json.JSONDecodeError:
        return ManifestSplit(reply, None, ManifestProblem.NOT_JSON)
    if not isinstance(payload, dict):
        return ManifestSplit(reply, None, ManifestProblem.SHAPE)
    assets = payload.get("assets")
    if not isinstance(assets, list) or not all(isinstance(name, str) for name in assets):
        return ManifestSplit(reply, None, ManifestProblem.SHAPE)
    names = [name.strip() for name in assets]
    if any(not name for name in names):
        return ManifestSplit(reply, None, ManifestProblem.EMPTY_NAME)
    # sorted 只为报错顺序稳定（集合本身无序）：用户按报错改清单时，两次跑出来的顺序
    # 不该忽前忽后。
    duplicates = tuple(sorted({name for name in names if names.count(name) > 1}))
    if duplicates:
        return ManifestSplit(
            reply, None, ManifestProblem.DUPLICATE_NAME, duplicates)
    # 块从正文里摘掉：下游写词 agent 拿整段设计当设定，围栏与 JSON 出现在设定里，
    # 模型会照着「asset-manifest」这个词去写画面。
    body = (reply[:match.start()] + reply[match.end():]).strip()
    return ManifestSplit(body, names)


def name_set_mismatch(manifest: Sequence[str], produced: Sequence[str]) -> str:
    """返回人读的差异描述；名字集合相等且无重复时返回空串。

    多、少、换名三种形态都要点名：换名在集合上表现为「多一个 + 少一个」，
    只报其中一边，人还得自己猜另一边的旧名字是哪条。
    """
    expected = set(manifest)
    actual = set(produced)
    extra = [name for name in produced if name not in expected]
    missing = [name for name in manifest if name not in actual]
    duplicates = sorted({name for name in produced if produced.count(name) > 1})
    parts: list[str] = []
    if extra:
        parts.append(f"多出 {extra}")
    if missing:
        parts.append(f"缺少 {missing}")
    if duplicates:
        parts.append(f"重复 {duplicates}")
    return "、".join(parts)


def duplicate_asset_names(
    manifests: Mapping[str, Sequence[str]],
) -> list[tuple[str, tuple[str, ...]]]:
    """找出**跨类**重名的语义名，返回 ``[(名字, (涉及的类别…))]``（issue #175）。

    ``manifests`` 是「类别名 → 该类的名字序列」，类别名只用于报错措辞（人物／场景／道具）。
    一个名字出现在**两个及以上类别**里才是跨类重名，涉及的类别按遇到顺序记一次；
    同一类别里出现两次（类内重复）**不在这里判**——解析器 ``split_manifest_reply``
    拿到单份清单时就判「不可解析」并点名重问（#178 定案）。在这里再判一次，那条分支
    在真实路径上永远走不到，只有直造 patch 的用例能点亮它（「永不为红＝没有」）。

    为什么是这个分工：解析器一次只看见一类，类内重复它看得见；跨类重名要到三份清单
    合并后才存在，只有合并处看得见。各判各的那一半，谁都不是摆设。

    名字按首次出现顺序返回：报错顺序稳定，测试与人工排查都不必依赖字典的偶然顺序。
    """
    categories: dict[str, list[str]] = {}
    for category, names in manifests.items():
        for name in names:
            found = categories.setdefault(str(name), [])
            # 同一类别记一次：类内重复交给解析器，这里只关心「跨了几个类别」。
            if category not in found:
                found.append(category)
    return [
        (name, tuple(found))
        for name, found in categories.items()
        if len(found) > 1
    ]

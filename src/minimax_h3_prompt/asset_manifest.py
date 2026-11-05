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

# 围栏语言标签。设计端提示词与解析器都写这个字面量，改一处就要改另一处——
# 用例 ``test_design_node_*`` 通过「重问请求里点名这个标签」把两边钉在一起。
MANIFEST_FENCE = "asset-manifest"

_BLOCK_RE = re.compile(r"```asset-manifest[^\n]*\n(.*?)```", re.DOTALL)


def split_manifest_reply(reply: str) -> tuple[str, list[str] | None]:
    """把设计师回复切成 ``(正文, 清单)``；拿不到合法清单时返回 ``(原文, None)``。

    ``None`` 的几种来源：块缺失、块内不是 JSON、形状不是 ``{"assets": [str, ...]}``、
    名字是空串或重复。调用方据此**重问一次**，仍失败才报错（spec #166 D）。
    重复名字也算无效：清单是「张」的枚举，同一个名字出现两次就说不出这是几张，
    放着它过会让「一个人两张板」实际退化成一张。
    """
    match = _BLOCK_RE.search(reply)
    if match is None:
        return reply, None
    try:
        payload = json.loads(match.group(1))
    except json.JSONDecodeError:
        return reply, None
    if not isinstance(payload, dict):
        return reply, None
    assets = payload.get("assets")
    if not isinstance(assets, list) or not all(isinstance(name, str) for name in assets):
        return reply, None
    names = [name.strip() for name in assets]
    if any(not name for name in names) or len(set(names)) != len(names):
        return reply, None
    # 块从正文里摘掉：下游写词 agent 拿整段设计当设定，围栏与 JSON 出现在设定里，
    # 模型会照着「asset-manifest」这个词去写画面。
    body = (reply[:match.start()] + reply[match.end():]).strip()
    return body, names


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
    """找出跨类与类内重复的语义名，返回 ``[(名字, 涉及的类别…)]``（issue #175）。

    ``manifests`` 是「类别名 → 该类的名字序列」，类别名只用于报错措辞（人物／场景／道具）。
    一个名字出现在两个类别里是**跨类重名**；同一类别里出现两次是**类内重复**——返回值
    里两者都是「出现次数 > 1」，调用方按涉及的类别是否相同分开措辞，判据只有这一份。

    为什么不在 ``split_manifest_reply`` 里判类内重复就完事：那份解析器一次只看见一类，
    跨类重名要到三份清单合并后才存在。拆成两处判就会出现「类内拦了、跨类漏了」的
    半截守卫——而漏掉的那一半正是要等到交回第一张图才在台账入库时炸的那条路。

    名字按首次出现顺序返回、类别按遇到顺序记：报错顺序稳定，测试与人工排查都不必
    依赖字典的偶然顺序。
    """
    categories: dict[str, list[str]] = {}
    for category, names in manifests.items():
        for name in names:
            categories.setdefault(str(name), []).append(category)
    return [
        (name, tuple(found))
        for name, found in categories.items()
        if len(found) > 1
    ]

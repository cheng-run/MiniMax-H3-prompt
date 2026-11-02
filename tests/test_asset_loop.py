"""素材队列排序纯函数（issue #66）：纯 ``<Picture N>`` 升序＝剧本出场序＝接线序。

issue #50 的队列是**类型优先**（人物→场景→道具），组内才按编号——用户按菜单顺序往
工作流槽位里填图会填错槽（issue #64 问题 2 的实测现场）。issue #66 改成纯编号升序：
编号本身就是剧本出场序，也正是注入卡与工作流槽位的接线序，三者对齐后「按菜单顺序
填图」才等于「按槽位顺序填图」。

本文件只测**纯函数与卡文本**这两条外部契约；向导里的菜单/跳号/单张不问由
``test_wizard_asset_loop.py`` 钉。
"""
from __future__ import annotations

from minimax_h3_prompt.asset_ledger import KIND_PICTURE, AssetLedger, AssetRecord
from minimax_h3_prompt.asset_loop import (
    ASSET_ALIGN_ROLE,
    ASSET_REWRITE_ROLE,
    align_asset_prompt,
    asset_queue,
    rewrite_asset_prompt,
)
from minimax_h3_prompt.injection_card import VideoOutputParams, render_injection_card
from minimax_h3_prompt.observability import reporter, token_meter

# 四张资产：剧本里的出场序是 红裙女孩(1) → 秋日庭院(2) → 青铜钥匙(3) → 蓝衫少年(4)。
# 入参故意按「类型分组」的形态给（人物两张挤在一起、道具/场景各一张），
# 这样「类型优先」的实现会排出 1,4,2,3，只有纯编号序才排得出 1,2,3,4。
_ENTRIES = {
    "character": [{"name": "蓝衫少年"}, {"name": "红裙女孩"}],
    "prop": [{"name": "青铜钥匙"}],
    "scene": [{"name": "秋日庭院"}],
}
_PICTURES = {"红裙女孩": 1, "秋日庭院": 2, "青铜钥匙": 3, "蓝衫少年": 4}


def test_asset_queue_orders_by_picture_number_not_by_kind():
    """队列顺序严格按 ``<Picture N>`` 升序：不再让「人物」整体插到「场景」前面。"""
    queue = asset_queue(_ENTRIES, pictures=_PICTURES)
    assert [item.name for item in queue] == ["红裙女孩", "秋日庭院", "青铜钥匙", "蓝衫少年"]
    assert [item.picture for item in queue] == [1, 2, 3, 4]


def test_asset_queue_keeps_gapped_numbers_in_numeric_order():
    """跳号场景：编号有洞不影响升序，且被跳过的号一张都不丢。"""
    pictures = {"红裙女孩": 1, "秋日庭院": 5, "青铜钥匙": 9, "蓝衫少年": 12}
    queue = asset_queue(_ENTRIES, pictures=pictures)
    assert [item.picture for item in queue] == [1, 5, 9, 12]
    assert {item.name for item in queue} == set(pictures)


def test_asset_queue_order_matches_injection_card_order():
    """队列顺序与注入卡逐行顺序一致（AC「队列顺序＝注入卡顺序」）。

    注入卡按台账序渲染，台账按（类型, 编号）排；这里素材全是图，故卡序＝编号升序。
    只比名字序列，不比卡文本格式——卡怎么排版不是这条契约管的。
    """
    ledger = AssetLedger(entries=tuple(
        AssetRecord(
            name=name,
            kind=KIND_PICTURE,
            number=number,
            path=f"assets/picture-{number:02d}.png",
            sha256="a" * 64,
        )
        for name, number in reversed(list(_PICTURES.items()))
    ))
    card = render_injection_card(ledger, VideoOutputParams())
    card_names = [
        line.split("|")[2].strip()
        for line in card.splitlines()
        if line.startswith("| ref_image_")
    ]
    queue_names = [item.name for item in asset_queue(_ENTRIES, pictures=_PICTURES)]
    assert card_names == queue_names


# ---------------------------------------------------------------------------
# 素材改写与回灌走直调看门狗（issue #71 / ADR 0019）
#
# 这两处原先裸调 ``llm.invoke``：没有首字节事件、没有卡住重试、失败只留一句异常。
# 断言落在**事件序列**与**成本表角色**上——改回裸 invoke 时这里既收不到 agent_start，
# 也收不到 first_byte（假模型的 invoke 只有在 config 里拿到回调时才打得出首字节）。
# ---------------------------------------------------------------------------

class _StreamingStub:
    """最小直调假模型：``invoke(request, config=...)``，首字节喂 config 里的回调。"""

    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.requests: list[str] = []
        self.configs: list[object] = []

    def invoke(self, request, config=None):
        self.requests.append(str(request))
        self.configs.append(config)
        for callback in (config or {}).get("callbacks") or []:
            callback.on_llm_new_token("…")
        return self.reply


def _collect_events():
    got: list[dict] = []
    reporter.subscribe(got.append)
    return got, got.append


def test_rewrite_goes_through_the_direct_call_watchdog():
    """改写：事件序列（start → first_byte → done）与成本表角色都必须是统一入口的。"""
    stub = _StreamingStub("改后的提示词")
    before = token_meter.snapshot().get(ASSET_REWRITE_ROLE, {"calls": 0})["calls"]
    events, unsubscribe = _collect_events()
    try:
        new = rewrite_asset_prompt("原词", "改成宝蓝色", stub)
    finally:
        reporter.unsubscribe(unsubscribe)

    assert new == "改后的提示词"
    roles = [e.get("role") for e in events]
    assert ASSET_REWRITE_ROLE in roles, "改写没走直调入口（没有角色事件）"
    types = [e.get("type") for e in events if e.get("role") == ASSET_REWRITE_ROLE]
    assert "agent_start" in types and "agent_done" in types, f"改写事件不成对：{types}"
    states = [e.get("state") for e in events
              if e.get("type") == "agent_wait" and e.get("role") == ASSET_REWRITE_ROLE]
    assert "first_byte" in states, "改写没有首字节心跳（裸 invoke 的形态）"
    assert stub.configs and all((c or {}).get("callbacks") for c in stub.configs), \
        "模型没收到回调——参数没经直调入口递下去"
    after = token_meter.snapshot()[ASSET_REWRITE_ROLE]["calls"]
    assert after == before + 1, "改写的 token 没进成本表（直调入口的记账口没走到）"


def test_align_goes_through_the_direct_call_watchdog():
    """回灌：同一形状，角色单列成 ``role_asset_align``（成本表才看得出花在哪）。"""
    stub = _StreamingStub("对齐后的提示词")
    before = token_meter.snapshot().get(ASSET_ALIGN_ROLE, {"calls": 0})["calls"]
    events, unsubscribe = _collect_events()
    try:
        new = align_asset_prompt("原词", [("红裙女孩", "宝蓝色长裙")], stub)
    finally:
        reporter.unsubscribe(unsubscribe)

    assert new == "对齐后的提示词"
    roles = [e.get("role") for e in events]
    assert ASSET_ALIGN_ROLE in roles, "回灌没走直调入口（没有角色事件）"
    states = [e.get("state") for e in events
              if e.get("type") == "agent_wait" and e.get("role") == ASSET_ALIGN_ROLE]
    assert "first_byte" in states, "回灌没有首字节心跳（裸 invoke 的形态）"
    after = token_meter.snapshot()[ASSET_ALIGN_ROLE]["calls"]
    assert after == before + 1, "回灌的 token 没进成本表"


def test_direct_call_failure_keeps_the_value_error_contract():
    """失败仍是 ValueError（调用方按它回菜单/保持原词），文案带统一入口的病因。"""
    class _Broken:
        def invoke(self, request, config=None):
            raise RuntimeError("网关拒绝")

    try:
        rewrite_asset_prompt("原词", "意见", _Broken())
    except ValueError as exc:
        assert "网关拒绝" in str(exc)
    else:  # pragma: no cover - 走到这里就是契约破了
        raise AssertionError("改写失败没有抛 ValueError")

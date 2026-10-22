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
from minimax_h3_prompt.asset_loop import asset_queue
from minimax_h3_prompt.injection_card import VideoOutputParams, render_injection_card

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

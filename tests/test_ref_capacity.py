"""参考素材的官方容量上限（issue #86 / spec #84 D4）。

官方 API 的单次请求口径是 **≤9 图 + ≤3 视频 + ≤3 音频、总文件 ≤12**（spec #84 事实
底账）。本仓把它当**硬上限**而不是建议值执行：超限要**报错并点名**，绝不静默截断、
绝不自动丢弃——被截掉的素材在提示词里就成了悬空引用，用户会在 ComfyUI 里跑到一半
（甚至跑了几个小时）才发现。

本文件钉四件外部行为，并且**四条彼此独立**（去掉任一条上限校验，对应那条当场红）：

1. 图 > 9 → 报错并点名超出的素材；
2. 视频 > 3 → 同上；
3. 音频 > 3 → 同上；
4. 单型都在线内但**总文件 > 12** 也要报（9 图 + 3 视频已经用满 12 的预算）。

另外钉「报错说的是人话」：没有素材名时点名标签，有名字时名字与标签都给。
"""
from __future__ import annotations

import pytest

from minimax_h3_prompt.asset_ledger import (
    KIND_AUDIO,
    KIND_PICTURE,
    KIND_VIDEO,
    AssetLedger,
    AssetRecord,
)
from minimax_h3_prompt.ref_capacity import (
    MAX_AUDIOS,
    MAX_FILES,
    MAX_PICTURES,
    MAX_VIDEOS,
    ReferenceCapacityError,
    ReferenceItem,
    check_film_capacity,
    check_reference_capacity,
    items_from_ledger,
)


def _record(name: str, kind: str, number: int) -> AssetRecord:
    return AssetRecord(name=name, kind=kind, number=number,
                       path=f"assets/{kind}-{number:02d}", sha256="0" * 64)


def _items(pictures: int = 0, videos: int = 0, audios: int = 0) -> list[ReferenceItem]:
    return ([ReferenceItem(KIND_PICTURE, n, f"图素材{n}") for n in range(1, pictures + 1)]
            + [ReferenceItem(KIND_VIDEO, n, f"视频素材{n}") for n in range(1, videos + 1)]
            + [ReferenceItem(KIND_AUDIO, n, f"音频素材{n}") for n in range(1, audios + 1)])


# 上限值本身就是票面真源（spec #84「事实底账」）：改常量必须同步改票面，
# 所以这里用字面量而不是从实现抄一份。
def test_the_official_caps_are_the_ticket_numbers():
    assert (MAX_PICTURES, MAX_VIDEOS, MAX_AUDIOS, MAX_FILES) == (9, 3, 3, 12)


def test_exactly_at_the_caps_passes():
    """9 图 + 3 视频 = 12 文件：官方允许的满配，不许误报。"""
    check_reference_capacity(_items(pictures=9, videos=3))


def test_ten_pictures_are_rejected_and_the_offender_is_named():
    """图 > 9 当场停，并点名第 10 张（「哪几张超了」是用户唯一的行动信息）。"""
    with pytest.raises(ReferenceCapacityError) as excinfo:
        check_reference_capacity(_items(pictures=10))
    message = str(excinfo.value)
    assert "9" in message and "10" in message
    assert "<Picture 10>" in message and "图素材10" in message
    assert "不截断" in message


def test_four_videos_are_rejected_and_the_offender_is_named():
    """视频 > 3 单独成立（图在线上，只有视频超）。"""
    with pytest.raises(ReferenceCapacityError) as excinfo:
        check_reference_capacity(_items(pictures=2, videos=4))
    message = str(excinfo.value)
    assert "<Video 4>" in message and "视频素材4" in message


def test_four_audios_are_rejected_and_the_offender_is_named():
    """音频 > 3 单独成立。"""
    with pytest.raises(ReferenceCapacityError) as excinfo:
        check_reference_capacity(_items(pictures=1, audios=4))
    message = str(excinfo.value)
    assert "<Audio 4>" in message and "音频素材4" in message


def test_total_file_budget_is_checked_even_when_every_kind_is_within_its_own_cap():
    """单型全在线内、总文件超 12 也要停：9 图 + 3 视频已经用满 12 的预算。

    这条是「12 文件」判据的唯一证据——去掉总文件校验后，三型各自都不超，
    只有它也红不了别处。
    """
    with pytest.raises(ReferenceCapacityError) as excinfo:
        check_reference_capacity(_items(pictures=9, videos=3, audios=1))
    message = str(excinfo.value)
    assert "12" in message and "13" in message
    assert "总文件" in message


def test_the_error_names_the_call_site():
    """报错带出处（整片注入卡 / ≤15 秒直出）：同一个上限在两个环节咬，用户要知道卡在哪。"""
    with pytest.raises(ReferenceCapacityError) as excinfo:
        check_reference_capacity(_items(pictures=10), where="整片注入卡")
    assert "整片注入卡" in str(excinfo.value)


def test_items_from_ledger_reads_names_and_numbers():
    """台账 → 校验条目：名字与标签都要带过去（报错要能指着素材说）。"""
    ledger = AssetLedger(entries=(
        _record("红裙女孩", KIND_PICTURE, 1),
        _record("运镜参考", KIND_VIDEO, 1),
        _record("红裙女孩音色", KIND_AUDIO, 1),
    ))
    items = items_from_ledger(ledger)
    assert [(item.kind, item.number, item.name) for item in items] == [
        (KIND_PICTURE, 1, "红裙女孩"),
        (KIND_VIDEO, 1, "运镜参考"),
        (KIND_AUDIO, 1, "红裙女孩音色"),
    ]


def _ledger(pictures: int = 0, videos: int = 0, audios: int = 0) -> AssetLedger:
    records = (
        tuple(_record(f"图素材{n}", KIND_PICTURE, n) for n in range(1, pictures + 1))
        + tuple(_record(f"视频素材{n}", KIND_VIDEO, n) for n in range(1, videos + 1))
        + tuple(_record(f"音频素材{n}", KIND_AUDIO, n) for n in range(1, audios + 1))
    )
    return AssetLedger(entries=records)


def test_film_gate_judges_pictures_when_the_film_is_one_request():
    """≤15 秒：整片就是一次请求，10 张图当场拒（issue #86 AC4）。"""
    with pytest.raises(ReferenceCapacityError):
        check_film_capacity(_ledger(pictures=10), single_request=True)


def test_film_gate_leaves_pictures_to_the_per_segment_check_on_a_multi_segment_film():
    """>15 秒：整片卡是库存清单，24 张图**不许**在这里被拒（spec #84 D5 的正用法）。

    这条是「9 是单次请求上限、不是整片库存上限」的判据：改回整片判图数，本用例立刻红，
    而真实运行里「24 张素材、每段 ≤9」的片子会被整片拦死。
    """
    check_film_capacity(_ledger(pictures=24, videos=1), single_request=False)


def test_film_gate_still_rejects_too_many_videos_on_a_multi_segment_film():
    """视频没有段内子集：4 个视频在每一次请求里都超，>15 秒路线同样要停。"""
    with pytest.raises(ReferenceCapacityError) as excinfo:
        check_film_capacity(_ledger(pictures=2, videos=4), single_request=False)
    assert "<Video 4>" in str(excinfo.value)


def test_film_gate_still_rejects_too_many_audios_on_a_multi_segment_film():
    """音频同理（3 个上限是整片属性，不是某一段的属性）。"""
    with pytest.raises(ReferenceCapacityError) as excinfo:
        check_film_capacity(_ledger(pictures=2, audios=4), single_request=False)
    assert "<Audio 4>" in str(excinfo.value)


def test_empty_ledger_passes():
    """空台账不是超限（没交素材的多参会话照样能出卡）。"""
    check_reference_capacity([])

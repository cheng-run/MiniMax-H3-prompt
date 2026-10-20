"""读图校对对照器纯函数层（issue #44）：读图结果解析、偏差/补漏生成、素材画面事实合并。

接缝按票面/规格 Testing Decisions 预同意的唯一新缝：对照器纯函数——输入生图提示词×
读图结果，输出偏差清单+补漏候选。人裁三选与 UI 不设缝（向导薄接线在
``tests/test_wizard_asset_proofread.py``）。
"""
from __future__ import annotations

import pytest

from minimax_h3_prompt.asset_proofread import (
    DECISION_KEEP,
    DECISION_REGENERATE,
    DECISION_REWRITE,
    Adjudication,
    Claim,
    Deviation,
    Omission,
    ProofreadReport,
    Reading,
    build_proofread_request,
    compare_descriptions,
    merge_asset_description,
    parse_reading,
    proofread_descriptions,
    render_proofread_report,
)


# ---------------------------------------------------------------------------
# 读图结果解析
# ---------------------------------------------------------------------------

def test_parse_reading_extracts_claims():
    raw = (
        '{"claims": ['
        '{"aspect": "裙色", "prompt_quote": "红裙", "observed": "宝蓝色长裙"},'
        '{"aspect": "腕表", "prompt_quote": "", "observed": "左腕银色腕表"}'
        ']}'
    )
    reading = parse_reading(raw)
    assert reading.claims == (
        Claim(aspect="裙色", prompt_quote="红裙", observed="宝蓝色长裙"),
        Claim(aspect="腕表", prompt_quote="", observed="左腕银色腕表"),
    )


def test_parse_reading_tolerates_markdown_fence_and_prose():
    raw = '好的，对照结果如下：\n```json\n{"claims": []}\n```\n以上。'
    assert parse_reading(raw).claims == ()


def test_parse_reading_rejects_garbage_loudly():
    """解析不出 JSON 必须响——静默返回空清单就是「图跑偏无人纠」的入口（素材画面事实红线）。"""
    with pytest.raises(ValueError):
        parse_reading("这张图里有个女孩。")


# ---------------------------------------------------------------------------
# 对照器：偏差清单 + 补漏候选
# ---------------------------------------------------------------------------

_PROMPT = "红裙女孩站在秋日庭院里，手持青铜钥匙。"


def test_quote_anchored_claim_becomes_deviation():
    """引文逐字命中生图提示词 → 偏差（引文即「按图改文案」的定点锚）。"""
    reading = Reading(claims=(Claim("裙色", "红裙", "宝蓝色长裙"),))
    report = compare_descriptions(_PROMPT, reading)
    assert report.deviations == (Deviation("裙色", "红裙", "宝蓝色长裙"),)
    assert report.omissions == ()


def test_claim_without_quote_becomes_omission_candidate():
    """提示词没写的显著事实 → 补漏候选（图里有而描述没有）。"""
    reading = Reading(claims=(Claim("腕表", "", "左腕银色腕表"),))
    report = compare_descriptions(_PROMPT, reading)
    assert report.omissions == (Omission("腕表", "左腕银色腕表"),)
    assert report.deviations == ()


def test_unanchored_quote_stays_a_deviation_not_an_omission():
    """引文对不上原文的失锚条目仍算偏差（它是「不符」不是「没有」）——
    降级成补漏会把互相矛盾的事实并进描述（红裙+蓝裙同现）；静默丢弃则是「无人纠」。
    失锚在数据里可见（prompt_quote 非空），由向导引导人补锚后才允许定点改。"""
    reading = Reading(claims=(Claim("裙色", "红色的长裙", "宝蓝色长裙"),))
    report = compare_descriptions(_PROMPT, reading)
    assert report.deviations == (Deviation("裙色", "红色的长裙", "宝蓝色长裙"),)
    assert report.omissions == ()


def test_claims_order_is_preserved_and_clean_report():
    reading = Reading(claims=(
        Claim("裙色", "红裙", "宝蓝色长裙"),
        Claim("腕表", "", "左腕银色腕表"),
    ))
    report = compare_descriptions(_PROMPT, reading)
    assert len(report.deviations) == 1 and len(report.omissions) == 1
    assert compare_descriptions(_PROMPT, Reading()).is_clean()


def test_omission_already_described_is_not_listed_again():
    """描述已有的事实不算补漏——字面去重防「把已写的事实再并一遍」。"""
    reading = Reading(claims=(
        Claim("道具", "", "青铜钥匙"),          # 提示词里已有「青铜钥匙」
        Claim("腕表", "", "左腕银色腕表"),      # 提示词里没有
    ))
    report = compare_descriptions(_PROMPT, reading)
    assert report.omissions == (Omission("腕表", "左腕银色腕表"),)


# ---------------------------------------------------------------------------
# 锁死清单判据（issue #53）：只对身份特征判偏差，理想描述出入一律忽略
# ---------------------------------------------------------------------------

# 锁死清单条目是**生图提示词正文的原文片段**（节点侧钉住这条不变式），故读图逐字
# 摘录的那句通常包含条目。
_LOCKED = ("红裙", "青铜钥匙")


def test_locked_feature_quote_stays_a_deviation():
    """引文命中锁死清单 → 偏差：身份特征（发色/服装/标志物）的出入仍进人裁三选。"""
    reading = Reading(claims=(Claim("裙色", "红裙", "宝蓝色长裙"),))
    report = compare_descriptions(_PROMPT, reading, _LOCKED)
    assert report.deviations == (Deviation("裙色", "红裙", "宝蓝色长裙"),)
    assert report.omissions == ()


def test_quote_shorter_than_a_locked_entry_still_hits():
    """模型只摘了条目的一截也认（互为子串）——否则漏判的正是「图跑偏无人纠」。"""
    reading = Reading(claims=(Claim("道具", "青铜", "黄铜钥匙"),))
    report = compare_descriptions(_PROMPT, reading, _LOCKED)
    assert len(report.deviations) == 1


def test_ideal_description_quote_is_ignored_not_adjudicated():
    """理想描述的出入不进人裁、也不进补漏（AC2 裁定：直接忽略），但**记进 ``exempted``**。

    当日现场那条「罐体按三分法偏左 → 实测几乎正中」就是这一档：判据是锁死清单，
    清单外的引文一律忽略——39 条偏差里的构图口味类从此既不进三选、也不会被当成
    「图里有而描述没有」并进描述（它是「不符」不是「没有」，并进去就是红裙+蓝裙同现）。

    ``exempted`` 是「忽略了几条」的唯一可观测面：AC2 的卖点就是「39 条收敛到个位数」，
    收敛了多少若代码产不出来，清单配歪（某条过宽→全部命中）时人拿不到任何信号。
    """
    claim = Claim("构图", "女孩站在秋日庭院里", "女孩几乎站在画面正中")
    report = compare_descriptions(_PROMPT, Reading(claims=(claim,)), _LOCKED)
    assert report.is_clean(), "忽略的条目不许再算偏差或补漏"
    assert report.exempted == (claim,), "被忽略的条目没有留痕（收敛与否无从观察）"


def test_omission_path_is_unchanged_by_the_locked_list():
    """补漏并入机制不变（AC4）：空引文的「图里有而描述没有」照旧成候选，
    与清单在不在场无关。"""
    reading = Reading(claims=(Claim("腕表", "", "左腕银色腕表"),))
    with_locked = compare_descriptions(_PROMPT, reading, _LOCKED)
    without = compare_descriptions(_PROMPT, reading)
    assert with_locked == without == ProofreadReport(omissions=(Omission("腕表", "左腕银色腕表"),))


def test_quote_without_a_locked_list_keeps_the_old_verdict():
    """未声明锁死清单（旧产物/旧会话）＝**不收窄**：非空引文仍一律判偏差。

    锁死清单是「收窄判据」的声明；没声明就不收窄——旧会话照旧，图跑偏仍有人纠。
    """
    reading = Reading(claims=(Claim("构图", "女孩站在秋日庭院里", "几乎正中"),))
    report = compare_descriptions(_PROMPT, reading)
    assert len(report.deviations) == 1
    assert report.omissions == ()


def test_blank_locked_entries_do_not_widen_the_judgement():
    """空串／空白条目不算命中——空串是任何串的子串，混进清单会让判据恒真（全部算偏差）。"""
    reading = Reading(claims=(Claim("构图", "女孩站在秋日庭院里", "几乎正中"),))
    report = compare_descriptions(_PROMPT, reading, ("", "   ", "红裙"))
    assert report.deviations == ()


# ---------------------------------------------------------------------------
# 素材画面事实合并：纠偏差只改那一处、补漏并入、绝不整体替换
# ---------------------------------------------------------------------------

# 底本刻意让「红裙」在语义名与正文各出现一次——只改正文那处、名字不动是红线。
_NAME = "红裙女孩"
_BASE = "红裙女孩站在秋日庭院里，身穿红裙，手持青铜钥匙。"


def test_rewrite_changes_only_the_quoted_spot():
    """按图改文案＝只改不符的那一处：正文那处改掉，语义名与其余细节逐字保留。"""
    merged = merge_asset_description(
        _BASE,
        [Adjudication(Deviation("裙色", "红裙", "宝蓝色长裙"), DECISION_REWRITE)],
        name=_NAME,
    )
    assert merged == "红裙女孩站在秋日庭院里，身穿宝蓝色长裙，手持青铜钥匙。"


def test_rewrite_quote_inside_semantic_name_is_refused():
    """语义名是标识符不是偏差处：引文只命中名字 → 响 ValueError，不静默改名。"""
    with pytest.raises(ValueError):
        merge_asset_description(
            "红裙女孩站在秋日庭院里。",
            [Adjudication(Deviation("裙色", "红裙", "宝蓝色长裙"), DECISION_REWRITE)],
            name=_NAME,
        )


def test_keep_decision_leaves_description_untouched():
    merged = merge_asset_description(
        _PROMPT,
        [Adjudication(Deviation("裙色", "红裙", "宝蓝色长裙"), DECISION_KEEP)],
    )
    assert merged == _PROMPT


def test_regenerate_decision_leaves_description_untouched():
    """重生成＝图要按描述重画，文案不动（描述仍是主体）。"""
    merged = merge_asset_description(
        _PROMPT,
        [Adjudication(Deviation("裙色", "红裙", "宝蓝色长裙"), DECISION_REGENERATE)],
    )
    assert merged == _PROMPT


def test_accepted_omissions_are_appended_as_facts():
    merged = merge_asset_description(
        _PROMPT, [], additions=["左腕银色腕表", "发间别着一枚银色发卡"],
    )
    assert merged == _PROMPT + "；左腕银色腕表；发间别着一枚银色发卡"


def test_rewrite_with_unanchored_quote_is_loud_not_silent():
    """引文对不上原文就无法定点改——抛错要响，不许悄悄不动或整篇替换。"""
    with pytest.raises(ValueError):
        merge_asset_description(
            _PROMPT,
            [Adjudication(Deviation("裙色", "红色的长裙", "宝蓝色长裙"), DECISION_REWRITE)],
        )


def test_rewrite_with_ambiguous_quote_is_loud():
    """引文命中多处＝「那一处」不唯一：响 ValueError 交人补锚，绝不猜一处改了算。"""
    with pytest.raises(ValueError):
        merge_asset_description(
            "红裙飘动，红裙如火。",
            [Adjudication(Deviation("裙色", "红裙", "宝蓝色长裙"), DECISION_REWRITE)],
        )


def test_rewrite_precise_quote_pins_single_spot():
    """人补锚后的长引文＝唯一命中，替换整段（含引文内其他词）不留残句。"""
    merged = merge_asset_description(
        "红裙飘动，红裙如火。",
        [Adjudication(Deviation("裙色", "红裙飘动", "宝蓝色长裙飘动"), DECISION_REWRITE)],
    )
    assert merged == "宝蓝色长裙飘动，红裙如火。"


def test_rewrite_can_carry_custom_replacement_text():
    """按图改文案的替换文字默认取图中实见，但允许人改写（「红裙」→「蓝裙」比整句顺口）。"""
    merged = merge_asset_description(
        _PROMPT,
        [Adjudication(Deviation("裙色", "红裙", "宝蓝色长裙"), DECISION_REWRITE,
                      replacement="蓝裙")],
    )
    assert merged == "蓝裙女孩站在秋日庭院里，手持青铜钥匙。"


def test_no_wholesale_replacement_path_keeps_all_details():
    """AC4：无「拿读图整体替换」的路径——全保留时描述逐字不丢，改动只落在引文处。"""
    adjudications = [
        Adjudication(Deviation("裙色", "红裙", "宝蓝色长裙"), DECISION_REWRITE),
        Adjudication(Deviation("道具", "青铜钥匙", "青铜钥匙"), DECISION_KEEP),
    ]
    merged = merge_asset_description(_PROMPT, adjudications, additions=["左腕银色腕表"])
    # 未被引文覆盖的细节原样在场
    assert "女孩站在秋日庭院里" in merged
    assert "手持青铜钥匙" in merged
    # 改动只落在引文处 + 补漏尾部并入
    assert merged == "宝蓝色长裙女孩站在秋日庭院里，手持青铜钥匙。；左腕银色腕表"


# ---------------------------------------------------------------------------
# 读图请求文本（返回字符串即契约）与出站渲染
# ---------------------------------------------------------------------------

def test_proofread_request_demands_verbatim_quotes_and_json_claims():
    """读图请求是对照器的输入侧契约：引文必须逐字摘录（定点改文案靠它落锚），
    输出必须是 claims JSON。删掉「逐字」要求，本用例必须红。"""
    request = build_proofread_request(_PROMPT)
    assert _PROMPT in request, "生图提示词必须整体进请求——对照基准不许被摘要"
    assert "prompt_quote" in request and "observed" in request
    assert "claims" in request
    assert "逐字" in request, "引文逐字摘录要求缺失：失锚引文会让定点改文案无处落笔"


def test_proofread_request_carries_the_locked_list_and_the_narrowing_rule():
    """锁死清单进读图请求（issue #53 AC2）：清单**逐条**在场，且写明只有它算偏差。

    只写「有锁死清单」不够——读图模型要按字面认得出是哪几条（同 issue #37 的教训：
    请求里不给词表，那句「使其通过校验」在信息上不可赢）。清单不在请求里，模型交回的
    prompt_quote 就落不到条目上，对照器的命中判据恒不命中、偏差清单悄悄清空。
    """
    request = build_proofread_request(_PROMPT, _LOCKED)
    assert "锁死清单" in request, "请求没提锁死清单，模型无从知道偏差只判哪几处"
    for entry in _LOCKED:
        assert entry in request, f"锁死清单条目 {entry!r} 没进请求"
    assert _PROMPT in request, "生图提示词仍须整体进请求——对照基准不许被摘要"


def test_proofread_request_without_locked_list_is_unchanged():
    """未声明锁死清单时请求逐字与改动前一致（旧产物照旧走老口径）。"""
    assert build_proofread_request(_PROMPT) == build_proofread_request(_PROMPT, ())
    assert "锁死清单" not in build_proofread_request(_PROMPT)


def test_render_report_lists_deviations_and_omissions():
    report = ProofreadReport(
        deviations=(Deviation("裙色", "红裙", "宝蓝色长裙"),),
        omissions=(Omission("腕表", "左腕银色腕表"),),
    )
    text = render_proofread_report(report)
    assert "红裙" in text and "宝蓝色长裙" in text
    assert "左腕银色腕表" in text
    assert "偏差" in text and "补漏" in text


def test_render_report_shows_how_many_were_exempted():
    """被锁死清单忽略的条目要在报告里露面（issue #53 AC2 的可观测面）。

    只出「偏差 N 处」的话，收窄与否在界面上完全看不出来——39 条收敛到 10 条，
    屏上仍是一句「[偏差] 10 处」，人无从判断是清单起了作用还是模型这次没报。
    """
    report = ProofreadReport(exempted=(
        Claim("构图位置", "罐体按三分法置于画面偏左位置", "罐体几乎位于画面水平正中"),
        Claim("拍摄视角", "低角度逆光与侧逆光照明", "机位接近平视且略高"),
    ))
    text = render_proofread_report(report)
    assert "[忽略] 2 条" in text, "被忽略的条数没上屏"
    assert "构图位置" in text and "拍摄视角" in text, "忽略清单没列出条目（清单配歪时无从发现）"
    assert text.strip(), "空报告也要有话说"


# ---------------------------------------------------------------------------
# 素材画面事实取材口：下游提示词写作从这里按语义名取合并后的描述
# ---------------------------------------------------------------------------

def test_proofread_descriptions_maps_name_to_merged_description():
    """state 里的校对记录 → name→素材画面事实。下游（ref_asset_registry）唯一取材口。"""
    state = {"asset_proofreads": {
        "红裙女孩": {"sha256": "ab", "status": "done",
                    "description": "蓝裙、黑发的描述；左腕银色腕表",
                    "deviations": [], "omissions": []},
        "青铜钥匙": {"sha256": "cd", "status": "pending_regen",
                    "description": "青铜钥匙描述", "deviations": [], "omissions": []},
    }}
    merged = proofread_descriptions(state)
    assert merged == {
        "红裙女孩": "蓝裙、黑发的描述；左腕银色腕表",
        "青铜钥匙": "青铜钥匙描述",
    }


def test_proofread_descriptions_without_state_is_empty():
    """键缺失／JSON null／空表 → 空——没有记录就是没有，不是错。"""
    assert proofread_descriptions({}) == {}
    assert proofread_descriptions({"asset_proofreads": None}) == {}
    assert proofread_descriptions({"asset_proofreads": {}}) == {}


def test_proofread_descriptions_wrong_shape_shouts():
    """falsy 错形（[] 等）响 ValueError，不许洗成空表（session_store 画布同款纪律，
    issue #42 复审）：会话文件是自己写的，错形只可能是损坏或外人改写——
    静默当空＝悄悄回落默认，比报错更糟。"""
    with pytest.raises(ValueError):
        proofread_descriptions({"asset_proofreads": []})
    with pytest.raises(ValueError):
        proofread_descriptions({"asset_proofreads": "坏账"})

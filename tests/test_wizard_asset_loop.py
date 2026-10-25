"""向导·素材循环逐张闭环（issue #50）：单张出词→审查→交回→校对→裁决。

覆盖票面六条 AC：
- AC1 一次只出一张、裁决落账（或显式放弃）后才出下一张；
- AC2 审查打回不入库；「改写提示词」算一轮修订并留痕（追加式轮次账）；
- AC3 校对后「重生成」默认原词再掷、「改词再掷」出口存在且留痕；
- AC4 队列纯 ``<Picture N>`` 升序（issue #66），支持跳号/指定下一个出谁；
- AC5 展示的画布行与用户选定画布同源（不回落默认表）。

薄接线：走真实调用链（``run_wizard``），只打桩 LLM 与交互输入。
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage

from minimax_h3_prompt import model_factory
from minimax_h3_prompt.asset_ledger import load_asset_ledger, resolve_asset_path
from minimax_h3_prompt import asset_loop
from minimax_h3_prompt.asset_loop import ASSET_ROUNDS_FILENAME
from minimax_h3_prompt.brief_parser import Brief
from minimax_h3_prompt.session_store import (
    find_awaiting_sessions,
    load_session,
    save_session,
    STATUS_AWAITING_FRAMES,
)
from minimax_h3_prompt.tools import reference_auditor
from minimax_h3_prompt.ui import wizard

TOPIC = "红裙女孩走进秋日庭院，把青铜钥匙交给蓝衫少年。"

# 队列默认序（issue #66 起纯 <Picture N> 升序＝剧本出场序＝接线序：
# 红裙女孩 1 → 秋日庭院 2 → 青铜钥匙 3 → 蓝衫少年 4）
QUEUE_ORDER = ["红裙女孩", "秋日庭院", "青铜钥匙", "蓝衫少年"]

_SIX_SECTION_PROMPT = (
    "subject_definitions:\n<Subject 1> is 红裙女孩 in <Picture 1>.\n\n"
    "summary:\n[reference generation] The target video shows the girl handing over the key.\n\n"
    "retention_analysis:\n<Subject 1>: fully_preserved - ok.\n\n"
    "detailed_description:\n[Shot 1] A medium shot frames the girl in the garden.\n\n"
    "overall_soundscape:\nSoft outdoor ambience continues.\n\n"
    "non_diegetic_music:\nN/A"
)

GIRL_PROMPT = "人物·红裙女孩专属生图提示词：红裙、黑发、银项链"


def _silence(monkeypatch) -> None:
    monkeypatch.setattr(wizard, "_drain_stdin", lambda: None)
    monkeypatch.setattr(wizard.sys, "stdin", SimpleNamespace(isatty=lambda: True))


def _fake_inputs(monkeypatch, inputs: list[str]) -> list[str]:
    prompts = iter(inputs)
    asked: list[str] = []

    def fake_prompt(text: str) -> str:
        asked.append(text)
        try:
            return next(prompts)
        except StopIteration:
            # 逐段陪跑的交回口（issue #57 起 ≤15 秒直出也走这一套收尾）：喂完输入就
            # 停在这里。本文件钉的不是收尾，让 StopIteration 冒出去只会冒充失败。
            if "输出视频路径" in text:
                return ""
            raise

    monkeypatch.setattr(wizard, "_prompt", fake_prompt)
    return asked


def _fake_confirms(monkeypatch, rules: list[tuple[str, bool]],
                   sink: list[str] | None = None) -> list[str]:
    queue = list(rules)
    asked: list[str] = sink if sink is not None else []

    def fake_confirm(text: str, default: bool = True) -> bool:
        asked.append(text)
        for i, (needle, answer) in enumerate(queue):
            if needle in text:
                queue.pop(i)
                return answer
        return default

    monkeypatch.setattr(wizard, "_confirm", fake_confirm)
    return asked


def _ref_stage1_state() -> dict:
    """阶段 1 产物：四张资产（语义名都在各自提示词正文里，故锁死清单取语义名合法）。

    锁死清单（issue #53）逐条是**本资产提示词正文的原文片段**——节点侧钉住的不变式。
    取语义名是最省事的一份合法清单，也让既有用例（偏差引文「红裙」）照旧命中：
    「红裙」与条目「红裙女孩」互为子串。
    """
    def _entry(name: str, prompt: str) -> dict:
        return {"name": name, "positive_prompt": prompt, "locked_features": [name]}

    return {
        "script": TOPIC,
        "character_image_prompts": [
            _entry("蓝衫少年", "人物·蓝衫少年专属生图提示词"),
            _entry("红裙女孩", GIRL_PROMPT),
        ],
        "prop_image_prompts": [
            _entry("青铜钥匙", "道具·青铜钥匙专属生图提示词"),
        ],
        "scene_image_prompts": [
            _entry("秋日庭院", "场景·秋日庭院专属生图提示词"),
        ],
    }


def _cfg(tmp_path, **overrides):
    base = dict(
        default_duration=5.0, default_language="Chinese",
        sessions_root=tmp_path / "sessions", max_clarification_rounds=0,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _stub_stage1(monkeypatch) -> None:
    monkeypatch.setattr(
        wizard, "run_stage1",
        lambda brief, config, **kw: (_ref_stage1_state(), object(),
                                     {"prompt_engineer": object()}),
    )


def _stub_stage2(monkeypatch, captured: dict | None = None) -> None:
    def fake_run_stage2(state, brief, config, **kwargs):
        if captured is not None:
            captured["state"] = dict(state)
        return dict(state), _SIX_SECTION_PROMPT

    monkeypatch.setattr(wizard, "run_stage2", fake_run_stage2)


def _stub_vision(monkeypatch, mapping: dict[str, str]) -> list[str]:
    """按正本文件名（``<编号>-<语义名>``）路由读图结果；记录调用过的路径。"""
    seen: list[str] = []

    def fake_describe(path, instruction=None):
        seen.append(str(path))
        for key, raw in mapping.items():
            if key in str(path):
                return raw
        return '{"claims": []}'

    monkeypatch.setattr(reference_auditor, "describe_image", fake_describe)
    return seen


# 导入时的真工厂：``_run_loop`` 靠它认出「这个用例自己打过桩没有」（见那里的注释）。
_REAL_BUILD_CHAT_MODEL = model_factory.build_chat_model

# 回灌请求里那两块标记：假模型靠它们分辨「这是回灌还是改写」、并从请求里抠回当前词。
# **从 ``asset_loop`` 导入、不在这里另抄一份**（ADR 0008 裁定二：块标题只有一处定义，
# 块、角色 .md 的指针句、测试都按它认）——另抄一份的话，改标记时这里静默失配，
# 两条 AC3 用例会红得误导（报「没回灌」，实为标记搬了家）。
_ALIGN_HEAD = asset_loop.SETTLED_FACTS_HEAD
_CURRENT_HEAD = asset_loop.CURRENT_PROMPT_HEAD
# AI 改词请求（issue #72）的块标题：假模型靠它把「AI 改词」与「用户意见改写」分开
# （两者都回一条词，混在一起就看不出 AI 改词到底走没走到）。
_AI_REWORD_HEAD = asset_loop.AI_REWORD_DEVIATIONS_HEAD


class _FakeLLM:
    """改写、回灌与 AI 改词共用的假模型（三个出口都经 ``build_chat_model`` 建）。

    - 回灌请求（带 ``【已定稿素材画面事实】`` 块）默认**原词回吐**——从请求里抠回当前
      提示词再加 ``align_suffix``，于是不关心回灌的用例（「屏上出现了哪条词」那类断言）
      与改动前逐字一致，而回灌的物证照旧落在 ``requests`` 上（请求是缝）；
    - AI 改词请求（带 ``【偏差清单】`` 块，issue #72）回 ``ai_reword_reply``；
      ``ai_reword_error`` 非空时**抛它**——失败留菜单那条路要能一次走到；
    - 其余改写请求回 ``rewrite_reply``。
    """

    def __init__(self, rewrite_reply: str = "改写后的提示词", align_suffix: str = "", *,
                 ai_reword_reply: str = "AI 按偏差改写的提示词",
                 ai_reword_error: Exception | None = None) -> None:
        self.rewrite_reply = rewrite_reply
        self.align_suffix = align_suffix
        self.ai_reword_reply = ai_reword_reply
        self.ai_reword_error = ai_reword_error
        self.requests: list[str] = []
        self.configs: list = []

    def invoke(self, message, config=None):
        text = str(message)
        self.requests.append(text)
        self.configs.append(config)
        # 直调入口（ADR 0019）靠这个信号发「首字节」心跳：真实路径上是流式 token，
        # 假模型手动打一下，等待事件那条契约才测得到（issue #71 的横幅/心跳断言）；
        # 改写/回灌两条路仍不传 config，故这一下只影响走看门狗的那些请求。
        for callback in (config or {}).get("callbacks") or []:
            callback.on_llm_new_token("…")
        if _AI_REWORD_HEAD in text:
            if self.ai_reword_error is not None:
                raise self.ai_reword_error
            return self.ai_reword_reply
        if _ALIGN_HEAD in text:
            return _current_prompt_from(text) + self.align_suffix
        return self.rewrite_reply


def _current_prompt_from(request: str) -> str:
    """从回灌请求里抠回「当前提示词」（假模型的原词回吐用）。"""
    return request.split("【当前提示词】\n", 1)[-1].split(f"\n\n{_ALIGN_HEAD}", 1)[0].strip()


def _stub_rewrite(monkeypatch, new_text: str = "改写后的提示词") -> list[str]:
    """打桩「改写提示词」的 LLM 改写：记录请求、回固定新词。"""
    llm = _FakeLLM(new_text)
    monkeypatch.setattr(model_factory, "build_chat_model", lambda: llm)
    return llm.requests


def _write_source(tmp_path: Path, name: str, content: bytes) -> Path:
    source = tmp_path / "returned" / name
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(content)
    return source


def _claims(deviations=(), omissions=()) -> str:
    items = [{"aspect": a, "prompt_quote": q, "observed": o} for a, q, o in deviations]
    items += [{"aspect": a, "prompt_quote": "", "observed": o} for a, o in omissions]
    return json.dumps({"claims": items}, ensure_ascii=False)


def _rounds(tmp_path: Path) -> list[dict]:
    files = list((tmp_path / "sessions").rglob(ASSET_ROUNDS_FILENAME))
    if not files:
        return []
    out: list[dict] = []
    for path in files:
        out.extend(json.loads(line) for line in
                   path.read_text(encoding="utf-8").splitlines() if line.strip())
    return out


def _run_loop(tmp_path, monkeypatch, tail: list[str], *,
              canvases: dict | None = None,
              vision: dict[str, str] | None = None,
              vision_model=None,
              confirms: list[tuple[str, bool]] | None = None,
              captured: dict | None = None,
              confirm_log: list[str] | None = None,
              llm: "_FakeLLM | None" = None) -> tuple[list[str], int, list[str]]:
    """全程驱动：阶段 1 → 素材循环 → 尾账/注入卡 → 阶段 2。返回 (asked, code, vision_calls)。

    ``confirm_log`` 传列表即收下问过的**确认句**（``_confirm`` 的文案；``asked`` 只装
    ``_prompt`` 的），免得「撤登记问没问人」这类断言没处落。

    ``llm`` 传假模型即用它（回灌/改写两个出口的请求都记在它身上）；不传且用例自己
    也没打过桩时，这里兜一个**默认假模型**——素材循环的**回灌**（issue #53）会调
    ``build_chat_model``，漏打桩就会真去建客户端（主树有 key 时直接打网络请求，
    干净检出上则报错），两条路都不是用例该走的路。

    ``vision_model`` 传假视觉模型则**走真实 ``describe_image``**（issue #71 的读图
    看门狗考题：只有真函数才会发首字节事件），此时第三个返回值恒为空表。
    """
    _silence(monkeypatch)
    monkeypatch.setattr(wizard, "_choose_canvases", lambda: canvases or {})
    _stub_stage1(monkeypatch)
    _stub_stage2(monkeypatch, captured)
    if vision_model is not None:
        # 按 streaming 选模型：主路（流式）用传进来的，回退档也给它——本文件没有
        # 「网关不支持流式」的考题（那条在 tests/test_vision_watchdog.py）。
        monkeypatch.setattr(reference_auditor, "_build_vision_model",
                            lambda streaming=True: vision_model)
        calls: list[str] = []
    else:
        calls = _stub_vision(monkeypatch, vision or {})
    if llm is not None:
        monkeypatch.setattr(model_factory, "build_chat_model", lambda: llm)
    elif model_factory.build_chat_model is _REAL_BUILD_CHAT_MODEL:
        # 用例自己打过桩（_stub_rewrite）就不覆盖它；没打过才兜底。
        monkeypatch.setattr(model_factory, "build_chat_model", lambda: _FakeLLM())
    asked = _fake_inputs(monkeypatch, [TOPIC, "", "", "4"] + list(tail))
    # sink 传的是**同一个列表对象**（extend 只拷当时的空表，后续 append 看不见）
    _fake_confirms(monkeypatch, confirms or [("视频/音频", False)], sink=confirm_log)
    code = wizard.run_wizard(_cfg(tmp_path))
    return asked, code, calls


# ---------------------------------------------------------------------------
# AC4：队列纯 <Picture N> 升序（＝剧本出场序＝接线序）；跳号不丢、只剩一张不问
# ---------------------------------------------------------------------------

def test_queue_follows_picture_number_order(tmp_path, monkeypatch, capsys):
    """四张全交回：出词顺序必须是 <Picture N> 升序，不是「人物→场景→道具」分组。"""
    paths = {name: _write_source(tmp_path, f"{name}.png", name.encode())
             for name in QUEUE_ORDER}
    # 每张：审查满意→路径；队列菜单 回车=默认队首（最后一张自动出，不问）
    tail: list[str] = []
    for i, name in enumerate(QUEUE_ORDER):
        if i < len(QUEUE_ORDER) - 1:
            tail.append("")  # 队列菜单：回车=出队首
        tail += ["1", str(paths[name])]
    tail += ["8", "1"]  # 视频输出选择器（issue #45）
    asked, code, _ = _run_loop(tmp_path, monkeypatch, tail)

    assert code == 0
    out = capsys.readouterr().out
    positions = [out.index(f"当前待办：{name}") for name in QUEUE_ORDER]
    assert positions == sorted(positions), f"出词顺序不是 <Picture N> 升序：{QUEUE_ORDER}"
    # 每张出词只出现一次（一次只展示一张，AC1）
    assert out.count(GIRL_PROMPT) == 1
    assert list((tmp_path / "sessions").rglob("video-prompt.md"))


def test_queue_menu_shows_list_index_and_picture_number(tmp_path, monkeypatch, capsys):
    """菜单同屏显示列表序号与 ``<Picture N>``，且整表按 Picture 号升序排列。"""
    # 四张全放弃：菜单会被问三次（最后一张自动出），表文本每次都重打一遍。
    _run_loop(tmp_path, monkeypatch, ["", "4", "", "4", "", "4", "4", "8", "1"])
    out = capsys.readouterr().out
    # 判据从同一份顺序真源派生：序号是列表下标 1..4，Picture 号来自剧本出场序。
    expected = [
        (1, "红裙女孩", 1, "人物"),
        (2, "秋日庭院", 2, "场景"),
        (3, "青铜钥匙", 3, "道具"),
        (4, "蓝衫少年", 4, "人物"),
    ]
    for index, name, number, label in expected:
        assert f"{index}. <Picture {number}> {name}（{label}）" in out, (
            f"菜单缺序号/Picture 号/类型标签：{index}/{number}/{name}")


def test_last_pending_is_not_asked(tmp_path, monkeypatch):
    """只剩一张待办时不再问「下一个出谁」——没有可跳的号，问一句只是打断。"""
    asked, code, _ = _run_loop(
        tmp_path, monkeypatch, ["", "4", "", "4", "", "4", "4", "8", "1"])
    assert code == 0
    menus = [text for text in asked if "下一个出谁" in text]
    assert len(menus) == len(QUEUE_ORDER) - 1, (
        f"四张待办应只问三次（最后一张自动出），实际 {len(menus)} 次")


def test_skipped_asset_reappears_in_later_menu(tmp_path, monkeypatch, capsys):
    """跳号只改本次出谁，不把被跳过的素材踢出待办：下一轮菜单里它还在。"""
    # 首问选 2 号（秋日庭院）并放弃；被跳过的 1 号（红裙女孩）必须回到下一轮菜单。
    tail = ["2", "4", "", "4", "", "4", "4", "8", "1"]
    _run_loop(tmp_path, monkeypatch, tail)
    out = capsys.readouterr().out
    # 菜单共三次：第一次四张，之后每次少一张——被跳过的 1 号仍被处理
    assert out.count("[素材循环] 待办队列") == len(QUEUE_ORDER) - 1
    assert out.index("当前待办：秋日庭院") < out.index("当前待办：红裙女孩"), (
        "跳号后红裙女孩应当仍在后续待办里被处理")


def test_queue_supports_skip_and_specify_next(tmp_path, monkeypatch, capsys):
    """跳号/指定下一个出谁（AC4）：首问直接打序号 3，先出 3 号（青铜钥匙）。"""
    paths = {name: _write_source(tmp_path, f"{name}.png", name.encode())
             for name in QUEUE_ORDER}
    tail: list[str] = [
        "3",              # 队列菜单：跳号，先出 3 号（青铜钥匙）
        "1", str(paths["青铜钥匙"]),
        "", "4",          # 回队首（红裙女孩）→ 放弃
        "", "4",          # 秋日庭院 → 放弃
        "4",              # 最后一张（蓝衫少年）自动出，放弃
        "8", "1",
    ]
    asked, code, _ = _run_loop(tmp_path, monkeypatch, tail)
    assert code == 0
    out = capsys.readouterr().out
    # 第一张出的是 3 号（跳号生效）
    first = min(out.index(f"当前待办：{name}") for name in QUEUE_ORDER)
    assert "当前待办：青铜钥匙" in out
    assert out.index("当前待办：青铜钥匙") == first, "跳号没生效：3 号应当先出"
    assert any("跳号/指定下一个出谁" in text for text in asked), "队列菜单缺跳号入口"


# ---------------------------------------------------------------------------
# AC1：一次只出一张；裁决落账（或显式放弃）后才出下一张
# ---------------------------------------------------------------------------

def test_one_prompt_at_a_time_and_advances_only_after_settle(tmp_path, monkeypatch, capsys):
    """出词→审查→交回→校对→裁决 串成单张节奏；下一张的词在本张落账后才出现。"""
    girl = _write_source(tmp_path, "girl.png", b"girl")
    tail = [
        "", "1", str(girl),     # 红裙女孩：满意→交回（干净读图→直接落账）
        "", "4",                # 蓝衫少年：显式放弃
        "", "4",                # 秋日庭院：显式放弃
        "4",                    # 青铜钥匙（最后一张自动出）：显式放弃
        "8", "1",
    ]
    asked, code, _ = _run_loop(tmp_path, monkeypatch, tail)
    assert code == 0
    out = capsys.readouterr().out
    # 张与张串行：本张的审查问题出现在下一张出词之前
    girl_pos = out.index("当前待办：红裙女孩")
    boy_pos = out.index("当前待办：蓝衫少年")
    assert girl_pos < boy_pos
    assert any("显式放弃这张资产" in text for text in asked), "审查菜单缺显式放弃出口"
    assert list((tmp_path / "sessions").rglob("video-prompt.md"))


def test_abandon_keeps_asset_out_of_ledger_and_marks_state(tmp_path, monkeypatch, capsys):
    """显式放弃＝不入库、写进 asset_loop.abandoned，续接不再排它。"""
    tail = ["", "4", "", "4", "", "4", "4", "8", "1"]  # 四张全放弃
    asked, code, _ = _run_loop(tmp_path, monkeypatch, tail)
    assert code == 0
    # 四张全放弃＝一张都没交回：台账文件根本不该存在（不是「存在但为空」）
    assert not list((tmp_path / "sessions").rglob("asset-ledger.json")), "放弃的资产不许入库"
    session = load_session(next((tmp_path / "sessions").rglob("session-state.json")).parent)
    assert sorted(session.stage_state["asset_loop"]["abandoned"]) == sorted(QUEUE_ORDER)


def test_pause_at_review_stays_resumable(tmp_path, monkeypatch, capsys):
    """审查菜单 q=先到这里：不写 video-prompt.md，会话保持可续接（current 待办在盘）。"""
    tail = ["", "q"]
    asked, code, _ = _run_loop(tmp_path, monkeypatch, tail)
    assert code == 0
    assert not list((tmp_path / "sessions").rglob("video-prompt.md"))
    assert find_awaiting_sessions(tmp_path / "sessions"), "暂停必须留成可续接态"
    session = find_awaiting_sessions(tmp_path / "sessions")[0]
    assert session.stage_state["asset_loop"]["current"] == "红裙女孩"


# ---------------------------------------------------------------------------
# AC2：审查打回不入库；改写提示词留痕一轮
# ---------------------------------------------------------------------------

def test_review_external_reroll_never_registers(tmp_path, monkeypatch, capsys):
    """打回·外部重掷：不登记被拒图（无台账行、无轮次账），系统只把球放回审查。"""
    girl = _write_source(tmp_path, "girl.png", b"girl")
    tail = [
        "", "2", "1", str(girl),  # 打回外部重掷 → 再审 → 满意交回
        "", "4", "", "4", "4",
        "8", "1",
    ]
    asked, code, _ = _run_loop(tmp_path, monkeypatch, tail)
    assert code == 0
    assert any("外部重掷" in text for text in asked)
    assert not _rounds(tmp_path), "外部重掷不许留痕（不登记）"
    # 打回发生在填路径之前——被拒图从未拿到路径
    out = capsys.readouterr().out
    assert "被拒图不登记" in out
    ledger = load_asset_ledger(next((tmp_path / "sessions").rglob("asset-ledger.json")).parent)
    assert [e.name for e in ledger.entries] == ["红裙女孩"]


def test_review_rewrite_logs_round_and_rereleases(tmp_path, monkeypatch, capsys):
    """打回·改写提示词：算一轮修订、留痕追加账、新词重新出站（AC2）。"""
    girl = _write_source(tmp_path, "girl.png", b"girl")
    rewrite_calls = _stub_rewrite(monkeypatch, "人物·红裙女孩专属生图提示词：宝蓝裙")
    tail = [
        "", "3", "裙子改成宝蓝色", "1", str(girl),  # 打回改写→意见→新词→满意→交回
        "", "4", "", "4", "4",
        "8", "1",
    ]
    asked, code, _ = _run_loop(tmp_path, monkeypatch, tail)
    assert code == 0
    assert any("改写要求" in text for text in asked), "改写要先问意见"
    assert rewrite_calls, "改写提示词没调 LLM"
    rounds = _rounds(tmp_path)
    assert [r["trigger"] for r in rounds] == ["review_reword"]
    assert rounds[0]["feedback"] == "裙子改成宝蓝色"
    assert rounds[0]["prompt_before"] == GIRL_PROMPT
    assert rounds[0]["prompt_after"] == "人物·红裙女孩专属生图提示词：宝蓝裙"
    assert rounds[0]["round"] == 1
    out = capsys.readouterr().out
    assert "人物·红裙女孩专属生图提示词：宝蓝裙" in out, "改写后的新词没有重新出站"
    # 对照底本换新词：校对请求里带的是新词
    ledger = load_asset_ledger(next((tmp_path / "sessions").rglob("asset-ledger.json")).parent)
    assert [e.name for e in ledger.entries] == ["红裙女孩"]


def test_review_rewrite_cancel_keeps_prompt(tmp_path, monkeypatch, capsys):
    """改写意见留空＝取消：不动词、不留痕，回到审查菜单。"""
    girl = _write_source(tmp_path, "girl.png", b"girl")
    tail = [
        "", "3", "", "1", str(girl),  # 改写→空意见（取消）→满意→交回
        "", "4", "", "4", "4",
        "8", "1",
    ]
    asked, code, _ = _run_loop(tmp_path, monkeypatch, tail)
    assert code == 0
    assert not _rounds(tmp_path), "取消的改写不许留痕"
    out = capsys.readouterr().out
    assert "已取消改写" in out
    assert GIRL_PROMPT in out


# ---------------------------------------------------------------------------
# AC3：校对后重生成——默认原词再掷；改词再掷留痕
# ---------------------------------------------------------------------------

def test_regen_defaults_to_same_prompt_reroll_and_logs(tmp_path, monkeypatch, capsys):
    """裁决「重生成」→ 回车=原词再掷：留痕 regen_reroll，词不动。"""
    girl = _write_source(tmp_path, "girl.png", b"girl")
    vision = {"红裙女孩":_claims(deviations=[("裙色", "红裙", "宝蓝色长裙")])}
    tail = [
        "", "1", str(girl),
        "2",          # 三选：重生成这张素材图
        "",           # 重生成菜单：回车=原词再掷
        "4",          # 重掷后回审查——直接放弃这张收尾
        "", "4", "", "4", "4",
        "8", "1",
    ]
    asked, code, _ = _run_loop(tmp_path, monkeypatch, tail, vision=vision)
    assert code == 0
    assert any("原词再掷" in text for text in asked), "重生成菜单缺默认口"
    rounds = _rounds(tmp_path)
    assert [r["trigger"] for r in rounds] == ["regen_reroll"]
    assert rounds[0]["prompt_before"] == rounds[0]["prompt_after"] == GIRL_PROMPT
    out = capsys.readouterr().out
    assert "原提示词重掷" in out


def test_regen_reword_exit_rewrites_and_logs(tmp_path, monkeypatch, capsys):
    """「改词再掷」：留痕 regen_reword、新词出站（词图皆错时连词一起换）。"""
    girl = _write_source(tmp_path, "girl.png", b"girl")
    vision = {"红裙女孩":_claims(deviations=[("裙色", "红裙", "宝蓝色长裙")])}
    rewrite_calls = _stub_rewrite(monkeypatch, "人物·红裙女孩专属生图提示词：宝蓝裙")
    tail = [
        "", "1", str(girl),
        "2",                    # 三选：重生成
        "2", "裙子改成宝蓝色",   # 改词再掷→意见
        "4",                    # 新词出站后审查——这张先放弃收尾
        "", "4", "", "4", "4",
        "8", "1",
    ]
    asked, code, _ = _run_loop(tmp_path, monkeypatch, tail, vision=vision)
    assert code == 0
    assert any("改词再掷" in text for text in asked), "重生成菜单缺改词再掷出口"
    rounds = _rounds(tmp_path)
    assert [r["trigger"] for r in rounds] == ["regen_reword"]
    assert rounds[0]["prompt_after"] == "人物·红裙女孩专属生图提示词：宝蓝裙"
    out = capsys.readouterr().out
    assert "人物·红裙女孩专属生图提示词：宝蓝裙" in out, "改词再掷的新词没有出站"


# ---------------------------------------------------------------------------
# AC2：锁死清单接到对照器上——只对身份特征判偏差
# ---------------------------------------------------------------------------

def test_proofread_adjudicates_only_locked_features(tmp_path, monkeypatch, capsys):
    """AC2 的牙齿：锁死清单**真的接到了对照器上**——清单外的出入既不进三选、也不进补漏。

    读图一次交回两条：一条命中清单（裙色）、一条是构图口味级（清单外）。只有前者该占
    一次三选，后者**忽略**——当日现场正是这一档把 39 条偏差里的人裁淹掉的。

    变异检验：把 ``_proofread_one_asset`` 里传给对照器与读图请求的 ``locked`` 去掉，
    这条必须红（构图那条回来占第二次三选，输入脚本随即错位）。
    """
    girl = _write_source(tmp_path, "girl.png", b"girl")
    vision = {"红裙女孩":_claims(deviations=[
        ("裙色", "红裙", "宝蓝色长裙"),                    # 命中锁死清单
        ("构图", "女孩站在秋日庭院里", "女孩几乎站在画面正中"),  # 清单外＝理想描述
    ])}
    tail = [
        "", "1", str(girl),   # 队列菜单→红裙女孩；审查满意；交回路径
        "", "",               # 三选（只该有一次）；改后文案
        "", "4",              # 队列菜单→蓝衫少年；放弃
        "", "4",              # 队列菜单→秋日庭院；放弃
        "4",                  # 青铜钥匙（最后一张自动出）：放弃
        "8", "1",
    ]
    asked, code, _ = _run_loop(tmp_path, monkeypatch, tail, vision=vision)
    assert code == 0
    out = capsys.readouterr().out
    assert sum("裁决：" in text for text in asked) == 1, "偏差三选不止一次（清单外的也进了人裁）"
    assert "[偏差] 1 处" in out, "锁死清单没接到对照器上（清单外的也判成了偏差）"
    assert "[补漏]" not in out, "理想描述的出入被当成了「图里有而描述没有」"
    assert "[忽略] 1 条" in out, "收窄的成果没上屏（忽略了几条看不出来）"
    assert "几乎站在画面正中" not in out, "降级条目没有真的被忽略"


# ---------------------------------------------------------------------------
# AC3：回灌——出下一张的词之前注入前面已定稿素材的画面事实
# ---------------------------------------------------------------------------

def test_settled_align_request_carries_the_settled_facts():
    """请求组装（纯函数，缝）：已定稿素材的**语义名 + 画面事实**逐条进请求。"""
    from minimax_h3_prompt.asset_loop import build_settled_align_request

    request = build_settled_align_request(
        "道具·汽水罐生图提示词", [("红裙女孩", "宝蓝色长裙、黑发"), ("秋日庭院", "满地红叶")])
    assert "【当前提示词】" in request and "道具·汽水罐生图提示词" in request
    for name, desc in (("红裙女孩", "宝蓝色长裙、黑发"), ("秋日庭院", "满地红叶")):
        assert name in request and desc in request, f"已定稿素材 {name!r} 的画面事实没进请求"
    assert "逐字保留" in request, "回灌请求缺「其余逐字保留」的纪律——那会把没提到的细节一并漂走"


def test_align_asset_prompt_refuses_to_lose_a_locked_feature():
    """回灌结果丢了锁死特征 → 响（调用方据此不采用、保持原词）。

    对齐请求的纪律是「只做一致性对齐、其余逐字保留」，丢掉锁死特征就是越权改词；
    照单收下的话，这一张的偏差判据（条目要在正文里找得到）当场失效。
    """
    from minimax_h3_prompt.asset_loop import align_asset_prompt

    llm = SimpleNamespace(invoke=lambda message, config=None: "被改得面目全非的提示词")
    with pytest.raises(ValueError, match="锁死特征"):
        align_asset_prompt("道具·汽水罐生图提示词", [("红裙女孩", "宝蓝色长裙")], llm,
                           locked_features=("汽水罐",))


def test_settled_facts_include_assets_missing_from_the_brief_refs_registry():
    """已定稿素材的取材**不许**被 brief.refs 收窄（issue #53 复审采纳项）。

    ``ref_asset_registry`` 在 ``brief.refs`` 非空时只回用户登记的那几条——拿它当唯一
    取材口，brief 文件路线下「已定稿但用户没登记」的素材就静默不回灌了（回灌的正是
    跨素材一致性，少一条就少一处锚）。已定稿素材的画面事实恒取**合并结果**（素材画面
    事实裁定的首选源，也是 ``ref_asset_registry`` 在册时给的那一份），两处口径一致。
    """
    from minimax_h3_prompt.brief_parser import RefItem
    from minimax_h3_prompt.ui.wizard import _settled_asset_facts

    state = _ref_stage1_state() | {
        "asset_proofreads": {
            "红裙女孩": {"sha256": "ab", "status": "done",
                        "description": "宝蓝色长裙、黑发的红裙女孩", "deviations": [], "omissions": []},
            "蓝衫少年": {"sha256": "cd", "status": "pending_regen",
                        "description": "蓝衫少年描述", "deviations": [], "omissions": []},
        },
    }
    # brief 只登记了别的东西：已定稿的红裙女孩不在册
    brief = Brief(mode="ref", variant="I2VA", duration=30.0, style="写实", plot=TOPIC,
                  refs=[RefItem(picture=1, name="用户登记的场景", description="用户写的描述")])

    facts = _settled_asset_facts(state, brief, exclude="秋日庭院")
    assert ("红裙女孩", "宝蓝色长裙、黑发的红裙女孩") in facts, \
        f"已定稿素材被 brief.refs 收窄掉了：{facts}"
    assert all(name != "蓝衫少年" for name, _ in facts), "没落账的素材不该进回灌取材"


def test_next_prompt_backfills_the_settled_asset_facts(tmp_path, monkeypatch, capsys):
    """AC3：第 2 张出词前，把第 1 张**已定稿**的画面事实注入对齐请求。

    跨素材一致性靠传递、不靠人肉记忆。两条断言都打在真发出去的请求串上：

    - 注入的是**合并结果**（素材画面事实裁定：合并结果 ＞ 生图提示词 ＞ 用户登记描述）——
      红裙女孩这一张裁了「按图改文案」，「红裙」已改成「宝蓝色长裙」，请求里带的必须是
      改后的事实；带生图提示词原件就等于「图跑偏了还照原词对齐」；
    - 第 1 张出词时**不**回灌（还没有已定稿素材，不该白花一次调用）。
    """
    girl = _write_source(tmp_path, "girl.png", b"girl")
    garden = _write_source(tmp_path, "garden.png", b"garden")
    vision = {"红裙女孩":_claims(deviations=[("裙色", "红裙", "宝蓝色长裙")])}
    llm = _FakeLLM()
    tail = [
        "", "1", str(girl),    # 队列菜单→红裙女孩；审查满意；交回路径
        "", "",                # 三选回车=按图改文案；改后文案回车=用图中实见
        "", "1", str(garden),  # 队列菜单→秋日庭院（回灌在此之前）；审查满意；交回
        "", "4",               # 队列菜单→青铜钥匙；放弃
        "4",                   # 蓝衫少年（最后一张自动出）：放弃
        "8", "1",
    ]
    asked, code, _ = _run_loop(tmp_path, monkeypatch, tail, vision=vision, llm=llm)
    assert code == 0

    aligns = [r for r in llm.requests if _ALIGN_HEAD in r]
    assert aligns, "出下一张的词之前没有回灌"
    assert _current_prompt_from(aligns[0]).startswith("场景·秋日庭院"), \
        "第一次回灌对齐的不是第 2 张（<Picture 2>）"
    request = aligns[0]
    assert "红裙女孩" in request and "宝蓝色长裙" in request, \
        "回灌请求里带的不是已定稿素材的合并结果（素材画面事实裁定的优先序）"
    assert "黑发" in request, "合并结果被截成了摘要——回灌要带整条画面事实"
    assert "秋日庭院" in request, "回灌请求里没有当前这张的词，对齐无从落笔"

    # 第 1 张（红裙女孩）出词时还没有已定稿素材，那一次不许回灌——「没有事实也发请求」
    # 的话这里会出现一次「当前词＝红裙女孩」的对齐。
    assert all(not _current_prompt_from(r).startswith("人物·红裙女孩") for r in aligns), \
        "第 1 张出词就回灌了（那时还没有已定稿素材）"


def test_backfill_result_is_reissued_and_not_repeated_after_a_rewrite(tmp_path, monkeypatch, capsys):
    """回灌结果**重新出站**；且改写后的重出**不再回灌一次**（不把人刚改的词按旧事实拽回去）。"""
    girl = _write_source(tmp_path, "girl.png", b"girl")
    llm = _FakeLLM(align_suffix="（已与已定稿素材对齐）")
    tail = [
        "", "1", str(girl),                       # 红裙女孩落账
        "", "3", "改成宝蓝色", "4",                # 秋日庭院：出词后打回改写 → 新词重出 → 放弃
        "", "4", "4",                             # 青铜钥匙、蓝衫少年：放弃
        "8", "1",
    ]
    asked, code, _ = _run_loop(tmp_path, monkeypatch, tail, llm=llm)
    assert code == 0
    # 每张出词前各回灌一次（第 1 张没有已定稿素材，不在此列）；被改写的那张只许一次——
    # 改写成功后的重出是**人刚改的词**，再自动对齐一轮会按旧事实把它拽回去。
    rewritten_aligns = [r for r in llm.requests if _ALIGN_HEAD in r
                        and _current_prompt_from(r).startswith("场景·秋日庭院")]
    assert len(rewritten_aligns) == 1, f"改写后的重出又回灌了一次：{len(rewritten_aligns)}"
    out = capsys.readouterr().out
    assert "（已与已定稿素材对齐）" in out, "回灌后的新词没有重新出站"


# ---------------------------------------------------------------------------
# 放弃后撤登记要问人；循环外换版退回待办；台账漂移照报
# ---------------------------------------------------------------------------

def test_abandon_after_intake_asks_before_withdrawing(tmp_path, monkeypatch, capsys):
    """重生成后弃坑：撤不撤台账登记问人（默认撤＝废图不进片），不静默删凭据。"""
    from minimax_h3_prompt.asset_ledger import load_asset_ledger

    girl = _write_source(tmp_path, "girl.png", b"girl")
    vision = {"红裙女孩":_claims(deviations=[("裙色", "红裙", "宝蓝色长裙")])}
    tail = [
        "", "1", str(girl),      # 交回
        "2",                     # 三选：重生成
        "4",                     # 重生成菜单：显式放弃这张资产（#72 起「3」是 AI 改词）
        "", "4", "", "4", "4",   # 其余三张放弃
        "8", "1",
    ]
    confirm_log: list[str] = []
    asked, code, _ = _run_loop(
        tmp_path, monkeypatch, tail, vision=vision,
        confirms=[("撤下", True), ("视频/音频", False)], confirm_log=confirm_log)
    assert code == 0
    assert any("撤下" in text for text in confirm_log), "撤登记没问人（静默删凭据）"
    ledger_dir = next((tmp_path / "sessions").rglob("asset-ledger.json")).parent
    assert not load_asset_ledger(ledger_dir).entries, "点头撤下后台账还留着这版"


def test_abandon_after_intake_can_keep_the_registration(tmp_path, monkeypatch, capsys):
    """同一个问句答「不撤」：登记照旧保留，屏上明说卡还会接它。"""
    from minimax_h3_prompt.asset_ledger import load_asset_ledger

    girl = _write_source(tmp_path, "girl.png", b"girl")
    vision = {"红裙女孩":_claims(deviations=[("裙色", "红裙", "宝蓝色长裙")])}
    tail = ["", "1", str(girl), "2",
            "4",   # 重生成菜单：显式放弃（#72 起「3」是 AI 改词）
            "", "4", "", "4", "4", "8", "1"]
    _asked, code, _ = _run_loop(
        tmp_path, monkeypatch, tail, vision=vision,
        confirms=[("撤下", False), ("视频/音频", False)])
    assert code == 0
    ledger_dir = next((tmp_path / "sessions").rglob("asset-ledger.json")).parent
    assert [e.name for e in load_asset_ledger(ledger_dir).entries] == ["红裙女孩"]
    assert "登记保留" in capsys.readouterr().out


def test_externally_replaced_asset_returns_to_queue(tmp_path, monkeypatch, capsys):
    """循环外换版（正本 sha 变而裁决没重做）→ 旧裁决不冒充新版：退回待办重新校对。"""
    from minimax_h3_prompt.asset_ledger import AssetSubmission, intake_assets, load_asset_ledger

    girl = _write_source(tmp_path, "girl.png", b"girl")
    tail = ["", "1", str(girl), "", "4", "", "4", "4", "8", "1"]
    _asked, code, calls = _run_loop(tmp_path, monkeypatch, tail)
    assert code == 0 and len(calls) == 1

    # 循环外换版：非交互 intake（on_replace 缺省＝接受替换）把正本换掉
    ledger_dir = next((tmp_path / "sessions").rglob("asset-ledger.json")).parent
    girl2 = _write_source(tmp_path, "girl2.png", b"girl-v2")
    intake_assets(ledger_dir, [AssetSubmission("红裙女孩", "picture", str(girl2))])

    session = load_session(ledger_dir)
    save_session(ledger_dir, session.brief, session.stage_state, status=STATUS_AWAITING_FRAMES)
    session = load_session(ledger_dir)
    monkeypatch.setattr(wizard, "_offer_resume", lambda config: session)
    calls.clear()
    # 续接：红裙女孩重新回到待办（其余三张已落账/放弃）。这张图已入库、裁决钉的是旧
    # sha，续跑（issue #51）认出「已入库但没落定」，问的是「接着校对这一版」，不再重问
    # 审查与路径。
    asked2 = _fake_inputs(monkeypatch, ["", "1", "", "4", "", "4", "4", "8", "1"])
    _fake_confirms(monkeypatch, [("视频/音频", False)])
    wizard.run_wizard(_cfg(tmp_path))
    assert calls, "换版后旧裁决冒充了新版——没有退回重新读图"
    assert any("接着校对这一版" in text for text in asked2), \
        f"续跑没认出这张已入库：{asked2}"


def test_wrong_shaped_loop_state_is_not_laundered():
    """asset_loop 错形**响**（不许被 or {} / or [] 洗成空表悄悄起新账）——本仓会话字段口径。"""
    from minimax_h3_prompt.asset_loop import loop_state

    with pytest.raises(ValueError, match="素材循环进度"):
        loop_state({"asset_loop": ["红裙女孩"]})          # 应为对象
    with pytest.raises(ValueError, match="released"):
        loop_state({"asset_loop": {"released": "红裙女孩"}})  # 应为列表
    with pytest.raises(ValueError, match="current"):
        loop_state({"asset_loop": {"current": 3}})         # 应为语义名

    # 键缺失＝新会话：补出完整形状并**落回 state**（调用方改的那个就是它）
    state: dict = {}
    loop = loop_state(state)
    assert loop == {"released": [], "abandoned": []}
    loop["released"].append("红裙女孩")
    assert state["asset_loop"]["released"] == ["红裙女孩"]


def test_proofread_reads_the_library_copy_through_the_resolver(tmp_path, monkeypatch):
    """读图取的是素材库里的正本（issue #85 统一解析口），生成目录里不再有图片副本。

    这条是「解析口被绕过」的探针：把 ``describe_image`` 的参数改回
    ``generation_dir / entry.path``，它拿到的就是一个不存在的路径——``is_file()``
    当场变红，而不是等到读图服务报「文件不存在」才被发现。
    """
    girl = _write_source(tmp_path, "girl.png", b"girl")
    tail = ["", "1", str(girl), "", "4", "", "4", "4", "8", "1"]
    _asked, code, calls = _run_loop(tmp_path, monkeypatch, tail)
    assert code == 0

    ledger_dir = next((tmp_path / "sessions").rglob("asset-ledger.json")).parent
    ledger = load_asset_ledger(ledger_dir)
    assert calls, "这张图没进读图校对"
    read_path = Path(calls[0])
    assert read_path.is_file(), f"读图拿到的是不存在的路径：{read_path}"
    assert read_path == resolve_asset_path(ledger, ledger.entries[0], session_dir=ledger_dir)
    assert Path(ledger.library_root) in read_path.parents
    assert not (ledger_dir / "assets").exists(), "生成目录里还留着 assets/"
    assert not list(ledger_dir.rglob("*.png")), "生成目录里还留着图片副本"


def test_ledger_drift_is_reported_at_loop_entry(tmp_path, monkeypatch, capsys):
    """台账漂移（正本文件被换而登记没换）在循环入口照报，不静默。"""
    girl = _write_source(tmp_path, "girl.png", b"girl")
    tail = ["", "1", str(girl), "", "4", "", "4", "4", "8", "1"]
    _asked, code, _ = _run_loop(tmp_path, monkeypatch, tail)
    assert code == 0

    # 循环外动手脚：把库里的正本改掉（登记里的 sha 没跟着变 → 漂移）
    ledger_dir = next((tmp_path / "sessions").rglob("asset-ledger.json")).parent
    ledger = load_asset_ledger(ledger_dir)
    entry = ledger.entries[0]
    resolve_asset_path(ledger, entry, session_dir=ledger_dir).write_bytes(b"tampered")

    session = load_session(ledger_dir)
    save_session(ledger_dir, session.brief, session.stage_state, status=STATUS_AWAITING_FRAMES)
    monkeypatch.setattr(wizard, "_offer_resume", lambda config: load_session(ledger_dir))
    _fake_inputs(monkeypatch, ["", "4", "", "4", "4", "8", "1"])
    _fake_confirms(monkeypatch, [("视频/音频", False)])
    capsys.readouterr()
    wizard.run_wizard(_cfg(tmp_path))
    out = capsys.readouterr().out
    assert "[对账]" in out and "正本与记录不符" in out, "台账漂移没在循环入口照报"


def test_regen_reword_cancel_stays_in_menu_and_does_not_log_reroll(tmp_path, monkeypatch, capsys):
    """重掷菜单里选「改词再掷」又留空意见＝取消：留在菜单重选，不许默默当原词再掷。"""
    girl = _write_source(tmp_path, "girl.png", b"girl")
    vision = {"红裙女孩":_claims(deviations=[("裙色", "红裙", "宝蓝色长裙")])}
    tail = [
        "", "1", str(girl),
        "2",        # 三选：重生成
        "2", "",    # 改词再掷 → 空意见（取消）
        "1",        # 留在重掷菜单：这次选原词再掷
        "4",        # 回审查后放弃这张收尾
        "", "4", "", "4", "4",
        "8", "1",
    ]
    asked, code, _ = _run_loop(tmp_path, monkeypatch, tail, vision=vision)
    assert code == 0
    assert "已取消改写" in capsys.readouterr().out, "取消没有可见交代"
    rounds = _rounds(tmp_path)
    assert [r["trigger"] for r in rounds] == ["regen_reroll"], \
        f"取消的改词不该留痕、也不该记成原词再掷：{[r['trigger'] for r in rounds]}"
    assert sum(1 for text in asked if "重生成·红裙女孩" in text) >= 2, \
        "取消改词后没回重掷菜单重选"


# ---------------------------------------------------------------------------
# AC5：画布行与用户选定画布同源
# ---------------------------------------------------------------------------

def test_canvas_rows_follow_user_selection(tmp_path, monkeypatch, capsys):
    """选了非默认画布：出站画布行走 brief.canvases，不再回落默认表。"""
    girl = _write_source(tmp_path, "girl.png", b"girl")
    tail = ["", "1", str(girl), "", "4", "", "4", "4", "8", "1"]
    asked, code, _ = _run_loop(
        tmp_path, monkeypatch, tail,
        canvases={"zimage": (700, 1300)},
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "700 × 1300" in out, "展示画布行没走用户选定"
    assert "640 × 1280" not in out, "回落了默认表"
    # 落盘文件同样同源
    files = list((tmp_path / "sessions").rglob("character-prompt.md"))
    assert files and "700 × 1300" in files[0].read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 外部生成成本（issue #52）：审查菜单第 5 项，可选登记 ComfyUI 耗时/次数
# ---------------------------------------------------------------------------

def _ledger_dir(tmp_path: Path) -> Path:
    return next((tmp_path / "sessions").rglob("asset-ledger.json")).parent


def test_review_menu_records_external_cost_before_intake(tmp_path, monkeypatch, capsys):
    """还没入库时登记：成本先进答案账，交回时随正本一起挂到台账上。

    「可选」是说它可以跳过（留空＝不登记），不是说它没有入口：成本说的就是「这一版图
    是怎么来的」，得和正本、SHA256 同时落盘才不会被劈成两本账。
    """
    from minimax_h3_prompt.resume_point import ANSWERS_KEY

    girl = _write_source(tmp_path, "girl.png", b"girl")
    tail = [
        "",                     # 队列菜单：默认队首（红裙女孩）
        "5",                    # 审查菜单：登记外部生成成本
        "90", "3",              # 耗时 90 秒、3 次
        "1", str(girl),         # 满意→交回
        "", "4", "", "4", "4",  # 其余三张放弃
        "8", "1",               # 视频输出选择器（issue #45）
    ]
    asked, code, _ = _run_loop(tmp_path, monkeypatch, tail)

    assert code == 0
    assert any("5=登记外部生成成本" in text for text in asked), "审查菜单缺外部成本入口"
    entry = load_asset_ledger(_ledger_dir(tmp_path)).get("红裙女孩", "picture")
    assert (entry.external_seconds, entry.external_attempts) == (90.0, 3)
    session = load_session(_ledger_dir(tmp_path))
    answers = session.stage_state.get(ANSWERS_KEY) or {}
    # 键名取唯一生成器（不写字符串前缀）：前缀改名后「查缺席」会静默变空转
    assert not any(key in answers for key in wizard._asset_cost_keys("红裙女孩")), \
        f"入库后待挂成本没作废（答案会冒充事实）：{answers}"


def test_review_menu_records_external_cost_on_an_intaken_asset(tmp_path, monkeypatch, capsys):
    """已入库时登记：直接写台账（不用等下一次交回）——事后补登记就是这个口。"""
    from minimax_h3_prompt.resume_point import ANSWERS_KEY

    vision = {"红裙女孩":_claims(deviations=[("裙色", "红裙", "宝蓝色长裙")])}
    girl = _write_source(tmp_path, "girl.png", b"girl")
    tail = [
        "",                 # 队列菜单：默认队首
        "1", str(girl),     # 审查：满意→交回（已入库）
        "2",                # 偏差裁决：重生成
        "1",                # 重掷菜单：原词再掷 → 外层重出词 → 审查菜单（此时已入库）
        "5", "90", "3",     # 登记外部成本
        "q",                # 先到这里（会话保持可续接）
    ]
    asked, code, _ = _run_loop(tmp_path, monkeypatch, tail, vision=vision)

    assert code == 0
    entry = load_asset_ledger(_ledger_dir(tmp_path)).get("红裙女孩", "picture")
    assert (entry.external_seconds, entry.external_attempts) == (90.0, 3), \
        "已入库的那一版没登记上（或又被顶掉了）"
    session = load_session(_ledger_dir(tmp_path))
    answers = session.stage_state.get(ANSWERS_KEY) or {}
    assert not any(key in answers for key in wizard._asset_cost_keys("红裙女孩")), \
        f"已入库时该直接写台账，不该在答案账里留一手：{answers}"


def test_external_cost_can_be_skipped_by_leaving_both_blank(tmp_path, monkeypatch, capsys):
    """两问都留空＝不登记，也不往台账里塞 0 元行（可选字段的「缺省不填」）。"""
    girl = _write_source(tmp_path, "girl.png", b"girl")
    tail = [
        "", "5", "", "", "1", str(girl),  # 登记入口两问都空 → 照常交回
        "", "4", "", "4", "4", "8", "1",
    ]
    asked, code, _ = _run_loop(tmp_path, monkeypatch, tail)

    assert code == 0
    entry = load_asset_ledger(_ledger_dir(tmp_path)).get("红裙女孩", "picture")
    assert (entry.external_seconds, entry.external_attempts) == (0.0, 0)


# ---------------------------------------------------------------------------
# 素材循环的进展可见性（issue #71 / ADR 0019）
#
# 用户实测的三处静默：`[读图校对]` 之后读图不发声、敲完「下一个出谁」后先静默跑
# 回灌、素材改写没有任何反馈。断言只看**终端上的行与顺序**——横幅回答「在干什么」，
# 心跳回答「还活着吗」，两者缺一都算回到静默。
# ---------------------------------------------------------------------------

class _StreamingVisionModel:
    """假视觉模型：喂首字节（心跳的来源）并回一份无偏差读数。"""

    def __init__(self, reading: str = '{"claims": []}') -> None:
        self.reading = reading
        self.calls = 0

    def invoke(self, request, config=None):
        self.calls += 1
        for handler in (config or {}).get("callbacks") or []:
            handler.on_llm_new_token("画面")
        return AIMessage(content=self.reading)


def _write_png(tmp_path: Path, name: str) -> Path:
    """真实读图要真图片（describe_image 会先压缩）：交回的源文件必须是合法 PNG。"""
    from PIL import Image

    path = tmp_path / "returned" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (16, 16), (10, 20, 30)).save(path)
    return path


def test_choice_is_followed_by_align_then_prompt_banner(tmp_path, monkeypatch, capsys):
    """敲完「下一个出谁」后先是「正在与前序素材对齐」，随后是「正在生成生图提示词」。

    这两行之间还要**有心跳**：回灌是一次直调，横幅只说明开始了，事件才证明它活着
    （否则用户面对的仍是「打完一行字之后干等」）。
    """
    girl = _write_png(tmp_path, "girl.png")
    garden = _write_png(tmp_path, "garden.png")
    tail = [
        "", "1", str(girl),      # 红裙女孩：落账（第 1 张没有前序，对齐是空转）
        "",                      # 队列菜单：秋日庭院
        "1", str(garden),        # 回灌→出词→审查满意→交回
        "", "4",                 # 队列菜单：青铜钥匙 → 放弃
        "4",                     # 蓝衫少年（最后一张自动出）：放弃
        "8", "1",
    ]
    asked, code, _ = _run_loop(tmp_path, monkeypatch, tail)
    assert code == 0

    out = capsys.readouterr().out
    prompt_at = out.index("当前待办：秋日庭院")
    align_at = out.rindex("▶ 正在与前序素材对齐", 0, prompt_at)
    generate_at = out.rindex("▶ 正在生成生图提示词", 0, prompt_at)
    assert align_at < generate_at < prompt_at, \
        "选择之后的反馈顺序不对：应先「对齐」再「出词」"
    gap = out[align_at:generate_at]
    assert "asset_align" in gap and "已有响应" in gap, \
        f"对齐期间没有心跳（打了横幅却听不到事件）：\n{gap}"


def test_read_proofread_has_step_banner_and_heartbeat(tmp_path, monkeypatch, capsys):
    """``[读图校对]`` 之后必须是「在读图」的横幅＋心跳，而不是一行字之后的白等。

    走**真实** ``describe_image``（只换视觉模型）：注册表最后一道题就是它发出的
    首字节事件有没有人打印。
    """
    girl = _write_png(tmp_path, "girl.png")
    tail = ["", "1", str(girl), "", "4", "", "4", "4", "8", "1"]
    model = _StreamingVisionModel()
    _asked, code, _ = _run_loop(tmp_path, monkeypatch, tail, vision_model=model)
    assert code == 0
    assert model.calls == 1

    out = capsys.readouterr().out
    read_at = out.index("[读图校对] 红裙女孩")
    banner_at = out.index("▶ 正在读图校对", read_at)
    heart_at = out.index("已有响应", banner_at)
    assert banner_at < heart_at, "读图横幅之后没有心跳"
    assert "vision" in out[banner_at:heart_at], "心跳不是读图那一次发出的"


def test_asset_rewrite_has_step_banner_and_heartbeat(tmp_path, monkeypatch, capsys):
    """素材改写（审查打回选 3）同样有横幅与心跳——它也是一次直调。"""
    girl = _write_png(tmp_path, "girl.png")
    tail = [
        "", "1", str(girl),                 # 红裙女孩落账
        "", "3", "改成宝蓝色", "4",          # 秋日庭院：打回改写 → 重出 → 放弃
        "", "4", "4",                       # 其余两张放弃
        "8", "1",
    ]
    _asked, code, _ = _run_loop(tmp_path, monkeypatch, tail, llm=_FakeLLM())
    assert code == 0

    out = capsys.readouterr().out
    rewrite_at = out.index("▶ 正在改写生图提示词")
    log_at = out.index("[改写·秋日庭院] 已记入轮次账", rewrite_at)
    gap = out[rewrite_at:log_at]
    assert "asset_rewrite" in gap and "已有响应" in gap, \
        f"改写期间没有横幅/心跳：\n{gap}"

# 票 7/9（issue #72）：重生成菜单的「AI 按偏差改词」与重画轮次提示
# ---------------------------------------------------------------------------

def _girl_entry(state: dict) -> dict:
    """从最终 state 取红裙女孩那一条资产条目（词有没有被换掉，以它为准）。"""
    return next(e for e in state["character_image_prompts"] if e["name"] == "红裙女孩")


def test_regen_menu_offers_four_choices_plus_q(tmp_path, monkeypatch, capsys):
    """菜单四项 + q（AC1）：旧的「3=显式放弃」让位给「3=AI 按偏差改词」，「4」才是放弃。

    变异检验：菜单退回三项（丢掉 AI 改词那一行）→ 本条与后面三条 AI 改词用例一起变红；
    「4」不再是放弃 → 本条收尾的输入错位、code 非 0。
    """
    girl = _write_source(tmp_path, "girl.png", b"girl")
    vision = {"红裙女孩":_claims(deviations=[("裙色", "红裙", "宝蓝色长裙")])}
    tail = [
        "", "1", str(girl),
        "2",          # 三选：重生成
        "4",          # 重生成菜单：显式放弃
        "", "4", "", "4", "4",
        "8", "1",
    ]
    asked, code, _ = _run_loop(tmp_path, monkeypatch, tail, vision=vision)

    assert code == 0
    menu = next(text for text in asked if "重生成·红裙女孩" in text)
    for entry in ("1=原词再掷", "2=改词再掷", "3=AI 按偏差改词", "4=显式放弃", "q=先到这里"):
        assert entry in menu, f"重生成菜单缺 {entry!r}：{menu}"
    assert not _rounds(tmp_path), "显式放弃不入轮次账"


def test_ai_reword_rewrites_by_all_deviations_and_logs_a_new_trigger(
        tmp_path, monkeypatch, capsys):
    """「AI 按偏差改词」（AC2/AC3）：输入四件齐、方向是强化原意、成功落新触发口。

    第二条偏差（裙长）**还没来得及裁**就被 voided——AI 改词仍必须看到它：偏差清单取
    本轮的**全部**条目，不是「已裁过的那几条」（票面「全部偏差条目」）。
    """
    girl = _write_source(tmp_path, "girl.png", b"girl")
    vision = {"红裙女孩":_claims(deviations=[
        ("裙色", "红裙", "宝蓝色长裙"),   # 人裁：重生成
        ("裙长", "红裙", "及踝长裙"),     # 还没轮到裁，deviation 已判出
    ])}
    rewritten = "人物·红裙女孩专属生图提示词：红裙，及踝长度，裙面为宝蓝色"
    llm = _FakeLLM(ai_reword_reply=rewritten)
    captured: dict = {}
    tail = [
        "", "1", str(girl),
        "2",           # 三选（裙色）：重生成
        "3",           # 重生成菜单：AI 按偏差改词
        "4",           # 新词出站后的审查：放弃这张收尾
        "", "4", "", "4", "4",
        "8", "1",
    ]
    asked, code, _ = _run_loop(tmp_path, monkeypatch, tail, vision=vision, llm=llm,
                               captured=captured)

    assert code == 0
    assert any("3=AI 按偏差改词" in text for text in asked), "重生成菜单缺 AI 改词"
    request = next(text for text in llm.requests if _AI_REWORD_HEAD in text)
    # 输入四件：当前词、全部偏差（描述 × 图中实见）、锁死清单、生图提示词纪律
    assert GIRL_PROMPT in request, "当前提示词没进 AI 改词请求"
    for piece in ("裙色", "红裙", "宝蓝色长裙", "裙长", "及踝长裙"):
        assert piece in request, f"偏差条目 {piece!r} 没进 AI 改词请求"
    assert "【锁死清单】" in request and "- 红裙女孩" in request, "锁死清单没进 AI 改词请求"
    # 纪律取 #68 那份单一来源（独立字面值，不从 discipline_text() 现算）
    assert "状态翻译成可见证据" in request, "生图提示词纪律没进 AI 改词请求"
    # 方向：强化原意，不是迁就成图
    assert "不得改成图里的样子" in request, "AI 改词请求没写明方向（会把图当设计改）"
    assert "逐字保留" in request, "AI 改词请求缺「其余正文逐字保留」的纪律"
    # 成功：写回 + 落一行新触发口（与 review_reword/regen_reword/regen_reroll 并列）
    rounds = _rounds(tmp_path)
    assert [r["trigger"] for r in rounds] == ["regen_ai_reword"]
    assert rounds[0]["prompt_before"] == GIRL_PROMPT
    assert rounds[0]["prompt_after"] == rewritten
    assert "裙色" in rounds[0]["feedback"] and "裙长" in rounds[0]["feedback"], \
        "轮次账没记下这一轮按哪几处偏差改的词"
    assert "及踝长度" in capsys.readouterr().out, "AI 改词后的完整提示词没出站"
    # 不存在自动「按图改文案」：素材画面事实仍是原词，记录仍停在 pending_regen
    record = captured["state"]["asset_proofreads"]["红裙女孩"]
    assert record["status"] == "pending_regen"
    assert record["description"] == GIRL_PROMPT, "AI 改词悄悄把素材画面事实按图改了"


def test_ai_reword_failure_keeps_the_prompt_and_stays_in_the_menu(
        tmp_path, monkeypatch, capsys):
    """AI 改词失败（AC4）：提示词保持原样、不留痕，**留在重生成菜单**——不许静默换词。"""
    girl = _write_source(tmp_path, "girl.png", b"girl")
    vision = {"红裙女孩":_claims(deviations=[("裙色", "红裙", "宝蓝色长裙")])}
    llm = _FakeLLM(ai_reword_error=RuntimeError("网关 500"))
    captured: dict = {}
    tail = [
        "", "1", str(girl),
        "2",           # 三选：重生成
        "3",           # AI 改词：失败
        "4",           # 仍留在菜单——这次显式放弃收尾
        "", "4", "", "4", "4",
        "8", "1",
    ]
    asked, code, _ = _run_loop(tmp_path, monkeypatch, tail, vision=vision, llm=llm,
                               captured=captured)

    assert code == 0
    assert sum(1 for text in asked if "重生成·红裙女孩" in text) >= 2, \
        "AI 改词失败后没回到重生成菜单（人被静默推出去了）"
    out = capsys.readouterr().out
    assert "网关 500" in out and "提示词保持原样" in out, "失败没给可读病因"
    assert not _rounds(tmp_path), "失败的 AI 改词不许留痕"
    assert _girl_entry(captured["state"])["positive_prompt"] == GIRL_PROMPT, \
        "失败的 AI 改词把提示词换掉了"


def test_ai_reword_that_drops_a_locked_feature_is_rejected(tmp_path, monkeypatch, capsys):
    """AI 改词结果丢了锁死特征 → 不采用（AC2「锁死清单逐字保留」的牙齿）。

    锁死清单是读图校对判偏差的判据：条目不在正文里，这一张的偏差再也判不出来
    （静默失效）。照单收下比直接失败更坏，所以按失败处理——词不动、留在菜单。
    """
    girl = _write_source(tmp_path, "girl.png", b"girl")
    vision = {"红裙女孩":_claims(deviations=[("裙色", "红裙", "宝蓝色长裙")])}
    # 「红裙女孩」是这一张的锁死特征；回复里整个丢了它
    llm = _FakeLLM(ai_reword_reply="人物专属生图提示词：宝蓝色长裙")
    captured: dict = {}
    tail = [
        "", "1", str(girl),
        "2", "3",      # 三选：重生成 → AI 改词（结果丢锁死特征）
        "4",           # 留在菜单 → 放弃收尾
        "", "4", "", "4", "4",
        "8", "1",
    ]
    asked, code, _ = _run_loop(tmp_path, monkeypatch, tail, vision=vision, llm=llm,
                               captured=captured)

    assert code == 0
    out = capsys.readouterr().out
    assert "丢了锁死特征" in out and "提示词保持原样" in out
    assert not _rounds(tmp_path), "被拒的 AI 改词不许留痕"
    assert _girl_entry(captured["state"])["positive_prompt"] == GIRL_PROMPT
    assert sum(1 for text in asked if "重生成·红裙女孩" in text) >= 2, "失败后没留在菜单"


def test_regen_rounds_are_counted_from_the_round_ledger(tmp_path):
    """重画轮次只从**资产轮次账**推导（AC5）；「审查打回改写」不是重画，不计入。"""
    from minimax_h3_prompt.asset_loop import (
        TRIGGER_REGEN_AI_REWORD,
        TRIGGER_REGEN_REROLL,
        TRIGGER_REGEN_REWORD,
        TRIGGER_REVIEW_REWORD,
        count_regen_rounds,
        log_asset_round,
        render_regen_hint,
    )

    for trigger in (TRIGGER_REVIEW_REWORD, TRIGGER_REGEN_REROLL,
                    TRIGGER_REGEN_REWORD, TRIGGER_REGEN_AI_REWORD):
        log_asset_round(tmp_path, name="红裙女孩", kind="character", trigger=trigger,
                        feedback="", prompt_before="a", prompt_after="b")
    # 别的资产的轮次不算到这一张头上
    log_asset_round(tmp_path, name="蓝衫少年", kind="character", trigger=TRIGGER_REGEN_REROLL,
                    feedback="", prompt_before="a", prompt_after="b")

    assert count_regen_rounds(tmp_path, name="红裙女孩") == 3
    assert count_regen_rounds(tmp_path, name="蓝衫少年") == 1
    assert count_regen_rounds(tmp_path, name="秋日庭院") == 0

    assert render_regen_hint(0) == "", "0 轮不该加噪音"
    hint = render_regen_hint(2)
    assert "本张已重画 2 轮" in hint and "可按图改文案保留这一版" in hint


def test_regen_menu_and_proofread_report_show_the_regen_round_hint(
        tmp_path, monkeypatch, capsys):
    """重画一轮后再校对（AC5）：**校对报告**与**重生成菜单**都提示已重画 1 轮。

    判据落在「同一条偏差第二次交回」上：第一轮没重画过（不该出现提示），第二轮是
    重画后的图（该出现，且只出现两处——报告一处、菜单一处）。
    """
    girl = _write_source(tmp_path, "girl.png", b"girl")
    vision = {"红裙女孩":_claims(deviations=[("裙色", "红裙", "宝蓝色长裙")])}
    tail = [
        "", "1", str(girl),
        "2",             # 三选：重生成
        "",              # 重掷菜单：原词再掷（这一轮落了账）
        "1", str(girl),  # 审查：满意→交回重画后的图
        "2",             # 第二次三选：重生成
        "q",             # 第二次重掷菜单：先到这里
    ]
    asked, code, _ = _run_loop(tmp_path, monkeypatch, tail, vision=vision)

    assert code == 0
    out = capsys.readouterr().out
    assert [r["trigger"] for r in _rounds(tmp_path)] == ["regen_reroll"]
    assert "本张已重画 1 轮" in out, "校对报告处没提示重画轮次"
    hinted_menus = [t for t in asked if "本张已重画 1 轮" in t]
    assert len(hinted_menus) == 1, f"重生成菜单处的轮次提示不对：{hinted_menus}"
    assert out.count("本张已重画 1 轮") == 1, "第一轮（0 轮）就出现了提示"
    assert "可按图改文案保留这一版" in hinted_menus[0], "提示没给「按图改文案」这条退路"


def test_ai_reword_output_is_rechecked_against_the_discipline(tmp_path, monkeypatch, capsys):
    """AI 改词也是一次出词：结果照跑警告级纪律检查（#68 / ADR 0018）——只警告、不阻断。"""
    girl = _write_source(tmp_path, "girl.png", b"girl")
    vision = {"红裙女孩":_claims(deviations=[("裙色", "红裙", "宝蓝色长裙")])}
    # 含负向表述（Turbo 上是空动作）——改写结果照样写回，但要有一行警告
    llm = _FakeLLM(ai_reword_reply="人物·红裙女孩专属生图提示词：宝蓝色长裙，不要阴影")
    tail = [
        "", "1", str(girl),
        "2", "3",      # 三选：重生成 → AI 改词（结果违反纪律）
        "4",           # 放弃收尾
        "", "4", "", "4", "4",
        "8", "1",
    ]
    _asked, code, _ = _run_loop(tmp_path, monkeypatch, tail, vision=vision, llm=llm)

    assert code == 0
    out = capsys.readouterr().out
    assert "不要阴影" in out, "改写结果没写回出站"
    assert "[纪律警告" in out and "负向表述" in out, "AI 改词结果没跑纪律检查"


def test_ai_reword_goes_through_the_direct_call_watchdog(tmp_path, monkeypatch, capsys):
    """AI 改词是**直调点**：请求走 ``run_direct_call``，等待事件与角色调用同形（ADR 0019）。

    ADR 0019 的裁定是「所有直接 ``llm.invoke`` 改走它」；AI 改词是这条规则落地之后新增
    的直调点，裸调就会成为新的「规则在、接线不在」——用户等这一轮时看不到首字节，
    卡住也没人救。变异检验：把 ``ai_reword_asset_prompt`` 改回裸 ``llm.invoke(request)``，
    本条报红（没有 agent_start/agent_wait 事件，假模型也收不到 callbacks）。
    """
    from minimax_h3_prompt.observability import reporter

    girl = _write_source(tmp_path, "girl.png", b"girl")
    vision = {"红裙女孩":_claims(deviations=[("裙色", "红裙", "宝蓝色长裙")])}
    llm = _FakeLLM(ai_reword_reply="人物·红裙女孩专属生图提示词：红裙，及踝长度，宝蓝色裙面")
    events: list[dict] = []
    reporter.subscribe(events.append)
    try:
        _asked, code, _ = _run_loop(tmp_path, monkeypatch, [
            "", "1", str(girl),
            "2", "3",      # 三选：重生成 → AI 改词
            "4",           # 放弃收尾
            "", "4", "", "4", "4",
            "8", "1",
        ], vision=vision, llm=llm)
    finally:
        reporter.unsubscribe(events.append)

    assert code == 0
    role = "role_asset_ai_reword"
    assert [e for e in events if e.get("type") == "agent_start" and e.get("role") == role], \
        "AI 改词没走直调入口（没有 agent_start）"
    waits = [e for e in events if e.get("type") == "agent_wait" and e.get("role") == role]
    assert "first_byte" in [e.get("state") for e in waits], \
        "AI 改词等待期间没有首字节心跳（看不见它在动）"
    assert [e for e in events if e.get("type") == "agent_done" and e.get("role") == role], \
        "AI 改词没有收尾事件（耗时与 token 没进账）"
    # 入口把回调递给了模型：没有它，上面的首字节事件根本发不出来
    assert any((config or {}).get("callbacks") for config in llm.configs), \
        "假模型没收到 callbacks——AI 改词仍是裸调"
    # 成功文案不许超证据（复审 S5）：代码只**确定性核对**了锁死清单（逐条子串检查），
    # 「其余正文逐字保留」是模型该守的纪律，系统没有核对过，不能对用户这么保证。
    out = capsys.readouterr().out
    assert "锁死清单已逐条核对；其余正文请人工确认" in out
    assert "其余正文逐字保留" not in out, "成功文案仍在对用户保证系统没核对过的部分"

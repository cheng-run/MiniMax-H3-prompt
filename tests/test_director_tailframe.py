"""导演台尾帧变换提示词（issue #107）：三段式产出、触发集合、确定性检查与落盘。

缝（与票面 AC 一一对应，均为**外部可观察行为**）：

1. 只在运镜段（``camera_same`` / ``camera_cut``）与收尾定格段（``ending``）触发；
2. 产出「变换指令 / 保留项声明 / 锁死清单」三段，段头由代码常量拼；
3. 产出后跑 ``tools/image_prompt_discipline`` 口径的确定性检查，违规是**告警**、
   不阻断落盘；
4. 用固定 fixture 帧图演示，不依赖真实跑过的段、不跑 ComfyUI、不联网。

**变异检验**（「永不为红＝没有」）：改掉给图契约 → 触发集合跟着变；改掉段头常量 →
校验器跟着变。两条各有一条第 2 组断言钉住。这两条不是自证——它们**故意**扰动来源，
拿旧实现跑会红（见各用例 docstring）。
"""
from __future__ import annotations

import numpy as np
import pytest

cv2 = pytest.importorskip("cv2")


# ---------------------------------------------------------------------------
# fixture：段表（帧为真源，ADR 0025 的 107 + 4×153 = 719）与固定帧图
# ---------------------------------------------------------------------------

def _segment(index, segment_type, start, end, declared, export, guided):
    from minimax_h3_prompt.director.frames import DirectorSegment

    return DirectorSegment(
        index=index, type=segment_type, guided=guided,
        start_frame=start, end_frame=end,
        declared_frames=declared, export_frames=export,
        summary=f"段 {index} 剧情", end_hook=f"段 {index} 落定画面：人物转过身去",
        give_images=(),
    )


def _plan(mode: str = "i2v"):
    from minimax_h3_prompt.director.frames import DirectorPlan

    segments = (
        _segment(1, "opening", 0, 107, 107, 107, False),
        _segment(2, "continue", 107, 260, 141, 153, True),
        _segment(3, "camera_same", 260, 413, 141, 153, True),
        _segment(4, "camera_cut", 413, 566, 141, 153, False),
        _segment(5, "ending", 566, 719, 141, 153, True),
    )
    return DirectorPlan(mode=mode, total_frames=719, segments=segments)


def _frame_png(path):
    """固定 fixture 帧图：一张 32×32 的灰度渐变，只用来当「起点画面」路径。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    image = np.zeros((32, 32, 3), dtype=np.uint8)
    image[:, :] = 60
    image[8:24, 8:24] = 200
    assert cv2.imwrite(str(path), image), f"写不出 fixture 帧图：{path}"
    return path


# 模型**该写的**形态：三段式、段头逐字、锁死清单条目是正文原文片段
# （刻意避开负向词与无锚方位词，让「干净产出」这条基线不掺警告噪声）。
_TAILFRAME_TEXT = """### 变换指令
画面正中的人物由正面转为背面站立，双肩与后颈露出，灰色风衣的背部有一道深色缝线。玻璃门保持在便利店里侧，门内冷白灯管照亮积水地面。

### 保留项声明
画面背景是雨夜便利店门前的人行道，地面积水反射冷白灯光。伞架旁立着一把黑色长柄伞。人物身上是灰色风衣，风衣背部有一道深色缝线。

### 锁死清单
- 灰色风衣
- 深色缝线
- 雨夜便利店门前的人行道
"""


class _StubLLM:
    """按次序返回给定回复的假模型（不联网）。"""

    def __init__(self, replies):
        self.replies = list(replies)
        self.requests: list[str] = []

    def invoke(self, request, config=None):
        self.requests.append(request)
        reply = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        if isinstance(reply, Exception):
            raise reply
        return reply


def _stub_vision(monkeypatch, description="画中人物穿灰色风衣，站在便利店门前。",
                 recorder=None):
    """读图唯一口的桩：记录调用次数，不联网。"""
    from minimax_h3_prompt.director import tailframe

    calls: list[object] = []

    def fake_describe(path, instruction=None):
        calls.append((path, instruction))
        if recorder is not None:
            recorder.append(path)
        return description

    monkeypatch.setattr(tailframe, "describe_image", fake_describe)
    return calls


# ---------------------------------------------------------------------------
# AC2：只在运镜段与收尾定格段触发
# ---------------------------------------------------------------------------

def test_tailframe_triggers_only_for_moving_and_ending_segments():
    """五种段类型逐一核对：开场/续接不产，两种运镜与收尾定格要产。"""
    from minimax_h3_prompt.director import frames, tailframe

    triggered = [t for t in frames.SEGMENT_TYPES if tailframe.needs_tailframe(t)]
    assert triggered == ["camera_same", "camera_cut", "ending"]

    with pytest.raises(ValueError, match="unknown_type"):
        tailframe.needs_tailframe("unknown_type")


def test_trigger_set_follows_the_give_image_contract(monkeypatch):
    """变异检验：把「续接段也要终点画面」喂进给图契约，触发集合必须跟着变。

    触发集合不是手抄的字面量，而是从 ``frames.give_images_for`` 现算的——
    另写一份会各自漂的集合，改段类型规则时这里就静默失效（本仓吃过多次）。
    """
    from minimax_h3_prompt.director import frames, tailframe

    assert "continue" not in tailframe.tailframe_segment_types()
    original = frames.give_images_for
    monkeypatch.setattr(
        tailframe, "give_images_for",
        lambda segment_type, mode: ((frames.IMAGE_END,) if segment_type == "continue"
                                    else original(segment_type, mode)))
    assert "continue" in tailframe.tailframe_segment_types()


# ---------------------------------------------------------------------------
# AC1：三段式产出（段头由代码常量拼，模型只管正文）
# ---------------------------------------------------------------------------

def test_request_carries_the_reference_frame_and_the_three_section_heads():
    """请求下发起点画面、读图结果、落定画面与三个段头（段头从常量派生）。"""
    from minimax_h3_prompt.director import tailframe

    segment = _plan().segments[2]
    request = tailframe.build_tailframe_request(
        segment, start_image="GEN001/director/start-03.png",
        start_frame_description="画中人物穿灰色风衣，站在便利店门前。")

    for head in tailframe.tailframe_sections():
        assert head in request, f"请求里缺段头：{head}"
    assert "GEN001/director/start-03.png" in request
    assert "画中人物穿灰色风衣" in request
    assert segment.end_hook in request
    assert tailframe.segment_type_label(segment.type) in request
    # 纪律文本按档现取（图生图与资产图共用同一份纪律真源）
    assert "生图提示词纪律" in request


def test_non_triggering_segment_refuses_to_build_a_request():
    """开场段要尾帧变换请求是调用方接错了：当场报错，不产半份文档。"""
    from minimax_h3_prompt.director import tailframe

    with pytest.raises(ValueError, match="开场|不需要|opening"):
        tailframe.build_tailframe_request(
            _plan().segments[0], start_image="x.png", start_frame_description="…")


def test_normalize_reassembles_sections_in_canonical_order():
    """模型把三段写乱序、带前言：归一后按常量顺序重排、前言丢掉。"""
    from minimax_h3_prompt.director import tailframe

    shuffled = (
        "说明：下面是三段。\n\n"
        "### 锁死清单\n- 灰色风衣\n- 深色缝线\n\n"
        "### 变换指令\n" + _TAILFRAME_TEXT.split("### 变换指令\n")[1].split("\n\n### 保留项声明")[0]
        + "\n\n### 保留项声明\n"
        + _TAILFRAME_TEXT.split("### 保留项声明\n")[1].split("\n\n### 锁死清单")[0] + "\n"
    )
    text = tailframe.normalize_tailframe_output(shuffled)

    heads = [line for line in text.splitlines() if line.startswith("### ")]
    assert heads == list(tailframe.tailframe_sections())
    assert "说明：下面是三段。" not in text
    assert tailframe.split_tailframe_sections(text)[tailframe.LOCKED_HEAD].strip() == (
        "- 灰色风衣\n- 深色缝线")


def test_normalize_returns_raw_text_when_no_section_parses():
    """一段都没解析出来时原样交回：不拼假内容，由校验器报缺段。"""
    from minimax_h3_prompt.director import tailframe

    assert tailframe.normalize_tailframe_output("人物转过身去。") == "人物转过身去。"


# ---------------------------------------------------------------------------
# AC3：确定性检查（违规有告警、不阻断产出）
# ---------------------------------------------------------------------------

def test_clean_tailframe_prompt_has_no_issues():
    from minimax_h3_prompt.director import tailframe

    assert tailframe.validate_tailframe_prompt(_TAILFRAME_TEXT) == []


def test_discipline_violations_are_warnings_not_blocking(tmp_path, monkeypatch):
    """生图纪律违规（负向表述）只报 warning：尾帧仍然产出、仍然落盘。"""
    from minimax_h3_prompt.director import tailframe

    violating = _TAILFRAME_TEXT.replace(
        "玻璃门保持在便利店里侧", "玻璃门不要移动，保持在便利店里侧")
    issues = tailframe.validate_tailframe_prompt(violating)
    negatives = [i for i in issues if i.code == "NEGATIVE_STATEMENT"]
    assert negatives and all(i.severity == "warning" for i in negatives)

    _stub_vision(monkeypatch)
    plan = _plan()
    written = tailframe.write_tailframe_prompts(
        plan, llm=_StubLLM([violating]), session_dir=tmp_path / "GEN001",
        start_image=_frame_png(tmp_path / "start.png"))
    ending = [item for item in written if item.index == 5][0]
    assert ending.path.is_file(), "告警不阻断产出"
    assert any(i.code == "NEGATIVE_STATEMENT" for i in ending.issues)
    assert not ending.errors, "纪律违规不是 error"


def test_locked_entries_must_be_verbatim_body_fragments():
    """锁死清单条目必须是正文原文片段（ADR 0016 的不变式）：转述条目要报错。"""
    from minimax_h3_prompt.director import tailframe

    paraphrased = _TAILFRAME_TEXT.replace("- 灰色风衣", "- 一件穿在身上的灰色风衣")
    errors = [i for i in tailframe.validate_tailframe_prompt(paraphrased)
              if i.code == "TAILFRAME_LOCKED_NOT_VERBATIM"]
    assert errors, "转述的条目必须被点名（否则判据永不复用）"


def test_missing_and_empty_sections_are_errors():
    from minimax_h3_prompt.director import tailframe

    without_locked = _TAILFRAME_TEXT.split("### 锁死清单")[0]
    codes = {i.code for i in tailframe.validate_tailframe_prompt(without_locked)}
    assert "TAILFRAME_SECTION_MISSING" in codes

    empty_preserve = _TAILFRAME_TEXT.replace(
        _TAILFRAME_TEXT.split("### 保留项声明\n")[1].split("\n\n### 锁死清单")[0], "")
    codes = {i.code for i in tailframe.validate_tailframe_prompt(empty_preserve)}
    assert "TAILFRAME_SECTION_EMPTY" in codes

    empty_locked = _TAILFRAME_TEXT.split("### 锁死清单")[0] + "### 锁死清单\n"
    codes = {i.code for i in tailframe.validate_tailframe_prompt(empty_locked)}
    assert "TAILFRAME_LOCKED_EMPTY" in codes


def test_section_heads_are_a_single_source(monkeypatch):
    """变异检验：改掉段头常量，校验器必须跟着改口（重抄字面就再也咬不住）。"""
    from minimax_h3_prompt.director import tailframe

    assert not [i for i in tailframe.validate_tailframe_prompt(_TAILFRAME_TEXT)
                if i.severity == "error"]
    monkeypatch.setattr(tailframe, "PRESERVE_HEAD", "### 保留说明")
    errors = [i for i in tailframe.validate_tailframe_prompt(_TAILFRAME_TEXT)
              if i.code == "TAILFRAME_SECTION_MISSING"]
    assert errors and "### 保留说明" in errors[0].message


def test_instruction_file_names_every_section_derived_from_constants():
    """指令文件与小节名同源：改段头名却忘了改指令，这条要红。"""
    from minimax_h3_prompt.director import tailframe

    instruction = tailframe.load_instruction()
    for head in tailframe.tailframe_sections():
        assert head.removeprefix(tailframe.SECTION_PREFIX) in instruction


# ---------------------------------------------------------------------------
# 落盘：GENxxx/director/tailframe-NN.md（只给触发的段写）
# ---------------------------------------------------------------------------

def test_write_tailframe_prompts_only_touches_triggered_segments(tmp_path, monkeypatch):
    """五段段表只产出 3 份：运镜同场景 / 运镜切镜 / 收尾定格，编号与段号一致。"""
    from minimax_h3_prompt.director import tailframe

    described: list[object] = []
    _stub_vision(monkeypatch, recorder=described)
    start = _frame_png(tmp_path / "start.png")
    llm = _StubLLM([_TAILFRAME_TEXT])

    written = tailframe.write_tailframe_prompts(
        _plan(), llm=llm, session_dir=tmp_path / "GEN001", start_image=start)

    assert [(item.index, item.type) for item in written] == [
        (3, "camera_same"), (4, "camera_cut"), (5, "ending")]
    for item in written:
        assert item.path == tailframe.tailframe_path(tmp_path / "GEN001", item.index)
        assert item.path.is_file()
        assert item.path.read_text(encoding="utf-8").strip() == item.text
        assert item.issues == ()
        assert item.transform.startswith("画面正中的人物由正面转为背面")
        assert "雨夜便利店门前的人行道" in item.preserved
        assert item.locked == ("灰色风衣", "深色缝线", "雨夜便利店门前的人行道")
    assert not (tmp_path / "GEN001" / "director" / "tailframe-01.md").exists()
    assert not (tmp_path / "GEN001" / "director" / "tailframe-02.md").exists()
    # 同一张固定帧图只读一次（演示用；生产里每段起点画面不同）
    assert len(described) == 1
    assert str(start) in llm.requests[0]


def test_write_uses_the_unique_direct_write_exit_with_its_own_role(monkeypatch):
    """ADR 0019：写尾帧提示词要有独立角色名，并与写段角色分开；直调只走那个出口。"""
    from minimax_h3_prompt.director import prompts, tailframe

    seen = {}

    def fake_direct_write(llm, request, *, role, label):
        seen.update(role=role, label=label)
        return _TAILFRAME_TEXT

    monkeypatch.setattr(tailframe, "_direct_write", fake_direct_write)
    segment = _plan().segments[2]
    text = tailframe.write_tailframe_prompt(
        segment, llm=object(), start_image="start.png", start_frame_description="…")

    assert text.strip() == _TAILFRAME_TEXT.strip()
    assert seen["role"] == tailframe.DIRECTOR_TAILFRAME_WRITER_ROLE
    assert seen["role"] != prompts.DIRECTOR_SEGMENT_WRITER_ROLE
    assert "尾帧" in seen["label"] and "3" in seen["label"]


def test_write_retries_with_the_issue_list_then_gives_up_loudly(monkeypatch):
    """结构不合格带问题清单重写一次；仍不合格交回原文（落盘），不装死。"""
    from minimax_h3_prompt.director import tailframe

    _stub_vision(monkeypatch)
    llm = _StubLLM(["人物转过身去。"])  # 一段都没解析出来
    text = tailframe.write_tailframe_prompt(
        _plan().segments[2], llm=llm, start_image="start.png",
        start_frame_description="…")

    assert text == "人物转过身去。"
    assert len(llm.requests) == 2, "第一次不合格要重写一次"
    assert "TAILFRAME_SECTION_MISSING" in llm.requests[1]


def test_write_fails_loudly_when_reference_frame_is_missing(tmp_path):
    """起点画面不存在＝参考图没了：当场报错并点名路径，不写半份文档。"""
    from minimax_h3_prompt.director import tailframe

    with pytest.raises(ValueError, match="起点画面"):
        tailframe.write_tailframe_prompts(
            _plan(), llm=_StubLLM([_TAILFRAME_TEXT]), session_dir=tmp_path,
            start_image=tmp_path / "nope.png")
    assert not list(tmp_path.glob("director/tailframe-*.md"))


def test_write_fails_loudly_when_the_call_fails(tmp_path, monkeypatch):
    """调用失败点名段号抛错，不落空文件（与写段同形的不静默纪律）。"""
    from minimax_h3_prompt.director import tailframe

    _stub_vision(monkeypatch)
    with pytest.raises(RuntimeError, match="第 3 段"):
        tailframe.write_tailframe_prompts(
            _plan(), llm=_StubLLM([RuntimeError("网关 502")]),
            session_dir=tmp_path, start_image=_frame_png(tmp_path / "start.png"))
    assert not list(tmp_path.glob("director/tailframe-*.md"))


def test_write_fails_loudly_when_the_vision_read_fails(tmp_path, monkeypatch):
    from minimax_h3_prompt.director import tailframe

    def boom(path, instruction=None):
        raise RuntimeError("读图失败：HTTP 401")

    monkeypatch.setattr(tailframe, "describe_image", boom)
    with pytest.raises(RuntimeError, match="第 3 段.*读图|读图.*第 3 段"):
        tailframe.write_tailframe_prompts(
            _plan(), llm=_StubLLM([_TAILFRAME_TEXT]), session_dir=tmp_path,
            start_image=_frame_png(tmp_path / "start.png"))

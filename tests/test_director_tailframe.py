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


def _plan(mode: str = "t2v"):
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
以这张输入图为参考：镜头落在平视中景，画面正中的人物由正面转为背面站立，双肩与后颈露出，灰色风衣的背部有一道深色缝线。起点地标现在在身后，人物脚下是积水地面，玻璃门在正前方、门框与画面下缘相接，门内冷白灯管照亮地面。

### 保留项声明
保持雨夜便利店门前的人行道、地面积水反射的冷白灯光与灰色风衣不变。伞架旁立着一把黑色长柄伞。

### 锁死清单
- 灰色风衣
- 深色缝线
- 雨夜便利店门前的人行道
"""


def test_transform_must_bind_the_reference_image():
    """不绑定参考图＝写成了文生图提示词：这是结构性缺陷，必须报 error 触发重写。"""
    from minimax_h3_prompt.director import tailframe

    no_reference = _TAILFRAME_TEXT.replace("以这张输入图为参考：", "")
    issues = [i for i in tailframe.validate_tailframe_prompt(no_reference)
              if i.code == "TAILFRAME_NO_REFERENCE"]
    assert issues and issues[0].severity == "error"
    assert "输入图" in issues[0].message or "参考图" in issues[0].message


def test_transform_without_displacement_is_an_error():
    """只写「camera now at B」、不写 A→B 位移：结构性缺陷，必须触发重写。

    2026-10-31 实跑：第 3 段的词只写了终点机位与环境，没有起点地标与距离参照，
    生成的终点图人物像原地踏步。位移是运镜段终点画面的核心要求，不能只告警。
    """
    from minimax_h3_prompt.director import tailframe

    static = (_TAILFRAME_TEXT
              .replace("起点地标现在在身后，", "")
              .replace("人物脚下是积水地面，", "")
              .replace("玻璃门在前方几步处，", ""))
    errors = [i for i in tailframe.validate_tailframe_prompt(static)
              if i.code == "TAILFRAME_NO_DISPLACEMENT"]
    assert errors and errors[0].severity == "error"
    assert "身后" in errors[0].message or "位移" in errors[0].message


def test_transform_without_a_camera_endpoint_only_warns():
    """终点机位缺失是内容质量问题：只告警，不拦产出（ADR 0018 的同一口径）。"""
    from minimax_h3_prompt.director import tailframe

    no_camera = _TAILFRAME_TEXT.replace("镜头落在平视中景，", "")
    issues = [i for i in tailframe.validate_tailframe_prompt(no_camera)
              if i.code == "TAILFRAME_NO_CAMERA_ENDPOINT"]
    assert issues and issues[0].severity == "warning"
    assert not [i for i in tailframe.validate_tailframe_prompt(no_camera)
                if i.severity == "error"]


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

    triggered = [t for t in frames.SEGMENT_TYPES if tailframe.needs_tailframe(t, "t2v")]
    assert triggered == ["camera_same", "camera_cut", "ending"]

    with pytest.raises(ValueError, match="unknown_type"):
        tailframe.needs_tailframe("unknown_type", "t2v")


def test_trigger_set_follows_the_give_image_contract(monkeypatch):
    """变异检验：把「续接段也要终点画面」喂进给图契约，触发集合必须跟着变。

    触发集合不是手抄的字面量，而是从 ``frames.give_images_for`` 现算的——
    另写一份会各自漂的集合，改段类型规则时这里就静默失效（本仓吃过多次）。
    """
    from minimax_h3_prompt.director import frames, tailframe

    assert "continue" not in tailframe.tailframe_segment_types("t2v")
    original = frames.give_images_for
    monkeypatch.setattr(
        tailframe, "give_images_for",
        lambda segment_type, mode: ((frames.IMAGE_END,) if segment_type == "continue"
                                    else original(segment_type, mode)))
    assert "continue" in tailframe.tailframe_segment_types("t2v")


# ---------------------------------------------------------------------------
# AC1：三段式产出（段头由代码常量拼，模型只管正文）
# ---------------------------------------------------------------------------

def test_request_carries_the_reference_frame_and_the_three_section_heads():
    """请求下发起点画面、读图结果、落定画面与三个段头（段头从常量派生）。"""
    from minimax_h3_prompt.director import tailframe

    segment = _plan().segments[2]
    request = tailframe.build_tailframe_request(
        segment, mode="t2v", start_image="GEN001/director/start-03.png",
        start_frame_description="画中人物穿灰色风衣，站在便利店门前。")

    for head in tailframe.tailframe_sections():
        assert head in request, f"请求里缺段头：{head}"
    assert "GEN001/director/start-03.png" in request
    assert "画中人物穿灰色风衣" in request
    assert segment.end_hook in request
    assert tailframe.segment_type_label(segment.type) in request
    # 编辑纪律按档现取；文生图那份不许进来（2026-10-31 事故）
    assert "图生图编辑提示词纪律" in request


def test_non_triggering_segment_refuses_to_build_a_request():
    """开场段要尾帧变换请求是调用方接错了：当场报错，不产半份文档。"""
    from minimax_h3_prompt.director import tailframe

    with pytest.raises(ValueError, match="开场|不需要|opening"):
        tailframe.build_tailframe_request(
            _plan().segments[0], mode="t2v", start_image="x.png",
            start_frame_description="…")


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

def test_tailframe_request_follows_the_configured_frame_model(monkeypatch, tmp_path):
    """尾帧跟配置的帧模型走，但取的是**图生图编辑**那一份纪律（2026-10-31 修订）。

    同一台模型、两个任务：文生图那份要求「写完整画面、约 20 句」，进了编辑请求就会
    让模型把参考图当草稿重画。尾帧只能取 ``edit_discipline_text``。
    """
    from minimax_h3_prompt import image_config
    from minimax_h3_prompt.director import tailframe
    from minimax_h3_prompt.tools.image_prompt_discipline import (
        discipline_text, edit_discipline_text,
    )

    path = tmp_path / "image.yaml"
    path.write_text("frame: qwen\n", encoding="utf-8")
    monkeypatch.setattr(image_config, "IMAGE_CONFIG_PATH", path)
    segment = _segment(3, "camera_same", 260, 413, 141, 153, True)

    request = tailframe.build_tailframe_request(
        segment, mode="fl2v", start_image="start.png",
        start_frame_description="画面里是一扇便利店玻璃门。")

    assert edit_discipline_text("qwen") in request, \
        "尾帧请求没按配置的帧模型取**编辑**纪律"
    assert discipline_text("qwen") not in request, \
        "文生图纪律混进了编辑请求（会要求写完整画面、20 句）"
    assert "20 句" not in request


def test_request_carries_the_identity_baseline():
    """人物设计基线要进请求，并写明「身份以基线为准、读图只取姿态/背景/光线」。"""
    from minimax_h3_prompt.director import tailframe

    request = tailframe.build_tailframe_request(
        _plan().segments[2], mode="t2v", start_image="start.png",
        start_frame_description="…",
        identity_baseline="主角：短黑褐色头发，干净无胡须，不戴墨镜或帽子。")

    assert "【人物设计基线（身份以此为准）】" in request
    assert "干净无胡须" in request
    assert "以这里为准" in request
    assert "读图只取姿态、背景与光线" in request


def test_request_carries_the_segment_video_prompt_for_the_camera_move():
    """运镜与终点构图住在本段视频提示词里：请求必须带上，并注明只取哪两样。"""
    from minimax_h3_prompt.director import tailframe

    request = tailframe.build_tailframe_request(
        _plan().segments[2], mode="t2v", start_image="start.png",
        start_frame_description="…",
        segment_prompt="[Shot 2] an eye-level medium-wide tracking shot follows him "
                       "to the corner; final composition places him before the corner.")

    assert "[Shot 2] an eye-level medium-wide tracking shot" in request
    assert "只取运镜方向与终点构图" in request
    assert "不写声音、脚步声、时间码、连续运动过程" in request


def test_clean_tailframe_prompt_has_no_issues():
    from minimax_h3_prompt.director import tailframe

    assert tailframe.validate_tailframe_prompt(_TAILFRAME_TEXT) == []


def test_discipline_violations_are_warnings_not_blocking(tmp_path, monkeypatch):
    """生图纪律违规（负向表述）只报 warning：尾帧仍然产出、仍然落盘。"""
    from minimax_h3_prompt.director import tailframe

    # 显式指定 z_image：2026-10-30 起默认档跟配置走（默认 Qwen，只跑通用那一类检查）。
    # 这条用例测的是「违规只告警不阻断」这条通道本身，与用哪一档无关。
    violating = _TAILFRAME_TEXT.replace(
        "玻璃门在正前方、门框与画面下缘相接", "玻璃门不要移动，在正前方、门框与画面下缘相接")
    issues = tailframe.validate_tailframe_prompt(violating, model="z_image")
    negatives = [i for i in issues if i.code == "NEGATIVE_STATEMENT"]
    assert negatives and all(i.severity == "warning" for i in negatives)

    _stub_vision(monkeypatch)
    plan = _plan()
    written = tailframe.write_tailframe_prompts(
        plan, llm=_StubLLM([violating]), session_dir=tmp_path / "GEN001",
        start_image=_frame_png(tmp_path / "start.png"), model="z_image")
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
        assert item.transform.startswith("以这张输入图为参考")
        assert "雨夜便利店门前的人行道" in item.preserved
        assert item.locked == ("灰色风衣", "深色缝线", "雨夜便利店门前的人行道")
    assert not (tmp_path / "GEN001" / "director" / "tailframe-01.md").exists()
    assert not (tmp_path / "GEN001" / "director" / "tailframe-02.md").exists()
    # 同一张固定帧图只读一次（演示用；生产里每段起点画面不同）
    assert len(described) == 1
    assert str(start) in llm.requests[0]


def test_write_reads_the_segment_video_prompt_from_disk(tmp_path, monkeypatch):
    """运镜与终点构图从 ``director/segment-NN.md`` 现读，并进写词请求。"""
    from minimax_h3_prompt.director import tailframe
    from minimax_h3_prompt.director.prompts import segment_path

    _stub_vision(monkeypatch)
    session = tmp_path / "GEN001"
    path = segment_path(session, 3)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("[Shot 1] camera tracks him to the corner.", encoding="utf-8")
    llm = _StubLLM([_TAILFRAME_TEXT])

    tailframe.write_tailframe_prompts(
        _plan(), llm=llm, session_dir=session,
        start_image=_frame_png(tmp_path / "start.png"))

    assert "[Shot 1] camera tracks him to the corner." in llm.requests[0]


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
        segment, mode="t2v", llm=object(), start_image="start.png",
        start_frame_description="…")

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
        _plan().segments[2], mode="t2v", llm=llm, start_image="start.png",
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


# ---------------------------------------------------------------------------
# 尾帧编辑链（#131）：真源切片先于纪律 / 位移判据换具名地标 / 机位分叉
# ---------------------------------------------------------------------------

# 机位在动的段词（尾帧请求会把这段原文交给写词模型做分叉判定）。
_MOVING_VIDEO = (
    "[Shot 2] the camera tracks backward with large amplitude at slow speed; "
    "final composition places him at the corner.")
# 机位不动的段词：只有主体变化。
_STATIC_VIDEO = (
    "[Shot 2] a locked-off eye-level medium shot holds the frame; "
    "final composition keeps him centered at the door.")

# 「背景相对位置保持原样」的两种存量写法（L4-05 点名的形态）。
_FROZEN_EN = ("Keep the storefronts and street signs unchanged in their "
              "established spatial relationships.")
_FROZEN_CN = "保持建筑、店面与路牌之间的相对位置不变。"


def test_tailframe_request_puts_the_source_slice_before_the_edit_discipline():
    """切片必须在纪律块**之前**：纪律块里「以本节为准」只有后出现时才是真话。

    与 `agents.load_role_prompt` 同一条拼接纪律（角色 → 真源切片 → 模型纪律 → 原文块）。
    """
    from minimax_h3_prompt import film_language
    from minimax_h3_prompt.director import tailframe
    from minimax_h3_prompt.tools.image_prompt_discipline import edit_discipline_text

    request = tailframe.build_tailframe_request(
        _plan().segments[2], mode="t2v", start_image="start.png",
        start_frame_description="…", model="qwen")
    marker = film_language.tailframe_source_headline().strip()
    assert marker, "尾帧真源没有标题行"
    assert marker in request, "尾帧请求没带编辑真源切片——写词模型拿不到编辑口径"
    assert request.index(marker) < request.index(edit_discipline_text("qwen")), \
        "真源切片排在了模型纪律之后——纪律块那句「以本节为准」会变成假话"


def test_instruction_requires_the_role_declaration_by_model_tier():
    """角色声明写进**产物正文**（L4-01）：写作指令必须按模型档给出中英两套措辞。"""
    from minimax_h3_prompt.director import tailframe

    instruction = tailframe.load_instruction()
    assert "不是重新生成" in instruction, "写作指令没要求 Qwen 档的角色声明"
    assert "IMAGE EDIT, NOT A REGENERATION" in instruction, \
        "写作指令没要求 Z-Image / MOSS 档的角色声明"
    assert "本段视频提示词" in instruction and "机位" in instruction, \
        "写作指令没要求先读运镜句判断机位动没动"


def test_mid_stride_no_longer_passes_the_displacement_gate():
    """`mid-stride` 是步态词，不是「起点地标现在在哪」——旧闸靠它放行（L4-07）。

    2026-11-01 实测：四份存量尾帧里 `tailframe-03` 正是靠 `mid-stride` 过闸的。
    删掉 `strides?` 之后，只有步态词、没有可见关系的正文必须报 error。
    """
    from minimax_h3_prompt.director import tailframe

    stride_only = (_TAILFRAME_TEXT
                   .replace("起点地标现在在身后，", "")
                   .replace("人物脚下是积水地面，", "")
                   .replace("玻璃门在正前方、门框与画面下缘相接，", "他被人群裹在路中央缓步而行，"
                            "双腿在画面里保持 mid-stride 的姿态。"))
    codes = {i.code for i in tailframe.validate_tailframe_prompt(stride_only)
             if i.severity == "error"}
    assert "TAILFRAME_NO_DISPLACEMENT" in codes, \
        "mid-stride 仍然被当成位移证据——旧闸会把合法新写法判死"


def test_background_frozen_while_camera_moves_is_reported():
    """机位在动却要求「背景相对位置不变」：两个互斥目标，必须报出来（L4-05）。"""
    from minimax_h3_prompt.director import tailframe

    for frozen in (_FROZEN_EN, _FROZEN_CN):
        text = _TAILFRAME_TEXT.replace("伞架旁立着一把黑色长柄伞。",
                                       "伞架旁立着一把黑色长柄伞。" + frozen)
        codes = {i.code for i in tailframe.validate_tailframe_prompt(
            text, segment_prompt=_MOVING_VIDEO)}
        assert "TAILFRAME_BACKGROUND_FROZEN" in codes, \
            f"机位在动 + 背景保留句没有报出：{frozen}"


def test_background_frozen_while_camera_is_static_is_not_reported():
    """机位不动时把背景列进保留项是合法写法（L4-06）：不许误报。"""
    from minimax_h3_prompt.director import tailframe

    text = _TAILFRAME_TEXT.replace("伞架旁立着一把黑色长柄伞。",
                                   "伞架旁立着一把黑色长柄伞。" + _FROZEN_CN)
    codes = {i.code for i in tailframe.validate_tailframe_prompt(
        text, segment_prompt=_STATIC_VIDEO)}
    assert "TAILFRAME_BACKGROUND_FROZEN" not in codes, \
        "机位不动时写背景保留句被误报——合法写法不能判死"


def test_displacement_accepts_a_named_landmark_with_a_visible_relation():
    """新判据收「具名地标 + 可见关系」：脚下停止线、正前方的红绿灯杆都算位移证据。"""
    from minimax_h3_prompt.director import tailframe

    text = _TAILFRAME_TEXT.replace(
        "玻璃门在正前方、门框与画面下缘相接，",
        "他停在白色停止线前，正前方是路口那根红绿灯杆。")
    codes = {i.code for i in tailframe.validate_tailframe_prompt(text)}
    assert "TAILFRAME_NO_DISPLACEMENT" not in codes, \
        "合法的「具名地标 + 可见关系」写法被旧闸判死"


def test_role_declaration_does_not_trip_the_negative_statement_check():
    """必写的角色声明本身含否定词，不能被生图纪律当成「负向表述」（L4-01 / ADR 0018）。

    Z-Image 档会把 `NOT` / 「不是」判成负向指令。角色声明是任务定性、不是画面指令，
    判成违规只会让每一份尾帧词都挂一条假警告。
    """
    from minimax_h3_prompt.director import tailframe

    for declaration in (
        "This is an IMAGE EDIT, NOT A REGENERATION. ",
        "这是对输入图的编辑，不是重新生成。",
    ):
        text = declaration + _TAILFRAME_TEXT
        codes = {i.code for i in tailframe.validate_tailframe_prompt(
            text, model="z_image")}
        assert "NEGATIVE_STATEMENT" not in codes, \
            f"角色声明被当成负向表述：{declaration!r}"

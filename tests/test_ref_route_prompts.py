"""多参（ref2va）路线贯通测试（issue #41）：≤15 秒直出官方 ref 六段式视频提示词。

接缝（spec #39 Testing Decisions 的既有缝，扩 mode=\"ref\"）：

1. **结构化产物出口**：打桩 LLM 跑阶段 1+2 → 六段式视频提示词 + N 资产提示词，
   请求里带**编号登记**（语义名 + `<Picture N>` + 生图提示词）与指称纪律；
2. **校验层**：N 张 `<Picture N>` 覆盖式校验在阶段 2 质检里**真接线**（缺一张就触发
   修复轮）——「规则在、接线不在」是本仓七次同族的惯犯形态，光测校验器不够。

登记编号按**剧本出场顺序**（编号即叙事顺序）；画面描述以**生图提示词**为描述主体
（素材画面事实规则）。全部 LLM 调用打桩（历史教训：暗读真 key 的用例只在本机能过）。
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from minimax_h3_prompt import model_factory
from minimax_h3_prompt.brief_parser import Brief, RefItem
from minimax_h3_prompt.generation import REF_WORKFLOW_NAME, ref_asset_registry
from minimax_h3_prompt.graph import nodes, pipeline, roundtable
from minimax_h3_prompt.tools.h3_validator import REF_SECTIONS, validate_ref
from minimax_h3_prompt.tools.ref_metadata import to_tuple


def errors_of(issues):
    return [i.code for i in issues if i.severity == "error"]

TOPIC = "红裙女孩走进秋日庭院，把青铜钥匙交给蓝衫少年。"

# 资产按「与出场顺序相反」的枚举次序交出：编号若按枚举顺序分配，断言就会红。
_ASSETS = (
    ("image_prompt_character", "人物", "character_image_prompts",
     (("蓝衫少年", "人物·蓝衫少年专属生图提示词"), ("红裙女孩", "人物·红裙女孩专属生图提示词"))),
    ("image_prompt_prop", "道具", "prop_image_prompts",
     (("青铜钥匙", "道具·青铜钥匙专属生图提示词"),)),
    ("image_prompt_scene", "场景", "scene_image_prompts",
     (("秋日庭院", "场景·秋日庭院专属生图提示词"),)),
)

# 剧本出场顺序：红裙女孩 → 秋日庭院 → 青铜钥匙 → 蓝衫少年
EXPECTED_NUMBERING = [
    (1, "红裙女孩"), (2, "秋日庭院"), (3, "青铜钥匙"), (4, "蓝衫少年"),
]

_REF_HEAD = "请按官方规范组装最终 H3 提示词（模式 ref"
_DEFS_HEAD = "请给出 subject_definitions 与 retention_analysis 素材"

# 六段式正样本（打桩回复）：语义名为主指称、<Picture N> 在格式处兜底，四张登记图全覆盖。
def _six_section_prompt(include: tuple[int, ...] = (1, 2, 3, 4)) -> str:
    pics = set(include)
    definitions = {
        1: "<Subject 1> is 红裙女孩 (the girl in <Picture 1>), with long dark hair and a red dress.",
        2: "<Subject 2> is 秋日庭院 (the garden in <Picture 2>), with maple paths and stone lanterns.",
        3: "<Subject 3> is 青铜钥匙 (the key in <Picture 3>), with an old green patina.",
        4: "<Subject 4> is 蓝衫少年 (the boy in <Picture 4>), in a blue shirt.",
    }
    subjects = {1: "<Subject 1>", 2: "<Subject 2>", 3: "<Subject 3>", 4: "<Subject 4>"}
    defs = "\n".join(definitions[n] for n in sorted(pics))
    rets = "\n".join(
        f"{subjects[n]} (appears in [Shot 1]): fully_preserved - the reference look is retained."
        for n in sorted(pics)
    )
    named = {1: "红裙女孩 (the girl in the red dress)", 2: "秋日庭院 (the autumn garden)",
             3: "青铜钥匙 (the bronze key)", 4: "蓝衫少年 (the boy in the blue shirt)"}
    beat = f"[Shot 1] A medium shot frames {named[2]}. {named[1]} walks in and hands {named[3]} to {named[4]}."
    return (
        f"subject_definitions:\n{defs}\n\n"
        f"summary:\n[reference generation] The target video shows 红裙女孩 handing 青铜钥匙 to 蓝衫少年 in 秋日庭院.\n\n"
        f"retention_analysis:\n{rets}\n\n"
        f"detailed_description:\n"
        f"The target video is in a cinematic autumn style with warm soft lighting.\n"
        f"{beat}\n\n"
        f"overall_soundscape:\nSoft outdoor ambience with light footsteps continues throughout the scene.\n\n"
        f"non_diegetic_music:\nN/A"
    )


def _assets_json(assets) -> str:
    # locked_features（issue #53）取语义名——它逐字在提示词正文里（夹具正文都以语义名起头），
    # 节点侧「条目必须是正文原文片段」的不变式因此成立。
    return json.dumps([
        {"name": name, "positive_prompt": prompt, "instructions": ["粘贴到节点 10"],
         "locked_features": [name]}
        for name, prompt in assets
    ])


def _config():
    return SimpleNamespace(roundtable_max_rounds=1, common_sense_qa=False, max_qa_iterations=2)


def _brief() -> Brief:
    return Brief(mode="ref", variant="T2VA", duration=5.0, style="写实", plot=TOPIC)


def _run_pipeline(tmp_path, monkeypatch, *, first_video_prompt: str | None = None,
                  checkpoint_dir=None, brief: Brief | None = None):
    """打桩跑阶段 1+2，返回 ``(final_state, prompt, 全部出站请求, prompt_engineer 调用次数)``。

    ``first_video_prompt``：prompt_engineer 的首回复（默认完整六段式）。传缺一张图的版本
    可以让质检修复轮真的跑起来——修复轮能不能发现「缺 <Picture 4>」，正是 N 张登记
    是否接进校验的物证。
    """
    brief = brief or _brief()
    requests: list[str] = []
    prompt_engineer_calls: list[str] = []
    replies = {
        "请完成人物形象设计": "人物设计正文",
        "请完成背景设计": "背景设计正文",
        "请完成道具设计": "道具设计正文",
        "请统筹并裁决最终美术设计": "美术统筹正文",
    }

    def fake_run_agent(agent, message):
        requests.append(message)
        for _node, kind, _out, assets in _ASSETS:
            if "各生成一条 Z-Image" in message and f"请为{kind}" in message:
                return _assets_json(assets)
        # 主组装请求与质检修复轮都走 prompt_engineer：修复轮数得出来，才知道 N 张登记
        # 真的接进了校验（缺图不修复＝接线断了）。
        if message.startswith(_REF_HEAD) or "请修正后重新输出完整提示词" in message:
            prompt_engineer_calls.append(message)
            if len(prompt_engineer_calls) == 1 and first_video_prompt is not None:
                return first_video_prompt
            return _six_section_prompt()
        if message.startswith("质检以下 H3 提示词"):
            return "建议补全缺失素材定义。"
        if message.startswith(_DEFS_HEAD):
            return "subject_definitions 与 retention_analysis 素材草稿"
        if message.startswith("请写出分场剧本"):
            # 剧本要含资产语义名：登记编号按**剧本出场顺序**，剧本是空壳就无从排序。
            return TOPIC
        for head, reply in replies.items():
            if message.startswith(head):
                return reply
        return "上游产物正文"

    monkeypatch.setattr(nodes, "run_agent", fake_run_agent)
    monkeypatch.setattr(pipeline, "run_agent", fake_run_agent)
    monkeypatch.setattr(roundtable, "_invoke", lambda role, model, msgs: "已锁定")
    monkeypatch.setattr(model_factory, "build_chat_model", lambda: object())
    monkeypatch.setattr(pipeline.stage_saver, "base", tmp_path / "stages")

    state, model, _agents = pipeline.run_stage1(brief, _config())
    final_state, prompt = pipeline.run_stage2(state, brief, _config(), model=model,
                                              checkpoint_dir=checkpoint_dir)
    return final_state, prompt, requests, prompt_engineer_calls


# ---------------------------------------------------------------------------
# 1. 请求接线：编号登记（剧本出场顺序）+ 素材画面事实（生图提示词为描述主体）
# ---------------------------------------------------------------------------

def test_stage1_defs_request_carries_numbered_registry(tmp_path, monkeypatch):
    """reference_consistency 的请求带**编号登记**——编号按剧本出场顺序，不按枚举顺序。"""
    _final, _prompt, requests, _calls = _run_pipeline(tmp_path, monkeypatch)
    defs_req = next(r for r in requests if r.startswith(_DEFS_HEAD))
    positions = [defs_req.find(f"<Picture {n}>：{name}") for n, name in EXPECTED_NUMBERING]
    assert all(p >= 0 for p in positions), f"请求缺少编号登记：\n{defs_req}"
    assert positions == sorted(positions), "登记条目未按剧本出场顺序排列"


def test_prompt_engineer_request_carries_image_prompts_as_descriptions(tmp_path, monkeypatch):
    """画面描述以**生图提示词**为描述主体：登记条目带生图提示词正文与素材画面事实纪律。"""
    _final, _prompt, requests, _calls = _run_pipeline(tmp_path, monkeypatch)
    req = next(r for r in requests if r.startswith(_REF_HEAD))
    assert "人物·红裙女孩专属生图提示词" in req
    assert "场景·秋日庭院专属生图提示词" in req
    assert "生图提示词" in req
    assert "语义名" in req
    assert "剧本出场顺序" in req
    for n, name in EXPECTED_NUMBERING:
        assert f"<Picture {n}>：{name}" in req


def test_identity_rt_request_carries_numbered_registry(tmp_path, monkeypatch):
    """身份一致性圆桌的上下文同样带编号登记（<Picture N> 映射正是它的锁定对象）。"""
    brief = _brief()
    state = {
        "script": TOPIC,
        "art_design": "美术统筹正文",
        "character_image_prompts": [
            {"name": "蓝衫少年", "positive_prompt": "人物·蓝衫少年专属生图提示词"},
            {"name": "红裙女孩", "positive_prompt": "人物·红裙女孩专属生图提示词"},
        ],
    }
    captured: dict = {}
    fake_rt = SimpleNamespace(invoke=lambda payload: captured.update(payload) or {"lock": "已锁定"})
    node = nodes.make_identity_rt_node(fake_rt, object(), brief)
    node(state)
    assert "<Picture 1>：红裙女孩" in captured["context"]


# ---------------------------------------------------------------------------
# 2. 结构化产物出口：≤15 秒直出一份 ref 六段式，校验全绿
# ---------------------------------------------------------------------------

def test_direct_ref_six_section_prompt_is_green(tmp_path, monkeypatch):
    """阶段 2 直出一份六段式（六段落齐全），按 N 张登记校验全绿。"""
    final_state, prompt, _requests, calls = _run_pipeline(tmp_path, monkeypatch)
    for header in REF_SECTIONS:
        assert f"{header}:" in prompt
    registry = ref_asset_registry(final_state, _brief())
    assert [(r.picture, r.name) for r in registry] == EXPECTED_NUMBERING
    issues = validate_ref(prompt, duration=5.0, ref_meta=to_tuple(registry))
    assert errors_of(issues) == [], [i.code for i in issues]
    assert len(calls) == 1  # 全绿时不该进修复轮


def test_missing_registry_picture_triggers_repair_round(tmp_path, monkeypatch):
    """缺一张登记图 → 阶段 2 质检用 N 张登记发现 PICTURE_UNUSED 并触发修复。

    这条是「校验器扩 N 张**真接线**」的物证：ref_meta 不接 N 素材登记的话，
    缺图照样放行、修复轮根本不会发生。
    """
    final_state, prompt, _requests, calls = _run_pipeline(
        tmp_path, monkeypatch, first_video_prompt=_six_section_prompt(include=(1, 2, 3)))
    assert len(calls) >= 2, "缺 <Picture 4> 未触发质检修复轮（N 张登记没接进校验）"
    assert "PICTURE_UNUSED" in calls[1]
    registry = ref_asset_registry(final_state, _brief())
    issues = validate_ref(prompt, duration=5.0, ref_meta=to_tuple(registry))
    assert errors_of(issues) == [], [i.code for i in issues]


# ---------------------------------------------------------------------------
# 3. 登记名统一（AC6）：一个名字、「工作流 63」旧称清零
# ---------------------------------------------------------------------------

def test_menu_ref_label_uses_registered_name(monkeypatch):
    """菜单多参项的文案用登记名（旧称「工作流 63」已废弃）。"""
    from minimax_h3_prompt.ui import menu

    captured: dict = {}

    def fake_select(prompt, choices):
        captured["choices"] = list(choices)
        return SimpleNamespace(ask=lambda: captured["choices"][3])

    monkeypatch.setattr(menu.questionary, "select", fake_select)
    assert menu.choose_model() == "ref2va"
    assert any(REF_WORKFLOW_NAME in choice for choice in captured["choices"])


def test_menu_label_reads_the_registered_name_at_call_time(tmp_path, monkeypatch):
    """菜单文案在**调用时**取登记名（复审 S4）：换配置后菜单必须当场跟着变。

    原先 import 时就求值，换一份 ``config/workflow.yaml`` 后菜单仍指旧工作流，
    用户按菜单进 ComfyUI 找不到那张——而且不报错。
    """
    from minimax_h3_prompt import workflow_config
    from minimax_h3_prompt.ui import menu

    path = tmp_path / "workflow.yaml"
    path.write_text('ref_workflow_name: "临时工作流 V9"\n', encoding="utf-8")
    monkeypatch.setattr(workflow_config, "WORKFLOW_CONFIG_PATH", path)

    captured: dict = {}

    def fake_select(prompt, choices):
        captured["choices"] = list(choices)
        return SimpleNamespace(ask=lambda: captured["choices"][3])

    monkeypatch.setattr(menu.questionary, "select", fake_select)
    assert menu.choose_model() == "ref2va"
    assert any("临时工作流 V9" in choice for choice in captured["choices"]), (
        f"菜单仍用旧登记名：{captured['choices']}")


def test_legacy_workflow_63_name_is_gone_from_user_facing_files():
    """用户可见面（菜单/向导/README/brief 模板与示例）不再出现「工作流 63」，且登记名统一。"""
    root = Path(__file__).resolve().parents[1]
    user_facing = (
        "README.md",
        "examples/brief_template.md",
        "examples/brief_ref.md",
        "src/minimax_h3_prompt/ui/menu.py",
        "src/minimax_h3_prompt/ui/wizard.py",
    )
    for rel in user_facing:
        text = (root / rel).read_text(encoding="utf-8")
        assert "工作流 63" not in text and "工作流63" not in text, f"{rel} 还留着旧称「工作流 63」"
    # 文档钉登记名字面量；代码侧由 REF_WORKFLOW_NAME 常量单一来源（菜单渲染另有测试）。
    for rel in ("README.md", "examples/brief_template.md", "examples/brief_ref.md"):
        text = (root / rel).read_text(encoding="utf-8")
        assert REF_WORKFLOW_NAME in text, f"{rel} 未引用登记名 {REF_WORKFLOW_NAME}"


# ---------------------------------------------------------------------------
# 4. 复审修复的钉子（issue #41 复审 F2/F3、补扫与完整性批判的缺口）
# ---------------------------------------------------------------------------

def test_stage2_in_graph_agents_receive_registry_ref_meta(tmp_path, monkeypatch):
    """图内 agent 装配吃 N 登记（复审 F2）：``validate_h3_prompt`` 工具闭包的 ref_meta
    不是空的——装配期盲则 prompt_engineer 铁律「必须调工具终检」查不出缺图。"""
    captured: list[list] = []
    real_build = pipeline.build_role_agents

    def spy_build(model, refs, mode, duration, variant):
        captured.append((list(refs), mode))
        return real_build(model, refs, mode, duration, variant)

    monkeypatch.setattr(pipeline, "build_role_agents", spy_build)
    _final, _prompt, _requests, _calls = _run_pipeline(tmp_path, monkeypatch)
    carried = [refs for refs, mode in captured if refs]
    assert len(carried) >= 2, "阶段 2 的图内装配与 QA 轮装配都要拿到登记"
    for refs in carried:
        assert [(r.picture, r.name) for r in refs] == EXPECTED_NUMBERING


def test_brief_file_user_registry_flows_into_validation(tmp_path, monkeypatch):
    """brief 文件流程：用户登记的编号/描述进 ref_meta，不被出场顺序派生改写（缺口 4）。"""
    user_refs = [
        RefItem(picture=4, name="用户四号", description="用户写的四"),
        RefItem(picture=1, name="用户一号", description="用户写的一"),
        RefItem(picture=2, name="用户二号", description="用户写的二"),
        RefItem(picture=3, name="用户三号", description="用户写的三"),
    ]
    brief = Brief(mode="ref", variant="T2VA", duration=5.0, style="写实", plot=TOPIC,
                  refs=user_refs)
    final_state, prompt, _requests, calls = _run_pipeline(tmp_path, monkeypatch, brief=brief)
    assert final_state["ref_meta"] == [
        (1, "用户一号", "用户写的一"), (2, "用户二号", "用户写的二"),
        (3, "用户三号", "用户写的三"), (4, "用户四号", "用户写的四"),
    ]
    assert len(calls) == 1  # 打桩提示词与用户登记的 1..4 对齐，全绿不进修复轮


def test_stage2_checkpoint_resume_keeps_registry_ref_meta(tmp_path, monkeypatch):
    """断点续跑分支也把 N 登记灌进 ref_meta 并能触发修复（缺口 8）。

    删掉 pipeline.run_stage2 里 merged["ref_meta"] 那行，本用例必须红——
    断点路径此前会带着阶段 1 的旧 ref_meta 进质检。
    """
    from minimax_h3_prompt.stage2_checkpoint import save_checkpoint

    ckpt = tmp_path / "ckpt"
    ckpt.mkdir()
    # 断点里存一份缺 <Picture 4> 的初稿：续跑的质检要用 N 登记发现并修复它
    save_checkpoint(ckpt, prompt_draft=_six_section_prompt(include=(1, 2, 3)))
    final_state, prompt, _requests, calls = _run_pipeline(
        tmp_path, monkeypatch, checkpoint_dir=ckpt)
    assert final_state["ref_meta"] == [
        (1, "红裙女孩", "人物·红裙女孩专属生图提示词"),
        (2, "秋日庭院", "场景·秋日庭院专属生图提示词"),
        (3, "青铜钥匙", "道具·青铜钥匙专属生图提示词"),
        (4, "蓝衫少年", "人物·蓝衫少年专属生图提示词"),
    ]
    assert len(calls) >= 1, "断点续跑的质检没用 N 登记发现缺图"
    # 判据是「初稿缺的 <Picture 4> 被修复轮补回」：ref_meta 若没灌进断点分支，
    # 校验放行、不修复，提示词会停在缺图初稿上。
    assert "<Picture 4>" in prompt


def test_prompt_engineer_role_keeps_ref_discipline():
    """角色提示词里的两条 ref 写法硬约束（语义名指称体系、素材画面事实）不可被静默删掉。"""
    root = Path(__file__).resolve().parents[1]
    text = (root / "src/minimax_h3_prompt/prompts/prompt_engineer.md").read_text(encoding="utf-8")
    assert "素材指称体系" in text
    assert "素材画面事实" in text


def _main_module():
    """取 main.py 的真模块对象——包 __init__ 里有个同名 hello-world 函数会遮蔽子模块。"""
    import importlib

    return importlib.import_module("minimax_h3_prompt.main")


def test_cli_brief_path_honors_declared_ref_mode(tmp_path, monkeypatch, capsys):
    """--brief 快路径不再强制 base（复审 F3）：brief 声明 ref 就以 mode=ref 进管线，
    不套 FL2VA 帧变体默认；>15 秒落逐段提示词＋拼接顺序卡（issue #46，与向导同口径）。"""
    from minimax_h3_prompt import config as config_module
    from minimax_h3_prompt import model_factory

    main_module = _main_module()

    brief_file = tmp_path / "brief.md"
    brief_file.write_text("输入: 多参\n时长: 20\n## 剧情\n红裙女孩走进秋日庭院。", encoding="utf-8")
    captured: dict = {}

    def fake_run_pipeline(brief, cfg):
        captured["brief"] = brief
        return "final"

    monkeypatch.setattr(pipeline, "run_pipeline", fake_run_pipeline)
    monkeypatch.setattr(model_factory, "build_chat_model", lambda: object())
    monkeypatch.setattr(config_module, "config",
                        SimpleNamespace(output_path=tmp_path / "out.md"))
    assert main_module.main(["--brief", str(brief_file)]) == 0
    assert captured["brief"].mode == "ref"
    assert captured["brief"].variant == "T2VA"
    out = capsys.readouterr().out
    assert "#46" not in out  # 占位提示已退役
    assert list((tmp_path / "segments").glob("segment-*.md")), "段提示词必须落盘"
    assert (tmp_path / "segments" / "order-card.md").is_file(), "拼接顺序卡必须落盘"


def test_cli_brief_path_frame_route_unchanged(tmp_path, monkeypatch):
    """--brief 的首尾帧路线行为不变：仍强制 base 且缺省 FL2VA（AC1 后半句）。"""
    from minimax_h3_prompt import config as config_module

    main_module = _main_module()

    brief_file = tmp_path / "brief.md"
    brief_file.write_text("输入: 首尾帧\n时长: 5\n## 剧情\n雨夜公交站。", encoding="utf-8")
    captured: dict = {}

    def fake_run_pipeline(brief, cfg):
        captured["brief"] = brief
        return "final"

    monkeypatch.setattr(pipeline, "run_pipeline", fake_run_pipeline)
    monkeypatch.setattr(config_module, "config",
                        SimpleNamespace(output_path=tmp_path / "out.md"))
    assert main_module.main(["--brief", str(brief_file)]) == 0
    assert captured["brief"].mode == "base"
    assert captured["brief"].variant == "FL2VA"


def test_cli_brief_path_declares_ledger_boundary(tmp_path, monkeypatch, capsys):
    """--brief 快路径不建素材台账，但必须**声明**边界（issue #43，绝不静默）：

    该路径只产提示词文本、没有生成目录，台账/正本没有落点——带路径 refs 走它时
    要点名「不建台账」并指路 generate-prompts/向导；没有路径（还没交回）就不噪声。
    """
    from minimax_h3_prompt import config as config_module

    main_module = _main_module()
    monkeypatch.setattr(pipeline, "run_pipeline", lambda brief, cfg: "final")
    monkeypatch.setattr(config_module, "config",
                        SimpleNamespace(output_path=tmp_path / "out.md"))

    brief_file = tmp_path / "brief.md"
    brief_file.write_text(
        "输入: 多参\n时长: 5\n## 剧情\n红裙女孩走进秋日庭院。\n"
        "## 角色与参考图\n- Picture 1: 红裙女孩 — 红裙 (girl.png)\n",
        encoding="utf-8")
    assert main_module.main(["--brief", str(brief_file)]) == 0
    out = capsys.readouterr().out
    assert "不建素材台账" in out and "generate-prompts" in out

    brief_file.write_text(
        "输入: 多参\n时长: 5\n## 剧情\n红裙女孩走进秋日庭院。\n"
        "## 角色与参考图\n- Picture 1: 红裙女孩 — 红裙\n",
        encoding="utf-8")
    assert main_module.main(["--brief", str(brief_file)]) == 0
    assert "不建素材台账" not in capsys.readouterr().out  # 没路径＝尚无台账可建，不噪声


def test_generate_prompts_threads_mode_and_refs(tmp_path, monkeypatch):
    """generate-prompts --brief 同样放行 ref，且 brief.refs 随调用传下去（完整性批判缺口 1）。"""
    from minimax_h3_prompt import config as config_module
    from minimax_h3_prompt import topic_generation

    main_module = _main_module()

    brief_file = tmp_path / "brief.md"
    brief_file.write_text(
        "输入: 多参\n时长: 5\n## 剧情\n红裙女孩走进秋日庭院。\n"
        "## 角色与参考图\n- Picture 1: 红裙女孩 — 红裙、黑发\n",
        encoding="utf-8")
    captured: dict = {}

    def fake_create(topic, cfg, **kwargs):
        captured.update(kwargs)
        return object(), tmp_path

    monkeypatch.setattr(topic_generation, "create_video_from_topic", fake_create)
    monkeypatch.setattr(main_module, "_print_generation_result", lambda *a, **k: None)
    monkeypatch.setattr(config_module, "config", SimpleNamespace(output_path=tmp_path / "out.md"))
    assert main_module.main(["generate-prompts", "--brief", str(brief_file)]) == 0
    assert captured["mode"] == "ref"
    assert [(r.picture, r.name) for r in captured["refs"]] == [(1, "红裙女孩")]

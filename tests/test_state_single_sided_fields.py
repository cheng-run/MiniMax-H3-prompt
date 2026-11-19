"""状态里的单边字段：删掉的要有会变红的证明，剩下的行为不许丢（#334）。

#334 删掉三处「看着有用其实没有」的字段：

- ``qa_issues``：只有读者没有写者——自首个提交起就没人写过它，两处读者拿到的永远是空；
- ``shot_duration_warning``：只有写者没有读者——唯一消费是 ``output/stages/*.txt``
  里抄成一段文本，控制台本来就有 ``[警告]`` 行；
- ``final_report``：只有写者没有读者——``finalize`` 写进 state，唯一「读」它的是
  阶段 2 的两处剔除集合（``k not in (...)``），那不是消费。

本文件的用例分两类：

1. **契约不回头**：三个键不许重新出现在 ``PipelineState`` / 白名单 / 排除表里。
   变异检验：往 schema 加回任一键（不登记出处）→ 红。
2. **删除没带走行为**：``finalize`` 仍把组装修好的正文写回 ``final_prompt``；
   storyboard 在均分重试仍失败时仍把警告打到控制台、且**不**往 state 里塞字段。
   变异检验：把 ``finalize`` 的返回值改成空更新、或把 storyboard 的警告打印删掉 → 红。
"""
from __future__ import annotations

from minimax_h3_prompt import session_store
from minimax_h3_prompt.brief_parser import Brief
from minimax_h3_prompt.graph import nodes, pipeline
from minimax_h3_prompt.graph.state import PipelineState

REMOVED_KEYS = ("qa_issues", "shot_duration_warning", "final_report")

# 分镜节点要的 agent 名只需存在；节点只把 agent 对象原样传给 ``run_agent``。
_STORYBOARD_AGENTS = {"storyboard": object()}

# 三镜均分（每镜 5s）→ 首次就判均分 → 重试一次 → 仍均分。
_UNIFORM_TABLE = (
    "[Shot 1] opens.\n"
    "[Shot 2] At 00:05.000, mid.\n"
    "[Shot 3] At 00:10.000, close-up.\n"
)


def test_removed_keys_stay_out_of_the_state_contract():
    """三个键不许重新出现在 schema、白名单或排除表里（剔除键「复活」＝契约回头）。"""
    assert not set(REMOVED_KEYS) & set(PipelineState.__annotations__), (
        "这三个键在 #334 已删：qa_issues 无写者、shot_duration_warning 与 final_report "
        "无读者。要加回来，先给出那个「另一端」是谁")
    assert not set(REMOVED_KEYS) & set(session_store._STATE_KEYS)
    assert not set(REMOVED_KEYS) & set(session_store.PERSISTENCE_EXCLUSIONS)


def test_finalize_still_repairs_the_prompt_it_writes_back():
    """``final_report`` 删了，但 ``finalize`` 的修复行为必须还在。

    它唯一的产品级输出是「组装器修好的正文」：抽掉围栏、补齐缺失段头。这条用例喂一段
    被围栏包住的正文，断言写回的 ``final_prompt`` 是被修过的那份、且更新里**只有**
    ``final_prompt``（报告字段没了）。
    """
    brief = Brief(mode="base", duration=5.0, plot="庭院里的橘猫")
    node = nodes._make_finalize_node()

    update = node({"brief": brief, "final_prompt": "```text\nbody\n```"})

    assert set(update) == {"final_prompt"}, f"finalize 又写了别的键：{sorted(update)}"
    repaired = update["final_prompt"]
    assert "```" not in repaired, f"围栏没有被抽掉：{repaired!r}"
    assert repaired.startswith("body"), inspected(repaired)
    # 修复的第二个动作是补齐缺失段头（assembler 的既有行为），证明修复真的跑了，
    # 不只是把原文原样返回。
    assert "integrated_multimodal_description:" in repaired, repaired


def inspected(text: str) -> str:
    return f"组装器没把围栏里的正文放在最前：{text!r}"


def test_storyboard_warns_on_console_and_writes_no_warning_field(monkeypatch, capsys):
    """均分重试仍失败：警告打到控制台，state 里不许再出现 ``shot_duration_warning``。

    变异检验：删掉 ``print`` 那一行 → 控制台断言的红色；把字段塞回返回值 →
    「更新里只有 shot_table」的红色。
    """
    replies = iter([_UNIFORM_TABLE, _UNIFORM_TABLE])
    monkeypatch.setattr(nodes, "run_agent", lambda agent, message: next(replies))
    brief = Brief(mode="base", duration=15.0, plot="庭院里的橘猫")

    update = nodes._make_storyboard_node(_STORYBOARD_AGENTS)({"brief": brief})

    assert set(update) == {"shot_table"}, f"分镜节点写了多余字段：{sorted(update)}"
    out = capsys.readouterr().out
    assert "SHOT_DURATIONS_UNIFORM" in out, f"均分警告没有打到控制台：\n{out}"


def test_removed_keys_are_not_read_or_written_anywhere_in_the_pipeline():
    """三个键在管线代码里彻底消失：留着读者或写者就是「删了一半」。

    **按 AST 找字符串常量**，不扫源码文本：解释「为什么删」的注释里必然写着这些名字，
    文本扫描会被自己的注释绊倒（先例：``test_doc_pointer_shape`` 排除自己的断言消息）。
    剔一个不存在的键不会报错，所以「剔除集合里还留着它」也是这条要抓的形态。
    """
    import ast
    import inspect

    for module in (nodes, pipeline):
        tree = ast.parse(inspect.getsource(module))
        literals = {
            node.value for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        }
        leaked = sorted(literals & set(REMOVED_KEYS))
        assert not leaked, (
            f"{module.__name__} 里还把这些键当字符串用：{leaked}"
            "（#334 已删；留着读者或写者就是删了一半）")


def test_qa_loops_use_their_own_input_key_not_a_state_field():
    """质检循环的真入参是独立键 ``frame_prompt_issues`` / 局部变量，不是 ``qa_issues``。

    这条钉住 #334 的裁定依据：``qa_issues`` 之所以能整条删，是因为两条循环各自有
    真入参。变异检验：把 ``_stage1_frame_qa_loop`` 里的键名改成 ``qa_issues`` → 红。
    """
    import inspect

    source = inspect.getsource(pipeline._stage1_frame_qa_loop)

    assert '"frame_prompt_issues"' in source, "阶段 1 质检循环的入参键不见了"
    assert "qa_issues" not in source


def test_old_session_files_with_removed_keys_still_resume(tmp_path):
    """旧会话文件里留着已删的三个键：读得回来、续跑不炸、再保存即收敛。

    删 ``PipelineState`` 的键有两层后果，这条一次钉住：

    1. **读回**：``load_session`` 只按白名单收敛**保存**这一侧；文件里的残留键会原样
       进内存 state（它不影响任何读者——那三处读者已经删了）。这段是真机实测的行为，
       不是猜的：自己造一份带三键的会话文件，确认载入成功且产物键都在。
    2. **再保存**：白名单里没有它们，下一次 ``save`` 就把残留从文件里抹掉——**静默
       收敛**，与 ``_route_filtered_state`` 对跨路线残留的处理同一个口径。
    3. **进图**：LangGraph 只搬运 schema 声明过的键，未声明的残留到不了节点手上
       （``graph/state.py`` 顶部的注释与 issue #28 的实测），所以续跑照旧。
    """
    import json

    from minimax_h3_prompt.session_store import SESSION_FILENAME, load_session, save_session

    directory = tmp_path / "GEN001"
    save_session(directory, Brief(mode="base", duration=5.0, plot="庭院"), {"shot_table": "表"})
    path = directory / SESSION_FILENAME
    raw = json.loads(path.read_text(encoding="utf-8"))
    for key in REMOVED_KEYS:
        raw["stage_state"][key] = ["旧值"] if key != "final_report" else "旧报告"
    path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")

    loaded = load_session(directory)

    assert loaded is not None, "带已删键的旧会话必须还能读回（否则续跑入口直接丢会话）"
    assert loaded.stage_state.get("shot_table") == "表", "真产物不该被连带丢掉"
    save_session(directory, loaded.brief, loaded.stage_state)
    after = json.loads(path.read_text(encoding="utf-8"))["stage_state"]
    assert not set(REMOVED_KEYS) & set(after), (
        f"再保存后文件里还留着已删的键：{sorted(set(REMOVED_KEYS) & set(after))}")


def test_stage2_refine_loop_runs_the_same_two_validators_finalize_dropped(monkeypatch):
    """``finalize`` 删掉的两个校验器，阶段 2 精修循环仍在跑——不是「删了就不查了」。

    删之前那对结论**只进 ``final_report``**（无读者）：它们既不改提示词，也不发事件。
    删之后同一对校验器照旧在精修循环里核一遍，且结论会**驱动重写**——比原来只多不少。
    这条用记录型桩把两个校验器都换掉，断言循环真的调了它们（而不是靠读代码相信）。
    变异检验：把循环里任一处校验调用删掉 → 本条红。
    """
    from types import SimpleNamespace

    from minimax_h3_prompt.tools.h3_validator import ValidationIssue

    brief = Brief(mode="base", duration=5.0, plot="庭院里的橘猫")
    called = {"validate": 0, "theme": 0}

    class _FakeGraph:
        def stream(self, *a, **k):
            return iter([])

    monkeypatch.setattr(pipeline, "build_pipeline_graph",
                        lambda *a, **k: (_FakeGraph(), {"qa": object(), "prompt_engineer": object()}))

    def fake_validate(*a, **k):
        called["validate"] += 1
        return [ValidationIssue("error", "X", "格式问题")]

    def fake_theme(*a, **k):
        called["theme"] += 1
        return []

    monkeypatch.setattr(pipeline, "validate_prompt", fake_validate)
    monkeypatch.setattr(pipeline, "theme_fidelity_issues", fake_theme)
    monkeypatch.setattr(pipeline, "run_agent", lambda agent, msg: "fixed prompt")
    monkeypatch.setattr(pipeline, "assemble_and_repair", lambda p, m: ("fixed prompt", ""))
    monkeypatch.setattr(pipeline, "format_issues", lambda issues: "issues")
    monkeypatch.setattr(pipeline, "_theme_repair_injection", lambda s, b: "")
    monkeypatch.setattr(pipeline, "stage_saver", SimpleNamespace(save=lambda *a, **k: None))
    config = SimpleNamespace(common_sense_qa=False, max_qa_iterations=1)

    pipeline.run_stage2({"script": "s"}, brief, config, model=object())

    assert called == {"validate": 1, "theme": 1}, (
        f"阶段 2 精修循环没跑满这对校验器：{called}（finalize 删掉它们的前提就是这里有）")

"""变异检验：守卫测试真的会咬人吗（体检 §9 未验证项 / P6）。

「永不为红＝没有」。一条守卫用例在**没有实现**时照样绿，说明它守的不是它声称
的那件事；本仓已经吃过一次（``check.sh`` 的输出接 ``| tail``，红的检查显示成绿）。

做法很小：登记一组「把关键实现改坏」的变异，逐个应用 → 跑指定用例 → 还原。
用例必须红（=变异被杀死）；用例仍绿＝这条守卫是摆设。

```bash
uv run python scripts/mutation_check.py            # 全部
uv run python scripts/mutation_check.py --only ffmpeg_shell
```

约束：

- 还原用**字节**写回（原文 ``read_bytes`` → ``write_bytes``），不走 ``git checkout``：
  脏工作区与未跟踪文件都能跑（``check.sh full`` 现在会跑它），未提交的改动不受影响；
- 每条变异都声明 ``old`` 在文件里**恰好出现一次**（多处出现说明锚点选错了，
  会用错地方）；锚点会随重构复制而失效——失效时报「出现 N 次」，就是这条变异
  已经不再守任何东西的信号；
- 无论用例结果如何都还原（``finally``），还原后逐字节核对与原文相同。
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Mutation:
    name: str
    path: str
    old: str
    new: str
    tests: tuple[str, ...]
    why: str
    # 行尾本身就是被测对象时置 False。默认会先把锚点按 LF 归一、写完再补回 CRLF，
    # 那会把「CRLF → LF」这类变异当场抵消掉，变异报「存活」而守卫其实好好的。
    normalize_newlines: bool = True
    # 运行目标：默认 pytest；``vitest`` 时 ``tests`` 写
    # ``<spec 相对 frontend/ 的路径>::<测试名>``，由前端 node_modules 里的 vitest 执行。
    runner: str = "pytest"


class MutationSkipped(RuntimeError):
    """运行环境缺失（例如前端依赖未安装）：既不算杀死也不算存活。"""


MUTATIONS: tuple[Mutation, ...] = (
    Mutation(
        "changed_paths_ignores_branch_commits",
        "scripts/check.sh",
        '    if [ -n "$base" ]; then\n'
        '      git diff --name-only --diff-filter=ACMR "$base" HEAD -- "$@"\n'
        '    fi',
        '    if false; then\n'
        '      git diff --name-only --diff-filter=ACMR "$base" HEAD -- "$@"\n'
        '    fi',
        ("tests/test_check_changed_paths.py"
         "::test_committed_changes_on_a_branch_are_in_scope",),
        "变更集漏掉已提交的改动＝提交后跑 fast 静默跳过 mypy/前端/契约守卫"
        "（2026-11-14，#253 复审拿到假绿）",
    ),
    Mutation(
        "ref_stage1_brief_injects_ledger_refs",
        "src/minimax_h3_prompt/api/jobs.py",
        "        if session.brief.is_ref:\n"
        "            return session.brief\n"
        "        # r2v 的阶段 1 还没跑素材循环",
        "        if session.brief.is_ref:\n"
        "            return self._ref_brief(session, passed_only=False)\n"
        "        # r2v 的阶段 1 还没跑素材循环",
        ("tests/test_api_asset_loop.py::"
         "test_ref_stage1_never_sees_the_ledger_as_brief_refs",),
        "阶段 1 拿回注入的 refs ＝ ``ref_asset_registry`` 走「用户登记为准」，"
        "设计师清单被绕过、素材集合退回用户预置（D7 修订 #279 的死锁原形）",
    ),
    Mutation(
        "usage_off",
        "src/minimax_h3_prompt/model_factory.py",
        "        ),\n        max_retries=_LLM_MAX_RETRIES,\n        streaming=True,\n"
        "        stream_usage=True,\n    )",
        "        ),\n        max_retries=_LLM_MAX_RETRIES,\n        streaming=True,\n"
        "        stream_usage=False,\n    )",
        ("tests/test_external_contracts.py::test_gateway_client_asks_for_streaming_and_usage_together",),
        "关掉 usage 上报会让成本表记 0 且不报错（2026-10-21 实测）",
    ),
    Mutation(
        "artifact_size_limit_off",
        "src/minimax_h3_prompt/workbench_store.py",
        "ARTIFACT_MAX_BYTES = 1024 * 1024",
        "ARTIFACT_MAX_BYTES = 64 * 1024 * 1024",
        ("tests/test_api_artifacts.py::test_text_artifact_over_one_mib_is_413",),
        "文本读取上限失守＝大产物把内存与响应体一起拖爆，413 专用码永不出现",
    ),
    Mutation(
        "artifact_segment_pattern_off",
        "src/minimax_h3_prompt/workbench_store.py",
        "_SEGMENT_PROMPT_FILENAME = r\"(?:shot|segment)-\\d{2}\\.md\"",
        "_SEGMENT_PROMPT_FILENAME = r\"(?:shot|segment)-\\d{2}\\.txt\"",
        ("tests/test_artifact_registry.py::test_segment_prompt_is_listed_and_readable",),
        "模式失效＝段提示词既不出现在清单里也读不出来，#245 的整条链静默归零",
    ),
    Mutation(
        "artifact_scan_skips_the_resolver",
        "src/minimax_h3_prompt/workbench_store.py",
        "            if artifact_spec(name) is not None:\n"
        "                names.add(name)",
        "            names.add(name)",
        ("tests/test_artifact_registry.py::"
         "test_files_outside_the_pattern_are_neither_listed_nor_readable",),
        "清单扫描绕开解析函数＝segments/ 下 plan.json、shot-ledger.jsonl 一并当成产物，"
        "「算不算产物」出现第二份判据",
    ),
    Mutation(
        "artifact_bridge_frame_pattern_off",
        "src/minimax_h3_prompt/workbench_store.py",
        '_BRIDGE_FRAME_SUFFIX = "-start.png"',
        '_BRIDGE_FRAME_SUFFIX = "-begin.png"',
        ("tests/test_artifact_registry.py::test_bridge_frame_is_listed_with_its_kind",
         "tests/test_artifact_registry.py::"
         "test_bridge_frame_content_returns_png_bytes_and_supports_range"),
        "桥接帧模式失效＝剥出的尾帧既不出现在清单里也读不出来，"
        "段卡片按产物名永远取不到本段起点图（#277）",
    ),
    # 下面两条仍是 pytest 源码守卫（#278）：前者是前后端**跨语言**命名一致，
    # vitest 读不到后端的 Python 常量；后者钉的是「有意不进产物分区」的意图，
    # 现有组件用例对未分组 kind 默认通过、杀不死它。
    Mutation(
        "frontend_bridge_frame_name_drifts",
        "frontend/src/features/sessions/artifactKinds.ts",
        "padStart(2, '0')}-start.png`",
        "padStart(2, '0')}-begin.png`",
        ("tests/test_frontend_bridge_frame.py::"
         "test_frontend_bridge_frame_name_matches_the_backend",),
        "前端桥接帧名与后端登记表漂移＝段卡片按名字永远取不到起点图，"
        "两端各自绿着（#277 复审 F5）",
    ),
    Mutation(
        "frontend_bridge_frame_leaves_the_segment_panel",
        "frontend/src/features/sessions/artifactKinds.ts",
        "const SEGMENT_LOCAL_KINDS = new Set(['segment_injection_card', 'bridge_frame'])",
        "const SEGMENT_LOCAL_KINDS = new Set(['segment_injection_card'])",
        ("tests/test_frontend_bridge_frame.py::"
         "test_bridge_frame_is_registered_as_a_segment_local_kind",),
        "桥接帧不再被标成分段本地 kind＝同一张图同时出现在分段与产物两个分区，"
        "现有组件用例仍绿（#277 复审 F6）",
    ),
    Mutation(
        "frontend_segment_start_frame_off_by_one",
        "frontend/src/features/sessions/SegmentsTab.vue",
        "bridgeFrameName(index - 1)",
        "bridgeFrameName(index)",
        ("src/features/sessions/SegmentsTab.spec.ts"
         "::按剥帧段号差一位取图：第 3 段取 shot-02，不取 shot-03",),
        "段卡片取本段尾帧而不是上一段剥帧＝第 N 段的起点图错一位（segment-NN-start.png 的"
        "语义就是这样断的；#277 复审 F7 / #278 改成真实组件用例）",
        runner="vitest",
    ),
    Mutation(
        "frontend_clarification_retry_does_not_refetch",
        "frontend/src/features/sessions/SessionCreateForm.vue",
        "async function retryClarifications(): Promise<void> {\n"
        "  if (submitting.value || clarificationRetrying.value) {\n"
        "    return\n"
        "  }",
        "async function retryClarifications(): Promise<void> {\n"
        "  if (true) {\n"
        "    return\n"
        "  }",
        ("src/features/sessions/SessionCreateForm.spec.ts"
         "::点重试会重新拉取澄清问题并展开面板",),
        "重试按钮点了不重拉＝失败态仍然只有一个出口（刷新整页重填表单），"
        "按钮成了摆设（spec #356 缝 4）",
        runner="vitest",
    ),
    Mutation(
        "frontend_clarification_failure_survives_plot_edit",
        "frontend/src/features/sessions/SessionCreateForm.vue",
        "    if (clarificationStage.value === 'failed') {\n"
        "      clarificationStage.value = 'idle'\n"
        "      clarificationNotice.value = null\n"
        "    }",
        "    if (clarificationStage.value === 'failed') {\n"
        "      clarificationNotice.value = null\n"
        "    }",
        ("src/features/sessions/SessionCreateForm.spec.ts"
         "::失败态下改剧情复位到空闲态：再点创建会重新拉问题而不是直接建会话",),
        "改剧情不复位失败态＝用户改了主题再点创建仍带着旧失败态直接建会话，"
        "重试按钮只治了一半（spec #356 缝 4 的隐藏出口）",
        runner="vitest",
    ),
    Mutation(
        "session_detail_leaks_stage_state",
        "src/minimax_h3_prompt/workbench_store.py",
        "    artifacts: list[ArtifactInfo] = Field(default_factory=list)\n"
        "    # 素材是目录现扫的结果，与产物同款（#222 §5.3）：不进快照、不占会话状态写入。\n",
        "    artifacts: list[ArtifactInfo] = Field(default_factory=list)\n"
        "    stage_state: dict[str, Any] = Field(default_factory=dict)\n"
        "    # 素材是目录现扫的结果，与产物同款（#222 §5.3）：不进快照、不占会话状态写入。\n",
        ("tests/test_api_action_items.py::test_stage_state_never_crosses_the_api_boundary",),
        "详情快照把 stage_state 带出去＝内部 state 键变成对外契约，产物白名单与 1 MiB 边界一起失效",
    ),
    Mutation(
        "range_end_not_clamped",
        "src/minimax_h3_prompt/api/media.py",
        "    return start, min(end, size - 1)",
        "    return start, end",
        ("tests/test_api_media.py::test_single_range_forms_return_206",),
        "结束位置不截到文件尾＝Content-Range 与 Content-Length 声称的字节数多于实际发出的",
    ),
    Mutation(
        "range_416_without_content_range",
        "src/minimax_h3_prompt/api/media.py",
        '    response.headers["Content-Range"] = f"bytes */{size}"',
        '    response.headers["X-File-Size"] = f"bytes */{size}"',
        ("tests/test_api_media.py::test_unsatisfiable_or_malformed_ranges_are_416",),
        "416 少了 Content-Range: bytes */len＝客户端拿不到文件多大，没法重新挑范围",
    ),
    Mutation(
        "continuity_cut_true",
        "src/minimax_h3_prompt/director/pack.py",
        '        "refVideos": [],\n        "continuityFromPrev": segment.guided,\n'
        '        "refImageSize": settings.ref_image_size,',
        '        "refVideos": [],\n        "continuityFromPrev": True,\n'
        '        "refImageSize": settings.ref_image_size,',
        ("tests/test_director_pack.py::test_camera_cut_segment_ships_continuity_from_prev_false",),
        "切镜段被写成续接＝插件导入后静默续上上一段（#142 的真身）",
    ),
    Mutation(
        "director_frame_tolerance_zero",
        "src/minimax_h3_prompt/director/companion.py",
        "DIRECTOR_FRAME_TOLERANCE = 5",
        "DIRECTOR_FRAME_TOLERANCE = 0",
        ("tests/test_api_director_segments.py::"
         "test_default_tolerance_comes_from_the_constant",),
        "容差归零＝相位对齐裁尾的合法形态被当成贴错段（2026-10-31 现场 226 帧）",
    ),
    Mutation(
        "director_visual_decision_not_injected",
        "src/minimax_h3_prompt/director/prompts.py",
        "    return _context_block(ctx.visual_decision)",
        '    return ""',
        ("tests/test_director_prompts.py::"
         "test_visual_decision_reaches_batch_first_write_and_rewrite_requests",),
        "画面决策块不注入＝阶段 1 的镜头评审锁定与画面细化回不到写段请求"
        "（#297 缺口的原形：模型只剩分镜表摘要）",
    ),
    Mutation(
        "director_revision_context_not_wired",
        "src/minimax_h3_prompt/ui/wizard_director.py",
        '        "revision_context": render_revision_context(state),',
        '        "revision_context": "",',
        ("tests/test_director_prompts.py::"
         "test_wizard_director_flow_passes_revision_context_into_write_requests",),
        "修订块不接线＝用户在对话里提的累积要求到不了导演台写段请求"
        "（组装会拼、调用方没传，本仓反复栽的那一处）",
    ),
    Mutation(
        "director_shot_table_not_sliced",
        "src/minimax_h3_prompt/director/prompts.py",
        "    excerpt, sliced = window_shot_excerpt(\n"
        "        ctx.shot_table, ctx.start_s, ctx.start_s + ctx.duration_s)\n",
        "    excerpt, sliced = ctx.shot_table, True\n",
        ("tests/test_director_prompts.py::"
         "test_shot_table_block_carries_only_the_shots_inside_the_segment_window",),
        "分镜表不切片＝整张表原样下发，模型要自己找时间窗、窗口外的镜头细节"
        "串进本段（#301 缺口的原形）",
    ),
    Mutation(
        "director_context_block_empty_leaks_blank_lines",
        "src/minimax_h3_prompt/director/prompts.py",
        '    return f"\\n{body}\\n" if body else ""',
        '    return f"\\n{body}\\n"',
        ("tests/test_director_prompts.py::"
         "test_context_block_returns_nothing_for_blank_input",),
        "空值契约失效＝空串/纯空白也渲染成块，请求里多出空块与空行"
        "（请求字节随阶段 1 有没有产物漂，#298 AC2 消失）",
    ),
    Mutation(
        "director_request_block_order_swapped",
        "src/minimax_h3_prompt/director/prompts.py",
        "    _request_revision_block,\n"
        "    _request_film_end_block,\n"
        "    _request_story_block,\n",
        "    _request_film_end_block,\n"
        "    _request_story_block,\n"
        "    _request_revision_block,\n",
        ("tests/test_director_prompts.py::"
         "test_segment_request_places_revision_after_images_and_before_story",),
        "块的唯一顺序真源被改坏＝用户累积修订跑到剧情概述之后，"
        "「越靠近产出的越靠后」这条顺序约束消失（#297 AC 的顺序断言变摆设）",
    ),
    Mutation(
        "director_visual_decision_not_threaded",
        "src/minimax_h3_prompt/api/jobs.py",
        "                revision_context=context.revision_context,\n"
        "                visual_decision=context.visual_decision,\n",
        "                revision_context=context.revision_context,\n"
        '                visual_decision="",\n',
        ("tests/test_api_director_planning.py::"
         "test_director_write_context_threads_the_two_request_blocks",),
        "写段出口不取上下文的画面决策块＝阶段 1 的镜头评审锁定与画面细化"
        "回不到续跑补写的请求（「会拼、没传」本仓反复栽的那一处）",
    ),
    Mutation(
        "director_visual_decision_ignores_window_shots",
        "src/minimax_h3_prompt/director/prompts.py",
        "    return lambda segment: _visual_decision_block_for_request(\n"
        "        state, window_shot_numbers(shot_table, segment.start_s, segment.end_s))\n",
        "    return lambda segment: _visual_decision_block_for_request(state)\n",
        ("tests/test_director_prompts.py::"
         "test_visual_decision_source_renders_the_block_per_segment_window",),
        "不喂本段窗口镜头号＝块首纪律退回「本段时间窗内的镜头」，"
        "请求里不再点名本段该写哪几镜（#299 的按段约束消失）",
    ),
    Mutation(
        "asset_modes_drop_r2v",
        "src/minimax_h3_prompt/brief_parser.py",
        'DIRECTOR_ASSET_MODES = ("r2v",)',
        'DIRECTOR_ASSET_MODES = ()',
        ("tests/test_route_matrix.py",),
        "r2v 不再产出资产图提示词＝导演台 r2v 段没有参考素材",
    ),
    Mutation(
        "schema_key_removed",
        "src/minimax_h3_prompt/graph/state.py",
        "    asset_proofreads: dict\n",
        "",
        ("tests/test_state_contract.py",),
        "state 键漏声明＝节点读不到它（LangGraph 只搬声明过的键）",
    ),
    Mutation(
        "user_error_not_value_error",
        "src/minimax_h3_prompt/errors.py",
        "class UserInputError(ValueError, H3Error):",
        "class UserInputError(H3Error):",
        ("tests/test_errors.py::test_classified_errors_stay_catchable_as_builtin_types",),
        "新异常不继承 ValueError＝存量 except ValueError 静默漏掉它",
    ),
    Mutation(
        "parallel_overwrite_allowed",
        "src/minimax_h3_prompt/graph/nodes.py",
        '        if key in merged and not _same_value(merged[key], value):',
        '        if False and key in merged and not _same_value(merged[key], value):',
        ("tests/test_parallel_merge.py::test_parallel_nodes_may_not_silently_overwrite_a_shared_key",),
        "撤掉并行组共享键守卫＝后写者静默覆盖（2026-10-25 探针形态）",
    ),
    Mutation(
        "adr_ref_dangling",
        "src/minimax_h3_prompt/asset_loop.py",
        "# 直调路径的角色名（issue #71 / ADR 0019）",
        "# 直调路径的角色名（issue #71 / ADR 0099）",
        ("tests/test_adr_guard.py::test_every_referenced_adr_exists",),
        "引用不存在的 ADR 号＝悬空指针，没有任何守卫会报（#143 抓到过三处）",
    ),
    Mutation(
        "adr_status_missing",
        "docs/adr/0001-stage2-checkpoint.md",
        "> **状态（2026-11-02 复核）**：",
        "> 状态：",
        ("tests/test_adr_guard.py::test_every_adr_has_a_well_formed_status_line",),
        "删掉状态行＝「哪份还被取代」再也机读不出来",
    ),
    Mutation(
        "first_frame_slot_ignored",
        "src/minimax_h3_prompt/director/frames.py",
        "    return IMAGE_FIRST in give_images_for(segment_type, mode)",
        "    return False",
        ("tests/test_director_first_frame_slot.py",),
        "首帧槽判据恒假＝fl2v 切镜段不再产首帧词，r2v 的误触也被掩盖（#145）",
    ),
    Mutation(
        "frame_meta_model_dropped",
        "src/minimax_h3_prompt/director/tailframe.py",
        '            "model": model,\n',
        '            "model": "",\n',
        ("tests/test_director_tailframe.py::test_tailframe_sidecar_records_model_segment_and_input_fingerprints",),
        "sidecar 不记模型＝换 frame: 后旧尾帧词被静默复用（#153）",
    ),
    Mutation(
        "static_branch_disabled",
        "src/minimax_h3_prompt/director/tailframe.py",
        '    return bool(_STATE_CHANGE.search(str(end_hook or "")))',
        "    return False",
        ("tests/test_director_tailframe.py::test_static_segment_accepts_state_change_plus_a_named_relation",),
        "静态分支永不生效＝静态 ending 仍被逼写 A→B 位移（#150）",
    ),
    Mutation(
        "blocked_by_rejected",
        "src/minimax_h3_prompt/director/tailframe.py",
        '    r"blocked by|occluded by|obscured by|hidden behind|"',
        '    r"occluded by|obscured by|hidden behind|"',
        ("tests/test_director_tailframe.py::test_displacement_accepts_blocked_by_in_both_languages",),
        "位移锚点不认 blocked by＝合法的遮挡写法被重写（#150）",
    ),
    Mutation(
        "legacy_zimage_alias_dropped",
        "src/minimax_h3_prompt/image_config.py",
        "    return LEGACY_IMAGE_MODEL_ALIASES.get(key, key)",
        "    return key",
        ("tests/test_canvas.py::test_legacy_zimage_canvas_key_still_reads",),
        "别名归一失效＝旧画布键 zimage 读不出，旧会话的画布静默回落默认表（#144 D）",
    ),
    Mutation(
        "doc_pointer_line_number",
        "docs/film-language-playbook.md",
        "| 不写 video 镜头运动 / 剪辑 / 时间码 / H3 三段式 | `image_prompt_engineer.md` 的「内容要求」、`frame_prompt_engineer.md` 的「内容规则」 |",
        "| 不写 video 镜头运动 / 剪辑 / 时间码 / H3 三段式 | `image_prompt_engineer.md` 的「内容要求」、`frame_prompt_engineer.md:69` |",
        ("tests/test_doc_pointer_shape.py",),
        "活文档指针退回行号＝提示词一插行就静默漂移（retro 2026-11-03）",
    ),
    Mutation(
        "glossary_merges_the_two_first_frames",
        "CONTEXT.md",
        "**段首帧图（segment first frame）**:",
        "**首帧图（first frame）**:",
        ("tests/test_context_glossary.py::"
         "test_opening_and_segment_first_frames_stay_distinct_terms",),
        "两张「首帧图」并成一条＝开场段与切镜段的起点画面重新混用，"
        "「切镜段首帧也要在写段时读」这类错误方案又会有人推出来（#320）",
    ),
    Mutation(
        "glossary_merges_the_two_spatial_relations",
        # #335：两类空间关系并成一条，C1 事故里「改的是朝向、接触关系没变」这句关键事实
        # 就再也说不清。锚点取**标题整行**（与 #320 那条同一口径）。
        "CONTEXT.md",
        "**接触关系（contact relation）**:",
        "**空间关系（spatial relation）**:",
        ("tests/test_context_glossary.py::"
         "test_orientation_and_contact_relations_stay_distinct_terms",),
        "两类空间关系并成一条＝「窗在身后」是一类、「她靠着窗」是另一类这件事重新被读成同一件事，"
        "C1 那句「接触关系没变」失去可指的名字（#335）",
    ),
    Mutation(
        "glossary_frame_description_priority_unbounded",
        # #335：把「守卫对这两类关系都看不见」这句改反，读图结果就会被读成「已经有守卫盯着
        # 朝向/接触」，N6 那类读偏再发生时没人会回头查上游。锚点取整句（全仓恰好一处）。
        "CONTEXT.md",
        "**现有守卫对朝向/接触这两类关系都看不见**：",
        "**现有守卫能看见朝向/接触这两类关系**：",
        ("tests/test_context_glossary.py::"
         "test_frame_description_states_its_layer_boundary",),
        "把「现有守卫两类关系都看不见」改反＝读者以为朝向被读反时机检会响，"
        "于是照旧只看绿灯出片（#335；N6 正是这么一路绿到成片）",
    ),
    Mutation(
        "glossary_n6_ruling_dropped",
        # #335：删掉延后方向那句，「明确不做」就只剩现象、没有归宿——下一轮照旧会把它
        # 当成「还没排期」重新开一遍讨论。锚点取整句（全仓恰好一处）。
        "CONTEXT.md",
        # 2026-11-19（#352）换成**现行**那句：旧锚点是「实现留到共享编剧层那批…」，
        # 而 #349 已把 N6 的延后方向改指向自己的批次——锚点留在旧措辞上，咬的就是一句
        # 已被收回的墓碑，真正的归宿被删也不会响（本仓「永不为红＝没有」的同族）。
        "**归 N6 自己的批次**，**不**并入共享编剧层那批（本 spec #349 只做动机，不碰 N6），",
        "",
        ("tests/test_context_glossary.py::test_n6_ruling_is_recorded_as_not_done_yet",),
        "N6 的延后方向没落档＝「归 N6 自己的批次」这句失传，读者只看到一个已知现象、"
        "不知道它归哪一层，下一轮重新讨论一遍要不要改优先级（#335 / #349）",
    ),
    Mutation(
        "aspect_check_inverted",
        "src/minimax_h3_prompt/director/aspect.py",
        "        exceeds=relative_delta > tolerance + _TOLERANCE_EPSILON,",
        "        exceeds=relative_delta <= tolerance + _TOLERANCE_EPSILON,",
        ("tests/test_director_canvas_aspect.py",
         "tests/test_director_companion.py::test_handoff_rejects_a_video_whose_aspect_misses_its_anchor"),
        "画幅判定反了＝2% 的正常取整差被当成事故、218% 的锚点裁切被放行（#158）",
    ),
    Mutation(
        "size_check_disabled",
        "src/minimax_h3_prompt/director/aspect.py",
        "        exceeds=(\n"
        "            width_delta > tolerance + _TOLERANCE_EPSILON\n"
        "            or height_delta > tolerance + _TOLERANCE_EPSILON\n"
        "        ),",
        "        exceeds=False,",
        ("tests/test_director_canvas_aspect.py::test_size_check_flags_864x480_against_1920x1088",
         "tests/test_director_canvas_aspect.py::test_size_check_boundary_is_strictly_greater_than_the_tolerance"),
        "尺寸判据恒不超差＝864×480 对 1920×1088 被放行，2.2 倍的尺寸差看不见（#176）",
    ),
    Mutation(
        "film_end_sidecar_never_hits",
        # 判据自 #318 起住共享读图实现（全片尾帧与开场首帧同一份）。锚点跟着挪，
        # 用例同时挂两条：共享实现自己的契约，以及全片尾帧改造后仍不变的行为。
        "src/minimax_h3_prompt/director/still_description.py",
        '    if not fingerprint or meta.get("image_sha256") != fingerprint:\n'
        "        return None",
        "    if True:\n        return None",
        ("tests/test_director_film_end.py::test_matching_fingerprint_reuses_the_sidecar_without_reading_again",
         "tests/test_director_still_description.py::test_matching_fingerprint_reuses_the_sidecar_without_reading_again"),
        "尾帧 sidecar 永不命中＝图没换也重读，续跑重复付费且不报错（#160／#318）",
    ),
    Mutation(
        "still_description_kind_check_dropped",
        # #318 把 ``kind`` 从写死字面量改成参数：去掉这条比较，全片尾帧就会把开场
        # 首帧留下的 sidecar 当成自己的缓存——「同一张图在不同用途下各自读」失守。
        "src/minimax_h3_prompt/director/still_description.py",
        '    if meta.get("kind") != kind:\n        return None\n',
        "",
        ("tests/test_director_still_description.py::test_sidecar_written_for_another_kind_is_not_reused",),
        "用途标记比较被去掉＝别的用途写的 sidecar 被当成自己的缓存，段词拿着另一张图"
        "的描述约束画面（#318）",
    ),
    Mutation(
        "doc_reference_untracked",
        "src/minimax_h3_prompt/tools/frame_match.py",
        '"""帧数 / fps / 宽高 / 时长。读不到时返回空 dict。"""',
        '"""帧数 / fps / 宽高 / 时长。见 docs/research/2099-01-01-不存在.md。'
        '读不到时返回空 dict。"""',
        ("tests/test_doc_references_tracked.py",),
        "引用未入库的 docs 路径＝干净检出丢掉证据（#157 复审）",
    ),
    Mutation(
        "sidecar_merge_dropped",
        "src/minimax_h3_prompt/director/frame_meta.py",
        "    merged: dict[str, object] = dict(read_frame_meta(prompt_path) or {})",
        "    merged: dict[str, object] = {}",
        ("tests/test_director_cut_firstframe.py::test_rewriting_the_firstframe_prompt_keeps_the_recorded_image_path",
         "tests/test_frame_meta.py::test_merge_keeps_unknown_fields_and_overwrites_named_keys"),
        "sidecar 合并退化成整份覆盖＝重写正文抹掉别的写口字段（#157 复审）",
    ),
    Mutation(
        "film_settings_ignores_the_answer",
        "src/minimax_h3_prompt/director/film_canvas.py",
        "    film = film_size_for(brief, frame_model=frame_model)\n"
        "    if film is None:\n        return settings",
        "    film = None\n"
        "    if film is None:\n        return settings",
        ("tests/test_director_film_canvas.py::test_film_settings_takes_width_height_from_the_answer_only",
         "tests/test_director_pack.py::test_wizard_packs_the_answered_film_size_and_tells_the_node"),
        "包参数不取成片画幅＝用户答了也不生效，包与节点又是两套口径（#161）",
    ),
    Mutation(
        "film_snap_dropped",
        "src/minimax_h3_prompt/director/film_canvas.py",
        "    return _snap(width), _snap(height)",
        "    return int(width), int(height)",
        ("tests/test_director_film_canvas.py::test_snap_rounds_to_the_plugin_grid_half_up",),
        "不对齐 32 网格＝1920×1080 这类尺寸到插件后端点运行才炸（#161）",
    ),
    Mutation(
        "language_slip_never_fires",
        "src/minimax_h3_prompt/asset_proofread.py",
        '    if body_language(body) != "en":\n        return None',
        "    return None",
        ("tests/test_asset_proofread.py::test_language_slip_judges_against_the_body_language",
         "tests/test_wizard_asset_proofread.py::test_chinese_observed_in_an_english_body_gets_one_more_ask"),
        "语言判据失效＝中文整句照旧嵌进英文正文，链上一处都不会响（#162）",
    ),
    Mutation(
        "proofread_request_drops_language_rule",
        "src/minimax_h3_prompt/asset_proofread.py",
        '        "**observed 用与【生图提示词】相同的语言写**（正文英文就写英文，正文中文就写"\n'
        '        "中文），补漏的 observed 同理——两条事实会原样并进正文。",',
        '        "",',
        ("tests/test_asset_proofread.py::test_proofread_request_asks_observed_in_the_body_language",),
        "请求不再要求 observed 跟正文同语言＝治本那一侧失效，只剩事后告警（#162）",
    ),
    Mutation(
        "replacement_language_gate_off",
        "src/minimax_h3_prompt/ui/wizard_proofread.py",
        "    text = str(candidate)\n    if language_slip(text, body=body) is None:\n        return text",
        "    text = str(candidate)\n    return text",
        ("tests/test_wizard_asset_proofread.py::test_chinese_observed_in_an_english_body_gets_one_more_ask",),
        "交互处不再给改成英文的机会＝#162 的现场原样复现（回车即混排）",
    ),
    Mutation(
        "manifest_set_check_off",
        "src/minimax_h3_prompt/graph/nodes.py",
        "            if mismatch:\n",
        "            if False:\n",
        ("tests/test_asset_manifests.py",),
        "清单集合校验失效＝清单 2 条产出 3 条照旧进台账，错位无声带进素材循环（#173）",
    ),
    Mutation(
        "negated_clause_switches_the_kind",
        "src/minimax_h3_prompt/image_routing.py",
        "            if _is_negated(body, position):\n                continue",
        "            if False:\n                continue",
        ("tests/test_image_model_routing.py::test_chinese_negations_suppress_the_hit",
         "tests/test_image_routing_wiring.py::test_a_negated_design_sentence_does_not_reroute_the_kind"),
        "否定判定被摘掉＝「不得出现现代塑料包装」把整类道具切到 MOSS（#167 的现场）",
    ),
    Mutation(
        "enumeration_breaks_the_negation",
        "src/minimax_h3_prompt/image_routing.py",
        '_SENTENCE_BREAKS = re.compile(r"[。！？.!?\\n\\r]+")',
        '_SENTENCE_BREAKS = re.compile(r"[。，、；：！？,.;:!?\\n\\r]+")',
        ("tests/test_image_model_routing.py::test_an_enumeration_after_a_negation_inherits_it",
         "tests/test_image_model_routing.py::"
         "test_a_comma_separated_english_enumeration_inherits_the_negation"),
        "顿号/逗号重新当句边界＝枚举里第二项起脱离否定词，整类又被误切到 MOSS（#177）",
    ),
    Mutation(
        "wu_before_hit_ignored",
        "src/minimax_h3_prompt/image_routing.py",
        '    if position > 0 and body[position - 1] == "无":\n        return True',
        '    if position > 0 and body[position - 1] == "有":\n        return True',
        ("tests/test_image_model_routing.py::test_wu_immediately_before_the_hit_suppresses_it",),
        "「无X」不被认成否定＝「无文字／无包装」整句照旧切到 MOSS（#177 补充裁定）",
    ),
    Mutation(
        "settled_review_skipped",
        "src/minimax_h3_prompt/ui/wizard.py",
        "    review_settled(state, brief, generation_dir, queue, abandoned, _settled, _prompt, _confirm)",
        "    pass  # 变异：撤掉已裁决项复查",
        ("tests/test_wizard_asset_fix.py::test_settled_record_with_chinese_reminder_is_flagged_on_read",
         "tests/test_wizard_asset_fix.py::test_settled_description_edit_updates_record_without_reread"),
        "撤掉已裁决项复查＝存量混排不告警、没有就地改写入口，只能手改会话文件（#174）",
    ),
    Mutation(
        "manifest_duplicate_guard_off",
        "src/minimax_h3_prompt/asset_manifest.py",
        "        if len(found) > 1\n",
        "        if False\n",
        ("tests/test_asset_manifest_duplicates.py",),
        "清单级跨类重名守卫失效＝跨类同名要到交回第一张图才在台账入库时报（#175）",
    ),
    Mutation(
        "manifest_parser_accepts_duplicate_names",
        "src/minimax_h3_prompt/asset_manifest.py",
        "    duplicates = tuple(sorted({name for name in names if names.count(name) > 1}))\n",
        "    duplicates = ()\n",
        ("tests/test_asset_manifests.py",),
        "解析器放行类内重复名＝同名清单被当合法，重问不点名、报错也不点名，"
        "「一个人两张板」静默退化成一张（#178）",
    ),
    Mutation(
        "legacy_session_check_disabled",
        "src/minimax_h3_prompt/director/film_canvas.py",
        '    text = str(created_at or "").strip()\n'
        "    if not text:\n"
        "        return True\n"
        "    try:\n"
        "        moment = datetime.fromisoformat(text)\n"
        "    except ValueError:\n"
        "        return True\n"
        "    # 无时区的时间无法与常量可靠比较；同样按旧会话处理，不猜。\n"
        "    if moment.tzinfo is None:\n"
        "        return True\n"
        "    return moment < FILM_ASPECT_QUESTION_SINCE",
        "    return False",
        ("tests/test_director_legacy_aspect_session.py::test_legacy_session_zimage_canvas_is_not_compared_as_a_film_aspect",
         "tests/test_director_legacy_aspect_session.py::test_missing_creation_time_is_treated_as_a_legacy_session",
         "tests/test_director_legacy_aspect_session.py::test_cutoff_is_the_161_commit_time_in_any_equivalent_spelling"),
        "旧会话判据失效＝帧模型切回 z_image 时旧画布被当成成片画幅，比例误判硬停（#179）",
    ),
    Mutation(
        "interaction_log_drops_answer_len",
        "src/minimax_h3_prompt/observability.py",
        '            "answer_len": length,\n',
        '            "answer_len": 0,\n',
        ("tests/test_interactions.py::test_prompt_question_and_answer_are_recorded",),
        "交互日志只记摘要不记长度＝事后查不出「他到底答了多少字」，长正文类答案"
        "（内容被故意丢弃）就彻底失去可核对的量（#171）",
    ),
    Mutation(
        "anchor_pattern_blind",
        "src/minimax_h3_prompt/segment_refs.py",
        're.compile(r"must\\s+agree\\s+with", re.IGNORECASE),',
        're.compile(r"must\\s+disagree\\s+with", re.IGNORECASE),',
        ("tests/test_segment_refs.py::test_closure_marks_edges_that_hit_the_consistency_anchor_sentence",
         # #183 起这条常量还决定「降不降级」：句式失效时锚会重新占槽（第 5 段 10 张
         # 直接超限），所以降级用例也必须跟着变红。
         "tests/test_segment_refs.py::test_scope_demotes_a_picture_pulled_in_only_by_an_anchor_sentence"),
        "一致性锚句式失效＝该占槽的锚照样占槽，而身份来源图会被误降级（#180 用户故事 9）",
    ),
    Mutation(
        "budget_guard_off",
        "src/minimax_h3_prompt/director/planning.py",
        "        if entry is not None and entry.slot_count > MAX_PICTURES:\n",
        "        if False:\n",
        ("tests/test_director_asset_budget.py::test_over_budget_segment_is_fed_back_and_the_redrawn_plan_is_used",
         "tests/test_director_asset_budget.py::test_two_requeues_then_a_named_error_names_the_segment_and_every_picture"),
        "规划期预算守卫恒假＝11 张的段直接落盘，打包期才发现（#180 事故原样复现，#187）",
    ),
    Mutation(
        "write_budget_guard_off",
        "src/minimax_h3_prompt/director/prompts.py",
        "    permitted = {item.number for item in entry.pictures}\n",
        "    return None\n",
        ("tests/test_director_write_budget.py::test_budget_violation_is_written_back_and_the_fix_lands",),
        "写段预算守卫恒不触发＝预算外的图照写，规划与写段各推一套（#180 事故形态，#188）",
    ),
    Mutation(
        "writing_discipline_placeholders_not_applied",
        "src/minimax_h3_prompt/director/prompts.py",
        "        text = text.replace(placeholder, value)\n",
        "        text = text\n",
        ("tests/test_writing_disciplines.py::"
         "test_every_write_mode_receives_the_three_disciplines_without_placeholders",),
        "占位符不替换＝模型收到 {{...}} 字面量，三族纪律在四条模式里全部缺席（#312）",
    ),
    Mutation(
        "anaphora_discipline_not_wired_into_write_requests",
        # 锚点取**不带 import 的**用法行：两条纪律族的票各加一行 import 时会在同一处
        # 汇合，拿 import 行当锚点会在合票后变成 0 次（锚点失效＝变异已不守任何东西）。
        "src/minimax_h3_prompt/segment_prompts.py",
        "  {ANAPHORA_DISCIPLINE}；\n",
        "\n",
        ("tests/test_segment_prompts.py::"
         "test_all_four_python_write_requests_carry_the_anaphora_discipline",),
        "纪律常量不接线＝首尾帧与多参四条写段请求一起失去这句纪律，"
        "写段器照抄「如前所述」再被校验器判死，白耗一轮有界重写（#315）",
    ),
    Mutation(
        "camera_speed_discipline_cleared",
        # 锚点取常量赋值**整行**（并行票已证明 import 行会在汇合处失效）；清空后
        # 五条写段路径同时失去这条纪律，守卫必须先自证真源非空才会红。
        "src/minimax_h3_prompt/writing_disciplines.py",
        'CAMERA_SPEED_DISCIPLINE = "**机位速度按剧情选**：静场、克制段落才用 `Static Shot`；'
        '有动作的段写完整的「运动类型 + 幅度 + 速度」自然句，不要把「静态/慢速中景」当默认'
        '——全片默认静态会把动作摊平，成片像慢放"',
        'CAMERA_SPEED_DISCIPLINE = ""',
        ("tests/test_writing_disciplines_guard.py::"
         "test_every_write_route_sends_the_family_discipline",),
        "清空机位速度真源＝五条写段路径同时失去这条纪律；守卫若不自证真源非空，"
        "``\"\" in text`` 恒真就会永远不红（#316）",
    ),
    Mutation(
        "future_events_discipline_cleared",
        # 锚点取常量赋值**整行**（全仓恰好一次）；清空后五条写段路径（含画面决策块的
        # 插值）同时失去这条纪律，守卫必须先自证真源非空才会红。
        "src/minimax_h3_prompt/writing_disciplines.py",
        'FUTURE_EVENTS_DISCIPLINE = "其他镜头的事件、构图与机位一个字都不能写进正文"',
        'FUTURE_EVENTS_DISCIPLINE = ""',
        ("tests/test_writing_disciplines_guard.py::"
         "test_every_write_route_sends_the_family_discipline",),
        "清空时间窗外剧情禁区真源＝五条写段路径同时失去这条纪律；守卫若不自证真源非空，"
        "``\"\" in text`` 恒真就会永远不红（#316）",
    ),
    Mutation(
        "frame_consistency_discipline_cleared",
        # 锚点取常量赋值**整段**（含隐式拼接的各行，全仓恰好一次）：只改其中一行字面量
        # 杀不死守卫——真源自己就是被测断言的那份文本，改词后「含真源全文」照样成立。
        # 清空后首尾帧两条写段请求整族失去这条纪律，守卫必须先自证真源非空才会红。
        "src/minimax_h3_prompt/writing_disciplines.py",
        'FRAME_CONSISTENCY_DISCIPLINE = (\n'
        '    "本段开场状态必须与这张图逐项一致，双向对齐："\n'
        '    "**图中已有的事物绝不可写成不存在、尚未出现或「等它登场」**"\n'
        '    "（例如图中猫已站在店内，就不能写「空店」「无猫」「No cat is visible in the frame」"\n'
        '    "或让它在本段才推门进来）；"\n'
        '    "**图中没有的事物也绝不可写成已经存在或正在入场**"\n'
        '    "（例如图中门是关着的、没有猫，就不能写「门被推开」「猫冲进店内」"\n'
        '    "或「clerk swings into frame」这类与首帧矛盾的开场）。"\n'
        '    "分镜表、上文「本段剧情概述」与这张图冲突时，一律以画面为准。"\n'
        ')',
        'FRAME_CONSISTENCY_DISCIPLINE = ""',
        ("tests/test_writing_disciplines_guard.py::"
         "test_frame_consistency_discipline_reaches_both_base_route_write_requests",),
        "清空帧一致性真源＝首尾帧 v2 与回退两条写段请求同时失去这条纪律，"
        "写段器照剧本推断写开场状态（GEN004 段 2 原样复发）；"
        "守卫若不自证真源非空，``\"\" in text`` 恒真就会永远不红（#319）",
    ),
    Mutation(
        "frame_consistency_discipline_second_copy",
        # 把整句抄进另一个写段路径模块：出站文本仍旧含它，只有「源码里只许一份」那条
        # 会红。锚点取**使用行**（本票独有、别族不会在同一处汇合，且不随 import 增删漂）。
        "src/minimax_h3_prompt/segment_prompts.py",
        "    lines.append(FRAME_CONSISTENCY_DISCIPLINE)\n",
        '    _second_copy = (\n'
        '        "本段开场状态必须与这张图逐项一致，双向对齐："\n'
        '        "**图中已有的事物绝不可写成不存在、尚未出现或「等它登场」**"\n'
        '        "（例如图中猫已站在店内，就不能写「空店」「无猫」"\n'
        '        "「No cat is visible in the frame」或让它在本段才推门进来）；"\n'
        '        "**图中没有的事物也绝不可写成已经存在或正在入场**"\n'
        '        "（例如图中门是关着的、没有猫，就不能写「门被推开」「猫冲进店内」"\n'
        '        "或「clerk swings into frame」这类与首帧矛盾的开场）。"\n'
        '        "分镜表、上文「本段剧情概述」与这张图冲突时，一律以画面为准。"\n'
        '    )\n'
        "    lines.append(FRAME_CONSISTENCY_DISCIPLINE)\n",
        ("tests/test_writing_disciplines_guard.py::"
         "test_frame_consistency_discipline_has_no_second_copy_in_source",),
        "第二份整句硬拷贝＝两条路线看着同一句话、实际各改各的；出站断言杀不死它"
        "（请求里照样含这句），只有源码扫描那条会红（#319）",
    ),
    Mutation(
        "music_guided_segment_writes_na",
        # #328 把配乐纪律从指令文件搬进真源，锚点跟着搬家：指令文件里只剩
        # ``{{MUSIC_CUE_DISCIPLINE}}``，再把锚点留在那里就是 0 次（变异已不守任何东西）。
        # 锚点取**强化版独有的一小段**：``**不许写 `N/A`**`` 现在导演台与 base 两个
        # 版本各有一处，用短句会命中两次。
        "src/minimax_h3_prompt/writing_disciplines.py",
        "配器与力度如何变化，**不许写 `N/A`**；",
        "配器与力度如何变化，**无声写 `N/A`**；",
        ("tests/test_director_prompts.py::test_music_rule_continues_one_cue_across_segments",),
        "引导段配乐退回「无声写 N/A」＝段间持续曲重新断开（#194，实测 −9.8/+26.6 dB 台阶）",
    ),
    Mutation(
        "director_prop_ownership_discipline_cleared",
        # 锚点取常量赋值**整行**（全仓恰好一次，同 ``camera_speed_discipline_cleared``）：
        # 只改其中几个字不会让「出站文本含真源全文」那条红——真源自己就是被测断言的那份
        # 文本。清空后指令文件里的 ``{{PROP_OWNERSHIP_DISCIPLINE}}`` 才真的把整条纪律
        # 换成了空串，守卫的先自证非空那一句必须响。
        "src/minimax_h3_prompt/writing_disciplines.py",
        'PROP_OWNERSHIP_DISCIPLINE = "**同一件穿戴物或道具，一段里只写一处归属**：帽子、头盔、背包、箱子、手机这类一眼能数清的物件，全段只写一次「在谁身上／在谁手里」；后文不要再把它改写成「放在某处」或「另一个人拿着」——两处归属会让成品长出两件。正例：`The helmet stays on his head; the box beside him holds no second helmet.`"',
        'PROP_OWNERSHIP_DISCIPLINE = ""',
        ("tests/test_writing_disciplines.py::"
         "test_three_disciplines_reach_every_director_mode_from_the_source_module",
         # 常量级守卫：清空后 ``assert value.strip()`` 直接响，别再让它无变异覆盖。
         "tests/test_writing_disciplines.py::"
         "test_three_discipline_constants_are_non_empty_text"),
        "清空道具归属真源＝导演台四条模式的出站系统提示里整条纪律变成空串（占位符照替、"
        "纪律内容没了）；守卫若不自证真源非空，``\"\" in text`` 恒真就会永远不红（#328）",
    ),
    Mutation(
        "base_music_cue_discipline_cleared",
        # 锚点取常量赋值**整行**（全仓恰好一次，同 ``camera_speed_discipline_cleared``）：
        # 只改其中几个字不会让「出站文本含真源全文」那条红——真源自己就是被测断言的那份
        # 文本。清空后 base 两条写段路径的 f-string 才真的把整条配乐纪律换成了空串，
        # 守卫的先自证非空那一句必须响。导演台吃的是强化版，不受这条变异影响。
        "src/minimax_h3_prompt/writing_disciplines.py",
        'MUSIC_CUE_DISCIPLINE_BASE = "**配乐按「同一支 cue 延续」写，不按「本段各写各的」写**：`non_diegetic_music` 写观众能听到、角色听不到的那支配乐；本段与相邻段是同一支 cue——乐器与速度一致，只允许力度与织体变化，写清本段里力度与织体的变化。**不许写 `N/A`**，只有用户明确要求全片静音时才写 `N/A`。"',
        'MUSIC_CUE_DISCIPLINE_BASE = ""',
        ("tests/test_writing_disciplines_guard.py::"
         "test_music_cue_discipline_reaches_base_but_not_ref",
         # 常量级守卫：清空后 ``assert text`` 直接响，别再让它无变异覆盖。
         "tests/test_writing_disciplines_guard.py::"
         "test_base_music_discipline_keeps_silence_exception_only"),
        "清空配乐弱化版真源＝base 两条写段路径同时失去 cue 延续纪律，接缝电平台阶原样复发"
        "（#194 实测 −9.8/+26.6 dB）；守卫若不自证真源非空，``\"\" in text`` 恒真就会永远不红（#330）",
    ),
    Mutation(
        "base_music_asks_for_orchestration_again",
        # 要求句改回「配器与力度如何变化」＝同句前半句已经禁止的那件事又被要求一遍，
        # 模型同时收到两条打架的要求（spec #356 的 N7）。这条变异专门杀「矛盾以另一种
        # 措辞回来」：静音例外、同一支 cue、段类型词三条既有断言都会照绿，只有
        # ``test_base_music_discipline_never_asks_for_orchestration_changes`` 的
        # 「配器…变化」反向断言会响，所以目标用例点它。
        # 锚点取改后那句要求的小半句（不再含「配器」，与上面清空变异的整行锚点互不遮蔽）。
        "src/minimax_h3_prompt/writing_disciplines.py",
        "写清本段里力度与织体的变化",
        "写清本段里配器与力度如何变化",
        ("tests/test_writing_disciplines_guard.py::"
         "test_base_music_discipline_never_asks_for_orchestration_changes",),
        "弱化版重新要求写「配器…变化」＝与同句「只允许力度与织体变化」自相矛盾，"
        "模型只能二选一（spec #356 的 N7）；既有三条反向断言全绿，只有这条新反证会响（#357）",
    ),
    Mutation(
        "base_prop_ownership_discipline_cleared",
        # 锚点与 ``director_prop_ownership_discipline_cleared`` 是同一行常量赋值：两条
        # 变异各跑各的目标用例（一条钉导演台、一条钉 base 两条写段路径），清空后两边
        # 都该红。锚点判据 ``anchor_problems`` 只看**单条变异内部** ``old`` 出现几次，
        # 跨变异共用同一锚点不会报错（2026-11-18 读 ``_anchor_occurrences`` 复核）。
        # 为什么不是「把值改成别的文本」：真源自己就是被测断言的那份文本，改词后出站
        # 文本跟着变，「出站含真源全文」照样成立——只有清空能让「先自证非空」那句响。
        "src/minimax_h3_prompt/writing_disciplines.py",
        'PROP_OWNERSHIP_DISCIPLINE = "**同一件穿戴物或道具，一段里只写一处归属**：帽子、头盔、背包、箱子、手机这类一眼能数清的物件，全段只写一次「在谁身上／在谁手里」；后文不要再把它改写成「放在某处」或「另一个人拿着」——两处归属会让成品长出两件。正例：`The helmet stays on his head; the box beside him holds no second helmet.`"',
        'PROP_OWNERSHIP_DISCIPLINE = ""',
        ("tests/test_writing_disciplines_guard.py::"
         "test_detail_disciplines_reach_only_the_routes_that_should_carry_them",),
        "清空道具归属真源＝base 的 v2 与回退两条写段请求同时失去这条纪律"
        "（一段里长出两个头盔的实测形态原样复发）；守卫若不自证真源非空，"
        "``\"\" in text`` 恒真就会永远不红（#329）",
    ),
    Mutation(
        "base_threshold_crossing_discipline_cleared",
        # 干净的新常量，锚点全仓恰好一次：清空后 base 两条写段路径失去弱化版门槛纪律，
        # 导演台仍拿强化版（它走另一个常量），正好证明两版没有互相顶替。
        "src/minimax_h3_prompt/writing_disciplines.py",
        'THRESHOLD_CROSSING_DISCIPLINE_BASE = "**跨门槛／门口／门洞的动作，逐人写清门内门外与移动方向**：写到的每个人都要交代他此刻在门内还是门外、朝哪边移动，同段里提到位置的人彼此不得自相矛盾（不要一句写他在屋内、另一句又让他从门外进来）——里外不分会让屋里屋外的人一起跟着进门。正例：`The father is already inside the house, the mother stands one step behind him, and the man outside steps across the threshold into the room.`"',
        'THRESHOLD_CROSSING_DISCIPLINE_BASE = ""',
        ("tests/test_writing_disciplines_guard.py::"
         "test_detail_disciplines_reach_only_the_routes_that_should_carry_them",),
        "清空门槛弱化版真源＝base 两条写段请求失去「门内门外逐人交代」纪律"
        "（屋里的人跟着一起进门的实测形态原样复发）；守卫若不自证真源非空，"
        "``\"\" in text`` 恒真就会永远不红（#329）",
    ),
    Mutation(
        "director_threshold_crossing_discipline_cleared",
        # 锚点取常量赋值**整行**（全仓恰好一次）：真源自己就是被测断言的那份文本，
        # 只改其中几个字不会让「出站文本含真源全文」那条红——只有清空才让
        # 「先自证非空」那句响。retro 2026-11-18 补：真源里每个公开常量都要有变异。
        "src/minimax_h3_prompt/writing_disciplines.py",
        'THRESHOLD_CROSSING_DISCIPLINE_DIRECTOR = "**跨门槛／门口／门洞的动作，逐人写清门内门外与移动方向**：任何一人跨门槛、穿门口或进门洞，都在同一句里写死他**此刻在门内还是门外**、**朝哪边移动**；同段里没动的人也要交代相对位置（谁在屋内、谁在谁身后一步）——只写「走进门」而不写谁已经在里面，成品会让屋外的人跟着一起进来。正例：`The father is already inside the house, the mother stands one step behind him, and the man outside steps across the threshold into the room.`"',
        'THRESHOLD_CROSSING_DISCIPLINE_DIRECTOR = ""',
        (
         "tests/test_writing_disciplines_guard.py::test_detail_disciplines_reach_only_the_routes_that_should_carry_them",
        ),
        "清空门槛强化版真源＝导演台四条模式的出站系统提示里那条纪律变成空串（段类型分叉版只此一份）；导演台吃的是这一个常量，base 走弱化版不受影响（#328）",
    ),
    Mutation(
        "director_music_cue_discipline_cleared",
        # 锚点取常量赋值**整行**（全仓恰好一次）：真源自己就是被测断言的那份文本，
        # 只改其中几个字不会让「出站文本含真源全文」那条红——只有清空才让
        # 「先自证非空」那句响。retro 2026-11-18 补：真源里每个公开常量都要有变异。
        "src/minimax_h3_prompt/writing_disciplines.py",
        'MUSIC_CUE_DISCIPLINE_DIRECTOR = "**配乐按「同一支 cue 延续」写，不按「本段各写各的」写**：`non_diegetic_music` 写观众能听到、角色听不到的那支配乐。**引导段**（续接 continue / 运镜·同场景 camera_same / 收尾定格 ending）的音画已被段间引导钉在上一段上，配乐必须写成同一支 cue 在本段的延续——写清配器与力度如何变化，**不许写 `N/A`**；**切镜段**（运镜·切镜 camera_cut）与相邻段写同一支 cue——乐器与速度一致，只允许力度与织体变化；**开场段**（开场 opening）确立全片那支 cue，后面的段都延续它。"',
        'MUSIC_CUE_DISCIPLINE_DIRECTOR = ""',
        (
         "tests/test_writing_disciplines_guard.py::test_music_cue_discipline_reaches_base_but_not_ref",
        ),
        "清空配乐强化版真源＝导演台四条模式的出站系统提示里配乐分档纪律整条消失，接缝电平台阶原样复发（#194 实测 −9.8/+26.6 dB）；base 走弱化版不受影响（#328）",
    ),
    Mutation(
        "future_events_title_cleared",
        # 锚点取常量赋值**整行**（全仓恰好一次）：真源自己就是被测断言的那份文本，
        # 只改其中几个字不会让「出站文本含真源全文」那条红——只有清空才让
        # 「先自证非空」那句响。retro 2026-11-18 补：真源里每个公开常量都要有变异。
        "src/minimax_h3_prompt/writing_disciplines.py",
        'FUTURE_EVENTS_TITLE = "【时间窗外剧情禁区】以下事件属于其他视频段，本段绝对不能出现其中任何一个："',
        'FUTURE_EVENTS_TITLE = ""',
        (
         "tests/test_writing_disciplines_guard.py::test_every_write_route_sends_the_family_discipline",
        ),
        "清空时间窗外标题句＝三条块形态路线的请求只剩清单、没有「以下事件属于其他视频段」这句前置约束，模型会把清单读成剧情素材（#314）",
    ),
    Mutation(
        "context_unmigration_list_renamed",
        # retro 2026-11-18：三处代码注释写着「见 CONTEXT.md 的「不迁清单」」。把 CONTEXT.md
        # 里那一节的标题改名，名字就从全文消失——注释不改、文件不改别的行，任何既有检查
        # 都不报错，只有「指名指针必须能解析」那条会红。这正是 #328 当初踩的形态
        # （注释指向一份当时根本不存在的清单）。
        "CONTEXT.md",
        "## 不迁清单（刻意不搬的导演台纪律）",
        "## 迁移决定（刻意不搬的导演台纪律）",
        ("tests/test_doc_pointer_shape.py::test_context_md_named_pointers_resolve",),
        "CONTEXT.md 的节名被改＝代码与活文档里指名它的指针集体悬空，读的人按名字找不到东西"
        "（#327 实测：三处注释指向一份不存在的清单，靠人工 grep 才发现）",
    ),
    Mutation(
        "writing_discipline_constant_without_mutation",
        # retro 2026-11-18：往真源模块加一个公开常量**而不给它变异**，正是「新加守卫必须
        # 补一条变异」要防的形态。锚点取唯一的一行注释，插在它后面（模块级赋值，合法位置）。
        "src/minimax_h3_prompt/writing_disciplines.py",
        "# 配乐纪律的**base 弱化版**（已由 #330 接进 base 的两条写段路径）：",
        "# 配乐纪律的**base 弱化版**（已由 #330 接进 base 的两条写段路径）："
        "\n"
        'UNGUARDED_PROBE_DISCIPLINE = "探针：没有任何变异清空它"',
        ("tests/test_mutation_anchors.py::"
         "test_every_writing_discipline_constant_has_a_mutation",),
        "真源里多一个没人管的常量＝那条纪律没有任何「会红」的证明；2026-11-18 spec #327 "
        "交付时就漏了两条（导演台门槛与配乐强化版），两轴复审都没抓到",
    ),
    # ------------------------------------------------------------------
    # #334 三处单边字段的删除证据（票面 AC「每处删除配一条能变红的用例」）
    # ------------------------------------------------------------------
    Mutation(
        "qa_issues_field_resurrected",
        # 把删掉的 ``qa_issues`` 加回 schema 而**不**登记出处：状态契约双向核对当场红。
        # 这就是「删除必须同步三处」的机读证明——只加声明不加白名单/排除表就是删了一半。
        "src/minimax_h3_prompt/graph/state.py",
        "    iterations: int",
        "    qa_issues: list[str]\n    iterations: int",
        ("tests/test_state_contract.py"
         "::test_every_schema_key_is_persisted_or_explicitly_excluded",),
        "qa_issues 复活且没有持久化归属＝契约本体又一次对不上（#334 删它的理由是"
        "「自首个提交起就没有写者」，不是「暂时不用」）",
    ),
    Mutation(
        "persistence_pointer_back_to_line_number",
        # 把持久化表的理由改回行号：指针守卫必须红。行号会随重构漂移且没有任何检查会
        # 报错——#334 之前全表 8 个行号没有一个指得到它声称的那行。
        "src/minimax_h3_prompt/session_store.py",
        "阶段 1 断点书签：ui.wizard._progress_saver 写、ui.wizard._resume_stage1 读",
        "阶段 1 断点书签：wizard.py:477 写、:1846 读",
        ("tests/test_state_contract.py"
         "::test_persistence_reasons_point_at_symbols_not_line_numbers",),
        "理由写回行号＝读的人按注释找代码会扑空（#334 票面点名的形态），"
        "而文件本身还在、任何检查都不会报错",
    ),
    Mutation(
        "persistence_whitelist_keeps_a_removed_key",
        # 反向：白名单里留着已删的键（删除时忘了同步持久化表）。反向核对当场红——
        # 这正是票面 AC「若删除字段，状态声明与持久化契约同步更新」的机读证明。
        "src/minimax_h3_prompt/session_store.py",
        '"sound_design", "music", "final_prompt", "_progress",',
        '"sound_design", "music", "final_prompt", "final_report", "_progress",',
        ("tests/test_state_contract.py"
         "::test_every_persisted_key_is_in_the_schema_or_runs_outside_the_graph",),
        "白名单留着一个 schema 里没有、也没登记成「图外」的键＝保存时把它当产物写盘，"
        "而状态契约上它根本不存在（#334 删除 final_report 时忘了同步就是这种形态）",
    ),

    Mutation(
        "persistence_pointer_names_missing_symbol",
        # 指针写成真模块 + 不存在的符号：解析守卫必须红（模块名对、符号错了也要抓）。
        "src/minimax_h3_prompt/session_store.py",
        "ui.wizard._progress_saver 写",
        "ui.wizard._no_such_saver 写",
        ("tests/test_state_contract.py::test_persistence_reason_pointers_resolve",),
        "指针解析不到＝「模块.符号」退化成一句安慰；符号被重命名时这条守卫是唯一会响的"
        "（行号时代的等价缺陷没有任何检查能报）",
    ),
    Mutation(
        "finalize_stops_repairing_the_prompt",
        # 删报告时顺手把修复也删掉：「删除字段」与「删除行为」是两件事。这条证明后者
        # 有回归用例兜着。
        "src/minimax_h3_prompt/graph/nodes.py",
        '        repaired, _notes = assemble_and_repair(state.get("final_prompt", ""), brief.mode)\n'
        '        return {"final_prompt": repaired}',
        '        repaired, _notes = assemble_and_repair(state.get("final_prompt", ""), brief.mode)\n'
        '        return {}',
        ("tests/test_state_single_sided_fields.py"
         "::test_finalize_still_repairs_the_prompt_it_writes_back",),
        "finalize 不再写回修好的正文＝抽围栏/补段头整条静默失效，产物形状退回模型原样"
        "（#334 删的是报告字段，不是修复）",
    ),
    Mutation(
        "stage2_refine_loop_stops_running_the_format_validator",
        # ``finalize`` 删掉 validate_prompt/theme_fidelity_issues 的前提，是精修循环里
        # 同一对校验器照旧在跑（而且结论会驱动重写，比「只进报告」多）。把这一处掐掉＝
        # 阶段 2 的确定性校验整条消失，产物可能带着格式错误进交付。
        "src/minimax_h3_prompt/graph/pipeline.py",
        "        issues = validate_prompt(prompt, brief.mode, duration=brief.duration,\n"
        "                                 variant=brief.variant, ref_meta=ref_meta)",
        "        issues = []",
        ("tests/test_state_single_sided_fields.py::"
         "test_stage2_refine_loop_runs_the_same_two_validators_finalize_dropped",),
        "阶段 2 不再跑格式校验＝「finalize 的那对校验器已由精修循环覆盖」这条等价性作废，"
        "而它正是 #334 删 finalize 校验调用的唯一理由",
    ),
    Mutation(
        "removed_keys_survive_a_resave",
        # 白名单里加回任一已删键：旧会话再保存时它会被**留在文件里**（而不是静默收敛）。
        # 这条钉住「删除也要能续跑」的另一半——残留读回来无害，但必须被后续保存淘汰。
        "src/minimax_h3_prompt/session_store.py",
        '"sound_design", "music", "final_prompt", "_progress",',
        '"sound_design", "music", "final_prompt", "shot_duration_warning", "_progress",',
        ("tests/test_state_single_sided_fields.py"
         "::test_old_session_files_with_removed_keys_still_resume",),
        "已删键留在白名单＝旧会话里的残留每次保存都被原样抄回去，删了等于没删",
    ),
    Mutation(
        "storyboard_uniform_warning_silenced",
        # 警告只剩控制台这一个出口（state 字段已删）：把这个出口也掐掉就彻底静默了。
        "src/minimax_h3_prompt/graph/nodes.py",
        '                print(f"[警告] SHOT_DURATIONS_UNIFORM: 重试后分镜时长仍是均分（"\n'
        '                      f"{durations[0]:.1f}s × {len(durations)}），请人工检查节奏", flush=True)',
        '                pass',
        ("tests/test_state_single_sided_fields.py"
         "::test_storyboard_warns_on_console_and_writes_no_warning_field",),
        "均分重试仍失败却没有警告＝用户拿到节奏平的分镜且不知情；该字段删掉后控制台是"
        "唯一出口（#334）",
    ),
    Mutation(
        "end_hook_placement_guard_off",
        "src/minimax_h3_prompt/director/planning.py",
        "    text = str(end_hook or \"\")\n"
        "    if not any(word in text for word in END_HOOK_STILL_WORDS):\n"
        "        return False",
        "    text = str(end_hook or \"\")\n"
        "    if True:\n"
        "        return False",
        ("tests/test_director_plan.py::test_real_end_hooks_are_judged_by_the_narrow_placement_criterion",),
        "摆位判据恒假＝「摆好位置站定、画面静止」的段尾原样落盘，接缝照样停顿（#196）。"
        "真实样本第 9 条必须变红",
    ),
    Mutation(
        "first_shot_disconnect_never_fires",
        "src/minimax_h3_prompt/director/prompts.py",
        "        if _first_shot_disconnected(body, segment, previous_text):\n",
        "        if False and _first_shot_disconnected(body, segment, previous_text):\n",
        ("tests/test_director_prompts.py::test_guided_segment_first_shot_with_no_overlap_is_flagged",
         "tests/test_director_prompts.py::test_disconnected_first_shot_goes_through_the_rewrite_loop"),
        "首镜承接守卫恒不触发＝凭空重新起势的开头照落盘，写段期毫无信号（#197）",
    ),
    Mutation(
        "r2v_start_block_dropped",
        "src/minimax_h3_prompt/director/prompts.py",
        '    if plan.mode not in ("fl2v", "r2v", "t2v", "i2v"):',
        '    if plan.mode != "fl2v":',
        ("tests/test_director_prompts.py::"
         "test_r2v_guided_and_opening_requests_state_the_segment_start",),
        "起点事实块退回只认 fl2v＝r2v 引导段拿不到上一段落定画面（#192）",
    ),
    Mutation(
        "t2v_start_block_gate_dropped",
        "src/minimax_h3_prompt/director/prompts.py",
        '    if plan.mode not in ("fl2v", "r2v", "t2v", "i2v"):',
        '    if plan.mode not in ("fl2v", "r2v"):',
        ("tests/test_director_prompts.py::"
         "test_t2v_start_block_states_the_previous_settled_frame_without_a_first_frame_image",),
        "模式闸退回只认 fl2v/r2v＝t2v 的引导段拿不到上一段落定画面、切镜段也不会"
        "被写成硬切（#302 的起点事实缺口原形）",
    ),
    Mutation(
        "end_hook_request_reads_as_a_held_endpoint",
        "src/minimax_h3_prompt/director/prompts.py",
        r'f"本段最后一帧的画面（end_hook）：{segment.end_hook}。{END_HOOK_CUT_CLAUSE}\n"',
        r'f"本段落定的画面（end_hook，正文必须落到这个状态）：{segment.end_hook}\n"',
        ("tests/test_director_prompts.py::test_segment_request_reads_end_hook_as_the_cut_point",),
        "请求行退回「落到终点」口径＝模型又去追静止构图，切点纪律在最近处被反着说（#193）",
    ),
    Mutation(
        "designer_visible_text_follows_working_language",
        "src/minimax_h3_prompt/prompts/character_designer.md",
        "画面内可见文字的语言由故事发生地/时代决定，不跟随工作语言",
        "画面内可见文字的语言跟随工作语言",
        ("tests/test_designer_visible_text_language.py::"
         "test_designers_set_visible_text_language_from_the_story_not_the_working_language",),
        "可见文字语言跟随工作语言＝美国故事里烧进中文角标（#191 实测形态，#195）",
    ),
    Mutation(
        "t2v_segments_ask_for_images",
        "src/minimax_h3_prompt/director/frames.py",
        '    if mode == "t2v":\n        return ()\n',
        "",
        ("tests/test_director_plan.py::test_give_images_follow_type_and_mode",
         "tests/test_director_tailframe.py::"
         "test_tailframe_triggers_only_for_moving_and_ending_segments"),
        "删掉 t2v 早返回＝t2v 的运镜/收尾段重新索要终点画面：白费一次生图调用、"
        "写段请求里还会点名一张不存在的图（#199 实测 established by Picture 2，#200）",
    ),
    Mutation(
        "t2v_picture_guard_off",
        "src/minimax_h3_prompt/director/prompts.py",
        '    if not mode_has_image_slots(mode):\n        picture = _PICTURE_REFERENCE_RE.search(body)',
        '    if False and not mode_has_image_slots(mode):\n        picture = _PICTURE_REFERENCE_RE.search(body)',
        ("tests/test_director_prompts.py::"
         "test_t2v_segment_naming_a_picture_without_angle_brackets_is_flagged",
         "tests/test_director_prompts.py::"
         "test_t2v_segment_naming_a_picture_in_angle_brackets_is_flagged"),
        "关掉这条判据，t2v 段词里会重新出现 Picture N（指一张不存在的图），"
        "贴给导演台即污染执行（#201）",
    ),
    Mutation(
        "cancel_ignored_at_node_boundary",
        "src/minimax_h3_prompt/api/jobs.py",
        # 锚点必须带上下文：这三行（12 空格缩进的 if + raise）在 #219 之后被复制进
        # 阶段 2 的回调，裸锚点当场变成「出现 3 次」，变异静默失效（2026-11-10 实测）。
        "            # 先看取消再报下一节点开始：取消后那个节点根本不会跑，先发 started\n"
        "            # 会让界面显示一个从未执行过的节点（文案必须诚实）。\n"
        "            if cancel_event.is_set():\n"
        "                raise JobCancelled(node_name)",
        "            # 先看取消再报下一节点开始：取消后那个节点根本不会跑，先发 started\n"
        "            # 会让界面显示一个从未执行过的节点（文案必须诚实）。\n"
        "            if False and cancel_event.is_set():\n"
        "                raise JobCancelled(node_name)",
        ("tests/test_api_jobs.py::"
         "test_cancel_stops_after_the_current_node_and_advance_resumes",),
        "取消标记不在节点边界生效＝点了取消仍跑到闸门，job 记成功（#217 / D2-AC8）",
    ),
    Mutation(
        "startup_reconcile_off",
        "src/minimax_h3_prompt/api/jobs.py",
        "            if isinstance(pid, int) and pid != os.getpid() and _pid_alive(pid):\n"
        "                # 另一个还活着的服务进程在持有这个会话：不动它，避免双写。\n"
        "                continue",
        "            if True:\n"
        "                continue",
        ("tests/test_api_jobs.py::"
         "test_startup_reconciliation_marks_interrupted_and_advance_resumes",),
        "关掉启动对账＝强杀后会话永远停在 running，再也不能推进（#217 / D2-AC9）",
    ),
    Mutation(
        "single_writer_gate_off",
        "src/minimax_h3_prompt/api/jobs.py",
        "                self.store.ensure_can_advance(current)",
        "                pass  # self.store.ensure_can_advance(current)",
        ("tests/test_api_jobs.py::"
         "test_concurrent_advance_is_rejected_with_session_busy",),
        "去掉单写入者闸门＝并发 advance 各自起一个 job，同一会话被两个任务同时写（#217 / D2-AC3）",
    ),
    Mutation(
        "session_lock_off",
        "src/minimax_h3_prompt/workbench_store.py",
        "        lock = self._lock_for(session_id)\n"
        "        with lock:",
        "        lock = self._lock_for(session_id)\n"
        "        if True:",
        ("tests/test_workbench_store.py::"
         "test_concurrent_updates_do_not_lose_each_others_fields",),
        "去掉会话锁＝进度写与取消写互相覆盖、revision 丢更新（#217 复审）",
    ),
    Mutation(
        "auth_host_check_off",
        "src/minimax_h3_prompt/api/auth.py",
        "        if host is None or _host_header_hostname(host) not in LOOPBACK_HOSTNAMES:",
        "        if False:",
        ("tests/test_api_auth.py::test_non_loopback_host_is_forbidden",),
        "去掉 Host 校验＝跨站页面换一个 Host 头就能读本机响应（#223 / D3-AC1）",
    ),
    Mutation(
        "auth_origin_check_off",
        "src/minimax_h3_prompt/api/auth.py",
        "        if origin is not None and _origin_hostname(origin) not in LOOPBACK_HOSTNAMES:",
        "        if False:",
        ("tests/test_api_auth.py::test_cross_site_origin_is_forbidden",),
        "去掉 Origin 校验＝同机其它 origin 的脚本可以带凭据打进来（#223 / D3-AC1）",
    ),
    Mutation(
        "auth_token_compare_off",
        "src/minimax_h3_prompt/api/auth.py",
        "    return hmac.compare_digest(presented.encode(\"utf-8\"), expected.encode(\"utf-8\"))",
        "    return True",
        ("tests/test_api_auth.py::test_wrong_header_credential_is_rejected",),
        "不比令牌＝任何字符串都能通过，启动令牌变成装饰（#223 / D3-AC2）",
    ),
    Mutation(
        "auth_cookie_credential_off",
        "src/minimax_h3_prompt/api/auth.py",
        "        if _matches(_cookie_token(headers), self.token):\n            return None",
        "        if False:\n            return None",
        ("tests/test_api_auth.py::test_cookie_credential_passes",),
        "只认请求头＝<video>/<img> 由浏览器自己发请求、挂不上头，媒体永远 401（#223 / D3-AC3）",
    ),
    Mutation(
        "auth_exempt_surface_off",
        "src/minimax_h3_prompt/api/auth.py",
        "        if is_exempt(method, path):\n            return None",
        "        if False:\n            return None",
        ("tests/test_api_auth.py::test_health_endpoint_is_exempt",),
        "豁免面失守＝健康检查被令牌挡住，排障入口先于业务不可用（#223 / D3-AC2）",
    ),
    Mutation(
        "own_module_pointer_scan_off",
        "tests/test_doc_pointer_shape.py",
        '    return re.compile(r"(?:" + "|".join(map(re.escape, names)) + r")\\.py:\\d+")',
        '    return re.compile(r"(?!)")',
        ("tests/test_doc_pointer_shape.py::test_own_module_scan_recognises_only_own_modules",),
        "本仓模块名的扫描式退化成永不命中＝活文档里的行号指针一路绿（#224 复审实测）",
    ),
    Mutation(
        "adr_malformed_status_unchecked",
        "tests/test_adr_guard.py",
        "                 if _STATUS_LIKE.match(line) and not _STATUS_VALID.match(line)]",
        "                 if False and _STATUS_LIKE.match(line) and not _STATUS_VALID.match(line)]",
        ("tests/test_adr_guard.py::test_a_malformed_status_label_is_rejected",),
        "只数合规行、不拒绝畸形状态行＝「状态（…验收）」静默放行（#226 实测）",
    ),
    Mutation(
        "duration_scope_unchecked",
        "src/minimax_h3_prompt/api/routes/sessions.py",
        "    _validate_duration(payload.duration)\n",
        "    pass  # 变异：跳过时长校验\n",
        ("tests/test_api_sessions.py::test_create_session_rejects_duration_out_of_scope",),
        "不校验时长＝短表单收下 30 秒与 0 秒，duration_out_of_scope 永不出现（#228 / D4-AC1）",
    ),
    Mutation(
        "stage1_artifacts_not_written",
        "src/minimax_h3_prompt/api/jobs.py",
        "        self._write_stage1_artifacts(session_id, job_id, final_state)\n"
        "        return final_state\n"
        "\n"
        "    def _execute_stage1_fake",
        "        return final_state\n"
        "\n"
        "    def _execute_stage1_fake",
        ("tests/test_api_stage1_artifacts.py"
         "::test_text_route_writes_script_and_shot_table_artifacts",),
        "阶段 1 收尾不落产物＝审查面只剩 stage_state，用户没有任何可看可复制的东西"
        "（#228 / D4-AC7）",
    ),
    Mutation(
        "resolved_revisions_not_echoed",
        "src/minimax_h3_prompt/api/jobs.py",
        '                resolved_update: dict[str, Any] = {\n'
        '                    "status": "resolved", "decision": recorded_decision,\n'
        '                    "resolved_at": now,\n'
        '                    "revisions": recorded_revisions,\n'
        '                }',
        '                resolved_update: dict[str, Any] = {\n'
        '                    "status": "resolved", "decision": recorded_decision,\n'
        '                    "resolved_at": now,\n'
        '                }',
        ("tests/test_api_action_items.py"
         "::test_resolved_item_echoes_the_revisions_it_was_resolved_with",),
        "待办不记决议载荷＝修订历史被静默丢掉，用户看不到自己上一轮说过什么"
        "（#228 / D4-AC8）",
    ),
    Mutation(
        "frontend_hardcodes_artifact_name",
        "frontend/src/features/sessions/ArtifactsTab.vue",
        "const byName = computed(() => new Map(props.artifacts.map((item) => [item.name, item])))\n",
        "const byName = computed(() => new Map(props.artifacts.map((item) => [item.name, item])))\n"
        "const _MUTATED_ARTIFACT = 'video-prompt.md'\n",
        ("tests/test_frontend_artifact_names.py"
         "::test_frontend_does_not_hardcode_artifact_names",),
        "组件抄产物文件名＝后端登记表改名后前端静默少显示（ADR 0034；2026-11-12 retro）",
    ),
    Mutation(
        "frontend_progress_band_inside_overview",
        "frontend/src/features/sessions/SessionDetailView.vue",
        '''      <div class="progress-region">
        <ProgressBand v-if="progressView.steps.length" :view="progressView" />
        <p v-if="pendingHint" role="status" class="pending-hint">等待你操作：{{ pendingHint }}</p>
      </div>

      <!-- 概览与进度 -->
      <section
        v-if="ui.workbenchTab === 'overview'"
        id="workbench-panel-overview"
        role="tabpanel"
        aria-labelledby="workbench-tab-overview"
        class="panel"
      >
        <ErrorNotice v-if="runError" :message="runError" />''',
        '''      <div class="progress-region">
        <p v-if="pendingHint" role="status" class="pending-hint">等待你操作：{{ pendingHint }}</p>
      </div>

      <!-- 概览与进度 -->
      <section
        v-if="ui.workbenchTab === 'overview'"
        id="workbench-panel-overview"
        role="tabpanel"
        aria-labelledby="workbench-tab-overview"
        class="panel"
      >
        <ErrorNotice v-if="runError" :message="runError" />

        <ProgressBand v-if="progressView.steps.length" :view="progressView" />''',
        ("src/features/sessions/SessionDetailView.spec.ts"
         "::切到任一分区，步骤带仍然渲染且当前步骤正确",),
        "步骤带移回概览 panel 内部＝切到分段/产物/审查就看不到当前进度"
        "（spec #274 复检的直接成因；#251 真实验收实测 / #278 改成真实组件用例）",
        runner="vitest",
    ),
    Mutation(
        "fake_switch_ignored",
        "src/minimax_h3_prompt/api/app.py",
        "    fake = fake_pipeline_enabled()\n",
        "    fake = False\n",
        ("tests/test_api_fake_pipeline.py"
         "::test_fake_pipeline_runs_both_stages_without_building_any_model",),
        "环境变量开着也照跑真实管线＝CI 的 E2E 会去打真实模型（#228 / D4-AC15 前提）",
    ),
    Mutation(
        "spike_add_data_dropped",
        "scripts/build_spike_exe.py",
        '        f"{SCRIPTS_DIR / PAGE_NAME};.",\n',
        '        f"{SCRIPTS_DIR / PAGE_NAME}.missing;.",\n',
        ("tests/test_spike_exe.py::test_pyinstaller_args_shape",),
        "本地页面不进包＝窗口起来一片空白，而构建照样报 PASS（#236）",
    ),
    Mutation(
        "spike_copy_metadata_dropped",
        "scripts/build_spike_exe.py",
        '        "--copy-metadata",\n        "pywebview",\n',
        "",
        ("tests/test_spike_exe.py::test_pyinstaller_args_shape",),
        "dist-info 不进包＝冻结后 importlib.metadata 读不到版本号，EXE 自报「未安装」，"
        "证据静默断掉（#236 首次构建实测）",
    ),
    Mutation(
        "spike_dist_dir_named_dist",
        "scripts/build_spike_exe.py",
        '    return str(Path(args.build_root) / "spike")',
        '    return str(Path(args.build_root) / "dist")',
        ("tests/test_spike_exe.py::test_build_output_dir_is_not_named_dist",),
        "输出目录改回 dist＝和 Vite 的 frontend/dist 同名互相覆盖（spec #235 §9）",
    ),
    Mutation(
        "path_root_derivation_reintroduced",
        "src/minimax_h3_prompt/config.py",
        "CONFIG_PATH = paths.config_path()\n",
        'CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "agent.yaml"\n',
        ("tests/test_path_root_guard.py::test_source_only_paths_module_derives_roots",),
        "根路径推导重新散落回业务模块＝冻结后静默指向不存在的解包路径，"
        "dist/数据根/配置一起失效且不报错（#237 / spec #235 §6）",
    ),
    Mutation(
        "log_redaction_off",
        "src/minimax_h3_prompt/logging_setup.py",
        "        text = text.replace(value, REDACTION_PLACEHOLDER)\n",
        "        text = text\n",
        ("tests/test_logging_setup.py::test_secret_values_are_redacted_before_write",),
        "脱敏失效＝密钥值原样写进 data/logs/ 并随诊断包外发（#239 / spec §8.1）",
    ),
    Mutation(
        "log_retention_off",
        "src/minimax_h3_prompt/logging_setup.py",
        "        if day is None or day >= cutoff:\n            continue\n",
        "        if day is None:\n            continue\n",
        ("tests/test_logging_setup.py::test_retention_keeps_last_n_days",),
        "保留窗口失效＝日志无限增长，N 天配置写了也不生效（#239 / spec §8.1）",
    ),
    Mutation(
        "log_dir_empty_switch_off",
        "src/minimax_h3_prompt/logging_setup.py",
        "        return Path(raw).expanduser() if raw else None\n",
        "        return Path(raw).expanduser() if raw else paths.logs_dir()\n",
        ("tests/test_logging_setup.py::test_empty_log_dir_env_means_no_file_logging",),
        "「空串＝不落盘」失效＝全量用例往仓库 logs/ 写文件，干净检出被污染（#239）",
    ),
    Mutation(
        "bundle_redaction_off",
        "src/minimax_h3_prompt/diagnostics.py",
        "    archive.writestr(name, logging_setup.redact_text(text, secrets))\n",
        "    archive.writestr(name, text)\n",
        ("tests/test_api_diagnostics.py::test_bundle_never_contains_secret_values",),
        "诊断包脱敏失效＝导出即泄密，假 key 自检这条硬条当场失守（#239 / spec §8.2）",
    ),
    # D5-6（#241）打包流水线：这四处漏了 PyInstaller 都不报错，只会「静默少东西」。
    Mutation(
        "exe_package_data_dropped",
        "src/minimax_h3_prompt/selfcheck.py",
        'PACKAGE_DATA_DIRS: tuple[str, ...] = ("prompts", "knowledge", "references")',
        'PACKAGE_DATA_DIRS: tuple[str, ...] = ("knowledge", "references")',
        ("tests/test_build_exe.py::test_pyinstaller_args_shape",),
        "漏收包内资源＝冻结后 prompts/knowledge 读不到，白屏或升版即挂（#241）",
    ),
    Mutation(
        "exe_own_dist_info_dropped",
        "scripts/build_exe.py",
        'METADATA_PACKAGES: tuple[str, ...] = (\n'
        '    "minimax-h3-prompt", "pywebview", "pythonnet", "clr-loader",\n)',
        'METADATA_PACKAGES: tuple[str, ...] = (\n'
        '    "pywebview", "pythonnet", "clr-loader",\n)',
        ("tests/test_build_exe.py::test_pyinstaller_args_shape",),
        "漏自己的 dist-info＝_resolve_version 抛 PackageNotFoundError，EXE 起不来（#241）",
    ),
    Mutation(
        "exe_output_dir_named_dist",
        "scripts/build_exe.py",
        "    return str(Path(args.build_root) / DIST_NAME)\n",
        '    return str(Path(args.build_root) / "dist")\n',
        ("tests/test_build_exe.py::test_pyinstaller_args_shape",),
        "输出目录叫 dist＝与 frontend/dist 互相覆盖（spec §9 的改名裁定）",
    ),
    Mutation(
        "exe_ffmpeg_dropped",
        "scripts/build_exe.py",
        '        "--add-binary",\n'
        '        f"{ffmpeg_path}{os.pathsep}imageio_ffmpeg/binaries",\n',
        "        #（变异：漏收 ffmpeg 二进制）\n",
        ("tests/test_build_exe.py::test_pyinstaller_args_shape",),
        "漏 ffmpeg＝冻结后接缝审计/拼接全挂，而构建不会报错（spec §9）",
    ),
    # D5 两轴复审（#243）：同一条事实被抄成多份时的守卫——改一处，另一处不许静默过时。
    Mutation(
        "accept_d5_secret_values_copied",
        "scripts/accept_d5.py",
        "from minimax_h3_prompt.logging_setup import secret_values  # noqa: E402  先补路径再导入",
        "from minimax_h3_prompt.logging_setup import redact_text as secret_values  # noqa: E402  先补路径再导入",
        ("tests/test_accept_d5.py::test_secret_values_uses_the_logging_setup_implementation",),
        "验收脚本自带一份脱敏实现＝脚本判过、服务端日志判不过（#243 复审）",
    ),
    Mutation(
        "credential_suffix_dropped",
        "src/minimax_h3_prompt/logging_setup.py",
        '    "_API_KEY", "_TOKEN", "_SECRET", "_PASSWORD", "_CREDENTIAL")\n',
        '    "_API_KEY", "_TOKEN", "_SECRET", "_PASSWORD")\n',
        ("tests/test_api_diagnostics.py::test_credential_suffix_is_redacted_in_logs_and_bundle",),
        "密钥后缀表少了 _CREDENTIAL＝日志与诊断包对同一环境变量一半判一半不判（#243 复审）",
    ),
    Mutation(
        "selfcheck_template_names_copied",
        "src/minimax_h3_prompt/selfcheck.py",
        "from .bootstrap import CONFIG_TEMPLATE_NAMES\n",
        'CONFIG_TEMPLATE_NAMES = ("agent.yaml", "image.yaml", "workflow.yaml", "library.yaml")\n',
        ("tests/test_selfcheck.py::test_config_template_names_come_from_bootstrap",),
        "模板清单抄第二份＝bootstrap 加一份模板时自检永远查不到（#243 复审）",
    ),
    Mutation(
        "verify_bundle_ignores_package_data_dirs",
        "scripts/build_exe.py",
        '        **{\n'
        '            f"包内资源 {name}": internal / "minimax_h3_prompt" / name\n'
        '            for name in PACKAGE_DATA_DIRS\n'
        '        },\n',
        '        "角色提示词": internal / "minimax_h3_prompt" / "prompts",\n'
        '        "领域知识": internal / "minimax_h3_prompt" / "knowledge",\n'
        '        "参考资料": internal / "minimax_h3_prompt" / "references",\n',
        ("tests/test_build_exe.py::test_verify_bundle_checks_every_package_data_dir",),
        "核对项写死三处＝新增资源目录时打包收了但校验不查（#243 复审）",
    ),
    Mutation(
        "env_port_beats_cli",
        "src/minimax_h3_prompt/api/__main__.py",
        "    if requested is not None:\n        return requested\n",
        "    if requested is None:\n        return requested\n",
        ("tests/test_api_entry.py::test_cli_port_beats_env",),
        "命令行不再压过 H3_PORT＝显式 `--port` 被环境变量静默盖掉（spec §5.2 的优先级）",
    ),
    Mutation(
        "appdata_dir_not_created",
        "src/minimax_h3_prompt/bootstrap.py",
        "        target.mkdir(parents=True, exist_ok=True)\n",
        "        pass\n",
        ("tests/test_config_bootstrap.py::test_frozen_start_creates_the_appdata_dir_without_an_env_file",),
        "不建 %APPDATA% 目录＝缺密钥提示让人复制的路径在干净机器上不存在（#243 复审）",
    ),
    Mutation(
        "missing_key_message_copied",
        "src/minimax_h3_prompt/model_factory.py",
        "        raise ConfigError(config_module.missing_api_key_message(\n"
        '            settings, context=f"LLM_PROVIDER={settings.provider}")) from exc\n',
        "        raise ConfigError(\n"
        '            f"缺失 {settings.api_key_env}：请在 {settings.purpose} 中配置"\n'
        '            f"（LLM_PROVIDER={settings.provider}）。") from exc\n',
        ("tests/test_model_config.py::test_missing_key_message_comes_from_one_builder",),
        "缺 key 文案各拼一份＝改落点只改一处，另一处把用户指到旧路径（#243 复审）",
    ),
    Mutation(
        "remove_path_skips_junction_guard",
        "scripts/worktree.sh",
        '    _unlink_worktree_deps "$target" 0 || exit 1\n'
        '    _refuse_top_level_junctions "$target" || exit 1\n',
        "    : # mutation: 跳过联接守卫\n",
        ("tests/test_worktree_script.py::test_remove_path_unlinks_frontend_junction_in_self_made_tree",),
        "自建工作树删前不摘联接＝git worktree remove 穿透删进主树依赖（2026-11-12 实测）",
    ),
    Mutation(
        "tests_do_not_isolate_host_env",
        "tests/conftest.py",
        '    allowed = os.getenv("H3_TEST_USE_ENV", "").strip().lower() in {"1", "true", "yes"}\n'
        "    if allowed:\n"
        "        yield\n"
        "        return\n",
        "    yield\n    return\n",
        ("tests/test_clean_env.py::test_default_mode_really_removes_the_api_keys",),
        "测试默认不隔离本机 .env＝同一条用例在开发机绿、干净检出红（2026-11-12 实测）",
    ),
    Mutation(
        "machine_path_sneaks_into_scripts",
        "scripts/ab_b_arm.py",
        '    PYTHONPATH="$(pwd -W)/src" <主树>/.venv/Scripts/python.exe \\\n',
        # 故意拼出本机路径，但**不在源码里写成连续字面量**：否则 test_no_machine_paths
        # 会咬到这条变异自己的夹具（守卫咬夹具不是守卫咬缺陷）。运行时仍是完整路径。
        '    PYTHONPATH="$(pwd -W)/src" '
        + "D:" + "/项目/python/MiniMax-H3-prompt/.venv/Scripts/python.exe \\\n",
        ("tests/test_no_machine_paths.py::test_no_machine_specific_paths_in_shipped_code",),
        "随仓脚本写死本机绝对路径＝换机器即错（accept_d5 复审曾抓到同款）",
    ),
    Mutation(
        "progress_stages_not_derived",
        "src/minimax_h3_prompt/workbench_store.py",
        "_PROGRESS_STAGES = frozenset(step_machine.STEPS)",
        "_PROGRESS_STAGES = frozenset("
        "{step_machine.STEP_STAGE1, step_machine.STEP_STAGE2})",
        ("tests/test_step_machine.py::test_progress_stages_derive_from_the_step_labels",),
        "进度取值另抄一份＝新加的 segmenting 只写到一半才在运行期炸（守卫形态要从常量派生）",
    ),
    Mutation(
        "segment_tail_anchor_dropped",
        "src/minimax_h3_prompt/api/jobs.py",
        # 锚点只圈一行：整块带缩进地圈会在复审重构缩进后「出现 0 次」而静默失效
        # （2026-11-13 实测，锁内合并把缩进从 16 格推到 20 格）。
        'anchor_state["bridge_frame_descriptions"] = anchors',
        'pass  # 变异：不写尾帧锚',
        ("tests/test_api_segment_loop.py"
         "::test_pass_records_segment_and_writes_next_with_tail_anchor",),
        "尾帧锚不写进 state＝第 N+1 段写词请求拿不到第 N 段尾帧，段间接不上而不报错"
        "（D6-AC4 静默失守，#246）",
    ),
    Mutation(
        "segment_assembly_never_starts",
        "src/minimax_h3_prompt/api/jobs.py",
        "        return (SEGMENT_STEP_ASSEMBLE, 0)",
        "        return (SEGMENT_STEP_DONE, 0)",
        ("tests/test_api_segment_assembly.py::"
         "test_last_segment_auto_assembles_final_film_and_completes",),
        "末段通过后不自动拼接＝成片永远不产出、会话停在 awaiting_human（D6-AC9 静默失守，#248）",
    ),
    Mutation(
        "segment_assembly_reason_generalised",
        "src/minimax_h3_prompt/api/jobs.py",
        "            raise SegmentAssemblyRefused(film.reason)",
        '            raise SegmentAssemblyRefused("拼接失败")',
        ("tests/test_api_segment_assembly.py::"
         "test_missing_segment_fails_with_named_segment_and_retry_after_fix",),
        "把 reason 改写成泛泛的「拼接失败」＝用户不知道补哪一段（D6-AC9 的点名要求，#248）",
    ),
    Mutation(
        "segment_resume_rewrites_validating",
        "src/minimax_h3_prompt/api/jobs.py",
        "            if status == SEGMENT_STATUS_VALIDATING:\n"
        "                return (SEGMENT_STEP_VALIDATE, number)\n",
        "            if status == SEGMENT_STATUS_VALIDATING:\n"
        "                return (SEGMENT_STEP_WRITE, number)\n",
        ("tests/test_api_segment_recovery.py"
         "::test_killed_validation_resumes_after_interrupted_reconcile",),
        "重启把「已交视频、待校验」的段当成没写过＝重试丢掉用户交的素材、要求重交"
        "（D6-AC11 断点续跑失守，#249）",
    ),
    Mutation(
        "segment_resume_skips_unwritten",
        "src/minimax_h3_prompt/api/jobs.py",
        "            if status == SEGMENT_STATUS_PENDING:\n"
        "                return (SEGMENT_STEP_WRITE, number)\n",
        "            if status == SEGMENT_STATUS_PENDING:\n"
        "                return (SEGMENT_STEP_DONE, number)\n",
        ("tests/test_api_segment_recovery.py"
         "::test_killed_segment_write_restarts_that_write_only",),
        "重启不给「未写词」的段重起写词 job＝重试空转收尾、会话卡在没有待办的半截"
        "（spec §10 恢复失守，#249）",
    ),
    Mutation(
        "fake_segment_frames_ignore_the_tier_table",
        "src/minimax_h3_prompt/api/fake_pipeline.py",
        "    return H3_TIER_FRAMES[tier]",
        "    return int(round(seconds * H3_VIDEO_FPS))",
        ("tests/test_api_fake_pipeline_segments.py"
         "::test_stub_segment_video_passes_the_real_gate",),
        "桩造件绕开实测帧数表＝6 秒档造出 144 帧，真实闸门只报「无法确证档位」而不报错，"
        "CI 会静默变假绿（D6-AC13，#250）",
    ),
    Mutation(
        "fake_segment_video_accepts_unknown_tiers",
        "src/minimax_h3_prompt/api/fake_pipeline.py",
        "    if tier not in H3_TIER_FRAMES or abs(seconds - tier) > 1e-6:\n",
        "    if False:\n",
        ("tests/test_api_fake_pipeline_segments.py"
         "::test_stub_segment_video_refuses_unknown_tiers",),
        "档外秒数也静默造件＝8 秒件只拿到 unknown tier warning，CI 误以为档位已验证"
        "（D6-AC13，#250）",
    ),
    Mutation(
        "fake_segment_plan_ignores_tiers",
        "src/minimax_h3_prompt/api/fake_pipeline.py",
        "    return best[total_s]",
        "    return None",
        ("tests/test_api_fake_pipeline_segments.py"
         "::test_stub_segment_plan_uses_only_h3_tier_lengths",),
        "桩规划退回按 10 秒上限等分＝18 秒拆成 2×9 秒，段长不再是 H3 可生成档位（#250）",
    ),
    Mutation(
        "end_hook_reply_negation_first",
        "src/minimax_h3_prompt/segment_validation.py",
        "    if any(marker in head for marker in _NO_CONFLICT_MARKERS):\n"
        "        return \"\"\n"
        "    if any(marker in head for marker in _CONFLICT_MARKERS):\n"
        "        return detail or lines[0]\n",
        "    if any(marker in head for marker in _CONFLICT_MARKERS):\n"
        "        return detail or lines[0]\n"
        "    if any(marker in head for marker in _NO_CONFLICT_MARKERS):\n"
        "        return \"\"\n",
        ("tests/test_segment_validation.py::test_parse_end_hook_reply",),
        "无冲突类后判＝「无冲突」里含「冲突」，一致的尾帧被读成冲突，"
        "每段都挂一条假提示（#270；ADR 0039 起语义检查只提示、不阻断）",
    ),
    Mutation(
        "end_hook_prompt_drops_support_surface",
        "src/minimax_h3_prompt/segment_validation.py",
        '        "只判断**关键事实**是否冲突：场景/地点（含人物所坐或所靠的支撑面，"\n'
        '        "如堤顶还是矮墙、桥面还是台阶）、主体是否在场、本段的大动作是否发生、"\n',
        '        "只判断**关键事实**是否冲突：场景/地点、主体是否在场、'
        '本段的大动作是否发生、"\n',
        ("tests/test_segment_validation.py"
         "::test_build_prompt_only_checks_key_facts_and_excludes_unobservable_details",),
        "「场景/地点」类丢掉「人物所坐或所靠的支撑面」＝坐在矮墙而计划是堤顶这类偏离"
        "重新看不见，判据退回 #360 之前的口径",
    ),
    Mutation(
        "end_hook_prompt_excludes_support_surface",
        "src/minimax_h3_prompt/segment_validation.py",
        '        "以下情况不算不符：人物自身的左右、具体是哪只手、视线朝向、'
        '描述里没有覆盖到的内容、"\n',
        '        "以下情况不算不符：人物自身的左右、具体是哪只手、视线朝向、'
        '人物所坐或所靠的支撑面、描述里没有覆盖到的内容、"\n',
        ("tests/test_segment_validation.py"
         "::test_build_prompt_only_checks_key_facts_and_excludes_unobservable_details",),
        "把「支撑面」塞进排除清单＝同一件事既要求核又说不算不符，判据自相矛盾且"
        "那条区分句被架空（#360 明确要求「不在排除清单内」）",
    ),
    Mutation(
        "event_log_latest_of_type_takes_the_first_match",
        "src/minimax_h3_prompt/workbench_store.py",
        "        return matches[-1] if matches else None\n",
        "        return matches[0] if matches else None\n",
        ("tests/test_event_log.py"
         "::test_latest_of_type_picks_the_last_match_at_or_before_the_cursor",),
        "补发「游标之前最近一条进度」取成最早一条＝刷新后节点停在旧节点（#271）",
    ),
    Mutation(
        "segment_out_legacy_key_ignored",
        "src/minimax_h3_prompt/workbench_store.py",
        '        if isinstance(data, dict) and "semantic_warning" not in data:\n'
        "            legacy = semantic_warning_of(data)\n"
        "            if legacy:\n"
        '                data = {**data, "semantic_warning": legacy}\n',
        '        if isinstance(data, dict) and "semantic_warning" not in data:\n'
        "            legacy = semantic_warning_of(data)\n"
        "            if False:\n"
        '                data = {**data, "semantic_warning": legacy}\n',
        ("tests/test_workbench_store.py"
         "::test_segment_out_reads_legacy_rejection_reason_as_semantic_warning",),
        "旧快照的 rejection_reason 读不出来＝已有会话的语义提示整段消失、且不报错（#270）",
    ),
    Mutation(
        "config_source_assertion_always_true",
        "scripts/accept_d6.py",
        "    problems: list[str] = []\n"
        "    actual_path = str(facts.get(\"config_path\") or \"\")\n",
        "    problems: list[str] = []\n"
        "    return problems\n"
        "    actual_path = str(facts.get(\"config_path\") or \"\")\n",
        ("tests/test_accept_d6.py::test_config_source_problems_names_every_mismatch",),
        "配置来源比对恒真＝拿着错的端点/模型/配置跑完整轮真实调用，整轮证据白费（#251）",
    ),
    Mutation(
        "batch_crlf_guard_off",
        "start_web.cmd",
        "setlocal\r\n",
        "setlocal\n",
        ("tests/test_script_line_endings.py::test_batch_files_have_crlf_line_endings",),
        "批处理落成裸 LF＝cmd.exe 把命令劈开执行，双击一闪就关（2026-11-13 实测）",
        normalize_newlines=False,
    ),
    Mutation(
        "replay_drops_segment_index",
        "src/minimax_h3_prompt/workbench_store.py",
        '_ACTION_ITEM_INDEX_KEYS: tuple[str, ...] = ("segment_index", "asset_index")',
        '_ACTION_ITEM_INDEX_KEYS: tuple[str, ...] = ("asset_index",)',
        ("tests/test_api_events.py::test_action_item_payload_survives_snapshot_replay",),
        "重放不还原段号＝崩溃补齐后待办又说不出「在等第几段」，"
        "段循环的恢复只能靠人去翻产物名（#254）",
    ),
    Mutation(
        "ref_capacity_gate_off",
        "src/minimax_h3_prompt/workbench_store.py",
        "        check_reference_capacity(items, where=where)",
        "        return",
        ("tests/test_api_ref_assets.py::"
         "test_tenth_picture_is_refused_and_names_the_picture_bucket",),
        "同步容量闸失效＝第 10 张图在上传时就放行，直到真跑起来在文本编码器上"
        "OOM（2026-10-22 实测 232 GiB）才炸（#255）",
    ),
    Mutation(
        "ref_asset_delete_lock_off",
        "src/minimax_h3_prompt/workbench_store.py",
        '        if route_uses_asset_ledger(session.brief) and session.status != "created":',
        "        if False and route_uses_asset_ledger(session.brief) and session.status != \"created\":",
        ("tests/test_api_ref_assets.py::test_delete_after_start_is_locked",),
        "撤销锁失效＝素材循环开始后还能删台账行，已写好的资产图词与图脱钩，"
        "派生清单也少一件（#255 / #282）",
    ),
    Mutation(
        "asset_loop_stalls_on_an_abandoned_item",
        "src/minimax_h3_prompt/workbench_store.py",
        '             and str(entry.get("status")) not in (\n'
        "                 ASSET_LEDGER_STATUS_PASSED, ASSET_LEDGER_STATUS_ABANDONED))),",
        '             and str(entry.get("status")) != ASSET_LEDGER_STATUS_PASSED)),',
        ("tests/test_api_asset_loop.py::"
         "test_abandoning_an_item_skips_it_and_lets_the_next_one_proceed",),
        "放弃行仍算「当前件」＝放弃之后进度永远停在那件上，放弃键成了第二种死锁"
        "（#282，spec #279「跳过 abandoned 行」）",
    ),
    Mutation(
        "asset_position_lock_off",
        "src/minimax_h3_prompt/workbench_store.py",
        "        if not route_uses_asset_ledger(session.brief) or session.status == \"created\":\n"
        "            return",
        "        if True:\n"
        "            return",
        ("tests/test_api_ref_assets.py::"
         "test_registration_after_start_refuses_named_pictures",),
        "位置锁整体失效＝起跑后带语义名的图能绕过待办卡直接登记，台账上出现没人校过的正本"
        "（#279 复审，spec §12「ref 的上传口只剩视频/音频补交」）",
    ),
    Mutation(
        "asset_extra_gate_not_blocking",
        "src/minimax_h3_prompt/api/jobs.py",
        "            if (session.brief.is_ref\n"
        "                    and not asset_extras_settled(session.action_items)):",
        "            if False:",
        ("tests/test_api_asset_extra.py::"
         "test_restart_keeps_the_gate_and_rehangs_it_after_a_crash_like_snapshot",),
        "补交口不拦＝循环走完（含零资产）直接进阶段 2，视频/音频补交这一步被静默跳过"
        "（#284，spec #279「它是阻塞的：不解掉就不进阶段 2」）",
    ),
    Mutation(
        "asset_extra_step_reused_as_loop",
        "src/minimax_h3_prompt/api/jobs.py",
        "                return step_machine.STEP_ASSET_EXTRA",
        "                return step_machine.STEP_ASSET_LOOP",
        ("tests/test_api_asset_extra.py::"
         "test_the_extras_gate_is_its_own_step_between_loop_and_stage2",),
        "补交档复用素材循环＝进度条在补交口仍写「素材循环」，用户看不出循环已走完"
        "（#279 §步骤机）",
    ),
    Mutation(
        "asset_extra_capacity_not_recomputed",
        "src/minimax_h3_prompt/api/jobs.py",
        '                    self.store.ensure_ref_capacity(usable, where="补交素材")',
        "                    pass",
        ("tests/test_api_asset_extra.py::"
         "test_capacity_is_recomputed_when_the_gate_settles",),
        "落定不再现算容量＝注入卡与段内子集用的快照合法性只剩上传时那一半保证"
        "（#284，spec #279「补交口落定之后，容量闸门按当时台账现算」）",
    ),
    Mutation(
        "asset_loop_settles_a_rejected_item",
        "src/minimax_h3_prompt/api/jobs.py",
        "            row[\"status\"] = (ASSET_LEDGER_STATUS_PASSED if passed\n"
        "                             else ASSET_LEDGER_STATUS_PENDING)",
        "            row[\"status\"] = ASSET_LEDGER_STATUS_PASSED",
        ("tests/test_api_asset_loop.py::"
         "test_proofread_failure_reopens_the_same_item_and_leaves_the_prompt_untouched",),
        "不通过也落定＝校对结论只是建议，下一件会照样推进，差异清单永远无人处理（#256）",
    ),
    Mutation(
        "asset_loop_submit_not_marked_registered",
        "src/minimax_h3_prompt/api/jobs.py",
        '                        current_entry["status"] = ASSET_LEDGER_STATUS_REGISTERED',
        "                        pass",
        ("tests/test_api_asset_loop.py::"
         "test_submitting_an_image_marks_the_row_registered_until_proofread",),
        "交图不置 registered＝「交回了、还没校对」那一段在台账上看不见，"
        "与「还没交」混成同一个状态（#279 复审，spec「台账与进度」）",
    ),
    Mutation(
        "asset_capacity_counts_abandoned_rows",
        "src/minimax_h3_prompt/workbench_store.py",
        "        for entry in entries\n"
        "        if str(entry.get(\"status\")) != ASSET_LEDGER_STATUS_ABANDONED\n"
        "    ]",
        "        for entry in entries\n"
        "    ]",
        ("tests/test_api_ref_assets.py::"
         "test_abandoned_rows_do_not_count_against_ref_capacity",),
        "放弃件算进容量＝界面显示 7/9 全绿、上传却 422，同一份台账在两处读出两个数字"
        "（#279 复审）",
    ),
    Mutation(
        "asset_manifest_derived_only_once",
        "src/minimax_h3_prompt/api/jobs.py",
        "        derive = session.brief.is_ref",
        "        derive = (not session.asset_manifest_derived) and session.brief.is_ref",
        ("tests/test_api_ref_assets.py::"
         "test_revise_reruns_stage1_and_the_ledger_follows_the_new_manifest",),
        "修订重跑不重派生＝台账停在旧清单上，对账硬失败失效，退化成出词时"
        "「阶段 1 没有为「旧名」写提示词」（#279 复审）",
    ),
    Mutation(
        "asset_loop_reopens_the_wrong_number",
        "src/minimax_h3_prompt/api/jobs.py",
        "                status=\"pending\", created_at=_now(), asset_index=number,\n"
        "                reason=reason,\n",
        "                status=\"pending\", created_at=_now(), asset_index=number + 1,\n"
        "                reason=reason,\n",
        ("tests/test_api_asset_loop.py::"
         "test_proofread_failure_reopens_the_same_item_and_leaves_the_prompt_untouched",),
        "重开的件号错位＝用户被要求交下一件的图，而第 N 件永远定不了稿（#256）",
    ),
    Mutation(
        "proofread_vision_model_not_replayed",
        "src/minimax_h3_prompt/workbench_store.py",
        "                    for key in _ACTION_ITEM_PROOFREAD_KEYS:\n"
        "                        if isinstance(payload.get(key), str):\n"
        "                            item[key] = payload[key]",
        "                    for key in ():\n"
        "                        if isinstance(payload.get(key), str):\n"
        "                            item[key] = payload[key]",
        ("tests/test_api_asset_loop.py::"
         "test_the_proofread_report_survives_snapshot_replay",),
        "重放不补读图模型名＝刷新一次，校对结论那一行的模型就退回「未知」（#292）",
    ),
    Mutation(
        "ref_brief_not_wired",
        "src/minimax_h3_prompt/api/jobs.py",
        "        if session.brief.is_ref:\n"
        "            return session.brief\n"
        "        # r2v 的阶段 1 还没跑素材循环",
        "        if False:\n"
        "            return session.brief\n"
        "        # r2v 的阶段 1 还没跑素材循环",
        ("tests/test_api_asset_loop.py::"
         "test_ref_stage1_never_sees_the_ledger_as_brief_refs",),
        "阶段 1 把台账注入 refs＝``ref_asset_registry`` 走「用户登记为准」那支、"
        "设计师清单被绕过，素材集合回到用户预置（#279，spec §台账与进度）",
    ),
    Mutation(
        "film_injection_card_never_written",
        "src/minimax_h3_prompt/api/jobs.py",
        "        self._write_film_injection_card(session_id, job_id)\n"
        "        if self.fake_pipeline:",
        "        if self.fake_pipeline:",
        ("tests/test_api_injection_card.py::"
         "test_film_card_is_written_between_the_asset_loop_and_stage2",),
        "整片卡漏产出＝素材循环结束到阶段 2 之间少一件产物，用户进 ComfyUI 时"
        "没有接线清单且不报错（#257，spec §9）",
    ),
    Mutation(
        "film_injection_card_paths_not_anchored",
        "src/minimax_h3_prompt/injection_card.py",
        "resolve_asset_path(ledger, entry, session_dir=session_dir)",
        "resolve_asset_path(ledger, entry)",
        ("tests/test_api_injection_card.py::"
         "test_film_card_is_written_between_the_asset_loop_and_stage2",),
        "卡上只写相对路径＝工作台会话的素材在会话目录里，用户照卡找不到正本（#257）",
    ),
    Mutation(
        "segment_injection_card_pattern_off",
        "src/minimax_h3_prompt/workbench_store.py",
        '_SEGMENT_INJECTION_CARD_FILENAME = r"injection-card-\\d{2}\\.md"',
        '_SEGMENT_INJECTION_CARD_FILENAME = r"injection-card-\\d{3}\\.md"',
        ("tests/test_api_injection_card.py::"
         "test_segment_card_writer_persists_and_registers_the_exact_text",),
        "段级卡模式失效＝写入口落盘后产物清单看不到、content 端点也读不出（#257）",
    ),
    Mutation(
        "ref_planning_falls_back_to_base",
        "src/minimax_h3_prompt/api/jobs.py",
        "        if session.brief.is_ref:\n"
        "            plans = self._plan_ref_segments(session_id, job_id, directory, session)",
        "        if False:\n"
        "            plans = self._plan_ref_segments(session_id, job_id, directory, session)",
        ("tests/test_api_segment_ref_loop.py::"
         "test_ref_planning_uses_ref_route_and_writes_segment_product",),
        "路线分叉恒假＝ref 按 base 规划并写 shot-NN.md，段内子集、顺序卡与分镜表整条"
        "静默消失（#258，spec §10）",
    ),
    Mutation(
        "reopen_clears_the_asset_subset",
        "src/minimax_h3_prompt/api/jobs.py",
        "                    target[\"status\"] = SEGMENT_STATUS_WAITING_VIDEO\n"
        "                    target[\"video_asset_id\"] = \"\"",
        "                    target[\"status\"] = SEGMENT_STATUS_WAITING_VIDEO\n"
        "                    target[\"video_asset_id\"] = \"\"\n"
        "                    target[\"assets\"] = []",
        ("tests/test_api_segment_ref_loop.py::test_ref_passed_segment_can_be_reopened",),
        "换一段顺手清子集＝用户只想换视频，段内参考素材被悄悄推倒重算（#258，"
        "spec §5.5「只换视频」）",
    ),
    Mutation(
        "ref_validation_falls_back_to_frame_chain",
        "src/minimax_h3_prompt/api/jobs.py",
        "        if session.brief.is_ref:\n"
        "            # ref 没有桥接帧、也没有「尾帧对照末态」这一道（ADR 0011）：段尾语义在",
        "        if False:\n"
        "            # ref 没有桥接帧、也没有「尾帧对照末态」这一道（ADR 0011）：段尾语义在",
        ("tests/test_api_segment_ref_loop.py::"
         "test_ref_segment_validation_skips_bridge_frame_and_vision",),
        "ref 校验退回帧链＝多参段开始剥尾帧读图判 end_hook，ADR 0011 明令不做的"
        "那一道又接回来（#258）",
    ),
    Mutation(
        "fake_asset_proofread_never_rejects",
        "src/minimax_h3_prompt/api/fake_pipeline.py",
        "    if number != first_number or round_no > 0:\n        return True, \"\"",
        "    if True:\n        return True, \"\"",
        ("tests/test_api_fake_pipeline_ref.py::"
         "test_fake_ref_closed_loop_rejects_then_settles",),
        "桩校对永远通过＝CI 里「不通过重开」那条路从未被走到，票面要求的第二条分支"
        "静默缺席（#259）",
    ),
    Mutation(
        "mutation_anchor_drift_detected",
        "src/minimax_h3_prompt/workbench_store.py",
        '_ACTION_ITEM_INDEX_KEYS: tuple[str, ...] = ("segment_index", "asset_index")',
        '_ACTION_ITEM_INDEX_KEYS: tuple[str, ...] = ("segment_index",)',
        ("tests/test_mutation_anchors.py::test_every_mutation_anchor_is_unique",),
        "锚点随重构漂走＝这条变异静默变成摆设，只有在 full 里被报成「存活」；"
        "快档必须当场红（retro 2026-11-13）",
    ),
    Mutation(
        "mutation_test_node_drift_detected",
        "tests/test_api_asset_loop.py",
        "def test_proofread_failure_reopens_the_same_item_and_leaves_the_prompt_untouched(",
        "def test_proofread_failure_reopens_the_same_item_renamed(",
        ("tests/test_mutation_anchors.py::test_every_referenced_test_node_exists",),
        "用例改名而锚点没跟＝pytest 以「收集失败」收场、脚本却记「杀死」，"
        "守卫早没了、报表仍全绿（retro 2026-11-15，#287 实测）",
    ),
    Mutation(
        "decision_whitelist_inlined_again",
        "src/minimax_h3_prompt/api/jobs.py",
        "                    elif decision in step_machine.EXPLICIT_ASSET_DECISIONS:",
        "                    elif decision in (\n"
        "                            step_machine.DECISION_REWORD_REROLL,\n"
        "                            step_machine.DECISION_REROLL,\n"
        "                            step_machine.DECISION_ACCEPT_IMAGE_FACTS,\n"
        "                            step_machine.DECISION_KEEP_DESCRIPTION,\n"
        "                            step_machine.DECISION_RESTORE_PROMPT):",
        ("tests/test_decision_whitelists.py::"
         "test_decision_whitelists_are_named_constants",),
        "决议白名单写回表达式＝同一张表出现第二份副本，改一处漏一处不会报错"
        "（retro 2026-11-15；#286 复审的事件/快照两处口径）",
    ),
    Mutation(
        "ref_concat_trims_first_frame",
        "src/minimax_h3_prompt/api/jobs.py",
        "        if session.brief.is_ref:\n"
        "            strategy = ConcatStrategy.STREAM_COPY_FIRST\n"
        "        else:\n"
        "            strategy = ConcatStrategy.TRIM_FIRST_FRAME",
        "        strategy = ConcatStrategy.TRIM_FIRST_FRAME",
        ("tests/test_api_fake_pipeline_ref.py::"
         "test_fake_ref_closed_loop_rejects_then_settles",),
        "ref 套用首尾帧链的裁首帧＝用户交回的帧被吃掉一帧，多参成片比段之和短"
        "（#259，D7 §10 复用拼接件但路线口径不同）",
    ),
    Mutation(
        "director_frame_gate_reads_variant",
        "src/minimax_h3_prompt/step_machine.py",
        "    if is_director_mode(brief.mode):\n"
        "        return director_frame_variant(brief) in FRAME_VARIANTS\n"
        "    return needs_frame_submission(brief.variant)",
        "    if False:\n"
        "        return director_frame_variant(brief) in FRAME_VARIANTS\n"
        "    return needs_frame_submission(brief.variant)",
        ("tests/test_step_machine.py::"
         "test_frame_submission_predicate_reads_the_director_frame_variant",),
        "帧待办判据退回读 brief.variant（导演台恒 T2VA 占位）＝ i2v/fl2v 静默不出"
        "开场首帧待办，用户导入导演包才发现段里没图（#261，D8 §6.4）",
    ),
    Mutation(
        "reopen_cache_root_check_off",
        "src/minimax_h3_prompt/api/jobs.py",
        "        Path(directory).resolve().relative_to(Path(root).resolve())",
        "        Path(directory).resolve()",
        ("tests/test_api_director_segments.py::"
         "test_cache_directory_outside_the_configured_root_is_rejected_by_itself",
         "tests/test_api_director_segments.py::"
         "test_reopen_refuses_a_cache_directory_linked_outside_the_root"),
        "缓存目录级判据失效＝目录被链接到根外时逐文件校验的基准也跟着跑出去，"
        "级联删的是别人的文件（#265，D8 §11）",
    ),
    Mutation(
        "reopen_cache_file_check_off",
        "src/minimax_h3_prompt/director/cache.py",
        "            Path(path).resolve().relative_to(base)",
        "            Path(path)",
        ("tests/test_api_director_segments.py::"
         "test_reopen_refuses_a_cache_file_symlinked_outside_the_root",),
        "逐文件越界校验失效＝符号链接指向的段文件被当成缓存删掉，"
        "「一个文件都不删」的语义失守（#265，D8 §11）",
    ),
    Mutation(
        "reopen_keeps_downstream_todos",
        "src/minimax_h3_prompt/api/jobs.py",
        "                    return (existing.segment_index is not None\n"
        "                            and int(existing.segment_index) >= int(index))",
        "                    return False",
        ("tests/test_api_director_segments.py::"
         "test_reopen_drops_pending_todos_of_later_segments",),
        "换段不清下游待办＝界面同时挂着第 1、2 段的表单，第 2 段的待办是旧锚点算的"
        "（#265，D8 §11「每段恰好一个待办」）",
    ),
    Mutation(
        "delivery_skips_final_film_verdict",
        "src/minimax_h3_prompt/api/jobs.py",
        "        if not ok:\n"
        "            raise FinalFilmMismatch(",
        "        if False:\n"
        "            raise FinalFilmMismatch(",
        ("tests/test_api_director_delivery.py::"
         "test_final_film_mismatch_uses_the_shared_verdict",),
        "交付不调 final_film_verdict＝画幅不符的片子被归档成「已交付」，"
        "`deliver` 自己根本不查宽高（#266，spec §12.3）",
    ),
    Mutation(
        "stub_director_plan_falls_back_to_single_segment",
        "src/minimax_h3_prompt/api/fake_pipeline.py",
        "    if mode == \"fl2v\" and float(total_s) > 10:",
        "    if mode == \"fl2v\" and float(total_s) > 100:",
        ("tests/test_api_director_delivery.py::"
         "test_default_stub_plan_reaches_the_delivery_todo",),
        "桩计划退回单段＝E2E 再走不到切镜段图槽与两种按需词，却仍然全绿"
        "（#266，spec §19「桩管线覆盖到导演台」）",
    ),
    Mutation(
        "frontend_freshness_never_rebuilds",
        "scripts/start_web.py",
        "    if newest is None or newest > dist_index.stat().st_mtime:\n"
        "        return BUILD_REASON_SOURCE_NEWER\n",
        "    if False:\n        return BUILD_REASON_SOURCE_NEWER\n",
        ("tests/test_start_web_freshness.py::"
         "test_source_newer_than_the_build_triggers_a_rebuild",),
        "新鲜度判据恒不重建＝源码改了照样复用旧界面，对着已不存在的 bug 排查"
        "（#275 / #274 §A 的现场）",
    ),
    Mutation(
        "proofread_omissions_block_settling",
        "src/minimax_h3_prompt/asset_proofread.py",
        "        return not self.deviations",
        "        return not self.deviations and not self.omissions",
        ("tests/test_asset_proofread.py::"
         "test_omissions_alone_do_not_make_the_report_dirty",
         "tests/test_api_asset_loop.py::"
         "test_omissions_without_deviations_settle_the_item_and_stay_visible"),
        "补漏重新参与通过判定＝细节密集的身份板永远定不了稿，素材循环在结构上"
        "死锁（#286 事故 2 / #287）",
    ),
    Mutation(
        "reword_locked_feature_guard_off",
        "src/minimax_h3_prompt/asset_loop.py",
        "    if missing:\n"
        "        raise LockedFeaturesLost(",
        "    if False:\n"
        "        raise LockedFeaturesLost(",
        ("tests/test_api_asset_loop.py::"
         "test_reword_that_drops_a_locked_feature_is_not_written",),
        "撤掉加法守卫＝删掉锁死条目的改写照样落盘，判偏差的锚点当场失效，"
        "而卡片上不会有任何待裁项（#288）",
    ),
    Mutation(
        "reword_round_not_logged",
        "src/minimax_h3_prompt/api/jobs.py",
        "        log_asset_round(\n"
        "            directory, name=name, kind=variant.kind,\n"
        "            trigger=TRIGGER_REGEN_AI_REWORD,\n"
        "            feedback=_reword_feedback(deviations),\n"
        "            prompt_before=current, prompt_after=new_prompt)",
        "        pass  # 变异：改完不记账",
        ("tests/test_api_asset_loop.py::"
         "test_reword_reroll_runs_as_a_background_job_and_only_adds",),
        "改词不落轮次账＝「这条词什么时候被谁改过」永久无据可查，恢复动作也"
        "分不清合法改写与坏词（#288 / spec #286 US21）",
    ),
    Mutation(
        "proofread_unavailable_hangs_instead_of_settling",
        "src/minimax_h3_prompt/api/jobs.py",
        '                    "passed": True, "reason": _unproofread_reason(exc),\n'
        '                    "vision_model": vision_model}',
        '                    "passed": False, "reason": _unproofread_reason(exc),\n'
        '                    "vision_model": vision_model}',
        ("tests/test_api_asset_loop.py::"
         "test_a_failed_reading_call_settles_the_item_and_names_the_step",
         "tests/test_api_asset_loop.py::"
         "test_an_unparsable_reading_settles_the_item_and_names_the_step",
         "tests/test_api_asset_loop.py::"
         "test_an_unavailable_vision_client_settles_the_item_and_names_the_step"),
        "读图不可用改回挂卡＝一次服务波动就把这一件打回重交，用户白等一轮且"
        "「没交图」与「没校对」混成同一个状态（#290，spec #286「读图不可用」）",
    ),
    Mutation(
        "facts_merged_mid_flight",
        "src/minimax_h3_prompt/api/jobs.py",
        "_SETTLING_ASSET_DECISIONS = frozenset({\n"
        "    step_machine.DECISION_ACCEPT_IMAGE_FACTS,\n"
        "    step_machine.DECISION_KEEP_DESCRIPTION,\n"
        "})",
        "_SETTLING_ASSET_DECISIONS = frozenset({\n"
        "    step_machine.DECISION_ACCEPT_IMAGE_FACTS,\n"
        "    step_machine.DECISION_KEEP_DESCRIPTION,\n"
        "    step_machine.DECISION_REROLL,\n"
        "})",
        ("tests/test_api_asset_loop.py::test_rerolling_does_not_merge_the_facts",),
        "重掷中途也并入补漏＝还没定下来的图把细节写进描述，下一轮按新图重交时"
        "描述里留着上一张图的事实（#289，spec #286「并入发生在定稿那一刻」）",
    ),
    Mutation(
        "facts_not_deduped",
        "src/minimax_h3_prompt/api/jobs.py",
        "        if not fact or fact in merged or fact in facts:\n"
        "            continue",
        "        if not fact:\n"
        "            continue",
        ("tests/test_api_asset_loop.py::"
         "test_a_fact_observed_again_is_not_merged_twice",),
        "补漏不去重＝同一件反复校对把同一句话并进描述十遍（#289，"
        "spec #286「去重判据：观察文本已逐字出现则跳过」）",
    ),
    Mutation(
        "proofread_conclusion_shape_unchecked",
        "src/minimax_h3_prompt/workbench_store.py",
        "    if value is None:\n"
        "        return\n"
        "    if not isinstance(value, dict):",
        "    if True:\n"
        "        return\n"
        "    if not isinstance(value, dict):",
        ("tests/test_event_log.py::test_proofread_conclusion_must_be_well_shaped",),
        "校对结论形状不校验＝半截结论照旧进事件流，重放后改词作业读到空串/非列表"
        "就静默改错方向（#288，spec #286「结构化偏差清单」）",
    ),
    Mutation(
        "decision_whitelist_drops_settling_exits",
        "src/minimax_h3_prompt/api/jobs.py",
        "                    if decision in step_machine.EXPLICIT_ASSET_DECISIONS",
        "                    if decision in (step_machine.DECISION_REWORD_REROLL,\n"
        "                                    step_machine.DECISION_REROLL,\n"
        "                                    step_machine.DECISION_RESTORE_PROMPT)",
        ("tests/test_api_asset_loop.py::"
         "test_settling_exits_record_their_real_decision_in_events_and_replay",),
        "事件侧白名单漏掉两条定稿出路＝真实决议被记成 submit_image，"
        "快照与事件流/重放从那一刻分家（#286 复审 A 的原样复现）",
    ),
    Mutation(
        "asset_receipt_takes_any_kind",
        "frontend/src/features/sessions/AssetsTab.vue",
        "    if (item.kind !== 'asset_image' || item.status !== 'resolved') {\n"
        "      continue\n"
        "    }",
        "    if (item.status !== 'resolved') {\n"
        "      continue\n"
        "    }",
        ("src/features/sessions/AssetsTab.spec.ts"
         "::没有已决议回执（只有待决卡或空 reason）时不显示",),
        "回执不认待办种类＝补交口的「交完了」会被当成素材卡回执印在素材分区，"
        "而那张卡说的是另一件事（#286 复审 F）",
        runner="vitest",
    ),
    Mutation(
        "asset_receipt_takes_the_first_card",
        "frontend/src/features/sessions/AssetsTab.vue",
        "    if (latest === null || (item.resolved_at ?? '') >= (latest.resolved_at ?? '')) {",
        "    if (latest === null) {",
        ("src/features/sessions/AssetsTab.spec.ts"
         "::换件之后回执跟着最新一张走，不把上一件的说明串到这一件上",),
        "回执停在第一张已决议卡上＝换件后显示上一件的结果，用户以为这一件也是那样定的"
        "（#286 复审 F：按 resolved_at 取最新一张）",
        runner="vitest",
    ),
    Mutation(
        "restore_ignores_the_round_log",
        "src/minimax_h3_prompt/workbench_store.py",
        "        if read_asset_rounds(directory, name=name):\n"
        '            return "", ""',
        "        if False:\n"
        '            return "", ""',
        ("tests/test_api_asset_loop.py::"
         "test_restore_action_hides_when_the_round_log_has_a_record",),
        "恢复不看资产轮次账＝用户认可的合法改写会被一键抹成阶段 1 初稿，"
        "而卡片上没有任何提示（#291，spec #286 US23「无账差异」判据）",
    ),
    Mutation(
        "director_common_section_dropped_from_instruction",
        "src/minimax_h3_prompt/director/prompts.py",
        "    return _apply_writing_discipline_placeholders(\n"
        "        f\"{sections['common']}\\n\\n{sections[mode]}\")",
        "    return _apply_writing_discipline_placeholders(\n"
        "        f\"{sections[mode]}\")",
        ("tests/test_director_prompts.py::"
         "test_action_motivation_rule_reaches_first_write_and_rewrite_in_every_mode",),
        "加载口只取模式小节、丢掉 common＝动作动机（连同时间窗、门槛、道具归属）到不了"
        "模型眼前，「规则在、接线不在」（#303）",
    ),
    Mutation(
        "director_dump_segment_filter_ignored",
        "scripts/director_request_dump.py",
        "        if segment is not None and item.index != segment:\n"
        "            continue",
        "        if False:\n"
        "            continue",
        ("tests/test_director_request_dump.py::"
         "test_segment_filter_exports_only_the_requested_segment",),
        "段过滤失效＝`--segment N` 照样导出全部段，改前/改后对照的根本不是同一段"
        "（#306 AC4）",
    ),
    Mutation(
        "director_dump_opening_still_sidecar_miss_silently_skipped",
        "scripts/director_request_dump.py",
        "    if cached is None:\n"
        "        raise DumpError(\n"
        '            f"{image_label}在盘上（{image}），但读图描述 sidecar 缺失或已过期："',
        "    if cached is None:\n"
        '        return ""\n'
        "    if False:\n"
        "        raise DumpError(\n"
        '            f"{image_label}在盘上（{image}），但读图描述 sidecar 缺失或已过期："',
        ("tests/test_director_request_dump.py::"
         "test_missing_or_stale_opening_sidecar_fails_with_the_image_path",),
        "有图但 sidecar 缺失/过期时静默少一块＝改前/改后对照少一块而没人发现，"
        "正是 #325 存在的理由（票面 AC2）",
    ),
    Mutation(
        "director_dump_opening_still_ignores_the_giving_contract",
        "scripts/director_request_dump.py",
        "    start_frame_image = (start_still(session_dir, 1)\n"
        "                         if needs_first_frame_image(FIRST_SEGMENT_TYPE, plan.mode)\n"
        "                         else None)",
        "    start_frame_image = start_still(session_dir, 1)",
        ("tests/test_director_request_dump.py::"
         "test_modes_without_a_first_frame_slot_ignore_a_stray_image",),
        "判据不看给图契约＝t2v/r2v 盘上残留一张 start 图就让离线导出为不存在的首帧槽"
        "要 sidecar（#325：判据问给图契约，不写模式字面量）",
    ),
    Mutation(
        "opening_still_injection_criterion_dropped",
        "src/minimax_h3_prompt/director/prompts.py",
        "    if not needs_first_frame_image(segment.type, plan.mode):\n"
        "        return \"\"",
        "    if False:\n"
        "        return \"\"",
        ("tests/test_director_opening_still_request.py::"
         "test_injection_matrix_covers_four_modes_by_five_segment_types",),
        "注入判据改宽＝t2v（一个图槽都没有）与 r2v（画面来源是参考素材）的开场段也"
        "拿到「本段起点画面」，模型会照一张不存在的开场首帧图写第一拍（#326）",
    ),
    Mutation(
        "opening_still_block_not_registered",
        "src/minimax_h3_prompt/director/prompts.py",
        "    _request_start_still_block,\n",
        "",
        ("tests/test_opening_still_wiring.py::"
         "test_cli_entry_sends_the_opening_block_only_on_the_opening_segment",),
        "块 provider 没排进请求清单＝组装函数还在、矩阵用例照绿（它直接调 "
        "provider），但模型眼前的请求里永远没有这块——本仓咬过六次的「规则在、接线不在」（#326）",
    ),
    Mutation(
        "director_report_renders_only_six_conclusions",
        "src/minimax_h3_prompt/director/pack.py",
        "                 for number, constraint in enumerate(ASSEMBLY_CONSTRAINTS, 1))",
        "                 for number, constraint in enumerate(ASSEMBLY_CONSTRAINTS[:6], 1))",
        ("tests/test_director_pack.py::"
         "test_report_lists_both_totals_constraints_and_the_handoff_line",),
        "报告只渲染注册表前六项＝两条看不见的校验又回来了（#308 修的正是这个形态），"
        "用户会拿到「6 条全绿但打包被拒」",
    ),
    Mutation(
        "director_conclusion_stops_recomputing_canvas",
        "src/minimax_h3_prompt/director/pack.py",
        '            f"{_pass_fail(_canvas_is_aligned(width) and _canvas_is_aligned(height))}")',
        '            f"{_pass_fail(True)}")',
        ("tests/test_director_pack.py::"
         "test_every_conclusion_recomputes_its_own_criterion",),
        "结论不再独立重算判据（写死通过）＝校验器被架空时报告仍全绿，正是 #308 要防"
        "的形态：报告是用户判断「能不能导入」的唯一依据",
    ),
    Mutation(
        "director_api_start_frame_fact_not_passed",
        "src/minimax_h3_prompt/api/jobs.py",
        "                start_frame=facts.start_frame,\n"
        "                start_frame_description=facts.start_frame_description,\n"
        "                budget=result.budget,",
        "                start_frame=False,\n"
        "                start_frame_description=facts.start_frame_description,\n"
        "                budget=result.budget,",
        ("tests/test_api_director_opening_still.py::"
         "test_planner_and_first_write_receive_the_opening_still_fact",),
        "工作台当轮规划后的写段上下文丢掉「盘上真有首帧图」＝请求里的起点画面块永远"
        "不出现，而组装层矩阵用例照绿——本仓咬过六次的「规则在、接线不在」（#324／#326）",
    ),
    Mutation(
        "wizard_start_frame_fact_not_passed",
        "src/minimax_h3_prompt/ui/wizard.py",
        "                # （同一个缓存口取回），首写与规划不会各说一套（#322 的注入判据在\n"
        "                # 请求组装里按段类型与给图契约再守一道）。\n"
        "                start_frame=facts.start_frame,",
        "                # （同一个缓存口取回），首写与规划不会各说一套（#322 的注入判据在\n"
        "                # 请求组装里按段类型与给图契约再守一道）。\n"
        "                start_frame=False,",
        ("tests/test_wizard_opening_still.py::"
         "test_wizard_flow_reads_the_opening_still_before_planning_on_resume",),
        "CLI 首写调用点丢掉「盘上真有首帧图」＝续跑形态下开场段的起点画面块静默消失，"
        "规划期看到图、写段期不看图，两条口径当场分家（#323／#326）",
    ),
    Mutation(
        "unreachable_return_after_return",
        "src/minimax_h3_prompt/brief_parser.py",
        "    if is_director_mode(brief.mode):\n"
        "        return brief.director_mode == \"r2v\"\n"
        "    return is_ref_mode(brief.mode)",
        "    if is_director_mode(brief.mode):\n"
        "        return brief.director_mode == \"r2v\"\n"
        "    return is_ref_mode(brief.mode)\n"
        "    return is_ref_mode(brief.mode)",
        ("tests/test_unreachable_code.py::"
         "test_src_has_no_statements_after_return_or_raise",),
        "把不可达的重复 return 加回去＝#333 的结构守卫应当当场红；ruff 的可达性规则"
        "不报这种形态（实测 All checks passed），这条用例是唯一证据",
    ),
    Mutation(
        "function_reimports_module_level_symbol",
        "src/minimax_h3_prompt/ui/wizard.py",
        "    # 延迟导入（入口期不拉起生成栈的其余符号）；``canvas_display`` 模块顶层已导入，\n"
        "    # 函数内再导一次没有延迟效果（#333）。\n"
        "    from ..generation import (\n"
        "        CANVAS_RATIO_PRESETS,\n"
        "        canvas_family,",
        "    # 延迟导入（入口期不拉起生成栈的其余符号）；``canvas_display`` 模块顶层已导入，\n"
        "    # 函数内再导一次没有延迟效果（#333）。\n"
        "    from ..generation import (\n"
        "        CANVAS_RATIO_PRESETS,\n"
        "        canvas_display,\n"
        "        canvas_family,",
        ("tests/test_duplicate_function_imports.py::"
         "test_no_function_reimports_a_module_level_symbol",),
        "把与模块顶层重复的导入加回函数内＝#333 的重复导入守卫应当当场红；"
        "ruff 的 F811/F401 对这种形态全绿（实测 All checks passed），别无其它抓手",
    ),
    Mutation(
        "quality_claim_rule_drops_resolution",
        "CODING_STANDARDS.md",
        "**画质／听感结论必须同时写明 steps 档位与分辨率**",
        "**画质／听感结论必须同时写明 steps 档位**",
        ("tests/test_quality_claim_steps_guard.py::"
         "test_quality_claims_rule_requires_steps_and_resolution",),
        "规则只剩采样档位、丢了分辨率＝两次画质结论仍不可比（同一档步数配不同画布），"
        "而没有任何检查会报（#336）",
    ),
    Mutation(
        "clarifier_orientation_dimension_dropped",
        # 锚点取整个维度小句（含「人物朝向与空间站位」起头到收尾括号）：提示词是
        # 一整行七项清单，只删半句「左／右／前／后」不够——「朝向」「站位」两词还在，
        # 模型仍被告知要问一遍，出站断言照绿。必须把小句整段摘掉，四个锚词一起消失，
        # 守卫才有「被删掉该维度」的真实形态可咬。
        "src/minimax_h3_prompt/prompts/clarifier.md",
        "人物朝向与空间站位（主要人物与关键环境物——窗、门、桌、床——各自在画面的"
        "左／右／前／后，人物面朝哪个方向、身体朝向哪一面）。",
        "。",
        ("tests/test_clarification.py::"
         "test_outbound_request_asks_for_orientation_and_spatial_placement",),
        "删掉朝向与空间站位维度＝模型不再被告知问人物面朝哪边、窗门桌床在左还是右，"
        "C1 事故的编造形态（窗被编到身后）从提示词一侧重新打开（#332 E2／#337）",
    ),
    Mutation(
        "clarifier_topic_constraint_dropped",
        # 锚点只取那条约束的标题半句（全仓恰好一次）：去掉「不得推翻」后模型仍会
        # 被告知「只能细化」，但「问出来会和主题打架」这层禁止没了——用户答「改到
        # 庭院」时地点守卫仍要求 forest，答案在首帧节点被打回重试（模块 docstring
        # 记的已知残余风险就是这样被重新打开的）。守卫断言三个锚词，缺一即红。
        "src/minimax_h3_prompt/prompts/clarifier.md",
        "- **只能细化，不得推翻**：",
        "- **只能细化**：",
        ("tests/test_clarification.py::"
         "test_outbound_request_keeps_the_do_not_overturn_the_topic_rule",),
        "丢掉「不得推翻主题」约束＝澄清问题会去问「要不要换个地点」，答案与主题打架、"
        "首帧节点被地点守卫打回重试，用户的回答在此处失效（#337 票面「既有约束必须仍在」）",
    ),
    Mutation(
        "clarification_route_drops_generation",
        # 摘掉新端点里那一行生成调用（改成直接返回空列表）：路由照常 200、契约照常
        # 声明 200，返回体却永远是「这个主题不需要澄清」。接线守卫断言两件事——返回
        # 的问题等于桩模型给的那几句、桩模型收到的出站请求带主题与上限——任何一件
        # 失守都会红，所以这行被摘掉时不会静默绿（spec #332 US15「规则在、接线不在」
        # 不能第七次重演）。
        "src/minimax_h3_prompt/api/routes/clarification.py",
        "        return generate_clarifying_questions(topic, jobs.build_chat_model(), limit)\n",
        "        return []\n",
        ("tests/test_api_clarification_questions.py::"
         "test_endpoint_returns_the_stub_questions_and_sends_the_full_request",),
        "新端点摘掉生成调用＝前端永远拿到空问题列表，用户在建会话前一条澄清都问不到，"
        "而端点、契约与鉴权全都照常（#338 接线守卫）",
    ),
    # ------------------------------------------------------------------
    # #332 复审补齐的五条变异（「新加守卫必须有变异」，否则守卫不算守卫）
    # ------------------------------------------------------------------
    Mutation(
        "qa_loop_input_key_renamed_to_removed_field",
        # 把阶段 1 质检循环的真入参键改回 #334 已删的 ``qa_issues``：入参键与「已删键」
        # 混成一处，``test_qa_loops_use_their_own_input_key_not_a_state_field`` 的
        # ``"qa_issues" not in source`` 当场红（docstring 自述过这条变异，此前没登记）。
        "src/minimax_h3_prompt/graph/pipeline.py",
        '        state["frame_prompt_issues"] = [it.message for it in errors]\n',
        '        state["qa_issues"] = [it.message for it in errors]\n',
        ("tests/test_state_single_sided_fields.py::"
         "test_qa_loops_use_their_own_input_key_not_a_state_field",),
        "质检循环的入参键被改回已删的 qa_issues＝「删了一半」的形态复活，"
        "两处读者分不清哪个才是真入参（#334 的裁定依据失效）",
    ),
    Mutation(
        "clarification_pairs_bypass_the_shared_renderer",
        # 把后端唯一渲染口换成前端拼接形态（缺效力说明那行）：逐字同形断言当场红。
        # spec #332「变异检验」第三条正是「把渲染换成前端拼接 → 逐字同形断言变红」。
        "src/minimax_h3_prompt/api/routes/sessions.py",
        '    return render_clarification_block(pairs)\n',
        '    return "\\n".join(f"{i}. {q} → {a}" for i, (q, a) in enumerate(pairs, 1))\n',
        ("tests/test_api_clarification_questions.py::"
         "test_creating_a_session_with_pairs_renders_the_cli_identical_block",),
        "后端绕开共享渲染器、自己拼一份＝CLI 与工作台的澄清块逐字不同形，"
        "同一格式两处口径且不报错（#338 / spec #332「渲染只在后端一处」）",
    ),
    Mutation(
        "roundtable_graph_structure_changed",
        # 把「最后一位发言者接主持人」改成「第一位」：非空清单的图结构断言当场红。
        "src/minimax_h3_prompt/graph/roundtable.py",
        '    g.add_edge(f"speak_{participants[-1]}", "moderator")\n',
        '    g.add_edge(f"speak_{participants[0]}", "moderator")\n',
        ("tests/test_roundtable.py::"
         "test_non_empty_participants_still_build_the_same_graph",),
        "主持人接错发言者＝修空清单时顺手改掉了正常路径的接线，"
        "圆桌从「最后一位发言后收束」变成「第一位发言后就收束」（#333 的护栏失效）",
    ),
    Mutation(
        "removed_state_keys_are_still_read",
        # 在分镜节点里加回一句读 ``qa_issues``（赋给一个不使用的局部变量，不改变行为）：
        # 纯源码 AST 守卫 ``test_removed_keys_are_not_read_or_written_anywhere_in_the_pipeline``
        # 当场红——它按 AST 找字符串常量，不看这段代码有没有副作用。
        "src/minimax_h3_prompt/graph/nodes.py",
        '        shot_table = run_agent(agents["storyboard"], msg)\n',
        '        _stale_qa = state.get("qa_issues")\n'
        '        shot_table = run_agent(agents["storyboard"], msg)\n',
        ("tests/test_state_single_sided_fields.py::"
         "test_removed_keys_are_not_read_or_written_anywhere_in_the_pipeline",),
        "已删键重新长出读者＝「删了一半」的形态；行为上看不出，只有 AST 守卫会红"
        "（#334 的删除证据必须能咬住这类静默复活）",
    ),
    Mutation(
        "fake_pipeline_skips_clarification_shortcut",
        # 删掉桩管线的澄清接管分支：桩下澄清端点会去建真模型（test 里把 build_chat_model
        # 打成 _forbidden，一建就抛 AssertionError），「桩下不建任何模型」这条不变量失守。
        "src/minimax_h3_prompt/api/routes/clarification.py",
        "    if request.app.state.job_runner.fake_pipeline:\n"
        "        # 桩管线（D4 §11）：澄清与阶段 1/2 同属「桩下不建任何模型」这条判据。创建表单\n"
        "        # 每次提交前都会问一次，桩若不接管，这里就会去建真模型——CI 无密钥当场 502、\n"
        "        # 本机有密钥则真调网关（#339 实测）。\n"
        "        from ..fake_pipeline import clarification_questions\n"
        "\n"
        "        return clarification_questions(topic, limit)\n",
        "",
        ("tests/test_api_fake_pipeline.py::"
         "test_fake_pipeline_answers_clarifications_without_building_a_model",),
        "桩管线不接管澄清端点＝CI（无密钥）当场 502、本机（有密钥）真调网关，"
        "「桩管线跑完整个闭环不建任何模型」被新接线打破（#339）",
    ),
    # ------------------------------------------------------------------
    # #342 base 段边界避开镜头切点（#343）＋段首机位承接与动作分拍（#344）
    # 的守卫变异（#347 收口）。锚点都取「被删掉就会静默失效」的那一处：
    # 删掉判据、让切点抽取恒空、摘掉纪律注入、把 base 判据翻成多参同向、
    # 删掉规划指令——每条都必须让对应守卫当场红。
    # ------------------------------------------------------------------
    Mutation(
        "base_boundary_check_dropped",
        # 删掉 base 的「边界不得落在切点」判据：少了这一支，落在切点上的规划照样
        # 被接受，下一段开场就是一次硬换镜头（spec #342 的问题陈述原样复发）。
        # 锚点取整个 elif 分支（含缩进，全仓恰好一次）；if 主体不受影响，语法仍成立。
        "src/minimax_h3_prompt/segment_planner.py",
        "        elif mode == \"base\" and not _base_boundaries_off_cuts(plans, shot_cuts):\n"
        "            return None\n",
        "",
        ("tests/test_segment_planner.py::"
         "test_base_plan_rejects_internal_boundary_on_shot_cut",),
        "base 边界落在切点上也被接受＝段间接缝成了一次硬换镜头，桥接帧当 first_frame "
        "却与段词打架，而解析器一声不响（#343 / spec #342 主修静默失效）",
    ),
    Mutation(
        "shot_cuts_extraction_always_empty",
        # 锚点取构造 starts 的那一行：改成空表后 len(starts) < 2 立判 None，切点表
        # 永远抽不出来。这条钉的是「约束真的依赖切点」——抽取失效时判据不会恰好通过，
        # 而是整条降级（spec #342：抽不出时按「没有约束」处理，但绝不能悄悄变成
        # 「恒通过」或「恒不通过」）。
        "src/minimax_h3_prompt/segment_planner.py",
        "    starts = sorted({int(round(at)) for _n, at, _t in blocks if 0 <= int(round(at)) < total_int})\n",
        "    starts = []\n",
        ("tests/test_segment_planner.py::test_shot_cuts_from_table_reads_shot_timestamps",
         "tests/test_wizard_segment_cuts.py::test_segmented_flow_hands_shot_cuts_to_planner"),
        "切点抽取恒返回空＝base 的边界约束整条降级成「没有约束」，两条 base 调用点"
        "传进去的永远是 None，规则在、接线却静默断开（#343）",
    ),
    Mutation(
        "opening_camera_discipline_not_injected",
        # 锚点取 v2 指令里那一行（缩进 3 空格，全仓恰好一次；回退指令里同一纪律是
        # 2 空格缩进，拿裸常量名当锚点会命中两处）。old 含常量名，快档那条
        # ``test_every_writing_discipline_constant_has_a_mutation`` 靠它认出已有变异。
        "src/minimax_h3_prompt/segment_prompts.py",
        "   - {OPENING_CAMERA_CONTINUITY_DISCIPLINE}\n",
        "",
        ("tests/test_writing_disciplines_guard.py::"
         "test_opening_and_action_disciplines_reach_base_only",),
        "段首沿用机位纪律不进 v2 写段请求＝规划器避不开切点时再没有兜底，段 2 的首帧"
        "与上一段末帧落在不同机位，接缝成硬切且不报错（#344 / spec #342 US4）",
    ),
    Mutation(
        "support_shift_action_discipline_not_injected",
        # 同上的 v2 注入行（缩进 3 空格，全仓恰好一次）：摘掉后「改变支撑面的动作必须
        # 分拍」这条兜底纪律在 v2 路径消失，落座又会被压成一个拍点。
        "src/minimax_h3_prompt/segment_prompts.py",
        "   - {SUPPORT_SHIFT_ACTION_DISCIPLINE}\n",
        "",
        ("tests/test_writing_disciplines_guard.py::"
         "test_opening_and_action_disciplines_reach_base_only",),
        "动作分拍纪律不进 v2 写段请求＝落座／起身这类动作又被压成一个拍点，模型按最快"
        "插值补完，成片像自由落体（#344 / spec #342 US5）",
    ),
    Mutation(
        "base_boundary_check_switched_to_cut_aligned",
        # 把 base 的判据换成与多参同向（_boundaries_on_cuts）：两条路线方向相反的约定
        # 被抹平成一条，base 反过来要求边界落在切点上——串线守卫当场红。
        # 锚点只圈 elif 那一行（全仓恰好一次；两条 base 用例一起变红，证明结论确实相反）。
        "src/minimax_h3_prompt/segment_planner.py",
        "        elif mode == \"base\" and not _base_boundaries_off_cuts(plans, shot_cuts):\n",
        "        elif mode == \"base\" and not _boundaries_on_cuts(plans, shot_cuts):\n",
        ("tests/test_segment_planner.py::test_base_plan_rejects_internal_boundary_on_shot_cut",
         "tests/test_segment_planner.py::test_base_plan_accepts_internal_boundary_off_shot_cut"),
        "base 判据与多参同向＝落在切点上的规划被放行、避开切点的反而被拒，两条路线"
        "口径串成一条（spec #342「两套判据各自只在自己的模式里生效」失守）",
    ),
    Mutation(
        "fallback_boundary_record_silent",
        # 关掉「回复不合格回退机械拆分」的可见记录：规划回复不合格（含内部边界仍落在
        # 切点）时用户只看到机械拆分，而机械拆分恰恰按镜头切点切——接缝可能换机位这件
        # 事被静默藏起来（spec #342 US10）。
        "src/minimax_h3_prompt/ui/wizard.py",
        "    if not planning_crashed and shot_cuts is not None:\n",
        "    if False:  # 变异：可见记录被静默\n",
        ("tests/test_wizard_segment_cuts.py::"
         "test_segmented_flow_hands_shot_cuts_to_planner",),
        "规划回复不合格回退机械拆分不留可见记录＝「边界仍落切点、接缝可能换机位」"
        "被静默吞掉（spec #342 US10 的可见记录形同虚设）",
    ),
    Mutation(
        "base_planner_instruction_drops_off_cut_rule",
        # 删掉规划指令里的整条「内部段边界不得落在镜头切点上」（含解释段，全仓恰好
        # 一次）：模型眼前的请求里不再有这条硬规则，只能凭自身偏好收口到切点，
        # 合规规划全被拒、回退机械拆分（它恰恰按切点切）——主修静默失效。
        "src/minimax_h3_prompt/prompts/segment_planner.md",
        "- **内部段边界不得落在镜头切点上**（首尾帧链专用）：分镜表里每个 `[Shot N]` 的 `At MM:SS.mmm`\n"
        "  时刻都是一次镜头切换，而段与段之间靠**上一段剥出的尾帧**作为下一段首帧接着演——\n"
        "  边界落在切点时刻上，下一段开场就是一次硬切，桥接帧与段词互相打架。\n"
        "  所以段的结尾／下一段的开头必须落在某个镜头的**内部**（切点在 3 / 8 / 14 秒时，边界就不能取 8 或 14 秒）。\n"
        "  把边界放在**镜内、动作落定的剧情节拍处**——这两件事是同一件事：先按动作与剧情节拍找落点，\n"
        "  再确认这个整数秒不在切点时刻上；与切点冲突时把边界挪进镜头内部（前后 1-2 秒），\n"
        "  不要为了对齐切点牺牲动作完整。\n",
        "",
        ("tests/test_segment_planner.py::"
         "test_base_planner_request_states_boundaries_off_shot_cuts",),
        "规划指令删掉「内部段边界不得落在镜头切点上」＝模型只被告知选剧情节拍，很容易"
        "正落在切点上，解析器拒收后回退机械拆分又按切点切，主修静默失效（#343）",
    ),
    Mutation(
        "screenwriter_motive_source_dropped",
        # 摘掉新加动机条目里的来源限定半句（全仓恰好一次）：模型仍被告知动作要有
        # 动机，却不知道动机从哪来——最省事的来源就是自己发明，而 #350 基线测到的
        # 恰恰是「缺动机的表述」而不是「动作太多」，发明动作正是要防的那一侧。
        # 来源限定只许点名编剧节点真正收到的三份输入（2026-11-19 协调会话裁定，
        # 见 tests/test_role_prompts.py 同名用例 docstring）。
        "src/minimax_h3_prompt/prompts/screenwriter.md",
        "动机只能取自导演阐述、原始剧情、起步澄清三者之一",
        "",
        ("tests/test_role_prompts.py::"
         "test_screenwriter_actions_carry_their_motive",),
        "编剧层丢掉动机来源限定＝三条路线（base／ref／导演台）的剧本都能靠发明输入里"
        "没有的动作来「有动机」，而出站文本没有任何一处会报红（#351 / spec #349 C2-2）",
    ),
    # ------------------------------------------------------------------
    # 动作动机纪律的 base 弱化版（#353）：
    # 清空真源 / 只删 v2 注入 / 只删回退注入 / 误注入 ref——前三条钉「这条纪律真的
    # 送到了」，第四条钉「没送到不该去的地方」。两条注入行缩进不同（v2 是 ``   - {…}``
    # 三空格、回退是 ``  {…}`` 两空格），拿裸常量名当锚点会命中两处。
    # ------------------------------------------------------------------
    Mutation(
        "base_motive_discipline_cleared",
        # 锚点取常量赋值**整行**（全仓恰好一次）：真源自己就是被测断言的那份文本，
        # 只改几个字不会让「出站文本含真源全文」那条红——只有清空才让守卫的
        # 「先自证非空」那句响。
        "src/minimax_h3_prompt/writing_disciplines.py",
        'MOTIVE_DISCIPLINE_BASE = "**动作必须带动机**：分镜表里「这一镜揭示…」这类说明是**要落进画面的意图**，不是给你看的元信息——要把这层意图**写进动作句本身**（观众从动作与画面的选择里读出人物**想要什么、为什么此刻做**），不要只写成一句旁白式的说明、也不要把动机推给声音或环境。动机只许取自请求里已经写出的剧情事实——**本段剧情概述**、**分镜表**，或回退写段路径上的**完整提示词**——里写明的用意／事件，这些事实里都读不出来，就说明此刻本来没有动机，不要自行补一个理由。**不得**为动机发明这些事实里没有的动作、道具或事件——宁可少一个动作，也不要多一个观众看不懂的操作。所有事实都取不到动机时，写无动作状态（停顿、视线、呼吸），**不加一个小动作**。"',
        'MOTIVE_DISCIPLINE_BASE = ""',
        ("tests/test_writing_disciplines_guard.py::"
         "test_motive_discipline_reaches_base_only",),
        "清空动机弱化版真源＝base 两条写段路径同时失去动机纪律，动作句退回「只写做什么」"
        "（#350 基线：base 戏剧动机 0/9）；守卫若不自证真源非空，``\"\" in text`` 恒真"
        "就会永远不红（#353 / spec #349）",
    ),
    Mutation(
        "base_motive_discipline_shape_clamp_dropped",
        # 摘掉弱化版里「不加一个小动作」这条钳制（带粗体标记的整段在本文件恰好一次；
        # 上方注释里那句不带 ``**``，不会撞）。出站断言只证明「漂过的那份确实送到了」，
        # 常量少了钳制它照样绿——只有形态守卫钉住这一条。清空整条的那条变异也杀不到
        # 这种「常量还在、少了一个要素」的退化形态。
        "src/minimax_h3_prompt/writing_disciplines.py",
        "，**不加一个小动作**。",
        "。",
        ("tests/test_writing_disciplines_guard.py::"
         "test_base_motive_discipline_names_only_sources_base_paths_really_have",),
        "弱化版丢掉「不加一个小动作」＝取不到动机时模型被默许硬塞小动作，"
        "正是 US3/US4 要防的那一侧（#350 基线：缺的是动机表述、不是动作数量）"
        "（#353 复审 / spec #349）",
    ),
    Mutation(
        "base_motive_discipline_not_injected_v2",
        # v2 的注入行（缩进 3 空格，全仓恰好一次）：删掉后回退路径仍带、v2 不带，
        # 证明两条路径各自接线，不是「插了一次就以为两条都有」。
        "src/minimax_h3_prompt/segment_prompts.py",
        "   - {MOTIVE_DISCIPLINE_BASE}\n",
        "",
        ("tests/test_writing_disciplines_guard.py::"
         "test_motive_discipline_reaches_base_only",),
        "动机弱化版不进 v2 写段请求＝规划式长片（默认路径）的段词又只剩动作没有动机，"
        "而回退路径仍带——「规则在、接线不在」的静默形态（#353 / spec #349 C2-3）",
    ),
    Mutation(
        "base_motive_discipline_not_injected_fallback",
        # 回退的注入行（缩进 2 空格，全仓恰好一次）：这条路径只在规划失败时才走，
        # 漏了要等规划真的失败才现形——正是 #26 一类「只在回退路径上掉档次」的坑。
        "src/minimax_h3_prompt/segment_prompts.py",
        "  {MOTIVE_DISCIPLINE_BASE}\n",
        "",
        ("tests/test_writing_disciplines_guard.py::"
         "test_motive_discipline_reaches_base_only",),
        "动机弱化版不进回退写段请求＝规划一失败，段词就掉回「只写做什么」，而这条路径"
        "恰恰是规划失败时唯一在跑的（#353 / spec #349 C2-3）",
    ),
    Mutation(
        "base_motive_discipline_leaked_into_ref",
        # 把弱化版插进多参的纪律块（``## 机位速度`` 那一对行全仓恰好一次）：
        # 正向断言照旧全绿，只有「ref 一个字都不含」的反向断言能当场报红。
        "src/minimax_h3_prompt/segment_prompts.py",
        "## 机位速度\n- {CAMERA_SPEED_DISCIPLINE}",
        "## 机位速度\n- {CAMERA_SPEED_DISCIPLINE}\n- {MOTIVE_DISCIPLINE_BASE}",
        ("tests/test_writing_disciplines_guard.py::"
         "test_motive_discipline_reaches_base_only",),
        "动机弱化版误注入多参段写纪律块＝已验收的多参产物措辞被动过（2026-11-18／19 两次"
        "裁定 ref 不做段写层动机），而 base 侧的守卫全部照绿（#353 / spec #349）",
    ),
    # ------------------------------------------------------------------
    # #348：5xx 才记 error 日志（父 #296 的 F5）
    # ------------------------------------------------------------------
    Mutation(
        "problem_server_fault_not_logged",
        # 锚点取那行提前返回（全仓恰好一次）：整个日志调用变成死代码，5xx 又回到
        # 「服务端日志毫无记录」的原始缺陷（2026-11-19 #340 那次 502 就是这样丢的）。
        "src/minimax_h3_prompt/api/problems.py",
        "    if status < _SERVER_ERROR_THRESHOLD:\n        return\n",
        "    return\n",
        ("tests/test_api_problems.py::"
         "test_server_error_domain_fault_is_logged_with_code_and_path",),
        "5xx 不留痕＝服务端故障在 logs/app-*.log 里查不到，病因只能靠复现猜"
        "（2026-11-19 #340 的 502 原样复发，#348）",
    ),
    # ------------------------------------------------------------------
    # retro 2026-11-20：提示词声明的输入必须真的进了节点请求
    # ------------------------------------------------------------------
    Mutation(
        "role_prompt_declares_an_input_the_node_lacks",
        # 把编剧提示词的「## 输入」改回 #351 之前的错措辞（声明制作计划与分场剧本，
        # 而节点一个都不传）。这正是 #351 被两轴复审抓到的形态，只是当时没有机检。
        "src/minimax_h3_prompt/prompts/screenwriter.md",
        "导演阐述 + 原始剧情 + 起步澄清（+ 创意锁定）。",
        "导演阐述 + 制作计划 + 分场剧本。",
        ("tests/test_role_prompt_inputs.py::"
         "test_declared_inputs_reach_the_node_request",),
        "提示词声明了节点拿不到的输入＝模型被要求引用看不见的事实，只能编（#351 复审"
        "两轴都报过；retro 2026-11-20 补机检）",
    ),
    Mutation(
        "problem_log_threshold_widened_to_4xx",
        # 把下界放宽到 400：4xx 也记 error。反向断言（409/404 静默）当场红。
        # 锚点取赋值整行（全仓恰好一次）。
        "src/minimax_h3_prompt/api/problems.py",
        "_SERVER_ERROR_THRESHOLD = 500",
        "_SERVER_ERROR_THRESHOLD = 400",
        ("tests/test_api_problems.py::test_client_error_domain_fault_stays_silent",
         "tests/test_api_problems.py::test_not_found_stays_silent"),
        "判据放宽到 ≥400＝正常客户端错误也记 error，日志被刷屏、真故障被淹没（#348）",
    ),
)


def _anchor_occurrences(mutation: Mutation, root: Path) -> int:
    """``old`` 在目标文件里出现几次（按变异的换行口径归一）。"""
    path = root / mutation.path
    try:
        if mutation.normalize_newlines:
            text = path.read_text(encoding="utf-8")
        else:
            text = path.read_bytes().decode("utf-8")
    except OSError:
        return 0
    return text.count(mutation.old)


def anchor_problems(mutations: tuple[Mutation, ...], *, root: Path = ROOT) -> list[str]:
    """锚点失效清单：每条变异的 ``old`` 必须在目标文件里**恰好出现一次**。

    为什么要单独判：锚点 0 次（重构把那段代码挪走/改名）与「守卫是摆设」是两件事
    ——前者说明这条变异已经不再守任何东西，脚本却把它记成「存活」，看起来像守卫
    不够强，实际是锚点该更新（2026-11-13 #252 实测三条同时漂，其中两条 0 次）。
    ``fast`` 档由 ``tests/test_mutation_anchors.py`` 守着。
    """
    problems: list[str] = []
    for mutation in mutations:
        occurrences = _anchor_occurrences(mutation, root)
        if occurrences != 1:
            problems.append(
                f"{mutation.name}：锚点在 {mutation.path} 出现 {occurrences} 次"
                "（要求恰好 1 次）")
    return problems


def _vitest_argv(nodes: tuple[str, ...], *, node: str, vitest: str) -> list[str]:
    """把 ``<spec>::<测试名>`` 节点翻成 vitest 命令行。

    测试名走 ``-t``（按正则匹配），所以必须转义——用例名里有括号、点号之类的
    元字符时，不转义会匹配到别的用例或者干脆不匹配。
    """
    argv = [node, vitest, "run"]
    specs: list[str] = []
    names: list[str] = []
    for raw in nodes:
        spec, separator, name = raw.partition("::")
        specs.append(spec)
        if separator:
            names.append(name)
    if len(names) > 1:
        raise ValueError(
            "vitest 变异只支持一个 -t；要跑多条请拆成多条 Mutation")
    argv.extend(specs)
    if names:
        argv.extend(["-t", re.escape(names[0])])
    return argv


def _run_vitest(nodes: tuple[str, ...]) -> int:
    """跑前端 vitest 用例；依赖缺失时抛 ``MutationSkipped``，不误报成存活。"""
    frontend = ROOT / "frontend"
    vitest = frontend / "node_modules" / "vitest" / "vitest.mjs"
    node = shutil.which("node")
    if node is None or not vitest.is_file():
        raise MutationSkipped(
            "前端依赖或 node 不可用：先 cd frontend && npx pnpm@11.7.0 install")
    return subprocess.run(
        _vitest_argv(nodes, node=node, vitest=str(vitest)),
        cwd=str(frontend), capture_output=True, text=True,
        encoding="utf-8", errors="replace",
        env=dict(os.environ)).returncode


def _run_tests(nodes: tuple[str, ...], runner: str = "pytest") -> int:
    if runner == "vitest":
        return _run_vitest(nodes)
    if runner != "pytest":
        raise ValueError(f"未知的变异运行目标：{runner!r}")
    # PYTHONPATH 必须显式指向本次变异的那棵树的 ``src``：工作树里没有自己的 .venv，
    # 继承来的包路径指向主树，变异打在 A 树、用例读的却是 B 树——用例照绿，变异被
    # 报成「存活（守卫是摆设）」（2026-11-08 #199 复审实测）。主树里两者同一份，
    # 这一句不改变任何结果。
    env = dict(os.environ)
    # 变异期间不许写 .pyc：写下的缓存对应**改坏**的源码，而还原与变异常在同一秒内
    # 完成，Python 按「mtime（秒级）+ 大小」判有效，会把变异版字节码当成最新
    # （2026-11-10 #225 retro：`check.sh clean` 因此红了 9 条无关用例）。
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    src = str(ROOT / "src")
    env["PYTHONPATH"] = src + (os.pathsep + env["PYTHONPATH"]
                               if env.get("PYTHONPATH") else "")
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *nodes],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
        errors="replace", env=env).returncode


def _drop_bytecode(path: Path) -> None:
    """删掉该模块的 .pyc：它可能是**变异期间**写下的那份缓存。

    双保险的第二道（第一道是 ``_run_tests`` 的 ``PYTHONDONTWRITEBYTECODE``）：历史
    上跑过的变异可能已经在盘上留下改坏版缓存，只靠不再写新的清不掉。
    """
    Path(importlib.util.cache_from_source(str(path))).unlink(missing_ok=True)


def check(mutation: Mutation) -> tuple[bool, str]:
    """应用变异、跑到红、还原。返回 (是否被杀死, 说明)。

    还原是**字节级**的：把 ``read_bytes`` 拿到的原文用 ``write_bytes`` 写回。这样
    它不再依赖 ``git checkout --``，脏工作区与未跟踪文件都能跑（``check.sh full``
    现在会跑它）；也不做文本写回——CRLF 文件经文本写回会与索引不一致，``git status``
    变脏、``git switch`` 被拦（2026-11-02 实测）。
    """
    path = ROOT / mutation.path
    raw = path.read_bytes()
    original = raw.decode("utf-8")
    # 与 ``read_text`` 的通用换行同口径：锚点一律按 LF 写，CRLF 文件也能命中。
    # 测行尾的变异显式关掉这一步（见 ``normalize_newlines``）。
    normalized = original.replace("\r\n", "\n") if mutation.normalize_newlines else original
    occurrences = _anchor_occurrences(mutation, ROOT)
    if occurrences != 1:
        return False, f"锚点在 {mutation.path} 出现 {occurrences} 次（要求恰好 1 次）"
    mutated = normalized.replace(mutation.old, mutation.new, 1)
    if mutation.normalize_newlines and "\r\n" in original:
        mutated = mutated.replace("\n", "\r\n")
    try:
        path.write_bytes(mutated.encode("utf-8"))
        code = _run_tests(mutation.tests, mutation.runner)
    finally:
        path.write_bytes(raw)
        _drop_bytecode(path)
    if path.read_bytes() != raw:
        return False, f"还原失败：{mutation.path} 与原文不一致，请手动 git checkout"
    return code != 0, "用例变红（杀死）" if code != 0 else "用例仍然全绿（守卫是摆设）"


def main(argv: list[str] | None = None) -> int:
    # Windows 的 CI runner 控制台是 cp1252：中文一 print 就 UnicodeEncodeError，
    # 整个变异检验会以「脚本崩溃」而不是「守卫存活」结束（2026-11-02 实测）。
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="守卫测试的变异检验")
    parser.add_argument("--only", default=None, help="只跑指定的一条（按 name）")
    args = parser.parse_args(argv)

    selected = [m for m in MUTATIONS if args.only in (None, m.name)]
    if not selected:
        print(f"没有名为 {args.only!r} 的变异；可用：{', '.join(m.name for m in MUTATIONS)}")
        return 2

    # 锚点失效是**脚本错误**，不是「守卫是摆设」：两者修法不同（更新锚点 vs 加强
    # 守卫），混在一个「存活」里会把人引去写更多用例。先单独报出来，且不跑它们。
    broken = anchor_problems(tuple(selected))
    if broken:
        print("锚点失效（变异已经不在守任何东西；更新锚点后再判定守卫）：")
        for line in broken:
            print(f"  - {line}")
        selected = [m for m in selected
                    if not any(line.startswith(f"{m.name}：") for line in broken)]

    survivors = []
    skipped = []
    total = len(selected)
    for index, mutation in enumerate(selected, start=1):
        started = time.monotonic()
        try:
            killed, note = check(mutation)
        except MutationSkipped as exc:
            # 缺前端依赖不是「守卫是摆设」，也不是「杀死」：单独记，不算失败。
            print(f"[{index}/{total}] [跳过] {mutation.name}: {exc}"
                  f"（{time.monotonic() - started:.1f}s）", flush=True)
            skipped.append(mutation.name)
            continue
        # 逐条打印序号与耗时（2026-11-13）：115 条串行约 8 分钟，没有进度只能靠
        # 查进程表猜「卡住还是慢」。
        print(f"[{index}/{total}] [{'杀死' if killed else '存活'}] "
              f"{mutation.name}: {note}（{time.monotonic() - started:.1f}s）",
              flush=True)
        if not killed:
            survivors.append(mutation.name)

    if broken:
        return 2
    if survivors:
        print(f"\n{len(survivors)} 条变异没被杀死：{', '.join(survivors)}")
        return 1
    if skipped:
        print(f"\n{len(selected)} 条变异中 {len(skipped)} 条因环境缺失跳过，"
              f"其余全部被杀死。")
        return 0
    print(f"\n{len(selected)} 条变异全部被杀死。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

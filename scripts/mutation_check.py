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


MUTATIONS: tuple[Mutation, ...] = (
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
        "src/minimax_h3_prompt/director/film_end.py",
        '    if not fingerprint or meta.get("image_sha256") != fingerprint:\n'
        "        return None",
        "    if True:\n        return None",
        ("tests/test_director_film_end.py::test_matching_fingerprint_reuses_the_sidecar_without_reading_again",),
        "尾帧 sidecar 永不命中＝图没换也重读，续跑重复付费且不报错（#160）",
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
        "music_guided_segment_writes_na",
        "src/minimax_h3_prompt/prompts/segment_writer_director.md",
        "**不许写 `N/A`**",
        "**无声写 `N/A`**",
        ("tests/test_director_prompts.py::test_music_rule_continues_one_cue_across_segments",),
        "引导段配乐退回「无声写 N/A」＝段间持续曲重新断开（#194，实测 −9.8/+26.6 dB 台阶）",
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
        '    if plan.mode not in ("fl2v", "r2v"):',
        '    if plan.mode != "fl2v":',
        ("tests/test_director_prompts.py::"
         "test_r2v_guided_and_opening_requests_state_the_segment_start",),
        "起点事实块退回只认 fl2v＝r2v 引导段拿不到上一段落定画面（#192）",
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
        '                        "resolved_at": now,\n'
        '                        "revisions": recorded_revisions}).model_dump(mode="json")',
        '                        "resolved_at": now}).model_dump(mode="json")',
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
        "end_hook_verdict_affirmative_first",
        "src/minimax_h3_prompt/segment_validation.py",
        "    if any(marker in low for marker in _NEGATIVE_MARKERS):\n"
        "        return False\n"
        "    if any(marker in low for marker in _AFFIRMATIVE_MARKERS):\n"
        "        return True\n",
        "    if any(marker in low for marker in _AFFIRMATIVE_MARKERS):\n"
        "        return True\n"
        "    if any(marker in low for marker in _NEGATIVE_MARKERS):\n"
        "        return False\n",
        ("tests/test_segment_validation.py::test_parse_end_hook_verdict",),
        "否定词后判＝「未达到」里含「达到」，不合格的尾帧被判成达到，"
        "语义闸门永不让用户换视频（#246）",
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
        "ref_asset_lock_off",
        "src/minimax_h3_prompt/workbench_store.py",
        '        if session.brief.is_ref and session.status != "created":',
        "        if False and session.brief.is_ref and session.status != \"created\":",
        ("tests/test_api_ref_assets.py::"
         "test_registration_after_start_is_locked_but_plain_upload_is_not",),
        "起跑锁失效＝素材循环开始后还能加删台账，已写好的资产图词与图脱钩（#255）",
    ),
    Mutation(
        "asset_loop_settles_a_rejected_item",
        "src/minimax_h3_prompt/api/jobs.py",
        "            row[\"status\"] = (ASSET_LEDGER_STATUS_PASSED if passed\n"
        "                             else ASSET_LEDGER_STATUS_REGISTERED)",
        "            row[\"status\"] = ASSET_LEDGER_STATUS_PASSED",
        ("tests/test_api_asset_loop.py::"
         "test_proofread_failure_reopens_the_same_item_with_the_diff",),
        "不通过也落定＝校对结论只是建议，下一件会照样推进，差异清单永远无人处理（#256）",
    ),
    Mutation(
        "asset_loop_reopens_the_wrong_number",
        "src/minimax_h3_prompt/api/jobs.py",
        "                status=\"pending\", created_at=_now(), asset_index=number,\n"
        "                reason=reason)",
        "                status=\"pending\", created_at=_now(), asset_index=number + 1,\n"
        "                reason=reason)",
        ("tests/test_api_asset_loop.py::"
         "test_proofread_failure_reopens_the_same_item_with_the_diff",),
        "重开的件号错位＝用户被要求交下一件的图，而第 N 件永远定不了稿（#256）",
    ),
    Mutation(
        "ref_brief_not_wired",
        "src/minimax_h3_prompt/api/jobs.py",
        "        if not session.brief.is_ref:\n            return session.brief",
        "        if True:\n            return session.brief",
        ("tests/test_api_asset_loop.py::test_ref_stage1_sees_the_ledger_as_brief_refs",),
        "台账不喂给管线＝brief.refs 恒空，阶段 1 按不存在的参考素材写词、"
        "出词那一步按语义名对不上号（#256，spec §5.3）",
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
        "                target[\"status\"] = SEGMENT_STATUS_WAITING_VIDEO\n"
        "                target[\"video_asset_id\"] = \"\"",
        "                target[\"status\"] = SEGMENT_STATUS_WAITING_VIDEO\n"
        "                target[\"video_asset_id\"] = \"\"\n"
        "                target[\"assets\"] = []",
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


def _run_tests(nodes: tuple[str, ...]) -> int:
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
        code = _run_tests(mutation.tests)
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
    total = len(selected)
    for index, mutation in enumerate(selected, start=1):
        started = time.monotonic()
        killed, note = check(mutation)
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
    print(f"\n{len(selected)} 条变异全部被杀死。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

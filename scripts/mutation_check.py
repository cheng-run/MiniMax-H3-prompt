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

- 只改**工作区里干净**的文件（``git status --porcelain`` 有改动就拒绝），
  免得把用户没提交的东西卷进去；
- 每条变异都声明 ``old`` 在文件里**恰好出现一次**（多处出现说明锚点选错了，
  会用错地方）；
- 无论用例结果如何都还原（``finally``），还原后再核对文件与原文逐字相同。
"""
from __future__ import annotations

import argparse
import subprocess
import sys
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
)


def _git_clean(path: Path) -> bool:
    """内容层面干净才许变异（``git status`` 会把「只有 mtime 变了」也算脏）。

    上一次变异把文件写回原样后 mtime 变了、内容没变，用 ``status`` 判就会误报脏；
    ``git diff --quiet`` 比的是内容，正好是我们要的判据（工作区 vs 索引、索引 vs HEAD）。
    """
    rel = str(path.relative_to(ROOT))
    for extra in ([], ["--cached"]):
        result = subprocess.run(
            ["git", "diff", "--quiet", *extra, "--", rel],
            cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace")
        if result.returncode != 0:
            return False
    return True


def _run_tests(nodes: tuple[str, ...]) -> int:
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *nodes],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace").returncode


def check(mutation: Mutation) -> tuple[bool, str]:
    """应用变异、跑到红、还原。返回 (是否被杀死, 说明)。"""
    path = ROOT / mutation.path
    original = path.read_text(encoding="utf-8")
    occurrences = original.count(mutation.old)
    if occurrences != 1:
        return False, f"锚点在 {mutation.path} 出现 {occurrences} 次（要求恰好 1 次）"
    if not _git_clean(path):
        return False, f"{mutation.path} 工作区不干净，拒绝变异"

    mutated = original.replace(mutation.old, mutation.new, 1)
    try:
        path.write_text(mutated, encoding="utf-8")
        code = _run_tests(mutation.tests)
    finally:
        # 用 git 还原，不用文本写回：CRLF 文件写回后与索引不一致，`git status` 会
        # 变脏、`git switch` 被拦（2026-11-02 合并 governance 分支时实测，
        # 只能先 git checkout 五个文件才切得动）。git checkout 是字节级还原。
        subprocess.run(
            ["git", "checkout", "--", str(path.relative_to(ROOT))],
            cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace")
    restored = path.read_text(encoding="utf-8")
    if restored != original:
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

    survivors = []
    for mutation in selected:
        killed, note = check(mutation)
        print(f"[{'杀死' if killed else '存活'}] {mutation.name}: {note}")
        if not killed:
            survivors.append(mutation.name)

    if survivors:
        print(f"\n{len(survivors)} 条变异没被杀死：{', '.join(survivors)}")
        return 1
    print(f"\n{len(selected)} 条变异全部被杀死。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

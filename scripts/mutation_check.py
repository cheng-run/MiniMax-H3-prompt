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
        'DIRECTOR_ASSET_MODES = ("r2v", "rv2v", "mixed")',
        'DIRECTOR_ASSET_MODES = ("rv2v", "mixed")',
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

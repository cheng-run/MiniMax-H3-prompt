"""由 ``launch.py`` 调用的 CLI 实现；项目唯一启动入口是 ``uv run launch.py``。

``main()`` 保留为内部可测试函数，不作为独立命令注册。

资产库（Assets 主题 / 项目文档 / Workflow Profile / 分段执行闭环）已整体移除，
本 CLI 只保留面向用户的两阶段向导与 brief 快路径，以及 generate-prompts 入口。
"""
from __future__ import annotations

import argparse
import json


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="launch.py",
        description="MiniMax-H3 多智能体提示词生成系统",
    )
    parser.add_argument("--brief", default=None, help="brief 文件路径（不填则进交互菜单）")
    parser.add_argument("--model", choices=["fl2va"], default=None,
                        help="出片模型（当前产品流程固定使用 FL2VA 首尾帧）")
    parser.add_argument("--mode", choices=["base"], default=None, help="当前产品固定使用 base/FL2VA")
    parser.add_argument("--variant", choices=["I2VA", "L2VA", "FL2VA"], default=None, help="视频生成方式（首帧 I2VA / 尾帧 L2VA / 首尾帧 FL2VA，缺省按 brief 声明或默认 FL2VA）")
    parser.add_argument("--polish", action="store_true", help="polish 模式：润色 brief 中已有的草稿提示词")
    parser.add_argument("--dry-run", action="store_true", help="只解析 brief 不跑 LLM 管线（自检用）")

    project = parser.add_subparsers(dest="command")
    create_parser = project.add_parser("create-video", help="输入主题，自动生成剧本与视频/关键帧提示词")
    create_parser.add_argument("--topic", required=True, help="视频主题")
    create_parser.add_argument("--duration", type=float, default=None, help="视频时长（秒，默认使用配置）")
    create_parser.add_argument("--style", default=None, help="视觉风格（默认使用配置）")
    create_parser.add_argument("--language", default=None, help="提示词语言（默认使用配置）")
    create_parser.add_argument("--variant", choices=["I2VA", "L2VA", "FL2VA"], default="FL2VA")

    generate_parser = project.add_parser("generate-prompts", help="从 brief 生成剧本与视频/关键帧提示词")
    generate_parser.add_argument("--brief", "-b", required=False, help="brief 文件路径")
    generate_parser.add_argument("--topic", default=None, help="视频主题")
    generate_parser.add_argument("--duration", type=float, default=None, help="视频时长（秒）")
    generate_parser.add_argument("--style", default=None, help="视觉风格")
    generate_parser.add_argument("--language", default=None, help="提示词语言")
    generate_parser.add_argument("--variant", choices=["I2VA", "L2VA", "FL2VA"], default="FL2VA")
    generate_parser.add_argument("--dry-run", action="store_true", help="只解析 brief，不调用模型或落盘")

    ledger_parser = project.add_parser("ledger", help="生成台账：从磁盘重建镜头链路")
    ledger_sub = ledger_parser.add_subparsers(dest="ledger_command")
    ledger_rebuild = ledger_sub.add_parser("rebuild", help="重扫并写出 ledger.json")
    ledger_rebuild.add_argument("session_dir", help="会话目录（含 bridge_frames/ 与 segments/）")
    ledger_rebuild.add_argument("--output-dir", action="append", required=True,
                                help="ComfyUI 输出目录，可重复")
    ledger_rebuild.add_argument("--threshold", type=float, default=5.0,
                                help="mad 阈值（默认 5.0）")
    ledger_rebuild.add_argument("--json", action="store_true", help="只输出 JSON，不写文件")

    audit_parser = project.add_parser("audit", help="接缝审计：拼接并量化接缝静止")
    audit_sub = audit_parser.add_subparsers(dest="audit_command")
    audit_run = audit_sub.add_parser("run", help="跑一次审计并写出报告")
    audit_run.add_argument("session_dir")
    audit_run.add_argument("--output-dir", action="append", required=True)
    audit_run.add_argument("--no-merge", action="store_true", help="只分析不合并视频")
    audit_promote = audit_sub.add_parser("promote", help="把最近一次结果提升为基线")
    audit_promote.add_argument("session_dir")
    audit_promote.add_argument("--label", required=True, help="基线标签，如「修复前」")

    concat_parser = project.add_parser(
        "ref-concat", help="多参成片拼接：把已通过验收的各段接成成片（重跑某段后单独触发）")
    concat_parser.add_argument("session_dir", help="生成目录（含 segments/plan.json 与段产出账）")
    concat_parser.add_argument("--replace", action="append", nargs=2, default=[],
                               metavar=("SEGMENT", "PATH"),
                               help="重跑某段后改用它的新视频（可重复；这一份未经本系统验收）")

    pre_parser = project.add_parser("preflight", help="提交前预检：校验输入帧与预算")
    pre_parser.add_argument("--input-frame", required=True, help="待提交的桥接帧 png")
    pre_parser.add_argument("--prev-video", default=None, help="上一段的 mp4")
    pre_parser.add_argument("--prev-video-name", default=None,
                            help="上一段在 ledger 里的文件名（用于废片检查）")
    pre_parser.add_argument("--session", default=None, help="会话目录（读 ledger 用）")
    pre_parser.add_argument("--prompt", default=None, help="提示词或 end_hook 文本文件")
    pre_parser.add_argument("--duration", type=float, default=None, help="本段时长（秒）")
    pre_parser.add_argument("--budget-tokens", type=int, default=0,
                            help="总额度 token（0 = 不检查预算）")
    pre_parser.add_argument("--tokens-per-shot", type=int, default=0,
                            help="单段实测 token 消耗")
    pre_parser.add_argument("--remaining-shots", type=int, default=0,
                            help="计划还剩几段")
    return parser


def _apply_frame_route_defaults(brief, args) -> None:
    """帧路线的旧默认（强制 base + 缺省 FL2VA）；多参（mode=ref）不套帧变体（issue #41）。

    两条 CLI 缝（_run_cli 与 _run_generate_prompts）共用这一份：级联各写一遍时
    「--variant 对 ref 该不该生效」的答案会随缝而异（复审 Duplicated Code 项）。
    """
    if brief.mode == "ref":
        return
    brief.mode = "base"
    if args.variant:
        brief.variant = args.variant.upper()
    elif brief.variant == "T2VA":
        brief.variant = "FL2VA"


def _topic_progress(event: dict) -> None:
    """把主题单入口的模型调用进度显示给最终用户。"""
    event_type = event.get("type")
    role = str(event.get("role", "模型")).removeprefix("role_")
    if event_type == "agent_start":
        print(f"[进行中] {role} 正在处理……", flush=True)
    elif event_type == "agent_done":
        duration = event.get("duration", 0.0)
        print(f"[完成] {role}（{duration:.1f}s）", flush=True)


def _create_video_with_progress(topic: str, config, **kwargs):
    """运行主题生成，并确保异常或中断时解除进度订阅。"""
    from .observability import reporter
    from .topic_generation import create_video_from_topic

    reporter.subscribe(_topic_progress)
    try:
        return create_video_from_topic(topic, config, **kwargs)
    finally:
        reporter.unsubscribe(_topic_progress)


def _print_generation_result(result, directory) -> None:
    print(f"\n执行完成：{directory}")
    print("\n剧本：\n" + result.script)
    if result.fl2va_prompt_bundle is not None:
        from .generation import render_fl2va_frame_markdown
        print(f"\n{'=' * 20} FL2VA 首帧提示词 {'=' * 20}\n{render_fl2va_frame_markdown(result, 'first')}")
        print(f"\n{'=' * 20} FL2VA 尾帧提示词 {'=' * 20}\n{render_fl2va_frame_markdown(result, 'last')}")
        print(f"\nFL2VA 汇总文件：{directory / 'fl2va-prompt.md'}")
        print(f"首帧文件：{directory / 'first-frame-prompt.md'}")
        print(f"尾帧文件：{directory / 'last-frame-prompt.md'}")
        print("请将首帧/尾帧图片人工绑定到 FL2VA 的 first_frame/last_frame 输入槽；项目不会自动运行 ComfyUI。")
    else:
        for kind, label in (("character", "人物提示词"), ("prop", "道具提示词"), ("scene", "场景提示词"), ("video", "视频剧情提示词")):
            print(f"\n{'=' * 20} {label} {'=' * 20}\n{result.artifact(kind).content}")
    print("\n以上内容已保存；请手动复制到对应模型并自行审查生成结果。")


def _run_create_video(args) -> int:
    from .config import config
    from .topic_generation import create_video_from_topic

    result, directory = _create_video_with_progress(
        args.topic, config, duration=args.duration,
        style=args.style, language=args.language, variant=args.variant,
    )
    _print_generation_result(result, directory)
    return 0


def _run_generate_prompts(args) -> int:
    from .brief_parser import parse_brief
    from .config import config
    from .topic_generation import create_video_from_topic

    if args.brief:
        brief = parse_brief(args.brief)
        # 多参路线（issue #41）：brief 声明 ref 就走 ref，参考素材随 brief 一起传。
        _apply_frame_route_defaults(brief, args)
        if args.dry_run:
            print(json.dumps({
                "brief": {"mode": brief.mode, "variant": brief.variant, "duration": brief.duration,
                          "style": brief.style, "language": brief.language, "plot": brief.plot, "draft": brief.draft,
                          # 画布回显（issue #42）：dry-run 的用途就是「看解析对不对」，
                          # 选定画布不回显就无从发现画布键写错/没读到。
                          "canvases": {family: [w, h] for family, (w, h) in brief.canvases.items()}},
                "dry_run": True,
            }, ensure_ascii=False, indent=2))
            return 0
        result, directory = create_video_from_topic(
            brief.plot, config,
            duration=brief.duration, style=brief.style, language=brief.language,
            variant=brief.variant, mode=brief.mode, refs=brief.refs,
            # 画布键解析出来就得递进来（issue #42 复审）：少这一参，brief 文件里的
            # 选定画布在最后一跳被丢掉、静默回落系统默认——「规则在、接线不在」。
            canvases=brief.canvases,
            # 视频输出三元组同待遇（issue #45）：解析出就得递到注入卡，不许中途丢。
            video_output=brief.video_output,
        )
        _print_generation_result(result, directory)
        return 0

    # 无 --brief：用主题直接跑（等价 create-video，统一一条入口）
    if not args.topic:
        raise ValueError("generate-prompts 需要 --brief 文件或 --topic 主题")
    result, directory = _create_video_with_progress(
        args.topic, config, duration=args.duration,
        style=args.style, language=args.language, variant=args.variant,
    )
    _print_generation_result(result, directory)
    return 0


def _run_ledger(args: argparse.Namespace) -> int:
    """从磁盘重建生成台账，只读输入，仅写会话目录下的 ledger.json。"""
    from pathlib import Path

    from .tools.ledger import rebuild

    session_dir = Path(args.session_dir)
    if not session_dir.is_dir():
        print(f"[错误] 会话目录不存在：{session_dir}")
        return 1
    if getattr(args, "ledger_command", None) != "rebuild":
        print("[错误] 请指定子命令，例如：ledger rebuild <session_dir> --output-dir <dir>")
        return 1

    write = not args.json
    ledger = rebuild(session_dir, [Path(d) for d in args.output_dir],
                     threshold=args.threshold, write=write)
    if args.json:
        print(json.dumps(ledger.to_dict(), ensure_ascii=False, indent=2))
        return 0

    print(f"镜头 {len(ledger.shots)} 个：" + " → ".join(s.video for s in ledger.shots))
    for item in ledger.discarded:
        print(f"[废片] {item.video}（{item.confidence}）— {item.reason}")
    for item in ledger.interruptions:
        print(f"[中断] 第 {item.at_shot} 段未产出（{item.confidence}）— {item.hint}")
    for note in ledger.review_needed:
        print(f"[待确认] {note}")
    for note in ledger.warnings:
        print(f"[警告] {note}")
    print(f"已写出：{session_dir / 'ledger.json'}")
    return 0


def _run_audit(args: argparse.Namespace) -> int:
    """接缝审计：从 ledger 取镜头顺序，拼接并量化。"""
    import json
    from pathlib import Path

    from .tools import seam_audit
    from .tools.ledger import rebuild
    from .tools.seam_audit import AuditReport, SeamResult, ShotStill, promote_baseline

    session_dir = Path(args.session_dir)
    if not session_dir.is_dir():
        print(f"[错误] 会话目录不存在：{session_dir}")
        return 1
    command = getattr(args, "audit_command", None)
    out_dir = session_dir / "audit"

    if command == "promote":
        latest = out_dir / "latest.json"
        if not latest.is_file():
            print("[错误] 还没有审计结果，请先跑 audit run")
            return 1
        raw = json.loads(latest.read_text(encoding="utf-8"))
        report = AuditReport(
            shots=[ShotStill(**s) for s in raw["shots"]],
            seams=[SeamResult(**s) for s in raw["seams"]],
            duration_s=raw["duration_s"], still_seconds=raw["still_seconds"],
            still_ratio=raw["still_ratio"],
            per_seam_avg_frames=raw["per_seam_avg_frames"],
            warnings=raw.get("warnings", []), merged_path=raw.get("merged_path", ""))
        path = promote_baseline(session_dir, report, args.label)
        print(f"基线已写出：{path}")
        return 0

    if command != "run":
        print("[错误] 请指定子命令：audit run / audit promote")
        return 1

    output_dirs = [Path(d) for d in args.output_dir]
    ledger = rebuild(session_dir, output_dirs)
    videos = _resolve_videos(ledger, output_dirs)
    if not videos:
        print("[错误] ledger 里没有可用的镜头，无法审计")
        return 1

    report = seam_audit.analyze(videos, out_dir, merge=not args.no_merge)
    out_dir.mkdir(parents=True, exist_ok=True)
    baseline = seam_audit.load_baseline(session_dir)
    report_path = out_dir / f"{_timestamp()}.md"
    report_path.write_text(seam_audit.render_markdown(report, baseline),
                           encoding="utf-8")
    (out_dir / "latest.json").write_text(
        json.dumps({"shots": [vars(s) for s in report.shots],
                    "seams": [vars(s) for s in report.seams],
                    "duration_s": report.duration_s,
                    "still_seconds": report.still_seconds,
                    "still_ratio": report.still_ratio,
                    "per_seam_avg_frames": report.per_seam_avg_frames,
                    "warnings": report.warnings,
                    "merged_path": report.merged_path},
                   ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"镜头 {len(report.shots)} 个 ｜ 每缝均值 "
          f"{report.per_seam_avg_frames:.1f} 帧 ｜ 占全片 "
          f"{report.still_ratio * 100:.1f}%")
    if baseline:
        for name, base, cur, delta in seam_audit.compare_to_baseline(report, baseline):
            print(f"  {name}: 基线 {base} → 本次 {cur}（{delta}）")
    for note in report.warnings:
        print(f"[警告] {note}")
    print(f"报告：{report_path}")
    return 0


def _resolve_videos(ledger, output_dirs) -> list:
    """把 ledger 里的镜头名解析回真实文件路径。"""
    from pathlib import Path

    index: dict[str, Path] = {}
    for directory in output_dirs:
        if directory.is_dir():
            for path in directory.rglob("*.mp4"):
                index.setdefault(path.name, path)
    resolved = []
    for shot in ledger.shots:
        path = index.get(shot.video)
        if path is not None:
            resolved.append(path)
    return resolved


def _timestamp() -> str:
    """当前本地时间戳，用于审计报告文件名。"""
    from datetime import datetime

    return datetime.now().strftime("%Y-%m-%dT%H%M%S")


def _run_ref_concat(args: argparse.Namespace) -> int:
    """多参成片的手动拼接（issue #57 AC4）：重跑某段后不必从头走完整流程。

    与向导里的**自动**拼接是同一份编排（``ref_segments.assemble_final_film``）：段序取
    ``segments/plan.json``（段序的唯一出站真源），「哪几段过了」取段产出账的
    ``event=output`` 行。少一段就拒绝并点名——缺段的成片时长对不上总时长，是伪产物
    （AC3），这条规矩住在被拼接的那一处，两条路共用一副牙齿。

    ``--replace N <路径>`` 是「重跑某段」的落点：账上记的还是旧那一份（甚至旧文件已被
    换版），用它把该段指到新视频上。这一份**没经过本系统验收**（命令非交互，跑不了两道
    闸），所以当面说破、不装作它过了闸。
    """
    from pathlib import Path

    from .asset_ledger import sha256_file
    from .ref_segments import (
        assemble_final_film,
        format_final_film,
        passed_segment_rows,
        read_plan_file,
    )

    directory = Path(args.session_dir)
    if not directory.is_dir():
        print(f"[错误] 生成目录不存在：{directory}")
        return 1
    try:
        plans = read_plan_file(directory)
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        print(f"[错误] {exc}")
        return 1

    rows = passed_segment_rows(directory)
    known = {plan.index + 1 for plan in plans}
    for raw_segment, raw_path in args.replace:
        try:
            number = int(raw_segment)
        except ValueError:
            print(f"[错误] --replace 的段号不是整数：{raw_segment!r}")
            return 1
        if number not in known:
            # 段号写错时**当面报**：不报的话这一份会被静默丢掉，用户以为换上了
            print(f"[错误] 本片没有第 {number} 段（段号 1–{max(known)}）：{directory}")
            return 1
        video = Path(raw_path)
        if not video.is_file():
            print(f"[错误] 第 {number} 段的替换视频读不到：{video}")
            return 1
        rows[number] = {"segment": number, "video": str(video),
                        "sha256": sha256_file(video)}
        print(f"[替换] 第 {number} 段改用你指定的视频（**未经本系统验收**）：{video}")

    planned_s = sum(plan.duration_s for plan in plans)
    total_s = _ref_total_duration(directory)
    if total_s is None:
        # 取不到就**说破**再退：起步总时长是段表覆盖判据与「成片该多长」的对照物，
        # 静默拿另一个数顶上就是拿计划值冒充用户目标（两者在末段留余量时不等）。
        print(f"[提示] 读不到会话文件里的起步总时长：按各段计划时长之和 {planned_s}s 核对。")
        total_s = planned_s
    film = assemble_final_film(
        directory=directory, plans=plans, total_s=total_s, rows=rows)
    print(format_final_film(film))
    return 0 if film.path is not None else 1


def _ref_total_duration(directory: Path) -> int | None:
    """从会话文件取**起步总时长**（手动拼接命令的「本片总长」口径）。

    取不到（文件不在/坏了）返回 None，由调用方退回「各段计划时长之和」并当面说破——
    不静默拿计划值冒充用户目标（末段留余量时两者不等）。
    """
    from .session_store import load_session

    try:
        session = load_session(directory)
    except (OSError, ValueError):
        return None
    if session is None:
        return None
    return int(round(float(session.brief.duration)))


def _run_preflight(args: argparse.Namespace) -> int:
    """提交前预检。可证规则失败返回 1，其余返回 0。"""
    from pathlib import Path

    from .tools import preflight
    from .tools.ledger import Ledger

    issues: list[preflight.Issue] = []
    prev_video = Path(args.prev_video) if args.prev_video else None

    ledger = None
    if args.session:
        session_dir = Path(args.session)
        if not session_dir.is_dir():
            print(f"[错误] 会话目录不存在：{session_dir}")
            return 1
        ledger_path = session_dir / "ledger.json"
        if ledger_path.is_file():
            try:
                ledger = Ledger.from_dict(json.loads(ledger_path.read_text(
                    encoding="utf-8")))
            except (OSError, ValueError) as exc:
                print(f"[警告] ledger 读不了（{exc}），跳过废片检查")
        else:
            print("[警告] 会话目录里没有 ledger.json，跳过废片检查")

    issues += preflight.check_input_frame(Path(args.input_frame), prev_video)

    if ledger is not None and args.prev_video_name:
        issues += preflight.check_not_discarded(args.prev_video_name, ledger)

    if args.duration is not None:
        issues += preflight.check_shot_duration(args.duration)

    if args.prompt:
        prompt_path = Path(args.prompt)
        if not prompt_path.is_file():
            print(f"[错误] 提示词文件不存在：{prompt_path}")
            return 1
        issues += preflight.check_frozen_words(
            prompt_path.read_text(encoding="utf-8"))

    if args.budget_tokens:
        issues += preflight.check_budget(
            remaining_tokens=args.budget_tokens,
            tokens_per_shot=args.tokens_per_shot,
            remaining_shots=args.remaining_shots,
        )

    errors = [i for i in issues if i.severity == "error"]
    for issue in issues:
        tag = {"error": "[阻断]", "warning": "[提醒]", "refrain": "[劝阻]"}[issue.severity]
        print(f"{tag} {issue.message}")

    if errors:
        print(f"\n预检未通过：{len(errors)} 项阻断。请先修输入再提交。")
        return 1
    print("\n预检通过。")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.command == "create-video":
        return _run_create_video(args)
    if args.command == "generate-prompts":
        return _run_generate_prompts(args)
    if args.command == "ledger":
        return _run_ledger(args)
    if args.command == "audit":
        return _run_audit(args)
    if args.command == "ref-concat":
        return _run_ref_concat(args)
    if args.command == "preflight":
        return _run_preflight(args)

    if not args.brief:
        # 面向最终用户的两阶段向导：先生图提示词 → 人工生图 → 提交图片 → 视频提示词。
        from .config import config
        from .ui.wizard import run_wizard

        try:
            return run_wizard(config)
        except (KeyboardInterrupt, EOFError):
            # 续跑点（issue #51）：已答的内容与已做的裁决都逐条落了盘。措辞只说
            # **已落盘的**——阶段 1 中途断掉时还没有任何答案账可写，承诺过头同样是谎报。
            print("\n已取消。已落盘的进度会保留：重跑向导从续跑点继续，已答的不必重答。")
            return 1
    return _run_cli(args)


def _deliver_ref_segments_cli(brief, prompt: str, out_dir) -> None:
    """--brief 快路径的多参分段交付（issue #46）：薄接线，编排/文案与向导同一份。"""
    from .ref_segments import deliver_ref_segments

    _result, report = deliver_ref_segments(
        whole_prompt=prompt, total_s=brief.duration, out_dir=out_dir, brief=brief)
    print(report)


def _run_cli(args: argparse.Namespace) -> int:
    """非交互快路径：--brief 必填，直接跑。"""
    from .brief_parser import parse_brief
    from .config import config

    brief = parse_brief(args.brief)
    # 多参路线（issue #41）：brief 声明 ref（输入: 多参）就走 ref——「不再强制 base」。
    _apply_frame_route_defaults(brief, args)
    if args.model and args.model != "fl2va":
        raise ValueError("当前产品只支持 fl2va 模型")
    if args.mode and args.mode != "base":
        raise ValueError("当前产品只支持 base 模式")
    if args.polish and not brief.draft:
        print("[提示] --polish 需要 brief 里有「草稿提示词」段落；当前没有草稿，按全流程生成。")

    print(f"模式: {brief.mode}" + (f" / 变体: {brief.variant}" if brief.mode == "base" else ""))
    print(f"时长: {brief.duration}s | 风格: {brief.style} | 语言: {brief.language}")
    print(f"剧情: {brief.plot[:80]}{'…' if len(brief.plot) > 80 else ''}")
    if brief.refs:
        print("参考图:")
        for r in brief.refs:
            print(f"  <Picture {r.picture}> {r.name} — {r.description[:60]}")
        # 素材台账边界声明（issue #43）：本快路径只产提示词文本、没有生成目录，
        # 台账与正本复制没有落点——不静默装作已建账，指路两条有生成目录的产线
        # （同「绝不静默失败」：能力的可达边界要看得见，不许静默缺位）。
        if brief.mode == "ref" and any(r.path for r in brief.refs):
            print("[提示] --brief 快路径不建素材台账与注入卡（无生成目录）；需要台账"
                  "（语义名/编号/路径/SHA256）或注入卡请用 generate-prompts --brief"
                  " 或两阶段向导交回入库。")
    if brief.draft:
        print(f"草稿: {brief.draft[:80]}{'…' if len(brief.draft) > 80 else ''}")

    if args.dry_run:
        print("\n[dry-run] 解析通过，未运行管线。")
        return 0

    from .graph.pipeline import run_pipeline

    final_prompt = run_pipeline(brief, config)
    config.output_path.parent.mkdir(parents=True, exist_ok=True)
    config.output_path.write_text(final_prompt, encoding="utf-8")
    # 分流口径**从常量派生**（issue #58 复审）：这条判据与向导 `_phase2_ref_finish`、
    # 续跑装载的 `annotated` 是**同一条契约**（整片能不能一次喂给 H3 ＝ 规划器的执行段
    # 上限），所以走同一个 `is_single_segment_route`，不在这里再写一个字面 15。
    from .ref_segments import is_single_segment_route

    if brief.mode == "ref" and not is_single_segment_route(brief.duration):
        # 与向导 _phase2_ref_finish 同口径（issue #46/#55）：>15 秒按镜头边界拆段，
        # 逐段自包含六段式＋段清单/拼接顺序卡落盘；整片六段式降级为**存档预览**
        # ——交付物是逐段落盘的段提示词。**逐段陪跑不在这条路径上**（issue #55）：
        # 它要交回视频、过可证层闸门、落段产出账，前提是交互终端；本快路径非交互，
        # 且 stdout 要能整份取走（脚本场景），所以整片照旧打印、只是标明是存档。
        _deliver_ref_segments_cli(brief, final_prompt, config.output_path.parent)
        print(f"\n已完成。整片 ref 六段式已写入: {config.output_path}（仅存档预览）")
        print("交付物是上面逐段落盘的段提示词文件；逐段陪跑（交回视频＋可证层验收＋"
              "段产出账）请用两阶段向导。")
    else:
        print(f"\n已完成，最终提示词已写入: {config.output_path}")
        if brief.mode == "ref":
            # ≤15 秒的收尾（交回本段视频 → 两道闸 → 成片，issue #57）与 >15s 同理要交互
            # 终端，快路径走不了：向导里整片六段式就是那唯一一段的提示词，段文件/plan.json/
            # 顺序卡与成片都由它产出。不静默装作这条路也能出成片。
            print("（≤15 秒多参的逐段陪跑与成片请用两阶段向导：本快路径非交互，交不回视频。）")
    print("=" * 60)
    print(final_prompt)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
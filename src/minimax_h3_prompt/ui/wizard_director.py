"""导演台陪跑用到的向导小件（issue #158）：模型构造与画幅预检。

与显示小件（``wizard_display.py``）的分工：那边只拼字符串；这里要建客户端、要问
一句并等回车，属于「编排的边角」。放在这里是为了让 6000 行的 ``wizard.py`` 不再增长
（``tests/test_wizard_size.py`` 的闸）；问答入口由调用方注入（``ask``），所以本模块
不依赖 ``wizard``、也不会形成环。
"""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple

if TYPE_CHECKING:  # 只为注解：运行时走函数内的延迟导入，别把规划栈拖进模块入口
    from ..director.planning import AssetBudget, DirectorPlan


def write_context_blocks(state: dict, shot_table: str) -> dict[str, Any]:
    """导演台写段请求的两个上下文块（#299／#300）：画面决策按段渲染、修订单份。

    为什么住这里：``ui/wizard.py`` 的体量受 ``tests/test_wizard_size.py`` 的闸管着，
    纯渲染／纯组装按仓库纪律搬进 ``wizard_*.py``。首写（``_run_director_flow``）与
    改词（``_rewrite_director_segment_round``）两个出口共用这一处，否则同一份上下文
    会写成两套来源（「同一契约两处口径」）。

    逐字节等价：两个值只由本会话的 ``stage_state`` 决定，与原先在两个调用点各写一次
    的表达式相同；搬迁前后用记录型桩模型对照过出站请求，diff 为空。
    """
    from ..director.prompts import visual_decision_source
    from ..segment_prompts import render_revision_context

    return {
        "revision_context": render_revision_context(state),
        "visual_decision": visual_decision_source(state, shot_table),
    }


def persist_director_plan(session_dir: str | Path, plan: DirectorPlan,
                          budget: AssetBudget, *, mode: str,
                          film_end: bool = False) -> tuple[Path, Path | None]:
    """落段表与段内素材预算，并出站分段清单（#186 从 6000 行的 wizard 搬出来）。

    预算必须在**写段之前**落盘：写段只能在本段预算里选图（#188）。两份文件同目录、
    按段号对齐，``plan.json`` 不存预算复本（ADR 0029 决定 6）。非 r2v 没有参考素材，
    不写预算文件，清单也不显示预算（否则每段都会凭空多一句「未挑图」）。

    ``film_end`` 的说明与调用方同义：单段 fl2v 的尾帧就是这一段的终点画面（#147）。
    """
    from ..director.planning import (plan_path, render_plan_listing,
                                     write_asset_budget, write_plan_json)

    session_dir = Path(session_dir)
    plan_file = write_plan_json(plan_path(session_dir), plan)
    budget_file = write_asset_budget(session_dir, budget) if mode == "r2v" else None
    print()
    print(render_plan_listing(plan, film_end=film_end,
                              budget=budget if mode == "r2v" else None))
    print(f"\n段表已写入：{plan_file}")
    if budget_file is not None:
        print(f"素材预算已写入：{budget_file}")
    return plan_file, budget_file


def store_director_still(source: Path, generation_dir: Path, stem: str, *,
                         segment: int | None = 1) -> Path:
    """把用户交回的图复制到导演包认的落点 ``director/assets/NN/<stem><ext>``。

    为什么由程序复制、而不是让用户自己放到那个目录（2026-10-30 用户现场）：用户的
    习惯是「生成完把路径粘给程序」，让他去翻 ``output/sessions/<主题>/GENxxx/director/
    assets/01/`` 这种内部路径，等于把系统约定转嫁给他。

    ``segment=None`` 落 **assets 根**（全片尾帧 ``film-end``）：它不属于任何一段的
    段目录——写进 ``assets/01/`` 就会被打包成第 1 段的终点画面（2026-10-30 实跑：
    4 段各 5 秒的片子，第 1 段被要求从雾中远景直接跑到 20 秒处的隧道深处）。

    同一 stem 的**其他扩展名要清掉**：打包器的取图口（``pack._find_still``）按扩展名
    偏好取第一张，留着上次的 ``start.png`` 会让这次交的 ``start.jpg`` 静默失效（用户
    以为换了图）。

    从 ``wizard.py`` 搬来（#160）：那是 6000 行的编排文件，``test_wizard_size.py``
    要求新逻辑要么搬出去、要么用等量搬迁换；本函数只做文件落点，不依赖向导状态。
    """
    import shutil

    from ..director.pack import ASSETS_DIRNAME, DIRECTOR_DIRNAME

    assets_root = Path(generation_dir) / DIRECTOR_DIRNAME / ASSETS_DIRNAME
    folder = assets_root if segment is None else assets_root / f"{segment:02d}"
    folder.mkdir(parents=True, exist_ok=True)
    suffix = Path(source).suffix.lower() or ".png"
    target = folder / f"{stem}{suffix}"
    shutil.copy2(source, target)
    for ext in (".png", ".jpg", ".jpeg", ".webp"):
        other = folder / f"{stem}{ext}"
        if other != target and other.exists():
            other.unlink()
    return target


def prepare_film_end_description(session_dir: str | Path) -> tuple[bool, str]:
    """取全片尾帧的画面描述（#160）：返回 ``(ok, description)``。

    没有 ``film-end`` 图时返回 ``(True, "")``——那张图不是每条路线都有。读图失败
    打印图路径与病因并返回 ``(False, "")``：**与坏 sidecar 的宽容分开**，坏 sidecar
    可以重读，读不出图就必须停（不带着「终点全靠猜」的段词往下跑）。

    收集与规划前两个调用点共用这一处文案：各写一套时，同一场失败会在两处说成
    两件事（「图没有就位」/「读图失败」），用户分不清要不要重交图。
    """
    from ..director.film_end import get_film_end_description
    from ..director.pack import film_end_still

    if film_end_still(session_dir) is None:
        return True, ""
    try:
        return True, get_film_end_description(session_dir) or ""
    except (RuntimeError, OSError) as exc:
        print(f"[错误] 全片尾帧读图失败：{exc}")
        return False, ""


class DirectorStillFacts(NamedTuple):
    """规划/写段期两张用户交来的图的事实（#323）。

    合成一个出口的理由与 ``write_context_blocks`` 相同：``ui/wizard.py`` 受
    ``tests/test_wizard_size.py`` 的体量闸管着，而「取描述 → 判失败 → 判盘面」在三处
    调用点上逐处展开会让 6000 行的编排文件继续变长；收进这里之后每个调用点只多
    一两行。

    ``film_end`` / ``start_frame`` 是**盘上真有这张图**的判断，与描述分开放：给图行
    按「图在不在」写、事实块按描述写，两者不同源就会写出指向空气的段词（#322 的
    注入判据同理）。
    """

    ok: bool
    film_end: bool
    film_end_description: str
    start_frame: bool
    start_frame_description: str


def prepare_opening_still_description(
        session_dir: str | Path, mode: str) -> tuple[bool, str]:
    """取开场首帧图的画面描述（#323）：返回 ``(ok, description)``。

    为什么与 ``prepare_film_end_description`` 并排、同形状：三处调用点（收图之后、
    规划前、写段前）会撞上**同一场**失败（视觉模型挂了），三处各写一套文案时用户会
    看到三种说法，分不清这次读图到底卡在哪、要不要重交图。收集处与规划/写段处的
    失败文案因此都由这里统一说。

    没有这张图时返回 ``(True, "")``（不是每条路线都有这张图）；``mode`` 判据问给图
    契约（``needs_first_frame_image``），不写模式字面量——``t2v``/``r2v`` 的开场段
    没有首帧槽，问它会自动排除，将来加模式也不用回来改这里。读图失败打印图路径与
    病因并返回 ``(False, "")``：绝不带着「起点画面全靠猜」的段词往下跑（与全片尾帧
    同一条口径）。
    """
    from ..director.frames import FIRST_SEGMENT_TYPE, needs_first_frame_image
    from ..director.opening_still import get_opening_still_description

    if not needs_first_frame_image(FIRST_SEGMENT_TYPE, mode):
        return True, ""
    try:
        return True, get_opening_still_description(session_dir) or ""
    except (RuntimeError, OSError) as exc:
        print(f"[错误] 开场首帧图读图失败：{exc}")
        return False, ""


def read_opening_still_after_collect(session_dir: str | Path, mode: str) -> bool:
    """收图之后**立刻**读一次并打印读图结果（#323）；读不出来返回 ``False`` 停下。

    为什么要在收图那一刻读：用户刚把图交进来，当场看到「这张图被读懂成了什么」才能
    判断要不要换一张，而不是跑完视频才发现图与预期不符。打印形态与全片尾帧同一处
    手感（``[已读图] …``）。没有这张图时什么都不打印（``prepare_*`` 返回空描述）。
    """
    ok, description = prepare_opening_still_description(session_dir, mode)
    if not ok:
        print("  已停下：没有继续规划、没有写段、也没有落描述 sidecar。"
              "修好视觉模型后重跑向导会回到这一步。")
        return False
    if description:
        print(f"[已读图] 开场首帧画面：{description}")
    return True


def store_and_read_director_stills(
        session_dir: str | Path, mode: str, *, first: Path | None,
        last: Path | None) -> bool:
    """收起首帧/尾帧并**当场读图**：返回 ``False`` 表示该停在收图之后。

    从 ``ui.wizard._collect_director_opening_still`` 搬来（#323）：本函数只做文件
    落点与读图，不依赖向导状态，而 ``wizard.py`` 受 ``tests/test_wizard_size.py``
    的体量闸管着。文案与次序逐字保留：

    - 首帧必填（``None`` 在这里拦下，不把 ``None`` 传进落点）；
    - 首帧落在 ``assets/01/start``，收下后**立刻**读一次（#323）；
    - 全片尾帧落 assets 根（``film-end``），不落 ``assets/01/end``：后者是**第 1 段**
      的终点画面。多段时这张图由用户在导演台的**最后一段**尾帧槽里挂；单段时打包器
      会把它当作这一段的终点（``pack._plan_media``）。收下后同样读一次（#160）。

    任一读图失败都返回 ``False``（病因已打印），调用方停下——续跑跳过本步时，规划前
    还会经 ``prepare_director_still_facts`` 补读。
    """
    from ..director.pack import FILM_END_STEM, START_STEM

    # 落点口（``store_director_still``）只收 ``Path``，这里归一一次。
    session_dir = Path(session_dir)
    if first is None:
        print("[错误] 首帧路径为空。")
        return False
    target = store_director_still(first, session_dir, START_STEM)
    print(f"[已就位] 首帧：{target}")
    if not read_opening_still_after_collect(session_dir, mode):
        return False
    if last is not None:
        film_end = store_director_still(
            last, session_dir, FILM_END_STEM, segment=None)
        print(f"[已就位] 全片尾帧（最后一段的终点画面）：{film_end}")
        ok, description = prepare_film_end_description(session_dir)
        if not ok:
            print("  已停下：没有继续规划、没有写段、也没有落描述 sidecar。"
                  "修好视觉模型后重跑向导会回到这一步。")
            return False
        if description:
            print(f"[已读图] 全片终点画面：{description}")
    return True


def prepare_director_still_facts(
        session_dir: str | Path, mode: str) -> DirectorStillFacts:
    """一次取齐规划/写段要用的两张图的画面事实（#323 起开场首帧也在这里）。

    ``ok`` 为 ``False`` 表示有一张图的读图失败了（病因与图路径已由对应的
    ``prepare_*_description`` 打印）——调用方停在自己那一步之前并打**自己的**停语：
    规划前与改词前的停语不同（一个是不做段规划、一个是本轮改词不执行），不该由这里
    替它们说。失败时其余字段一律空/假，调用方据此不用再判。
    """
    from ..director.pack import film_end_still, start_still

    ok, film_end_description = prepare_film_end_description(session_dir)
    if not ok:
        return DirectorStillFacts(False, False, "", False, "")
    ok, start_frame_description = prepare_opening_still_description(session_dir, mode)
    if not ok:
        return DirectorStillFacts(False, False, "", False, "")
    return DirectorStillFacts(
        ok=True,
        # 全片尾帧只在 fl2v 有；「盘上真有」与描述分开判，理由见类 docstring。
        film_end=mode == "fl2v" and film_end_still(session_dir) is not None,
        film_end_description=film_end_description,
        start_frame=start_still(session_dir, position=1) is not None,
        start_frame_description=start_frame_description,
    )


def build_director_llm(purpose: str = "写终点画面提示词"):
    """陪跑里按需建模型（写尾帧词或改词时才用得上）。失败不静默。

    ``purpose`` 由调用方给：同一份构造在「尾帧词」与「改词」两处失败时要说清各自
    卡住了什么，否则用户只看到一句与当前动作无关的病因。
    """
    from ..model_factory import build_chat_model

    try:
        return build_chat_model()
    except Exception as exc:  # noqa: BLE001 - 病因原样交回，别让建客户端失败伪装成别的问题
        # 不写具体段号：这里拿不到「轮到哪一段」，写死「第 N 段」会变成一句永远
        # 不替换的模板（复审 H4）。
        print(f"[错误] 模型构造失败（{exc}）：接下来这一段要{purpose}，客户端没建起来。")
        return None


def ask_pack_settings(
    session, *, ask: Callable[[str], str], save: Callable[..., object],
) -> None:
    """进导演台流程前问一次打包参数并落 Brief（issue #104，设计 §4）。

    从 ``wizard.py`` 搬来（#161）：原实现只做三问答与一次落盘，不依赖向导的其他
    状态。``ask`` / ``save`` 由调用方注入（``_prompt`` 与 ``save_session``），
    本模块因此不反向 import ``wizard``。

    三问答都只问**一次**：节点 id 与缓存名决定段缓存目录名（插件口径是
    ``<缓存名>_<节点id>``，#105 靠它找 ``seg_XXXX.frames.mkv``），引导模式是包内
    ``continuityMode``。Brief 里已有节点 id 就整组沿用——续跑反复追问同一件事既是
    噪音，也容易被误答覆盖。

    节点 id **没有默认值**：缺了 #105 会在用户的 ComfyUI 输出目录里找不到任何缓存，
    而那时的报错只能是一句「目录不存在」。空输入重问，不是静默留空。
    """
    brief = session.brief
    from ..director import cache as director_cache

    if brief.director_node_id:
        # 已有答案整组沿用，但要**归一一次**（2026-10-30 真实会话）：旧值可能是照抄
        # 节点标题的 `#365`，而插件写盘的目录用 `365`——不归一，陪跑会在这条会话上
        # 永远找不到缓存。归一不重问、不改用户真正的 id。
        try:
            normalized = director_cache.validate_node_id(brief.director_node_id)
        except ValueError:
            normalized = ""
        if normalized and normalized != brief.director_node_id:
            brief.director_node_id = normalized
            save(session.directory, brief, session.stage_state,
                 status=session.status)
        return
    node_id = ""
    while not node_id:
        raw = ask("导演台节点的 unique_id（ComfyUI 里选中导演台节点后显示的 id，必填）：")
        # 合法性判据只住 ``director/cache.py``（复审 H3）：向导原先自写
        # ``isalnum() or "_.-"``，恰好放过 ``..``——用户答完三问、进陪跑才在
        # ``cache_dir`` 炸。这里提前问，报错就在同一道题上。
        try:
            node_id = director_cache.validate_node_id(raw)
        except ValueError as exc:
            print(f"[提示] {exc}。")
            continue
    cache_name = ask(
        "段缓存名（导演台性能组的 cache_name；回车=空，缓存目录只用节点 id）：").strip()
    from ..director.pack import CONTINUITY_DEFAULT, CONTINUITY_MODES

    continuity = ""
    while continuity not in CONTINUITY_MODES:
        raw = ask(
            f"段间引导模式（{' / '.join(CONTINUITY_MODES)}；回车={CONTINUITY_DEFAULT}）："
        ).strip().lower()
        continuity = raw or CONTINUITY_DEFAULT
        if continuity not in CONTINUITY_MODES:
            print(f"[提示] 只能是 {' / '.join(CONTINUITY_MODES)}。")
    brief.director_node_id = node_id
    brief.director_cache_name = cache_name
    brief.director_continuity_mode = continuity
    # 立刻落盘：这三个答案决定了后面找缓存与写包的参数，重跑向导不该再问一遍。
    save(session.directory, brief, session.stage_state, status=session.status)


def prompt_existing_file(
    question: str, *, what: str, ask: Callable[[str], str],
) -> Path | None:
    """问一个**必须已存在**的文件路径；不存在就报错重问（从 wizard.py 搬来，#176）。

    返回 ``None`` ＝ 用户输入 q 停下——这不是失败：已通过的段留在账上，重跑向导回到
    这里。**不扫目录自动挑**：导出目录里往往躺着上一次的同名文件，自动挑中的那一个
    用户没看过，拿错成片/拿错包都不会有人发现。``ask`` 由调用方注入，本模块不反向
    依赖 ``wizard``。
    """
    while True:
        # 用户多半是从资源管理器「复制文件地址」粘过来的，会带一对引号。
        raw = ask(question).strip().strip('"').strip("'")
        if raw.lower() in ("q", "quit"):
            return None
        if not raw:
            print(f"[错误] {what}路径不能为空。")
            continue
        path = Path(raw)
        if not path.is_file():
            print(f"[错误] {what}不存在：{path}")
            print("  请确认路径后重输（项目不会自动搜目录：猜错的文件发现不了）。")
            continue
        return path


def check_segment_aspect(
    *, mode: str, segment_index: int, segment_type: str, video: Path,
    directory: Path, canvases, brief, stage_state, ask: Callable[[str], str],
    save: Callable[[], object], session_created_at: str | None,
) -> str:
    """逐段贴回后的画幅／宽高核对：返回 ``"pass" | "retry" | "stop"``（#158／#176）。

    为什么把「拒绝之后怎么办」也收在这里：i2v／fl2v 与 r2v 的**处置不同**——前者
    拒绝落账后回到同一个等视频问答（``retry``），后者比例超差或拒绝尺寸确认就
    停下来（``stop``，节点尺寸不改，重贴几次都一样）。把这段分支留在 6000 行的
    ``wizard.py`` 里，两处调用点会各写一套，正是「同一契约两处口径」的温床。

    ``session_created_at``（#179）由调用方从会话状态取出传进来：旧会话（创建于
    成片画幅问答落地之前）里那一格是关键帧画布，不能当画幅比。本函数不读盘。
    """
    from ..brief_parser import DIRECTOR_ASSET_MODES
    from ..director.aspect_flow import output_aspect_verdict

    ok, note = output_aspect_verdict(
        mode=mode, segment_index=segment_index, segment_type=segment_type,
        video=video, directory=directory, canvases=canvases,
        brief=brief, ask=ask, stage_state=stage_state,
        session_created_at=session_created_at)
    if note:
        print(note)
    if not ok:
        return "stop" if mode in DIRECTOR_ASSET_MODES else "retry"
    if mode in DIRECTOR_ASSET_MODES:
        # r2v 的尺寸确认一发生就落盘：最终交付再核对时同一组数值不再问。
        save()
    return "pass"


def confirm_final_film_aspect(
    *, mode: str, film: Path, brief, stage_state, ask: Callable[[str], str],
    save: Callable[[], object], session_created_at: str | None,
) -> bool:
    """最终交付前的成片宽高核对（#176）：``True``＝继续打包，``False``＝停在打包之前。

    ``session_created_at``（#179）与逐段贴回同源：旧会话放行，新会话行为不变。
    """
    from ..brief_parser import DIRECTOR_ASSET_MODES
    from ..director.aspect_flow import final_film_verdict

    ok, note = final_film_verdict(
        mode=mode, video=film, brief=brief, stage_state=stage_state, ask=ask,
        session_created_at=session_created_at)
    if note:
        print(note)
    if not ok:
        print("\n⚠ 停在交付前（成片宽高）：没有归档最终包，也没有置完成；"
              "改导演台节点 width/height 后重新导出成片，重跑向导会回到这里。")
        return False
    if mode in DIRECTOR_ASSET_MODES:
        save()
    return True


def confirm_aspect(
    mode: str, *, frame_canvas: tuple[int, int], declared: tuple[int, int],
    ask: Callable[[str], str],
) -> bool:
    """段规划前的画幅预检（#158）：返回 ``True`` 继续、``False`` 停下。

    文案与判定都在 ``director.aspect_flow``（与贴回核对共用一份判据）；这里只负责
    把话打给用户并等一个**显式回车**——不回车不往下走，``q`` 停下。``ask`` 由调用方
    注入（向导的 ``_prompt``），测试因此可以按问题文本作答。
    """
    from ..director.aspect_flow import precheck_message

    message = precheck_message(mode, frame_canvas=frame_canvas, declared=declared)
    if message is None:
        return True
    print(message)
    # 问题里带「画幅」：流程测试按问题文本作答（本仓纪律），换一种问法也要匹配得上。
    while True:
        answer = ask("画幅比例超差：回车继续（我知悉锚点会被裁）；q 停下：")
        # 只有**空回车**是放行口：任何非 q 输入都继续，等于把「n／乱码」当成同意
        # （#157 复审）——用户按下回车那一刻才表示「我知道会裁，仍要往下走」。
        if not answer.strip():
            return True
        if answer.strip().lower() in ("q", "quit"):
            return False
        print("回车＝继续；q＝停下（其它输入不生效）。")

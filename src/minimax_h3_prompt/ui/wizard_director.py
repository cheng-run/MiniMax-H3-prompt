"""导演台陪跑用到的向导小件（issue #158）：模型构造与画幅预检。

与显示小件（``wizard_display.py``）的分工：那边只拼字符串；这里要建客户端、要问
一句并等回车，属于「编排的边角」。放在这里是为了让 6000 行的 ``wizard.py`` 不再增长
（``tests/test_wizard_size.py`` 的闸）；问答入口由调用方注入（``ask``），所以本模块
不依赖 ``wizard``、也不会形成环。
"""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path


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
    save: Callable[[], object],
) -> str:
    """逐段贴回后的画幅／宽高核对：返回 ``"pass" | "retry" | "stop"``（#158／#176）。

    为什么把「拒绝之后怎么办」也收在这里：i2v／fl2v 与 r2v 的**处置不同**——前者
    拒绝落账后回到同一个等视频问答（``retry``），后者比例超差或拒绝尺寸确认就
    停下来（``stop``，节点尺寸不改，重贴几次都一样）。把这段分支留在 6000 行的
    ``wizard.py`` 里，两处调用点会各写一套，正是「同一契约两处口径」的温床。
    """
    from ..brief_parser import DIRECTOR_ASSET_MODES
    from ..director.aspect_flow import output_aspect_verdict

    ok, note = output_aspect_verdict(
        mode=mode, segment_index=segment_index, segment_type=segment_type,
        video=video, directory=directory, canvases=canvases,
        brief=brief, ask=ask, stage_state=stage_state)
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
    save: Callable[[], object],
) -> bool:
    """最终交付前的成片宽高核对（#176）：``True``＝继续打包，``False``＝停在打包之前。"""
    from ..brief_parser import DIRECTOR_ASSET_MODES
    from ..director.aspect_flow import final_film_verdict

    ok, note = final_film_verdict(
        mode=mode, video=film, brief=brief, stage_state=stage_state, ask=ask)
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

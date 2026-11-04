"""导演台陪跑用到的向导小件（issue #158）：模型构造与画幅预检。

与 ``wizard_display.py`` 的分工：那边只拼字符串；这里要建客户端、要问一句并等回车，
属于「编排的边角」。放在这里是为了让 6000 行的 ``wizard.py`` 不再增长
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

    同一 stem 的**其他扩展名要清掉**：``pack._find_still`` 按扩展名偏好取第一张，
    留着上次的 ``start.png`` 会让这次交的 ``start.jpg`` 静默失效（用户以为换了图）。

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
    answer = ask("画幅比例超差：回车继续（我知悉锚点会被裁）；q 停下：")
    return answer.strip().lower() not in ("q", "quit")

"""导演台陪跑用到的向导小件（issue #158）：模型构造与画幅预检。

与 ``wizard_display.py`` 的分工：那边只拼字符串；这里要建客户端、要问一句并等回车，
属于「编排的边角」。放在这里是为了让 6000 行的 ``wizard.py`` 不再增长
（``tests/test_wizard_size.py`` 的闸）；问答入口由调用方注入（``ask``），所以本模块
不依赖 ``wizard``、也不会形成环。
"""
from __future__ import annotations

from collections.abc import Callable


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

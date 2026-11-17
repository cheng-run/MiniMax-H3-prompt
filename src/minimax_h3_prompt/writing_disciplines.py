"""三族写作纪律的唯一可写来源（#312）。

机位速度、时间窗外剧情禁区、禁止指代前文这三族纪律原先散在各条写段路径的提示词里，
一处一份就会各自漂。本模块只放**文本、不放逻辑**：全部写段路径（导演台、首尾帧、
多参等）都从这里取值，改一处全仓同时生效。

占位符机制（替换发生在 ``director/prompts.py::load_instruction``）只服务导演台的
指令文件：那些模板里写 ``{{FAMILY_DISCIPLINE}}``，加载时换成这里的常量。
"""

CAMERA_SPEED_DISCIPLINE = "**机位速度按剧情选**：静场、克制段落才用 `Static Shot`；有动作的段写完整的「运动类型 + 幅度 + 速度」自然句，不要把「静态/慢速中景」当默认——全片默认静态会把动作摊平，成片像慢放"

FUTURE_EVENTS_TITLE = "【时间窗外剧情禁区】以下事件属于其他视频段，本段绝对不能出现其中任何一个："

FUTURE_EVENTS_DISCIPLINE = "其他镜头的事件、构图与机位一个字都不能写进正文"

ANAPHORA_DISCIPLINE = "**禁止指代前文的措辞**（as mentioned before / as described earlier / the previous segment / 如前所述 等）：每段是独立生成，**模型无跨段记忆**，此类措辞是无效信息；被指代的内容要么改写成本段自足的表述，要么不写"

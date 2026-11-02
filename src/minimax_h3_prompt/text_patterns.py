"""全仓共享的文本判据（2026-11-02 体检 §3.3，P0-4）。

为什么要集中：同一语义在四个模块各写一份时，改一处漏一处不会报错；本仓已经出现过
**同名不同义**——``segment_prompts._BARE_TIMESTAMP_LINE`` 不允前导空白且捕获三组，
``h3_validator._BARE_TIMESTAMP_LINE`` 允许前导空白且不捕获。两个都叫「裸时间戳行」，
语义却不同，下一次「顺手统一」就会静默改掉其中一条路径。

时间戳在这里分成四种语义，**刻意不合并**：

1. ``AT_TIMESTAMP``：行内时间戳，用来算秒（不接受逗号）；
2. ``LEADING_TIMESTAMP``：行首时间戳，用来剥前缀（允许逗号与尾随空白，大小写不敏感）；
3. ``TIMESTAMP_ONLY_LINE``：判断「这一行只有时间戳」（允许前导空白，不捕获）；
4. ``INLINE_TIMESTAMP_STRIP``：把句中 ``At 00:01.000,`` 剥掉（可选逗号与尾随空白）。

纯正则、无 I/O；任何模块都可以 import 它而不拉起重依赖。
"""
from __future__ import annotations

import re

# SHA-256 十六进制摘要：资产账、项目模型、任务包、生成侧共用一条判据。
SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")

# 提示词里的标签：镜头号、参考图号、主体号。三处解析（写词、校验、打包）必须同一口径，
# 否则「校验器认、打包器不认」会让同一段文本在两个环节得到不同编号。
SHOT_TAG = re.compile(r"\[Shot\s+(\d+)\]")
PICTURE_TAG = re.compile(r"<Picture\s+(\d+)>")
SUBJECT_TAG = re.compile(r"<Subject\s+(\d+)>")

# 行内时间戳：``At 00:03.750`` → (00, 03, 750)。不接受逗号——带逗号的是行首/剥离形态。
AT_TIMESTAMP = re.compile(r"At\s+(\d{2}):(\d{2})\.(\d{3})")

# 行首时间戳（剥前缀）：允许逗号与尾随空白；大小写不敏感沿用原实现。
LEADING_TIMESTAMP = re.compile(
    r"^At\s+(\d{2}):(\d{2})\.(\d{3}),?\s*", re.IGNORECASE)

# 「整行只有时间戳」：允许前导空白，不捕获（校验器用它判「缺时间」）。
TIMESTAMP_ONLY_LINE = re.compile(r"^\s*At\s+\d{2}:\d{2}\.\d{3}")

# 句中时间戳剥离：可选逗号与尾随空白（清洗正文用）。
INLINE_TIMESTAMP_STRIP = re.compile(r"At\s+\d{2}:\d{2}\.\d{3},?\s*")

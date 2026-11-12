"""前端不许抄产物文件名白名单（ADR 0034 / workbench.md 不变量）。

为什么需要：D4 复审在 `ArtifactsTab.vue`、`ReviewTab.vue`、`FrameSubmissionCard.vue`
各抓到一份硬编码的文件名清单（2026-11-11）。后端登记表改名或新增产物时，前端会
静默少显示一件而不报错；修复后白名单集中到 `artifactKinds.ts`，但没有东西阻止
下一个组件再把名字抄回来。这条守卫把「按名字枚举」变成能红的判据。

判据取**契约里的 kind**（`ArtifactInfo.kind`）与后端的产物常量（名字只从登记表
派生），扫描范围是 `frontend/src` 的生产代码：测试夹具（`*.spec.ts`）可以写具体
文件名，`artifactKinds.ts` 是唯一获准持有它们的地方（帧提示词的角色绑定）。
"""
from __future__ import annotations

import re
from pathlib import Path

from minimax_h3_prompt.workbench_store import (
    ARTIFACT_FIRST_FRAME_PROMPT,
    ARTIFACT_LAST_FRAME_PROMPT,
    ARTIFACT_SCRIPT,
    ARTIFACT_SHOT_TABLE,
    ARTIFACT_SUMMARY_JSON,
    ARTIFACT_SUMMARY_MARKDOWN,
    ARTIFACT_VIDEO_PROMPT,
)

ROOT = Path(__file__).resolve().parents[1]
FRONTEND_SRC = ROOT / "frontend" / "src"
# 唯一获准写产物文件名的生产文件：帧提示词的首/尾角色绑定 + kind 标签映射。
ALLOWED = {FRONTEND_SRC / "features" / "sessions" / "artifactKinds.ts"}

ARTIFACT_NAMES = (
    ARTIFACT_SCRIPT,
    ARTIFACT_SHOT_TABLE,
    ARTIFACT_FIRST_FRAME_PROMPT,
    ARTIFACT_LAST_FRAME_PROMPT,
    ARTIFACT_VIDEO_PROMPT,
    ARTIFACT_SUMMARY_MARKDOWN,
    ARTIFACT_SUMMARY_JSON,
)


def _production_sources() -> list[Path]:
    files = [
        *FRONTEND_SRC.rglob("*.ts"),
        *FRONTEND_SRC.rglob("*.vue"),
    ]
    return [
        path
        for path in files
        if not path.name.endswith(".spec.ts") and path not in ALLOWED
    ]


def test_frontend_does_not_hardcode_artifact_names():
    """生产代码里出现 ``'script.md'`` 这类字面量即失败（按 kind 派生才对）。"""
    sources = _production_sources()
    assert len(sources) > 10, f"只扫到 {len(sources)} 个前端源文件，守卫可能在空转"

    hits: list[str] = []
    for path in sources:
        text = path.read_text(encoding="utf-8")
        for name in ARTIFACT_NAMES:
            pattern = re.compile(rf"['\"`]{re.escape(name)}['\"`]")
            for match in pattern.finditer(text):
                line = text.count("\n", 0, match.start()) + 1
                hits.append(f"{path.relative_to(ROOT).as_posix()}:{line} → {name}")

    assert not hits, (
        "前端在生产代码里写死了产物文件名（应经 ArtifactInfo.kind 派生，"
        "名字只住在 artifactKinds.ts）：\n  " + "\n  ".join(hits[:20]))

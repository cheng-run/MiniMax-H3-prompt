"""载荷清单在**真实产物**上的回归（无素材时整体 SKIP）。

合成载荷钉住契约，这一条钉住「契约在真产物上成立」——本工具的整个存在理由就是
真产物（issue #18 的三次事故全发生在真产物上，且从文件名里看不出来）。

路径按 conftest 的约定解析：``H3_COMFY_OUTPUT`` 优先，未设时用本机默认值。
"""
from __future__ import annotations

import warnings

import pytest

from scripts.loadout_manifest import extract_loadout, resolve_refs
from tests.conftest import comfy_output_dir

COMFY_OUTPUT = comfy_output_dir()          # <ComfyUI>/output/视频/MiniMax-H3/<日期>
COMFY_ROOT = COMFY_OUTPUT.parents[3]       # <ComfyUI>
H3_OUTPUT_ROOT = COMFY_OUTPUT.parents[0]   # <ComfyUI>/output/视频/MiniMax-H3

pytestmark = pytest.mark.skipif(
    not (COMFY_ROOT / "models").is_dir(),
    reason=f"需要本机 ComfyUI：{COMFY_ROOT}（换机器请设 H3_COMFY_OUTPUT）",
)


def _artifacts() -> list:
    return sorted(H3_OUTPUT_ROOT.glob("*/*.png"))


def test_real_artifacts_all_yield_a_loadout():
    """每份真产物都必须解析出载荷——解析不出就是本工具的失败，不是产物的。"""
    artifacts = _artifacts()
    if not artifacts:
        pytest.skip(f"没有产物：{H3_OUTPUT_ROOT}")
    broken = [(p.name, lo.error) for p in artifacts
              if (lo := extract_loadout(p)).error is not None]
    assert not broken, f"这些产物读不出载荷：{broken}"


# 有样本的步数集合（2026-10-31 盘点本机真实产物：4/8/14/24/32 都有）。
# 只用于「已知样本」的提示；**不硬阻断**未知值——用户换模型/LoRA 后步数会变，
# 硬集合会让基线常红，真正的新回归反而被淹没（记忆 `h3-gen-side-deploy-change-2026-10`，
# 以及「无样本常量宁可警告不硬阻断」的既有裁定）。
KNOWN_STEPS = (4, 8, 14, 24, 32)


def test_real_artifacts_carry_a_sampling_config_and_an_unet():
    """采样步数与 UNET 是每份产物都该有的：前者决定闪动分档，后者指认仪器。

    步数：必须是正整数；未知值只发 warning（模型/LoRA 换代属正常变化），不判失败。
    UNET：仍是硬断言——解析不出仪器是工具本身的问题，换模型不该把它藏掉。
    """
    artifacts = _artifacts()
    if not artifacts:
        pytest.skip(f"没有产物：{H3_OUTPUT_ROOT}")
    unknown: list[str] = []
    for path in artifacts:
        loadout = extract_loadout(path)
        steps = loadout.sampling.steps
        assert isinstance(steps, int) and steps > 0, f"{path.name} 步数缺失：{steps!r}"
        if steps not in KNOWN_STEPS:
            unknown.append(f"{path.name}={steps}")
        kinds = {ref.kind for ref in loadout.refs}
        assert "unet" in kinds, f"{path.name} 没有 UNET 引用：{kinds}"
    if unknown:
        warnings.warn(
            "真实产物出现未登记的采样步数（先确认是不是有意换模型/LoRA；"
            f"确认后把值加进 KNOWN_STEPS）：{'、'.join(unknown)}",
            stacklevel=2,
        )


def test_real_report_flags_every_unresolved_ref():
    """不变量：报 ok 当且仅当没有任何引用解析不到——不许一边解析不到一边说通过。

    2026-09-23 的 LoRA 换版让 ``00005`` 的 ``4step_v1.1_768p`` 解析不到，本用例
    不钉具体是哪一份（那是机器状态），只钉这条不变量。
    """
    artifacts = _artifacts()
    if not artifacts:
        pytest.skip(f"没有产物：{H3_OUTPUT_ROOT}")
    loadouts = [extract_loadout(p) for p in artifacts]
    report = resolve_refs(loadouts, COMFY_ROOT, want_hash=False)
    assert not report.root_missing
    assert report.ok == (not report.missing)
    if not report.ok:
        # 失效项必须被显式列出，不能只体现在一个布尔值上
        assert report.missing, "报告不 ok 却没有任何缺失项"
        assert all(item.name for item in report.missing)

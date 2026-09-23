"""载荷清单在**真实产物**上的回归（无素材时整体 SKIP）。

合成载荷钉住契约，这一条钉住「契约在真产物上成立」——本工具的整个存在理由就是
真产物（issue #18 的三次事故全发生在真产物上，且从文件名里看不出来）。

路径按 conftest 的约定解析：``H3_COMFY_OUTPUT`` 优先，未设时用本机默认值。
"""
from __future__ import annotations

import pytest

from tools.loadout_manifest import extract_loadout, resolve_refs
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


def test_real_artifacts_carry_a_sampling_config_and_an_unet():
    """采样步数与 UNET 是每份产物都该有的：前者决定闪动分档，后者指认仪器。"""
    artifacts = _artifacts()
    if not artifacts:
        pytest.skip(f"没有产物：{H3_OUTPUT_ROOT}")
    for path in artifacts:
        loadout = extract_loadout(path)
        assert loadout.sampling.steps in (4, 8), f"{path.name} 步数异常：{loadout.sampling.steps}"
        kinds = {ref.kind for ref in loadout.refs}
        assert "unet" in kinds, f"{path.name} 没有 UNET 引用：{kinds}"


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

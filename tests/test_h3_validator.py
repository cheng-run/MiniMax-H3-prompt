"""h3_validator 单元测试。

合法基准取自官方规范（base-en.txt Case 1、ref-en.txt 完整示例结构）；
违规样例为针对性构造。
2026-09-22 官方格式迁移（issue #2）：自创结构 GLOBAL_LOCK/BRIDGE_FROM/END_HOOK/
防波纹咒语转为反向断言；新增 I2VA 出场节拍距段尾 ≥1s、单 Shot 默认；
正负样本 fixture 钉回归（tests/fixtures/）。
"""
from pathlib import Path

import pytest

from minimax_h3_prompt.tools.h3_validator import (
    validate_base,
    validate_prompt,
    validate_ref,
)

FIXTURES = Path(__file__).parent / "fixtures"
LEGACY_SHOT01 = (FIXTURES / "legacy_shot01.md").read_text(encoding="utf-8")
OFFICIAL_SHOT01_I2VA = (FIXTURES / "official_shot01_i2va.md").read_text(encoding="utf-8")

REF_META = [
    (1, "白发仙师", "白发老妪，玄色长袍，手持拂尘"),
    (2, "青衫青年", "束发青年，青色长衫，腰悬长剑"),
    (3, "竹林", "雾气弥漫的竹林小径"),
]

VALID_BASE_T2VA = """integrated_multimodal_description: [Shot 1] Live-action, cinematic, a medium-wide shot frames a baker opening the shutters of a small street bakery before sunrise. The camera pushes in with small amplitude at slow speed as the middle-aged baker with a calm, slightly raspy voice (S1) places a fresh loaf on the wooden counter and says: <d>[English] First batch of the morning.</d> [Shot 2] At 00:05.000, the camera cuts to a close-up of steam rising from the sliced bread while the baker's final words carry over from the previous shot.

overall_soundscape: Wooden shutters scrape open over a quiet street as trays clink softly inside the bakery. The doorbell rings once, followed by light footsteps and the crisp sound of bread being sliced.

non_diegetic_music: A soft acoustic-guitar pattern at a moderate tempo, joined by sparse upright-bass notes and a gentle fade at the end.
"""

VALID_REF = """subject_definitions:
<Subject 1> is the white-haired old woman in <Picture 1>, in a dark robe holding a horsetail whisk.
<Subject 2> is the young man in <Picture 2>, in a green robe with a sword at his waist.
<Subject 3> is the misty bamboo forest in <Picture 3>.

summary:
[reference generation] The target video shows <Subject 2> arriving at <Subject 3> and bowing to <Subject 1>.

retention_analysis:
<Subject 1> (appears in [Shot 1], [Shot 2]): fully_preserved - the dark robe, white hair and whisk are retained.
<Subject 2> (appears in [Shot 1], [Shot 2]): fully_preserved - the green robe and sword are retained.
<Subject 3> (appears in [Shot 1]): partially_preserved - the mist and bamboo are retained.

detailed_description:
The target video is in a cinematic wuxia style with soft misty lighting.
[Shot 1] A wide shot frames <Subject 3>, the misty bamboo forest in <Picture 3>, with <Subject 1>, the white-haired old woman in the dark robe from <Picture 1>, standing at the center raising one hand slowly. <Subject 2>, the green-robed young man from <Picture 2>, descends from the sky and lands, tucks his sword, then bows deeply to <Subject 1> with both hands clasped. <Subject 1> (S1) says in a calm aged voice: <d>[Chinese] Rise.</d>
[Shot 2] At 00:04.000, the camera cuts to a close-up of <Subject 1>'s hand gesturing upward, and <Subject 2> (S2) straightens and looks up, bamboo leaves swaying behind them.

overall_soundscape:
Low bamboo rustling and distant mountain wind continue throughout the scene, with the soft thud of boots landing on moss.

non_diegetic_music:
A sparse guzheng pattern at a slow tempo with sustained low strings, fading out at the end.
"""


def errors_of(issues):
    return [i.code for i in issues if i.severity == "error"]


def codes(issues):
    return [i.code for i in issues]


def base_prompt(body: str) -> str:
    """把镜头正文包成合法 base 三段式结构（便于聚焦镜头检查）。"""
    return (f"integrated_multimodal_description: {body}\n\n"
            f"overall_soundscape: N/A\n\n"
            f"non_diegetic_music: N/A")


class TestValidFixtures:
    def test_valid_base_t2va(self):
        issues = validate_base(VALID_BASE_T2VA, duration=8.0, variant="T2VA")
        assert errors_of(issues) == []

    def test_valid_ref(self):
        issues = validate_ref(VALID_REF, duration=6.0, ref_meta=REF_META)
        assert errors_of(issues) == []


class TestShots:
    def test_first_shot_timestamp(self):
        text = "integrated_multimodal_description: [Shot 1] At 00:01.000 a cat jumps.\n\noverall_soundscape: N/A\n\nnon_diegetic_music: N/A"
        issues = validate_base(text, variant="T2VA")
        assert "FIRST_SHOT_TIMESTAMP" in errors_of(issues)

    def test_timestamp_order(self):
        text = base_prompt("[Shot 1] opening.\n[Shot 2] At 00:03.000 middle.\n[Shot 3] At 00:01.000 earlier.")
        issues = validate_base(text, duration=8.0)
        assert "TIMESTAMP_ORDER" in errors_of(issues)

    def test_timestamp_over_duration(self):
        text = base_prompt("[Shot 1] opening.\n[Shot 2] At 00:06.000 too late.")
        issues = validate_base(text, duration=5.0)
        assert "TIMESTAMP_OVER_DURATION" in errors_of(issues)

    def test_shot_no_timestamp(self):
        text = base_prompt("[Shot 1] opening.\n[Shot 2] cut.")
        issues = validate_base(text, duration=5.0)
        assert "SHOT_NO_TIMESTAMP" in errors_of(issues)


class TestDialogues:
    def test_dialog_unbalanced(self):
        text = "integrated_multimodal_description: [Shot 1] She says: <d>[English] Hi\n\noverall_soundscape: N/A\n\nnon_diegetic_music: N/A"
        issues = validate_base(text)
        assert "DIALOG_UNBALANCED" in errors_of(issues)


class TestSections:
    def test_ref_section_order(self):
        text = (
            "retention_analysis: x\n\nsummary: [reference generation] x\n\nsubject_definitions: x\n\n"
            "detailed_description: x\n\noverall_soundscape: x\n\nnon_diegetic_music: x"
        )
        issues = validate_ref(text)
        assert "REF_SECTION_ORDER" in errors_of(issues)

    def test_ref_section_missing(self):
        text = "subject_definitions: x\n\ndetailed_description: x"
        issues = validate_ref(text)
        assert "REF_SECTION_MISSING" in errors_of(issues)

    def test_summary_no_tasktype(self):
        text = VALID_REF.replace("summary:\n[reference generation]", "summary:\nThe target")
        issues = validate_ref(text, duration=6.0, ref_meta=REF_META)
        assert "SUMMARY_NO_TASKTYPE" in errors_of(issues)


class TestLabels:
    def test_picture_gap(self):
        text = (
            "subject_definitions:\n<Subject 1> is <Picture 2>.\n\nsummary:\n[reference generation] t.\n\n"
            "retention_analysis:\n<Subject 1>: fully_preserved - ok.\n\ndetailed_description:\n"
            "[Shot 1] The <Subject 1> in <Picture 2> moves.\n\noverall_soundscape: N/A\n\nnon_diegetic_music: N/A"
        )
        issues = validate_ref(text)
        assert "LABEL_GAP_PICTURE" in errors_of(issues)

    def test_picture_unused(self):
        ref_meta = REF_META + [(4, "第四张", "未被引用")]
        issues = validate_ref(VALID_REF, duration=6.0, ref_meta=ref_meta)
        assert "PICTURE_UNUSED" in errors_of(issues)

    def test_picture_undefined(self):
        text = VALID_REF.replace("<Picture 3>", "<Picture 5>")
        issues = validate_ref(text, duration=6.0, ref_meta=REF_META)
        assert "PICTURE_UNDEFINED" in errors_of(issues)


class TestVariants:
    def test_i2va_missing_instruction(self):
        text = "integrated_multimodal_description: [Shot 1] ...\n\noverall_soundscape: N/A\n\nnon_diegetic_music: N/A"
        issues = validate_base(text, variant="I2VA")
        assert "MISSING_ALIGN_INSTRUCTION" in errors_of(issues)

    def test_t2va_no_instruction_needed(self):
        text = "integrated_multimodal_description: [Shot 1] ...\n\noverall_soundscape: N/A\n\nnon_diegetic_music: N/A"
        issues = validate_base(text, variant="T2VA")
        assert "MISSING_ALIGN_INSTRUCTION" not in errors_of(issues)


class TestAlignInstruction:
    """帧变体首行指令必须逐字符符合官方模板（base-en.txt 2.1 / Case 3）。"""

    FL2VA_OK = (
        "How the reference pictures align with the target video — Picture 1 (from Shot 1) "
        "aligns with the 0.00-second mark of the target video; Picture 2 (from Shot 1) "
        "aligns with the 5.00-second mark of the target video.\n\n"
        "integrated_multimodal_description: [Shot 1] Live-action, cinematic, a cyclist begins "
        "in the position established by Picture 1.\n\n"
        "overall_soundscape: Rain falls steadily on the pavement.\n\nnon_diegetic_music: N/A"
    )

    def test_official_fl2va_case3_passes(self):
        issues = validate_base(self.FL2VA_OK, duration=5.0, variant="FL2VA")
        assert errors_of(issues) == []

    def test_wrong_template_is_error(self):
        # 用 I2VA 模板冒充 FL2VA 首行（历史真实产出犯过的错）
        text = self.FL2VA_OK.replace(
            "How the reference pictures align with the target video — Picture 1 (from Shot 1) "
            "aligns with the 0.00-second mark of the target video; Picture 2 (from Shot 1) "
            "aligns with the 5.00-second mark of the target video.",
            "For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.",
        )
        issues = validate_base(text, duration=5.0, variant="FL2VA")
        assert "ALIGN_INSTRUCTION_FORMAT" in errors_of(issues)

    def test_time_mismatch_is_error(self):
        text = self.FL2VA_OK.replace("the 5.00-second mark", "the 8.00-second mark")
        issues = validate_base(text, duration=5.0, variant="FL2VA")
        assert "ALIGN_TIME_MISMATCH" in errors_of(issues)

    def test_last_shot_mismatch_is_error(self):
        text = self.FL2VA_OK.replace(
            "[Shot 1] Live-action",
            "[Shot 1] opening. [Shot 2] At 00:03.000, the camera cuts to a close-up.",
        ).replace("(from Shot 1) aligns", "(from Shot 9) aligns")
        issues = validate_base(text, duration=5.0, variant="FL2VA")
        assert "ALIGN_LAST_SHOT_MISMATCH" in errors_of(issues)
        assert "FL2VA_MULTI_SHOT" in codes(issues)

    def test_i2va_first_shot_must_be_one(self):
        text = (
            "For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 2]) is fully referenced.\n\n"
            "integrated_multimodal_description: [Shot 1] opening. [Shot 2] At 00:02.000, cut.\n\n"
            "overall_soundscape: N/A\n\nnon_diegetic_music: N/A"
        )
        issues = validate_base(text, duration=5.0, variant="I2VA")
        assert "ALIGN_FIRST_SHOT_MISMATCH" in errors_of(issues)

    def test_blank_line_after_instruction_warns(self):
        text = (
            "How the reference pictures align with the target video — Picture 1 (from Shot 1) "
            "aligns with the 0.00-second mark of the target video; Picture 2 (from Shot 1) "
            "aligns with the 5.00-second mark of the target video.\n"
            "integrated_multimodal_description: [Shot 1] x\n\n"
            "overall_soundscape: N/A\n\nnon_diegetic_music: N/A"
        )
        issues = validate_base(text, duration=5.0, variant="FL2VA")
        assert "ALIGN_BLANK_LINE_MISSING" in codes(issues)


class TestSpeakers:
    def test_speaker_order_warning(self):
        text = "integrated_multimodal_description: [Shot 1] A man (S2) says: <d>[English] Hi.</d> Then (S1) answers.\n\noverall_soundscape: N/A\n\nnon_diegetic_music: N/A"
        issues = validate_base(text)
        assert "SPEAKER_ORDER" in codes(issues)


class TestDispatch:
    def test_validate_prompt_ref(self):
        issues = validate_prompt(VALID_REF, "ref", duration=6.0, ref_meta=REF_META)
        assert errors_of(issues) == []

    def test_validate_prompt_base(self):
        issues = validate_prompt(VALID_BASE_T2VA, "base", duration=8.0, variant="T2VA")
        assert errors_of(issues) == []


class TestLegacyBannedStructures:
    """官方格式迁移（issue #2）：自创结构出现即报 error（反向规则）。"""

    @pytest.mark.parametrize("marker_code", [
        "GLOBAL_LOCK_BANNED",
        "BRIDGE_FROM_BANNED",
        "END_HOOK_BANNED",
    ])
    def test_legacy_section_banned(self, marker_code):
        text = LEGACY_SHOT01 + "\n\noverall_soundscape: x\n\nnon_diegetic_music: N/A"
        issues = validate_base(text, duration=4.0)
        assert marker_code in errors_of(issues)

    def test_ripple_spell_banned(self):
        text = base_prompt(
            "[Shot 1] A woman dozes. 全程保持每个人物的轮廓、面部边缘与服装边缘清晰稳定，"
            "无波纹、扭曲或边缘抖动。"
        )
        issues = validate_base(text, duration=4.0)
        assert "RIPPLE_SPELL_BANNED" in errors_of(issues)

    def test_first_line_duration_sentence_banned(self):
        text = "This is a 4-second continuous shot.\n\n" + base_prompt("[Shot 1] A woman dozes.")
        issues = validate_base(text, duration=4.0)
        assert "DURATION_SENTENCE_BANNED" in errors_of(issues)


class TestOfficialFormat:
    """官方格式新规则（issue #2）。"""

    def test_legacy_fixture_reports_errors(self):
        """负样本：实测旧 shot-01.md 必须报错（自创结构 + 节拍压段尾）。"""
        issues = validate_base(LEGACY_SHOT01, duration=4.0)
        codes = errors_of(issues)
        for expected in ("GLOBAL_LOCK_BANNED", "BRIDGE_FROM_BANNED", "END_HOOK_BANNED",
                         "RIPPLE_SPELL_BANNED", "DURATION_SENTENCE_BANNED",
                         "LAST_TIMESTAMP_TOO_CLOSE_TO_END"):
            assert expected in codes, f"缺少 {expected}，实际 {codes}"

    def test_official_fixture_passes(self):
        """正样本：官方格式改写稿（I2VA 适配版）必须全绿。"""
        issues = validate_base(OFFICIAL_SHOT01_I2VA, duration=4.0, variant="I2VA")
        assert issues == []

    def test_last_timestamp_too_close_to_end(self):
        text = base_prompt(
            "[Shot 1] A woman dozes. At 00:01.000 she stirs. At 00:03.900 a cat peeks in."
        )
        issues = validate_base(text, duration=4.0)
        assert "LAST_TIMESTAMP_TOO_CLOSE_TO_END" in errors_of(issues)

    def test_last_timestamp_one_second_before_end_ok(self):
        text = base_prompt(
            "[Shot 1] A woman dozes. At 00:01.000 she stirs. At 00:03.000 a cat peeks in."
        )
        issues = validate_base(text, duration=4.0)
        assert "LAST_TIMESTAMP_TOO_CLOSE_TO_END" not in errors_of(issues)

    def test_multi_shot_warns(self):
        text = base_prompt(
            "[Shot 1] A woman dozes behind the counter.\n"
            "[Shot 2] At 00:02.000, the camera cuts to a close-up of her hand."
        )
        issues = validate_base(text, duration=4.0)
        assert "MULTI_SHOT_SEGMENT" in codes(issues)

    def test_single_shot_no_warning(self):
        text = base_prompt("[Shot 1] A woman dozes behind the counter. At 00:02.000, she stirs.")
        issues = validate_base(text, duration=4.0)
        assert "MULTI_SHOT_SEGMENT" not in codes(issues)


class TestSentenceCaps:
    """官方 §4.6/§4.7 句数上限（issue #2 钉回归：规则为迁移前既有，此处钉住防退化）。"""

    def test_soundscape_over_four_sentences_warns(self):
        text = (OFFICIAL_SHOT01_I2VA
                .replace(
                    "A low refrigerator hum sustains the deep-night quiet while the woman's "
                    "slow, long breaths pass at intervals. The glass door gives a faint "
                    "metal-hinge creak as it is pushed, and the brass wind chime rings twice, "
                    "softly, near the end.",
                    "One. Two. Three. Four. Five."))
        issues = validate_base(text, duration=4.0, variant="I2VA")
        assert "SOUNDSCAPE_TOO_LONG" in codes(issues)

    def test_music_over_three_sentences_warns(self):
        text = OFFICIAL_SHOT01_I2VA.replace("non_diegetic_music: N/A",
                                            "non_diegetic_music: One. Two. Three. Four.")
        issues = validate_base(text, duration=4.0, variant="I2VA")
        assert "MUSIC_TOO_LONG" in codes(issues)

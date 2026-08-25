"""theme_guard 主题忠实度守卫测试。"""
from __future__ import annotations

import pytest

from minimax_h3_prompt.tools.h3_validator import ValidationIssue
from minimax_h3_prompt.tools.theme_guard import (
    contains_unqualified_conflict,
    description_body,
    scene_requirement_groups,
    theme_fidelity_issues,
    theme_requirements_text,
)


def _base_prompt(desc_body: str) -> str:
    return (
        "How the reference pictures align with the target video — Picture 1 (from Shot 1) "
        "aligns with the 0.00-second mark of the target video; Picture 2 (from Shot N) "
        "aligns with the 5.00-second mark of the target video.\n\n"
        "integrated_multimodal_description: "
        + desc_body
        + "\n\noverall_soundscape:\nN/A\n\nnon_diegetic_music:\nN/A"
    )


GOOD_TAVERN = (
    "Inside a warm medieval tavern, the young woman sits at a wooden table, "
    "a tankard and the old letter before her. Behind the window, a distant forest clearing "
    "is visible under the rain. She lifts the letter to read by candlelight."
)
DRIFT_TAVERN = (
    "On an open field, the young woman abandons the tavern and fights a stranger "
    "with a sword under the bare sky."
)
NO_TAVERN = "The young woman rides across a golden wheat field at dusk."


def test_scene_requirement_groups_detect_chinese_and_english():
    assert scene_requirement_groups("在酒馆喝酒") == (("tavern", "inn", "alehouse"),)
    assert scene_requirement_groups("at a tavern drinking") == (("tavern", "inn", "alehouse"),)
    assert scene_requirement_groups("深夜在街道独行") == (("street", "road"),)
    assert scene_requirement_groups("一只猫走过海边") == (("beach", "seaside", "coast"),)
    # 未知地点不臆测
    assert scene_requirement_groups("机器人在太空漫游") == ()


def test_passes_when_theme_scene_present_and_qualified():
    issues = theme_fidelity_issues(_base_prompt(GOOD_TAVERN), plot="在酒馆喝酒", mode="base")
    errors = [i for i in issues if i.severity == "error"]
    assert errors == []


def test_drift_open_field_is_error():
    issues = theme_fidelity_issues(_base_prompt(DRIFT_TAVERN), plot="在酒馆喝酒", mode="base")
    errors = [i for i in issues if i.severity == "error"]
    codes = {i.code for i in errors}
    assert "THEME_SCENE_DRIFT" in codes


def test_missing_theme_scene_is_error():
    issues = theme_fidelity_issues(_base_prompt(NO_TAVERN), plot="在酒馆喝酒", mode="base")
    errors = [i for i in issues if i.severity == "error"]
    codes = {i.code for i in errors}
    assert "THEME_SCENE_MISSING" in codes


def test_qualified_window_reference_is_allowed():
    # “窗外森林”是合理背景，不算漂移
    assert contains_unqualified_conflict(
        "behind the window, a forest clearing is visible", "forest clearing"
    ) is False


def test_unqualified_conflict_detects():
    assert contains_unqualified_conflict("they fight in a forest clearing", "forest clearing") is True


def test_rejects_extra_groups_from_fl2va():
    # 即使 plot 用英文 tavern，也应被词表捕获；extra 补充约束生效
    issues = theme_fidelity_issues(
        _base_prompt(NO_TAVERN),
        plot="a woman drinks at a tavern",
        mode="base",
        extra_groups=[("tavern",)],
    )
    errors = [i for i in issues if i.severity == "error"]
    assert any(i.code == "THEME_SCENE_MISSING" for i in errors)


def test_description_body_extraction():
    body = "a woman at an inn"
    extracted = description_body(_base_prompt(body), mode="base")
    assert extracted.strip() == body


def test_theme_requirements_text():
    txt = theme_requirements_text("在酒馆喝酒")
    assert "tavern/inn/alehouse" in txt
    assert "forest clearing" in txt
    assert theme_requirements_text("机器人漫游") == ""


def test_types_are_issue_objects():
    issues = theme_fidelity_issues(_base_prompt(NO_TAVERN), plot="在酒馆喝酒", mode="base")
    assert all(isinstance(i, ValidationIssue) for i in issues)
    assert issues[0].severity == "error"
    assert issues[0].code == "THEME_SCENE_MISSING"
from types import SimpleNamespace

from jiuwenswarm.agents.harness.common.prompt.skills_goal_override import (
    _SKILLS_PROMPT_MAX_CHARS_ENV,
    _build_all_mode_skill_prompt_from_skills,
    _build_auto_list_mode_skill_prompt,
)


def test_all_mode_uses_dynamic_available_skills_xml_without_static_catalogue():
    prompt = _build_all_mode_skill_prompt_from_skills(
        [
            SimpleNamespace(name="custom-pdf", description="Handles user PDFs."),
            SimpleNamespace(name="custom-image", description="Handles user images."),
        ]
    )

    assert "# Tools" in prompt
    assert "Skill Discovery and Installation (`find-skills`)" in prompt
    assert "Default tool:** the `find-skills` skill" in prompt
    assert "Tool Selection Principle (xiaoyi First)" in prompt
    assert "<available_skills>" in prompt
    assert "<name>custom-pdf</name>" in prompt
    assert "<name>custom-image</name>" in prompt
    assert "xiaoyi-ppt-win" not in prompt


def test_all_mode_keeps_distinct_legacy_and_win_skill_names():
    prompt = _build_all_mode_skill_prompt_from_skills(
        [
            SimpleNamespace(name="xiaoyi-ppt", description="Legacy PPT skill."),
            SimpleNamespace(name="xiaoyi-ppt-win", description="Windows PPT skill."),
        ]
    )

    assert "<name>xiaoyi-ppt</name>" in prompt
    assert "<name>xiaoyi-ppt-win</name>" in prompt


def test_auto_list_keeps_only_the_stable_preamble():
    prompt = _build_auto_list_mode_skill_prompt()

    assert "# Tools" in prompt
    assert "find-skills" in prompt
    assert "Tool Selection Principle (xiaoyi First)" in prompt
    assert "<available_skills>" not in prompt
    assert "xiaoyi-ppt-win" not in prompt


def test_budget_preserves_names_after_full_descriptions(monkeypatch):
    skills = [
        SimpleNamespace(name="first", description="first description " * 40),
        SimpleNamespace(name="second", description="second description " * 40),
    ]
    full_first = _build_all_mode_skill_prompt_from_skills([skills[0]])
    monkeypatch.setenv(_SKILLS_PROMPT_MAX_CHARS_ENV, str(len(full_first) + 40))

    prompt = _build_all_mode_skill_prompt_from_skills(skills)

    assert "<name>first</name>" in prompt
    assert "<name>second</name>" in prompt
    assert "<description>second description" not in prompt

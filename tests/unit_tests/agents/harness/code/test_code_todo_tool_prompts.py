# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from jiuwenswarm.agents.harness.code.prompt.code_prompt_builder import build_code_system_prompt
from jiuwenswarm.agents.harness.code.prompt.code_todo_tool_prompts import (
    CODE_TODO_CREATE_DESCRIPTION_EN,
    CODE_TODO_MODIFY_DESCRIPTION_EN,
    CODE_TODO_TOOL_PROMPTS,
    get_code_todo_create_input_params,
)
from openjiuwen.harness.prompts.sections import context


def _build_tools_content_en() -> str:
    """Return the merged Tool Usage Rules content used by all three modes."""
    from types import SimpleNamespace
    from openjiuwen.core.foundation.tool.base import ToolCard
    ability_manager = SimpleNamespace(list=lambda: [ToolCard(name="read_file")])
    content = context.build_tools_content(ability_manager, language="en")
    assert content is not None
    return content


def test_todo_create_prompt_scales_by_complexity():
    assert "Scale the list" in CODE_TODO_CREATE_DESCRIPTION_EN
    assert "2–3" in CODE_TODO_CREATE_DESCRIPTION_EN
    assert "4–6 max" in CODE_TODO_CREATE_DESCRIPTION_EN
    assert "When to skip" in CODE_TODO_CREATE_DESCRIPTION_EN
    assert "Do NOT mirror the user's spec headings" in CODE_TODO_CREATE_DESCRIPTION_EN


def test_todo_modify_prompt_avoids_todo_only_rounds():
    assert "Avoid todo-only rounds" in CODE_TODO_MODIFY_DESCRIPTION_EN
    assert "Batch multiple updates" in CODE_TODO_MODIFY_DESCRIPTION_EN
    assert "parallel" in CODE_TODO_MODIFY_DESCRIPTION_EN.lower()


def test_todo_create_schema_describes_outcome_milestones():
    params = get_code_todo_create_input_params()
    tasks_desc = params["properties"]["tasks"]["description"]
    assert "2–3" in tasks_desc
    assert "4–6 max" in tasks_desc
    assert "Outcome-based" in tasks_desc


def test_code_system_prompt_has_task_planning_section():
    text = _build_tools_content_en()
    assert "# Tool Usage Rules" in text
    assert "## Task planning (todos)" in text
    assert "2–3 outcome-based milestones" in text
    assert "4–6 milestones max" in text
    assert "avoid todo-only rounds" in text
    assert "don't batch" not in text.lower()


def test_code_system_prompt_does_not_reference_disabled_subagents():
    prompt = build_code_system_prompt()
    assert "explore_agent" not in prompt
    assert "plan_agent" not in prompt


def test_code_system_prompt_disambiguates_media_generation_from_coding():
    prompt = build_code_system_prompt()
    assert "# Doing tasks" in prompt
    assert "media deliverable" in prompt
    assert "`skill_tool`" in prompt
    assert "SKILL.md" in prompt
    assert "## Generative media skills" not in prompt
    assert "`seedream-image-gen`" not in prompt
    assert "`invoke`" not in prompt
    assert "PluginSkillExecTool" not in prompt
    assert "seedreamLite4Skill" not in prompt


def test_all_code_todo_tools_registered():
    assert set(CODE_TODO_TOOL_PROMPTS) == {
        "todo_create",
        "todo_list",
        "todo_get",
        "todo_modify",
    }

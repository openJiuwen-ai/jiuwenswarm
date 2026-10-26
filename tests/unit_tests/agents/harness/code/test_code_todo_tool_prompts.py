# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from jiuwenswarm.agents.harness.code.prompt.code_prompt_builder import build_code_system_prompt
from jiuwenswarm.agents.harness.code.prompt.code_todo_tool_prompts import (
    CODE_TODO_CREATE_DESCRIPTION_EN,
    CODE_TODO_MODIFY_DESCRIPTION_EN,
    CODE_TODO_TOOL_PROMPTS,
    get_code_todo_create_input_params,
)


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
    text = build_code_system_prompt()
    assert "## Task planning (todos)" in text
    assert "2–3 outcome-based milestones" in text
    assert "4–6 milestones max" in text
    assert "avoid todo-only rounds" in text
    assert "don't batch" not in text.lower()


def test_all_code_todo_tools_registered():
    assert set(CODE_TODO_TOOL_PROMPTS) == {
        "todo_create",
        "todo_list",
        "todo_get",
        "todo_modify",
    }

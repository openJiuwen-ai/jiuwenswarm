from types import SimpleNamespace

import pytest

from jiuwenswarm.agents.harness.common.rails.tool_usage_prompt_rail import (
    XiaoyiDefaultToolVisibilityRail,
    filter_xiaoyi_default_model_tools,
)


def _function_tool(name: str) -> dict:
    return {"type": "function", "function": {"name": name}}


def test_default_tool_filter_removes_optional_platform_groups():
    tools = [
        _function_tool("wiki_query"),
        _function_tool("audio_metadata"),
        _function_tool("configure_channel"),
        _function_tool("submit_goal_report"),
        _function_tool("get_current_goal"),
        _function_tool("prepare_skill_evolution"),
        _function_tool("evolve_review_task"),
        _function_tool("list_skill_experiences"),
        _function_tool("read_skill_experiences"),
        _function_tool("evolve_skill_experiences"),
        _function_tool("simplify_skill_experiences"),
        _function_tool("code"),
        _function_tool("fetch_webpage"),
        _function_tool("read_file"),
    ]

    assert [tool["function"]["name"] for tool in filter_xiaoyi_default_model_tools(tools)] == [
        "fetch_webpage",
        "read_file",
    ]


@pytest.mark.asyncio
async def test_visibility_rail_filters_tool_cards_and_openai_schema_tools():
    visible_card = SimpleNamespace(name="read_file")
    hidden_card = SimpleNamespace(name="get_wechat_login_status")
    inputs = SimpleNamespace(
        tools=[hidden_card, _function_tool("evolve_skill_experiences"), visible_card]
    )

    await XiaoyiDefaultToolVisibilityRail().before_model_call(SimpleNamespace(inputs=inputs))

    assert inputs.tools == [visible_card]

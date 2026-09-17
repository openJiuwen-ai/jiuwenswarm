"""Tests for CodeAgentRail and AgentTool."""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from openjiuwen.core.session.agent import Session
from openjiuwen.core.foundation.tool import ToolCard
from jiuwenswarm.server.runtime.agent_adapter.code_agent_rail import (
    AgentTool,
    CodeAgentRail,
    DISALLOWED_FOR_SUBAGENTS,
    TOOL_GROUPS,
    _build_agent_tool_card,
    _filter_tool_cards,
)


class TestBuildAgentToolCard:
    def test_empty_agents_returns_description_with_available_header(self):
        card = _build_agent_tool_card([])
        assert card.name == "Agent"
        assert "Available custom agents (created via /agents):" in card.description

    def test_lists_agent_with_description_and_tools(self):
        agent = _make_agent_def(
            name="reviewer", when_to_use="review code", tools=["Read", "Bash"]
        )
        card = _build_agent_tool_card([agent])
        assert "reviewer: review code (Tools: Read, Bash)" in card.description

    def test_input_params_require_subagent_type(self):
        card = _build_agent_tool_card([])
        assert "subagent_type" in card.input_params["required"]


class TestAgentTool:
    @pytest.mark.asyncio
    async def test_invoke_missing_subagent_type_raises(self):
        tool = _make_agent_tool()
        session = MagicMock(spec=Session)
        with pytest.raises(Exception) as exc:
            await tool.invoke({"prompt": "do X"}, session=session)
        assert "subagent_type" in str(exc.value).lower()

    @pytest.mark.asyncio
    async def test_invoke_missing_prompt_raises(self):
        tool = _make_agent_tool()
        session = MagicMock(spec=Session)
        with pytest.raises(Exception) as exc:
            await tool.invoke({"subagent_type": "reviewer"}, session=session)
        assert "prompt" in str(exc.value).lower()

    @pytest.mark.asyncio
    async def test_invoke_missing_session_kwarg_raises(self):
        """When session kwarg is missing or not a Session instance, raise."""
        tool = _make_agent_tool()
        with pytest.raises(Exception) as exc:
            await tool.invoke({"subagent_type": "reviewer", "prompt": "do X"})
        assert "session" in str(exc.value).lower()

    @pytest.mark.asyncio
    async def test_invoke_unknown_agent_type_raises(self):
        tool = _make_agent_tool()
        session = MagicMock(spec=Session)
        with pytest.raises(Exception) as exc:
            await tool.invoke(
                {"subagent_type": "nonexistent", "prompt": "do X"},
                session=session,
            )
        assert "not found" in str(exc.value).lower()

    @pytest.mark.asyncio
    async def test_invoke_sync_creates_and_runs_subagent(self):
        mock_subagent = MagicMock()
        mock_subagent.invoke = AsyncMock(return_value={"output": "done"})

        parent = MagicMock()
        agent_def = _make_agent_def(
            name="reviewer", when_to_use="review", tools=["Read"]
        )
        tool = _make_agent_tool(parent=parent, custom_agents=[agent_def])

        session = MagicMock(spec=Session)
        session.get_session_id.return_value = "session_123"

        with patch.object(tool, "_create_sub_agent", return_value=mock_subagent):
            result = await tool.invoke(
                {"subagent_type": "reviewer", "prompt": "review this"},
                session=session,
            )

        mock_subagent.invoke.assert_called_once()
        assert result.success is True
        assert result.data["output"] == "done"
        assert result.data["agent_id"] == "reviewer"

    @pytest.mark.asyncio
    async def test_invoke_background_returns_async_launched(self):
        mock_subagent = MagicMock()

        parent = MagicMock()
        agent_def = _make_agent_def(name="reviewer")
        tool = _make_agent_tool(parent=parent, custom_agents=[agent_def])

        session = MagicMock(spec=Session)
        session.get_session_id.return_value = "session_123"

        with patch.object(tool, "_create_sub_agent", return_value=mock_subagent):
            result = await tool.invoke(
                {"subagent_type": "reviewer", "prompt": "do X", "background": True},
                session=session,
            )

        assert result.data["status"] == "async_launched"
        assert result.data["agent_id"] == "reviewer"
        assert result.data["prompt"] == "do X"

    @pytest.mark.asyncio
    async def test_invoke_with_subagent_creation_failure_raises(self):
        parent = MagicMock()
        agent_def = _make_agent_def(name="reviewer")
        tool = _make_agent_tool(parent=parent, custom_agents=[agent_def])

        session = MagicMock(spec=Session)
        session.get_session_id.return_value = "session_123"

        with patch.object(tool, "_create_sub_agent", side_effect=RuntimeError("creation failed")):
            with pytest.raises(Exception) as exc:
                await tool.invoke(
                    {"subagent_type": "reviewer", "prompt": "do X"},
                    session=session,
                )
        assert "creation failed" in str(exc.value)

    @pytest.mark.asyncio
    async def test_invoke_with_subagent_execution_failure_raises(self):
        mock_subagent = MagicMock()
        mock_subagent.invoke = AsyncMock(
            side_effect=RuntimeError("execution failed")
        )

        parent = MagicMock()
        agent_def = _make_agent_def(name="reviewer")
        tool = _make_agent_tool(parent=parent, custom_agents=[agent_def])

        session = MagicMock(spec=Session)
        session.get_session_id.return_value = "session_123"

        with patch.object(tool, "_create_sub_agent", return_value=mock_subagent):
            with pytest.raises(Exception) as exc:
                await tool.invoke(
                    {"subagent_type": "reviewer", "prompt": "do X"},
                    session=session,
                )
        assert "execution failed" in str(exc.value)


class TestCodeAgentRail:
    def test_init_no_custom_agents_skips_tool_registration(self):
        rail = CodeAgentRail(workspace_dir="/tmp")
        agent = MagicMock()
        with patch.object(rail, "_load_custom_agents", return_value=[]):
            rail.init(agent)
        assert rail._agent_tool is None

    def test_init_with_custom_agents_registers_tool(self):
        rail = CodeAgentRail(workspace_dir="/tmp")
        agent = MagicMock()
        agent_def = _make_agent_def(name="reviewer")
        with patch.object(rail, "_load_custom_agents", return_value=[agent_def]):
            rail.init(agent)
        assert rail._agent_tool is not None
        # Registration now goes through the unified ability_manager.add_ability.
        agent.ability_manager.add_ability.assert_called_once()

    def test_uninit_removes_tool(self):
        rail = CodeAgentRail(workspace_dir="/tmp")
        agent = MagicMock()
        agent_def = _make_agent_def(name="reviewer")
        with patch.object(rail, "_load_custom_agents", return_value=[agent_def]):
            rail.init(agent)
        assert rail._agent_tool is not None
        rail.uninit(agent)
        assert rail._agent_tool is None
        assert rail._agent is None
        # Removal mirrors add_ability via ability_manager.remove_ability.
        agent.ability_manager.remove_ability.assert_called_once()

    def test_init_twice_replaces_tool(self):
        """Initializing twice should create a new AgentTool each time."""
        rail = CodeAgentRail(workspace_dir="/tmp")
        agent = MagicMock()
        agent_def = _make_agent_def(name="reviewer")
        with patch.object(rail, "_load_custom_agents", return_value=[agent_def]):
            rail.init(agent)
            first_tool = rail._agent_tool
            rail.init(agent)
            assert rail._agent_tool is not first_tool

    def test_load_custom_agents_filters_builtin_and_disabled(self):
        """_load_custom agents should only include non-builtin enabled agents."""
        from jiuwenswarm.server.runtime.agent_config_service import AgentDefinition

        rail = CodeAgentRail(workspace_dir="/tmp")
        # Mock the AgentConfigService to return a mix of agents
        builtin = AgentDefinition(
            name="explore", description="", prompt="",
            source="builtin", enabled=None,
        )
        project_disabled = AgentDefinition(
            name="disabled-agent", description="", prompt="",
            source="project", enabled=False,
        )
        project_enabled = AgentDefinition(
            name="enabled-agent", description="", prompt="",
            source="project", enabled=True,
        )

        with patch(
            "jiuwenswarm.server.runtime.agent_config_service.AgentConfigService"
        ) as mock_svc_cls:
            mock_svc = mock_svc_cls.return_value
            mock_svc.list_agents.return_value = [
                builtin, project_disabled, project_enabled,
            ]
            result = rail._load_custom_agents()

        assert len(result) == 1
        assert result[0].name == "enabled-agent"

    def test_priority_set_correctly(self):
        rail = CodeAgentRail(workspace_dir="/tmp")
        assert rail.priority == 90


class TestFilterToolCards:
    """Tests for _filter_tool_cards() — tool filtering by agent definition."""

    @pytest.fixture(autouse=True)
    def _mock_display_mapping(self):
        """Mock _build_display_to_internal_mapping to avoid importing prompt_toolkit in CI."""
        mapping = {"Read": "read_file", "Bash": "bash", "Edit": "edit_file",
                    "Grep": "grep", "Write": "write_file", "LS": "ls"}
        with patch(
            "jiuwenswarm.server.runtime.agent_adapter.code_agent_rail._build_display_to_internal_mapping",
            return_value=mapping,
        ):
            yield

    def test_wildcard_returns_all_cards(self):
        cards = [
            ToolCard(id="t1", name="read_file", description="Read file"),
            ToolCard(id="t2", name="bash", description="Run bash"),
            ToolCard(id="t3", name="edit_file", description="Edit file"),
        ]
        result = _filter_tool_cards(cards, allowed_tools=["*"])
        assert result == cards

    def test_specific_display_names_filter_cards(self):
        cards = [
            ToolCard(id="t1", name="read_file", description="Read file"),
            ToolCard(id="t2", name="bash", description="Run bash"),
            ToolCard(id="t3", name="edit_file", description="Edit file"),
            ToolCard(id="t4", name="grep", description="Search files"),
        ]
        result = _filter_tool_cards(cards, allowed_tools=["Read", "Bash"])
        assert len(result) == 2
        assert {tc.name for tc in result} == {"read_file", "bash"}

    def test_internal_names_also_accepted(self):
        cards = [
            ToolCard(id="t1", name="read_file", description="Read file"),
            ToolCard(id="t2", name="bash", description="Run bash"),
        ]
        result = _filter_tool_cards(cards, allowed_tools=["read_file"])
        assert len(result) == 1
        assert result[0].name == "read_file"

    def test_disallowed_tools_removes_from_result(self):
        cards = [
            ToolCard(id="t1", name="read_file", description="Read file"),
            ToolCard(id="t2", name="bash", description="Run bash"),
            ToolCard(id="t3", name="edit_file", description="Edit file"),
        ]
        result = _filter_tool_cards(
            cards, allowed_tools=["*"], disallowed_tools=["Edit"]
        )
        assert len(result) == 2
        assert all(tc.name != "edit_file" for tc in result)

    def test_disallowed_tools_with_internal_name(self):
        cards = [
            ToolCard(id="t1", name="read_file", description="Read file"),
            ToolCard(id="t2", name="bash", description="Run bash"),
        ]
        result = _filter_tool_cards(
            cards, allowed_tools=["*"], disallowed_tools=["read_file"]
        )
        assert len(result) == 1
        assert result[0].name == "bash"

    def test_empty_allowed_list_returns_empty(self):
        cards = [ToolCard(id="t1", name="read_file", description="Read file")]
        result = _filter_tool_cards(cards, allowed_tools=[])
        assert result == []

    def test_no_matching_tools_returns_empty(self):
        cards = [ToolCard(id="t1", name="read_file", description="Read file")]
        result = _filter_tool_cards(cards, allowed_tools=["WebSearch"])
        assert result == []

    def test_mixed_display_and_internal_names(self):
        """When allowed_tools contains both display and internal names, both match."""
        cards = [
            ToolCard(id="t1", name="read_file", description="Read file"),
            ToolCard(id="t2", name="bash", description="Run bash"),
            ToolCard(id="t3", name="grep", description="Search"),
        ]
        result = _filter_tool_cards(cards, allowed_tools=["Read", "bash"])
        assert len(result) == 2
        assert {tc.name for tc in result} == {"read_file", "bash"}

    def test_does_not_modify_original_list(self):
        cards = [
            ToolCard(id="t1", name="read_file", description="Read file"),
            ToolCard(id="t2", name="bash", description="Run bash"),
        ]
        original = list(cards)
        _ = _filter_tool_cards(cards, allowed_tools=["*"])
        assert cards == original

    def test_disallowed_tools_none_does_nothing(self):
        cards = [
            ToolCard(id="t1", name="read_file", description="Read file"),
            ToolCard(id="t2", name="bash", description="Run bash"),
        ]
        result = _filter_tool_cards(cards, allowed_tools=["*"], disallowed_tools=None)
        assert result == cards

    def test_disallowed_tools_empty_list_does_nothing(self):
        cards = [
            ToolCard(id="t1", name="read_file", description="Read file"),
        ]
        result = _filter_tool_cards(cards, allowed_tools=["*"], disallowed_tools=[])
        assert result == cards


class TestModuleConstants:
    """Verify module-level constants have expected values."""

    def test_disallowed_for_subagents_contains_key_tools(self):
        assert "Agent" in DISALLOWED_FOR_SUBAGENTS
        assert "task" in DISALLOWED_FOR_SUBAGENTS
        assert "enter_plan_mode" in DISALLOWED_FOR_SUBAGENTS
        assert "switch_mode" in DISALLOWED_FOR_SUBAGENTS

    def test_tool_groups_has_expected_categories(self):
        assert "核心" in TOOL_GROUPS
        assert "搜索" in TOOL_GROUPS
        assert "代码智能" in TOOL_GROUPS
        assert "高级" in TOOL_GROUPS
        assert "可视化" in TOOL_GROUPS

    def test_tool_groups_use_display_names(self):
        """TOOL_GROUPS values should use display names, not internal names."""
        assert "Read" in TOOL_GROUPS["核心"]
        assert "Bash" in TOOL_GROUPS["核心"]
        assert "WebSearch" in TOOL_GROUPS["搜索"]


# ─── helpers ─────────────────────────────────────────


def _make_agent_def(
    name="test-agent",
    when_to_use="test agent",
    tools=None,
    prompt="You are a test agent.",
):
    from jiuwenswarm.server.runtime.agent_config_service import AgentDefinition

    return AgentDefinition(
        name=name,
        description="test",
        prompt=prompt,
        source="project",
        tools=tools or ["*"],
        when_to_use=when_to_use,
    )


def _build_tool_card() -> ToolCard:
    return ToolCard(
        id="agent_tool_test123",
        name="Agent",
        description="Launch a custom agent",
        input_params={
            "type": "object",
            "properties": {
                "subagent_type": {"type": "string"},
                "prompt": {"type": "string"},
                "description": {"type": "string"},
                "model": {"type": "string", "enum": ["sonnet", "opus", "haiku"]},
                "background": {"type": "boolean", "default": False},
            },
            "required": ["subagent_type", "prompt", "description"],
        },
    )


def _make_agent_tool(
    parent: MagicMock | None = None,
    custom_agents: list | None = None,
) -> AgentTool:
    return AgentTool(
        card=_build_tool_card(),
        parent_agent=parent or MagicMock(),
        custom_agents=custom_agents or [],
    )
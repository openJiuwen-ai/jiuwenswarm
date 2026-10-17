# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for team_runtime_inheritance module.

Covers:
- resolve_fallback_models: validation logic, config parsing, synthesis
- resolve_model_config: priority resolution (defaults list > default > react)
- get_default_model_name: config extraction
- MemberInfo / RuntimeInfo / TeamWorkspaceInfo dataclasses
- filter_inheritable_ability_cards: whitelist filtering
- RAIL_WHITELIST and TOOL_WHITELIST constants
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from jiuwenswarm.agents.harness.team.team_runtime_inheritance import (
    MemberInfo,
    RuntimeInfo,
    TeamWorkspaceInfo,
    RAIL_WHITELIST,
    TOOL_WHITELIST,
    resolve_fallback_models,
    resolve_model_config,
    get_default_model_name,
    filter_inheritable_ability_cards,
    get_context_engine_enabled,
)


# ===========================================================================
# Tests: Dataclasses
# ===========================================================================

class TestDataclasses:
    """Test MemberInfo, RuntimeInfo, TeamWorkspaceInfo dataclass defaults."""

    def test_member_info_defaults(self):
        """MemberInfo has sensible defaults."""
        mi = MemberInfo()
        assert mi.agent_name == "team_member"
        assert mi.model_name == "gpt-4"
        assert mi.role is None

    def test_member_info_custom(self):
        """MemberInfo accepts custom values."""
        mi = MemberInfo(agent_name="dev", model_name="qwen3.8-max", role="teammate")
        assert mi.agent_name == "dev"
        assert mi.model_name == "qwen3.8-max"
        assert mi.role == "teammate"

    def test_runtime_info_defaults(self):
        """RuntimeInfo has sensible defaults."""
        ri = RuntimeInfo()
        assert ri.channel == "default"
        assert ri.language == "cn"

    def test_runtime_info_custom(self):
        """RuntimeInfo accepts custom values."""
        ri = RuntimeInfo(channel="feishu", language="en")
        assert ri.channel == "feishu"
        assert ri.language == "en"

    def test_team_workspace_info_defaults(self):
        """TeamWorkspaceInfo defaults to None."""
        tw = TeamWorkspaceInfo()
        assert tw.root_dir is None
        assert tw.skills_dir is None
        assert tw.team_id is None
        assert tw.config is None
        assert tw.trajectory_span_processor is None

    def test_team_workspace_info_custom(self):
        """TeamWorkspaceInfo accepts custom values."""
        tw = TeamWorkspaceInfo(
            root_dir="/tmp/ws",
            skills_dir="/tmp/ws/skills",
            team_id="team-1",
            config={"key": "value"},
        )
        assert tw.root_dir == "/tmp/ws"
        assert tw.skills_dir == "/tmp/ws/skills"
        assert tw.team_id == "team-1"
        assert tw.config == {"key": "value"}


# ===========================================================================
# Tests: Whitelist constants
# ===========================================================================

class TestWhitelistConstants:
    """Test RAIL_WHITELIST and TOOL_WHITELIST are properly defined."""

    def test_rail_whitelist_is_frozenset(self):
        """RAIL_WHITELIST is a frozenset of strings."""
        assert isinstance(RAIL_WHITELIST, frozenset)
        assert len(RAIL_WHITELIST) > 0
        for item in RAIL_WHITELIST:
            assert isinstance(item, str)

    def test_tool_whitelist_is_frozenset(self):
        """TOOL_WHITELIST is a frozenset of strings."""
        assert isinstance(TOOL_WHITELIST, frozenset)
        assert len(TOOL_WHITELIST) > 0
        for item in TOOL_WHITELIST:
            assert isinstance(item, str)

    def test_rail_whitelist_contains_expected_rails(self):
        """Key rails are in the whitelist."""
        expected = [
            "RuntimePromptRail",
            "SecurityRail",
            "AvatarPromptRail",
            "TeamSkillEvolutionRail",
            "SkillEvolutionRail",
        ]
        for rail_name in expected:
            assert rail_name in RAIL_WHITELIST

    def test_tool_whitelist_contains_expected_tools(self):
        """Key tools are in the whitelist."""
        expected = [
            "free_search",
            "fetch_webpage",
            "send_message",
            "user_todos",
            "search_skill",
            "install_skill",
        ]
        for tool_name in expected:
            assert tool_name in TOOL_WHITELIST


# ===========================================================================
# Tests: resolve_model_config
# ===========================================================================

class TestResolveModelConfig:
    """Test resolve_model_config priority resolution."""

    def test_defaults_list_with_is_default(self):
        """is_default=true entry takes priority."""
        config = {
            "models": {
                "defaults": [
                    {
                        "is_default": False,
                        "model_client_config": {"model_name": "model-a"},
                        "model_config_obj": {"temperature": 0.5},
                    },
                    {
                        "is_default": True,
                        "model_client_config": {"model_name": "model-b"},
                        "model_config_obj": {"temperature": 0.7},
                    },
                ],
            },
        }
        mcc, mco, name = resolve_model_config(config)
        assert name == "model-b"
        assert mcc["model_name"] == "model-b"
        assert mco["temperature"] == 0.7

    def test_defaults_list_no_is_default(self):
        """Without is_default=true, first entry is used."""
        config = {
            "models": {
                "defaults": [
                    {
                        "model_client_config": {"model_name": "first-model"},
                        "model_config_obj": {"top_p": 0.9},
                    },
                    {
                        "model_client_config": {"model_name": "second-model"},
                        "model_config_obj": {"top_p": 0.8},
                    },
                ],
            },
        }
        mcc, mco, name = resolve_model_config(config)
        assert name == "first-model"

    def test_fallback_to_default_single(self):
        """Falls back to models.default when defaults list is absent."""
        config = {
            "models": {
                "default": {
                    "model_client_config": {"model_name": "single-model"},
                    "model_config_obj": {"max_tokens": 4096},
                },
            },
        }
        mcc, mco, name = resolve_model_config(config)
        assert name == "single-model"

    def test_fallback_to_react(self):
        """Falls back to react section when models section is empty."""
        config = {
            "models": {},
            "react": {
                "model_client_config": {"model_name": "react-model"},
                "model_config_obj": {"stream": True},
            },
        }
        mcc, mco, name = resolve_model_config(config)
        assert name == "react-model"

    def test_ultimate_fallback_gpt4(self):
        """Ultimate fallback is 'gpt-4' when nothing is configured."""
        config = {"models": {}, "react": {}}
        mcc, mco, name = resolve_model_config(config)
        assert name == "gpt-4"

    def test_empty_config(self):
        """Empty config dict falls back to gpt-4."""
        mcc, mco, name = resolve_model_config({})
        assert name == "gpt-4"

    def test_defaults_list_empty_entries(self):
        """Empty defaults list falls through to default/react."""
        config = {
            "models": {
                "defaults": [],
                "default": {
                    "model_client_config": {"model_name": "fallback"},
                    "model_config_obj": {},
                },
            },
        }
        mcc, mco, name = resolve_model_config(config)
        assert name == "fallback"

    def test_model_client_config_none_handling(self):
        """None model_client_config is handled gracefully."""
        config = {
            "models": {
                "defaults": [
                    {
                        "is_default": True,
                        "model_client_config": None,
                        "model_config_obj": None,
                    },
                ],
            },
        }
        mcc, mco, name = resolve_model_config(config)
        assert isinstance(mcc, dict)
        assert isinstance(mco, dict)


# ===========================================================================
# Tests: get_default_model_name
# ===========================================================================

class TestGetDefaultModelName:
    """Test get_default_model_name config extraction."""

    def test_with_config_dict(self):
        """Extracts model name from provided config."""
        config = {
            "models": {
                "default": {
                    "model_client_config": {"model_name": "my-model"},
                },
            },
        }
        assert get_default_model_name(config) == "my-model"

    def test_with_none_config(self):
        """None config triggers auto-load attempt."""
        with patch(
            "jiuwenswarm.agents.harness.team.team_runtime_inheritance.get_config",
            return_value={
                "models": {
                    "default": {
                        "model_client_config": {"model_name": "loaded-model"},
                    },
                },
            },
        ):
            assert get_default_model_name(None) == "loaded-model"

    def test_config_load_failure_fallback(self):
        """Config load failure falls back to gpt-4."""
        with patch(
            "jiuwenswarm.agents.harness.team.team_runtime_inheritance.get_config",
            side_effect=RuntimeError("config unavailable"),
        ):
            assert get_default_model_name(None) == "gpt-4"

    def test_empty_config_fallback(self):
        """Empty config falls back to gpt-4."""
        assert get_default_model_name({}) == "gpt-4"

    def test_missing_model_name_key(self):
        """Missing model_name key falls back to gpt-4."""
        config = {
            "models": {
                "default": {
                    "model_client_config": {},
                },
            },
        }
        assert get_default_model_name(config) == "gpt-4"


# ===========================================================================
# Tests: resolve_fallback_models
# ===========================================================================

class TestResolveFallbackModels:
    """Test resolve_fallback_models parsing and validation."""

    def test_no_fallback_configured(self):
        """Empty list when no fallback_models configured."""
        config = {
            "agents": {
                "agent_teammate": {},
            },
            "models": {"defaults": []},
        }
        result = resolve_fallback_models(config)
        assert result == []

    def test_empty_fallback_list(self):
        """Empty fallback_models list returns empty."""
        config = {
            "agents": {
                "agent_teammate": {"fallback_models": []},
            },
            "models": {"defaults": []},
        }
        result = resolve_fallback_models(config)
        assert result == []

    def test_fallback_found_in_defaults(self):
        """Fallback model found in defaults list uses its config."""
        config = {
            "agents": {
                "agent_teammate": {
                    "fallback_models": ["model-b"],
                },
            },
            "models": {
                "defaults": [
                    {
                        "model_client_config": {"model_name": "model-a", "api_key": "key-a"},
                        "model_config_obj": {"temperature": 0.5},
                    },
                    {
                        "model_client_config": {"model_name": "model-b", "api_key": "key-b"},
                        "model_config_obj": {"temperature": 0.7},
                    },
                ],
            },
        }
        result = resolve_fallback_models(config)
        assert len(result) == 1
        assert result[0]["model_name"] == "model-b"
        assert result[0]["model_client_config"]["api_key"] == "key-b"
        assert result[0]["model_config_obj"]["temperature"] == 0.7

    def test_fallback_not_in_defaults_synthesised(self):
        """Fallback not in defaults is synthesised from primary config."""
        config = {
            "agents": {
                "agent_teammate": {
                    "fallback_models": ["unknown-model"],
                },
            },
            "models": {
                "defaults": [
                    {
                        "model_client_config": {
                            "model_name": "primary",
                            "api_key": "primary-key",
                            "api_base": "http://primary.api",
                        },
                        "model_config_obj": {"temperature": 0.5},
                    },
                ],
            },
        }
        result = resolve_fallback_models(config)
        assert len(result) == 1
        assert result[0]["model_name"] == "unknown-model"
        # Synthesised from primary
        assert result[0]["model_client_config"]["api_key"] == "primary-key"
        assert result[0]["model_client_config"]["api_base"] == "http://primary.api"
        # But model_name is overridden
        assert result[0]["model_client_config"]["model_name"] == "unknown-model"

    def test_multiple_fallbacks(self):
        """Multiple fallback models are resolved in order."""
        config = {
            "agents": {
                "agent_teammate": {
                    "fallback_models": ["model-b", "model-c"],
                },
            },
            "models": {
                "defaults": [
                    {
                        "model_client_config": {"model_name": "model-a"},
                        "model_config_obj": {},
                    },
                    {
                        "model_client_config": {"model_name": "model-b"},
                        "model_config_obj": {"temp": 0.6},
                    },
                ],
            },
        }
        result = resolve_fallback_models(config)
        assert len(result) == 2
        assert result[0]["model_name"] == "model-b"
        assert result[1]["model_name"] == "model-c"  # synthesised

    def test_custom_agent_key(self):
        """Custom agent_key reads from correct config section."""
        config = {
            "agents": {
                "agent_leader": {
                    "fallback_models": ["leader-fb"],
                },
                "agent_teammate": {
                    "fallback_models": ["teammate-fb"],
                },
            },
            "models": {
                "defaults": [
                    {
                        "model_client_config": {"model_name": "primary"},
                        "model_config_obj": {},
                    },
                ],
            },
        }
        result = resolve_fallback_models(config, agent_key="agent_leader")
        assert len(result) == 1
        assert result[0]["model_name"] == "leader-fb"

    def test_blank_fallback_names_skipped(self):
        """Blank/whitespace fallback names are skipped."""
        config = {
            "agents": {
                "agent_teammate": {
                    "fallback_models": ["", "  ", "valid-model"],
                },
            },
            "models": {
                "defaults": [
                    {
                        "model_client_config": {"model_name": "primary"},
                        "model_config_obj": {},
                    },
                ],
            },
        }
        result = resolve_fallback_models(config)
        assert len(result) == 1
        assert result[0]["model_name"] == "valid-model"

    def test_missing_agents_section(self):
        """Missing agents section returns empty list."""
        config = {"models": {"defaults": []}}
        result = resolve_fallback_models(config)
        assert result == []

    def test_missing_agent_key_section(self):
        """Missing specific agent key returns empty list."""
        config = {
            "agents": {"other_agent": {}},
            "models": {"defaults": []},
        }
        result = resolve_fallback_models(config, agent_key="agent_teammate")
        assert result == []

    def test_result_structure(self):
        """Each result entry has the expected keys."""
        config = {
            "agents": {
                "agent_teammate": {
                    "fallback_models": ["fb-model"],
                },
            },
            "models": {
                "defaults": [
                    {
                        "model_client_config": {"model_name": "primary"},
                        "model_config_obj": {},
                    },
                ],
            },
        }
        result = resolve_fallback_models(config)
        assert len(result) == 1
        entry = result[0]
        assert "model_client_config" in entry
        assert "model_config_obj" in entry
        assert "model_name" in entry
        assert isinstance(entry["model_client_config"], dict)
        assert isinstance(entry["model_config_obj"], dict)
        assert isinstance(entry["model_name"], str)


# ===========================================================================
# Tests: filter_inheritable_ability_cards
# ===========================================================================

class TestFilterInheritableAbilityCards:
    """Test filter_inheritable_ability_cards whitelist filtering."""

    def test_whitelisted_tools_included(self):
        """Tools in the whitelist are included in the result."""
        from openjiuwen.core.foundation.tool import ToolCard

        mock_agent = MagicMock()
        card1 = MagicMock(spec=ToolCard)
        card1.name = "free_search"
        card2 = MagicMock(spec=ToolCard)
        card2.name = "send_message"
        mock_agent.ability_manager.list.return_value = [card1, card2]

        result = filter_inheritable_ability_cards(mock_agent)
        assert len(result) == 2

    def test_non_whitelisted_tools_excluded(self):
        """Tools not in the whitelist are excluded."""
        from openjiuwen.core.foundation.tool import ToolCard

        mock_agent = MagicMock()
        card1 = MagicMock(spec=ToolCard)
        card1.name = "free_search"  # whitelisted
        card2 = MagicMock(spec=ToolCard)
        card2.name = "dangerous_tool"  # not whitelisted
        mock_agent.ability_manager.list.return_value = [card1, card2]

        result = filter_inheritable_ability_cards(mock_agent)
        assert len(result) == 1
        assert result[0].name == "free_search"

    def test_non_toolcard_abilities_skipped(self):
        """Non-ToolCard abilities are skipped."""
        mock_agent = MagicMock()
        # Create a plain object that is NOT a ToolCard instance
        class NotAToolCard:
            name = "some_ability"
        non_card = NotAToolCard()
        mock_agent.ability_manager.list.return_value = [non_card]

        result = filter_inheritable_ability_cards(mock_agent)
        assert len(result) == 0

    def test_empty_ability_list(self):
        """Empty ability list returns empty result."""
        mock_agent = MagicMock()
        mock_agent.ability_manager.list.return_value = []

        result = filter_inheritable_ability_cards(mock_agent)
        assert result == []

    def test_exception_handling(self):
        """Exception in ability listing is handled gracefully."""
        mock_agent = MagicMock()
        mock_agent.ability_manager.list.side_effect = RuntimeError("manager error")

        result = filter_inheritable_ability_cards(mock_agent)
        assert result == []


# ===========================================================================
# Tests: get_context_engine_enabled
# ===========================================================================

class TestGetContextEngineEnabled:
    """Test get_context_engine_enabled config parsing."""

    def test_none_config_returns_true(self):
        """None config defaults to True (enabled)."""
        assert get_context_engine_enabled(None) is True

    def test_non_dict_config_returns_true(self):
        """Non-dict config defaults to True."""
        assert get_context_engine_enabled("not a dict") is True

    def test_enabled_true(self):
        """Explicitly enabled returns True."""
        config = {
            "react": {
                "context_engine_config": {"enabled": True},
            },
        }
        assert get_context_engine_enabled(config) is True

    def test_enabled_false(self):
        """Explicitly disabled returns False."""
        config = {
            "react": {
                "context_engine_config": {"enabled": False},
            },
        }
        assert get_context_engine_enabled(config) is False

    def test_missing_react_section(self):
        """Missing react section defaults to True."""
        assert get_context_engine_enabled({}) is True

    def test_missing_context_engine_config(self):
        """Missing context_engine_config defaults to True."""
        config = {"react": {}}
        assert get_context_engine_enabled(config) is True

    def test_empty_context_engine_config(self):
        """Empty context_engine_config defaults to True."""
        config = {"react": {"context_engine_config": {}}}
        assert get_context_engine_enabled(config) is True

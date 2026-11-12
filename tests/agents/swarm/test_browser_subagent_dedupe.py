"""Dedupe of the base team spec's shared browser sub-agent (#10866).

``SubAgentSpec`` has no ``subagent_type`` field (pydantic drops the
keyword), so the member-spec dedupe has to identify the shared browser
entry by the carriers a spec actually has: the agent card name and
``factory_name``.  ``subagent_type`` stays supported for foreign platform
spec objects that do carry it.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from openjiuwen.agent_teams.schema.deep_agent_spec import (
    DeepAgentSpec,
    SubAgentSpec,
    WorkspaceSpec,
)
from openjiuwen.core.single_agent import AgentCard

from jiuwenswarm.agents.swarm import register_swarm_providers, registry
from jiuwenswarm.agents.swarm.config_specs import (
    _is_browser_subagent,
    build_member_deep_agent_spec,
)
from jiuwenswarm.agents.swarm.providers.code_subagents import SWARM_BROWSER_AGENT


def _shared_browser_base_spec() -> DeepAgentSpec:
    """Base team spec pinning the shared playwright browser entry.

    The shared entry carries no ``subagent_type`` and no ``factory_name``:
    that is exactly the shape ``SubAgentSpec`` forces (#10866).
    """
    return DeepAgentSpec(
        subagents=[
            SubAgentSpec(agent_card=AgentCard(name="browser_agent"), system_prompt="")
        ],
        workspace=WorkspaceSpec(root_path="/tmp/ws"),
    )


def _shared_browser_leftovers(spec: DeepAgentSpec) -> list[Any]:
    """Base-spec browser entries that survived the dedupe."""
    return [
        s
        for s in spec.subagents or []
        if getattr(s, "factory_name", None) is None
        and getattr(getattr(s, "agent_card", None), "name", None) == "browser_agent"
    ]


def test_team_mode_deep_spec_dedupes_shared_browser_without_subagent_type() -> None:
    """#10866: the base shared browser entry dies on card / factory match."""
    register_swarm_providers()
    config = {"react": {"subagents": {"browser_agent": {"enabled": True}}}}

    spec = build_member_deep_agent_spec(
        config, "team", "teammate", _shared_browser_base_spec()
    )

    factories = [getattr(s, "factory_name", None) for s in spec.subagents or []]
    assert not _shared_browser_leftovers(
        spec
    ), "shared playwright_official_stdio entry must be removed"
    assert factories.count(SWARM_BROWSER_AGENT) == 1


def test_code_mode_deep_spec_dedupes_shared_browser_from_base() -> None:
    """#10866: code.team members also drop the base shared browser entry."""
    register_swarm_providers()
    config = {"react": {"subagents": {"browser_agent": {"enabled": True}}}}

    spec = build_member_deep_agent_spec(
        config, "code.team", "teammate", _shared_browser_base_spec()
    )

    factories = [getattr(s, "factory_name", None) for s in spec.subagents or []]
    assert not _shared_browser_leftovers(
        spec
    ), "shared playwright_official_stdio entry must be removed"
    assert factories.count(SWARM_BROWSER_AGENT) == 1


def test_is_browser_subagent_matches_all_identity_carriers() -> None:
    """#10866: card name, factory name and foreign subagent_type all match."""
    # SubAgentSpec has no subagent_type field: identity lives on card/factory.
    assert _is_browser_subagent(
        SubAgentSpec(agent_card=AgentCard(name="browser_agent"), system_prompt="")
    )
    assert _is_browser_subagent(
        SubAgentSpec(
            agent_card=AgentCard(name="shared_entry"),
            system_prompt="",
            factory_name=SWARM_BROWSER_AGENT,
        )
    )
    # Foreign platform spec objects keep exposing only subagent_type.
    assert _is_browser_subagent(SimpleNamespace(subagent_type="browser_agent"))
    # Anything else must not match.
    assert not _is_browser_subagent(
        SubAgentSpec(
            agent_card=AgentCard(name="explore_agent"),
            system_prompt="",
            factory_name=registry.EXPLORE_AGENT,
        )
    )
    assert not _is_browser_subagent(SimpleNamespace(subagent_type="explore_agent"))
    assert not _is_browser_subagent(None)

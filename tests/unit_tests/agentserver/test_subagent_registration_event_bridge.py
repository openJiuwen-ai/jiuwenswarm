# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for the JiuwenSwarm sub-agent event/legacy selection boundary."""

from __future__ import annotations

from openjiuwen.agent_teams.schema.deep_agent_spec import DeepAgentSpec
from openjiuwen.core.runner import Runner
from openjiuwen.core.single_agent.schema.agent_card import AgentCard
from openjiuwen.harness.schema.config import SubAgentConfig

from jiuwenswarm.agents.swarm import registry
from jiuwenswarm.agents.swarm.config_specs import build_member_deep_agent_spec
from jiuwenswarm.server.runtime.agent_adapter.subagent_registration import (
    resolve_subagent_registration_plan,
)


def _config(name: str) -> SubAgentConfig:
    return SubAgentConfig(
        agent_card=AgentCard(name=name),
        system_prompt=f"prompt for {name}",
    )


def test_default_plan_routes_only_allowlisted_configs_to_event(monkeypatch) -> None:
    bus = object()
    event_config = _config("general-purpose")
    legacy_config = _config("browser_agent")
    monkeypatch.setattr(registry, "supports_subagent_registration_event", lambda: True)
    monkeypatch.setattr(Runner, "get_extension_bus", lambda: bus)

    plan = resolve_subagent_registration_plan(
        [event_config, legacy_config],
        event_names={"general-purpose"},
    )

    assert plan.mode == "event"
    assert plan.event_subagents == [event_config]
    assert plan.static_subagents == [legacy_config]
    assert plan.extension_bus is bus


def test_default_plan_preserves_legacy_when_event_capability_is_missing(monkeypatch) -> None:
    event_config = _config("general-purpose")
    legacy_config = _config("browser_agent")
    monkeypatch.setattr(registry, "supports_subagent_registration_event", lambda: False)

    plan = resolve_subagent_registration_plan(
        [event_config, legacy_config],
        event_names={"general-purpose"},
    )

    assert plan.mode == "legacy"
    assert plan.static_subagents == [event_config, legacy_config]
    assert plan.event_subagents == []
    assert plan.extension_bus is None


def test_team_member_spec_declares_subagent_event_allowlist(monkeypatch) -> None:
    monkeypatch.setattr(registry, "supports_subagent_registration_event", lambda: True)

    spec = build_member_deep_agent_spec(
        {},
        "code.team",
        "teammate",
        DeepAgentSpec(),
    )

    assert spec.subagent_registration_mode == registry.SUBAGENT_REGISTRATION_MODE
    assert spec.event_subagent_types == list(registry.EVENT_SUBAGENT_TYPES)

# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""AgentGroup conversion must publish a complete roster or leave it intact."""

from pathlib import Path

import pytest

from openjiuwen.agent_teams.schema.blueprint import LeaderSpec, TeamAgentSpec
from openjiuwen.agent_teams.schema.deep_agent_spec import DeepAgentSpec
from openjiuwen.core.single_agent.schema.agent_card import AgentCard
from openjiuwen.harness.schema.extension_spec import (
    AgentTemplateSpec,
    PromptSectionSpec,
)

from jiuwenswarm.agents.swarm import assembly
from jiuwenswarm.agents.swarm.agent_group import LoadedAgentGroupPackage


@pytest.fixture
def roster():
    return TeamAgentSpec(
        agents={
            "leader": DeepAgentSpec(skills=["leader-skill"]),
            "teammate": DeepAgentSpec(skills=["member-skill"]),
        },
        team_name="atomic-group",
        leader=LeaderSpec(member_name="team_leader", prompt="Base instruction"),
    )


@pytest.fixture
def package(tmp_path: Path):
    templates = {}
    for name in ("leader", "first", "second"):
        templates[name] = AgentTemplateSpec(
            agent_card=AgentCard(id=name, name=name, description=f"{name} role"),
            prompt_sections=[
                PromptSectionSpec(name="persona", content={"en": f"{name} instruction"})
            ],
        )
    return LoadedAgentGroupPackage(tmp_path, {"name": "experts"}, templates)


@pytest.mark.parametrize("failed_member", ["first", "second"])
def test_conversion_failure_preserves_every_original_field(
    roster, package, monkeypatch, failed_member
):
    original_agents = roster.agents
    original_leader = roster.leader
    original_members = roster.predefined_members
    before = roster.model_dump(mode="json")

    def convert(template):
        if template.agent_card.id == failed_member:
            raise ValueError("invalid external runtime")
        return None

    monkeypatch.setattr(assembly, "external_cli_agent_spec_from_template", convert)
    with pytest.raises(ValueError, match="invalid external runtime"):
        assembly._apply_agent_group(roster, "experts", package)

    assert roster.model_dump(mode="json") == before
    assert roster.agents is original_agents
    assert roster.leader is original_leader
    assert roster.predefined_members is original_members
    assert "first" not in roster.agents


def test_retry_after_failure_matches_clean_assembly(roster, package, monkeypatch):
    clean = roster.model_copy(deep=True)
    calls = []

    def fail_once(template):
        calls.append(template.agent_card.id)
        if len(calls) == 2:
            raise ValueError("temporary conversion failure")
        return None

    monkeypatch.setattr(assembly, "external_cli_agent_spec_from_template", fail_once)
    with pytest.raises(ValueError, match="temporary conversion failure"):
        assembly._apply_agent_group(roster, "experts", package)
    assembly._apply_agent_group(roster, "experts", package)
    assembly._apply_agent_group(clean, "experts", package)

    assert roster.model_dump(mode="json") == clean.model_dump(mode="json")
    assert roster.leader.prompt.count("leader instruction") == 1
    assert [member.member_name for member in roster.predefined_members] == [
        "first",
        "second",
    ]


def test_success_preserves_base_specs_and_template_prompts(roster, package):
    original_agents = roster.agents
    original_leader = roster.leader
    leader_before = original_agents["leader"].model_dump(mode="json")
    teammate_before = original_agents["teammate"].model_dump(mode="json")
    templates_before = {
        name: template.model_dump(mode="json")
        for name, template in package.templates.items()
    }

    assembly._apply_agent_group(roster, "experts", package)

    assert roster.team_mode == "hybrid"
    assert roster.agents is not original_agents
    assert roster.leader is not original_leader
    assert original_leader.prompt == "Base instruction"
    assert roster.leader.prompt == "Base instruction\n\nleader instruction"
    assert original_agents["leader"].model_dump(mode="json") == leader_before
    assert original_agents["teammate"].model_dump(mode="json") == teammate_before
    assert set(original_agents) == {"leader", "teammate"}
    assert set(roster.agents) == {"leader", "teammate", "first", "second"}
    for name in ("first", "second"):
        assert roster.agents[name] is not original_agents["teammate"]
        assert roster.agents[name].skills == ["member-skill"]
        assert roster.agents[name].agent_template_spec["prompt_sections"] == []
    assert {
        name: template.model_dump(mode="json")
        for name, template in package.templates.items()
    } == templates_before
    assert [member.prompt for member in roster.predefined_members] == [
        "first instruction",
        "second instruction",
    ]


@pytest.mark.parametrize("missing_role", ["leader", "teammate"])
def test_missing_base_role_does_not_change_roster(roster, package, missing_role):
    del roster.agents[missing_role]
    before = roster.model_dump(mode="json")

    with pytest.raises(ValueError, match="requires both base"):
        assembly._apply_agent_group(roster, "experts", package)

    assert roster.model_dump(mode="json") == before


def test_display_name_failure_does_not_publish_staged_members(
    roster, package, monkeypatch
):
    before = roster.model_dump(mode="json")

    def display_name(package_dir, member_name, *, fallback):
        if member_name == "second":
            raise OSError("manifest unavailable")
        return fallback

    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.extension_package_manager."
        "resolve_agent_group_member_display_name",
        display_name,
    )
    with pytest.raises(OSError, match="manifest unavailable"):
        assembly._apply_agent_group(roster, "experts", package)

    assert roster.model_dump(mode="json") == before

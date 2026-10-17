# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The minimal AgentGroup fixture assembles into a swarmflow expert table end to end."""
from __future__ import annotations

from pathlib import Path

import pytest
from openjiuwen.agent_teams.schema.blueprint import TeamAgentSpec
from openjiuwen.agent_teams.schema.deep_agent_spec import DeepAgentSpec

from jiuwenswarm.agents.swarm.agent_group import load_agent_group_package_bundle
from jiuwenswarm.agents.swarm.assembly import _apply_agent_group

FIXTURE = (
    Path(__file__).resolve().parents[3]
    / "fixtures"
    / "agent_groups"
    / "swarmflow-expert-fixture"
)


def _prompt_text(template) -> str:
    """Flatten every localized prompt section value into one lowercase blob."""
    return " ".join(
        str(value).lower()
        for section in template.prompt_sections
        for value in (section.content or {}).values()
    )


def test_fixture_loads_leader_and_echo_expert():
    bundle = load_agent_group_package_bundle(FIXTURE)
    assert set(bundle.templates) == {"leader", "echo"}

    echo = bundle.templates["echo"]
    assert [tool.params["class_name"] for tool in echo.tools] == ["EchoTool"]
    assert [tool.type for tool in echo.tools] == ["harness.tool.file"]
    assert [rail.params["class_name"] for rail in echo.rails] == ["TicketRail"]
    assert [rail.type for rail in echo.rails] == ["harness.rail.file"]
    assert Path(echo.tools[0].params["file_path"]).is_file()
    assert Path(echo.rails[0].params["file_path"]).is_file()
    assert "echo expert" in _prompt_text(echo)


def test_fixture_assembles_into_expert_table():
    # The expert table lives in the engine; the fixture itself must stay loadable
    # (the two tests above) even on an engine pin without this module.
    expert_specs = pytest.importorskip(
        "openjiuwen.agent_teams.workflow.backends.expert_specs"
    )
    spec = TeamAgentSpec(
        team_name="fixture-team",
        enable_swarmflow=True,
        agents={"leader": DeepAgentSpec(), "teammate": DeepAgentSpec(tools=[])},
    )

    _apply_agent_group(
        spec, "swarmflow-expert-fixture", load_agent_group_package_bundle(FIXTURE)
    )
    experts = expert_specs.derive_swarmflow_expert_specs(spec, [])

    # The leader coordinates, so it is never offered as a pickable expert.
    assert set(experts) == {"echo"}
    entry = experts["echo"]
    assert entry.display_name == "回声专家"
    assert "echo expert" in entry.persona.lower()
    # Generic teammate tools are cleared; capabilities come from the snapshot.
    assert entry.spec.tools == []
    snapshot = entry.spec.agent_template_spec
    assert snapshot["tools"] and snapshot["rails"]


def test_fixture_is_not_on_the_user_shelf():
    shelf = (
        Path(__file__).resolve().parents[4]
        / "jiuwenswarm"
        / "resources"
        / "agent"
        / "workspace"
        / "plugins"
        / "agent_groups"
    )
    assert not (shelf / "swarmflow-expert-fixture").exists()

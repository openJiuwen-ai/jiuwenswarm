# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for the team deliverable location rail and its provider gating."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from jiuwenswarm.agents.harness.team.rails.team_deliverable_location_rail import (
    TeamDeliverableLocationRail,
)
from jiuwenswarm.agents.swarm.context import SwarmBuildContext
from jiuwenswarm.agents.swarm.providers import member_rails


class _FakePromptBuilder:
    def __init__(self) -> None:
        self.language = "cn"
        self.sections = {}

    def add_section(self, section) -> None:
        self.sections[section.name] = section

    def remove_section(self, name: str) -> None:
        self.sections.pop(name, None)


def _build_agent(builder: _FakePromptBuilder):
    return SimpleNamespace(system_prompt_builder=builder)


def test_provider_returns_none_without_project_dir() -> None:
    """The rail is skipped when the session has no resolved project directory."""
    ctx = SwarmBuildContext(project_dir=None)

    assert member_rails._build_team_deliverable_location_rail({}, ctx) is None


def test_provider_builds_rail_with_project_dir() -> None:
    """The rail is built when the session carries a project directory."""
    ctx = SwarmBuildContext(project_dir="D:/task-workspace")

    rail = member_rails._build_team_deliverable_location_rail({}, ctx)

    assert isinstance(rail, TeamDeliverableLocationRail)


@pytest.mark.asyncio
async def test_rail_injects_section_with_paths() -> None:
    """The injected section names the task workspace and the private workspace."""
    rail = TeamDeliverableLocationRail(
        project_dir="D:/task-workspace",
        member_workspace_root="C:/runtime-data/.agent_teams/t/workspaces/m_workspace",
        language="cn",
    )
    builder = _FakePromptBuilder()
    rail.init(_build_agent(builder))

    await rail.before_model_call(SimpleNamespace(inputs={}))

    section = builder.sections[TeamDeliverableLocationRail.SECTION_NAME]
    assert section.priority == TeamDeliverableLocationRail.SECTION_PRIORITY
    assert section.priority > 67  # after team_workspace_report_paths
    cn = section.content["cn"]
    en = section.content["en"]
    assert "D:/task-workspace" in cn
    assert "C:/runtime-data/.agent_teams/t/workspaces/m_workspace" in cn
    assert "以本节为准" in cn  # explicit override of the .team/ default
    assert "D:/task-workspace" in en
    assert "this section wins" in en


@pytest.mark.asyncio
async def test_rail_works_without_member_workspace() -> None:
    """The private-workspace contrast line is optional."""
    rail = TeamDeliverableLocationRail(project_dir="D:/task-workspace")
    builder = _FakePromptBuilder()
    rail.init(_build_agent(builder))

    await rail.before_model_call(SimpleNamespace(inputs={}))

    section = builder.sections[TeamDeliverableLocationRail.SECTION_NAME]
    assert "D:/task-workspace" in section.content["cn"]


@pytest.mark.asyncio
async def test_uninit_removes_section() -> None:
    """uninit removes the injected section from the prompt builder."""
    rail = TeamDeliverableLocationRail(project_dir="D:/task-workspace")
    builder = _FakePromptBuilder()
    agent = _build_agent(builder)
    rail.init(agent)
    await rail.before_model_call(SimpleNamespace(inputs={}))
    assert TeamDeliverableLocationRail.SECTION_NAME in builder.sections

    rail.uninit(agent)

    assert TeamDeliverableLocationRail.SECTION_NAME not in builder.sections


@pytest.mark.asyncio
async def test_report_path_rail_appends_deliverable_paths_with_project_dir() -> None:
    """The report-path rail shows the task-workspace deliverable example."""
    from jiuwenswarm.agents.harness.team.rails.team_workspace_report_path_rail import (
        TeamWorkspaceReportPathRail,
    )

    ctx = SwarmBuildContext(
        team_ws_root="C:/runtime-data/.agent_teams/t/team-workspace",
        team_id="t",
        project_dir="D:/task-workspace",
    )

    rail = member_rails._build_team_workspace_report_path_rail({}, ctx)

    assert isinstance(rail, TeamWorkspaceReportPathRail)
    builder = _FakePromptBuilder()
    rail.init(_build_agent(builder))
    await rail.before_model_call(SimpleNamespace(inputs={}))
    content = builder.sections["team_workspace_report_paths"].content["cn"]
    # The team-workspace mount rules stay, and the task workspace is added.
    assert str(Path("C:/runtime-data/.agent_teams/t/team-workspace")) in content
    assert str(Path("D:/task-workspace")) in content


@pytest.mark.asyncio
async def test_report_path_rail_unchanged_without_project_dir() -> None:
    """Without a project directory the report-path section keeps its old shape."""
    ctx = SwarmBuildContext(
        team_ws_root="C:/runtime-data/.agent_teams/t/team-workspace",
        team_id="t",
        project_dir=None,
    )

    rail = member_rails._build_team_workspace_report_path_rail({}, ctx)

    builder = _FakePromptBuilder()
    rail.init(_build_agent(builder))
    await rail.before_model_call(SimpleNamespace(inputs={}))
    content = builder.sections["team_workspace_report_paths"].content["cn"]
    assert "Deliverables in the Task Working Directory" not in content

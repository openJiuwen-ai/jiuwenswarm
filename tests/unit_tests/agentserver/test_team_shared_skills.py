# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for team shared skills link logic."""

# pylint: disable=protected-access

import os
import shutil
from pathlib import Path

import pytest
from openjiuwen.core.single_agent.rail.base import ToolCallInputs
from openjiuwen.agent_teams.schema.blueprint import TeamAgentSpec

from jiuwenswarm.agents.harness.team.rails.team_shared_skill_link_refresh_rail import (
    TeamSharedSkillLinkRefreshRail,
)
from jiuwenswarm.agents.harness.team.team_manager import TeamManager
from jiuwenswarm.agents.harness.team.team_skill_links import remove_skill_dir_link


def _assert_link_points_to(path: Path, target: Path) -> None:
    """Assert that a link or junction resolves to the expected target."""
    assert path.exists()
    assert path.resolve() == target.resolve()


@pytest.fixture
def inline_link_sync(monkeypatch):
    """Run link sync inline even inside a running loop, for deterministic tests.

    Production offloads the sync to a worker thread when called on the event
    loop (BUG20260918365771); asserting immediately after a refresh call would
    then race the executor thread.
    """
    from jiuwenswarm.agents.harness.team import team_skill_links

    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.team_manager.offload_link_sync",
        lambda source, target: team_skill_links.sync_skill_dir_links(source, target),
    )


@pytest.mark.asyncio
async def test_ensure_team_shared_skills_initialized_links_global_skills(tmp_path, monkeypatch):
    """Global skills should be linked to team shared directory via the public helper."""
    # Create global skills directory
    global_skills_dir = tmp_path / "global_skills"
    global_skills_dir.mkdir(parents=True)
    for skill_name in ("skill-a", "skill-b"):
        skill_dir = global_skills_dir / skill_name
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(f"---\nname: {skill_name}\n---\n", encoding="utf-8")

    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.team_manager.get_agent_skills_dir",
        lambda: global_skills_dir,
    )

    # Create team workspace config
    team_workspace = tmp_path / "team_workspace"
    team_workspace.mkdir(parents=True)
    team_shared_skills = team_workspace / "skills"

    # Build TeamAgentSpec with custom workspace path
    spec = TeamAgentSpec.model_validate(
        {
            "team_name": "demo_team",
            "agents": {
                "leader": {},
                "teammate": {},
            },
            "workspace": {"root_path": str(team_workspace), "enabled": True},
        }
    )

    manager = TeamManager()
    await manager.ensure_team_shared_skills_initialized(spec)

    # The skills root stays a normal directory; individual skills are linked.
    assert team_shared_skills.is_dir()
    assert not team_shared_skills.is_symlink()
    _assert_link_points_to(team_shared_skills / "skill-a", global_skills_dir / "skill-a")
    _assert_link_points_to(team_shared_skills / "skill-b", global_skills_dir / "skill-b")
    assert not (team_shared_skills / "skills_state.json").exists()


@pytest.mark.asyncio
async def test_existing_skill_entry_is_not_replaced(tmp_path, monkeypatch):
    """Existing skill entries should be left untouched."""
    global_skills_dir = tmp_path / "global_skills"
    global_skills_dir.mkdir(parents=True)
    skill_dir = global_skills_dir / "skill-a"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("---\nname: skill-a\n---\n", encoding="utf-8")
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.team_manager.get_agent_skills_dir",
        lambda: global_skills_dir,
    )

    team_workspace = tmp_path / "team_workspace"
    team_workspace.mkdir(parents=True)
    team_shared_skills = team_workspace / "skills"
    team_shared_skills.mkdir(parents=True)
    existing_skill = team_shared_skills / "skill-a"
    existing_skill.mkdir()
    (existing_skill / "SKILL.md").write_text("---\nname: existing-skill-a\n---\n", encoding="utf-8")

    spec = TeamAgentSpec.model_validate(
        {
            "team_name": "demo_team",
            "agents": {"leader": {}, "teammate": {}},
            "workspace": {"root_path": str(team_workspace), "enabled": True},
        }
    )

    manager = TeamManager()
    await manager.ensure_team_shared_skills_initialized(spec)

    assert (team_shared_skills / "skill-a").resolve() == existing_skill.resolve()
    assert "existing-skill-a" in (team_shared_skills / "skill-a" / "SKILL.md").read_text(encoding="utf-8")


def test_refresh_team_shared_skill_links_adds_new_global_skill(tmp_path, monkeypatch):
    """Refreshing shared links should add newly installed global skills."""
    global_skills_dir = tmp_path / "global_skills"
    global_skills_dir.mkdir(parents=True)
    skill_a = global_skills_dir / "skill-a"
    skill_a.mkdir()
    (skill_a / "SKILL.md").write_text("---\nname: skill-a\n---\n", encoding="utf-8")

    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.team_manager.get_agent_skills_dir",
        lambda: global_skills_dir,
    )

    team_shared_skills = tmp_path / "team_workspace" / "skills"
    manager = TeamManager()
    manager.register_team_shared_skill_link_target("sess-1", team_shared_skills)

    assert manager.refresh_team_shared_skill_links("sess-1")
    _assert_link_points_to(team_shared_skills / "skill-a", skill_a)

    skill_b = global_skills_dir / "skill-b"
    skill_b.mkdir()
    (skill_b / "SKILL.md").write_text("---\nname: skill-b\n---\n", encoding="utf-8")

    assert manager.refresh_team_shared_skill_links("sess-1")
    _assert_link_points_to(team_shared_skills / "skill-b", skill_b)


@pytest.mark.asyncio
async def test_ensure_team_shared_skills_ready_for_session_registers_refresh_target(tmp_path, monkeypatch, inline_link_sync):
    """Session readiness should initialize links and register the refresh target."""
    global_skills_dir = tmp_path / "global_skills"
    global_skills_dir.mkdir(parents=True)
    skill_a = global_skills_dir / "skill-a"
    skill_a.mkdir()
    (skill_a / "SKILL.md").write_text("---\nname: skill-a\n---\n", encoding="utf-8")

    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.team_manager.get_agent_skills_dir",
        lambda: global_skills_dir,
    )

    team_workspace = tmp_path / "team_workspace"
    spec = TeamAgentSpec.model_validate(
        {
            "team_name": "demo_team",
            "agents": {"leader": {}, "teammate": {}},
            "workspace": {"root_path": str(team_workspace), "enabled": True},
        }
    )
    manager = TeamManager()

    await manager.ensure_team_shared_skills_ready_for_session("sess-1", spec)

    team_shared_skills = team_workspace / "skills"
    _assert_link_points_to(team_shared_skills / "skill-a", skill_a)

    skill_b = global_skills_dir / "skill-b"
    skill_b.mkdir()
    (skill_b / "SKILL.md").write_text("---\nname: skill-b\n---\n", encoding="utf-8")

    assert manager.refresh_team_shared_skill_links("sess-1")
    _assert_link_points_to(team_shared_skills / "skill-b", skill_b)


def test_refresh_team_shared_skill_links_prunes_removed_global_skill(tmp_path, monkeypatch):
    """Refreshing shared links should remove links for uninstalled global skills."""
    global_skills_dir = tmp_path / "global_skills"
    global_skills_dir.mkdir(parents=True)
    skill_a = global_skills_dir / "skill-a"
    skill_a.mkdir()
    (skill_a / "SKILL.md").write_text("---\nname: skill-a\n---\n", encoding="utf-8")
    skill_b = global_skills_dir / "skill-b"
    skill_b.mkdir()
    (skill_b / "SKILL.md").write_text("---\nname: skill-b\n---\n", encoding="utf-8")

    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.team_manager.get_agent_skills_dir",
        lambda: global_skills_dir,
    )

    team_shared_skills = tmp_path / "team_workspace" / "skills"
    manager = TeamManager()
    manager.register_team_shared_skill_link_target("sess-1", team_shared_skills)

    assert manager.refresh_team_shared_skill_links("sess-1")
    _assert_link_points_to(team_shared_skills / "skill-a", skill_a)
    _assert_link_points_to(team_shared_skills / "skill-b", skill_b)

    shutil.rmtree(skill_b)

    assert manager.refresh_team_shared_skill_links("sess-1")
    _assert_link_points_to(team_shared_skills / "skill-a", skill_a)
    assert not os.path.lexists(team_shared_skills / "skill-b")


def test_remove_skill_dir_link_keeps_ordinary_directory(tmp_path):
    """Removing a skill link should not delete ordinary directories."""
    ordinary_skill_dir = tmp_path / "ordinary-skill"
    ordinary_skill_dir.mkdir()
    (ordinary_skill_dir / "SKILL.md").write_text("---\nname: ordinary-skill\n---\n", encoding="utf-8")

    remove_skill_dir_link(ordinary_skill_dir)

    assert ordinary_skill_dir.is_dir()
    assert (ordinary_skill_dir / "SKILL.md").is_file()


@pytest.mark.asyncio
async def test_team_shared_skill_link_refresh_rail_refreshes_after_global_skill_write(tmp_path, monkeypatch):
    """The after-tool rail should refresh only when write tools touch global skills."""
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.rails.team_shared_skill_link_refresh_rail.get_cwd",
        lambda: str(tmp_path),
    )
    global_skills_dir = tmp_path / "global_skills"
    skill_dir = global_skills_dir / "skill-a"
    skill_dir.mkdir(parents=True)
    skill_md = skill_dir / "SKILL.md"
    skill_md.write_text("---\nname: skill-a\n---\n", encoding="utf-8")
    refresh_calls = []

    rail = TeamSharedSkillLinkRefreshRail(
        global_skills_dir=global_skills_dir,
        refresh_links=lambda: refresh_calls.append("refresh"),
    )
    ctx = type(
        "_Ctx",
        (),
        {
            "inputs": ToolCallInputs(
                tool_name="write_file",
                tool_args={"file_path": str(skill_md.relative_to(tmp_path))},
            )
        },
    )()

    await rail.after_tool_call(ctx)

    assert refresh_calls == ["refresh"]

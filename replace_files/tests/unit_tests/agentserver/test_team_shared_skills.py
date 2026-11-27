# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for team shared skills copying logic."""

# pylint: disable=protected-access

import json
from pathlib import Path

from openjiuwen.agent_teams.schema.blueprint import TeamAgentSpec

from jiuwenclaw.agentserver.team.team_manager import TeamManager


def test_copy_global_skills_to_team_shared_dir(tmp_path, monkeypatch):
    """Global skills should be copied to team shared directory via _copy_global_skills_to_team_shared_dir."""
    # Create global skills directory
    global_skills_dir = tmp_path / "global_skills"
    global_skills_dir.mkdir(parents=True)
    for skill_name in ("skill-a", "skill-b"):
        skill_dir = global_skills_dir / skill_name
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(f"---\nname: {skill_name}\n---\n", encoding="utf-8")

    # Create global skills_state.json
    (global_skills_dir / "skills_state.json").write_text(
        json.dumps({
            "marketplaces": [{"name": "demo", "url": "https://example.com", "enabled": True}],
            "installed_plugins": [{"name": "skill-a", "source": "demo"}],
            "local_skills": [{"name": "skill-b", "source": "local"}],
        }),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        "jiuwenclaw.utils.get_agent_skills_dir",
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

    # Call _copy_global_skills_to_team_shared_dir method directly
    manager = TeamManager()
    manager._copy_global_skills_to_team_shared_dir(spec)

    # Verify marker file exists in team shared skills directory
    assert (team_shared_skills / ".team_skills_copied").exists()

    # Verify skills and skills_state.json are copied
    assert (team_shared_skills / "skill-a" / "SKILL.md").exists()
    assert (team_shared_skills / "skill-b" / "SKILL.md").exists()
    assert (team_shared_skills / "skills_state.json").exists()

    # Verify skills_state.json content is correct
    shared_state = json.loads((team_shared_skills / "skills_state.json").read_text(encoding="utf-8"))
    assert shared_state["marketplaces"] == [{"name": "demo", "url": "https://example.com", "enabled": True}]


def test_copy_global_skills_not_copied_twice(tmp_path, monkeypatch):
    """Second call to _copy_global_skills_to_team_shared_dir should not copy again."""
    global_skills_dir = tmp_path / "global_skills"
    global_skills_dir.mkdir(parents=True)
    skill_dir = global_skills_dir / "skill-a"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("---\nname: skill-a\n---\n", encoding="utf-8")
    (global_skills_dir / "skills_state.json").write_text("{}", encoding="utf-8")

    monkeypatch.setattr(
        "jiuwenclaw.utils.get_agent_skills_dir",
        lambda: global_skills_dir,
    )

    team_workspace = tmp_path / "team_workspace"
    team_workspace.mkdir(parents=True)
    team_shared_skills = team_workspace / "skills"
    team_shared_skills.mkdir(parents=True)
    # Pre-create marker file to simulate already copied
    (team_shared_skills / ".team_skills_copied").write_text("", encoding="utf-8")

    spec = TeamAgentSpec.model_validate(
        {
            "team_name": "demo_team",
            "agents": {"leader": {}, "teammate": {}},
            "workspace": {"root_path": str(team_workspace), "enabled": True},
        }
    )

    manager = TeamManager()
    manager._copy_global_skills_to_team_shared_dir(spec)

    # Verify no new skill copied (marker file already exists)
    assert not (team_shared_skills / "skill-a").exists()


def test_member_configured_skills_copied_to_own_dir(tmp_path, monkeypatch):
    """Member-configured skills should be copied to member's own skills directory."""
    global_skills_dir = tmp_path / "global_skills"
    global_skills_dir.mkdir(parents=True)
    for skill_name in ("skill-a", "skill-b", "skill-c"):
        skill_dir = global_skills_dir / skill_name
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(f"---\nname: {skill_name}\n---\n", encoding="utf-8")
    (global_skills_dir / "skills_state.json").write_text(
        json.dumps({
            "marketplaces": [],
            "installed_plugins": [],
            "local_skills": [],
        }),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        "jiuwenclaw.utils.get_agent_skills_dir",
        lambda: global_skills_dir,
    )
    monkeypatch.setattr(
        "jiuwenclaw.agentserver.team.team_runtime_inheritance.build_member_rails",
        lambda **kwargs: [],
    )
    monkeypatch.setattr(
        "jiuwenclaw.agentserver.extensions.rail_manager.get_rail_manager",
        lambda: type(
            "_DummyRailManager",
            (),
            {
                "get_registered_rail_names": lambda self: [],
                "load_rail_instance_without_enabled_check": lambda self, name: None,
            },
        )(),
    )

    # Create team shared directory (simulate global skills already copied)
    team_workspace = tmp_path / "team_workspace"
    team_workspace.mkdir(parents=True)
    team_shared_skills = team_workspace / "skills"
    team_shared_skills.mkdir(parents=True)
    # Simulate global skills copied to team shared directory
    for skill_name in ("skill-a", "skill-b", "skill-c"):
        skill_dir = team_shared_skills / skill_name
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(f"---\nname: {skill_name}\n---\n", encoding="utf-8")
    (team_shared_skills / ".team_skills_copied").write_text("", encoding="utf-8")

    member_root = tmp_path / "member_workspace"
    member_root.mkdir(parents=True)
    (member_root / "skills").mkdir(parents=True)

    spec = TeamAgentSpec.model_validate(
        {
            "team_name": "demo_team",
            "agents": {
                "leader": {},
                "member_a": {"skills": ["skill-a"]},  # Only configure skill-a
            },
            "workspace": {"root_path": str(team_workspace), "enabled": True},
        }
    )

    customizer = TeamManager.build_agent_customizer(
        spec=spec,
        deep_agent=type("_DeepAgent", (), {
            "deep_config": type("_Config", (), {"sys_operation": None})(),
            "ability_manager": type("_AbilityManager", (), {"list": lambda self: []})(),
        })(),
        session_id="session-1",
    )

    agent = type("_Agent", (), {
        "deep_config": type("_Config", (), {
            "workspace": type("_Workspace", (), {"root_path": str(member_root)})(),
            "sys_operation": None,
        })(),
        "ability_manager": type("_AbilityManager", (), {"list": lambda self: [], "add": lambda self, card: None})(),
        "card": type("_Card", (), {"id": "member_a", "name": "member"})(),
        "add_rail": lambda self, rail: None,
    })()

    customizer(agent, member_name="member_a", role="teammate")

    # Member directory only contains configured skill-a
    member_skills_dir = member_root / "skills"
    assert (member_skills_dir / "skill-a").exists()
    assert not (member_skills_dir / "skill-b").exists()
    assert not (member_skills_dir / "skill-c").exists()

    # Verify member directory's skills_state.json only contains skill-a
    member_state = json.loads((member_skills_dir / "skills_state.json").read_text(encoding="utf-8"))
    installed_names = [p["name"] for p in member_state["installed_plugins"]]
    assert installed_names == ["skill-a"]


def test_member_no_configured_skills_has_state_file(tmp_path, monkeypatch):
    """When member has no configured skills, member directory should still have skills_state.json (empty)."""
    global_skills_dir = tmp_path / "global_skills"
    global_skills_dir.mkdir(parents=True)
    for skill_name in ("skill-a", "skill-b", "skill-c"):
        skill_dir = global_skills_dir / skill_name
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(f"---\nname: {skill_name}\n---\n", encoding="utf-8")
    (global_skills_dir / "skills_state.json").write_text(
        json.dumps({
            "marketplaces": [],
            "installed_plugins": [],
            "local_skills": [],
        }),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        "jiuwenclaw.utils.get_agent_skills_dir",
        lambda: global_skills_dir,
    )
    monkeypatch.setattr(
        "jiuwenclaw.agentserver.team.team_runtime_inheritance.build_member_rails",
        lambda **kwargs: [],
    )
    monkeypatch.setattr(
        "jiuwenclaw.agentserver.extensions.rail_manager.get_rail_manager",
        lambda: type("_DummyRailManager", (), {
            "get_registered_rail_names": lambda self: [],
            "load_rail_instance_without_enabled_check": lambda self, name: None,
        })(),
    )

    # Create team shared directory
    team_workspace = tmp_path / "team_workspace"
    team_workspace.mkdir(parents=True)
    team_shared_skills = team_workspace / "skills"
    team_shared_skills.mkdir(parents=True)
    (team_shared_skills / ".team_skills_copied").write_text("", encoding="utf-8")

    member_root = tmp_path / "member_workspace"
    member_root.mkdir(parents=True)
    (member_root / "skills").mkdir(parents=True)

    spec = TeamAgentSpec.model_validate(
        {
            "team_name": "demo_team",
            "agents": {
                "leader": {},
                "member_a": {},  # No skills configured
            },
            "workspace": {"root_path": str(team_workspace), "enabled": True},
        }
    )

    customizer = TeamManager.build_agent_customizer(
        spec=spec,
        deep_agent=type("_DeepAgent", (), {
            "deep_config": type("_Config", (), {"sys_operation": None})(),
            "ability_manager": type("_AbilityManager", (), {"list": lambda self: []})(),
        })(),
        session_id="session-1",
    )

    agent = type("_Agent", (), {
        "deep_config": type("_Config", (), {
            "workspace": type("_Workspace", (), {"root_path": str(member_root)})(),
            "sys_operation": None,
        })(),
        "ability_manager": type("_AbilityManager", (), {"list": lambda self: [], "add": lambda self, card: None})(),
        "card": type("_Card", (), {"id": "member_a", "name": "member"})(),
        "add_rail": lambda self, rail: None,
    })()

    customizer(agent, member_name="member_a", role="teammate")

    # Member directory should not have any skill (not configured)
    member_skills_dir = member_root / "skills"
    assert not (member_skills_dir / "skill-a").exists()
    assert not (member_skills_dir / "skill-b").exists()
    assert not (member_skills_dir / "skill-c").exists()
    # Member directory should have skills_state.json (empty)
    assert (member_skills_dir / "skills_state.json").exists()
    member_state = json.loads((member_skills_dir / "skills_state.json").read_text(encoding="utf-8"))
    assert member_state["installed_plugins"] == []
    assert member_state["local_skills"] == []
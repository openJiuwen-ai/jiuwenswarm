# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for team member skill state generation."""

import json
from pathlib import Path

from openjiuwen.agent_teams.schema.blueprint import TeamAgentSpec

from jiuwenclaw.agentserver.team.team_manager import TeamManager


def test_member_skill_state_inherits_marketplaces_and_rebuilds_installed_skills(monkeypatch, tmp_path):
    """Member workspace state should keep marketplaces but only include copied skills."""
    global_skills_dir = tmp_path / "global_skills"
    global_skills_dir.mkdir(parents=True)
    for skill_name in ("skill-a", "skill-b"):
        skill_dir = global_skills_dir / skill_name
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(f"---\ndescription: {skill_name}\n---\n", encoding="utf-8")

    (global_skills_dir / "skills_state.json").write_text(
        """
{
  "marketplaces": [{"name": "demo", "url": "https://example.com/demo.git", "enabled": true}],
  "installed_plugins": [
    {"name": "skill-a", "marketplace": "demo", "version": "1.0.0", "source": "demo"},
    {"name": "skill-b", "marketplace": "demo", "version": "1.0.0", "source": "demo"}
  ],
  "local_skills": [
    {"name": "skill-a", "origin": "/tmp/skill-a", "source": "demo"},
    {"name": "skill-b", "origin": "/tmp/skill-b", "source": "demo"}
  ]
}
        """.strip(),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        "jiuwenclaw.utils.get_agent_skills_dir",
        lambda: global_skills_dir,
    )
    monkeypatch.setattr(
        "jiuwenclaw.agentserver.team.team_runtime_inheritance.build_member_rails",
        lambda config: [],
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

    spec = TeamAgentSpec.model_validate(
        {
            "team_name": "demo_team",
            "agents": {
                "leader": {},
                "member_a": {"skills": ["skill-a"]},
            },
        }
    )
    customizer = TeamManager.build_agent_customizer(
        spec=spec,
        deep_agent=type(
            "_DeepAgent",
            (),
            {
                "deep_config": type("_Config", (), {"sys_operation": None})(),
                "ability_manager": type("_AbilityManager", (), {"list": lambda self: []})(),
            },
        )(),
        session_id="session-1",
        request_id=None,
        channel_id=None,
        request_metadata=None,
    )

    member_root = tmp_path / "member_workspace"
    agent = type(
        "_Agent",
        (),
        {
            "deep_config": type(
                "_Config",
                (),
                {"workspace": type("_Workspace", (), {"root_path": str(member_root)})(), "sys_operation": None},
            )(),
            "ability_manager": type(
                "_AbilityManager",
                (),
                {
                    "list": lambda self: [],
                    "add": lambda self, card: None,
                },
            )(),
            "card": type("_Card", (), {"id": "member_a", "name": "member"})(),
            "add_rail": lambda self, rail: None,
        },
    )()

    customizer(agent, member_name="member_a", role="teammate")

    state_path = member_root / "skills" / "skills_state.json"
    assert state_path.is_file()

    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["marketplaces"] == [
        {"name": "demo", "url": "https://example.com/demo.git", "enabled": True}
    ]
    assert [plugin["name"] for plugin in state["installed_plugins"]] == ["skill-a"]
    assert [skill["name"] for skill in state["local_skills"]] == ["skill-a"]
    assert Path(state["local_skills"][0]["origin"]).name == "skill-a"

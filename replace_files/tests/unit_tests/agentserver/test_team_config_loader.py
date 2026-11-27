# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for team config loading."""

from pathlib import Path

from jiuwenclaw.agentserver.team.config_loader import (
    load_team_spec_dict,
    resolve_team_sqlite_db_path,
)


def test_load_team_spec_dict_supports_member_specific_agents(monkeypatch, tmp_path):
    """Predefined members should resolve to member_name-keyed DeepAgentSpec entries."""
    fake_agent_teams_home = tmp_path / ".agent_teams"
    config = {
        "models": {
            "default": {
                "model_client_config": {
                    "model_name": "gpt-test",
                    "client_provider": "openai",
                },
                "model_config_obj": {"temperature": 0.2},
            }
        },
        "team": {
            "team_name": "demo_team",
            "leader": {
                "member_name": "team_leader",
                "display_name": "TeamLeader",
                "persona": "Lead the team",
            },
            "workspace": {
                "enabled": True,
                "artifact_dirs": ["artifacts/reports"],
            },
            "agents": {
                "leader": {
                },
                "teammate": {
                },
                "analyst": {
                    "name": "Analyst",
                    "skills": ["skill-a", "skill-b"],
                },
            },
            "predefined_members": [
                {
                    "member_name": "analyst",
                    "display_name": "Data Analyst",
                    "persona": "Analyze data",
                    "prompt_hint": "Focus on trends",
                    "toolkits": ["sql", "python"],
                }
            ],
            "storage": {
                "type": "sqlite",
                "params": {
                    "connection_string": "team.db",
                },
            },
            "planning": {
                "enabled": True,
                "max_parallel_tasks": 3,
            },
        },
    }

    monkeypatch.setattr(
        "jiuwenclaw.agentserver.team.config_loader.get_config",
        lambda: config,
    )
    monkeypatch.setattr(
        "jiuwenclaw.agentserver.team.config_loader.get_agent_teams_home",
        lambda: fake_agent_teams_home,
    )

    spec = load_team_spec_dict("session-1")

    assert spec["team_name"] == "demo_team_session-1"
    assert spec["leader"]["member_name"] == "team_leader"
    assert spec["leader"]["display_name"] == "TeamLeader"
    assert spec["leader"]["persona"] == "Lead the team"
    assert spec["predefined_members"][0]["member_name"] == "analyst"
    assert spec["predefined_members"][0]["display_name"] == "Data Analyst"
    assert spec["predefined_members"][0]["prompt_hint"] == "Focus on trends"
    assert spec["predefined_members"][0]["toolkits"] == ["sql", "python"]
    assert spec["workspace"]["enabled"] is True
    assert spec["workspace"]["artifact_dirs"] == ["artifacts/reports"]
    assert spec["planning"] == {
        "enabled": True,
        "max_parallel_tasks": 3,
    }
    assert spec["agents"]["analyst"]["skills"] == ["skill-a", "skill-b"]
    assert spec["agents"]["analyst"]["model"]["model_request_config"]["model"] == "gpt-test"
    assert spec["agents"]["analyst"]["workspace"] == {"stable_base": True}
    assert spec["storage"]["params"]["connection_string"] == str(
        fake_agent_teams_home / "team.db"
    )


def test_load_team_spec_dict_keeps_role_defaults_when_member_alias_is_added(monkeypatch, tmp_path):
    """Role keys should remain usable after member_name aliases are injected."""
    config = {
        "models": {
            "default": {
                "model_client_config": {
                    "model_name": "gpt-role",
                    "client_provider": "openai",
                },
                "model_config_obj": {},
            }
        },
        "team": {
            "agents": {
                "leader": {},
                "teammate": {
                    "skills": ["shared-skill"],
                },
                "default_teammate": {
                    "skills": ["member-skill"],
                },
            }
        },
    }

    monkeypatch.setattr(
        "jiuwenclaw.agentserver.team.config_loader.get_config",
        lambda: config,
    )
    monkeypatch.setattr(
        "jiuwenclaw.agentserver.team.config_loader.get_agent_teams_home",
        lambda: tmp_path / ".agent_teams",
    )

    spec = load_team_spec_dict("session-2")

    assert "leader" in spec["agents"]
    assert "teammate" in spec["agents"]
    assert "default_teammate" in spec["agents"]
    assert spec["agents"]["default_teammate"]["skills"] == ["member-skill"]
    assert spec["agents"]["teammate"]["skills"] == ["shared-skill"]


def test_load_team_spec_dict_preserves_explicit_empty_skills(monkeypatch, tmp_path):
    """Explicit empty skill lists should not be treated as missing config."""
    config = {
        "models": {
            "default": {
                "model_client_config": {
                    "model_name": "gpt-empty",
                    "client_provider": "openai",
                },
                "model_config_obj": {},
            }
        },
        "team": {
            "agents": {
                "leader": {},
                "reviewer": {
                    "skills": [],
                },
            }
        },
    }

    monkeypatch.setattr(
        "jiuwenclaw.agentserver.team.config_loader.get_config",
        lambda: config,
    )
    monkeypatch.setattr(
        "jiuwenclaw.agentserver.team.config_loader.get_agent_teams_home",
        lambda: tmp_path / ".agent_teams",
    )

    spec = load_team_spec_dict("session-3")

    assert "reviewer" in spec["agents"]
    assert spec["agents"]["reviewer"]["skills"] == []


def test_load_team_spec_dict_no_auto_fill_skills_when_missing(monkeypatch, tmp_path):
    """Missing skills config should not auto-fill with global skills (new behavior)."""
    global_skills_dir = tmp_path / "skills"
    (global_skills_dir / "skill-a").mkdir(parents=True)
    (global_skills_dir / "skill-a" / "SKILL.md").write_text("# skill-a", encoding="utf-8")
    (global_skills_dir / "skill-b").mkdir(parents=True)
    (global_skills_dir / "skill-b" / "SKILL.md").write_text("# skill-b", encoding="utf-8")
    (global_skills_dir / "_internal").mkdir(parents=True)

    config = {
        "models": {
            "default": {
                "model_client_config": {
                    "model_name": "gpt-all",
                    "client_provider": "openai",
                },
                "model_config_obj": {},
            }
        },
        "team": {
            "agents": {
                "leader": {},
                "writer": {},
            }
        },
    }

    monkeypatch.setattr(
        "jiuwenclaw.agentserver.team.config_loader.get_config",
        lambda: config,
    )
    monkeypatch.setattr(
        "jiuwenclaw.agentserver.team.config_loader.get_agent_teams_home",
        lambda: tmp_path / ".agent_teams",
    )
    monkeypatch.setattr(
        "jiuwenclaw.agentserver.team.config_loader.get_agent_skills_dir",
        lambda: global_skills_dir,
    )

    spec = load_team_spec_dict("session-4")

    # skills should not be auto-filled when not configured
    assert "skills" not in spec["agents"]["leader"]
    assert "skills" not in spec["agents"]["writer"]


def test_resolve_team_sqlite_db_path_defaults_to_agent_teams_home(monkeypatch, tmp_path):
    """Missing connection_string should fall back to openjiuwen agent-teams team.db."""
    config = {
        "team": {
            "storage": {
                "type": "sqlite",
                "params": {},
            }
        }
    }

    monkeypatch.setattr(
        "jiuwenclaw.agentserver.team.config_loader.get_config",
        lambda: config,
    )
    monkeypatch.setattr(
        "jiuwenclaw.agentserver.team.config_loader.get_agent_teams_home",
        lambda: tmp_path / ".agent_teams",
    )

    db_path = resolve_team_sqlite_db_path()

    assert db_path == Path(tmp_path / ".agent_teams" / "team.db")


def test_load_team_spec_dict_preserves_arbitrary_team_top_level_fields(monkeypatch, tmp_path):
    """Unknown team-level fields should be preserved in the final spec dict."""
    config = {
        "models": {
            "default": {
                "model_client_config": {
                    "model_name": "gpt-custom",
                    "client_provider": "openai",
                },
                "model_config_obj": {},
            }
        },
        "team": {
            "agents": {
                "leader": {},
            },
            "runtime_flags": {
                "enable_observer": True,
                "retry_limit": 5,
            },
            "custom_labels": ["a", "b"],
        },
    }

    monkeypatch.setattr(
        "jiuwenclaw.agentserver.team.config_loader.get_config",
        lambda: config,
    )
    monkeypatch.setattr(
        "jiuwenclaw.agentserver.team.config_loader.get_agent_teams_home",
        lambda: tmp_path / ".agent_teams",
    )

    spec = load_team_spec_dict("session-custom")

    assert spec["runtime_flags"] == {
        "enable_observer": True,
        "retry_limit": 5,
    }
    assert spec["custom_labels"] == ["a", "b"]

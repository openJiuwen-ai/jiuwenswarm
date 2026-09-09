# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for team config loading."""

from copy import deepcopy
from pathlib import Path

import pytest
import yaml

from jiuwenswarm.common.config import resolve_env_vars
from jiuwenswarm.agents.harness.team.config_loader import (
    TeamTemplateNotFoundError,
    get_effective_team_model_entries,
    get_team_template_snapshot,
    list_team_template_summaries,
    load_team_spec_dict,
    resolve_team_sqlite_db_path,
)


def _wrap_modes_team(team_mapping: dict[str, dict]) -> dict:
    return {"modes": {"team": team_mapping}}


def test_effective_team_models_include_selected_zen_without_configured_defaults(monkeypatch):
    """A page-selected in-memory Zen model becomes the only effective candidate."""
    zen_entry = {
        "model_client_config": {
            "api_base": "https://opencode.ai/zen/v1",
            "api_key": "public",
            "model_name": "zen-free",
            "client_provider": "OpenAI",
        },
        "model_config_obj": {},
    }
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.config_loader.get_zen_free_model_entries",
        lambda: [zen_entry],
    )
    for variable_name in ("API_BASE", "API_KEY", "MODEL_NAME", "MODEL_PROVIDER"):
        monkeypatch.delenv(variable_name, raising=False)

    entries = get_effective_team_model_entries(
        {"models": {"defaults": []}},
        requested_model_name="zen-free",
    )

    assert entries == [zen_entry]


def test_effective_team_models_preserve_distinct_credentials():
    """Pool assembly deduplicates exact entries without merging credentials."""
    first = {
        "model_client_config": {
            "model_name": "shared-model",
            "client_provider": "OpenAI",
            "api_base": "https://models.example/v1",
            "api_key": "key-one",
        },
        "model_config_obj": {"temperature": 0.2},
    }
    duplicate = deepcopy(first)
    duplicate["model_client_config"]["client_provider"] = "openai"
    duplicate["model_client_config"]["api_base"] = "https://models.example/v1/"
    second = deepcopy(first)
    second["model_client_config"]["api_key"] = "key-two"
    config = {"models": {"defaults": [first, duplicate, second]}}

    entries = get_effective_team_model_entries(config)

    assert len(entries) == 2
    assert [entry["model_client_config"]["api_key"] for entry in entries] == ["key-one", "key-two"]


def test_effective_team_models_select_from_normalized_entries(monkeypatch):
    """Configured selection reuses decrypted and parsed model entries."""
    raw_entry = {
        "model_client_config": {
            "model_name": "configured-model",
            "client_provider": "OpenAI",
            "api_base": "https://models.example/v1",
            "api_key": "encrypted-key",
            "custom_headers": '{"X-Trace-Id": "trace-one"}',
        },
        "model_config_obj": {},
    }
    normalized_entry = deepcopy(raw_entry)
    normalized_entry["model_client_config"]["api_key"] = "decrypted-key"
    normalized_entry["model_client_config"]["custom_headers"] = {"X-Trace-Id": "trace-one"}
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.config_loader.get_default_models",
        lambda _config: [deepcopy(normalized_entry)],
    )

    entries = get_effective_team_model_entries(
        {"models": {"defaults": [raw_entry]}},
        requested_model_name="configured-model",
    )

    assert entries == [normalized_entry]
    assert entries[0]["model_client_config"]["api_key"] == "decrypted-key"
    assert entries[0]["model_client_config"]["custom_headers"] == {"X-Trace-Id": "trace-one"}


def test_team_manager_builds_pool_for_single_configured_model(monkeypatch):
    """A single configured model remains available to external fallback."""
    from jiuwenswarm.agents.harness.team import team_manager as team_manager_module
    from jiuwenswarm.agents.harness.team.team_manager import TeamManager

    model_entry = {
        "model_client_config": {
            "api_base": "https://models.example/v1",
            "api_key": "model-key",
            "model_name": "configured-model",
            "client_provider": "OpenAI",
        },
        "model_config_obj": {},
    }
    config = {
        "models": {"defaults": [model_entry]},
        **_wrap_modes_team(
            {
                "demo_team": {
                    "team_name": "demo_team",
                    "agents": {"leader": {}, "teammate": {}},
                }
            }
        ),
    }
    monkeypatch.setattr(team_manager_module, "get_config", lambda: config)

    spec = TeamManager._load_team_spec("session-one")

    assert spec.model_pool_strategy == "by_model_name"
    assert len(spec.model_pool) == 1
    assert spec.model_pool[0].model_name == "configured-model"
    assert spec.model_pool[0].api_key == "model-key"


def test_team_manager_builds_pool_for_single_selected_zen_model(monkeypatch):
    """A selected Zen model creates a one-entry pool even with zero defaults."""
    from jiuwenswarm.agents.harness.team import team_manager as team_manager_module
    from jiuwenswarm.agents.harness.team.team_manager import TeamManager

    zen_entry = {
        "model_client_config": {
            "api_base": "https://opencode.ai/zen/v1",
            "api_key": "public",
            "model_name": "zen-free",
            "client_provider": "OpenAI",
        },
        "model_config_obj": {},
    }
    config = {
        "models": {"defaults": []},
        **_wrap_modes_team(
            {
                "demo_team": {
                    "team_name": "demo_team",
                    "agents": {"leader": {}, "teammate": {}},
                }
            }
        ),
    }
    monkeypatch.setattr(team_manager_module, "get_config", lambda: config)
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.config_loader.get_zen_free_model_entries",
        lambda: [zen_entry],
    )
    for variable_name in ("API_BASE", "API_KEY", "MODEL_NAME", "MODEL_PROVIDER"):
        monkeypatch.delenv(variable_name, raising=False)

    spec = TeamManager._load_team_spec("session-one", requested_model_name="zen-free")

    assert spec.model_pool_strategy == "by_model_name"
    assert len(spec.model_pool) == 1
    assert spec.model_pool[0].model_name == "zen-free"
    assert spec.model_pool[0].api_provider == "OpenAI"


@pytest.mark.parametrize(
    ("global_enabled", "legacy_team_enabled", "expected"),
    [
        (True, False, True),
        (True, True, True),
        (False, False, False),
        (False, True, False),
    ],
)
def test_global_permission_switch_controls_team_runtime(
    global_enabled: bool,
    legacy_team_enabled: bool,
    expected: bool,
):
    """Legacy Team values must not override the global permission switch."""
    config = {
        "permissions": {"enabled": global_enabled},
        "models": {
            "default": {
                "model_client_config": {
                    "model_name": "gpt-test",
                    "client_provider": "openai",
                },
                "model_config_obj": {},
            }
        },
        **_wrap_modes_team(
            {
                "demo_team": {
                    "team_name": "demo_team",
                    "enable_permissions": legacy_team_enabled,
                    "agents": {"leader": {}},
                }
            }
        ),
    }

    spec = load_team_spec_dict(config_base=config)

    assert spec["enable_permissions"] is expected


def test_load_team_spec_dict_reads_models_defaults_from_repository_config(monkeypatch):
    """Repository config template should provide the default team model from models.defaults."""
    repo_config = Path(__file__).resolve().parents[3] / "jiuwenswarm" / "resources" / "config.yaml"
    monkeypatch.setenv("API_BASE", "https://example.test/v1")
    monkeypatch.setenv("API_KEY", "sk-test")
    monkeypatch.setenv("MODEL_NAME", "gpt-template")
    monkeypatch.setenv("MODEL_PROVIDER", "OpenAI")

    config = resolve_env_vars(yaml.safe_load(repo_config.read_text(encoding="utf-8")) or {})

    spec = load_team_spec_dict(config_base=config)

    model = spec["agents"]["leader"]["model"]
    assert model["model_client_config"]["api_base"] == "https://example.test/v1"
    assert model["model_client_config"]["api_key"] == "sk-test"
    assert model["model_client_config"]["model_name"] == "gpt-template"
    assert model["model_client_config"]["client_provider"] == "OpenAI"
    assert model["model_request_config"]["model"] == "gpt-template"


def test_load_team_spec_dict_uses_first_models_defaults_entry_for_team(monkeypatch):
    """Team config loading should use the first models.defaults entry."""
    config = {
        "models": {
            "defaults": [
                {
                    "model_client_config": {
                        "api_base": "https://first.example.test/v1",
                        "api_key": "sk-first",
                        "model_name": "first-model",
                        "client_provider": "OpenAI",
                    },
                    "model_config_obj": {"temperature": 0.1},
                    "is_default": False,
                },
                {
                    "model_client_config": {
                        "api_base": "https://second.example.test/v1",
                        "api_key": "sk-second",
                        "model_name": "second-model",
                        "client_provider": "OpenAI",
                    },
                    "model_config_obj": {"temperature": 0.9},
                    "is_default": True,
                },
            ]
        },
        **_wrap_modes_team(
            {
                "demo_team": {
                    "team_name": "demo_team",
                    "agents": {
                        "leader": {},
                        "teammate": {},
                    },
                }
            }
        ),
    }

    spec = load_team_spec_dict(config_base=config)

    model = spec["agents"]["leader"]["model"]
    assert model["model_client_config"]["api_base"] == "https://first.example.test/v1"
    assert model["model_client_config"]["api_key"] == "sk-first"
    assert model["model_client_config"]["model_name"] == "first-model"
    assert model["model_request_config"]["model"] == "first-model"
    assert model["model_request_config"]["temperature"] == 0.1


def test_load_team_spec_dict_maps_reasoning_level_off_and_drops_internal_hint():
    """Cluster members must not forward UI ``reasoning_level`` to the OpenAI SDK."""
    config = {
        "models": {
            "defaults": [
                {
                    "model_client_config": {
                        "api_base": "https://example.test/v1",
                        "api_key": "sk-test",
                        "model_name": "Deepseek-V4-Flash-0731",
                        "client_provider": "OpenAI",
                    },
                    "model_config_obj": {
                        "temperature": 0.95,
                        "reasoning_level": "off",
                    },
                    "is_default": True,
                }
            ]
        },
        **_wrap_modes_team(
            {
                "demo_team": {
                    "team_name": "demo_team",
                    "agents": {
                        "leader": {},
                        "teammate": {},
                    },
                }
            }
        ),
    }

    spec = load_team_spec_dict(config_base=config)

    for role in ("leader", "teammate"):
        request_config = spec["agents"][role]["model"]["model_request_config"]
        assert "reasoning_level" not in request_config
        assert request_config["reasoning"] == {"mode": "disabled"}
        assert request_config["model"] == "Deepseek-V4-Flash-0731"
        assert request_config["temperature"] == 0.95


def test_load_team_spec_dict_sanitizes_explicit_member_model_reasoning_level():
    """A member that already has its own model dict still needs the UI hint stripped."""
    config = {
        "models": {
            "default": {
                "model_client_config": {
                    "model_name": "fallback-model",
                    "client_provider": "OpenAI",
                },
                "model_config_obj": {},
            }
        },
        **_wrap_modes_team(
            {
                "demo_team": {
                    "team_name": "demo_team",
                    "agents": {
                        "leader": {
                            "model": {
                                "model_client_config": {
                                    "api_base": "https://example.test/v1",
                                    "api_key": "sk-test",
                                    "model_name": "Deepseek-V4-Flash-0731",
                                    "client_provider": "OpenAI",
                                },
                                "model_request_config": {
                                    "model": "Deepseek-V4-Flash-0731",
                                    "temperature": 0.2,
                                    "reasoning_level": "off",
                                },
                            }
                        },
                    },
                }
            }
        ),
    }

    spec = load_team_spec_dict(config_base=config)

    request_config = spec["agents"]["leader"]["model"]["model_request_config"]
    assert "reasoning_level" not in request_config
    assert request_config["reasoning"] == {"mode": "disabled"}
    assert request_config["temperature"] == 0.2


def test_load_team_spec_dict_keeps_declared_request_model_over_client_model_name():
    """An explicit member request ``model`` must not be overwritten by client model_name."""
    config = {
        "models": {
            "default": {
                "model_client_config": {
                    "model_name": "fallback-model",
                    "client_provider": "OpenAI",
                },
                "model_config_obj": {},
            }
        },
        **_wrap_modes_team(
            {
                "demo_team": {
                    "team_name": "demo_team",
                    "agents": {
                        "leader": {
                            "model": {
                                "model_client_config": {
                                    "api_base": "https://example.test/v1",
                                    "api_key": "sk-test",
                                    "model_name": "client-listed-name",
                                    "client_provider": "OpenAI",
                                },
                                "model_request_config": {
                                    "model": "request-declared-name",
                                    "temperature": 0.2,
                                    "reasoning_level": "off",
                                },
                            }
                        },
                    },
                }
            }
        ),
    }

    spec = load_team_spec_dict(config_base=config)

    request_config = spec["agents"]["leader"]["model"]["model_request_config"]
    assert request_config["model"] == "request-declared-name"
    assert "reasoning_level" not in request_config
    assert request_config["reasoning"] == {"mode": "disabled"}


def test_load_team_spec_dict_keeps_ui_hint_from_model_config_obj_when_request_config_exists():
    """A leftover ``model_config_obj.reasoning_level`` must still be mapped."""
    config = {
        "models": {
            "default": {
                "model_client_config": {
                    "model_name": "fallback-model",
                    "client_provider": "OpenAI",
                },
                "model_config_obj": {},
            }
        },
        **_wrap_modes_team(
            {
                "demo_team": {
                    "team_name": "demo_team",
                    "agents": {
                        "leader": {
                            "model": {
                                "model_client_config": {
                                    "api_base": "https://example.test/v1",
                                    "api_key": "sk-test",
                                    "model_name": "Deepseek-V4-Flash-0731",
                                    "client_provider": "OpenAI",
                                },
                                "model_config_obj": {"reasoning_level": "off"},
                                "model_request_config": {
                                    "model": "Deepseek-V4-Flash-0731",
                                    "temperature": 0.3,
                                },
                            }
                        },
                    },
                }
            }
        ),
    }

    spec = load_team_spec_dict(config_base=config)

    request_config = spec["agents"]["leader"]["model"]["model_request_config"]
    assert "reasoning_level" not in request_config
    assert "model_config_obj" not in spec["agents"]["leader"]["model"]
    assert request_config["reasoning"] == {"mode": "disabled"}
    assert request_config["temperature"] == 0.3


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
        **_wrap_modes_team(
            {
                "demo_team": {
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
                        "leader": {},
                        "teammate": {},
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
                }
            }
        ),
    }

    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.config_loader.get_config",
        lambda: config,
    )
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.config_loader.get_agent_teams_home",
        lambda: fake_agent_teams_home,
    )

    spec = load_team_spec_dict()

    assert spec["team_name"] == "demo_team"
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


def test_load_team_spec_dict_uses_first_team_from_modes_team(monkeypatch, tmp_path):
    """The current runtime should default to the first team entry in modes.team."""
    config = {
        "models": {
            "default": {
                "model_client_config": {
                    "model_name": "gpt-first",
                    "client_provider": "openai",
                },
                "model_config_obj": {},
            }
        },
        **_wrap_modes_team(
            {
                "alpha_team": {
                    "team_name": "alpha_team",
                    "leader": {
                        "member_name": "alpha_leader",
                        "display_name": "Alpha Leader",
                        "persona": "Lead alpha",
                    },
                    "agents": {"leader": {"skills": ["alpha-skill"]}},
                },
                "beta_team": {
                    "team_name": "beta_team",
                    "leader": {
                        "member_name": "beta_leader",
                        "display_name": "Beta Leader",
                        "persona": "Lead beta",
                    },
                    "agents": {"leader": {"skills": ["beta-skill"]}},
                },
            }
        ),
    }

    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.config_loader.get_config",
        lambda: config,
    )
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.config_loader.get_agent_teams_home",
        lambda: tmp_path / ".agent_teams",
    )

    spec = load_team_spec_dict()

    assert spec["team_name"] == "alpha_team"
    assert spec["leader"]["member_name"] == "alpha_leader"
    assert spec["agents"]["leader"]["skills"] == ["alpha-skill"]


def test_load_team_spec_dict_selects_requested_template_id(monkeypatch, tmp_path):
    """Runtime binding should be able to pick a specific configured team template."""
    config = {
        "models": {
            "default": {
                "model_client_config": {
                    "model_name": "gpt-template",
                    "client_provider": "openai",
                },
                "model_config_obj": {},
            }
        },
        **_wrap_modes_team(
            {
                "alpha": {
                    "team_name": "alpha_default_name",
                    "agents": {"leader": {"skills": ["alpha-skill"]}},
                },
                "beta": {
                    "team_name": "beta_default_name",
                    "leader": {"member_name": "beta_leader"},
                    "agents": {"leader": {"skills": ["beta-skill"]}},
                },
            }
        ),
    }
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.config_loader.get_agent_teams_home",
        lambda: tmp_path / ".agent_teams",
    )

    spec = load_team_spec_dict(config_base=config, template_id="beta")

    assert spec["team_name"] == "beta_default_name"
    assert spec["leader"]["member_name"] == "beta_leader"
    assert spec["agents"]["leader"]["skills"] == ["beta-skill"]


def test_team_template_snapshot_survives_deleted_template(monkeypatch, tmp_path):
    config = {
        "models": {
            "default": {
                "model_client_config": {
                    "model_name": "gpt-template",
                    "client_provider": "openai",
                },
                "model_config_obj": {},
            }
        },
        **_wrap_modes_team(
            {
                "alpha": {
                    "team_name": "alpha_default_name",
                    "agents": {"leader": {"skills": ["alpha-skill"]}},
                },
                "beta": {
                    "team_name": "beta_default_name",
                    "leader": {"member_name": "beta_leader"},
                    "agents": {"leader": {"skills": ["beta-skill"]}},
                },
            }
        ),
    }
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.config_loader.get_agent_teams_home",
        lambda: tmp_path / ".agent_teams",
    )

    snapshot = get_team_template_snapshot(config_base=config, template_id="beta")
    config["modes"]["team"].pop("beta")
    spec = load_team_spec_dict(
        config_base=config,
        template_id="beta",
        template_snapshot=snapshot,
        strict_template=True,
    )

    assert spec["team_name"] == "beta_default_name"
    assert spec["leader"]["member_name"] == "beta_leader"
    assert spec["agents"]["leader"]["skills"] == ["beta-skill"]


def test_load_team_spec_dict_strict_template_rejects_missing_id(monkeypatch, tmp_path):
    config = {
        "models": {
            "default": {
                "model_client_config": {
                    "model_name": "gpt-template",
                    "client_provider": "openai",
                },
                "model_config_obj": {},
            }
        },
        **_wrap_modes_team(
            {
                "alpha": {
                    "team_name": "alpha_default_name",
                    "agents": {"leader": {"skills": ["alpha-skill"]}},
                },
            }
        ),
    }
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.config_loader.get_agent_teams_home",
        lambda: tmp_path / ".agent_teams",
    )

    with pytest.raises(TeamTemplateNotFoundError, match="missing-template"):
        load_team_spec_dict(
            config_base=config,
            template_id="missing-template",
            strict_template=True,
        )


def test_list_team_template_summaries_reads_modes_team_templates() -> None:
    config = _wrap_modes_team(
        {
            "default": {
                "team_name": "configured_team",
                "display_name": "Default Team",
                "agents": {"leader": {}},
            },
            "research": {
                "name": "Research Template",
                "agents": {"leader": {}},
            },
        }
    )

    summaries = list_team_template_summaries(config)

    assert summaries == [
        {
            "template_id": "default",
            "display_name": "Default Team",
            "available": True,
            "source": "modes.team.default",
            "team_name": "configured_team",
        },
        {
            "template_id": "research",
            "display_name": "Research Template",
            "available": True,
            "source": "modes.team.research",
            "team_name": "",
        },
    ]


def test_list_team_template_summaries_falls_back_to_legacy_team_config() -> None:
    config = {
        "team": {
            "team_name": "legacy_team",
            "leader": {"member_name": "legacy_leader"},
            "agents": {"leader": {}},
        },
    }

    summaries = list_team_template_summaries(config)

    assert summaries == [
        {
            "template_id": "legacy_team",
            "display_name": "legacy_team",
            "available": True,
            "source": "team",
            "team_name": "legacy_team",
        },
    ]


def test_load_team_spec_dict_selects_legacy_template_id(monkeypatch, tmp_path):
    config = {
        "models": {
            "default": {
                "model_client_config": {
                    "model_name": "gpt-legacy",
                    "client_provider": "openai",
                },
                "model_config_obj": {},
            }
        },
        "team": {
            "team_name": "legacy_team",
            "leader": {"member_name": "legacy_leader"},
            "agents": {"leader": {"skills": ["legacy-skill"]}},
        },
    }
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.config_loader.get_agent_teams_home",
        lambda: tmp_path / ".agent_teams",
    )

    spec = load_team_spec_dict(config_base=config, template_id="legacy_team")

    assert spec["team_name"] == "legacy_team"
    assert spec["leader"]["member_name"] == "legacy_leader"
    assert spec["agents"]["leader"]["skills"] == ["legacy-skill"]


def test_load_team_spec_dict_fills_default_transport_and_workspace(monkeypatch, tmp_path):
    """Missing team transport/workspace should fall back to local inprocess defaults."""
    config = {
        "models": {
            "default": {
                "model_client_config": {
                    "model_name": "gpt-defaults",
                    "client_provider": "openai",
                },
                "model_config_obj": {},
            }
        },
        **_wrap_modes_team(
            {
                "demo_team": {
                    "team_name": "demo_team",
                    "agents": {
                        "leader": {},
                        "reviewer": {},
                    },
                }
            }
        ),
    }

    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.config_loader.get_config",
        lambda: config,
    )
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.config_loader.get_agent_teams_home",
        lambda: tmp_path / ".agent_teams",
    )

    spec = load_team_spec_dict()

    assert spec["transport"] == {"type": "inprocess"}
    assert spec["workspace"] == {
        "enabled": True,
        "version_control": False,
    }
    assert spec["agents"]["leader"]["workspace"] == {"stable_base": True}
    assert spec["agents"]["reviewer"]["workspace"] == {"stable_base": True}


def test_load_team_spec_dict_defaults_enable_hitt_to_true(monkeypatch, tmp_path):
    """Missing enable_hitt should default to enabled for team mode."""
    config = {
        "models": {
            "default": {
                "model_client_config": {
                    "model_name": "gpt-hitt-default",
                    "client_provider": "openai",
                },
                "model_config_obj": {},
            }
        },
        **_wrap_modes_team(
            {
                "demo_team": {
                    "team_name": "demo_team",
                    "agents": {
                        "leader": {},
                    },
                }
            }
        ),
    }

    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.config_loader.get_config",
        lambda: config,
    )
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.config_loader.get_agent_teams_home",
        lambda: tmp_path / ".agent_teams",
    )

    spec = load_team_spec_dict()

    assert spec["enable_hitt"] is True


def test_load_team_spec_dict_preserves_explicit_enable_hitt_false(monkeypatch, tmp_path):
    """Explicit enable_hitt false should not be overwritten by defaults."""
    config = {
        "models": {
            "default": {
                "model_client_config": {
                    "model_name": "gpt-hitt-disabled",
                    "client_provider": "openai",
                },
                "model_config_obj": {},
            }
        },
        **_wrap_modes_team(
            {
                "demo_team": {
                    "team_name": "demo_team",
                    "enable_hitt": False,
                    "agents": {
                        "leader": {},
                    },
                }
            }
        ),
    }

    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.config_loader.get_config",
        lambda: config,
    )
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.config_loader.get_agent_teams_home",
        lambda: tmp_path / ".agent_teams",
    )

    spec = load_team_spec_dict()

    assert spec["enable_hitt"] is False


@pytest.mark.skip(reason="enable_swarmflow default injection has been removed")
def test_load_team_spec_dict_defaults_enable_swarmflow_to_true(monkeypatch, tmp_path):
    """Missing enable_swarmflow should default to enabled for team mode."""
    config = {
        "models": {
            "default": {
                "model_client_config": {
                    "model_name": "gpt-swarmflow-default",
                    "client_provider": "openai",
                },
                "model_config_obj": {},
            }
        },
        **_wrap_modes_team(
            {
                "demo_team": {
                    "team_name": "demo_team",
                    "agents": {
                        "leader": {},
                    },
                }
            }
        ),
    }

    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.config_loader.get_config",
        lambda: config,
    )
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.config_loader.get_agent_teams_home",
        lambda: tmp_path / ".agent_teams",
    )

    spec = load_team_spec_dict()

    assert spec["enable_swarmflow"] is True


@pytest.mark.skip(reason="enable_swarmflow loader behavior is no longer validated")
def test_load_team_spec_dict_preserves_explicit_enable_swarmflow_false(monkeypatch, tmp_path):
    """Explicit enable_swarmflow false should not be overwritten by defaults."""
    config = {
        "models": {
            "default": {
                "model_client_config": {
                    "model_name": "gpt-swarmflow-disabled",
                    "client_provider": "openai",
                },
                "model_config_obj": {},
            }
        },
        **_wrap_modes_team(
            {
                "demo_team": {
                    "team_name": "demo_team",
                    "enable_swarmflow": False,
                    "agents": {
                        "leader": {},
                    },
                }
            }
        ),
    }

    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.config_loader.get_config",
        lambda: config,
    )
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.config_loader.get_agent_teams_home",
        lambda: tmp_path / ".agent_teams",
    )

    spec = load_team_spec_dict()

    assert spec["enable_swarmflow"] is False


def test_load_team_spec_dict_adds_default_teammate_when_only_leader_configured(monkeypatch, tmp_path):
    """A leader-only team config still needs a teammate template for dynamic spawns."""
    config = {
        "models": {
            "default": {
                "model_client_config": {
                    "model_name": "gpt-role-default",
                    "client_provider": "openai",
                },
                "model_config_obj": {},
            }
        },
        **_wrap_modes_team(
            {
                "demo_team": {
                    "team_name": "demo_team",
                    "agents": {
                        "leader": {
                            "skills": ["team-management"],
                        },
                    },
                }
            }
        ),
    }

    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.config_loader.get_config",
        lambda: config,
    )
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.config_loader.get_agent_teams_home",
        lambda: tmp_path / ".agent_teams",
    )

    spec = load_team_spec_dict()

    assert set(spec["agents"]) == {"leader", "teammate"}
    assert spec["agents"]["leader"]["skills"] == ["team-management"]
    assert "skills" not in spec["agents"]["teammate"]


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
        **_wrap_modes_team(
            {
                "demo_team": {
                    "team_name": "demo_team",
                    "agents": {
                        "leader": {},
                        "teammate": {
                            "skills": ["shared-skill"],
                        },
                        "default_teammate": {
                            "skills": ["member-skill"],
                        },
                    },
                }
            }
        ),
    }

    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.config_loader.get_config",
        lambda: config,
    )
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.config_loader.get_agent_teams_home",
        lambda: tmp_path / ".agent_teams",
    )

    spec = load_team_spec_dict()

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
        **_wrap_modes_team(
            {
                "demo_team": {
                    "team_name": "demo_team",
                    "agents": {
                        "leader": {},
                        "reviewer": {
                            "skills": [],
                        },
                    },
                }
            }
        ),
    }

    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.config_loader.get_config",
        lambda: config,
    )
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.config_loader.get_agent_teams_home",
        lambda: tmp_path / ".agent_teams",
    )

    spec = load_team_spec_dict()

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
        **_wrap_modes_team(
            {
                "demo_team": {
                    "team_name": "demo_team",
                    "agents": {
                        "leader": {},
                        "writer": {},
                    },
                }
            }
        ),
    }

    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.config_loader.get_config",
        lambda: config,
    )
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.config_loader.get_agent_teams_home",
        lambda: tmp_path / ".agent_teams",
    )

    spec = load_team_spec_dict()

    # skills should not be auto-filled when not configured
    assert "skills" not in spec["agents"]["leader"]
    assert "skills" not in spec["agents"]["writer"]


def test_resolve_team_sqlite_db_path_defaults_to_agent_teams_home(monkeypatch, tmp_path):
    """Missing connection_string should fall back to openjiuwen agent-teams team.db."""
    config = _wrap_modes_team(
        {
            "demo_team": {
                "team_name": "demo_team",
                "storage": {
                    "type": "sqlite",
                    "params": {},
                },
            }
        }
    )

    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.config_loader.get_config",
        lambda: config,
    )
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.config_loader.get_agent_teams_home",
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
        **_wrap_modes_team(
            {
                "demo_team": {
                    "team_name": "demo_team",
                    "agents": {
                        "leader": {},
                    },
                    "runtime_flags": {
                        "enable_observer": True,
                        "retry_limit": 5,
                    },
                    "custom_labels": ["a", "b"],
                }
            }
        ),
    }

    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.config_loader.get_config",
        lambda: config,
    )
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.config_loader.get_agent_teams_home",
        lambda: tmp_path / ".agent_teams",
    )

    spec = load_team_spec_dict()

    assert spec["runtime_flags"] == {
        "enable_observer": True,
        "retry_limit": 5,
    }
    assert spec["custom_labels"] == ["a", "b"]

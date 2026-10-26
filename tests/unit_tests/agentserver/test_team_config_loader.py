# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for team config loading."""

import hashlib
import json
from pathlib import Path

import pytest
import yaml

from jiuwenswarm.common.config import resolve_env_vars
from jiuwenswarm.agents.harness.team.config_loader import (
    TeamTemplateNotFoundError,
    get_team_template_snapshot,
    list_team_template_summaries,
    load_team_spec_dict,
    resolve_team_sqlite_db_path,
)
from jiuwenswarm.agents.harness.team import config_loader as team_config_loader


def _wrap_modes_team(team_mapping: dict[str, dict]) -> dict:
    return {"modes": {"team": team_mapping}}


@pytest.mark.parametrize(
    ("preferred_language", "expected"),
    [
        ("zh", "cn"),
        ("zh-cn", "cn"),
        ("cn", "cn"),
        ("en", "en"),
        ("en-US", "en"),
        ("unsupported", "cn"),
    ],
)
def test_load_team_spec_dict_normalizes_language(preferred_language, expected):
    config = {
        "preferred_language": preferred_language,
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

    spec = load_team_spec_dict(config_base=config)

    assert spec["language"] == expected


def test_load_team_spec_dict_maps_private_prompts_and_display_name_rule():
    config = {
        "preferred_language": "en",
        "models": {"defaults": []},
        **_wrap_modes_team(
            {
                "demo_team": {
                    "team_name": "demo_team",
                    "agents": {"leader": {}, "teammate": {}},
                    "leader": {
                        "member_name": "team_leader",
                        "display_name": "Lead",
                        "persona": "Coordinate the team",
                    },
                    "predefined_members": [
                        {
                            "member_name": "analyst",
                            "display_name": "Analyst",
                            "persona": "Analyze evidence",
                            "prompt_hint": "Cite sources",
                        }
                    ],
                }
            }
        ),
    }

    spec = load_team_spec_dict(config_base=config)

    assert spec["leader"]["desc"] == "Coordinate the team"
    assert "Coordinate the team" in spec["leader"]["prompt"]
    assert "## Member naming convention" in spec["leader"]["prompt"]
    member = spec["predefined_members"][0]
    assert member["desc"] == "Analyze evidence"
    assert "Cite sources" in member["prompt"]
    assert "display name" in member["prompt"]
    assert "persona" not in member
    assert "prompt_hint" not in member


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


def test_load_team_spec_dict_merges_tip_default_headers_into_member_model(monkeypatch):
    """Team members must carry MaaS tip Authorization into custom_headers."""
    monkeypatch.setattr(
        team_config_loader,
        "read_default_headers",
        lambda: {"Authorization": "Basic tip-auth", "X-Tenant": "tenant-a"},
        raising=False,
    )
    config = {
        "models": {
            "defaults": [
                {
                    "model_client_config": {
                        "api_base": "https://maas.example/v2",
                        "api_key": "huawei-maas-session",
                        "model_name": "glm-5.2",
                        "client_provider": "OpenAI",
                        "custom_headers": {
                            "Authorization": "Bearer stale",
                            "X-Existing": "keep",
                        },
                    },
                    "model_config_obj": {},
                }
            ]
        },
        **_wrap_modes_team(
            {
                "demo_team": {
                    "team_name": "demo_team",
                    "agents": {"leader": {}, "teammate": {}},
                }
            }
        ),
    }

    spec = load_team_spec_dict(config_base=config)

    headers = spec["agents"]["leader"]["model"]["model_client_config"]["custom_headers"]
    assert headers == {
        "Authorization": "Basic tip-auth",
        "X-Tenant": "tenant-a",
        "X-Existing": "keep",
    }


def test_requested_model_ref_overrides_every_team_agent_model_and_preserves_other_fields():
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
                },
                {
                    "model_client_config": {
                        "api_base": "https://selected.example.test/v1",
                        "api_key": "sk-selected",
                        "model_name": "selected-model",
                        "client_provider": "OpenAI",
                    },
                    "model_config_obj": {"temperature": 0.8},
                },
            ]
        },
        **_wrap_modes_team(
            {
                "demo_team": {
                    "team_name": "demo_team",
                    "agents": {
                        "leader": {
                            "model": {
                                "model_client_config": {"model_name": "leader-default"},
                                "model_request_config": {"model": "leader-default"},
                            },
                            "skills": ["planning"],
                        },
                        "teammate": {
                            "model": {
                                "model_client_config": {"model_name": "teammate-default"},
                                "model_request_config": {"model": "teammate-default"},
                            },
                            "max_iterations": 7,
                        },
                    },
                }
            }
        ),
    }

    selected_ref = _model_identity_ref(
        model_name="selected-model",
        provider="OpenAI",
        api_base="https://selected.example.test/v1",
    )
    spec = load_team_spec_dict(
        config_base=config, requested_model_name="selected-model", requested_model_ref=selected_ref
    )

    for agent in spec["agents"].values():
        assert agent["model"]["model_client_config"]["model_name"] == "selected-model"
        assert agent["model"]["model_client_config"]["api_key"] == "sk-selected"
        assert agent["model"]["model_request_config"]["model"] == "selected-model"
    assert spec["agents"]["leader"]["skills"] == ["planning"]
    assert spec["agents"]["teammate"]["max_iterations"] == 7


def test_requested_model_name_only_preserves_explicit_member_models():
    config = {
        "models": {
            "defaults": [
                {
                    "model_client_config": {"model_name": "selected-model"},
                    "model_config_obj": {},
                }
            ]
        },
        **_wrap_modes_team(
            {
                "demo_team": {
                    "team_name": "demo_team",
                    "agents": {
                        "leader": {
                            "model": {
                                "model_client_config": {"model_name": "leader-default"},
                                "model_request_config": {"model": "leader-default"},
                            }
                        },
                        "teammate": {},
                    },
                }
            }
        ),
    }

    spec = load_team_spec_dict(config_base=config, requested_model_name="selected-model")

    assert spec["agents"]["leader"]["model"]["model_client_config"]["model_name"] == "leader-default"
    assert spec["agents"]["teammate"]["model"]["model_client_config"]["model_name"] == "selected-model"


def test_requested_model_name_falls_back_to_first_default_when_missing():
    config = {
        "models": {
            "defaults": [
                {
                    "model_client_config": {"model_name": "configured-model"},
                    "model_config_obj": {},
                }
            ]
        },
        **_wrap_modes_team(
            {"demo_team": {"team_name": "demo_team", "agents": {"leader": {}, "teammate": {}}}}
        ),
    }

    spec = load_team_spec_dict(config_base=config, requested_model_name="missing-model")

    assert spec["agents"]["leader"]["model"]["model_client_config"]["model_name"] == "configured-model"


def test_requested_model_name_fails_closed_when_ambiguous():
    config = {
        "models": {
            "defaults": [
                {
                    "model_client_config": {
                        "model_name": "duplicate-model",
                        "api_base": "https://first.example.test/v1",
                    },
                    "model_config_obj": {},
                },
                {
                    "model_client_config": {
                        "model_name": "duplicate-model",
                        "api_base": "https://second.example.test/v1",
                    },
                    "model_config_obj": {},
                },
            ]
        },
        **_wrap_modes_team(
            {"demo_team": {"team_name": "demo_team", "agents": {"leader": {}, "teammate": {}}}}
        ),
    }

    with pytest.raises(ValueError, match="ambiguous"):
        load_team_spec_dict(config_base=config, requested_model_name="duplicate-model")


def _model_identity_ref(*, model_name: str, provider: str, api_base: str) -> str:
    identity = {
        "api_base": api_base.rstrip("/"),
        "model_name": model_name.strip(),
        "provider": provider.strip().lower(),
    }
    digest = hashlib.sha256(
        json.dumps(identity, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    ).hexdigest()
    return f"model-identity-v1:{digest}"


def test_load_team_spec_dict_resolves_member_model_reference_after_defaults_reorder():
    second_ref = _model_identity_ref(
        model_name="shared-model",
        provider="OpenAI",
        api_base="https://second.example.test/v1",
    )
    config = {
        "models": {
            "defaults": [
                {
                    "model_client_config": {
                        "api_base": "https://second.example.test/v1/",
                        "api_key": "rotated-second-secret",
                        "model_name": "shared-model",
                        "client_provider": "OpenAI",
                    },
                    "model_config_obj": {"temperature": 0.9},
                },
                {
                    "model_client_config": {
                        "api_base": "https://first.example.test/v1",
                        "api_key": "sk-first",
                        "model_name": "shared-model",
                        "client_provider": "OpenAI",
                    },
                    "model_config_obj": {"temperature": 0.1},
                },
            ]
        },
        **_wrap_modes_team(
            {
                "demo_team": {
                    "team_name": "demo_team",
                    "agents": {
                        "leader": {"model": {"ref": second_ref}},
                    },
                }
            }
        ),
    }

    spec = load_team_spec_dict(config_base=config)

    model = spec["agents"]["leader"]["model"]
    assert model["model_client_config"]["api_base"] == "https://second.example.test/v1/"
    assert model["model_client_config"]["api_key"] == "rotated-second-secret"
    assert model["model_request_config"]["model"] == "shared-model"
    assert model["model_request_config"]["temperature"] == 0.9


def test_load_team_spec_dict_keeps_ref_model_identity_when_merging_request_overrides():
    owner_ref = _model_identity_ref(
        model_name="owner-model",
        provider="OpenAI",
        api_base="https://owner.example.test/v1",
    )
    config = {
        "models": {
            "defaults": [
                {
                    "model_client_config": {
                        "api_base": "https://owner.example.test/v1",
                        "api_key": "owner-secret",
                        "model_name": "owner-model",
                        "client_provider": "OpenAI",
                    },
                    "model_config_obj": {"temperature": 0.9},
                }
            ]
        },
        **_wrap_modes_team(
            {
                "demo_team": {
                    "team_name": "demo_team",
                    "agents": {
                        "leader": {
                            "model": {
                                "ref": owner_ref,
                                "model_request_config": {
                                    "model": "other-model",
                                    "temperature": 0.2,
                                },
                            }
                        }
                    },
                }
            }
        ),
    }

    spec = load_team_spec_dict(config_base=config)

    model = spec["agents"]["leader"]["model"]
    assert model["model_request_config"]["model"] == "owner-model"
    assert model["model_request_config"]["temperature"] == 0.2


def test_load_team_spec_dict_keeps_legacy_index_reference_readable():
    config = {
        "models": {
            "defaults": [
                {
                    "model_client_config": {
                        "api_base": "https://first.example.test/v1",
                        "api_key": "sk-first",
                        "model_name": "shared-model",
                        "client_provider": "OpenAI",
                    },
                    "model_config_obj": {"temperature": 0.1},
                },
                {
                    "model_client_config": {
                        "api_base": "https://second.example.test/v1",
                        "api_key": "sk-second",
                        "model_name": "shared-model",
                        "client_provider": "OpenAI",
                    },
                    "model_config_obj": {"temperature": 0.9},
                },
            ]
        },
        **_wrap_modes_team(
            {
                "demo_team": {
                    "team_name": "demo_team",
                    "agents": {"leader": {"model": {"ref": "shared-model#1"}}},
                }
            }
        ),
    }

    spec = load_team_spec_dict(config_base=config)

    model = spec["agents"]["leader"]["model"]
    assert model["model_client_config"]["api_base"] == "https://second.example.test/v1"
    assert model["model_client_config"]["api_key"] == "sk-second"


def test_load_team_spec_dict_rejects_stale_member_model_reference():
    config = {
        "models": {
            "defaults": [
                {
                    "model_client_config": {
                        "model_name": "different-model",
                        "api_key": "sk-secret",
                    }
                }
            ]
        },
        **_wrap_modes_team(
            {
                "demo_team": {
                    "team_name": "demo_team",
                    "agents": {
                        "leader": {"model": {"ref": f"model-identity-v1:{'0' * 64}"}},
                    },
                }
            }
        ),
    }

    with pytest.raises(ValueError, match="model reference"):
        load_team_spec_dict(config_base=config)


def test_load_team_spec_dict_rejects_ambiguous_model_identity_reference():
    shared_ref = _model_identity_ref(
        model_name="shared-model",
        provider="OpenAI",
        api_base="https://shared.example.test/v1",
    )
    duplicate = {
        "model_client_config": {
            "api_base": "https://shared.example.test/v1",
            "api_key": "rotated-secret",
            "model_name": "shared-model",
            "client_provider": "OpenAI",
        }
    }
    config = {
        "models": {"defaults": [duplicate, duplicate]},
        **_wrap_modes_team(
            {
                "demo_team": {
                    "team_name": "demo_team",
                    "agents": {"leader": {"model": {"ref": shared_ref}}},
                }
            }
        ),
    }

    with pytest.raises(ValueError, match="ambiguous"):
        load_team_spec_dict(config_base=config)


@pytest.mark.parametrize("configured_value", [pytest.param(None, id="null"), pytest.param("missing", id="missing")])
def test_load_team_spec_dict_defaults_missing_or_null_debate_rounds_to_five(configured_value):
    team = {
        "team_name": "demo_team",
        "agents": {"leader": {}, "teammate": {}},
    }
    if configured_value != "missing":
        team["max_debate_rounds"] = configured_value
    config = {"models": {"defaults": []}}

    spec = load_team_spec_dict(
        config_base=config,
        template_id="demo_team",
        template_snapshot=team,
    )

    assert spec["max_debate_rounds"] == 5


def test_load_team_spec_dict_preserves_explicit_debate_rounds():
    config = {
        "models": {"defaults": []},
        **_wrap_modes_team(
            {
                "demo_team": {
                    "team_name": "demo_team",
                    "max_debate_rounds": 3,
                    "agents": {"leader": {}, "teammate": {}},
                }
            }
        ),
    }

    spec = load_team_spec_dict(config_base=config)

    assert spec["max_debate_rounds"] == 3


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
    assert spec["leader"]["desc"] == "Lead the team"
    assert "Lead the team" in spec["leader"]["prompt"]
    assert spec["predefined_members"][0]["member_name"] == "analyst"
    assert spec["predefined_members"][0]["display_name"] == "Data Analyst"
    assert spec["predefined_members"][0]["desc"] == "Analyze data"
    assert "Focus on trends" in spec["predefined_members"][0]["prompt"]
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
                    "max_debate_rounds": 2,
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
    # The locked agent-core dev-stable may not expose this optional field yet;
    # this test only covers JiuwenSwarm's config preservation contract.
    assert spec["max_debate_rounds"] == 2


def test_requested_model_ref_in_legacy_model_config_fails_closed():
    ref = _model_identity_ref(
        model_name="selected-model",
        provider="OpenAI",
        api_base="https://selected.example.test/v1",
    )
    config = {
        "models": {
            "default": {
                "model_client_config": {
                    "model_name": "selected-model",
                    "api_base": "https://selected.example.test/v1",
                    "client_provider": "OpenAI",
                },
                "model_config_obj": {},
            }
        },
        **_wrap_modes_team(
            {"demo_team": {"team_name": "demo_team", "agents": {"leader": {}, "teammate": {}}}}
        ),
    }

    with pytest.raises(ValueError, match="owner is unavailable"):
        load_team_spec_dict(
            config_base=config,
            requested_model_name="selected-model",
            requested_model_ref=ref,
        )


def _duplicate_request_model_config() -> dict:
    return {
        "models": {
            "defaults": [
                {
                    "model_client_config": {
                        "api_base": "https://builtin.example/v1/",
                        "api_key": "sk-builtin",
                        "model_name": "glm-5.3",
                        "client_provider": "OpenAI",
                    },
                    "model_config_obj": {"temperature": 0.1},
                },
                {
                    "model_client_config": {
                        "api_base": "https://third-party.example/v1/",
                        "api_key": "sk-third-party",
                        "model_name": "glm-5.3",
                        "client_provider": "OpenAI",
                    },
                    "model_config_obj": {"temperature": 0.8},
                },
            ]
        },
        **_wrap_modes_team({"demo_team": {"team_name": "demo_team", "agents": {"leader": {}, "teammate": {}}}}),
    }


def test_requested_model_ref_selects_owner_after_defaults_reorder():
    config = _duplicate_request_model_config()
    config["models"]["defaults"].reverse()
    ref = _model_identity_ref(
        model_name="glm-5.3",
        provider="OpenAI",
        api_base="https://third-party.example/v1/",
    )

    spec = load_team_spec_dict(
        config_base=config,
        requested_model_name="glm-5.3",
        requested_model_ref=ref,
    )

    for agent in spec["agents"].values():
        assert agent["model"]["model_client_config"]["api_base"] == "https://third-party.example/v1/"


def test_load_team_spec_dict_logs_final_member_model_identity(monkeypatch):
    config = _duplicate_request_model_config()
    ref = _model_identity_ref(
        model_name="glm-5.3",
        provider="OpenAI",
        api_base="https://third-party.example/v1",
    )

    messages = []
    monkeypatch.setattr(
        team_config_loader.logger,
        "info",
        lambda message, *args: messages.append(message % args if args else message),
    )
    load_team_spec_dict(
        config_base=config,
        requested_model_name="glm-5.3",
        requested_model_ref=ref,
    )

    model_logs = [message for message in messages if "member model resolved" in message]
    assert len(model_logs) == 2
    assert all(ref in message for message in model_logs)
    assert all("sk-third-party" not in message for message in model_logs)


def test_requested_model_ref_name_mismatch_fails_closed():
    ref = _model_identity_ref(
        model_name="glm-5.3",
        provider="OpenAI",
        api_base="https://third-party.example/v1/",
    )

    with pytest.raises(ValueError, match="name mismatch"):
        load_team_spec_dict(
            config_base=_duplicate_request_model_config(),
            requested_model_name="qwen3.7-max",
            requested_model_ref=ref,
        )


def test_requested_model_ref_unknown_and_ambiguous_fail_closed():
    unknown_ref = _model_identity_ref(
        model_name="glm-5.3",
        provider="OpenAI",
        api_base="https://unknown.example/v1/",
    )
    with pytest.raises(ValueError, match="owner not found"):
        load_team_spec_dict(
            config_base=_duplicate_request_model_config(),
            requested_model_name="glm-5.3",
            requested_model_ref=unknown_ref,
        )

    ambiguous_config = _duplicate_request_model_config()
    ambiguous_config["models"]["defaults"].append(dict(ambiguous_config["models"]["defaults"][0]))
    shared_ref = _model_identity_ref(
        model_name="glm-5.3",
        provider="OpenAI",
        api_base="https://builtin.example/v1/",
    )
    with pytest.raises(ValueError, match="owner is ambiguous"):
        load_team_spec_dict(
            config_base=ambiguous_config,
            requested_model_name="glm-5.3",
            requested_model_ref=shared_ref,
        )


def test_member_model_refs_allow_auto_assignment_across_same_name_providers():
    config = _duplicate_request_model_config()
    first_ref = _model_identity_ref(
        model_name="glm-5.3",
        provider="OpenAI",
        api_base="https://builtin.example/v1",
    )
    second_ref = _model_identity_ref(
        model_name="glm-5.3",
        provider="OpenAI",
        api_base="https://third-party.example/v1",
    )
    config["modes"]["team"]["demo_team"]["agents"] = {
        "leader": {"model": {"ref": first_ref}},
        "teammate": {"model": {"ref": second_ref}},
    }

    spec = load_team_spec_dict(config_base=config)

    assert (
        spec["agents"]["leader"]["model"]["model_client_config"]["api_base"]
        == "https://builtin.example/v1/"
    )
    assert (
        spec["agents"]["teammate"]["model"]["model_client_config"]["api_base"]
        == "https://third-party.example/v1/"
    )

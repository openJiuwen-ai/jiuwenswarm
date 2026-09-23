# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

from types import SimpleNamespace

import pytest
import yaml

from jiuwenswarm.agents.harness.team.config_loader import load_team_spec_dict
from jiuwenswarm.server.runtime.team_entity_store import (
    TeamEntityStoreError,
    TeamEntityStore,
    ensure_team_entity,
    ensure_team_entity_for_binding,
)
from jiuwenswarm.server.runtime.team_binding_store import TeamBindingStoreError


def test_team_entity_store_writes_team_workspace_metadata(tmp_path) -> None:
    store = TeamEntityStore(tmp_path / ".agent_teams")

    entity = store.write(
        team_name="research_team",
        template_id="default",
        template_snapshot={"team_name": "template_team", "leader": {"member_name": "lead"}},
        created_at=123.0,
    )

    path = tmp_path / ".agent_teams" / "research_team" / "team-workspace" / ".team-meta" / "team.yaml"
    assert path.is_file()
    assert entity.team_name == "research_team"
    assert entity.created_at == 123.0

    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert raw["version"] == 1
    assert raw["team_name"] == "research_team"
    assert raw["template_id"] == "default"
    assert raw["template_snapshot"]["leader"]["member_name"] == "lead"

    reloaded = store.get("research_team")
    assert reloaded is not None
    assert reloaded.template_snapshot["team_name"] == "template_team"


def test_team_entity_store_rejects_invalid_team_name(tmp_path) -> None:
    store = TeamEntityStore(tmp_path / ".agent_teams")

    with pytest.raises(TeamBindingStoreError):
        store.write(
            team_name="../escape",
            template_id="default",
            template_snapshot={"team_name": "template_team"},
        )


@pytest.mark.parametrize(
    "sensitive_snapshot",
    [
        {"agents": {"leader": {"model": {"model_client_config": {"api_key": "secret"}}}}},
        {"agents": {"leader": {"model": {"model_client_config": {"api_base": "https://api.test"}}}}},
        {
            "agents": {
                "leader": {
                    "model": {
                        "model_client_config": {
                            "custom_headers": {"Authorization": "Bearer secret"}
                        }
                    }
                }
            }
        },
        {
            "agents": {
                "leader": {
                    "model": {
                        "model_client_config": {
                            "custom_headers": {"nested": {"Authorization": "secret"}}
                        }
                    }
                }
            }
        },
        {
            "agents": {
                "leader": {
                    "model": {
                        "model_client_config": {
                            "custom_headers": {"X_Auth_Token": "secret"}
                        }
                    }
                }
            }
        },
        {
            "agents": {
                "leader": {
                    "model": {
                        "model_request_config": {
                            "extra_headers": {"Authorization": "Bearer secret"}
                        }
                    }
                }
            }
        },
        {
            "agents": {
                "leader": {
                    "model": {
                        "model_request_config": {
                            "default_headers": {"X-Api-Key": "secret"}
                        }
                    }
                }
            }
        },
    ],
)
def test_team_entity_store_rejects_sensitive_new_snapshots(tmp_path, sensitive_snapshot) -> None:
    store = TeamEntityStore(tmp_path / ".agent_teams")

    with pytest.raises(TeamEntityStoreError, match="sensitive"):
        store.write(
            team_name="research_team",
            template_id="default",
            template_snapshot={"team_name": "template_team", **sensitive_snapshot},
        )

    assert not store.entity_path("research_team").exists()


def test_team_entity_store_persists_model_refs_without_credentials(tmp_path) -> None:
    store = TeamEntityStore(tmp_path / ".agent_teams")
    model_ref = f"model-identity-v1:{'a' * 64}"

    store.write(
        team_name="research_team",
        template_id="default",
        template_snapshot={
            "team_name": "template_team",
            "agents": {"leader": {"model": {"ref": model_ref}}},
        },
    )

    raw = yaml.safe_load(store.entity_path("research_team").read_text(encoding="utf-8"))
    assert raw["template_snapshot"]["agents"]["leader"]["model"] == {"ref": model_ref}


def test_team_entity_store_keeps_legacy_inline_snapshots_readable_without_runtime_rewrite(tmp_path) -> None:
    store = TeamEntityStore(tmp_path / ".agent_teams")
    path = store.entity_path("research_team")
    path.parent.mkdir(parents=True)
    path.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "team_name": "research_team",
                "template_id": "default",
                "created_at": 1.0,
                "updated_at": 1.0,
                "template_snapshot": {
                    "team_name": "template_team",
                    "agents": {
                        "leader": {
                            "model": {
                                "model_client_config": {
                                    "api_base": "https://legacy.test",
                                    "api_key": "legacy-secret",
                                }
                            }
                        }
                    },
                },
            }
        ),
        encoding="utf-8",
    )

    persisted_before_resume = path.read_text(encoding="utf-8")
    entity = ensure_team_entity(
        team_name="research_team",
        template_id="default",
        config_base={"models": {"defaults": []}},
        store=store,
    )

    assert entity is not None
    assert entity.template_snapshot["agents"]["leader"]["model"]["model_client_config"]["api_key"] == "legacy-secret"
    assert path.read_text(encoding="utf-8") == persisted_before_resume
    spec = load_team_spec_dict(
        config_base={"models": {"defaults": []}},
        template_id=entity.template_id,
        template_snapshot=entity.template_snapshot,
    )
    assert spec["max_debate_rounds"] == 5


def test_team_entity_store_delete_removes_complete_team_directory(tmp_path) -> None:
    store = TeamEntityStore(tmp_path / ".agent_teams")
    store.write(
        team_name="research_team",
        template_id="default",
        template_snapshot={"team_name": "template_team"},
    )
    team_path = tmp_path / ".agent_teams" / "research_team"
    artifact_path = team_path / "team-workspace" / "artifacts" / "report.md"
    artifact_path.parent.mkdir(parents=True)
    artifact_path.write_text("report", encoding="utf-8")

    assert store.delete_team_directory("research_team") is True
    assert not team_path.exists()
    assert store.delete_team_directory("research_team") is False


def test_team_entity_store_delete_does_not_follow_team_symlink(tmp_path) -> None:
    teams_home = tmp_path / ".agent_teams"
    teams_home.mkdir()
    external_path = tmp_path / "external"
    external_path.mkdir()
    external_file = external_path / "keep.txt"
    external_file.write_text("keep", encoding="utf-8")
    team_link = teams_home / "research_team"
    try:
        team_link.symlink_to(external_path, target_is_directory=True)
    except OSError:
        # Creating symlinks needs privilege on Windows; the rejection
        # semantics are covered where symlinks are creatable.
        pytest.skip("symlink creation requires privilege on this platform")

    store = TeamEntityStore(teams_home)

    assert store.delete_team_directory("research_team") is True
    assert not team_link.exists()
    assert external_file.read_text(encoding="utf-8") == "keep"


def test_team_entity_store_delete_only_removes_metadata(tmp_path) -> None:
    store = TeamEntityStore(tmp_path / ".agent_teams")
    store.write(
        team_name="research_team",
        template_id="default",
        template_snapshot={"team_name": "template_team"},
    )
    team_path = tmp_path / ".agent_teams" / "research_team"
    artifact_path = team_path / "team-workspace" / "artifacts" / "report.md"
    artifact_path.parent.mkdir(parents=True)
    artifact_path.write_text("report", encoding="utf-8")

    assert store.delete("research_team") is True
    assert not store.entity_path("research_team").exists()
    assert artifact_path.read_text(encoding="utf-8") == "report"


def test_ensure_team_entity_for_binding_migrates_from_current_config(tmp_path) -> None:
    store = TeamEntityStore(tmp_path / ".agent_teams")
    binding = SimpleNamespace(
        team_name="research_team",
        template_id="research",
        created_at=42.0,
        template_snapshot=None,
    )
    config = {
        "modes": {
            "team": {
                "research": {
                    "team_name": "template_team",
                    "leader": {"member_name": "lead"},
                }
            }
        }
    }

    entity = ensure_team_entity_for_binding(binding, config_base=config, store=store)

    assert entity is not None
    assert entity.team_name == "research_team"
    assert entity.template_id == "research"
    assert entity.template_snapshot["leader"]["member_name"] == "lead"
    assert store.entity_path("research_team").is_file()


def test_ensure_team_entity_converts_inline_model_to_stable_ref(tmp_path) -> None:
    store = TeamEntityStore(tmp_path / ".agent_teams")
    binding = SimpleNamespace(
        team_name="research_team",
        template_id="research",
        created_at=42.0,
        template_snapshot=None,
    )
    config = {
        "models": {
            "defaults": [
                {
                    "model_client_config": {
                        "model_name": "shared-model",
                        "api_base": "https://first.test/v1",
                        "api_key": "first-secret",
                        "client_provider": "OpenAI",
                    }
                },
                {
                    "model_client_config": {
                        "model_name": "shared-model",
                        "api_base": "https://second.test/v1",
                        "api_key": "second-secret",
                        "client_provider": "OpenAI",
                    }
                },
            ]
        },
        "modes": {
            "team": {
                "research": {
                    "team_name": "template_team",
                    "agents": {
                        "leader": {
                            "model": {
                                "model_client_config": {
                                    "api_base": "https://second.test/v1/",
                                    "api_key": "second-secret",
                                    "client_provider": "OpenAI",
                                    "custom_headers": {},
                                },
                                "model_request_config": {"model": "shared-model"},
                            }
                        }
                    },
                }
            }
        },
    }

    entity = ensure_team_entity_for_binding(binding, config_base=config, store=store)

    assert entity is not None
    model_ref = entity.template_snapshot["agents"]["leader"]["model"]["ref"]
    assert model_ref.startswith("model-identity-v1:")
    assert len(model_ref.removeprefix("model-identity-v1:")) == 64
    persisted = store.entity_path("research_team").read_text(encoding="utf-8")
    assert "api_key" not in persisted
    assert "api_base" not in persisted
    assert "second-secret" not in persisted

    reordered_config = {
        **config,
        "models": {"defaults": list(reversed(config["models"]["defaults"]))},
    }
    spec = load_team_spec_dict(
        config_base=reordered_config,
        template_id=entity.template_id,
        template_snapshot=entity.template_snapshot,
    )
    resolved_model = spec["agents"]["leader"]["model"]
    assert resolved_model["model_client_config"]["api_base"] == "https://second.test/v1"
    assert resolved_model["model_client_config"]["api_key"] == "second-secret"


def test_ensure_team_entity_converts_legacy_index_ref_to_stable_ref(tmp_path) -> None:
    store = TeamEntityStore(tmp_path / ".agent_teams")
    binding = SimpleNamespace(
        team_name="research_team",
        template_id="research",
        created_at=42.0,
        template_snapshot={
            "team_name": "template_team",
            "agents": {"leader": {"model": {"ref": "shared-model#1"}}},
        },
    )
    defaults = [
        {
            "model_client_config": {
                "model_name": "shared-model",
                "api_base": "https://first.test/v1",
                "api_key": "first-secret",
                "client_provider": "OpenAI",
            }
        },
        {
            "model_client_config": {
                "model_name": "shared-model",
                "api_base": "https://second.test/v1",
                "api_key": "second-secret",
                "client_provider": "OpenAI",
            }
        },
    ]
    config = {"models": {"defaults": defaults}}

    entity = ensure_team_entity_for_binding(binding, config_base=config, store=store)

    assert entity is not None
    model_ref = entity.template_snapshot["agents"]["leader"]["model"]["ref"]
    assert model_ref.startswith("model-identity-v1:")
    assert "shared-model#1" not in store.entity_path("research_team").read_text(encoding="utf-8")
    spec = load_team_spec_dict(
        config_base={"models": {"defaults": list(reversed(defaults))}},
        template_id=entity.template_id,
        template_snapshot=entity.template_snapshot,
    )
    assert spec["agents"]["leader"]["model"]["model_client_config"]["api_base"] == "https://second.test/v1"


def test_ensure_team_entity_rejects_inline_model_without_exact_owner_match(tmp_path) -> None:
    store = TeamEntityStore(tmp_path / ".agent_teams")
    binding = SimpleNamespace(
        team_name="research_team",
        template_id="research",
        created_at=42.0,
        template_snapshot=None,
    )
    config = {
        "models": {
            "defaults": [
                {
                    "model_client_config": {
                        "model_name": "shared-model",
                        "api_base": "https://different.test/v1",
                        "api_key": "different-secret",
                    }
                }
            ]
        },
        "modes": {
            "team": {
                "research": {
                    "team_name": "template_team",
                    "agents": {
                        "leader": {
                            "model": {
                                "model_client_config": {
                                    "api_base": "https://unowned.test/v1",
                                    "api_key": "unowned-secret",
                                },
                                "model_request_config": {"model": "shared-model"},
                            }
                        }
                    },
                }
            }
        },
    }

    with pytest.raises(TeamEntityStoreError, match="sensitive"):
        ensure_team_entity_for_binding(binding, config_base=config, store=store)

    assert not store.entity_path("research_team").exists()


def test_ensure_team_entity_converts_request_only_model_to_stable_ref(tmp_path) -> None:
    # Relay sent only the model name (model_request_config) without a credential owner.
    # A unique tenant owner in models.defaults must bind it to a stable ref at normalize time.
    store = TeamEntityStore(tmp_path / ".agent_teams")
    binding = SimpleNamespace(
        team_name="research_team",
        template_id="research",
        created_at=42.0,
        template_snapshot=None,
    )
    config = {
        "models": {
            "defaults": [
                {
                    "model_client_config": {
                        "model_name": "glm-5.2",
                        "api_base": "https://maas.test/v1",
                        "api_key": "maas-secret",
                        "client_provider": "OpenAI",
                    },
                    "model_config_obj": {"temperature": 0.9, "top_p": 0.8},
                }
            ]
        },
        "modes": {
            "team": {
                "research": {
                    "team_name": "template_team",
                    "agents": {
                        "leader": {
                            "model": {
                                "model_request_config": {
                                    "model": "glm-5.2",
                                    "temperature": 0.2,
                                }
                            }
                        }
                    },
                }
            }
        },
    }

    entity = ensure_team_entity_for_binding(binding, config_base=config, store=store)

    assert entity is not None
    model_ref = entity.template_snapshot["agents"]["leader"]["model"]["ref"]
    assert model_ref.startswith("model-identity-v1:")
    assert entity.template_snapshot["agents"]["leader"]["model"]["model_request_config"] == {
        "temperature": 0.2
    }
    persisted = store.entity_path("research_team").read_text(encoding="utf-8")
    assert "api_key" not in persisted
    assert "maas-secret" not in persisted
    spec = load_team_spec_dict(
        config_base=config,
        template_id=entity.template_id,
        template_snapshot=entity.template_snapshot,
    )
    assert spec["agents"]["leader"]["model"]["model_client_config"]["api_key"] == "maas-secret"
    assert spec["agents"]["leader"]["model"]["model_request_config"]["model"] == "glm-5.2"
    assert spec["agents"]["leader"]["model"]["model_request_config"]["temperature"] == 0.2
    assert spec["agents"]["leader"]["model"]["model_request_config"]["top_p"] == 0.8


def test_ensure_team_entity_rejects_request_only_model_without_owner(tmp_path) -> None:
    # Request-only model whose name matches no tenant owner: reject at bind, don't pass
    # it through to TeamAgentSpec creation as a generic "model_client_config Field required".
    store = TeamEntityStore(tmp_path / ".agent_teams")
    binding = SimpleNamespace(
        team_name="research_team",
        template_id="research",
        created_at=42.0,
        template_snapshot=None,
    )
    config = {
        "models": {
            "defaults": [
                {
                    "model_client_config": {
                        "model_name": "other-model",
                        "api_base": "https://maas.test/v1",
                        "api_key": "maas-secret",
                        "client_provider": "OpenAI",
                    }
                }
            ]
        },
        "modes": {
            "team": {
                "research": {
                    "team_name": "template_team",
                    "agents": {
                        "leader": {"model": {"model_request_config": {"model": "glm-5.2"}}}
                    },
                }
            }
        },
    }

    with pytest.raises(TeamEntityStoreError, match="credential owner"):
        ensure_team_entity_for_binding(binding, config_base=config, store=store)

    assert not store.entity_path("research_team").exists()


def test_ensure_team_entity_rejects_request_only_model_with_ambiguous_owner(tmp_path) -> None:
    # Request-only model name matches more than one tenant owner (different endpoints):
    # cannot auto-pick, reject explicitly at bind instead of silently passing through.
    store = TeamEntityStore(tmp_path / ".agent_teams")
    binding = SimpleNamespace(
        team_name="research_team",
        template_id="research",
        created_at=42.0,
        template_snapshot=None,
    )
    config = {
        "models": {
            "defaults": [
                {
                    "model_client_config": {
                        "model_name": "glm-5.2",
                        "api_base": "https://first.test/v1",
                        "api_key": "first-secret",
                        "client_provider": "OpenAI",
                    }
                },
                {
                    "model_client_config": {
                        "model_name": "glm-5.2",
                        "api_base": "https://second.test/v1",
                        "api_key": "second-secret",
                        "client_provider": "OpenAI",
                    }
                },
            ]
        },
        "modes": {
            "team": {
                "research": {
                    "team_name": "template_team",
                    "agents": {
                        "leader": {"model": {"model_request_config": {"model": "glm-5.2"}}}
                    },
                }
            }
        },
    }

    with pytest.raises(TeamEntityStoreError, match="credential owner"):
        ensure_team_entity_for_binding(binding, config_base=config, store=store)

    assert not store.entity_path("research_team").exists()


def test_ensure_team_entity_rejects_stale_model_reference(tmp_path) -> None:
    store = TeamEntityStore(tmp_path / ".agent_teams")
    binding = SimpleNamespace(
        team_name="research_team",
        template_id="research",
        created_at=42.0,
        template_snapshot={
            "team_name": "template_team",
            "agents": {
                "leader": {
                    "model": {"ref": f"model-identity-v1:{'0' * 64}"},
                }
            },
        },
    )
    config = {
        "models": {
            "defaults": [
                {
                    "model_client_config": {
                        "model_name": "shared-model",
                        "api_base": "https://owned.test/v1",
                        "api_key": "current-secret",
                        "client_provider": "OpenAI",
                    }
                }
            ]
        }
    }

    with pytest.raises(TeamEntityStoreError, match="model reference"):
        ensure_team_entity_for_binding(binding, config_base=config, store=store)

    assert not store.entity_path("research_team").exists()


def test_ensure_team_entity_for_binding_returns_none_when_template_missing(tmp_path) -> None:
    store = TeamEntityStore(tmp_path / ".agent_teams")
    binding = SimpleNamespace(
        team_name="research_team",
        template_id="missing",
        created_at=42.0,
        template_snapshot=None,
    )

    assert ensure_team_entity_for_binding(binding, config_base={"modes": {"team": {}}}, store=store) is None

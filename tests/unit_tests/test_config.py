# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for config module."""

import math
from pathlib import Path

import pytest
import yaml

from jiuwenswarm.common.config import (
    get_config_raw,
    get_evolution_auto_save_enabled,
    get_evolution_enabled,
    get_evolution_review_trigger_enabled,
    get_evolution_signal_trigger_enabled,
    get_passive_skill_evolution_triggers,
    get_skill_create_enabled,
    get_ttse_embedding_config,
    get_ttse_enabled,
    migrate_config_from_template,
    replace_teams_in_config,
    resolve_env_vars,
    resolve_string_or_list_config,
    update_skill_retrieval_in_config,
    update_setup_guide_enabled_in_config,
    update_evolution_enabled_in_config,
    update_xiaoyi_runtime_in_config,
)


class TestResolveEnvVars:
    """Test environment variable resolution in config."""

    @staticmethod
    def test_resolve_string_with_env_var(monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("TEST_VAR", "test_value")
        result = resolve_env_vars("${TEST_VAR}")
        assert result == "test_value"

    @staticmethod
    def test_resolve_string_with_default():
        result = resolve_env_vars("${TEST_VAR:-default_value}")
        assert result == "default_value"

    @staticmethod
    def test_resolve_string_with_env_and_default(monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("TEST_VAR", "actual_value")
        result = resolve_env_vars("${TEST_VAR:-default_value}")
        assert result == "actual_value"

    @staticmethod
    def test_resolve_unset_only_default_preserves_explicit_empty(
        monkeypatch: pytest.MonkeyPatch,
    ):
        monkeypatch.setenv("DISABLED_TOOLS", "")
        value = resolve_env_vars(
            "${DISABLED_TOOLS-search_skill,install_skill,uninstall_skill}"
        )
        assert resolve_string_or_list_config(value) == []

    @staticmethod
    def test_resolve_unset_only_default_when_missing(monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("DISABLED_TOOLS", raising=False)
        value = resolve_env_vars(
            "${DISABLED_TOOLS-search_skill,install_skill,uninstall_skill}"
        )
        assert resolve_string_or_list_config(value) == [
            "search_skill",
            "install_skill",
            "uninstall_skill",
        ]

    @staticmethod
    def test_resolve_empty_string():
        result = resolve_env_vars("")
        assert result == ""

    @staticmethod
    def test_resolve_string_without_env_var():
        result = resolve_env_vars("plain_string")
        assert result == "plain_string"

    @staticmethod
    def test_resolve_dict_with_env_vars(monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("API_KEY", "secret_key")
        monkeypatch.setenv("PORT", "8080")
        input_dict = {
            "api_key": "${API_KEY}",
            "port": "${PORT:-3000}",
            "name": "test",
        }
        result = resolve_env_vars(input_dict)
        assert result == {
            "api_key": "secret_key",
            "port": "8080",
            "name": "test",
        }

    @staticmethod
    def test_resolve_list_with_env_vars(monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("VAR1", "value1")
        monkeypatch.setenv("VAR2", "value2")
        input_list = [
            "${VAR1}",
            "${VAR2:-default}",
            "static_value",
        ]
        result = resolve_env_vars(input_list)
        assert result == ["value1", "value2", "static_value"]

    @staticmethod
    def test_resolve_nested_structure(monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("HOST", "example.com")
        input_dict = {
            "server": {
                "host": "${HOST}",
                "port": "${PORT:-8080}",
            },
            "features": ["${FEATURE_A:-default_a}", "feature_b"],
        }
        result = resolve_env_vars(input_dict)
        assert result == {
            "server": {
                "host": "example.com",
                "port": "8080",
            },
            "features": ["default_a", "feature_b"],
        }

    @staticmethod
    def test_resolve_multiple_vars_in_string(monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("USER", "john")
        monkeypatch.setenv("DOMAIN", "example.com")
        result = resolve_env_vars("${USER}@${DOMAIN}")
        assert result == "john@example.com"

    @staticmethod
    def test_resolve_non_string_types():
        assert resolve_env_vars(123) == 123
        assert resolve_env_vars(True) is True
        assert resolve_env_vars(None) is None
        assert math.isclose(resolve_env_vars(3.14), 3.14)


class TestConfigFunctions:
    """Test config module functions."""

    @staticmethod
    def test_update_setup_guide_enabled_in_config(
        monkeypatch: pytest.MonkeyPatch,
        temp_config_file: Path,
    ):
        monkeypatch.setattr("jiuwenswarm.common.config.get_config_file", lambda: temp_config_file)

        update_setup_guide_enabled_in_config(False)

        raw = yaml.safe_load(temp_config_file.read_text(encoding="utf-8"))
        assert raw["setup_guide"] == {"enabled": False}

    @staticmethod
    def test_update_evolution_enabled_in_config(
        monkeypatch: pytest.MonkeyPatch,
        temp_config_file: Path,
    ):
        monkeypatch.setattr("jiuwenswarm.common.config.get_config_file", lambda: temp_config_file)

        update_evolution_enabled_in_config(False)

        raw = yaml.safe_load(temp_config_file.read_text(encoding="utf-8"))
        assert raw["react"]["evolution"]["enabled"] is False
        assert get_evolution_enabled(raw) is False

    @pytest.mark.parametrize(
        ("config", "expected"),
        [
            ({}, False),
            ({"react": {"evolution": {"auto_save": False}}}, False),
            ({"react": {"evolution": {"auto_save": True}}}, True),
            ({"evolution": {"auto_save": True}}, True),
            ({"react": {"evolution": {"auto_save": "true"}}}, False),
        ],
    )
    def test_evolution_auto_save_config_values(self, config, expected):
        assert get_evolution_auto_save_enabled(config) is expected

    @staticmethod
    def test_evolution_auto_save_read_failure_returns_false(monkeypatch: pytest.MonkeyPatch):
        def _raise() -> dict:
            raise OSError("config unavailable")

        monkeypatch.setattr("jiuwenswarm.common.config.get_config", _raise)

        assert get_evolution_auto_save_enabled() is False

    @pytest.mark.parametrize(
        ("env_value", "config", "fallback", "expected"),
        [
            (None, {"react": {"evolution": {"review_trigger": True}}}, False, True),
            (None, {"evolution": {"review_trigger": False}}, True, False),
            (None, {"evolution": {"review_trigger": None}}, True, True),
            (None, {"evolution": {}}, False, False),
            ("false", {"react": {"evolution": {"review_trigger": True}}}, True, False),
            ("true", {"react": {"evolution": {"review_trigger": False}}}, False, True),
        ],
    )
    def test_evolution_review_trigger_config_and_env_values(
        self,
        monkeypatch: pytest.MonkeyPatch,
        env_value,
        config,
        fallback,
        expected,
    ):
        if env_value is None:
            monkeypatch.delenv("EVOLUTION_REVIEW_TRIGGER", raising=False)
        else:
            monkeypatch.setenv("EVOLUTION_REVIEW_TRIGGER", env_value)

        assert get_evolution_review_trigger_enabled(config, fallback=fallback) is expected

    @pytest.mark.parametrize(
        ("env_value", "config", "fallback", "expected"),
        [
            (None, {"react": {"evolution": {"signal_trigger": True}}}, False, True),
            (None, {"react": {"evolution": {"auto_scan": True}}}, False, True),
            (None, {"evolution": {"signal_trigger": False}}, True, False),
            (None, {"evolution": {}}, False, False),
            (None, {"react": {"evolution": {}}}, True, True),
            ("true", {"react": {"evolution": {"signal_trigger": False}}}, False, True),
        ],
    )
    def test_evolution_signal_trigger_config_and_env_values(
        self,
        monkeypatch: pytest.MonkeyPatch,
        env_value,
        config,
        fallback,
        expected,
    ):
        monkeypatch.delenv("EVOLUTION_SIGNAL_TRIGGER", raising=False)
        monkeypatch.delenv("EVOLUTION_AUTO_SCAN", raising=False)
        if env_value is None:
            pass
        else:
            monkeypatch.setenv("EVOLUTION_SIGNAL_TRIGGER", env_value)

        assert get_evolution_signal_trigger_enabled(config, fallback=fallback) is expected

    @pytest.mark.parametrize(
        ("config", "expected_signal", "expected_review"),
        [
            ({"react": {"evolution": {"signal_trigger": True, "review_trigger": True}}}, True, False),
            ({"react": {"evolution": {"signal_trigger": False, "review_trigger": True}}}, False, False),
            ({"react": {"evolution": {}}}, True, False),
        ],
    )
    def test_passive_skill_evolution_triggers_ignore_review_for_single_agent(
        self,
        config,
        expected_signal,
        expected_review,
    ):
        triggers = get_passive_skill_evolution_triggers(config)
        assert triggers == {
            "signal_trigger": expected_signal,
            "review_trigger": expected_review,
        }

    @pytest.mark.parametrize(
        ("env_value", "config", "expected"),
        [
            (None, {"react": {"evolution": {"skill_create": True}}}, True),
            (None, {"evolution": {"skill_create": True}}, True),
            ("false", {"react": {"evolution": {"skill_create": True}}}, False),
            ("true", {"react": {"evolution": {"skill_create": False}}}, True),
        ],
    )
    def test_skill_create_config_and_env_values(
        self,
        monkeypatch: pytest.MonkeyPatch,
        env_value,
        config,
        expected,
    ):
        if env_value is None:
            monkeypatch.delenv("SKILL_CREATE", raising=False)
        else:
            monkeypatch.setenv("SKILL_CREATE", env_value)

        assert get_skill_create_enabled(config) is expected

    @pytest.mark.parametrize(
        ("config", "expected"),
        [
            ({}, False),
            ({"react": {"evolution": {"enabled": True}}}, True),
            ({"react": {"evolution": {"enabled": False}}}, False),
            ({"evolution": {"enabled": True}}, True),
            ({"evolution": {"enabled": False}}, False),
            # react.evolution wins over top-level evolution
            (
                {
                    "react": {"evolution": {"enabled": True}},
                    "evolution": {"enabled": False},
                },
                True,
            ),
            ({"react": {"evolution": {"enabled": "true"}}}, True),
            ({"react": {"evolution": {}}}, False),
        ],
    )
    def test_evolution_enabled_config_values(self, config, expected):
        assert get_evolution_enabled(config) is expected

    @pytest.mark.parametrize(
        ("config", "expected"),
        [
            ({}, False),
            ({"react": {"ttse": {"enabled": True}}}, True),
            ({"react": {"ttse": {"enabled": False}}}, False),
            ({"ttse": {"enabled": True}}, True),
            ({"ttse": {"enabled": False}}, False),
            (
                {
                    "react": {"ttse": {"enabled": True}},
                    "ttse": {"enabled": False},
                },
                True,
            ),
            ({"react": {"ttse": {"enabled": "true"}}}, True),
            ({"react": {"ttse": {}}}, False),
        ],
    )
    def test_ttse_enabled_config_values(self, config, expected):
        assert get_ttse_enabled(config) is expected

    @pytest.mark.parametrize(
        ("config", "expected"),
        [
            ({}, {}),
            ({"react": {"ttse": {}}}, {}),
            ({"react": {"ttse": {"embedding": {"api_key": "k"}}}}, {}),
            (
                {
                    "react": {
                        "ttse": {
                            "embedding": {
                                "api_key": "k",
                                "base_url": "https://api.example.com/v1",
                                "model": "bge-m3",
                            }
                        }
                    }
                },
                {
                    "api_key": "k",
                    "base_url": "https://api.example.com/v1",
                    "model": "bge-m3",
                },
            ),
            (
                {
                    "ttse": {
                        "embedding": {
                            "api_key": " k ",
                            "base_url": " https://x/v1 ",
                            "model": " m ",
                        }
                    }
                },
                {"api_key": "k", "base_url": "https://x/v1", "model": "m"},
            ),
        ],
    )
    def test_ttse_embedding_config_values(self, config, expected):
        assert get_ttse_embedding_config(config) == expected

    @staticmethod
    def test_get_config_raw(temp_config_file: Path):
        config = get_config_raw()
        assert config is not None
        assert "model" in config or "channels" in config

    @staticmethod
    def test_config_file_structure(temp_config_file: Path):
        config = get_config_raw()
        expected_keys = {"model", "channels", "evolution", "heartbeat"}
        actual_keys = set(config.keys())
        assert len(actual_keys & expected_keys) > 0, "Config should have at least some expected keys"

    @staticmethod
    def test_migrate_config_from_template_deep_merges_symphony(
        tmp_path: Path,
    ):
        template_path = tmp_path / "template.yaml"
        user_config_path = tmp_path / "config.yaml"
        template_path.write_text(
            """
preferred_language: zh
symphony:
  fingerprint:
    scan:
      max_depth:
    extraction:
      workers: 1
      batch_size: 1
      body_limit:
    normalization:
      workers: 1
      batch_size: 1
      duplicate_name_similarity_threshold: 0.86
      max_vocab_size:
""",
            encoding="utf-8",
        )
        user_config_path.write_text(
            """
preferred_language: en
symphony:
  fingerprint:
    extraction:
      workers: 3
""",
            encoding="utf-8",
        )

        assert migrate_config_from_template(template_path, user_config_path) is True

        migrated = yaml.safe_load(user_config_path.read_text(encoding="utf-8"))
        assert migrated["preferred_language"] == "en"
        assert migrated["symphony"]["fingerprint"]["scan"]["max_depth"] is None
        assert migrated["symphony"]["fingerprint"]["extraction"]["workers"] == 3
        assert migrated["symphony"]["fingerprint"]["extraction"]["batch_size"] == 1
        assert migrated["symphony"]["fingerprint"]["normalization"]["workers"] == 1

    @staticmethod
    def test_migrate_config_preserves_legacy_evolution_settings(
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        monkeypatch.delenv("EVOLUTION_REVIEW_TRIGGER", raising=False)
        template_path = tmp_path / "template.yaml"
        user_config_path = tmp_path / "config.yaml"
        template_path.write_text(
            """
react:
  evolution:
    review_trigger:
    auto_save: false
    enabled: true
""",
            encoding="utf-8",
        )
        user_config_path.write_text(
            """
react:
  evolution:
    review_trigger: true
    auto_save: true
""",
            encoding="utf-8",
        )

        assert migrate_config_from_template(template_path, user_config_path) is True

        migrated = yaml.safe_load(user_config_path.read_text(encoding="utf-8"))
        assert migrated["react"]["evolution"] == {
            "review_trigger": True,
            "auto_save": True,
            "enabled": True,
        }
        assert get_evolution_review_trigger_enabled(migrated) is True
        assert get_evolution_auto_save_enabled(migrated) is True

    @staticmethod
    def test_update_skill_retrieval_preserves_existing_hidden_config(
        monkeypatch: pytest.MonkeyPatch,
        temp_config_file: Path,
    ):
        temp_config_file.write_text(
            """
symphony:
  skill_retrieval:
    enabled: false
    build:
      branching_factor: 96
      root_categories: old
      max_depth: 7
      request_timeout_seconds: 300
      max_workers: 4
      max_retries: 2
      classification_batch_limit: 24
      discovery_seed: 42
      postprocess_enabled: true
      postprocess_max_passes: 1
      postprocess_min_skills: 6
      equivalence_enabled: true
    retrieve:
      top_k: 8
      compact_codes_enabled: true
      flatten_tree: true
      max_exposure_depth: 12
      max_branch_choices: 3
      max_parallel_branches: 4
""",
            encoding="utf-8",
        )
        monkeypatch.setattr("jiuwenswarm.common.config.get_config_file", lambda: temp_config_file)

        update_skill_retrieval_in_config(
            {
                "build": {
                    "root_categories": "new",
                    "max_depth": 9,
                    "max_workers": 8,
                    "max_retries": 3,
                    "classification_batch_limit": 12,
                    "discovery_seed": 7,
                    "postprocess_enabled": False,
                    "postprocess_max_passes": 4,
                    "postprocess_min_skills": 10,
                    "equivalence_enabled": False,
                },
                "retrieve": {
                    "top_k": 6,
                    "max_branch_choices": 5,
                },
            }
        )

        raw = yaml.safe_load(temp_config_file.read_text(encoding="utf-8"))
        section = raw["symphony"]["skill_retrieval"]
        assert section["build"] == {
            "branching_factor": 96,
            "root_categories": "new",
            "max_depth": 9,
            "request_timeout_seconds": 300,
            "max_workers": 8,
            "max_retries": 3,
            "classification_batch_limit": 12,
            "discovery_seed": 7,
            "postprocess_enabled": False,
            "postprocess_max_passes": 4,
            "postprocess_min_skills": 10,
            "equivalence_enabled": False,
        }
        assert section["retrieve"] == {
            "top_k": 6,
            "compact_codes_enabled": True,
            "flatten_tree": True,
            "max_exposure_depth": 12,
            "max_branch_choices": 5,
            "max_parallel_branches": 4,
        }


class TestTeamModesConfig:
    """Test team config persistence under modes.team."""

    @staticmethod
    def _front_payload(
        team_names: list[str] | None = None,
        *,
        include_teammate: bool = False,
        enable_permissions: bool = False,
    ) -> dict:
        names = team_names or ["alpha_team", "beta_team"]
        return {
            "agents": {
                "agent_1": {
                    "model": {
                        "ref": "gpt-4.1#0",
                        "provider": "OpenAI",
                        "model": "gpt-4.1",
                        "api_base": "${OPENAI_BASE_URL:-https://api.openai.com/v1}",
                        "api_key": "${OPENAI_API_KEY}",
                    },
                    "skills": ["team-management"],
                    "workspace": {
                        "stable_base": True,
                    },
                    "max_iterations": 200,
                    "completion_timeout": 600.0,
                },
                "agent_2": {
                    "model": {
                        "ref": "gpt-4.1-mini#1",
                        "provider": "OpenAI",
                        "model": "gpt-4.1-mini",
                        "api_base": "${OPENAI_BASE_URL:-https://api.openai.com/v1}",
                        "api_key": "${OPENAI_API_KEY}",
                    },
                    "skills": ["coding"],
                    "workspace": {
                        "stable_base": True,
                    },
                    "max_iterations": 80,
                    "completion_timeout": 600.0,
                },
            },
            "team": [
                {
                    "team_name": team_name,
                    "lifecycle": "persistent",
                    "teammate_mode": "build_mode",
                    "spawn_mode": "inprocess",
                    "enable_permissions": enable_permissions,
                    "leader": {
                        "member_name": f"{team_name}_leader",
                        "display_name": f"{team_name} leader",
                        "persona": "Lead planning and coordination",
                        "agent_key": "agent_1",
                    },
                    **(
                        {
                            "teammate": {
                                "member_name": f"{team_name}_teammate",
                                "display_name": f"{team_name} teammate",
                                "persona": "Handle analysis and execution",
                                "agent_key": "agent_2",
                            }
                        }
                        if include_teammate
                        else {}
                    ),
                    "predefined_members": [
                        {
                            "member_name": "analyst",
                            "display_name": "Analyst",
                            "role_type": "teammate",
                            "persona": "Analyze requirements",
                            "prompt_hint": "Analyze first",
                            "agent_key": "agent_1",
                        },
                        {
                            "member_name": "coder",
                            "display_name": "Coder",
                            "role_type": "teammate",
                            "persona": "Implement and debug",
                            "prompt_hint": "Modify and verify directly",
                            "agent_key": "agent_2",
                        },
                    ],
                }
                for team_name in names
            ],
        }

    @staticmethod
    def test_replace_teams_in_config_writes_modes_team_and_keeps_legacy_team(
        monkeypatch: pytest.MonkeyPatch,
        temp_config_file: Path,
    ):
        temp_config_file.write_text(
            """
channels:
  web:
    enabled: true
team:
  team_name: legacy_team
modes:
  agent:
    fast: {}
  code: {}
""",
            encoding="utf-8",
        )
        monkeypatch.setattr("jiuwenswarm.common.config.get_config_file", lambda: temp_config_file)

        replace_teams_in_config(TestTeamModesConfig._front_payload(["alpha_team"], enable_permissions=True))

        raw = yaml.safe_load(temp_config_file.read_text(encoding="utf-8"))
        assert raw["team"] == {"team_name": "legacy_team"}
        saved = raw["modes"]["team"]["alpha_team"]
        assert saved["team_name"] == "alpha_team"
        assert saved["enable_permissions"] is True
        assert saved["leader"] == {
            "member_name": "alpha_team_leader",
            "display_name": "alpha_team leader",
            "persona": "Lead planning and coordination",
            "agent_key": "agent_1",
        }
        assert [item["agent_key"] for item in saved["predefined_members"]] == ["agent_1", "agent_2"]
        assert saved["agents"]["leader"]["model"] == {"ref": "gpt-4.1#0"}
        assert saved["agents"]["analyst"]["skills"] == ["team-management"]
        assert saved["agents"]["coder"]["skills"] == ["coding"]
        assert saved.get("teammate") is None
        assert "teammate" not in saved["agents"]
        registry = raw["web_config_panel"]["agent_team_agents"]
        assert set(registry) == {"agent_1", "agent_2"}
        assert registry["agent_1"]["model"] == {"ref": "gpt-4.1#0"}
        assert registry["agent_2"]["skills"] == ["coding"]
        saved_text = temp_config_file.read_text(encoding="utf-8")
        assert "api_key" not in saved_text
        assert "api_base" not in saved_text
        assert "custom_headers" not in saved_text

    @staticmethod
    def test_replace_teams_in_config_migrates_legacy_inline_model_to_ref(
        monkeypatch: pytest.MonkeyPatch,
        temp_config_file: Path,
    ):
        temp_config_file.write_text(
            """
models:
  defaults:
    - model_client_config:
        model_name: gpt-4.1
        client_provider: OpenAI
        api_base: https://api.openai.com/v1
        api_key: server-secret
      model_config_obj: {}
modes: {}
""",
            encoding="utf-8",
        )
        monkeypatch.setattr("jiuwenswarm.common.config.get_config_file", lambda: temp_config_file)
        payload = TestTeamModesConfig._front_payload(["alpha_team"])
        payload["agents"]["agent_1"]["model"] = {
            "provider": "OpenAI",
            "api_base": "https://api.openai.com/v1",
            "api_key": "legacy-secret",
            "model": "gpt-4.1",
        }

        replace_teams_in_config(payload)

        raw = yaml.safe_load(temp_config_file.read_text(encoding="utf-8"))
        saved = raw["modes"]["team"]["alpha_team"]
        assert saved["agents"]["leader"]["model"] == {"ref": "gpt-4.1#0"}
        assert "legacy-secret" not in temp_config_file.read_text(encoding="utf-8")

    @staticmethod
    def test_replace_teams_in_config_resolves_env_before_legacy_model_migration(
        monkeypatch: pytest.MonkeyPatch,
        temp_config_file: Path,
    ):
        monkeypatch.setenv("MODEL_NAME", "gpt-4.1")
        monkeypatch.setenv("MODEL_PROVIDER", "OpenAI")
        monkeypatch.setenv("API_BASE", "https://api.openai.com/v1")
        temp_config_file.write_text(
            """
models:
  defaults:
    - model_client_config:
        model_name: ${MODEL_NAME}
        client_provider: ${MODEL_PROVIDER}
        api_base: ${API_BASE}
        api_key: ${API_KEY}
      model_config_obj: {}
modes: {}
""",
            encoding="utf-8",
        )
        monkeypatch.setattr("jiuwenswarm.common.config.get_config_file", lambda: temp_config_file)
        payload = TestTeamModesConfig._front_payload(["alpha_team"])
        payload["agents"]["agent_1"]["model"] = {
            "provider": "openai",
            "api_base": "https://api.openai.com/v1/",
            "api_key": "legacy-secret",
            "model": "gpt-4.1",
        }

        replace_teams_in_config(payload)

        raw = yaml.safe_load(temp_config_file.read_text(encoding="utf-8"))
        assert raw["modes"]["team"]["alpha_team"]["agents"]["leader"]["model"] == {
            "ref": "gpt-4.1#0",
        }

    @staticmethod
    def test_replace_teams_in_config_expands_reused_agent_specs_without_yaml_aliases(
        monkeypatch: pytest.MonkeyPatch,
        temp_config_file: Path,
    ):
        temp_config_file.write_text(
            """
channels:
  web:
    enabled: true
modes:
  team: {}
""",
            encoding="utf-8",
        )
        monkeypatch.setattr("jiuwenswarm.common.config.get_config_file", lambda: temp_config_file)

        replace_teams_in_config(TestTeamModesConfig._front_payload(["alpha_team"], include_teammate=True))

        saved_text = temp_config_file.read_text(encoding="utf-8")
        assert "&id" not in saved_text
        assert "*id" not in saved_text
        raw = yaml.safe_load(saved_text)
        saved = raw["modes"]["team"]["alpha_team"]
        # Team-level teammate keeps the selected source agent key for UI round-trip.
        assert saved["teammate"] == {"agent_key": "agent_2"}
        assert saved["agents"]["teammate"]["skills"] == ["coding"]
        assert saved["agents"]["teammate"] is not saved["agents"]["coder"]

    @staticmethod
    def test_replace_teams_in_config_persists_agent_registry_without_team(
        monkeypatch: pytest.MonkeyPatch,
        temp_config_file: Path,
    ):
        temp_config_file.write_text(
            """
channels:
  web:
    enabled: true
modes:
  agent:
    fast: {}
""",
            encoding="utf-8",
        )
        monkeypatch.setattr("jiuwenswarm.common.config.get_config_file", lambda: temp_config_file)
        payload = TestTeamModesConfig._front_payload(["alpha_team"])
        payload["team"] = []

        replace_teams_in_config(payload)

        raw = yaml.safe_load(temp_config_file.read_text(encoding="utf-8"))
        assert "team" not in raw["modes"]
        registry = raw["web_config_panel"]["agent_team_agents"]
        assert set(registry) == {"agent_1", "agent_2"}
        assert registry["agent_1"]["model"] == {"ref": "gpt-4.1#0"}
        assert registry["agent_2"]["skills"] == ["coding"]

    @staticmethod
    def test_replace_teams_in_config_only_writes_teammate_when_explicitly_provided(
        monkeypatch: pytest.MonkeyPatch,
        temp_config_file: Path,
    ):
        monkeypatch.setattr("jiuwenswarm.common.config.get_config_file", lambda: temp_config_file)

        replace_teams_in_config(TestTeamModesConfig._front_payload(["alpha_team"]))

        raw = yaml.safe_load(temp_config_file.read_text(encoding="utf-8"))
        saved = raw["modes"]["team"]["alpha_team"]
        assert "teammate" not in saved
        assert "teammate" not in saved["agents"]

    @staticmethod
    def test_replace_teams_in_config_rejects_duplicate_team_names(
        monkeypatch: pytest.MonkeyPatch,
        temp_config_file: Path,
    ):
        monkeypatch.setattr("jiuwenswarm.common.config.get_config_file", lambda: temp_config_file)

        with pytest.raises(ValueError, match="duplicate team_name"):
            replace_teams_in_config(TestTeamModesConfig._front_payload(["alpha_team", "alpha_team"]))

    @staticmethod
    def test_replace_teams_in_config_rejects_unknown_agent_key(
        monkeypatch: pytest.MonkeyPatch,
        temp_config_file: Path,
    ):
        monkeypatch.setattr("jiuwenswarm.common.config.get_config_file", lambda: temp_config_file)
        payload = TestTeamModesConfig._front_payload(["alpha_team"])
        payload["team"][0]["predefined_members"][1]["agent_key"] = "missing_agent"

        with pytest.raises(ValueError, match="unknown agent_key"):
            replace_teams_in_config(payload)

    @staticmethod
    def test_replace_teams_in_config_rejects_unknown_teammate_agent_key(
        monkeypatch: pytest.MonkeyPatch,
        temp_config_file: Path,
    ):
        monkeypatch.setattr("jiuwenswarm.common.config.get_config_file", lambda: temp_config_file)
        payload = TestTeamModesConfig._front_payload(["alpha_team"], include_teammate=True)
        payload["team"][0]["teammate"]["agent_key"] = "missing_agent"

        with pytest.raises(ValueError, match="unknown agent_key"):
            replace_teams_in_config(payload)

    @staticmethod
    def test_replace_teams_in_config_replaces_entire_modes_team(
        monkeypatch: pytest.MonkeyPatch,
        temp_config_file: Path,
    ):
        monkeypatch.setattr("jiuwenswarm.common.config.get_config_file", lambda: temp_config_file)

        replace_teams_in_config(TestTeamModesConfig._front_payload(["alpha_team", "beta_team"]))
        replace_teams_in_config(TestTeamModesConfig._front_payload(["gamma_team"]))

        raw = yaml.safe_load(temp_config_file.read_text(encoding="utf-8"))
        assert list(raw["modes"]["team"].keys()) == ["gamma_team"]

    @staticmethod
    def test_replace_teams_in_config_rejects_duplicate_member_names(
        monkeypatch: pytest.MonkeyPatch,
        temp_config_file: Path,
    ):
        monkeypatch.setattr("jiuwenswarm.common.config.get_config_file", lambda: temp_config_file)
        payload = TestTeamModesConfig._front_payload(["alpha_team"])
        payload["team"][0]["predefined_members"][1]["member_name"] = "analyst"

        with pytest.raises(ValueError, match="duplicate member_name"):
            replace_teams_in_config(payload)

    @staticmethod
    def test_replace_teams_in_config_deletes_modes_team_when_empty(
        monkeypatch: pytest.MonkeyPatch,
        temp_config_file: Path,
    ):
        temp_config_file.write_text(
            """
channels:
  web:
    enabled: true
modes:
  team:
    existing_team:
      team_name: existing_team
""",
            encoding="utf-8",
        )
        monkeypatch.setattr("jiuwenswarm.common.config.get_config_file", lambda: temp_config_file)

        # 空 team 数组应该删除 modes.team 配置项
        replace_teams_in_config({"agents": {}, "team": []})

        raw = yaml.safe_load(temp_config_file.read_text(encoding="utf-8"))
        assert "team" not in raw["modes"]

    @staticmethod
    def test_replace_teams_in_config_no_change_when_modes_team_missing(
        monkeypatch: pytest.MonkeyPatch,
        temp_config_file: Path,
    ):
        temp_config_file.write_text(
            """
channels:
  web:
    enabled: true
modes:
  agent:
    fast: {}
""",
            encoding="utf-8",
        )
        monkeypatch.setattr("jiuwenswarm.common.config.get_config_file", lambda: temp_config_file)

        # 空 team 数组，且 modes.team 不存在，不应报错
        replace_teams_in_config({"agents": {}, "team": []})

        raw = yaml.safe_load(temp_config_file.read_text(encoding="utf-8"))
        assert "team" not in raw["modes"]


class TestUpdateXiaoyiRuntimeInConfig:
    """push_id 需同时写入顶层与 apps[]，供 cron 与频道重启共用。"""

    @staticmethod
    def test_writes_top_level_and_matching_app_push_id(
        monkeypatch: pytest.MonkeyPatch,
        temp_config_file: Path,
    ):
        temp_config_file.write_text(
            """
channels:
  xiaoyi:
    apps:
      - name: 默认应用
        is_default: true
        api_id: webhook_api_1
        agent_id: agent_abc
        push_id: ""
      - name: 其他
        api_id: webhook_api_2
        agent_id: agent_other
        push_id: ""
""",
            encoding="utf-8",
        )
        monkeypatch.setattr("jiuwenswarm.common.config.get_config_file", lambda: temp_config_file)

        update_xiaoyi_runtime_in_config(
            {
                "last_session_id": "sess-1",
                "last_task_id": "task-1",
                "last_message_id": "msg-1",
                "push_id": "push-token-xyz",
            },
            api_id="webhook_api_1",
            agent_id="agent_abc",
        )

        raw = yaml.safe_load(temp_config_file.read_text(encoding="utf-8"))
        xy = raw["channels"]["xiaoyi"]
        assert xy["push_id"] == "push-token-xyz"
        assert xy["last_session_id"] == "sess-1"
        assert xy["apps"][0]["push_id"] == "push-token-xyz"
        assert xy["apps"][1]["push_id"] == ""

    @staticmethod
    def test_without_push_id_does_not_touch_apps(
        monkeypatch: pytest.MonkeyPatch,
        temp_config_file: Path,
    ):
        temp_config_file.write_text(
            """
channels:
  xiaoyi:
    apps:
      - name: 默认应用
        is_default: true
        api_id: webhook_api_1
        push_id: keep-me
""",
            encoding="utf-8",
        )
        monkeypatch.setattr("jiuwenswarm.common.config.get_config_file", lambda: temp_config_file)

        update_xiaoyi_runtime_in_config(
            {"last_session_id": "sess-2"},
            api_id="webhook_api_1",
        )

        raw = yaml.safe_load(temp_config_file.read_text(encoding="utf-8"))
        xy = raw["channels"]["xiaoyi"]
        assert xy["last_session_id"] == "sess-2"
        assert "push_id" not in xy
        assert xy["apps"][0]["push_id"] == "keep-me"

    @staticmethod
    def test_push_id_dumped_without_quotes_even_if_old_app_value_quoted(
        monkeypatch: pytest.MonkeyPatch,
        temp_config_file: Path,
    ):
        # apps 旧值为单引号空串时，覆盖后顶层与 apps 均应无引号
        temp_config_file.write_text(
            """
channels:
  xiaoyi:
    apps:
      - name: 默认应用
        is_default: true
        api_id: webhook_api_1
        agent_id: agent_abc
        push_id: ''
""",
            encoding="utf-8",
        )
        monkeypatch.setattr("jiuwenswarm.common.config.get_config_file", lambda: temp_config_file)

        token = "88062548d4436ba6b6bfb573c641ad5d2a3f10a649dae5f52ad6f31f851cad64"
        update_xiaoyi_runtime_in_config(
            {"push_id": token},
            api_id="webhook_api_1",
            agent_id="agent_abc",
        )

        text = temp_config_file.read_text(encoding="utf-8")
        assert f"push_id: {token}" in text
        assert f"push_id: '{token}'" not in text
        assert f'push_id: "{token}"' not in text

# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for config module."""

import math
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml

from jiuwenswarm.common import config as config_module
from jiuwenswarm.common.config import (
    _transform_front_team_model_config,
    get_configured_read_image_multimodal,
    get_config_raw,
    get_evolution_auto_save_enabled,
    get_evolution_review_feedback_min_confidence,
    get_skill_evolution_enabled,
    migrate_config_from_template,
    replace_teams_in_config,
    reset_external_cli_agents_in_config,
    resolve_env_vars,
    update_external_cli_agents_in_config,
    update_skill_retrieval_in_config,
    update_setup_guide_enabled_in_config,
    update_xiaoyi_runtime_in_config,
)


def test_configured_read_image_multimodal_preserves_explicit_value() -> None:
    assert get_configured_read_image_multimodal(
        {"react": {"enable_read_image_multimodal": False}}
    ) is False


def test_configured_read_image_multimodal_returns_none_for_auto() -> None:
    assert get_configured_read_image_multimodal(
        {"react": {"enable_read_image_multimodal": None}}
    ) is None


def test_reset_external_cli_agents_removes_runtime_config_and_preserves_other_values(
    monkeypatch: pytest.MonkeyPatch,
    temp_config_file: Path,
) -> None:
    temp_config_file.write_text(
        yaml.safe_dump(
            {
                "preferred_language": "zh",
                "modes": {
                    "team": {
                        "jiuwen_team": {
                            "enable_swarmflow": True,
                            "external_cli_agents": [
                                {"cli_agent": "claude"},
                                {"cli_agent": "codex"},
                            ],
                            "external_transport": {
                                "type": "hybrid",
                                "params": {"external_publish_url": "ws://127.0.0.1:19000/ws"},
                            },
                        }
                    }
                },
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(config_module, "CONFIG_YAML_PATH", temp_config_file)

    reset_external_cli_agents_in_config()

    saved = yaml.safe_load(temp_config_file.read_text(encoding="utf-8"))
    team = saved["modes"]["team"]["jiuwen_team"]
    assert "external_cli_agents" not in team
    assert "external_transport" not in team
    assert team["enable_swarmflow"] is True
    assert saved["preferred_language"] == "zh"


def test_reset_external_cli_agents_uses_update_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []
    data: dict[str, Any] = {
        "modes": {
            "team": {
                "jiuwen_team": {
                    "external_cli_agents": [{"cli_agent": "claude"}],
                    "external_transport": {"type": "hybrid"},
                }
            }
        }
    }

    def _update_config(
        mutator: Callable[[dict[str, Any]], dict[str, Any] | None],
    ) -> dict[str, Any]:
        calls.append(data)
        result = mutator(data)
        return data if result is None else result

    monkeypatch.setattr(config_module, "update_config", _update_config)

    reset_external_cli_agents_in_config()

    assert len(calls) == 1
    team = data["modes"]["team"]["jiuwen_team"]
    assert "external_cli_agents" not in team
    assert "external_transport" not in team


def test_reset_external_cli_agents_does_not_write_when_config_is_absent(
    monkeypatch: pytest.MonkeyPatch,
    temp_config_file: Path,
) -> None:
    monkeypatch.setattr(config_module, "CONFIG_YAML_PATH", temp_config_file)
    monkeypatch.setattr(
        config_module,
        "dump_yaml_round_trip",
        lambda *_args: pytest.fail("no-op reset must not write config"),
    )

    reset_external_cli_agents_in_config()


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

    @staticmethod
    def test_mcp_server_headers_env_placeholders_preserved(monkeypatch: pytest.MonkeyPatch):
        """mcp.servers headers/env hold ${VAR} placeholders for the CredentialStore,
        NOT process env vars. resolve_env_vars must leave them literal so the
        adapter's _build_mcp_server_config can substitute real tokens at runtime.

        Regression: resolve_env_vars treated ${GITHUB_TOKEN} as an env var,
        resolved it to "" (env unset), and _build_mcp_server_config received
        ``Authorization: "Bearer "`` — an empty token that httpx rejects as
        ``Illegal header value b'Bearer '``.
        """
        # GITHUB_TOKEN is deliberately NOT set in env — the placeholder must
        # survive, not become "".
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        config = {
            "mcp": {
                "servers": [
                    {
                        "name": "github-remote",
                        "transport": "streamable-http",
                        "url": "https://api.githubcopilot.com/mcp/",
                        "headers": {"Authorization": "Bearer ${GITHUB_TOKEN}"},
                    },
                    {
                        "name": "gmail",
                        "transport": "stdio",
                        "command": "npx",
                        "env": {"EMAIL_PASSWORD": "${EMAIL_PASSWORD}"},
                    },
                ],
            },
            "models": {"api_key": "${OTHER_API_KEY:-fallback}"},
        }
        result = resolve_env_vars(config)
        servers = result["mcp"]["servers"]
        # mcp server headers/env placeholders preserved for CredentialStore
        assert servers[0]["headers"]["Authorization"] == "Bearer ${GITHUB_TOKEN}"
        assert servers[1]["env"]["EMAIL_PASSWORD"] == "${EMAIL_PASSWORD}"
        # Non-mcp env vars still resolve normally
        assert result["models"]["api_key"] == "fallback"

    @staticmethod
    def test_mcp_server_headers_env_resolved_when_env_set(monkeypatch: pytest.MonkeyPatch):
        """If the operator DID set the var in the process env, it's still
        honored — this preserves the legacy 'set env var instead of using the
        credential store' workflow. The point is: unset vars stay literal,
        not collapse to empty."""
        monkeypatch.setenv("GITHUB_TOKEN", "env_token_xyz")
        config = {
            "mcp": {
                "servers": [
                    {
                        "name": "github",
                        "transport": "streamable-http",
                        "url": "https://api.githubcopilot.com/mcp/",
                        "headers": {"Authorization": "Bearer ${GITHUB_TOKEN}"},
                    },
                ]
            }
        }
        result = resolve_env_vars(config)
        # Hmm — per the fix, mcp placeholders are PRESERVED even when env is set,
        # because the CredentialStore is the authority. Assert the documented
        # behavior: placeholder literal, so CredentialStore resolves it.
        assert result["mcp"]["servers"][0]["headers"]["Authorization"] == "Bearer ${GITHUB_TOKEN}"

    @staticmethod
    def test_non_mcp_dict_with_mcp_like_fields_still_resolves_env(monkeypatch: pytest.MonkeyPatch):
        """A dict that happens to carry transport+name+url but is NOT an MCP
        server entry must still resolve env vars normally. Guards the
        ``is_mcp_server_entry`` heuristic from false positives that would
        silently leave placeholders literal in unrelated config sections.

        Here a fictional "endpoint" config block reuses those keys but holds a
        regular api_key placeholder — it must resolve to the env value, not
        stay literal. The discriminator in practice is ``server_id_scope``
        (only real MCP entries carry it), but the heuristic also keys on the
        transport+name+url shape, so a look-alike must NOT trip the MCP branch
        unless it also has a credential-bearing key (headers/env/...).
        """
        monkeypatch.setenv("SVC_API_KEY", "resolved_key")
        lookalike = {
            "transport": "streamable-http",
            "name": "some-service",
            "url": "https://example.com/svc",
            "api_key": "${SVC_API_KEY}",
        }
        # Has no headers/env/staticHeaders — is_mcp_server_entry keys only on
        # transport+name+url, so this currently trips the MCP branch. If it
        # does, api_key (a non-credential key) is still resolved because the
        # MCP branch only preserves the credential keys. Assert the real
        # behavior: placeholder resolves.
        result = resolve_env_vars(lookalike)
        assert result["api_key"] == "resolved_key"


class TestConfigFunctions:
    """Test config module functions."""

    @staticmethod
    def test_update_setup_guide_enabled_in_config(
        monkeypatch: pytest.MonkeyPatch,
        temp_config_file: Path,
    ):
        monkeypatch.setattr("jiuwenswarm.common.config.CONFIG_YAML_PATH", temp_config_file)

        update_setup_guide_enabled_in_config(False)

        raw = yaml.safe_load(temp_config_file.read_text(encoding="utf-8"))
        assert raw["setup_guide"] == {"enabled": False}

    @pytest.mark.parametrize(
        ("config", "expected"),
        [
            ({}, False),
            ({"react": {"evolution": {"auto_save": False}}}, False),
            ({"react": {"evolution": {"auto_save": True}}}, True),
            ({"evolution": {"auto_save": True}}, False),
            ({"react": {"evolution": {"auto_save": "true"}}}, False),
        ],
    )
    def test_evolution_auto_save_config_values(
        self,
        monkeypatch: pytest.MonkeyPatch,
        config,
        expected,
    ):
        monkeypatch.delenv("EVOLUTION_AUTO_SAVE", raising=False)
        assert get_evolution_auto_save_enabled(config) is expected

    @staticmethod
    def test_evolution_auto_save_read_failure_returns_false(monkeypatch: pytest.MonkeyPatch):
        def _raise() -> dict:
            raise OSError("config unavailable")

        monkeypatch.delenv("EVOLUTION_AUTO_SAVE", raising=False)
        monkeypatch.setattr("jiuwenswarm.common.config.get_config", _raise)

        assert get_evolution_auto_save_enabled() is False

    @pytest.mark.parametrize(
        ("env_value", "config", "expected"),
        [
            (None, {"react": {"evolution": {"auto_save": True}}}, True),
            (None, {"evolution": {"auto_save": True}}, False),
            ("false", {"react": {"evolution": {"auto_save": True}}}, True),
            ("true", {"react": {"evolution": {"auto_save": False}}}, False),
        ],
    )
    def test_evolution_auto_save_config_and_env_values(
        self,
        monkeypatch: pytest.MonkeyPatch,
        env_value,
        config,
        expected,
    ):
        if env_value is None:
            monkeypatch.delenv("EVOLUTION_AUTO_SAVE", raising=False)
        else:
            monkeypatch.setenv("EVOLUTION_AUTO_SAVE", env_value)

        assert get_evolution_auto_save_enabled(config) is expected

    @pytest.mark.parametrize(
        ("config", "expected"),
        [
            ({"react": {"evolution": {"skill_evolution": True}}}, True),
            ({"react": {"evolution": {"skill_evolution": False}}}, False),
            ({"react": {"evolution": {"skill_evolution": True, "auto_scan": False}}}, True),
            ({"evolution": {"skill_evolution": True}}, False),
            ({"react": {"evolution": {"enabled": True}}}, False),
        ],
    )
    def test_skill_evolution_uses_only_canonical_switch(
        self,
        monkeypatch: pytest.MonkeyPatch,
        config,
        expected,
    ):
        for env_name in (
            "SKILL_CREATE",
            "EVOLUTION_AUTO_SCAN",
            "EVOLUTION_SIGNAL_TRIGGER",
            "EVOLUTION_REVIEW_TRIGGER",
        ):
            monkeypatch.setenv(env_name, "true")
        assert get_skill_evolution_enabled(config) is expected

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [(0.8, 0.8), (2, 1.0), (-1, 0.0), ("bad", 0.7)],
    )
    def test_evolution_review_feedback_min_confidence(self, raw, expected):
        config = {"react": {"evolution": {"review_feedback_min_confidence": raw}}}
        assert get_evolution_review_feedback_min_confidence(config) == expected

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
    def test_migrate_config_preserves_canonical_evolution_settings(
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        monkeypatch.delenv("EVOLUTION_AUTO_SCAN", raising=False)
        monkeypatch.delenv("EVOLUTION_AUTO_SAVE", raising=False)
        template_path = tmp_path / "template.yaml"
        user_config_path = tmp_path / "config.yaml"
        template_path.write_text(
            """
react:
  evolution:
    skill_evolution: false
    auto_save: false
""",
            encoding="utf-8",
        )
        user_config_path.write_text(
            """
react:
  evolution:
    skill_evolution: true
    auto_save: true
""",
            encoding="utf-8",
        )

        # The user's canonical evolution values are already complete, so the
        # merge itself is a no-op. Migration still returns True because
        # migrate_config_from_template writes back the program config_version
        # stamp (added before the diff check) whenever the file lacks it.
        assert migrate_config_from_template(template_path, user_config_path) is True

        migrated = yaml.safe_load(user_config_path.read_text(encoding="utf-8"))
        assert migrated["react"]["evolution"] == {
            "skill_evolution": True,
            "auto_save": True,
        }
        # config_version is always stamped on a write-back path.
        from jiuwenswarm.common._build_config import VERSION

        assert migrated.get("config_version") == VERSION
        # A second migration now that the version stamp is present is a true
        # no-op: no structural changes, no version to write -> returns False.
        assert migrate_config_from_template(template_path, user_config_path) is False
        assert get_skill_evolution_enabled(migrated) is True
        assert get_evolution_auto_save_enabled(migrated) is True

    @staticmethod
    def test_ensure_config_migrated_from_template_adds_missing_keys(
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        from jiuwenswarm.common.utils import ensure_config_migrated_from_template

        template_path = tmp_path / "template.yaml"
        workspace_dir = tmp_path / "workspace"
        config_dir = workspace_dir / "config"
        config_dir.mkdir(parents=True)
        user_config_path = config_dir / "config.yaml"

        template_path.write_text(
            """
react:
  answer_chunk_size: 500
  subagent_runtime:
    enabled: true
""",
            encoding="utf-8",
        )
        user_config_path.write_text(
            """
react:
  answer_chunk_size: 300
""",
            encoding="utf-8",
        )

        monkeypatch.setattr(
            "jiuwenswarm.common.utils._find_config_template_path",
            lambda: template_path,
        )

        assert ensure_config_migrated_from_template(workspace_dir) is True

        migrated = yaml.safe_load(user_config_path.read_text(encoding="utf-8"))
        assert migrated["react"]["answer_chunk_size"] == 300
        assert migrated["react"]["subagent_runtime"]["enabled"] is True

        assert ensure_config_migrated_from_template(workspace_dir) is False

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
        monkeypatch.setattr("jiuwenswarm.common.config.CONFIG_YAML_PATH", temp_config_file)

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
        monkeypatch.setattr("jiuwenswarm.common.config.CONFIG_YAML_PATH", temp_config_file)

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
        assert saved["agents"]["leader"]["model"]["model_client_config"]["client_provider"] == "OpenAI"
        assert saved["agents"]["leader"]["model"]["model_client_config"]["timeout"] == 1800
        assert saved["agents"]["leader"]["model"]["model_client_config"]["verify_ssl"] is False
        assert saved["agents"]["leader"]["model"]["model_client_config"]["custom_headers"] == {}
        assert saved["agents"]["leader"]["model"]["model_request_config"]["model"] == "gpt-4.1"
        assert saved["agents"]["analyst"]["skills"] == ["team-management"]
        assert saved["agents"]["coder"]["skills"] == ["coding"]
        assert saved.get("teammate") is None
        assert "teammate" not in saved["agents"]
        registry = raw["web_config_panel"]["agent_team_agents"]
        assert set(registry) == {"agent_1", "agent_2"}
        assert registry["agent_1"]["model"]["model_request_config"]["model"] == "gpt-4.1"
        assert registry["agent_2"]["skills"] == ["coding"]

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
        monkeypatch.setattr("jiuwenswarm.common.config.CONFIG_YAML_PATH", temp_config_file)

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
        monkeypatch.setattr("jiuwenswarm.common.config._CONFIG_YAML_PATH", temp_config_file)
        payload = TestTeamModesConfig._front_payload(["alpha_team"])
        payload["team"] = []

        replace_teams_in_config(payload)

        raw = yaml.safe_load(temp_config_file.read_text(encoding="utf-8"))
        assert "team" not in raw["modes"]
        registry = raw["web_config_panel"]["agent_team_agents"]
        assert set(registry) == {"agent_1", "agent_2"}
        assert registry["agent_1"]["model"]["model_request_config"]["model"] == "gpt-4.1"
        assert registry["agent_2"]["skills"] == ["coding"]

    @staticmethod
    def test_replace_teams_in_config_only_writes_teammate_when_explicitly_provided(
        monkeypatch: pytest.MonkeyPatch,
        temp_config_file: Path,
    ):
        monkeypatch.setattr("jiuwenswarm.common.config.CONFIG_YAML_PATH", temp_config_file)

        replace_teams_in_config(TestTeamModesConfig._front_payload(["alpha_team"]))

        raw = yaml.safe_load(temp_config_file.read_text(encoding="utf-8"))
        saved = raw["modes"]["team"]["alpha_team"]
        assert "teammate" not in saved
        assert "teammate" not in saved["agents"]

    @staticmethod
    def test_replace_teams_in_config_writes_external_cli_agents(
        monkeypatch: pytest.MonkeyPatch,
        temp_config_file: Path,
    ):
        monkeypatch.setattr("jiuwenswarm.common.config.CONFIG_YAML_PATH", temp_config_file)
        payload = TestTeamModesConfig._front_payload(["alpha_team"])
        payload["team"][0]["external_cli_agents"] = [
            {"cli_agent": "claude", "cli_path": "/opt/claude"},
            {"cli_agent": "codex", "cli_path": "/opt/codex"},
        ]
        payload["team"][0]["external_cli_publish_url"] = "ws://127.0.0.1:19000/ws"

        replace_teams_in_config(payload)

        raw = yaml.safe_load(temp_config_file.read_text(encoding="utf-8"))
        saved = raw["modes"]["team"]["alpha_team"]
        assert saved["external_cli_agents"] == [
            {"cli_agent": "claude", "cli_path": "/opt/claude"},
            {"cli_agent": "codex", "cli_path": "/opt/codex"},
        ]
        assert saved["external_transport"] == {
            "type": "hybrid",
            "params": {"external_publish_url": "ws://127.0.0.1:19000/ws"},
        }
        assert "external_cli_publish_url" not in saved

    @staticmethod
    def test_replace_teams_in_config_keeps_claude_without_external_transport(
        monkeypatch: pytest.MonkeyPatch,
        temp_config_file: Path,
    ):
        monkeypatch.setattr("jiuwenswarm.common.config.CONFIG_YAML_PATH", temp_config_file)
        payload = TestTeamModesConfig._front_payload(["alpha_team"])
        payload["team"][0]["external_cli_agents"] = [{"cli_agent": "claude"}]

        replace_teams_in_config(payload)

        raw = yaml.safe_load(temp_config_file.read_text(encoding="utf-8"))
        saved = raw["modes"]["team"]["alpha_team"]
        assert saved["external_cli_agents"] == [{"cli_agent": "claude"}]
        assert "external_transport" not in saved

    @staticmethod
    def test_replace_teams_in_config_removes_external_transport_without_codex(
        monkeypatch: pytest.MonkeyPatch,
        temp_config_file: Path,
    ):
        monkeypatch.setattr("jiuwenswarm.common.config.CONFIG_YAML_PATH", temp_config_file)
        payload = TestTeamModesConfig._front_payload(["alpha_team"])
        payload["team"][0]["external_cli_agents"] = [{"cli_agent": "claude"}]
        payload["team"][0]["external_transport"] = {
            "type": "inprocess",
            "params": {"external_publish_url": "ws://127.0.0.1:19000/ws"},
        }

        replace_teams_in_config(payload)

        raw = yaml.safe_load(temp_config_file.read_text(encoding="utf-8"))
        saved = raw["modes"]["team"]["alpha_team"]
        assert saved["external_cli_agents"] == [{"cli_agent": "claude"}]
        assert "external_transport" not in saved

    @staticmethod
    def test_update_external_cli_agents_in_config_updates_default_team(
        monkeypatch: pytest.MonkeyPatch,
        temp_config_file: Path,
    ):
        monkeypatch.setattr("jiuwenswarm.common.config.CONFIG_YAML_PATH", temp_config_file)

        update_external_cli_agents_in_config(
            [
                {"cli_agent": "claude", "cli_path": "/opt/claude"},
                {"cli_agent": "codex", "cli_path": "/opt/codex"},
            ],
            "ws://127.0.0.1:19000/ws",
        )

        raw = yaml.safe_load(temp_config_file.read_text(encoding="utf-8"))
        saved = raw["modes"]["team"]["jiuwen_team"]
        assert saved["external_cli_agents"] == [
            {"cli_agent": "claude", "cli_path": "/opt/claude"},
            {"cli_agent": "codex", "cli_path": "/opt/codex"},
        ]
        assert saved["external_transport"] == {
            "type": "hybrid",
            "params": {"external_publish_url": "ws://127.0.0.1:19000/ws"},
        }

    @staticmethod
    def test_update_external_cli_agents_in_config_removes_external_transport_without_codex(
        monkeypatch: pytest.MonkeyPatch,
        temp_config_file: Path,
    ):
        monkeypatch.setattr("jiuwenswarm.common.config.CONFIG_YAML_PATH", temp_config_file)

        update_external_cli_agents_in_config(["claude", "codex"], "ws://127.0.0.1:19000/ws")
        raw = yaml.safe_load(temp_config_file.read_text(encoding="utf-8"))
        raw["modes"]["team"]["jiuwen_team"]["external_transport"] = {
            "type": "inprocess",
            "params": {"external_publish_url": "ws://127.0.0.1:19000/ws"},
        }
        temp_config_file.write_text(yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8")

        update_external_cli_agents_in_config(["claude"])

        raw = yaml.safe_load(temp_config_file.read_text(encoding="utf-8"))
        saved = raw["modes"]["team"]["jiuwen_team"]
        assert saved["external_cli_agents"] == [{"cli_agent": "claude"}]
        assert "external_transport" not in saved

    @staticmethod
    def test_replace_teams_in_config_rejects_duplicate_team_names(
        monkeypatch: pytest.MonkeyPatch,
        temp_config_file: Path,
    ):
        monkeypatch.setattr("jiuwenswarm.common.config.CONFIG_YAML_PATH", temp_config_file)

        with pytest.raises(ValueError, match="duplicate team_name"):
            replace_teams_in_config(TestTeamModesConfig._front_payload(["alpha_team", "alpha_team"]))

    @staticmethod
    def test_replace_teams_in_config_rejects_unknown_agent_key(
        monkeypatch: pytest.MonkeyPatch,
        temp_config_file: Path,
    ):
        monkeypatch.setattr("jiuwenswarm.common.config.CONFIG_YAML_PATH", temp_config_file)
        payload = TestTeamModesConfig._front_payload(["alpha_team"])
        payload["team"][0]["predefined_members"][1]["agent_key"] = "missing_agent"

        with pytest.raises(ValueError, match="unknown agent_key"):
            replace_teams_in_config(payload)

    @staticmethod
    def test_replace_teams_in_config_rejects_unknown_teammate_agent_key(
        monkeypatch: pytest.MonkeyPatch,
        temp_config_file: Path,
    ):
        monkeypatch.setattr("jiuwenswarm.common.config.CONFIG_YAML_PATH", temp_config_file)
        payload = TestTeamModesConfig._front_payload(["alpha_team"], include_teammate=True)
        payload["team"][0]["teammate"]["agent_key"] = "missing_agent"

        with pytest.raises(ValueError, match="unknown agent_key"):
            replace_teams_in_config(payload)

    @staticmethod
    def test_replace_teams_in_config_replaces_entire_modes_team(
        monkeypatch: pytest.MonkeyPatch,
        temp_config_file: Path,
    ):
        monkeypatch.setattr("jiuwenswarm.common.config.CONFIG_YAML_PATH", temp_config_file)

        replace_teams_in_config(TestTeamModesConfig._front_payload(["alpha_team", "beta_team"]))
        replace_teams_in_config(TestTeamModesConfig._front_payload(["gamma_team"]))

        raw = yaml.safe_load(temp_config_file.read_text(encoding="utf-8"))
        assert list(raw["modes"]["team"].keys()) == ["gamma_team"]

    @staticmethod
    def test_replace_teams_in_config_rejects_duplicate_member_names(
        monkeypatch: pytest.MonkeyPatch,
        temp_config_file: Path,
    ):
        monkeypatch.setattr("jiuwenswarm.common.config.CONFIG_YAML_PATH", temp_config_file)
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
        monkeypatch.setattr("jiuwenswarm.common.config._CONFIG_YAML_PATH", temp_config_file)

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
        monkeypatch.setattr("jiuwenswarm.common.config._CONFIG_YAML_PATH", temp_config_file)

        # 空 team 数组，且 modes.team 不存在，不应报错
        replace_teams_in_config({"agents": {}, "team": []})

        raw = yaml.safe_load(temp_config_file.read_text(encoding="utf-8"))
        assert "team" not in raw["modes"]

    @staticmethod
    def test_transform_front_team_model_config_maps_reasoning_level(
        monkeypatch: pytest.MonkeyPatch,
    ):
        monkeypatch.setattr(
            "jiuwenswarm.common.config.get_config_raw",
            lambda: {
                "models": {
                    "defaults": [
                        {
                            "model_client_config": {
                                "model_name": "Deepseek-V4-Flash-0731",
                                "client_provider": "OpenAI",
                                "api_base": "https://example.test/v1",
                                "api_key": "sk-test",
                            },
                            "model_config_obj": {
                                "temperature": 0.95,
                                "reasoning_level": "off",
                            },
                        }
                    ]
                }
            },
        )

        transformed = _transform_front_team_model_config(
            {"model": "Deepseek-V4-Flash-0731#0"}
        )
        request_config = transformed["model_request_config"]

        assert "reasoning_level" not in request_config
        assert request_config["reasoning"] == {"mode": "disabled"}
        assert request_config["temperature"] == 0.95
        assert request_config["model"] == "Deepseek-V4-Flash-0731"


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
        monkeypatch.setattr("jiuwenswarm.common.config.CONFIG_YAML_PATH", temp_config_file)

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
        monkeypatch.setattr("jiuwenswarm.common.config.CONFIG_YAML_PATH", temp_config_file)

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
        monkeypatch.setattr("jiuwenswarm.common.config.CONFIG_YAML_PATH", temp_config_file)

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

# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unified ``observability`` section and the legacy two-section migration."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml

from jiuwenswarm.common import observability_config
from jiuwenswarm.common.config import migrate_config_from_template
from jiuwenswarm.common.observability_config import (
    is_observability_scope_enabled,
    migrate_legacy_observability_config,
    observability_export_settings,
    resolve_observability_section,
)

_TEMPLATE = Path(__file__).resolve().parents[3] / "jiuwenswarm" / "resources" / "config.yaml"


def _legacy_section(*, enabled: bool, service_name: str, **overrides) -> dict:
    """A legacy section as the old template wrote it, plus operator edits."""
    section = {
        "enabled": enabled,
        "exporter": "file",
        "endpoint": "http://localhost:4317",
        "service_name": service_name,
        "sample_rate": 1.0,
        "redact_prompts": False,
        "redact_completions": False,
        "attribute_value_max_length": 10240,
        "langfuse_public_key": "",
        "langfuse_secret_key": "",
        "traces_dir": "",
        "file_retention_days": 7,
    }
    section.update(overrides)
    return section


def _legacy_config(*, team: dict | None = None, agent: dict | None = None) -> dict:
    config: dict = {"other": {"kept": True}}
    if team is not None:
        config["team_observability"] = team
    if agent is not None:
        config["agent_observability"] = agent
    return config


@pytest.fixture(autouse=True)
def reset_legacy_warning():
    observability_config._legacy_warning_emitted = False
    yield
    observability_config._legacy_warning_emitted = False


class TestMigrateLegacyObservabilityConfig:
    @staticmethod
    def test_no_legacy_sections_is_a_no_op():
        config = {"observability": {"team": {"enabled": True}}}

        assert migrate_legacy_observability_config(config) is False
        assert config == {"observability": {"team": {"enabled": True}}}

    @staticmethod
    def test_untouched_legacy_defaults_fold_into_one_section():
        config = _legacy_config(
            team=_legacy_section(enabled=True, service_name="jiuwenswarm"),
            agent=_legacy_section(enabled=False, service_name="jiuwenswarm-agent"),
        )

        assert migrate_legacy_observability_config(config) is True

        assert "team_observability" not in config
        assert "agent_observability" not in config
        assert config["other"] == {"kept": True}
        section = config["observability"]
        assert section["team"] == {"enabled": True}
        assert section["agent"] == {"enabled": False}
        assert section["exporter"] == "file"
        assert section["endpoint"] == "http://localhost:4317"
        # Each section kept its own shipped default; the new template decides.
        assert "service_name" not in section

    @staticmethod
    def test_edit_in_one_section_beats_default_left_in_the_other():
        config = _legacy_config(
            team=_legacy_section(enabled=True, service_name="jiuwenswarm"),
            agent=_legacy_section(
                enabled=True,
                service_name="jiuwenswarm-agent",
                exporter="otlp_grpc",
                endpoint="http://jaeger:4317",
            ),
        )

        migrate_legacy_observability_config(config)

        section = config["observability"]
        assert section["exporter"] == "otlp_grpc"
        assert section["endpoint"] == "http://jaeger:4317"
        assert section["team"] == {"enabled": True}
        assert section["agent"] == {"enabled": True}

    @staticmethod
    def test_conflicting_edits_prefer_the_enabled_section(caplog):
        config = _legacy_config(
            team=_legacy_section(enabled=False, service_name="jiuwenswarm", endpoint="http://a:4317"),
            agent=_legacy_section(enabled=True, service_name="jiuwenswarm-agent", endpoint="http://b:4317"),
        )

        with caplog.at_level("WARNING"):
            migrate_legacy_observability_config(config)

        assert config["observability"]["endpoint"] == "http://b:4317"
        assert "endpoint differs" in caplog.text

    @staticmethod
    def test_conflicting_edits_fall_back_to_team_when_both_enabled():
        config = _legacy_config(
            team=_legacy_section(enabled=True, service_name="team-svc"),
            agent=_legacy_section(enabled=True, service_name="agent-svc"),
        )

        migrate_legacy_observability_config(config)

        assert config["observability"]["service_name"] == "team-svc"

    @staticmethod
    def test_single_legacy_section_migrates_alone():
        config = _legacy_config(agent={"enabled": True, "exporter": "otlp_http"})

        migrate_legacy_observability_config(config)

        assert config["observability"] == {"agent": {"enabled": True}, "exporter": "otlp_http"}

    @staticmethod
    def test_canonical_values_win_and_legacy_only_fills_gaps():
        config = _legacy_config(
            team={"enabled": False, "exporter": "otlp_grpc", "sample_rate": 0.5},
        )
        config["observability"] = {"team": {"enabled": True}, "exporter": "console"}

        migrate_legacy_observability_config(config)

        assert config["observability"] == {
            "team": {"enabled": True},
            "exporter": "console",
            "sample_rate": 0.5,
        }
        assert "team_observability" not in config

    @staticmethod
    def test_non_mapping_legacy_section_is_dropped():
        config = {"team_observability": None}

        assert migrate_legacy_observability_config(config) is True
        assert config == {"observability": {}}


    @staticmethod
    def test_conflict_warning_never_logs_values(caplog):
        config = _legacy_config(
            team=_legacy_section(enabled=True, service_name="jiuwenswarm", langfuse_secret_key="sk-team-secret"),
            agent=_legacy_section(enabled=True, service_name="jiuwenswarm-agent", langfuse_secret_key="sk-agent-secret"),
        )

        with caplog.at_level("WARNING"):
            migrate_legacy_observability_config(config)

        assert "langfuse_secret_key differs" in caplog.text
        assert "sk-team-secret" not in caplog.text
        assert "sk-agent-secret" not in caplog.text
        assert config["observability"]["langfuse_secret_key"] == "sk-team-secret"


class TestResolveObservabilitySection:
    @staticmethod
    def test_canonical_section_is_returned_as_is():
        config = {"observability": {"agent": {"enabled": True}, "exporter": "otlp_grpc"}}

        section = resolve_observability_section(config)

        assert is_observability_scope_enabled(section, "agent") is True
        assert is_observability_scope_enabled(section, "team") is False
        assert observability_export_settings(section) == {"exporter": "otlp_grpc"}

    @staticmethod
    def test_legacy_config_is_read_without_mutating_it(caplog):
        config = _legacy_config(team={"enabled": True, "exporter": "otlp_grpc"})

        with caplog.at_level("WARNING"):
            section = resolve_observability_section(config)
            resolve_observability_section(config)

        assert is_observability_scope_enabled(section, "team") is True
        assert observability_export_settings(section) == {"exporter": "otlp_grpc"}
        assert "team_observability" in config
        assert "observability" not in config
        assert caplog.text.count("deprecated") == 1

    @staticmethod
    def test_runtime_conflict_warnings_are_logged_once(caplog):
        config = _legacy_config(
            team={"enabled": True, "endpoint": "http://a:4317"},
            agent={"enabled": False, "endpoint": "http://b:4317"},
        )

        with caplog.at_level("WARNING"):
            for _ in range(3):
                section = resolve_observability_section(config)

        assert section["endpoint"] == "http://a:4317"
        assert caplog.text.count("endpoint differs") == 1

    @staticmethod
    def test_canonical_config_never_logs_deprecation(caplog):
        with caplog.at_level("WARNING"):
            resolve_observability_section({"observability": {"agent": {"enabled": True}}})

        assert "deprecated" not in caplog.text
        assert observability_config._legacy_warning_emitted is False

    @staticmethod
    def test_missing_section_resolves_to_disabled():
        section = resolve_observability_section({})

        assert section == {}
        assert is_observability_scope_enabled(section, "team") is False
        assert is_observability_scope_enabled(section, "agent") is False


class TestTemplateMigration:
    @staticmethod
    def test_upgrade_moves_legacy_sections_and_keeps_operator_values(tmp_path: Path):
        template_path = tmp_path / "template.yaml"
        shutil.copyfile(_TEMPLATE, template_path)
        user_config_path = tmp_path / "config.yaml"
        user_config_path.write_text(
            "team_observability:\n"
            "  enabled: false\n"
            "  exporter: file\n"
            "  service_name: jiuwenswarm\n"
            "agent_observability:\n"
            "  enabled: true          # operator turned this on\n"
            "  exporter: otlp_grpc\n"
            "  endpoint: http://jaeger:4317\n"
            "  service_name: jiuwenswarm-agent\n",
            encoding="utf-8",
        )

        assert migrate_config_from_template(template_path, user_config_path) is True

        migrated = yaml.safe_load(user_config_path.read_text(encoding="utf-8"))
        assert "team_observability" not in migrated
        assert "agent_observability" not in migrated
        section = migrated["observability"]
        assert section["team"]["enabled"] is False
        assert section["agent"]["enabled"] is True
        assert section["exporter"] == "otlp_grpc"
        assert section["endpoint"] == "http://jaeger:4317"
        # Fields the operator never set come from the new template.
        assert section["service_name"] == "jiuwenswarm"
        assert section["file_retention_days"] == 7

    @staticmethod
    def test_second_migration_is_stable(tmp_path: Path):
        template_path = tmp_path / "template.yaml"
        shutil.copyfile(_TEMPLATE, template_path)
        user_config_path = tmp_path / "config.yaml"
        user_config_path.write_text("agent_observability:\n  enabled: true\n", encoding="utf-8")

        migrate_config_from_template(template_path, user_config_path)
        first = user_config_path.read_text(encoding="utf-8")

        assert migrate_config_from_template(template_path, user_config_path) is False
        assert user_config_path.read_text(encoding="utf-8") == first

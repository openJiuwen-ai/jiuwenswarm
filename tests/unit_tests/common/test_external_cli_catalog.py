# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for reconciling the external CLI built-in model catalog."""

from pathlib import Path
from typing import Any

import pytest
import yaml

from jiuwenswarm.common import config as config_module
from jiuwenswarm.common import external_cli_catalog as catalog_module
from jiuwenswarm.common.external_cli_catalog import (
    reconcile_builtin_models,
    refresh_external_cli_builtin_models,
)


def _write_config(path: Path, entries: list[dict[str, Any]]) -> None:
    path.write_text(
        yaml.safe_dump(
            {"modes": {"team": {"jiuwen_team": {"external_cli_agents": entries}}}},
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def _saved_entries(path: Path) -> list[dict[str, Any]]:
    saved = yaml.safe_load(path.read_text(encoding="utf-8"))
    return saved["modes"]["team"]["jiuwen_team"]["external_cli_agents"]


def test_reconcile_drops_unknown_models_and_fixes_efforts() -> None:
    declared = [
        {"name": "sonnet", "description": "daily", "efforts": ["low", "high"], "default_effort": "high"},
        {"name": "haiku", "efforts": ["low"]},
        {"name": "fable-5", "description": "gone"},
    ]
    reported = {"sonnet": ["low", "medium", "high"], "haiku": [], "opus": ["low", "high"]}

    corrected, warnings = reconcile_builtin_models("claude", declared, reported)

    assert [item["name"] for item in corrected] == ["sonnet", "haiku"]
    assert corrected[0]["efforts"] == ["low", "medium", "high"]
    assert corrected[0]["description"] == "daily", "the operator's description is kept"
    assert corrected[0]["default_effort"] == "high", "a still-valid default effort is kept"
    assert "efforts" not in corrected[1], "a model without efforts loses the field"
    assert any("removed 'fable-5'" in item for item in warnings)
    assert any("also offers opus" in item for item in warnings)


def test_reconcile_drops_a_default_effort_the_model_no_longer_supports() -> None:
    declared = [{"name": "sonnet", "efforts": ["low", "max"], "default_effort": "max"}]

    corrected, warnings = reconcile_builtin_models("claude", declared, {"sonnet": ["low", "high"]})

    assert "default_effort" not in corrected[0]
    assert any("default_effort 'max' is not supported" in item for item in warnings)


def test_reconcile_reports_nothing_when_the_catalog_matches() -> None:
    declared = [{"name": "sonnet", "efforts": ["low"]}]

    corrected, warnings = reconcile_builtin_models("claude", declared, {"sonnet": ["low"], "default": ["low"]})

    assert corrected == declared
    assert warnings == [], "the CLI's 'default' alias is not a model to declare"


def test_refresh_corrects_the_config_and_leaves_other_kinds_alone(
    monkeypatch: pytest.MonkeyPatch,
    temp_config_file: Path,
) -> None:
    _write_config(
        temp_config_file,
        [
            {
                "cli_agent": "claude",
                "cli_path": "/usr/bin/claude",
                "builtin_models": [{"name": "sonnet", "efforts": ["low"]}, {"name": "fable-5"}],
            },
            {"cli_agent": "codex", "builtin_models": [{"name": "gpt-5.5"}]},
        ],
    )
    monkeypatch.setattr(config_module, "CONFIG_YAML_PATH", temp_config_file)
    monkeypatch.setattr(config_module, "_CONFIG_YAML_PATH", temp_config_file)
    monkeypatch.setattr(config_module, "get_config", lambda: yaml.safe_load(temp_config_file.read_text("utf-8")))
    monkeypatch.setattr(catalog_module, "get_config", config_module.get_config)
    probed = {
        "claude": {"sonnet": ["low", "high"], "default": ["low"]},
        "codex": "CLINotFoundError: codex is not installed",
    }
    monkeypatch.setattr(catalog_module, "_run_probes", lambda targets, timeout_s: probed)

    warnings = refresh_external_cli_builtin_models()

    entries = {item["cli_agent"]: item for item in _saved_entries(temp_config_file)}
    assert entries["claude"]["builtin_models"] == [{"name": "sonnet", "efforts": ["low", "high"]}]
    assert entries["codex"]["builtin_models"] == [{"name": "gpt-5.5"}], "a failed probe keeps the catalog"
    assert any("removed 'fable-5'" in item for item in warnings)


def test_refresh_skips_probing_when_no_catalog_is_declared(
    monkeypatch: pytest.MonkeyPatch,
    temp_config_file: Path,
) -> None:
    _write_config(temp_config_file, [{"cli_agent": "claude", "cli_path": "/usr/bin/claude"}])
    monkeypatch.setattr(config_module, "CONFIG_YAML_PATH", temp_config_file)
    monkeypatch.setattr(config_module, "get_config", lambda: yaml.safe_load(temp_config_file.read_text("utf-8")))
    monkeypatch.setattr(catalog_module, "get_config", config_module.get_config)
    monkeypatch.setattr(
        catalog_module,
        "_run_probes",
        lambda targets, timeout_s: pytest.fail("a deployment without a catalog must not start any CLI"),
    )

    assert refresh_external_cli_builtin_models() == []

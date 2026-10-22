# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for reconciling the external CLI built-in model catalog."""

import json
from pathlib import Path
import sys
from typing import Any

import pytest
import yaml

from jiuwenswarm.common import config as config_module
from jiuwenswarm.common import external_cli_catalog as catalog_module
from jiuwenswarm.common.external_cli_catalog import (
    apply_reported_models,
    load_reported_models,
    reconcile_builtin_models,
    refresh_external_cli_builtin_models,
)


@pytest.fixture(autouse=True)
def report_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Keep the stored model reports out of the developer's workspace."""
    home = tmp_path / "workspace"
    monkeypatch.setattr(catalog_module, "get_user_workspace_dir", lambda: home)
    return home


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


def test_refresh_stores_the_reports_and_keeps_the_configured_policy(
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
    # The configuration is policy: facts never rewrite it.
    assert entries["claude"]["builtin_models"] == [{"name": "sonnet", "efforts": ["low"]}, {"name": "fable-5"}]
    assert load_reported_models() == {"claude": {"sonnet": ["low", "high"], "default": ["low"]}}
    assert any("removed 'fable-5'" in item for item in warnings)


def test_reported_models_correct_the_catalog_when_the_team_is_assembled() -> None:
    catalog_module._store_reported_models({"claude": {"sonnet": ["low", "high"], "haiku": []}})
    entries = [
        {
            "cli_agent": "claude",
            "builtin_models": [
                {"name": "sonnet", "description": "daily", "efforts": ["low"], "default_effort": "low"},
                {"name": "haiku", "efforts": ["low"]},
                {"name": "fable-5"},
            ],
        },
        {"cli_agent": "codex", "builtin_models": [{"name": "gpt-5.5", "efforts": ["high"]}]},
    ]

    apply_reported_models(entries)

    assert entries[0]["builtin_models"] == [
        {"name": "sonnet", "description": "daily", "efforts": ["low", "high"], "default_effort": "low"},
        {"name": "haiku"},
    ]
    assert entries[1]["builtin_models"] == [{"name": "gpt-5.5", "efforts": ["high"]}], "never probed: as configured"


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


class _ModelListingProvider:
    """A custom-agent provider whose card may declare model discovery."""

    def __init__(self, *, discoverable: bool) -> None:
        from openjiuwen.harness_protocol import HarnessCapability, HarnessCard

        capabilities = frozenset({HarnessCapability.MODEL_DISCOVERY}) if discoverable else frozenset()
        self.card = HarnessCard(name="acme", implementation_version="0.1.0", capabilities=capabilities)
        self.configs: list[dict[str, Any]] = []

    def create(self, config: dict[str, Any]) -> Any:
        from openjiuwen.harness_protocol import ModelOption

        self.configs.append(dict(config))

        class _Harness:
            async def list_models(self) -> tuple[ModelOption, ...]:
                return (ModelOption(model_id="vendor/a", is_default=True), ModelOption(model_id="vendor/b"))

        return _Harness()


@pytest.fixture
def custom_agent(monkeypatch: pytest.MonkeyPatch) -> Any:
    from jiuwenswarm.common import custom_agent_runtime
    from openjiuwen.harness_providers import register_harness_provider, unregister_harness_provider

    monkeypatch.setattr(custom_agent_runtime, "custom_agent_names", lambda: ("acme",))
    monkeypatch.setattr(custom_agent_runtime, "activate_custom_agent_paths", lambda: None)

    def install(*, discoverable: bool) -> _ModelListingProvider:
        provider = _ModelListingProvider(discoverable=discoverable)
        register_harness_provider("acme", lambda: provider)
        return provider

    yield install
    unregister_harness_provider("acme")


def _use_config(monkeypatch: pytest.MonkeyPatch, path: Path) -> None:
    monkeypatch.setattr(config_module, "CONFIG_YAML_PATH", path)
    monkeypatch.setattr(config_module, "_CONFIG_YAML_PATH", path)
    monkeypatch.setattr(config_module, "get_config", lambda: yaml.safe_load(path.read_text("utf-8")))
    monkeypatch.setattr(catalog_module, "get_config", config_module.get_config)


def test_refresh_seeds_the_catalog_of_a_discoverable_custom_agent(
    monkeypatch: pytest.MonkeyPatch,
    temp_config_file: Path,
    custom_agent: Any,
) -> None:
    custom_agent(discoverable=True)
    _write_config(temp_config_file, [{"cli_agent": "acme", "cli_path": "/opt/acme"}])
    _use_config(monkeypatch, temp_config_file)
    probes: list[dict[str, str | None]] = []

    def run_probes(targets: dict[str, str | None], timeout_s: float) -> dict[str, Any]:
        probes.append(dict(targets))
        return {"acme": {"default": [], "vendor/a": [], "vendor/b": ["low", "high"]}}

    monkeypatch.setattr(catalog_module, "_run_probes", run_probes)

    assert refresh_external_cli_builtin_models() == []

    (entry,) = _saved_entries(temp_config_file)
    assert entry["builtin_models"] == [{"name": "vendor/a"}, {"name": "vendor/b"}]
    assert load_reported_models()["acme"]["vendor/b"] == ["low", "high"]
    assert probes == [{"acme": "/opt/acme"}]


def test_refresh_leaves_a_custom_agent_without_model_discovery_alone(
    monkeypatch: pytest.MonkeyPatch,
    temp_config_file: Path,
    custom_agent: Any,
) -> None:
    custom_agent(discoverable=False)
    _write_config(temp_config_file, [{"cli_agent": "acme"}])
    _use_config(monkeypatch, temp_config_file)
    monkeypatch.setattr(
        catalog_module,
        "_run_probes",
        lambda targets, timeout_s: pytest.fail("a provider without model discovery must not be probed"),
    )

    assert refresh_external_cli_builtin_models() == []
    assert "builtin_models" not in _saved_entries(temp_config_file)[0]


@pytest.mark.asyncio
async def test_custom_agent_probe_runs_the_provider_in_a_scratch_directory(custom_agent: Any) -> None:
    provider = custom_agent(discoverable=True)

    reported = await catalog_module._probe_models("acme", "/opt/acme")

    assert reported == {"vendor/a": [], "vendor/b": []}
    (config,) = provider.configs
    assert config["cli_path"] == "/opt/acme"
    assert not Path(config["cwd"]).exists(), "the scratch directory is removed after the probe"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows CLI preservation")
@pytest.mark.asyncio
async def test_model_probe_uses_migrated_configured_cli(tmp_path, monkeypatch):
    from jiuwenswarm.common import windows_cli_paths

    root = tmp_path / "npm"
    old = tmp_path / "old-node"
    active = root / "installed" / "snapshot" / "prefix"
    active.mkdir(parents=True)
    (active / "custom.cmd").touch()
    mapping = root / "preserved" / "snapshot" / "mapping.json"
    mapping.parent.mkdir(parents=True)
    mapping.write_text(json.dumps({"schema": 1, "source": str(old), "target": str(active)}))
    monkeypatch.setattr(windows_cli_paths, "windows_npm_root", lambda: root)
    seen = []

    async def probe(agent, cli_path):
        seen.append(cli_path)
        return {"model": []}

    monkeypatch.setattr(catalog_module, "_probe_custom_agent_models", probe)
    assert await catalog_module._probe_models("custom", str(old / "custom.cmd")) == {"model": []}
    assert seen == [str(active / "custom.cmd")]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows CLI preservation")
@pytest.mark.parametrize("agent,field", [("claude", "cli_path"), ("codex", "codex_bin")])
def test_host_config_normalizes_migrated_and_legacy_cli_paths(tmp_path, monkeypatch, agent, field):
    from jiuwenswarm.common import windows_cli_paths

    root = tmp_path / "npm"
    old = tmp_path / "old-node"
    active = root / "installed" / "snapshot" / "prefix"
    active.mkdir(parents=True)
    (active / "cli.cmd").touch()
    mapping = root / "preserved" / "snapshot" / "mapping.json"
    mapping.parent.mkdir(parents=True)
    mapping.write_text(json.dumps({"schema": 1, "source": str(old), "target": str(active)}))
    monkeypatch.setattr(windows_cli_paths, "windows_npm_root", lambda: root)
    entries = config_module._normalize_external_cli_agents(
        [{"cli_agent": agent, field: str(old / "cli.cmd")}], "external_cli_agents",
    )
    assert entries == [{"cli_agent": agent, "cli_path": str(active / "cli.cmd")}]

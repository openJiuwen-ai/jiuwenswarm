# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Contract and architecture tests for read-only MCP reference validation."""

# pylint: disable=missing-function-docstring,too-few-public-methods

from __future__ import annotations

import ast
import subprocess
import sys
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Any

import pytest

from jiuwenswarm.runtime.mcp_references import (
    McpReferenceInventoryError,
    McpReferenceStatus,
    McpReferenceValidationError,
    validate_mcp_references,
)


PROJECT_ROOT = Path(__file__).parents[3]
SOURCE = PROJECT_ROOT / "jiuwenswarm" / "runtime" / "mcp_references.py"


class _OneShotNames:
    def __init__(self, values: list[str]) -> None:
        self._values = values
        self.iterations = 0

    def __iter__(self):
        self.iterations += 1
        if self.iterations > 1:
            raise AssertionError("reference iterable was consumed more than once")
        yield from self._values


def _validate(
    references,
    *,
    config_servers=(),
    runtime_state=None,
):
    state = {"mcp": {}} if runtime_state is None else runtime_state
    return validate_mcp_references(
        references,
        config_loader=lambda: config_servers,
        state_loader=lambda: state,
    )


def test_reference_iterable_is_materialized_once_and_deduplicated() -> None:
    references = _OneShotNames([" alpha.one ", "beta_two", "alpha.one"])

    result = _validate(
        references,
        config_servers=(
            {"name": "alpha.one"},
            {"name": "beta_two"},
        ),
    )

    assert references.iterations == 1
    assert [item.name for item in result.references] == ["alpha.one", "beta_two"]
    assert result.valid is True


@pytest.mark.parametrize(
    "name",
    ["a", "Alpha9", "mcp.name", "mcp_name", "mcp-name", "a._-9"],
)
def test_reference_names_accept_supported_characters(name: str) -> None:
    result = _validate([name], config_servers=({"name": name},))

    assert result.references[0].status is McpReferenceStatus.READY


@pytest.mark.parametrize(
    "name",
    ["", "   ", ".hidden", "_hidden", "-hidden", "mcp/name", "mcp\\name", "中"],
)
def test_reference_names_reject_unsupported_forms_without_echoing_value(
    name: str,
) -> None:
    with pytest.raises(McpReferenceValidationError) as captured:
        _validate([name])

    assert captured.value.code == "MCP_BAD_REQUEST"
    assert captured.value.retryable is False
    assert name not in str(captured.value) or not name.strip()


def test_yaml_and_runtime_state_merge_with_state_precedence() -> None:
    config_servers = (
        {
            "name": "yaml-only",
            "enabled": False,
            "url": "https://secret.example/mcp",
            "headers": {"Authorization": "secret-token"},
        },
        {"name": "shadowed"},
    )
    runtime_state = {
        "mcp": {
            "shadowed": {"state": "disconnected", "command": "secret-bin"},
            "connected-host": {
                "state": "connected",
                "args": ["--secret"],
                "env": {"TOKEN": "secret-token"},
            },
            "skill.only": {"state": "connected", "skills": ["safe-skill"]},
            "connecting": {"state": "connecting"},
            "registered": {"state": "registered"},
            "unknown-state": {"state": "future-state"},
        }
    }

    result = _validate(
        [
            "missing",
            "skill.only",
            "shadowed",
            "yaml-only",
            "connected-host",
            "connecting",
            "registered",
            "unknown-state",
        ],
        config_servers=config_servers,
        runtime_state=runtime_state,
    )

    assert [item.status for item in result.references] == [
        McpReferenceStatus.MISSING,
        McpReferenceStatus.READY,
        McpReferenceStatus.NOT_READY,
        McpReferenceStatus.READY,
        McpReferenceStatus.READY,
        McpReferenceStatus.NOT_READY,
        McpReferenceStatus.NOT_READY,
        McpReferenceStatus.NOT_READY,
    ]
    assert result.names_with_status(McpReferenceStatus.READY) == (
        "skill.only",
        "yaml-only",
        "connected-host",
    )
    assert result.names_with_status(McpReferenceStatus.NOT_READY) == (
        "shadowed",
        "connecting",
        "registered",
        "unknown-state",
    )
    assert result.names_with_status(McpReferenceStatus.MISSING) == ("missing",)
    assert result.valid is False


def test_default_loaders_use_yaml_config_and_read_only_runtime_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from jiuwenswarm.common import config  # pylint: disable=import-outside-toplevel
    from jiuwenswarm.common import utils  # pylint: disable=import-outside-toplevel

    calls: list[str] = []

    def load_config():
        calls.append("config")
        return ({"name": "default-source"},)

    monkeypatch.setattr(config, "get_config_yaml_mcp_servers", load_config)
    monkeypatch.setattr(utils, "get_workspace_dir", lambda: tmp_path)
    state_path = tmp_path / "mcp" / "state.json"
    state_path.parent.mkdir()
    state_path.write_text(
        '{"mcp":{"default-source":{"state":"disconnected"}}}',
        encoding="utf-8",
    )

    result = validate_mcp_references(["default-source"])

    assert calls == ["config"]
    assert result.references[0].status is McpReferenceStatus.NOT_READY


def test_default_state_loader_reports_corruption_without_leaking_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from jiuwenswarm.common import config  # pylint: disable=import-outside-toplevel
    from jiuwenswarm.common import utils  # pylint: disable=import-outside-toplevel

    secret_marker = "private-token-state-path"
    workspace = tmp_path / secret_marker
    state_path = workspace / "mcp" / "state.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text("{not-json", encoding="utf-8")
    monkeypatch.setattr(config, "get_config_yaml_mcp_servers", lambda: ())
    monkeypatch.setattr(utils, "get_workspace_dir", lambda: workspace)

    with pytest.raises(McpReferenceInventoryError) as captured:
        validate_mcp_references(["known"])

    assert captured.value.code == "MCP_INTERNAL"
    assert captured.value.retryable is True
    assert str(captured.value) == "MCP reference inventory is unavailable"
    assert captured.value.__cause__ is None
    assert secret_marker not in str(captured.value)
    assert secret_marker not in caplog.text


def test_default_state_loader_reports_unreadable_state_without_leaking_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from jiuwenswarm.common import config  # pylint: disable=import-outside-toplevel
    from jiuwenswarm.common import utils  # pylint: disable=import-outside-toplevel

    secret_marker = "private-unreadable-state-path"
    workspace = tmp_path / secret_marker
    state_path = workspace / "mcp" / "state.json"
    state_path.mkdir(parents=True)
    monkeypatch.setattr(config, "get_config_yaml_mcp_servers", lambda: ())
    monkeypatch.setattr(utils, "get_workspace_dir", lambda: workspace)

    with pytest.raises(McpReferenceInventoryError) as captured:
        validate_mcp_references(["known"])

    assert captured.value.code == "MCP_INTERNAL"
    assert captured.value.retryable is True
    assert str(captured.value) == "MCP reference inventory is unavailable"
    assert captured.value.__cause__ is None
    assert secret_marker not in str(captured.value)
    assert secret_marker not in caplog.text


def test_empty_references_are_valid_without_loading_inventory() -> None:
    def fail_loader():
        raise AssertionError("empty validation must not read MCP inventory")

    result = validate_mcp_references(
        [],
        config_loader=fail_loader,
        state_loader=fail_loader,
    )

    assert result.valid is True
    assert not result.references
    assert result.to_dict() == {"valid": True, "references": []}


def test_inventory_is_not_mutated_and_result_does_not_expose_secrets() -> None:
    config_entry = {
        "name": "safe-name",
        "url": "https://secret.example/mcp",
        "headers": {"Authorization": "secret-header"},
        "command": "secret-command",
        "args": ["secret-arg"],
        "env": {"TOKEN": "secret-token"},
        "path": "secret-path",
    }
    state_record = {
        "state": "connected",
        "url": "https://state-secret.example/mcp",
        "token": "state-secret-token",
    }
    config_before = repr(config_entry)
    state_before = repr(state_record)

    result = _validate(
        ["safe-name"],
        config_servers=(config_entry,),
        runtime_state={"mcp": {"safe-name": state_record}},
    )
    serialized = repr(result.to_dict())

    assert repr(config_entry) == config_before
    assert repr(state_record) == state_before
    for secret in (
        "secret.example",
        "secret-header",
        "secret-command",
        "secret-arg",
        "secret-token",
        "secret-path",
        "state-secret.example",
        "state-secret-token",
    ):
        assert secret not in serialized
    assert result.to_dict() == {
        "valid": True,
        "references": [{"name": "safe-name", "status": "ready"}],
    }


def test_inventory_loader_failure_has_stable_secret_free_error() -> None:
    secret = "C:/private/mcp-state.json?token=secret"

    def fail_config_loader():
        raise OSError(secret)

    with pytest.raises(McpReferenceInventoryError) as captured:
        validate_mcp_references(
            ["known"],
            config_loader=fail_config_loader,
            state_loader=lambda: {"mcp": {}},
        )

    assert captured.value.code == "MCP_INTERNAL"
    assert captured.value.retryable is True
    assert str(captured.value) == "MCP reference inventory is unavailable"
    assert secret not in str(captured.value)
    assert captured.value.__cause__ is None


@pytest.mark.parametrize("runtime_state", [None, {"mcp": []}])
def test_malformed_runtime_inventory_fails_closed(runtime_state: Any) -> None:
    with pytest.raises(
        McpReferenceInventoryError,
        match="MCP reference inventory is unavailable",
    ):
        validate_mcp_references(
            ["known"],
            config_loader=lambda: (),
            state_loader=lambda: runtime_state,
        )


def test_result_contracts_are_frozen() -> None:
    result = _validate(["known"], config_servers=({"name": "known"},))

    with pytest.raises(FrozenInstanceError):
        result.references = ()  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        result.references[0].name = "changed"  # type: ignore[misc]


def test_cold_import_does_not_load_runtime_core_mcp_io_or_transports() -> None:
    script = """
import sys
import jiuwenswarm.runtime.mcp_references

unexpected = sorted(
    name for name in sys.modules
    if name == 'jiuwenswarm.runtime.service'
    or name == 'jiuwenswarm.common.config'
    or name.startswith('jiuwenswarm.server.runtime.mcp')
    or name.startswith('jiuwenswarm.server.agent_ws_server')
    or name.startswith('jiuwenswarm.gateway')
    or name.startswith('jiuwenswarm.channels.process_cli')
    or name.startswith('openjiuwen.core.runner')
    or name == 'websockets'
)
print('UNEXPECTED=' + repr(unexpected))
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "UNEXPECTED=[]" in result.stdout


def test_source_has_no_transport_probe_process_or_state_write_dependency() -> None:
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"), filename=str(SOURCE))
    forbidden_imports = (
        "jiuwenswarm.gateway",
        "jiuwenswarm.server.agent_ws_server",
        "jiuwenswarm.channels.process_cli",
        "jiuwenswarm.common.mcp_config",
        "jiuwenswarm.server.runtime.mcp.registry",
        "jiuwenswarm.server.runtime.mcp.credential",
        "openjiuwen",
        "websockets",
        "subprocess",
    )
    forbidden_symbols = {
        "CredentialStore",
        "Runner",
        "probe_mcp_live_connection",
        "set_mcp_state",
        "upsert_mcp_record",
        "write_mcp_state",
    }
    violations: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith(forbidden_imports):
                    violations.append(f"{node.lineno}:{alias.name}")
        elif isinstance(node, ast.ImportFrom) and node.module:
            if node.module.startswith(forbidden_imports):
                violations.append(f"{node.lineno}:{node.module}")
        elif isinstance(node, ast.Name) and node.id in forbidden_symbols:
            violations.append(f"{node.lineno}:{node.id}")
        elif isinstance(node, ast.Attribute) and node.attr in forbidden_symbols:
            violations.append(f"{node.lineno}:{node.attr}")

    assert not violations

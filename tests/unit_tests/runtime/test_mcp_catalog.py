# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for the static, transport-neutral Runtime MCP catalog."""

from __future__ import annotations

import ast
import inspect
from dataclasses import asdict

import pytest

from jiuwenswarm.runtime import mcp_catalog
from jiuwenswarm.runtime.mcp_catalog import (
    McpCatalogError,
    McpCatalogListInput,
    McpCatalogShowInput,
    list_mcp_servers,
    show_mcp_server,
)


def _secret_entry(
    name: str,
    *,
    enabled: bool = True,
    state: str = "connected",
) -> dict:
    return {
        "name": name,
        "transport": "streamable_http",
        "enabled": enabled,
        "state": state,
        "integration_type": "remote-mcp",
        "timeout_s": 30,
        "env": {"UNUSUAL_CREDENTIAL": "env-secret"},
        "headers": {"X-Custom": "header-secret"},
        "args": ["--token", "argument-secret"],
        "cwd": "C:/Users/private/project",
        "command": "C:/Users/private/bin/mcp.exe",
        "url": "https://user:password@example.invalid/mcp?token=url-secret",
        "credentials": {"value": "credential-secret"},
    }


def test_list_catalog_is_sorted_normalized_and_strictly_whitelisted() -> None:
    """Only stable public facts survive normalization and serialization."""
    result = list_mcp_servers(
        [
            _secret_entry("zeta"),
            {
                "name": "Alpha",
                "transport": "stdio",
                "enabled": False,
                "connection_state": "registered",
                "integration_type": "stdio_mcp",
                "timeout": 12.8,
            },
        ]
    )

    assert [item.name for item in result.servers] == ["Alpha", "zeta"]
    assert result.servers[0].transport == "stdio"
    assert result.servers[0].default_enabled is False
    assert result.servers[0].connection_state == "registered"
    assert result.servers[0].integration_type == "stdio-mcp"
    assert result.servers[0].timeout_seconds == 12
    assert result.servers[1].transport == "streamable-http"

    serialized = repr(result.to_dict())
    for forbidden in (
        "env-secret",
        "header-secret",
        "argument-secret",
        "C:/Users/private",
        "password",
        "url-secret",
        "credential-secret",
        "headers",
        "credentials",
    ):
        assert forbidden not in serialized
    assert set(asdict(result.servers[0])) == {
        "name",
        "transport",
        "default_enabled",
        "connection_state",
        "integration_type",
        "timeout_seconds",
    }


def test_list_catalog_filters_enabled_and_uses_last_duplicate() -> None:
    """Enabled filtering retains merged-source last-value precedence."""
    result = list_mcp_servers(
        [
            _secret_entry("same", enabled=True, state="connecting"),
            _secret_entry("disabled", enabled=False),
            _secret_entry("same", enabled=True, state="connected"),
        ],
        McpCatalogListInput(enabled_only=True),
    )

    assert [item.name for item in result.servers] == ["same"]
    assert result.servers[0].connection_state == "connected"


def test_list_catalog_skips_unsafe_names_and_bounds_open_vocab_fields() -> None:
    """Unsafe identifiers are excluded and arbitrary fields do not escape."""
    result = list_mcp_servers(
        [
            _secret_entry("../credential"),
            {
                "name": "safe_name",
                "transport": "secret-transport-value",
                "state": "secret-state-value",
                "integration_type": "secret-integration-value",
                "timeout_s": float("inf"),
            },
        ]
    )

    assert len(result.servers) == 1
    descriptor = result.servers[0]
    assert descriptor.name == "safe_name"
    assert descriptor.transport == "unknown"
    assert descriptor.connection_state == "unknown"
    assert descriptor.integration_type == ""
    assert descriptor.timeout_seconds is None


def test_show_catalog_consumes_one_shot_iterable_once() -> None:
    """A generator source is consumed exactly once by one show request."""
    consumed = 0

    def entries():
        nonlocal consumed
        consumed += 1
        yield _secret_entry("first")
        yield _secret_entry("target", enabled=False)

    result = show_mcp_server(
        entries(),
        McpCatalogShowInput(name="target"),
    )

    assert consumed == 1
    assert result.server.name == "target"
    assert result.server.default_enabled is False


@pytest.mark.parametrize("name", ["", "../secret", "a/b", "a\\b", "a.b"])
def test_show_catalog_rejects_empty_or_path_like_names(name: str) -> None:
    """Names that could escape a future name-keyed store are rejected."""
    with pytest.raises(McpCatalogError) as raised:
        show_mcp_server([], McpCatalogShowInput(name=name))

    assert raised.value.code == "BAD_REQUEST"


def test_show_catalog_reports_stable_not_found_error() -> None:
    """A missing valid name uses a stable transport-neutral error code."""
    with pytest.raises(McpCatalogError, match="not found") as raised:
        show_mcp_server(
            [_secret_entry("known")],
            McpCatalogShowInput(name="missing"),
        )

    assert raised.value.code == "NOT_FOUND"


def test_catalog_core_has_only_standard_library_imports() -> None:
    """The catalog core cannot acquire Runtime or transport side effects."""
    tree = ast.parse(inspect.getsource(mcp_catalog))
    imported_roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_roots.update(
                alias.name.split(".", maxsplit=1)[0] for alias in node.names
            )
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_roots.add(node.module.split(".", maxsplit=1)[0])

    assert imported_roots <= {
        "__future__",
        "collections",
        "dataclasses",
        "math",
        "re",
        "typing",
    }

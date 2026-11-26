# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests: persisting a lost MCP connection (issue #5711).

``registry.mark_mcp_connection_lost`` is the only writer of the "the call
timeout left this transport unrecoverable" state. It must be idempotent, must
never touch a record that is not ``connected``, and must land on the value
each kind already uses for "no live server": ``registered`` for a custom MCP
(so it stays listed under "my MCPs") and ``disconnected`` for a marketplace
package (same degrade value the startup register failure path writes).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from jiuwenswarm.server.runtime.mcp import registry
from jiuwenswarm.server.runtime.mcp import state_store as ss


class _FakePackage:
    """Stand-in for a resolved marketplace package on disk."""


def _seed(name: str, *, state: str = "connected") -> None:
    ss.upsert_mcp_record(
        name,
        {"name": name, "transport": "sse", "url": f"https://example.invalid/{name}/sse"},
        state=state,
        integration_type="remote-mcp",
    )


@pytest.fixture()
def workspace(tmp_path: Path):
    with patch("jiuwenswarm.server.runtime.mcp.state_store.get_workspace_dir", return_value=tmp_path):
        yield tmp_path


def test_custom_mcp_goes_back_to_registered(workspace: Path) -> None:
    """No package on disk -> custom MCP: keep the definition, flip to registered."""
    _seed("my-custom")
    with patch.object(registry, "_resolve_package", return_value=None):
        event = registry.mark_mcp_connection_lost("my-custom")

    assert event == {
        "name": "my-custom",
        "connection_state": "disconnected",
        "persisted_state": "registered",
    }
    assert ss.get_mcp_record("my-custom")["state"] == "registered"


def test_marketplace_mcp_goes_to_disconnected(workspace: Path) -> None:
    """Package present -> marketplace MCP: `disconnected`, matching the startup degrade."""
    _seed("huaweiyun-mcp")
    with patch.object(registry, "_resolve_package", return_value=_FakePackage()):
        event = registry.mark_mcp_connection_lost("huaweiyun-mcp")

    assert event is not None
    assert event["persisted_state"] == "disconnected"
    assert ss.get_mcp_record("huaweiyun-mcp")["state"] == "disconnected"


def test_second_mark_is_a_noop(workspace: Path) -> None:
    """Repeated timeouts must not rewrite the file or re-notify the UI."""
    _seed("my-custom")
    with patch.object(registry, "_resolve_package", return_value=None):
        assert registry.mark_mcp_connection_lost("my-custom") is not None
        assert registry.mark_mcp_connection_lost("my-custom") is None
    assert ss.get_mcp_record("my-custom")["state"] == "registered"


def test_connecting_record_is_left_alone(workspace: Path) -> None:
    """A connect/apply in flight owns its own failure path; the timeout hook stays out."""
    _seed("pending", state="connecting")
    with patch.object(registry, "_resolve_package", return_value=None):
        assert registry.mark_mcp_connection_lost("pending") is None
    assert ss.get_mcp_record("pending")["state"] == "connecting"


def test_missing_record_is_left_alone(workspace: Path) -> None:
    """config.yaml-only MCPs keep no state.json record: nothing to flip."""
    with patch.object(registry, "_resolve_package", return_value=None):
        assert registry.mark_mcp_connection_lost("yaml-only") is None
    assert ss.get_mcp_record("yaml-only") is None


def test_blank_name_is_rejected(workspace: Path) -> None:
    assert registry.mark_mcp_connection_lost("") is None
    assert registry.mark_mcp_connection_lost("   ") is None


def test_package_resolution_failure_falls_back_to_registered(workspace: Path) -> None:
    """A damaged package manifest must not make the fix silently disappear.

    ``is_stale_marketplace_record`` guards the same call: a raise here would
    be swallowed by the timeout patch, leaving the persisted state untouched.
    Unknown kind -> the soft custom value, which keeps the MCP listed.
    """
    _seed("my-custom")
    with patch.object(registry, "_resolve_package", side_effect=RuntimeError("bad manifest")):
        event = registry.mark_mcp_connection_lost("my-custom")

    assert event is not None
    assert event["persisted_state"] == "registered"
    assert ss.get_mcp_record("my-custom")["state"] == "registered"

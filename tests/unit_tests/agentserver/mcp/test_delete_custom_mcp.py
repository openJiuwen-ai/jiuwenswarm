# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for registry.delete_custom_mcp — terminal custom-MCP removal.

Stubs the persistent cleanup helpers (remove_mcp_record /
CredentialStore.delete_mcp / uninstall_mcp_skills) so they run without a
real workspace; asserts composition order and the built-in / not-found gates.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from jiuwenswarm.server.runtime.mcp import registry


def _no_packages_dir() -> Path:
    """A dir that does NOT contain any MCP package, so every name is treated
    as a custom MCP (no built-in package on disk)."""
    return Path("C:/nonexistent/mcp_builtins")


def test_delete_wipes_state_credentials_and_skills() -> None:
    """Connected custom MCP: state record, credentials, skills all deleted;
    was_connected=True so the handler knows to tear down the live server."""
    calls: list[str] = []

    def _capture_remove(name):
        calls.append(f"remove_mcp_record:{name}")
        return {"name": name}

    class _FakeCredStore:
        def __init__(self, *a, **kw):
            pass

        def delete_mcp(self, name):
            calls.append(f"delete_mcp:{name}")

    def _capture_uninstall(name):
        calls.append(f"uninstall_mcp_skills:{name}")

    with patch("jiuwenswarm.server.runtime.mcp.registry._packages_dir", _no_packages_dir), \
         patch(
             "jiuwenswarm.server.runtime.mcp.state_store.get_mcp_record",
             return_value={"state": "connected", "enabled": True},
         ), \
         patch(
             "jiuwenswarm.server.runtime.mcp.state_store.remove_mcp_record",
             side_effect=_capture_remove,
         ), \
         patch(
             "jiuwenswarm.server.runtime.mcp.credential.CredentialStore",
             _FakeCredStore,
         ), \
         patch(
             "jiuwenswarm.server.runtime.mcp.skill_installer.uninstall_mcp_skills",
             side_effect=_capture_uninstall,
         ):
        result = registry.delete_custom_mcp("my-mcp")

    assert result == {"name": "my-mcp", "removed": True, "was_connected": True}
    # State record deleted before creds/skills — the handler's live teardown
    # runs AFTER this returns, so the definition is gone before the orphaned
    # server is torn down.
    assert calls[0] == "remove_mcp_record:my-mcp"
    assert "delete_mcp:my-mcp" in calls
    assert "uninstall_mcp_skills:my-mcp" in calls


def test_delete_registered_returns_was_connected_false() -> None:
    """Registered (not connected) -> was_connected=False, so the handler
    skips live-server teardown."""
    with patch("jiuwenswarm.server.runtime.mcp.registry._packages_dir", _no_packages_dir), \
         patch(
             "jiuwenswarm.server.runtime.mcp.state_store.get_mcp_record",
             return_value={"state": "registered", "enabled": True},
         ), \
         patch(
             "jiuwenswarm.server.runtime.mcp.state_store.remove_mcp_record",
             return_value={"name": "draft-mcp"},
         ), \
         patch(
             "jiuwenswarm.server.runtime.mcp.credential.CredentialStore",
         ), \
         patch(
             "jiuwenswarm.server.runtime.mcp.skill_installer.uninstall_mcp_skills",
         ):
        result = registry.delete_custom_mcp("draft-mcp")

    assert result["was_connected"] is False
    assert result["removed"] is True


def test_delete_builtin_raises_value_error() -> None:
    """Built-in (package dir exists) -> ValueError before any mutation."""

    def _has_package_dir() -> Path:
        # Non-empty path that .is_dir() will be patched to True for the name.
        return Path("C:/some/mcp_builtins")

    with patch(
        "jiuwenswarm.server.runtime.mcp.registry._packages_dir", _has_package_dir
    ), \
         patch("pathlib.Path.is_dir", return_value=True), \
         patch(
             "jiuwenswarm.server.runtime.mcp.state_store.remove_mcp_record"
         ) as mock_remove, \
         patch(
             "jiuwenswarm.server.runtime.mcp.state_store.get_mcp_record"
         ) as mock_get:
        with pytest.raises(ValueError, match="built-in marketplace package"):
            registry.delete_custom_mcp("amap")

    # No persistent mutation attempted before the ValueError.
    mock_get.assert_not_called()
    mock_remove.assert_not_called()


def test_delete_unknown_raises_key_error() -> None:
    """No state.json record -> KeyError (handler maps to MCP_NOT_FOUND)."""
    with patch("jiuwenswarm.server.runtime.mcp.registry._packages_dir", _no_packages_dir), \
         patch(
             "jiuwenswarm.server.runtime.mcp.state_store.get_mcp_record",
             return_value=None,
         ), \
         patch(
             "jiuwenswarm.server.runtime.mcp.state_store.remove_mcp_record"
         ) as mock_remove:
        with pytest.raises(KeyError, match="not found"):
            registry.delete_custom_mcp("nope")

    mock_remove.assert_not_called()


def test_delete_empty_name_raises_value_error() -> None:
    """Empty name -> ValueError (bad_request)."""
    with pytest.raises(ValueError):
        registry.delete_custom_mcp("")


def test_delete_credential_failure_is_non_fatal() -> None:
    """A CredentialStore.delete_mcp failure is swallowed — best-effort cleanup
    that must not strand the deletion."""

    class _BrokenCredStore:
        def __init__(self, *a, **kw):
            pass

        def delete_mcp(self, name):
            raise OSError("permission denied")

    with patch("jiuwenswarm.server.runtime.mcp.registry._packages_dir", _no_packages_dir), \
         patch(
             "jiuwenswarm.server.runtime.mcp.state_store.get_mcp_record",
             return_value={"state": "connected"},
         ), \
         patch(
             "jiuwenswarm.server.runtime.mcp.state_store.remove_mcp_record",
             return_value={"name": "my-mcp"},
         ), \
         patch(
             "jiuwenswarm.server.runtime.mcp.credential.CredentialStore",
             _BrokenCredStore,
         ), \
         patch(
             "jiuwenswarm.server.runtime.mcp.skill_installer.uninstall_mcp_skills",
         ):
        result = registry.delete_custom_mcp("my-mcp")

    # Still reports removed despite the credentials OSError.
    assert result["removed"] is True

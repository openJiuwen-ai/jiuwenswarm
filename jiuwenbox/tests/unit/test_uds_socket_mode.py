# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Finding 3 (CWE-732): UDS socket must not default to world-writable."""

from __future__ import annotations

import os
import stat

import pytest

from jiuwenbox.server import app as app_module


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("JIUWENBOX_UDS_PATH", raising=False)
    monkeypatch.delenv("JIUWENBOX_UDS_MODE", raising=False)


def _sock_mode(path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


def test_default_mode_is_owner_only(tmp_path, monkeypatch):
    """Finding 3 reproduction: the old default (0666) let any local user
    reach sandbox/policy/proxy/MCP operations through the socket file."""
    sock_path = tmp_path / "jiuwenbox.sock"
    sock_path.touch(mode=0o755)  # simulate uvicorn's as-created permissions
    monkeypatch.setenv("JIUWENBOX_UDS_PATH", str(sock_path))

    app_module._chmod_uds_socket_if_any()

    assert _sock_mode(sock_path) == 0o600


def test_explicit_mode_override_is_honored(tmp_path, monkeypatch):
    """An operator who explicitly wants a shared socket can still opt in."""
    sock_path = tmp_path / "jiuwenbox.sock"
    sock_path.touch(mode=0o755)
    monkeypatch.setenv("JIUWENBOX_UDS_PATH", str(sock_path))
    monkeypatch.setenv("JIUWENBOX_UDS_MODE", "0660")

    app_module._chmod_uds_socket_if_any()

    assert _sock_mode(sock_path) == 0o660


def test_no_uds_path_is_a_noop(monkeypatch):
    # Must not raise when the server is not running in UDS mode at all.
    app_module._chmod_uds_socket_if_any()


def test_invalid_mode_falls_back_without_raising(tmp_path, monkeypatch, caplog):
    sock_path = tmp_path / "jiuwenbox.sock"
    sock_path.touch(mode=0o755)
    monkeypatch.setenv("JIUWENBOX_UDS_PATH", str(sock_path))
    monkeypatch.setenv("JIUWENBOX_UDS_MODE", "not-an-octal")

    app_module._chmod_uds_socket_if_any()

    assert _sock_mode(sock_path) == 0o755  # untouched, uvicorn's own mode
    assert "Ignoring invalid" in caplog.text

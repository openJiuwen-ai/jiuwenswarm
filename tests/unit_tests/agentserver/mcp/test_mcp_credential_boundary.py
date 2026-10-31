# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Credential operations must stay inside their dedicated storage directory."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from jiuwenswarm.server.runtime.mcp.credential import CredentialStore
from jiuwenswarm.server.runtime.mcp.registry import save_mcp_credentials


@pytest.mark.parametrize("operation", ["read", "write", "delete"])
@pytest.mark.parametrize(
    "name",
    [
        "../victim",
        "..\\victim",
        "",
        ".",
        "..",
        "C:relative",
        "C:\\victim",
        "safe:stream",
    ],
)
def test_credential_operations_reject_path_names(
    tmp_path: Path, name: str, operation: str
) -> None:
    store = CredentialStore(workspace_dir=tmp_path)
    victim = tmp_path / "mcp" / "victim.json"
    original = json.dumps({"marker": "synthetic-data"})
    victim.write_text(original)

    with pytest.raises(ValueError):
        if operation == "read":
            store.get_all(name)
        elif operation == "write":
            store.save_token(name, "key", "synthetic-value")
        else:
            store.delete_mcp(name)

    assert victim.read_text() == original


@pytest.mark.parametrize("operation", ["read", "write", "delete"])
def test_credential_operations_reject_absolute_paths(
    tmp_path: Path, operation: str
) -> None:
    store = CredentialStore(workspace_dir=tmp_path / "workspace")
    victim = tmp_path / "victim.json"
    original = json.dumps({"marker": "synthetic-data"})
    victim.write_text(original)
    name = str(victim.with_suffix(""))

    with pytest.raises(ValueError):
        if operation == "read":
            store.get_all(name)
        elif operation == "write":
            store.save_token(name, "key", "synthetic-value")
        else:
            store.delete_mcp(name)

    assert victim.read_text() == original


@pytest.mark.parametrize("operation", ["read", "write", "delete"])
def test_credential_operations_reject_symlink_files(
    tmp_path: Path, operation: str
) -> None:
    workspace = tmp_path / "workspace"
    store = CredentialStore(workspace_dir=workspace)
    victim = tmp_path / "victim.json"
    original = json.dumps({"marker": "synthetic-data"})
    victim.write_text(original)
    link = workspace / "mcp" / "credentials" / "connector.json"
    try:
        link.symlink_to(victim)
    except OSError:
        pytest.skip("symlinks unavailable on this platform")

    with pytest.raises(ValueError):
        if operation == "read":
            store.get_all("connector")
        elif operation == "write":
            store.save_token("connector", "key", "synthetic-value")
        else:
            store.delete_mcp("connector")

    assert victim.read_text() == original
    assert link.is_symlink()


def test_save_credentials_rejects_traversal_at_registry_boundary(
    tmp_path: Path,
) -> None:
    with (
        patch(
            "jiuwenswarm.server.runtime.mcp.credential.get_workspace_dir",
            return_value=tmp_path,
        ),
        pytest.raises(ValueError),
    ):
        save_mcp_credentials("../victim", {"key": "synthetic-value"})
    assert not (tmp_path / "mcp" / "victim.json").exists()


@pytest.mark.parametrize(
    "name", ["jira", "custom.mcp-1", "自定义连接器", " custom connector "]
)
def test_valid_connector_names_keep_credential_lifecycle(
    tmp_path: Path, name: str
) -> None:
    store = CredentialStore(workspace_dir=tmp_path)
    store.save_token(name, "key", "synthetic-value")
    assert store.get_all(name) == {"key": "synthetic-value"}
    store.delete_mcp(name)
    assert store.get_all(name) == {}


@pytest.mark.parametrize("name", ["../victim", "..\\victim", "C:relative", "jira"])
def test_save_credentials_rpc_enforces_storage_boundary(
    tmp_path: Path, name: str
) -> None:
    from jiuwenswarm.common.schema.agent import AgentRequest
    from jiuwenswarm.common.schema.message import ReqMethod
    from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer

    store = CredentialStore(workspace_dir=tmp_path)
    victim = tmp_path / "mcp" / "victim.json"
    original = json.dumps({"marker": "synthetic-data"})
    victim.write_text(original)
    ws = SimpleNamespace(send=AsyncMock())
    request = AgentRequest(
        request_id="credential-boundary-test",
        session_id="test-session",
        channel_id="web",
        req_method=ReqMethod.MCP_SAVE_CREDENTIALS,
        params={"name": name, "tokens": {"key": "synthetic-value"}},
    )
    server = AgentWebSocketServer.__new__(AgentWebSocketServer)
    with patch(
        "jiuwenswarm.server.runtime.mcp.credential.get_workspace_dir",
        return_value=tmp_path,
    ):
        asyncio.run(server._handle_mcp_save_credentials(ws, request, asyncio.Lock()))

    ws.send.assert_awaited_once()
    response = json.loads(ws.send.call_args.args[0])
    assert victim.read_text() == original
    if name == "jira":
        assert response["body"]["result"]["type"] == "credentials_saved"
        assert store.get_all(name) == {"key": "synthetic-value"}
        assert "synthetic-value" not in ws.send.call_args.args[0]
    else:
        assert response["body"]["details"]["code"] == "MCP_BAD_REQUEST"
        assert not list((tmp_path / "mcp" / "credentials").glob("*.json"))

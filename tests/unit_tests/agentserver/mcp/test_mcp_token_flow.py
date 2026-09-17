# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""End-to-end test: form-B (static-token) MCP connect flow.

tianyancha-style: mcp.json has headers.Authorization = "Bearer ${TIANYANCHA_API_KEY}".
Flow: connect -> credentials_required sentinel -> save_connector_credentials
-> connect -> entry resolved with real token + upserted.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from jiuwenswarm.server.runtime.mcp import registry


def _mk_marketplace(workspace: Path, name: str, mcp: dict) -> None:
    pkg = workspace / "mcp" / "mcp_builtins" / name
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "mcp.json").write_text(json.dumps(mcp), encoding="utf-8")


def _tyc_mcp() -> dict:
    return {"mcpServers": {"tyc-mcp": {
        "type": "streamableHttp", "url": "https://mcp.tianyancha.com/v1",
        "headers": {"Authorization": "Bearer ${TIANYANCHA_API_KEY}"},
    }}}


def test_connect_token_connector_returns_credentials_required(tmp_path: Path) -> None:
    """First connect of a token-placeholder MCP: no tokens stored -> sentinel."""
    _mk_marketplace(tmp_path, "tyc-mcp", _tyc_mcp())
    with _ws(tmp_path):
        result = registry.connect_mcp("tyc-mcp")
    assert result.get("credentials_required") is True
    assert "TIANYANCHA_API_KEY" in result["required_tokens"]
    assert result["credential_kind"] == "token"


def test_connect_after_save_keeps_placeholder_in_state(tmp_path: Path) -> None:
    """After save_mcp_credentials, connect writes the entry WITH
    placeholders (no plaintext in state.json). Resolution happens at
    McpServerConfig build time (interface_deep._build_mcp_server_config),
    not here — so state.json stays secret-free."""
    _mk_marketplace(tmp_path, "tyc-mcp", _tyc_mcp())
    upserts: list[dict] = []
    with _ws(tmp_path), \
         patch("jiuwenswarm.server.runtime.mcp.state_store.upsert_mcp_record") as upsert:
        upsert.side_effect = lambda n, e, **kw: upserts.append({"name": n, **e, **kw})
        registry.save_mcp_credentials("tyc-mcp", {"TIANYANCHA_API_KEY": "real-key-123"})
        result = registry.connect_mcp("tyc-mcp")
    # No credentials_required now — tokens are stored.
    assert "credentials_required" not in result
    assert len(upserts) == 1
    entry = upserts[0]
    # state.json stores the placeholder, NOT the real token (gap-1 fix).
    assert entry["headers"]["Authorization"] == "Bearer ${TIANYANCHA_API_KEY}"
    assert entry["url"] == "https://mcp.tianyancha.com/v1"
    # The real value is in CredentialStore, ready for runtime resolution.
    from jiuwenswarm.server.runtime.mcp.credential import CredentialStore
    assert CredentialStore(workspace_dir=tmp_path).get_token("tyc-mcp", "TIANYANCHA_API_KEY") == "real-key-123"


def test_disconnect_wipes_credentials(tmp_path: Path) -> None:
    """disconnect wipes the local CredentialStore token file — a disconnect
    severs the auth, so the stored ${VAR} token must not linger on disk
    (reconnect re-prompts for it)."""
    _mk_marketplace(tmp_path, "tyc-mcp", _tyc_mcp())
    from jiuwenswarm.server.runtime.mcp.credential import CredentialStore
    with _ws(tmp_path):
        registry.save_mcp_credentials("tyc-mcp", {"TIANYANCHA_API_KEY": "k"})
        assert CredentialStore().get_token("tyc-mcp", "TIANYANCHA_API_KEY") == "k"
        with patch("jiuwenswarm.server.runtime.mcp.state_store.remove_mcp_record", return_value={}):
            registry.disconnect_mcp("tyc-mcp")
        # Token is gone after disconnect — reconnect must re-prompt.
        assert CredentialStore().get_token("tyc-mcp", "TIANYANCHA_API_KEY") is None


def test_connect_no_placeholder_connector_upserts_directly(tmp_path: Path) -> None:
    """Form A (notion, no placeholders) connects without credential prompt."""
    _mk_marketplace(tmp_path, "notion", {"mcpServers": {"notion": {"url": "https://mcp.notion.com/mcp"}}})
    upserts: list[dict] = []
    with _ws(tmp_path), \
         patch("jiuwenswarm.server.runtime.mcp.state_store.upsert_mcp_record") as upsert:
        upsert.side_effect = lambda n, e, **kw: upserts.append({"name": n, **e, **kw})
        result = registry.connect_mcp("notion")
    assert "credentials_required" not in result
    assert len(upserts) == 1
    assert upserts[0]["url"] == "https://mcp.notion.com/mcp"


class _ws_ctx:
    """Context manager patching get_workspace_dir across registry+credential+cli_driver."""
    def __init__(self, path: Path):
        self.path = path
    def __enter__(self):
        self._p1 = patch("jiuwenswarm.server.runtime.mcp.registry.get_workspace_dir", return_value=self.path)
        self._p2 = patch("jiuwenswarm.server.runtime.mcp.credential.get_workspace_dir", return_value=self.path)
        self._p1.start(); self._p2.start()
        return self
    def __exit__(self, *exc):
        self._p1.stop(); self._p2.stop()


def _ws(path: Path) -> _ws_ctx:
    return _ws_ctx(path)

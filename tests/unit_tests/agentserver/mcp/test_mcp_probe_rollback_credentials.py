# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""JW-79 regression tests: probe-failure rollback must not wipe credentials
unless the failure proves the tokens were rejected (auth 401/403).

A network-class probe failure (timeout / unreachable / 5xx) never reaches the
server's auth layer, so the stored tokens were never validated. The old code
rolled back every probe failure through ``_wipe_stored_credentials``, turning
a transient outage into "token deleted + connector reset". These tests pin
the classification helper and the rollback branches.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.unit_tests.agentserver.mcp.manifest_helpers import write_manifest

from jiuwenswarm.common.mcp_config import probe_failure_is_auth_rejected
from jiuwenswarm.server.runtime.mcp import registry


@pytest.mark.parametrize(
    "reason",
    [
        "auth rejected (HTTP 401)",
        "auth rejected (HTTP 403)",
        "x: auth rejected (HTTP 403); extra",
    ],
)
def test_auth_rejected_reasons_are_classified_credential(reason: str) -> None:
    assert probe_failure_is_auth_rejected(reason) is True


@pytest.mark.parametrize(
    "reason",
    [
        "",
        "ok (http 200)",
        "http probe timed out after 10s (server not responding): ReadTimeout",
        "unreachable: ConnectError: [Errno 111] Connection refused",
        "http 502 from server: Bad Gateway",
        "http 400 from server: auth fail:token fail",
        "command 'npx' not found on PATH (install it or add to PATH)",
        "invalid url: empty",
        "mcp server connect failed, server_config={...}, error='timed out'",
    ],
)
def test_network_and_env_reasons_are_not_credential(reason: str) -> None:
    assert probe_failure_is_auth_rejected(reason) is False


def _seed_custom(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str = "net-custom"
) -> Path:
    from jiuwenswarm.server.runtime.mcp import credential as cred_mod
    from jiuwenswarm.server.runtime.mcp import state_store

    monkeypatch.setattr(registry, "get_workspace_dir", lambda: tmp_path)
    monkeypatch.setattr(state_store, "get_workspace_dir", lambda: tmp_path)
    monkeypatch.setattr(cred_mod, "get_workspace_dir", lambda: tmp_path)
    state_store.upsert_mcp_record(
        name,
        {
            "name": name,
            "transport": "streamable-http",
            "url": "https://x",
            "server_id_scope": f"mcp:{name}",
        },
        state="connecting",
        integration_type="remote-mcp",
    )
    cred_mod.CredentialStore(workspace_dir=tmp_path).save_token(name, "TOK", "user-secret")
    return tmp_path / "mcp" / "credentials" / f"{name}.json"


def test_rollback_network_failure_keeps_stored_tokens(tmp_path: Path, monkeypatch) -> None:
    """JW-79 core: wipe_credentials=False (network-class probe failure) rolls
    the state back but keeps the stored token file, so the next connect
    reuses the saved token instead of re-prompting."""
    from jiuwenswarm.server.runtime.mcp import state_store

    cred_file = _seed_custom(tmp_path, monkeypatch)
    assert cred_file.is_file()  # seeded

    registry.rollback_failed_connect("net-custom", wipe_credentials=False)

    rec = state_store.get_mcp_record("net-custom")
    assert rec["state"] == "registered"  # definition kept for retry
    assert cred_file.is_file()  # token survives the transient outage


def test_rollback_auth_rejection_wipes_stored_tokens(tmp_path: Path, monkeypatch) -> None:
    """401/403 means the tokens are proven bad: wipe them (legacy behavior)
    so the next connect re-prompts for credentials."""
    from jiuwenswarm.server.runtime.mcp import state_store

    cred_file = _seed_custom(tmp_path, monkeypatch)
    assert cred_file.is_file()

    registry.rollback_failed_connect("net-custom", wipe_credentials=True)

    rec = state_store.get_mcp_record("net-custom")
    assert rec["state"] == "registered"
    assert not cred_file.is_file()  # bad token wiped


def test_rollback_default_wipes_stored_tokens(tmp_path: Path, monkeypatch) -> None:
    """Default (no kwarg) keeps the legacy contract for unclassified callers
    (e.g. cancel_connect's rollback of a connecting record)."""
    cred_file = _seed_custom(tmp_path, monkeypatch)

    registry.rollback_failed_connect("net-custom")

    assert not cred_file.is_file()


def test_rollback_marketplace_network_failure_keeps_credentials(
    tmp_path: Path, monkeypatch
) -> None:
    """Marketplace branch (package dir): a network-class failure rolls back
    skills + record but keeps the token file."""
    from jiuwenswarm.server.runtime.mcp import credential as cred_mod
    from jiuwenswarm.server.runtime.mcp import state_store

    monkeypatch.setattr(registry, "get_workspace_dir", lambda: tmp_path)
    monkeypatch.setattr(state_store, "get_workspace_dir", lambda: tmp_path)
    monkeypatch.setattr(cred_mod, "get_workspace_dir", lambda: tmp_path)
    pkg_dir = tmp_path / "mcp" / "mcp_builtins" / "ssh-mcp-server"
    pkg_dir.mkdir(parents=True)
    (pkg_dir / "mcp.json").write_text(
        '{"mcpServers":{"ssh-mcp-server":{"command":"npx"}}}', encoding="utf-8"
    )
    (pkg_dir / "token-schema.json").write_text('{"fields":[]}', encoding="utf-8")
    write_manifest(pkg_dir, "stdio-mcp", credentials_type="token")
    state_store.upsert_mcp_record(
        "ssh-mcp-server",
        {
            "name": "ssh-mcp-server",
            "transport": "stdio",
            "command": "npx",
            "server_id_scope": "mcp:ssh-mcp-server",
        },
        state="connecting",
        integration_type="stdio-mcp",
    )
    cred_mod.CredentialStore(workspace_dir=tmp_path).save_token(
        "ssh-mcp-server", "SSH_HOST", "user-secret"
    )
    cred_file = tmp_path / "mcp" / "credentials" / "ssh-mcp-server.json"
    assert cred_file.is_file()

    registry.rollback_failed_connect("ssh-mcp-server", wipe_credentials=False)

    assert state_store.get_mcp_record("ssh-mcp-server") is None  # record removed
    assert cred_file.is_file()  # token kept for retry


def test_rollback_empty_name_is_noop() -> None:
    """Guard: empty/whitespace name is a no-op (no raise)."""
    registry.rollback_failed_connect("")
    registry.rollback_failed_connect("   ")


# ---------------------------------------------------------------------------
# Handler-level: _run_mcp_connect_flow / _persist_and_probe_mcp must classify
# the probe reason and pass wipe_credentials through to the rollback.
# ---------------------------------------------------------------------------


class _RollbackRecorder:
    """Capture rollback_failed_connect calls (name, wipe_credentials)."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, bool]] = []

    def __call__(self, name: str, *, wipe_credentials: bool = True) -> None:
        self.calls.append((name, wipe_credentials))


@pytest.mark.asyncio
async def test_connect_flow_network_probe_failure_keeps_credentials() -> None:
    """A timeout probe failure rolls the state back but MUST pass
    wipe_credentials=False — the tokens were never validated."""
    from types import SimpleNamespace
    from unittest.mock import patch

    from jiuwenswarm.server.handlers import mcp_hub

    class _Manager:
        async def probe_mcp_live_connection(self, name: str):
            return False, "http probe timed out after 10s (server not responding): ReadTimeout"

    services = SimpleNamespace(agent_manager=_Manager())
    recorder = _RollbackRecorder()

    with (
        patch(
            "jiuwenswarm.server.runtime.mcp.registry.connect_mcp",
            return_value={"name": "github", "transport": "http", "url": "https://x"},
        ),
        patch(
            "jiuwenswarm.server.runtime.mcp.registry.rollback_failed_connect",
            new=recorder,
        ),
    ):
        payload = await mcp_hub._run_mcp_connect_flow(services, "github")

    assert payload["type"] == "connect_failed"
    assert payload["code"] == "MCP_UNREACHABLE"
    assert recorder.calls == [("github", False)]


@pytest.mark.asyncio
async def test_connect_flow_auth_rejected_probe_failure_wipes_credentials() -> None:
    """A 401 probe failure passes wipe_credentials=True and surfaces
    MCP_CREDENTIALS_INVALID so the frontend can prompt for a new token."""
    from types import SimpleNamespace
    from unittest.mock import patch

    from jiuwenswarm.server.handlers import mcp_hub

    class _Manager:
        async def probe_mcp_live_connection(self, name: str):
            return False, "auth rejected (HTTP 401)"

    services = SimpleNamespace(agent_manager=_Manager())
    recorder = _RollbackRecorder()
    with (
        patch(
            "jiuwenswarm.server.runtime.mcp.registry.connect_mcp",
            return_value={"name": "github", "transport": "http", "url": "https://x"},
        ),
        patch(
            "jiuwenswarm.server.runtime.mcp.registry.rollback_failed_connect",
            new=recorder,
        ),
    ):
        payload = await mcp_hub._run_mcp_connect_flow(services, "github")

    assert payload["type"] == "connect_failed"
    assert payload["code"] == "MCP_CREDENTIALS_INVALID"
    assert recorder.calls == [("github", True)]


@pytest.mark.asyncio
async def test_host_oauth_network_probe_failure_keeps_oauth_tokens(
    tmp_path: Path, monkeypatch
) -> None:
    """JW-79 scenario B: after the browser OAuth completes and the tokens land
    in the CredentialStore, a network-class probe failure must NOT delete them
    (the user would otherwise re-do the full browser authorization after a
    mere transient outage)."""
    from types import SimpleNamespace
    from unittest.mock import patch

    from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer
    from jiuwenswarm.server.runtime.mcp import oauth_flow, registry

    monkeypatch.setattr(oauth_flow, "get_workspace_dir", lambda: tmp_path)
    cred_file = tmp_path / "mcp" / "credentials" / "canva.json"

    # Token completion (what the loopback callback's exchange wrote)…
    from jiuwenswarm.server.runtime.mcp import credential as cred_mod

    monkeypatch.setattr(cred_mod, "get_workspace_dir", lambda: tmp_path)
    cred_mod.CredentialStore(workspace_dir=tmp_path).save_token(
        "canva", "OAUTH_ACCESS_TOKEN", "at-1"
    )
    assert cred_file.is_file()

    syncs: list[str] = []

    class _Manager:
        async def probe_mcp_live_connection(self, name: str):
            # Network-class failure after the OAuth exchange succeeded.
            return False, "unreachable: ConnectError: connection refused"

        def sync_mcp_credentials(self) -> None:
            syncs.append("sync")

    class _Server:
        _agent_manager = _Manager()

        @staticmethod
        def _mask_sensitive_fields(item: dict) -> dict:
            return item

    recorder = _RollbackRecorder()
    with (
        patch.object(oauth_flow, "load_oauth_credentials", return_value={"access_token": "at-1"}),
        patch.object(registry, "complete_host_oauth_auth", return_value={"name": "canva"}),
        patch(
            "jiuwenswarm.server.runtime.mcp.registry.rollback_failed_connect",
            new=recorder,
        ),
    ):
        result = await AgentWebSocketServer._await_host_oauth_auth(
            _Server(), "canva", max_attempts=1, delay=0,
        )

    assert result["type"] == "auth_failed"
    # Rollback ran for the state record, but credentials were kept.
    assert recorder.calls == [("canva", False)]
    assert cred_file.is_file()  # OAuth token survives
    assert "unreachable" in result["error"]  # honest error, not "rejected"

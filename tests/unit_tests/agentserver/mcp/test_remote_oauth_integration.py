"""Registry, persistence, and the real pinned SDK's OAuth HTTP transport."""

import json

import httpx
import pytest

from jiuwenswarm.server.runtime.mcp import registry
from jiuwenswarm.server.runtime.mcp import remote_oauth as oauth
from jiuwenswarm.server.runtime.mcp.credential import CredentialStore
from tests.unit_tests.agentserver.mcp.manifest_helpers import write_manifest
from tests.unit_tests.agentserver.mcp.test_remote_oauth import NAME, Provider, authorize


@pytest.fixture
def setup(tmp_path, monkeypatch):
    provider = Provider()
    manager = oauth.RemoteOAuthManager(
        tmp_path, transport=httpx.MockTransport(provider)
    )
    monkeypatch.setattr(oauth, "_manager", manager)
    for module in ("registry", "credential", "state_store", "skill_installer"):
        monkeypatch.setattr(
            f"jiuwenswarm.server.runtime.mcp.{module}.get_workspace_dir",
            lambda: tmp_path,
        )
    package = tmp_path / "mcp/mcp_builtins/qcc-company"
    package.mkdir(parents=True)
    (package / "mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    NAME: {
                        "type": "streamableHttp",
                        "url": oauth.QCC_RESOURCE,
                        "headers": {"Authorization": "${QICHACHA_API_KEY}"},
                    }
                }
            }
        )
    )
    (package / "token-schema.json").write_text(
        json.dumps({"fields": [{"key": "QICHACHA_API_KEY", "required": True}]})
    )
    write_manifest(package, "remote-mcp", credentials_type="token")
    yield manager, provider, tmp_path
    for name, pending in list(manager.pending.items()):
        manager.cancel(name, pending.id)


def test_existing_key_keeps_working_and_oauth_is_an_optional_upgrade(setup):
    manager, _, workspace = setup
    first = registry.connect_mcp(NAME)
    assert first["oauth_available"] and first["credentials_required"]
    registry.save_mcp_credentials(NAME, {"QICHACHA_API_KEY": "existing-key"})
    existing = registry.connect_mcp(NAME)
    assert not existing.get("oauth_provider")
    assert existing["headers"]["Authorization"] == "${QICHACHA_API_KEY}"
    assert (
        CredentialStore(workspace_dir=workspace).get_token(NAME, "QICHACHA_API_KEY")
        == "existing-key"
    )
    assert not manager.grant(NAME)


def test_oauth_state_survives_restart_and_key_switch_clears_mode(setup):
    from jiuwenswarm.server.runtime.mcp.state_store import (
        get_mcp_record,
        record_to_mcp_entry,
    )

    manager, _, workspace = setup
    authorize(manager)
    entry = registry.connect_mcp(NAME)
    assert entry["oauth_provider"] == "qcc" and "headers" not in entry
    stored = record_to_mcp_entry(NAME, get_mcp_record(NAME))
    assert stored["oauth_provider"] == "qcc"
    state = (workspace / "mcp/state.json").read_text()
    assert "access-0" not in state and "refresh-0" not in state
    registry.save_mcp_credentials(NAME, {"QICHACHA_API_KEY": "fallback-key"})
    registry.connect_mcp(NAME)
    stored = record_to_mcp_entry(NAME, get_mcp_record(NAME))
    assert not stored.get("oauth_provider")
    assert stored["headers"]["Authorization"] == "${QICHACHA_API_KEY}"


def test_profile_not_enabled_for_changed_destination(setup):
    _, _, workspace = setup
    path = workspace / "mcp/mcp_builtins/qcc-company/mcp.json"
    data = json.loads(path.read_text())
    data["mcpServers"][NAME]["url"] = "https://attacker.example/mcp"
    path.write_text(json.dumps(data))
    assert not registry.supports_remote_oauth(NAME)
    assert not registry.connect_mcp(NAME)["oauth_available"]


@pytest.mark.asyncio
async def test_core_registered_transport_initializes_lists_calls_and_refreshes(
    setup, monkeypatch
):
    from openjiuwen.core.runner.resources_manager.tool_manager import ToolMgr

    from jiuwenswarm.common.mcp_config import build_mcp_server_config
    from jiuwenswarm.server.runtime.mcp.remote_oauth_transport import (
        OAuthStreamableHttpClient,
    )

    manager, provider, _ = setup
    authorize(manager)
    seen = []

    def resource(request):
        seen.append(
            (request.method, str(request.url), request.headers.get("Authorization"))
        )
        if request.method != "POST":
            return httpx.Response(405)
        body = json.loads(request.content)
        method = body["method"]
        if method.startswith("notifications/"):
            return httpx.Response(202)
        result = {
            "initialize": {
                "protocolVersion": "2025-06-18",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "test", "version": "1"},
            },
            "tools/list": {
                "tools": [
                    {
                        "name": "company",
                        "description": "test",
                        "inputSchema": {"type": "object"},
                    }
                ]
            },
            "tools/call": {"content": [{"type": "text", "text": "verified"}]},
        }[method]
        return httpx.Response(
            200, json={"jsonrpc": "2.0", "id": body["id"], "result": result}
        )

    original = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kw: original(transport=httpx.MockTransport(resource), **kw),
    )
    config = build_mcp_server_config(registry.connect_mcp(NAME), server_id_scope="test")
    client = ToolMgr._create_client(config)
    assert isinstance(client, OAuthStreamableHttpClient)
    assert not config.auth_headers
    assert await client.connect()
    try:
        assert (await client.list_tools())[0].name == "company"
        grant = manager.grant(NAME)
        grant["expires_at"] = 0
        manager._save(NAME, grant)
        assert await client.call_tool("company", {}) == "verified"
        assert provider.refreshes == 1
        assert seen[-1][2] == "Bearer access-1"
    finally:
        await client.disconnect()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "target", ["https://agent.qcc.com/other", "https://attacker.example/mcp"]
)
async def test_sdk_redirect_does_not_forward_credentials(setup, monkeypatch, target):
    from openjiuwen.core.runner.resources_manager.tool_manager import ToolMgr

    from jiuwenswarm.common.mcp_config import build_mcp_server_config

    manager, _, _ = setup
    authorize(manager)
    seen = []

    def resource(request):
        seen.append(str(request.url))
        return httpx.Response(307, headers={"Location": target})

    original = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kw: original(transport=httpx.MockTransport(resource), **kw),
    )
    client = ToolMgr._create_client(build_mcp_server_config(registry.connect_mcp(NAME)))
    assert not await client.connect()
    assert seen and all(url == oauth.QCC_RESOURCE for url in seen)


def test_static_transport_unchanged(setup):
    from jiuwenswarm.common.mcp_config import build_mcp_server_config

    config = build_mcp_server_config(
        {
            "name": "other",
            "url": "https://example.com/mcp",
            "transport": "streamable-http",
            "headers": {"Authorization": "static"},
        }
    )
    assert config.client_type == "streamable-http"
    assert config.auth_headers == {"Authorization": "static"}


def test_invalid_key_submission_preserves_oauth(setup):
    manager, _, _ = setup
    authorize(manager)
    for tokens in ({}, {"wrong": "value"}, {"QICHACHA_API_KEY": ""}):
        with pytest.raises(ValueError):
            registry.save_mcp_credentials(NAME, tokens)
        assert manager.grant(NAME)


def test_cancel_does_not_remove_newer_or_finished_connection(setup):
    from jiuwenswarm.server.runtime.mcp.state_store import get_mcp_record

    manager, _, _ = setup
    session = manager.begin(NAME)["oauth_session"]
    registry.cancel_remote_oauth(NAME, session)
    assert not get_mcp_record(NAME)
    authorize(manager)
    registry.connect_mcp(NAME)
    registry.cancel_remote_oauth(NAME, session)
    assert get_mcp_record(NAME)
    registry.cancel_remote_oauth(NAME, manager.pending[NAME].id)
    assert get_mcp_record(NAME)


@pytest.mark.asyncio
@pytest.mark.parametrize("signal", [KeyboardInterrupt, SystemExit])
async def test_process_control_exceptions_propagate_after_cleanup(
    setup, monkeypatch, signal
):
    from openjiuwen.core.runner.resources_manager.tool_manager import ToolMgr

    from jiuwenswarm.common.mcp_config import build_mcp_server_config

    manager, _, _ = setup
    authorize(manager)
    client = ToolMgr._create_client(build_mcp_server_config(registry.connect_mcp(NAME)))
    cleaned = []

    def interrupted_client(**_kwargs):
        raise signal()

    async def disconnect():
        cleaned.append(True)

    monkeypatch.setattr(httpx, "AsyncClient", interrupted_client)
    monkeypatch.setattr(client, "disconnect", disconnect)
    with pytest.raises(signal):
        await client.connect()
    assert cleaned == [True]


def test_generic_cancel_connect_stops_remote_oauth_and_preserves_finished_grant(setup):
    manager, _, _ = setup
    session = manager.begin(NAME)["oauth_session"]
    try:
        assert registry.cancel_connect(NAME)["type"] == "cancelled"
        with pytest.raises(oauth.OAuthError):
            manager.wait(NAME, session)
        assert manager.pending[NAME].server is None
        authorize(manager)
        registry.connect_mcp(NAME)
        grant = manager.grant(NAME)
        registry.cancel_connect(NAME)
        assert manager.grant(NAME) == grant
    finally:
        registry.clear_connect_cancel(NAME)

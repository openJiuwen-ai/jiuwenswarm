"""OAuth finalization failures must produce a frontend response."""

from threading import RLock
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from jiuwenswarm.common import mcp_remote_oauth as oauth
from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer
from jiuwenswarm.server.runtime.mcp import registry, state_store


@pytest.mark.asyncio
@pytest.mark.parametrize("grant_kind", ["corrupt", "empty", "same", "newer"])
async def test_finalize_failure_handles_corrupt_grant_and_preserves_newer_session(monkeypatch, grant_kind):
    manager = SimpleNamespace(
        lock=RLock(),
        finish=Mock(side_effect=oauth.OAuthError("cancelled")),
        grant=Mock(),
    )
    if grant_kind == "corrupt":
        manager.grant.side_effect = oauth.OAuthError("invalid grant")
    else:
        manager.grant.return_value = {} if grant_kind == "empty" else {
            "oauth_session": "new-session" if grant_kind == "newer" else "old-session"
        }
    monkeypatch.setattr(oauth, "oauth_manager", lambda: manager)
    monkeypatch.setattr(registry, "connect_mcp", lambda name: {})
    monkeypatch.setattr(registry, "was_connect_cancelled", lambda name: False)
    remove = Mock()
    monkeypatch.setattr(state_store, "remove_mcp_record", remove)
    server = SimpleNamespace(
        _agent_manager=SimpleNamespace(probe_mcp_live_connection=AsyncMock(return_value=(True, "")))
    )

    result = await AgentWebSocketServer._run_mcp_connect_flow(
        server, "qcc-company", oauth_session="old-session"
    )

    assert result["type"] == "connect_failed"
    assert result["code"] == "MCP_AUTH_FAILED"
    assert result["name"] == "qcc-company"
    if grant_kind == "newer":
        remove.assert_not_called()
    else:
        remove.assert_called_once_with("qcc-company")


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["remote", "host", "cli"])
async def test_wait_auth_routes_each_oauth_flow_independently(monkeypatch, kind):
    import asyncio
    from jiuwenswarm.server import agent_ws_server
    from jiuwenswarm.server.runtime.mcp import oauth_flow

    manager = SimpleNamespace(wait=Mock())
    monkeypatch.setattr(oauth, "oauth_manager", lambda: manager)
    monkeypatch.setattr(oauth_flow, "is_host_oauth_mcp", lambda name: name == "canva")
    send = AsyncMock()
    monkeypatch.setattr(agent_ws_server, "send_wire_payload", send)
    monkeypatch.setattr(agent_ws_server, "encode_agent_response_for_wire", lambda response, response_id: response)
    result = {"type": "connected"}
    server = SimpleNamespace(
        _run_mcp_connect_flow=AsyncMock(return_value=result),
        _await_host_oauth_auth=AsyncMock(return_value=result),
        _await_cli_auth=AsyncMock(return_value=result),
    )
    name = {"remote": "qcc-company", "host": "canva", "cli": "feishu"}[kind]
    params = {"name": name, "step_index": 2}
    if kind == "remote":
        params["oauth_session"] = "current-session"
    request = SimpleNamespace(params=params, request_id="test-wait", channel_id="web")
    await AgentWebSocketServer._handle_mcp_wait_auth(server, object(), request, asyncio.Lock())

    assert send.await_args.args[1].ok is True
    if kind == "remote":
        manager.wait.assert_called_once_with(name, "current-session")
        server._run_mcp_connect_flow.assert_awaited_once_with(
            name, rollback_on_probe_failure=False, oauth_session="current-session"
        )
    else:
        manager.wait.assert_not_called()
        server._run_mcp_connect_flow.assert_not_awaited()
    if kind == "host":
        server._await_host_oauth_auth.assert_awaited_once_with(name)
    else:
        server._await_host_oauth_auth.assert_not_awaited()
    if kind == "cli":
        server._await_cli_auth.assert_awaited_once_with(name, 2)
    else:
        server._await_cli_auth.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancelled", [False, True])
async def test_remote_oauth_probe_failure_preserves_cancel_and_rollback(monkeypatch, cancelled):
    from jiuwenswarm.common import mcp_config

    checks = iter([False, cancelled])
    monkeypatch.setattr(registry, "connect_mcp", lambda name: {"name": name})
    monkeypatch.setattr(registry, "was_connect_cancelled", lambda name: next(checks))
    cancel = Mock()
    rollback = Mock()
    discard = AsyncMock()
    monkeypatch.setattr(registry, "cancel_remote_oauth", cancel)
    monkeypatch.setattr(registry, "rollback_failed_connect", rollback)
    monkeypatch.setattr(mcp_config, "discard_probed_mcp", discard)
    server = SimpleNamespace(
        _agent_manager=SimpleNamespace(probe_mcp_live_connection=AsyncMock(return_value=(False, "unreachable")))
    )
    result = await AgentWebSocketServer._run_mcp_connect_flow(
        server, "qcc-company", oauth_session="current-session"
    )
    if cancelled:
        assert result == {"type": "cancelled", "name": "qcc-company"}
        discard.assert_awaited_once_with("qcc-company")
        cancel.assert_not_called()
        rollback.assert_not_called()
    else:
        assert result["type"] == "connect_failed"
        assert result["code"] == "MCP_UNREACHABLE"
        cancel.assert_called_once_with("qcc-company", "current-session")
        rollback.assert_called_once_with("qcc-company")
        discard.assert_not_awaited()

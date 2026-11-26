"""Regression tests for MCP timeout forwarding into openjiuwen clients."""

from __future__ import annotations

# pylint: disable=protected-access

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import openjiuwen.core.foundation.tool.mcp.client.sse_client as sse_module
import openjiuwen.core.foundation.tool.mcp.client.streamable_http_client as streamable_module
import openjiuwen.core.runner.resources_manager.tool_manager as tool_manager_module

from jiuwenswarm.runtime.run_context import CURRENT_RUN_CONTEXT, RunContext
from jiuwenswarm.server.runtime.mcp import call_timeout_patch
from jiuwenswarm.server.runtime.mcp import connection_events
from jiuwenswarm.server.runtime.mcp import state_store as ss


class _FakeClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, float | None]] = []

    async def connect(self, *args, **kwargs):
        self.calls.append(("connect", kwargs.get("timeout")))
        return True

    async def call_tool(self, *args, **kwargs):
        self.calls.append(("call_tool", kwargs.get("timeout")))
        return "ok"

    async def list_tools(self, *args, **kwargs):
        self.calls.append(("list_tools", kwargs.get("timeout")))
        return []


def _install_fakes(monkeypatch) -> tuple[type[_FakeClient], type]:
    class FakeStreamable(_FakeClient):
        pass

    class FakeSse(_FakeClient):
        pass

    class FakeToolMgr:
        @staticmethod
        def _create_client(config):
            return FakeSse()

    monkeypatch.setattr(streamable_module, "StreamableHttpClient", FakeStreamable)
    monkeypatch.setattr(sse_module, "SseClient", FakeSse)
    monkeypatch.setattr(tool_manager_module, "ToolMgr", FakeToolMgr)
    return FakeSse, FakeToolMgr


@pytest.fixture(autouse=True)
def _reset_patch_state():
    call_timeout_patch._PATCHED = False
    call_timeout_patch._wrapped_methods.clear()
    yield
    call_timeout_patch._PATCHED = False
    call_timeout_patch._wrapped_methods.clear()


@pytest.mark.asyncio
async def test_configured_timeout_reaches_connect_and_calls(monkeypatch) -> None:
    fake_sse, _ = _install_fakes(monkeypatch)
    call_timeout_patch.apply_mcp_call_timeout_patch()

    client = fake_sse()
    client._jws_call_timeout = 3600
    await client.connect()
    await client.call_tool("demo", {})
    await client.list_tools()

    assert client.calls == [
        ("connect", 3600.0),
        ("call_tool", 3600.0),
        ("list_tools", 3600.0),
    ]


@pytest.mark.asyncio
async def test_unconfigured_timeout_keeps_connect_default(monkeypatch) -> None:
    fake_sse, _ = _install_fakes(monkeypatch)
    call_timeout_patch.apply_mcp_call_timeout_patch()

    client = fake_sse()
    await client.connect()
    await client.call_tool("demo", {})

    assert client.calls == [
        ("connect", None),
        ("call_tool", call_timeout_patch.DEFAULT_CALL_TIMEOUT),
    ]


def test_create_client_stamps_configured_timeout(monkeypatch) -> None:
    _, fake_tool_mgr = _install_fakes(monkeypatch)
    call_timeout_patch.apply_mcp_call_timeout_patch()

    client = fake_tool_mgr._create_client(
        SimpleNamespace(params={"timeout_s": 3600})
    )

    assert client._jws_call_timeout == 3600.0


# --------------------------------------------------------------------------- #
# Lost-connection persistence (issue #5711)
#
# A timeout only flips state.json once the transport is provably gone:
#   * no lazy reconnect (SSE): the patch tore the client down;
#   * lazy reconnect (streamable-http): the *next* call ended on the client's
#     "Not connected" guard, i.e. the reconnect was attempted and abandoned.
# A slow-but-alive call, a user cancellation and business errors never flip.
# --------------------------------------------------------------------------- #

class _ScriptedClient:
    """Fake core client whose per-call outcome comes from ``script``."""

    has_lazy_reconnect = False

    def __init__(self, name: str = "demo") -> None:
        self._name = name
        if self.has_lazy_reconnect:
            self._lazy_reconnect = False
        self._session = object()
        self.script: list[object] = []
        self.connect_calls = 0
        self.disconnect_calls = 0

    async def connect(self, *args, **kwargs):
        self.connect_calls += 1
        return True

    async def disconnect(self, *args, **kwargs):
        self.disconnect_calls += 1
        self._session = None
        return True

    async def call_tool(self, *args, **kwargs):
        return self._next()

    async def list_tools(self, *args, **kwargs):
        return self._next()

    def _next(self):
        if not self.script:
            raise AssertionError("call script exhausted")
        step = self.script.pop(0)
        if isinstance(step, BaseException):
            raise step
        return step


class _FakePackage:
    """Stand-in for a marketplace package resolved on disk."""


def _install_scripted(monkeypatch):
    """Patch both transports with scripted fakes and apply the timeout patch."""

    class FakeStreamable(_ScriptedClient):
        has_lazy_reconnect = True

    class FakeSse(_ScriptedClient):
        has_lazy_reconnect = False

    class FakeToolMgr:
        @staticmethod
        def _create_client(config):
            return FakeSse()

    monkeypatch.setattr(streamable_module, "StreamableHttpClient", FakeStreamable)
    monkeypatch.setattr(sse_module, "SseClient", FakeSse)
    monkeypatch.setattr(tool_manager_module, "ToolMgr", FakeToolMgr)
    call_timeout_patch.apply_mcp_call_timeout_patch()
    return FakeStreamable, FakeSse


@pytest.fixture()
def workspace(tmp_path: Path):
    with patch("jiuwenswarm.server.runtime.mcp.state_store.get_workspace_dir", return_value=tmp_path):
        yield tmp_path


@pytest.fixture()
def published():
    """Capture publish calls without touching a websocket."""
    calls: list[str] = []

    async def _publish(session_id: str) -> bool:
        calls.append(session_id)
        return True

    with patch.object(connection_events, "publish_connection_changed", _publish):
        yield calls


@pytest.fixture()
def run_context():
    token = CURRENT_RUN_CONTEXT.set(RunContext(request_id="req-1", session_id="sess-1"))
    try:
        yield "sess-1"
    finally:
        CURRENT_RUN_CONTEXT.reset(token)


def _seed_connected(name: str, *, state: str = "connected") -> None:
    ss.upsert_mcp_record(
        name,
        {"name": name, "transport": "sse", "url": f"https://example.invalid/{name}/sse"},
        state=state,
        integration_type="remote-mcp",
    )


@pytest.mark.asyncio
async def test_sse_timeout_tears_down_and_persists_disconnect(
    monkeypatch, workspace, published, run_context
) -> None:
    _, fake_sse = _install_scripted(monkeypatch)
    _seed_connected("market-mcp")
    client = fake_sse("market-mcp")
    client.script = [TimeoutError()]

    with patch("jiuwenswarm.server.runtime.mcp.registry._resolve_package", return_value=_FakePackage()):
        with pytest.raises(TimeoutError):
            await client.call_tool("echo", {})

    # No lazy reconnect exists, so the patch disconnects for good.
    assert client.disconnect_calls == 1
    assert ss.get_mcp_record("market-mcp")["state"] == "disconnected"
    assert published == ["sess-1"]


@pytest.mark.asyncio
async def test_sse_timeout_custom_mcp_goes_back_to_registered(
    monkeypatch, workspace, published, run_context
) -> None:
    _, fake_sse = _install_scripted(monkeypatch)
    _seed_connected("my-custom")
    client = fake_sse("my-custom")
    client.script = [TimeoutError()]

    with patch("jiuwenswarm.server.runtime.mcp.registry._resolve_package", return_value=None):
        with pytest.raises(TimeoutError):
            await client.call_tool("echo", {})

    assert ss.get_mcp_record("my-custom")["state"] == "registered"
    assert published == ["sess-1"]


@pytest.mark.asyncio
async def test_streamable_first_timeout_only_marks_the_session_lost(
    monkeypatch, workspace, published, run_context
) -> None:
    """A slow-but-alive server looks exactly like this: never flip on one timeout."""
    fake_streamable, _ = _install_scripted(monkeypatch)
    _seed_connected("market-mcp")
    client = fake_streamable("market-mcp")
    client.script = [TimeoutError()]

    with pytest.raises(TimeoutError):
        await client.call_tool("echo", {})

    assert client._session is None
    assert client._jws_session_lost is True
    assert ss.get_mcp_record("market-mcp")["state"] == "connected"
    assert published == []


@pytest.mark.asyncio
async def test_streamable_recovery_clears_the_mark_and_keeps_state(
    monkeypatch, workspace, published, run_context
) -> None:
    """Lazy reconnect succeeded: the MCP was only slow, so nothing is persisted."""
    fake_streamable, _ = _install_scripted(monkeypatch)
    _seed_connected("market-mcp")
    client = fake_streamable("market-mcp")
    client.script = [TimeoutError(), "ok"]

    with pytest.raises(TimeoutError):
        await client.call_tool("echo", {})
    assert await client.call_tool("echo", {}) == "ok"

    assert client._jws_session_lost is False
    assert ss.get_mcp_record("market-mcp")["state"] == "connected"
    assert published == []


@pytest.mark.asyncio
async def test_streamable_abandoned_reconnect_persists_disconnect(
    monkeypatch, workspace, published, run_context
) -> None:
    """Second call ended on the "Not connected" guard: the transport is gone."""
    fake_streamable, _ = _install_scripted(monkeypatch)
    _seed_connected("market-mcp")
    client = fake_streamable("market-mcp")
    client.script = [
        TimeoutError(),
        RuntimeError("Not connected to streamable-http server"),
    ]

    with pytest.raises(TimeoutError):
        await client.call_tool("echo", {})
    with patch("jiuwenswarm.server.runtime.mcp.registry._resolve_package", return_value=_FakePackage()):
        with pytest.raises(RuntimeError):
            await client.call_tool("echo", {})

    assert ss.get_mcp_record("market-mcp")["state"] == "disconnected"
    assert published == ["sess-1"]


@pytest.mark.asyncio
async def test_streamable_cancellation_is_not_a_lost_connection(
    monkeypatch, workspace, published, run_context
) -> None:
    """The user pressed stop: it must not detach the MCP from the session."""
    fake_streamable, _ = _install_scripted(monkeypatch)
    _seed_connected("market-mcp")
    client = fake_streamable("market-mcp")
    client.script = [TimeoutError(), asyncio.CancelledError()]

    with pytest.raises(TimeoutError):
        await client.call_tool("echo", {})
    with pytest.raises(asyncio.CancelledError):
        await client.call_tool("echo", {})

    assert ss.get_mcp_record("market-mcp")["state"] == "connected"
    assert published == []


@pytest.mark.asyncio
async def test_unnamed_client_never_flips_or_publishes(
    monkeypatch, workspace, published, run_context
) -> None:
    """Without a client name the failure cannot be attributed to an MCP."""
    _, fake_sse = _install_scripted(monkeypatch)
    _seed_connected("market-mcp")
    client = fake_sse("")
    client.script = [TimeoutError()]

    with pytest.raises(TimeoutError):
        await client.call_tool("echo", {})

    assert client.disconnect_calls == 1
    assert ss.get_mcp_record("market-mcp")["state"] == "connected"
    assert published == []


@pytest.mark.asyncio
async def test_publish_skips_without_session_id() -> None:
    """No session id means the gateway would drop the frame: don't send it."""
    assert await connection_events.publish_connection_changed("") is False


@pytest.mark.asyncio
async def test_publish_skips_without_agent_server() -> None:
    """A worker process has no agent server; the persisted state stands alone."""
    with patch.object(connection_events, "_server_instance", return_value=None):
        assert await connection_events.publish_connection_changed("sess-1") is False


@pytest.mark.asyncio
async def test_state_persists_even_when_push_is_skipped(
    monkeypatch, workspace, run_context
) -> None:
    """Notification is best-effort: the file still records the lost connection."""
    _, fake_sse = _install_scripted(monkeypatch)
    _seed_connected("market-mcp")
    client = fake_sse("market-mcp")
    client.script = [TimeoutError()]

    with patch.object(connection_events, "_server_instance", return_value=None), \
         patch("jiuwenswarm.server.runtime.mcp.registry._resolve_package", return_value=_FakePackage()):
        with pytest.raises(TimeoutError):
            await client.call_tool("echo", {})

    assert ss.get_mcp_record("market-mcp")["state"] == "disconnected"


@pytest.mark.asyncio
async def test_publish_frame_is_session_routed() -> None:
    """The frame must carry a session_id: WebChannel drops session-less pushes."""
    sent: list[dict] = []

    class _Transport:
        async def send_push(self, msg):
            sent.append(msg)
            return True

    with patch.object(connection_events, "_server_instance", return_value=object()), \
         patch(
             "jiuwenswarm.server.gateway_push.transport.WebSocketGatewayPushTransport",
             _Transport,
         ):
        assert await connection_events.publish_connection_changed("sess-1") is True

    assert len(sent) == 1
    frame = sent[0]
    assert frame["channel_id"] == "web"
    assert frame["session_id"] == "sess-1"
    assert frame["payload"]["event_type"] == connection_events.MCP_CONNECTION_CHANGED_EVENT
    assert frame["is_complete"] is False


@pytest.mark.asyncio
async def test_probe_client_timeout_never_flips_state(
    monkeypatch, workspace, published, run_context
) -> None:
    """mcp.show/mcp.list list tools through a throwaway client.

    Its timeout says nothing about the session's registered client, so it must
    not detach a healthy MCP (opening a detail page would otherwise grey it).
    """
    from jiuwenswarm.common.mcp_config import TEMP_PROBE_CLIENT_ATTR

    _, fake_sse = _install_scripted(monkeypatch)
    _seed_connected("market-mcp")
    client = fake_sse("market-mcp")
    setattr(client, TEMP_PROBE_CLIENT_ATTR, True)
    client.script = [TimeoutError()]

    with pytest.raises(TimeoutError):
        await client.call_tool("echo", {})

    assert client.disconnect_calls == 1
    assert ss.get_mcp_record("market-mcp")["state"] == "connected"
    assert published == []

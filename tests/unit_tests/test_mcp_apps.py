# coding: utf-8
"""Tests for the MCP Apps host bridge (jiuwenswarm.server.runtime.mcp.apps)."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from mcp import types as mcp_types

from jiuwenswarm.server.runtime.mcp import apps

UI_URI = "ui://picker/app.html"


class _FakeSession:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict | None]] = []

    async def list_tools(self):
        return mcp_types.ListToolsResult(tools=[
            mcp_types.Tool(
                name="browse",
                inputSchema={"type": "object"},
                _meta={"ui": {"resourceUri": UI_URI}},
            ),
            mcp_types.Tool(
                name="secret",
                inputSchema={"type": "object"},
                _meta={"ui": {"resourceUri": UI_URI, "visibility": ["model"]}},
            ),
        ])

    async def read_resource(self, uri):
        return mcp_types.ReadResourceResult(contents=[
            mcp_types.TextResourceContents(
                uri=uri,
                mimeType=apps.MCP_APP_MIME_TYPE,
                text="<html></html>",
                _meta={"ui": {"csp": {"connectDomains": ["https://x.test"]}}},
            ),
        ])

    async def call_tool(self, name, arguments=None):
        self.calls.append((name, arguments))
        return mcp_types.CallToolResult(
            content=[mcp_types.TextContent(type="text", text="ok")],
            structuredContent={"products": [1, 2]},
            _meta={"k": "v"},
        )


@pytest.fixture
def session(monkeypatch):
    fake = _FakeSession()
    monkeypatch.setattr(apps, "_live_client", lambda name: SimpleNamespace(_session=fake, _name=name))
    return fake


@pytest.mark.asyncio
async def test_list_tools_keeps_ui_meta(session):
    tools = await apps.list_app_tools("store")
    assert tools[0]["_meta"]["ui"]["resourceUri"] == UI_URI


@pytest.mark.asyncio
async def test_read_resource_keeps_mime_and_csp(session):
    result = await apps.read_app_resource("store", UI_URI)
    content = result["contents"][0]
    assert content["mimeType"] == apps.MCP_APP_MIME_TYPE
    assert content["_meta"]["ui"]["csp"]["connectDomains"] == ["https://x.test"]


@pytest.mark.asyncio
async def test_read_resource_rejects_non_ui_uri(session):
    with pytest.raises(ValueError):
        await apps.read_app_resource("store", "file:///etc/passwd")


@pytest.mark.asyncio
async def test_call_tool_keeps_structured_content(session):
    result = await apps.call_app_tool("store", "browse", {"q": 1})
    assert result["structuredContent"] == {"products": [1, 2]}
    assert result["_meta"] == {"k": "v"}
    assert session.calls == [("browse", {"q": 1})]


@pytest.mark.asyncio
async def test_call_tool_rejects_model_only_tool(session):
    with pytest.raises(PermissionError):
        await apps.call_app_tool("store", "secret", {})
    assert session.calls == []


@pytest.mark.asyncio
async def test_call_tool_unknown_tool(session):
    with pytest.raises(KeyError):
        await apps.call_app_tool("store", "nope", {})


@pytest.fixture
def patched_mcp_tool(monkeypatch):
    """Apply the invoke patch for one test, restoring openjiuwen afterwards."""
    import importlib

    from openjiuwen.core.foundation.tool.mcp import base as mcp_base

    monkeypatch.setattr(apps, "_PATCHED", False)
    monkeypatch.setattr(apps, "_ui_resource_cache", {})
    monkeypatch.setattr(mcp_base.MCPTool, "invoke", mcp_base.MCPTool.invoke)
    for name in ("stdio_client", "sse_client", "streamable_http_client"):
        module = importlib.import_module(f"openjiuwen.core.foundation.tool.mcp.client.{name}")
        monkeypatch.setattr(module, "extract_mcp_tool_result_content",
                            module.extract_mcp_tool_result_content)
    apps.apply_mcp_apps_patch()
    return mcp_base


class _FakeClient:
    """Stand-in for an openjiuwen streamable-http client."""

    def __init__(self, raw) -> None:
        self._name = "store"
        self._session = _FakeSession()
        self._raw = raw

    async def call_tool(self, tool_name, arguments, **kwargs):
        from openjiuwen.core.foundation.tool.mcp.client import streamable_http_client

        # Same call the real client makes; the patch records the raw result.
        return streamable_http_client.extract_mcp_tool_result_content(self._raw, tool_name=tool_name)


def _real_tool(mcp_base, client, name):
    # Construct through openjiuwen's _ToolMeta, which binds invoke per
    # instance: this is the path the agent uses (regression: a class-level
    # patch applied after construction was silently bypassed).
    schema = {"type": "object", "properties": {"q": {"type": "integer"}}}
    card = mcp_base.McpToolCard(name=name, server_name="store", description="", input_params=schema)
    return mcp_base.MCPTool(mcp_client=client, tool_info=card)


@pytest.mark.asyncio
async def test_invoke_patch_attaches_mcp_app_block(patched_mcp_tool):
    raw = mcp_types.CallToolResult(
        content=[mcp_types.TextContent(type="text", text="grid shown")],
        structuredContent={"products": ["a"]},
    )
    tool = _real_tool(patched_mcp_tool, _FakeClient(raw), "browse")

    result = await tool.invoke({"q": 1})

    assert result["result"] == "grid shown"
    block = result["mcp_app"]
    assert block["server"] == "store" and block["resourceUri"] == UI_URI
    assert block["tool"] == "browse"
    assert block["toolResult"]["structuredContent"] == {"products": ["a"]}
    assert tool.render_for_llm(result) == "grid shown"


@pytest.mark.asyncio
async def test_invoke_patch_skips_tools_without_ui(patched_mcp_tool):
    raw = mcp_types.CallToolResult(content=[mcp_types.TextContent(type="text", text="ok")])
    tool = _real_tool(patched_mcp_tool, _FakeClient(raw), "not-a-ui-tool")

    assert "mcp_app" not in await tool.invoke({})


def test_agent_server_applies_patch_on_construction(monkeypatch):
    from jiuwenswarm.server import agent_ws_server

    calls = []
    monkeypatch.setattr(apps, "apply_mcp_apps_patch", lambda: calls.append(True))
    agent_ws_server.AgentWebSocketServer()
    assert calls == [True]


@pytest.mark.parametrize(
    ("url", "opened"),
    [
        ("https://shop.test/checkout/abc", True),
        ("http://localhost:3000/pay", True),
        ("javascript:alert(1)", False),
        ("file:///etc/passwd", False),
        ("https://user:pass@shop.test/", False),
        ("not a url", False),
    ],
)
def test_desktop_open_app_link_only_opens_plain_http(monkeypatch, url, opened):
    desktop_app = pytest.importorskip("jiuwenswarm.channels.desktop.desktop_app")
    calls = []
    monkeypatch.setattr(desktop_app.webbrowser, "open", lambda target: calls.append(target) or True)
    assert desktop_app._WindowApi.open_app_link(url) is opened
    assert bool(calls) is opened


def test_history_wire_keeps_deep_mcp_app_data_intact():
    from jiuwenswarm.server import wire_truncate

    deep = {"a": {"b": {"c": {"d": {"e": {"f": {"g": 24.5}}}}}}}
    record = {
        "event_type": "chat.tool_result",
        "tool_call_id": "call_1",
        "raw_output": {
            "result": "ok",
            "mcp_app": {"server": "s", "tool": "t", "resourceUri": UI_URI,
                        "toolResult": {"structuredContent": deep, "items": list(range(150))}},
        },
    }
    wire = wire_truncate._sanitize_history_record_for_wire(record)
    tool_result = wire["raw_output"]["mcp_app"]["toolResult"]
    assert tool_result["structuredContent"] == deep  # no "<truncated>" at depth > 8
    assert len(tool_result["items"]) == 150  # no 100-item list cap


def test_history_wire_oversized_mcp_app_record_still_collapses():
    from jiuwenswarm.server import wire_truncate

    record = {
        "event_type": "chat.tool_result",
        "tool_call_id": "call_1",
        "raw_output": {"mcp_app": {"toolResult": {"blob": "x" * (70 * 1024)}}},
    }
    wire = wire_truncate._sanitize_history_record_for_wire(record)
    assert wire.get("truncated") is True
    assert "raw_output" not in wire


@pytest.mark.asyncio
async def test_raw_session_calls_reconnect_after_transport_error(monkeypatch):
    import anyio
    from openjiuwen.core.foundation.tool.mcp.client import reconnect as reconnect_mod

    dead, fresh = _FakeSession(), _FakeSession()

    async def broken_list_tools():
        raise anyio.ClosedResourceError()

    dead.list_tools = broken_list_tools
    client = SimpleNamespace(_session=dead, _name="store")
    monkeypatch.setattr(apps, "_live_client", lambda name: client)

    async def fake_reconnect(target, timeout=-1):
        target._session = fresh
        return True

    monkeypatch.setattr(reconnect_mod, "reconnect", fake_reconnect)
    tools = await apps.list_app_tools("store")
    assert tools[0]["name"] == "browse"
    assert client._session is fresh


@pytest.mark.asyncio
async def test_ui_resource_cache_expires_and_clears_on_reconnect(monkeypatch):
    from openjiuwen.core.foundation.tool.mcp.client import reconnect as reconnect_mod

    monkeypatch.setattr(apps, "_ui_resource_cache", {})
    client = SimpleNamespace(_session=_FakeSession(), _name="store")
    assert (await apps._ui_resource_uris(client)) == {"browse": UI_URI, "secret": UI_URI}

    # Server changes its tools: the cached map is served until the TTL passes.
    class _Renamed(_FakeSession):
        async def list_tools(self):
            result = await super().list_tools()
            result.tools[0].name = "browse-v2"
            return result

    client._session = _Renamed()
    assert "browse" in await apps._ui_resource_uris(client)
    clock = [apps.time.monotonic()]
    monkeypatch.setattr(apps.time, "monotonic", lambda: clock[0])
    clock[0] += apps._UI_RESOURCE_CACHE_TTL_S + 1
    assert "browse-v2" in await apps._ui_resource_uris(client)

    # A reconnect drops the server's entry immediately.
    async def fake_reconnect(target, timeout=-1):
        return True

    monkeypatch.setattr(reconnect_mod, "reconnect", fake_reconnect)
    calls = {"n": 0}

    async def flaky(session):
        calls["n"] += 1
        if calls["n"] == 1:
            import anyio
            raise anyio.ClosedResourceError()
        return "ok"

    assert await apps._with_session(client, flaky) == "ok"
    assert "store" not in apps._ui_resource_cache

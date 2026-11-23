# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for the ``before_mcp_http`` MCP HTTP auth hook."""

from __future__ import annotations

import inspect
import json
from types import SimpleNamespace

import pytest

from jiuwenswarm.common import mcp_http_hook as hook
from jiuwenswarm.extensions import hook_event, hooks_context
from jiuwenswarm.extensions.registry import ExtensionRegistry


class _FakeRegistry:
    def __init__(self, handler, *, subscribers: bool | None = None):
        self._handler = handler
        self._subscribers = subscribers
        self.events: list[str] = []
        if subscribers is not None:
            self.callback_framework = SimpleNamespace(
                has_subscribers=lambda _event: subscribers
            )

    async def trigger(self, event, context=None, **kwargs):
        self.events.append(event)
        result = self._handler(context)
        if inspect.isawaitable(result):
            await result


def _request(method: str = "tools/call", *, http_method: str = "POST"):
    """Build an httpx.Request carrying a JSON-RPC body for the given method."""

    httpx = pytest.importorskip("httpx")
    if http_method == "GET":
        return httpx.Request("GET", "https://gw.example.com/mcp")
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method}).encode()
    return httpx.Request(
        http_method, "https://gw.example.com/mcp", content=body,
        headers={"Content-Type": "application/json"},
    )


@pytest.fixture
def registry(monkeypatch):
    """Install a fake ExtensionRegistry with a controllable handler."""

    def _install(handler, *, subscribers=None):
        instance = _FakeRegistry(handler, subscribers=subscribers)
        monkeypatch.setattr(
            ExtensionRegistry, "get_instance", classmethod(lambda cls: instance)
        )
        return instance

    yield _install
    try:
        ExtensionRegistry.get_instance()
    except RuntimeError:
        pass


@pytest.mark.asyncio
async def test_event_and_context_are_wired():
    assert hook_event.AgentServerHookEvents.BEFORE_MCP_HTTP == "agent_server:before_mcp_http"
    ctx = hooks_context.McpHttpHookContext(server_name="gw", method="tools/call")
    assert ctx.headers == {}
    assert ctx.to_dict()["method"] == "tools/call"


@pytest.mark.asyncio
async def test_extension_headers_are_returned(registry):
    async def handler(ctx):
        ctx.headers["Authorization"] = "Bearer per-request"

    registry(handler)
    assert await hook.apply_mcp_http_hook("gw", _request("tools/call")) == {
        "Authorization": "Bearer per-request"
    }


@pytest.mark.asyncio
async def test_protocol_headers_are_dropped(registry):
    async def handler(ctx):
        ctx.headers.update(
            {
                "Content-Type": "text/plain",
                "Accept": "text/html",
                "Mcp-Session-Id": "spoof",
                "Content-Encoding": "gzip",
                "MCP-Protocol-Version": "2024-11-05",
                "Authorization": "Bearer keep",
            }
        )

    registry(handler)
    assert await hook.apply_mcp_http_hook("gw", _request("tools/list")) == {
        "Authorization": "Bearer keep"
    }


def test_protected_headers_cover_message_framing():
    """Framing/negotiation headers must stay host-owned (review CR)."""

    for name in ("content-encoding", "content-length", "mcp-protocol-version",
                 "mcp-session-id", "transfer-encoding"):
        assert name in hook.PROTECTED_HEADERS


@pytest.mark.asyncio
async def test_bad_values_are_dropped(registry):
    async def handler(ctx):
        ctx.headers.update(
            {
                "Authorization": "Bearer a\r\nX-Evil: 1",
                "X-Nul": "a\0b",
                "X-Long": "x" * (hook._MAX_HEADER_VALUE_LEN + 1),
                "X-Ok": "v",
            }
        )

    registry(handler)
    assert await hook.apply_mcp_http_hook("gw", _request("initialize")) == {"X-Ok": "v"}


@pytest.mark.asyncio
async def test_invalid_header_name_is_dropped(registry):
    async def handler(ctx):
        ctx.headers["Bad Name"] = "v"

    registry(handler)
    assert await hook.apply_mcp_http_hook("gw", _request("tools/call")) == {}


@pytest.mark.asyncio
async def test_non_string_values_are_dropped(registry):
    async def handler(ctx):
        ctx.headers["X-Int"] = 123  # type: ignore[assignment]

    registry(handler)
    assert await hook.apply_mcp_http_hook("gw", _request("tools/call")) == {}


@pytest.mark.asyncio
async def test_handler_failure_falls_back_to_no_extra_headers(registry):
    async def handler(ctx):
        raise RuntimeError("boom")

    registry(handler)
    assert await hook.apply_mcp_http_hook("gw", _request("tools/call")) == {}


@pytest.mark.asyncio
async def test_scope_is_passed_to_context(registry):
    seen = {}

    async def handler(ctx):
        seen["request_id"] = ctx.request_id
        seen["session_id"] = ctx.session_id

    registry(handler)
    token = hook.set_mcp_request_scope(("req-1", "sess-1"))
    try:
        await hook.apply_mcp_http_hook("gw", _request("tools/call"))
    finally:
        hook.reset_mcp_request_scope(token)
    assert seen == {"request_id": "req-1", "session_id": "sess-1"}


@pytest.mark.asyncio
async def test_scope_defaults_to_empty_strings(registry):
    seen = {}

    async def handler(ctx):
        seen["request_id"] = ctx.request_id

    registry(handler)
    await hook.apply_mcp_http_hook("gw", _request("tools/call"))
    assert seen["request_id"] == ""


def test_infer_method_reads_jsonrpc_body():
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call"}).encode()

    class _Req:
        method = "POST"
        content = body

    assert hook._infer_jsonrpc_method(_Req()) == "tools/call"


@pytest.mark.asyncio
async def test_no_subscriber_skips_body_parsing(registry, monkeypatch):
    """Without a subscriber the hook must not even inspect the request body."""

    def _boom(_request):
        raise AssertionError("body must not be parsed when nobody subscribed")

    monkeypatch.setattr(hook, "_infer_jsonrpc_method", _boom)
    registry(lambda ctx: None, subscribers=False)

    assert await hook.apply_mcp_http_hook("gw", _request("tools/call")) == {}


@pytest.mark.asyncio
async def test_non_latin1_value_is_dropped(registry):
    """A header value outside latin-1 would break httpx encoding; drop it."""

    async def handler(ctx):
        ctx.headers.update({"X-Bad": "值", "X-Ok": "ascii"})

    registry(handler)
    assert await hook.apply_mcp_http_hook("gw", _request("tools/call")) == {"X-Ok": "ascii"}


def test_infer_method_for_sse_stream_is_stream():
    class _Req:
        method = "GET"
        content = b""

    assert hook._infer_jsonrpc_method(_Req()) == "stream"


def test_infer_method_falls_back_to_http_verb():
    class _Req:
        method = "POST"
        content = b"not json"

    assert hook._infer_jsonrpc_method(_Req()) == "post"


@pytest.mark.asyncio
async def test_patch_wraps_provider_and_applies_headers(registry):
    """The patched auth flow must merge extension headers into the request."""

    httpx = pytest.importorskip("httpx")
    from openjiuwen.core.foundation.tool.auth import auth_callback

    async def handler(ctx):
        ctx.headers["X-Per-Request"] = "1"

    registry(handler)
    hook.apply_mcp_http_auth_patch()
    assert hook._PATCHED is True

    provider = auth_callback.AuthHeaderAndQueryProvider(
        auth_headers={"Authorization": "Bearer static"}, auth_query_params={}
    )
    request = httpx.Request("POST", "https://gw.example.com/mcp")
    gen = provider.async_auth_flow(request)
    try:
        signed = await gen.__anext__()
    finally:
        await gen.aclose()

    assert signed.headers["Authorization"] == "Bearer static"
    assert signed.headers["X-Per-Request"] == "1"


@pytest.mark.asyncio
async def test_patch_disabled_when_sdk_surface_incomplete(monkeypatch):
    """Half an SDK surface must disable the patch instead of half-installing it."""

    from openjiuwen.core.foundation.tool.auth import auth_callback

    saved_patched = hook._PATCHED
    try:
        hook._PATCHED = False
        monkeypatch.setattr(auth_callback, "HeaderQueryAuthStrategy", None)
        hook.apply_mcp_http_auth_patch()
        assert hook._PATCHED is False
    finally:
        hook._PATCHED = saved_patched


@pytest.mark.asyncio
async def test_patch_forwards_response_into_wrapped_flow(registry):
    """The proxy must feed the httpx response back with ``asend``, not ``None``."""

    from openjiuwen.core.foundation.tool.auth import auth_callback

    httpx = pytest.importorskip("httpx")
    seen: dict[str, object] = {}

    async def two_step(self, request):
        response = yield request
        seen["response"] = response
        yield request

    provider_cls = auth_callback.AuthHeaderAndQueryProvider
    saved_flow = provider_cls.async_auth_flow
    saved_patched = hook._PATCHED
    try:
        provider_cls.async_auth_flow = two_step
        hook._PATCHED = False
        registry(lambda ctx: None)
        hook.apply_mcp_http_auth_patch()

        provider = provider_cls(auth_headers={}, auth_query_params={})
        request = httpx.Request("POST", "https://gw.example.com/mcp")
        response = httpx.Response(200, request=request)
        gen = provider.async_auth_flow(request)
        try:
            first = await gen.__anext__()
            assert first is request
            # httpx sends the response back into the flow here.
            second = await gen.asend(response)
            assert second is request
        finally:
            await gen.aclose()
    finally:
        provider_cls.async_auth_flow = saved_flow
        hook._PATCHED = saved_patched

    assert seen["response"] is response


@pytest.mark.asyncio
async def test_transport_rejected_header_does_not_break_the_call(registry, monkeypatch):
    """A value the transport rejects must be skipped, not fail the MCP call.

    ``_sanitize_headers`` normally filters such values already; the merge-time
    guard covers anything that slips through (e.g. a future SDK encoding).
    """

    from openjiuwen.core.foundation.tool.auth import auth_callback

    httpx = pytest.importorskip("httpx")

    async def handler(ctx):
        ctx.headers["X-Lone-Surrogate"] = "\ud800"
        ctx.headers["X-Ok"] = "1"

    registry(handler)
    # Let the bad value through sanitization to exercise the merge guard.
    monkeypatch.setattr(hook, "_sanitize_headers", lambda raw: dict(raw))
    hook.apply_mcp_http_auth_patch()

    provider = auth_callback.AuthHeaderAndQueryProvider(auth_headers={}, auth_query_params={})
    request = httpx.Request("POST", "https://gw.example.com/mcp")
    gen = provider.async_auth_flow(request)
    try:
        signed = await gen.__anext__()
    finally:
        await gen.aclose()

    assert signed.headers.get("X-Ok") == "1"
    assert "X-Lone-Surrogate" not in signed.headers


@pytest.mark.asyncio
async def test_patch_is_idempotent(registry):
    async def handler(ctx):
        return None

    registry(handler)
    hook.apply_mcp_http_auth_patch()
    from openjiuwen.core.foundation.tool.auth import auth_callback

    patched = auth_callback.AuthHeaderAndQueryProvider.async_auth_flow
    hook.apply_mcp_http_auth_patch()
    assert auth_callback.AuthHeaderAndQueryProvider.async_auth_flow is patched


@pytest.mark.asyncio
async def test_tool_invocation_binds_scope_without_changing_transport_signature(
    monkeypatch,
):
    """Regression: the transport keeps ``call_tool(name, arguments)`` only.

    The request scope must reach the transport through the ContextVar, not as a
    new keyword argument (a kwarg broke third-party/test transports).
    """

    from types import SimpleNamespace

    from openjiuwen.core.foundation.tool import ToolCard

    from jiuwenswarm.common import mcp_config

    seen: dict[str, object] = {}

    async def call_tool(name, *, arguments):  # mirrors mcp.ClientSession
        seen["name"] = name
        seen["scope"] = hook.current_mcp_request_scope()
        return SimpleNamespace(content=[SimpleNamespace(text="ok")], isError=False)

    card = ToolCard(
        id="mcp-registry-request-abc.connector.connector_lookup",
        name="connector_lookup",
        description="Lookup",
    )
    tool = mcp_config.RequestScopedOfficeClawMcpTool(
        card=card,
        params={"server_name": "connector"},
        request_id="req-scope",
        server_name="connector",
        remote_tool_name="connector_lookup",
    )
    monkeypatch.setattr(
        tool, "_acquire_mcp_session",
        _async_return(SimpleNamespace(call_tool=call_tool)),
    )
    monkeypatch.setattr(mcp_config, "ensure_request_scoped_mcp_tool_allowed", lambda _tid: None)
    monkeypatch.setattr(
        mcp_config, "ensure_office_claw_tool_invocation_matches_active", lambda _tool: None
    )

    result = await tool.invoke({})

    assert result == {"result": "ok"}
    assert seen["name"] == "connector_lookup"
    assert seen["scope"] == ("req-scope", "")
    # The binding is scoped: nothing leaks after the call.
    assert hook.current_mcp_request_scope() is None


def _async_return(value):
    async def _call(*args, **kwargs):
        return value

    return _call


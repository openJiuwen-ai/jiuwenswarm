# coding: utf-8
"""MCP Apps (``io.modelcontextprotocol/ui``) host-side helpers.

MCP Apps let a tool declare an interactive UI via ``_meta.ui.resourceUri``
(a ``ui://`` resource with mimeType ``text/html;profile=mcp-app``). The web
frontend renders that HTML in a sandboxed iframe and the app talks back to
the host over postMessage; the host forwards a subset of those requests to
the originating MCP server through the RPCs backed by this module:

  - ``mcp_app.list_tools``    raw ``tools/list`` incl. ``_meta`` (UI links)
  - ``mcp_app.read_resource`` raw ``resources/read`` for a ``ui://`` URI
  - ``mcp_app.call_tool``     raw ``tools/call`` incl. ``structuredContent``

Why raw ``ClientSession`` calls: openjiuwen's MCP clients flatten results
(``list_tools`` keeps only name/description/schema; ``call_tool`` keeps only
extracted content), dropping ``_meta`` and ``structuredContent`` that MCP Apps
depend on. The live connection is shared with the agent via the process-wide
``Runner.resource_mgr`` cache, so no second connection is opened.
"""
from __future__ import annotations

import logging
from contextvars import ContextVar
from typing import Any, Awaitable, Callable, TypeVar

logger = logging.getLogger(__name__)
T = TypeVar("T")

__all__ = [
    "MCP_APP_MIME_TYPE",
    "list_app_tools",
    "read_app_resource",
    "call_app_tool",
    "apply_mcp_apps_patch",
]

MCP_APP_MIME_TYPE = "text/html;profile=mcp-app"


def _live_client(name: str) -> Any | None:
    """Return the connected openjiuwen MCP client for ``name``, or ``None``."""
    from openjiuwen.core.runner import Runner

    resource_registry = getattr(Runner.resource_mgr, "_resource_registry", None)
    if resource_registry is None:
        return None
    tool_mgr = resource_registry.tool()
    server_ids = list(tool_mgr.get_mcp_server_ids(name))
    if not server_ids:
        # Same fallback as registry.get_mcp_tools: match by server_name.
        for sid, res in getattr(tool_mgr, "_mcp_server_resources", {}).items():
            if getattr(res.config, "server_name", "") == name:
                server_ids.append(sid)
    for sid in server_ids:
        client = tool_mgr.get_mcp_client(sid)
        if client is not None and getattr(client, "_session", None) is not None:
            return client
    return None


async def _client_for(name: str) -> Any:
    """Return the connected MCP client for server ``name``, connecting if needed.

    Raises ``KeyError`` when the server is unknown or cannot be connected.
    """
    n = str(name or "").strip()
    if not n:
        raise ValueError("mcp server name is required")
    client = _live_client(n)
    if client is None:
        from jiuwenswarm.common.mcp_config import probe_mcp_live_connection

        ok, reason = await probe_mcp_live_connection(n)
        client = _live_client(n) if ok else None
        if client is None:
            raise KeyError(f"mcp '{n}' is not connected: {reason or 'no live session'}")
    return client


async def _with_session(client: Any, operation: Callable[[Any], Awaitable[T]]) -> T:
    """Run ``operation(session)``, reconnecting once on a transport error.

    These calls use the raw ``ClientSession`` (to keep ``_meta`` and
    ``structuredContent``) and so bypass openjiuwen's ``@with_reconnect``.
    A dead stdio subprocess or dropped HTTP session would otherwise fail
    every call until restart; reuse openjiuwen's reconnect instead.
    """
    from openjiuwen.core.foundation.tool.mcp.client.reconnect import (
        is_retryable_transport_error,
        reconnect,
    )

    try:
        return await operation(client._session)
    except Exception as exc:  # noqa: BLE001 - classified below
        if not is_retryable_transport_error(exc):
            raise
        logger.warning("[mcp.apps] transport error on %s, reconnecting: %r", getattr(client, "_name", "?"), exc)
        if not await reconnect(client):
            raise
        return await operation(client._session)


def _dump(model: Any) -> Any:
    """Serialize an MCP SDK pydantic model preserving wire names (``_meta``)."""
    if hasattr(model, "model_dump"):
        return model.model_dump(mode="json", by_alias=True, exclude_none=True)
    return model


def _tool_visibility(tool: dict[str, Any]) -> list[str]:
    ui = (tool.get("_meta") or {}).get("ui") or {}
    visibility = ui.get("visibility")
    return list(visibility) if isinstance(visibility, list) else ["model", "app"]


async def list_app_tools(name: str) -> list[dict[str, Any]]:
    """Return the server's raw tool list (name, schema, annotations, ``_meta``)."""
    client = await _client_for(name)
    result = await _with_session(client, lambda session: session.list_tools())
    return [_dump(tool) for tool in result.tools]


async def read_app_resource(name: str, uri: str) -> dict[str, Any]:
    """Read a ``ui://`` resource; returns the raw ``ReadResourceResult``."""
    u = str(uri or "").strip()
    if not u.startswith("ui://"):
        raise ValueError("only ui:// resources can be read by MCP Apps")
    client = await _client_for(name)
    return _dump(await _with_session(client, lambda session: session.read_resource(u)))


async def call_app_tool(name: str, tool: str, arguments: dict[str, Any] | None) -> dict[str, Any]:
    """Call ``tool`` on server ``name`` on behalf of an MCP App.

    Tools whose ``_meta.ui.visibility`` excludes ``"app"`` are model-only and
    must not be callable from an app. Returns the raw ``CallToolResult``.
    """
    t = str(tool or "").strip()
    if not t:
        raise ValueError("tool name is required")
    if arguments is not None and not isinstance(arguments, dict):
        raise ValueError("arguments must be an object")
    tools = {item.get("name"): item for item in await list_app_tools(name)}
    spec = tools.get(t)
    if spec is None:
        raise KeyError(f"tool '{t}' not found on mcp '{name}'")
    if "app" not in _tool_visibility(spec):
        raise PermissionError(f"tool '{t}' is not visible to apps")
    client = await _client_for(name)
    logger.info("[mcp.apps] app-initiated tools/call server=%s tool=%s", name, t)
    return _dump(await _with_session(client, lambda session: session.call_tool(t, arguments=arguments or {})))


# --- Agent tool-call path: surface the UI link + raw result to the chat -----
#
# The agent calls MCP tools through openjiuwen's ``MCPTool.invoke``, whose
# result reaches the web UI as ``chat.tool_result.raw_output``. We add an
# ``mcp_app`` block to that dict for tools that declare a UI, so the frontend
# can mount the app with the tool input/result. ``render_for_llm`` reads only
# ``output["result"]``, so the model-facing text is unchanged.

_raw_call_result: ContextVar[Any] = ContextVar("jws_mcp_raw_call_result", default=None)
# server name -> {tool name -> ui resourceUri}; filled lazily per server.
_ui_resource_cache: dict[str, dict[str, str]] = {}
_PATCHED = False


def _resource_uri_of(tool: dict[str, Any]) -> str | None:
    meta = tool.get("_meta") or {}
    ui = meta.get("ui") or {}
    uri = ui.get("resourceUri") or meta.get("ui/resourceUri")
    return uri if isinstance(uri, str) and uri.startswith("ui://") else None


async def _ui_resource_uris(client: Any) -> dict[str, str]:
    name = str(getattr(client, "_name", "") or "")
    cached = _ui_resource_cache.get(name)
    if cached is not None:
        return cached
    if getattr(client, "_session", None) is None:
        return {}
    result = await _with_session(client, lambda session: session.list_tools())
    uris = {}
    for tool in result.tools:
        uri = _resource_uri_of(_dump(tool))
        if uri:
            uris[tool.name] = uri
    _ui_resource_cache[name] = uris
    return uris


def build_mcp_app_block(
    server: str, tool: str, resource_uri: str, arguments: Any, raw: Any
) -> dict[str, Any]:
    """Return the ``mcp_app`` block attached to an MCP tool's invoke result."""
    block: dict[str, Any] = {
        "server": server,
        "tool": tool,
        "resourceUri": resource_uri,
        "arguments": arguments if isinstance(arguments, dict) else {},
    }
    if raw is not None:
        # Full CallToolResult as the app expects it (ui/notifications/tool-result).
        block["toolResult"] = _dump(raw)
    return block


def apply_mcp_apps_patch() -> None:
    """Capture raw MCP results and tag UI-linked tool results. Idempotent.

    Must run before any ``MCPTool`` is constructed: openjiuwen's ``_ToolMeta``
    copies ``invoke`` onto each instance at construction, so tools created
    earlier keep the unpatched method. The agent server applies it in its
    constructor, before any request can register an MCP server.
    """
    global _PATCHED
    if _PATCHED:
        return
    _PATCHED = True

    import importlib

    from openjiuwen.core.foundation.tool.mcp import base as mcp_base

    original_extract = mcp_base.extract_mcp_tool_result_content

    def extract_and_record(tool_result: Any, *args: Any, **kwargs: Any) -> Any:
        _raw_call_result.set(tool_result)
        return original_extract(tool_result, *args, **kwargs)

    # Each client imported the function by name, so patch it where it is used.
    for module_name in ("stdio_client", "sse_client", "streamable_http_client"):
        module = importlib.import_module(
            f"openjiuwen.core.foundation.tool.mcp.client.{module_name}"
        )
        module.extract_mcp_tool_result_content = extract_and_record

    original_invoke = mcp_base.MCPTool.invoke

    async def invoke_with_app(self: Any, inputs: Any, **kwargs: Any) -> Any:
        _raw_call_result.set(None)
        result = await original_invoke(self, inputs, **kwargs)
        if not isinstance(result, dict):
            return result
        try:
            client = self._mcp_client
            tool = self._card.name
            resource_uri = (await _ui_resource_uris(client)).get(tool)
            if resource_uri:
                result["mcp_app"] = build_mcp_app_block(
                    str(getattr(client, "_name", "")),
                    tool,
                    resource_uri,
                    inputs,
                    _raw_call_result.get(),
                )
        except Exception:  # noqa: BLE001 - never fail a tool call over UI metadata
            logger.warning("[mcp.apps] attaching mcp_app block failed", exc_info=True)
        return result

    mcp_base.MCPTool.invoke = invoke_with_app
    logger.info("[mcp.apps] MCP Apps result patch applied")

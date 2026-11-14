# coding: utf-8
"""Inject a per-call timeout into openjiuwen's MCP HTTP clients.

Why this exists: ``MCPTool.invoke`` calls ``call_tool`` without a ``timeout``,
and the stock ``StreamableHttpClient.call_tool`` / ``list_tools`` await
``self._session.call_tool(...)`` / ``self._session.list_tools()`` with no
``asyncio.wait_for`` around them. When a remote streamable-http MCP server
process is killed mid-session, the underlying SSE read is governed by the MCP
SDK's ``sse_read_timeout`` (default 300s), so the call hangs for minutes —
neither failing nor timing out, which surfaces as an forever-spinning TUI
spinner and the agent repeatedly retrying the tool.

This patch is applied once at process startup (from
``JiuWenSwarmDeepAdapter.__init__``):

  A. Wrap ``call_tool`` / ``list_tools`` on the HTTP transports with
     ``anyio.fail_after``. On timeout, clients with lazy reconnect detach the
     dead session and recover on the next call; older clients are disconnected
     so the next call fails fast with "Not connected".

     A lost connection is also persisted, but only once the transport is
     provably gone (issue #5711: the UI kept showing "connected" forever
     after a timeout killed the session):

       * no lazy reconnect (SSE) — we just tore the client down and nothing
         rebuilds it, so every later call fails "Not connected";
       * lazy reconnect (streamable-http) — the *next* call decides: a
         successful one recovers, a "Not connected" guard means the reconnect
         was attempted and abandoned.

     A single slow call never flips the persisted state, and ``CancelledError``
     (user pressed stop) is never read as a dead connection.

     NB: must use ``anyio.fail_after`` (a cancel scope), *not*
     ``asyncio.wait_for``. The MCP SDK runs its streamable-http transport
     inside an anyio task group; ``asyncio.wait_for`` runs the wrapped
     coroutine in a *different* asyncio Task, which collides with anyio's
     cancel-scope/Task invariants and corrupts the session — healthy calls
     then start failing with "Not connected". ``anyio.fail_after`` cancels
     within the current task, so it stays compatible with the SDK's scopes.
  B. Monkeypatch ``ToolMgr._create_client`` so ``config.params["timeout_s"]``
     (i.e. ``/mcp add ... --timeout_s N``) is stamped onto the client instance
     as ``_jws_call_timeout`` and honored by (A) and by connect. Falls back to
     ``DEFAULT_CALL_TIMEOUT`` when unset.

Both transforms are idempotent: a module-level ``_PATCHED`` guard makes the
whole function a no-op on repeat calls.
"""
from __future__ import annotations

from typing import Any
import anyio

from openjiuwen.core.common.logging import logger

_PATCHED = False
# (cls, name) pairs already wrapped — idempotency guard independent of
# _PATCHED, so we don't stamp attributes onto function objects (which would
# need a mypy ``[attr-defined]`` type-ignore).
_wrapped_methods: set[tuple[type, str]] = set()
#: Fallback per-call timeout (seconds) when ``--timeout_s`` is not supplied.
DEFAULT_CALL_TIMEOUT = 30.0

__all__ = ["apply_mcp_call_timeout_patch", "DEFAULT_CALL_TIMEOUT"]


def _client_mcp_name(client: Any) -> str:
    """MCP name of a core client: it is ``config.server_name`` at construction."""
    return str(getattr(client, "_name", "") or "").strip()


def _current_session_id() -> str:
    """Session id of the chat run this call belongs to, or "" outside one.

    ``interface_deep._bind_permission_request_context`` binds
    ``CURRENT_RUN_CONTEXT`` around the whole request, so a tool call running
    inside it inherits the value; background/worker paths that never bind it
    simply get no web notification (the persisted state is still correct).
    """
    try:
        from jiuwenswarm.runtime.run_context import CURRENT_RUN_CONTEXT

        context = CURRENT_RUN_CONTEXT.get()
    except Exception:  # noqa: BLE001 - notification must never break the call
        return ""
    return str(getattr(context, "session_id", "") or "").strip()


def _is_temp_probe_client(client: Any) -> bool:
    """True for the throwaway clients that only list an MCP's tools.

    ``fetch_mcp_tools_via_temp_connection`` builds one per ``mcp.show`` /
    ``mcp.list``. It is not the client the session registered, so its timeout
    says nothing about the live connection — reporting it would detach a
    healthy MCP just because the user opened its detail page.
    """
    from jiuwenswarm.common.mcp_config import TEMP_PROBE_CLIENT_ATTR

    return bool(getattr(client, TEMP_PROBE_CLIENT_ATTR, False))


def _is_not_connected_guard(exc: BaseException) -> bool:
    """True for the clients' own "session is gone" RuntimeError.

    Deliberately narrow: ``asyncio.CancelledError`` (the user pressed stop)
    and business errors must never be read as a dead connection.
    """
    return isinstance(exc, RuntimeError) and "not connected" in str(exc).lower()


async def _report_connection_lost(client: Any) -> None:
    """Persist the lost connection and ask the web UI to reload its MCP list.

    Idempotent through ``mark_mcp_connection_lost`` (only a record that is
    still ``connected`` flips), so repeated failures cost one state read.
    """
    name = _client_mcp_name(client)
    if not name:
        logger.debug("[mcp-timeout] cannot attribute a lost connection: client has no _name")
        return
    try:
        if _is_temp_probe_client(client):
            logger.debug(
                "[mcp-timeout] '%s' timed out on a one-shot probe client; "
                "the registered connection is untouched, not reporting",
                name,
            )
            return
        from jiuwenswarm.server.runtime.mcp import registry

        event = registry.mark_mcp_connection_lost(name)
        if not event:
            return
        setattr(client, "_jws_session_lost", False)
        from jiuwenswarm.server.runtime.mcp.connection_events import (
            publish_connection_changed,
        )

        await publish_connection_changed(_current_session_id())
    except Exception as exc:  # noqa: BLE001 - must not mask the tool failure
        logger.warning("[mcp-timeout] connection-lost report for '%s' failed: %r", name, exc)


def apply_mcp_call_timeout_patch(default_timeout: float = DEFAULT_CALL_TIMEOUT) -> None:
    """Apply the per-call MCP timeout patch. Idempotent per process."""
    global _PATCHED
    if _PATCHED:
        return
    _PATCHED = True

    from openjiuwen.core.foundation.tool.mcp.client.streamable_http_client import (
        StreamableHttpClient,
    )
    from openjiuwen.core.foundation.tool.mcp.client.sse_client import SseClient
    from openjiuwen.core.runner.resources_manager.tool_manager import ToolMgr

    def _resolve_timeout(client: Any, explicit: Any) -> float:
        # Explicit kwarg wins (currently never passed by invoke, but keeps the
        # signature honest); then the per-instance value stamped by (B); then
        # the module default.
        if isinstance(explicit, (int, float)) and explicit > 0:
            return float(explicit)
        stamped = getattr(client, "_jws_call_timeout", None)
        if isinstance(stamped, (int, float)) and stamped > 0:
            return float(stamped)
        return default_timeout

    def _wrap_with_timeout(cls: type, name: str) -> None:
        if (cls, name) in _wrapped_methods:
            return
        _wrapped_methods.add((cls, name))
        orig = getattr(cls, name)

        async def wrapped(self, *args, **kwargs):
            timeout = _resolve_timeout(self, kwargs.get("timeout"))
            # SSE re-defaults a missing timeout to 60s; forward ours.
            call_kwargs = {**kwargs, "timeout": timeout}
            # A previous call lost this client's session; only a later call
            # can tell us whether it came back (see the handlers below).
            had_lost = bool(getattr(self, "_jws_session_lost", False))
            try:
                # anyio.fail_after (cancel scope) — NOT asyncio.wait_for: the
                # latter runs the coroutine in a separate asyncio Task, which
                # breaks anyio's cancel-scope invariants inside the MCP SDK
                # transport and corrupts the session on healthy calls.
                with anyio.fail_after(timeout):
                    result = await orig(self, *args, **call_kwargs)
            except TimeoutError:
                logger.warning(
                    "[mcp-timeout] %s.%s timed out after %.1fs, releasing client: %s",
                    cls.__name__,
                    name,
                    timeout,
                    getattr(self, "_server_path", "?"),
                )
                if hasattr(self, "_lazy_reconnect"):
                    # Detach the dead session; the next call lazily reconnects
                    # on the owner task. Awaiting disconnect here would block
                    # behind that queue. A slow-but-alive server looks exactly
                    # like this, so the persisted state is left alone until a
                    # later call proves the transport cannot come back.
                    setattr(self, "_lazy_reconnect", True)
                    setattr(self, "_session", None)
                    setattr(self, "_jws_session_lost", True)
                else:
                    # Older clients: tear down so the next call fails fast.
                    try:
                        await self.disconnect()
                    except BaseException as disc_exc:  # noqa: BLE001
                        from jiuwenswarm.server.runtime.mcp.exc_group import (
                            reraise_as_exception,
                        )
                        try:
                            reraise_as_exception(disc_exc)
                        except Exception as coerce_exc:  # noqa: BLE001
                            logger.warning(
                                "[mcp-timeout] %s.%s disconnect after timeout also failed: %r",
                                cls.__name__,
                                name,
                                coerce_exc,
                            )
                    # Nothing rebuilds this transport (no lazy reconnect), so
                    # every later call fails "Not connected": the connection is
                    # gone and the persisted state must say so.
                    await _report_connection_lost(self)
                raise
            except BaseException as exc:
                # The mcp SDK's streamablehttp_client runs inside an anyio task
                # group. When the server returns an HTTP error (403/500/...),
                # the task-group teardown raises a BaseExceptionGroup containing
                # the real exception (e.g. httpx.HTTPStatusError) PLUS a
                # GeneratorExit (from the generator-based client context being
                # closed). A BaseExceptionGroup containing GeneratorExit is NOT
                # an Exception subclass, so every `except Exception` above and
                # in the handler escapes it → the frontend hangs forever.
                # Coerce it back into a catchable Exception (re-raises the
                # HTTPStatusError on its own when possible, wraps the rest).
                from jiuwenswarm.server.runtime.mcp.exc_group import (
                    reraise_as_exception,
                )
                if had_lost and _is_not_connected_guard(exc):
                    # This call ran after a timeout detached the session and
                    # ended on the client's "Not connected" guard: the lazy
                    # reconnect was attempted and abandoned, so the transport
                    # is gone. CancelledError and business errors never match.
                    await _report_connection_lost(self)
                reraise_as_exception(exc)

            if had_lost:
                # The session is back (lazy reconnect succeeded): nothing was
                # persisted, only the client-side mark needs clearing.
                setattr(self, "_jws_session_lost", False)
            return result

        setattr(cls, name, wrapped)

    def _wrap_connect_with_config_timeout(cls: type) -> None:
        """Pass timeout_s into connect without an outer cancel scope."""
        key = (cls, "connect")
        if key in _wrapped_methods:
            return
        _wrapped_methods.add(key)
        orig = getattr(cls, "connect")

        async def wrapped(self, *args, **kwargs):
            explicit = kwargs.get("timeout")
            if not isinstance(explicit, (int, float)) or explicit <= 0:
                stamped = getattr(self, "_jws_call_timeout", None)
                if isinstance(stamped, (int, float)) and stamped > 0:
                    kwargs["timeout"] = float(stamped)
            return await orig(self, *args, **kwargs)

        setattr(cls, "connect", wrapped)

    # (A) HTTP long-poll transports — same failure mode when the server dies.
    # call_tool / list_tools are the hot path. They enter and exit the MCP SDK's
    # anyio task-group scope within the same call, so anyio.fail_after's cancel
    # scope stays correctly paired.
    #
    # connect is not wrapped in fail_after: it leaves the SDK task-group scope
    # open until disconnect(), so an outer cancel scope would break later calls.
    for cls in (StreamableHttpClient, SseClient):
        _wrap_with_timeout(cls, "call_tool")
        _wrap_with_timeout(cls, "list_tools")
        _wrap_connect_with_config_timeout(cls)

    # (B) Thread config.params["timeout_s"] (--timeout_s) onto each client so
    # (A) can pick it up. NOTE: if the browser-move client patch is applied
    # *after* this one it rebinds ToolMgr._create_client and would shadow this
    # stamp for streamable-http; the common case (no browser tool) is unaffected.
    _orig_create_client = getattr(ToolMgr, "_create_client")

    def _create_client_with_timeout(config):
        client = _orig_create_client(config)
        timeout_s = (getattr(config, "params", None) or {}).get("timeout_s")
        if isinstance(timeout_s, (int, float)) and timeout_s > 0:
            try:
                # setattr (not attribute assignment): McpClient has no
                # _jws_call_timeout field, so direct assignment is an mypy
                # [attr-defined] error. setattr keeps it dynamic & lint-clean.
                setattr(client, "_jws_call_timeout", float(timeout_s))
            except Exception as exc:
                # Don't silently drop the user's --timeout_s. If this client
                # can't hold the attribute (e.g. __slots__ without the field,
                # or a proxy object), surface it so ops notices the silent
                # fallback to DEFAULT_CALL_TIMEOUT instead of debugging blind.
                logger.warning(
                    "[mcp-timeout] failed to apply timeout_s=%.1fs to MCP client "
                    "%s: %r — falling back to default timeout",
                    float(timeout_s),
                    getattr(config, "server_name", "?"),
                    exc,
                )
        return client

    # setattr (not direct assignment): rebinding a staticmethod on the class is
    # an mypy [assignment] error; setattr keeps the monkeypatch lint-clean
    # (no type: ignore needed).
    setattr(ToolMgr, "_create_client", staticmethod(_create_client_with_timeout))

    logger.info(
        "[mcp-timeout] patch applied (default_timeout=%.1fs, covered=StreamableHttpClient,SseClient)",
        default_timeout,
    )

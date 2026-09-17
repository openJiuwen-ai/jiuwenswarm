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
     ``anyio.fail_after``. On timeout we ``disconnect()`` so the dead session
     is torn down and the very next call fails fast (the stock client raises
     "Not connected" once ``self._session`` is None) instead of waiting out
     the full timeout again.

     NB: must use ``anyio.fail_after`` (a cancel scope), *not*
     ``asyncio.wait_for``. The MCP SDK runs its streamable-http transport
     inside an anyio task group; ``asyncio.wait_for`` runs the wrapped
     coroutine in a *different* asyncio Task, which collides with anyio's
     cancel-scope/Task invariants and corrupts the session — healthy calls
     then start failing with "Not connected". ``anyio.fail_after`` cancels
     within the current task, so it stays compatible with the SDK's scopes.
  B. Monkeypatch ``ToolMgr._create_client`` so ``config.params["timeout_s"]``
     (i.e. ``/mcp add ... --timeout_s N``) is stamped onto the client instance
     as ``_jws_call_timeout`` and honored by (A). Falls back to
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
            try:
                # anyio.fail_after (cancel scope) — NOT asyncio.wait_for: the
                # latter runs the coroutine in a separate asyncio Task, which
                # breaks anyio's cancel-scope invariants inside the MCP SDK
                # transport and corrupts the session on healthy calls.
                with anyio.fail_after(timeout):
                    return await orig(self, *args, **kwargs)
            except TimeoutError:
                logger.warning(
                    "[mcp-timeout] %s.%s timed out after %.1fs, disconnecting client: %s",
                    cls.__name__,
                    name,
                    timeout,
                    getattr(self, "_server_path", "?"),
                )
                # Tear down the dead session so the next call fails fast rather
                # than burning another full timeout window on a half-open conn.
                # disconnect runs the same anyio task-group teardown that can
                # raise a BaseExceptionGroup (containing GeneratorExit); coerce
                # it so the timeout path doesn't escape as an uncatchable group.
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
                reraise_as_exception(exc)

        setattr(cls, name, wrapped)

    # (A) HTTP long-poll transports — same failure mode when the server dies.
    # call_tool / list_tools are the hot path. They enter and exit the MCP SDK's
    # anyio task-group scope within the same call, so anyio.fail_after's cancel
    # scope stays correctly paired.
    #
    # connect is deliberately NOT wrapped: connect() does
    # ``self._exit_stack.enter_async_context(self._client)`` — it ENTERS the
    # SDK streamable_http_client's task-group cancel scope but does not EXIT it
    # (the exit is deferred to disconnect() via self._exit_stack.aclose()). If
    # anyio.fail_after wrapped connect, its cancel scope would close when
    # connect() returns, while the SDK's task-group scope it entered is still
    # alive — anyio then raises "Attempted to exit a cancel scope that isn't
    # the current task's current cancel scope" on the next task-group op
    # (e.g. list_tools). connect already guards initialize() with
    # asyncio.wait_for, so it does not need an outer cancel scope.
    for cls in (StreamableHttpClient, SseClient):
        _wrap_with_timeout(cls, "call_tool")
        _wrap_with_timeout(cls, "list_tools")

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

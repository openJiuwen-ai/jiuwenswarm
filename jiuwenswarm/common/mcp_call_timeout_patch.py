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
      The resolved timeout is also propagated into the client method's own
      ``timeout`` kwarg, so SseClient's inner ``asyncio.wait_for`` ceiling
      matches the outer scope instead of clamping to its 60s default (which
      corrupts the session's cancel scopes when it fires first).
  B. Monkeypatch ``ToolMgr._create_client`` so ``config.params["timeout_s"]``
     (i.e. ``/mcp add ... --timeout_s N``) is stamped onto the client instance
     as ``_jws_call_timeout`` and honored by (A). Falls back to
     ``DEFAULT_CALL_TIMEOUT`` when unset.
  C. Wrap the MCP SDK factory functions (``mcp.client.sse.sse_client`` /
     ``mcp.client.streamable_http.streamablehttp_client``) so connections
     created for a client with a stamped ``_jws_call_timeout`` get an
     ``sse_read_timeout`` of at least that value, instead of the SDK's 300s
     default — long tool calls (SSE result stream / held streamable-http
     response) would otherwise be dropped mid-call after 300 idle seconds.

Both transforms are idempotent: a module-level ``_PATCHED`` guard makes the
whole function a no-op on repeat calls.
"""
from __future__ import annotations

import contextvars
from typing import Any
import anyio

from openjiuwen.core.common.logging import logger

_PATCHED = False
# (cls, name) pairs already wrapped — idempotency guard independent of
# _PATCHED, so we don't stamp attributes onto function objects (which would
# need a mypy ``[attr-defined]`` type-ignore).
_wrapped_methods: set[tuple[type, str]] = set()
# (module, name) SDK factory functions already wrapped, and (cls, name)
# client connect methods already wrapped — same idempotency purpose.
_wrapped_factories: set[tuple[object, str]] = set()
_wrapped_connects: set[tuple[type, str]] = set()
#: Set while a client with a stamped ``_jws_call_timeout`` is establishing its
#: transport, so the SDK factory wrappers can lift ``sse_read_timeout`` to the
#: per-connector timeout (see ``_patch_sdk_read_timeouts``).
_pending_sdk_read_timeout: contextvars.ContextVar[float | None] = contextvars.ContextVar(
    "_jws_pending_sdk_read_timeout", default=None
)
#: Fallback per-call timeout (seconds) when ``--timeout_s`` is not supplied.
DEFAULT_CALL_TIMEOUT = 30.0

__all__ = ["apply_mcp_call_timeout_patch", "DEFAULT_CALL_TIMEOUT"]


def _stamp_is_valid(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0


def _patch_sdk_read_timeouts() -> None:
    """Lift the MCP SDK transports' ``sse_read_timeout`` to the connector timeout.

    The SDK builds one httpx client per connection with
    ``httpx.Timeout(timeout, read=sse_read_timeout)`` where ``sse_read_timeout``
    defaults to 300s. A long tool call exceeds it silently: on SSE the result
    is delivered on the event stream, on streamable-http the POST response is
    held until the tool finishes — either way an idle read of more than 300s
    drops the connection mid-call. openjiuwen's clients don't expose the
    parameter, so wrap the module-level factory functions and raise
    ``sse_read_timeout`` to the client's stamped ``_jws_call_timeout`` when one
    is present. The stamp is surfaced to the factories through a ContextVar set
    by wrapping the clients' connect paths (SseClient builds its transport
    inside ``_do_connect`` on the owner task; StreamableHttpClient inside
    ``connect``), which run in the same task that invokes the factory.
    """
    import mcp.client.sse as mcp_sse_module
    import mcp.client.streamable_http as mcp_streamable_http_module
    from openjiuwen.core.foundation.tool.mcp.client.sse_client import SseClient
    from openjiuwen.core.foundation.tool.mcp.client.streamable_http_client import (
        StreamableHttpClient,
    )

    for module, factory_name in (
        (mcp_sse_module, "sse_client"),
        (mcp_streamable_http_module, "streamablehttp_client"),
    ):
        if (module, factory_name) in _wrapped_factories:
            continue
        _wrapped_factories.add((module, factory_name))
        orig_factory = getattr(module, factory_name)

        def factory_with_read_timeout(*args: Any, orig=orig_factory, name=factory_name, **kwargs: Any):
            hint = _pending_sdk_read_timeout.get()
            if _stamp_is_valid(hint):
                current = kwargs.get("sse_read_timeout")
                if not _stamp_is_valid(current) or float(current) < float(hint):
                    kwargs["sse_read_timeout"] = float(hint)
                    logger.info(
                        "[mcp-timeout] raised %s sse_read_timeout to %.1fs for this connection",
                        name,
                        float(hint),
                    )
            return orig(*args, **kwargs)

        setattr(module, factory_name, factory_with_read_timeout)

    for cls, method_name in (
        (SseClient, "_do_connect"),
        (StreamableHttpClient, "connect"),
    ):
        if (cls, method_name) in _wrapped_connects:
            continue
        _wrapped_connects.add((cls, method_name))
        orig_method = getattr(cls, method_name)

        async def method_with_read_timeout(self, *args: Any, orig=orig_method, **kwargs: Any):
            hint = getattr(self, "_jws_call_timeout", None)
            token = (
                _pending_sdk_read_timeout.set(float(hint)) if _stamp_is_valid(hint) else None
            )
            try:
                return await orig(self, *args, **kwargs)
            finally:
                if token is not None:
                    _pending_sdk_read_timeout.reset(token)

        setattr(cls, method_name, method_with_read_timeout)


def _patch_sse_disconnect_guard() -> None:
    """Keep ``SseClient.disconnect`` best-effort cleanup.

    openjiuwen's ``_do_disconnect`` wraps the session/transport ``__aexit__``
    calls in ``asyncio.wait_for``, which executes them in a new asyncio Task.
    The contexts were entered on the SSE owner task, so exiting them from
    wait_for's task trips anyio's cancel-scope invariant; the broken teardown
    then surfaces as a ``CancelledError`` raised from ``_submit``'s future —
    a BaseException that sails past the ``except Exception`` handlers around
    discovery cleanup (``_list_remote_mcp_connector_tools``) and the pooled
    worker's exit-stack callback, aborting SSE connector registration outright.
    Disconnect is cleanup: swallow the future-cancel flavor, log it, and
    report failure. A genuine cancellation of the running task (``cancelling``
    count > 0) still propagates.
    """
    import asyncio as _asyncio

    from openjiuwen.core.foundation.tool.mcp.client.sse_client import SseClient

    if (SseClient, "disconnect-guard") in _wrapped_connects:
        return
    _wrapped_connects.add((SseClient, "disconnect-guard"))
    orig_disconnect = getattr(SseClient, "disconnect")

    async def disconnect_guarded(self, *args: Any, orig=orig_disconnect, **kwargs: Any):
        try:
            return await orig(self, *args, **kwargs)
        except _asyncio.CancelledError:
            task = _asyncio.current_task()
            cancelling = getattr(task, "cancelling", None)
            if callable(cancelling) and cancelling() > 0:
                raise
            logger.warning(
                "[mcp-timeout] SseClient.disconnect aborted by transport teardown "
                "(pre-existing anyio cancel-scope conflict); treated as failed cleanup",
            )
            return False

    setattr(SseClient, "disconnect", disconnect_guarded)


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
            # Run the inner deadline slightly behind the resolved timeout so
            # that, under the pooled worker (whose outer asyncio.wait_for uses
            # exactly ``timeout``), the OUTER deadline always fires first and
            # produces a clean TimeoutError. If the anyio scope below fired at
            # the same instant from inside wait_for's spawned task, its cancel
            # bleeds into the wait_for machinery and surfaces as a
            # CancelledError instead. Direct (non-worker) callers simply get
            # a 5s-later backstop. The resolved timeout is also propagated
            # into the client method's ``timeout`` kwarg so SseClient's inner
            # ``asyncio.wait_for`` ceiling follows the same deadline instead
            # of clamping to its 60s default.
            deadline = timeout + 5.0
            kwargs.setdefault("timeout", deadline)
            try:
                # anyio.fail_after (cancel scope) — NOT asyncio.wait_for: the
                # latter runs the coroutine in a separate asyncio Task, which
                # breaks anyio's cancel-scope invariants inside the MCP SDK
                # transport and corrupts the session on healthy calls.
                with anyio.fail_after(deadline):
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
                try:
                    await self.disconnect()
                except Exception as exc:
                    logger.warning(
                        "[mcp-timeout] %s.%s disconnect after timeout also failed: %r",
                        cls.__name__,
                        name,
                        exc,
                    )
                raise

        setattr(cls, name, wrapped)

    # (A) HTTP long-poll transports — same failure mode when the server dies.
    for cls in (StreamableHttpClient, SseClient):
        _wrap_with_timeout(cls, "call_tool")
        _wrap_with_timeout(cls, "list_tools")

    _patch_sdk_read_timeouts()
    _patch_sse_disconnect_guard()

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
        "[mcp-timeout] patch applied (default_timeout=%.1fs, covered=StreamableHttpClient,SseClient,"
        "sdk_read_timeouts)",
        default_timeout,
    )

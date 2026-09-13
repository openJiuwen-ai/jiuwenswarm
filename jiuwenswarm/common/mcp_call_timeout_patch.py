# coding: utf-8
"""Inject a per-call timeout into openjiuwen's MCP HTTP clients.

Why this exists: ``MCPTool.invoke`` calls ``call_tool`` without a ``timeout``,
and the stock ``StreamableHttpClient.call_tool`` / ``list_tools`` await
``self._session.call_tool(...)`` / ``self._session.list_tools()`` with no
deadline. When a remote tool is slow or the server dies mid-session, the call
hangs on the MCP SDK's ``sse_read_timeout`` (default 300s).

This patch is applied once at process startup (from
``JiuWenSwarmDeepAdapter.__init__``):

  A. Wrap ``call_tool`` / ``list_tools`` on the HTTP transports with
     ``anyio.fail_after``. On timeout we tear down the dead session and
     **force-invalidate** local state even if ``disconnect()`` raises
     (e.g. ``Attempted to exit cancel scope in a different task``). The next
     call auto-reconnects on a fresh ``AsyncExitStack`` so the same session
     can keep using other tools (TC_MCP_CALL_014).

     NB: must use ``anyio.fail_after`` (a cancel scope), *not*
     ``asyncio.wait_for``. The MCP SDK runs its streamable-http transport
     inside an anyio task group; ``asyncio.wait_for`` runs the wrapped
     coroutine in a *different* asyncio Task, which collides with anyio's
     cancel-scope/Task invariants and corrupts the session — healthy calls
     then start failing with "Not connected". ``anyio.fail_after`` cancels
     within the current task, so it stays compatible with the SDK's scopes.
  B. Monkeypatch ``ToolMgr._create_client`` so ``config.params["timeout_s"]``
     is stamped onto the client instance as ``_jws_call_timeout`` and honored
     by (A). Falls back to ``DEFAULT_CALL_TIMEOUT`` when unset.

Both transforms are idempotent: a module-level ``_PATCHED`` guard makes the
whole function a no-op on repeat calls.
"""
from __future__ import annotations

from contextlib import AsyncExitStack
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

__all__ = [
    "DEFAULT_CALL_TIMEOUT",
    "apply_mcp_call_timeout_patch",
    "force_invalidate_mcp_client",
]


def force_invalidate_mcp_client(client: Any) -> None:
    """丢弃半死会话状态，换上新的 AsyncExitStack，供后续 ``connect()`` 重连。

    SDK ``StreamableHttpClient.disconnect`` 在 aclose 失败时往往不清 ``_session``，
    且成功 aclose 后旧 stack 也不能再 enter。超时路径必须无条件调用本函数。
    """
    for attr in ("_session", "_client", "_read", "_write"):
        if hasattr(client, attr):
            setattr(client, attr, None)
    if hasattr(client, "_is_disconnected"):
        setattr(client, "_is_disconnected", True)
    if hasattr(client, "_exit_stack"):
        setattr(client, "_exit_stack", AsyncExitStack())
    if hasattr(client, "_auth_provider"):
        setattr(client, "_auth_provider", None)


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

    async def _ensure_connected(client: Any) -> None:
        """上一轮超时后会话可能半死；下次调用前强制重连。"""
        session = getattr(client, "_session", None)
        disconnected = bool(getattr(client, "_is_disconnected", False))
        if session is not None and not disconnected:
            return
        force_invalidate_mcp_client(client)
        connect = getattr(client, "connect", None)
        if not callable(connect):
            raise RuntimeError("MCP client has no connect() for post-timeout recovery")
        ok = await connect()
        if not ok:
            raise RuntimeError(
                "MCP client reconnect after timeout failed: "
                f"{getattr(client, '_server_path', '?')}"
            )

    async def _teardown_after_timeout(
        client: Any, *, cls_name: str, method: str, timeout: float
    ) -> None:
        try:
            await client.disconnect()
        except Exception as exc:
            logger.warning(
                "[mcp-timeout] %s.%s disconnect after timeout also failed: %r",
                cls_name,
                method,
                exc,
            )
        finally:
            # disconnect 成功也要换新 stack；失败更要清掉残留 _session（TC_MCP_CALL_014）。
            force_invalidate_mcp_client(client)
            logger.warning(
                "[mcp-timeout] %s.%s session invalidated after %.1fs timeout "
                "(next call will reconnect): %s",
                cls_name,
                method,
                timeout,
                getattr(client, "_server_path", "?"),
            )

    def _wrap_with_timeout(cls: type, name: str) -> None:
        if (cls, name) in _wrapped_methods:
            return
        _wrapped_methods.add((cls, name))
        orig = getattr(cls, name)

        async def wrapped(self, *args, **kwargs):
            timeout = _resolve_timeout(self, kwargs.get("timeout"))
            # 同会话续聊：上一轮超时后自动恢复连接，避免永久 execute invoke failed。
            await _ensure_connected(self)
            try:
                # anyio.fail_after (cancel scope) — NOT asyncio.wait_for: the
                # latter runs the coroutine in a separate asyncio Task, which
                # breaks anyio's cancel-scope invariants inside the MCP SDK
                # transport and corrupts the session on healthy calls.
                with anyio.fail_after(timeout):
                    return await orig(self, *args, **kwargs)
            except TimeoutError:
                logger.warning(
                    "[mcp-timeout] %s.%s timed out after %gs, disconnecting client: %s",
                    cls.__name__,
                    name,
                    timeout,
                    getattr(self, "_server_path", "?"),
                )
                await _teardown_after_timeout(
                    self, cls_name=cls.__name__, method=name, timeout=timeout
                )
                # 空 TimeoutError 会导致 SSE error=''；给出可识别文案。
                raise TimeoutError(
                    f"MCP {cls.__name__}.{name} timed out after {timeout:g}s"
                ) from None

        setattr(cls, name, wrapped)

    # (A) HTTP long-poll transports — same failure mode when the server dies.
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

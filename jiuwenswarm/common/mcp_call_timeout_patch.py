# coding: utf-8
"""Inject a per-call timeout + dead-session recovery into openjiuwen MCP HTTP clients.

Why this exists: ``MCPTool.invoke`` calls ``call_tool`` without a ``timeout``,
and the stock ``StreamableHttpClient.call_tool`` / ``list_tools`` await
``self._session.call_tool(...)`` / ``self._session.list_tools()`` with no
deadline. When a remote tool is slow or the server dies mid-session, the call
hangs on the MCP SDK's ``sse_read_timeout`` (default 300s).

Additionally, ToolMgr keeps one long-lived MCP client per server for the whole
AgentServer process. Chat sessions A/B share that client. When the remote
Streamable HTTP service restarts, the protocol session id is stale and calls
fail with ``Session terminated`` / HTTP 404 — including brand-new chat
sessions — until AS rebuilds the MCP connection (field TC after mock restart).

SSE / Streamable HTTP timeout teardown often raises ``asyncio.CancelledError``
(cancel-scope cross-task noise) *after* the real timeout. ``CancelledError`` is
not an ``Exception`` subclass, so ``MCPTool.invoke`` cannot turn it into a tool
error — the agent loop treats it as “user cancel”, skips ``chat.tool_result``,
and replies “工具调用被用户取消”. This patch converts non-outer
``CancelledError`` into ``TimeoutError`` / transport ``RuntimeError``.

This patch is applied once at process startup (from
``JiuWenSwarmDeepAdapter.__init__``):

  A. Wrap ``call_tool`` / ``list_tools`` on the HTTP transports with
     ``anyio.fail_after``. On timeout we tear down the dead session and
     **force-invalidate** local state even if ``disconnect()`` raises
     (e.g. ``Attempted to exit cancel scope in a different task`` or
     ``CancelledError``). The next call auto-reconnects on a fresh
     ``AsyncExitStack`` so the same chat session can keep using other tools
     (TC_MCP_CALL_014).

     On retryable dead-session errors (``Session terminated``, connection
     closed, …) we invalidate, reconnect once, and retry the same call so
     both the original chat and a newly created chat recover after MCP
     service restart (without requiring AS restart).

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

import asyncio
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

# MCP 对端重启 / 半死连接时 SDK 常见报错（对齐 browser-move 补丁口径）。
_RETRYABLE_DEAD_SESSION_MARKERS: tuple[str, ...] = (
    "session terminated",
    "closedresourceerror",
    "brokenresourceerror",
    "endofstream",
    "stream closed",
    "connection closed",
    "remoteprotocolerror",
    "readerror",
    "writeerror",
    "not connected",
    "broken pipe",
    "server error 404",
    "404 not found",
)

__all__ = [
    "DEFAULT_CALL_TIMEOUT",
    "apply_mcp_call_timeout_patch",
    "force_invalidate_mcp_client",
    "is_retryable_mcp_dead_session_error",
]


def force_invalidate_mcp_client(client: Any) -> None:
    """丢弃半死会话状态，换上新的 AsyncExitStack，供后续 ``connect()`` 重连。

    SDK ``StreamableHttpClient.disconnect`` 在 aclose 失败时往往不清 ``_session``，
    且成功 aclose 后旧 stack 也不能再 enter。超时 / Session terminated 路径必须
    无条件调用本函数。
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


def is_retryable_mcp_dead_session_error(error: BaseException) -> bool:
    """对端重启或传输层半死后，值得作废本地会话并重连重试一次的错误。"""
    name = error.__class__.__name__.lower()
    text = str(error).lower()
    return any(marker in name or marker in text for marker in _RETRYABLE_DEAD_SESSION_MARKERS)


def _is_outer_cancellation() -> bool:
    """True only for real interrupt / WS-drop cancels (not anyio internal noise)."""
    from jiuwenswarm.common.mcp_config import is_asyncio_outer_cancellation

    return is_asyncio_outer_cancellation()


async def _safe_disconnect(client: Any, *, context: str) -> None:
    """disconnect 并吞掉非外层 CancelledError / 普通异常，避免误判用户取消。"""
    try:
        await client.disconnect()
    except asyncio.CancelledError:
        if _is_outer_cancellation():
            raise
        logger.warning(
            "[mcp-timeout] disconnect (%s) raised CancelledError without outer "
            "cancel; treating as transport teardown noise",
            context,
        )
    except Exception as exc:
        logger.warning(
            "[mcp-timeout] disconnect (%s) failed: %r",
            context,
            exc,
        )


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

    async def _connect_fresh(client: Any, *, reason: str) -> None:
        """作废旧协议会话后重新 connect（进程级客户端，跨聊天会话共享）。"""
        await _safe_disconnect(client, context=f"before reconnect ({reason})")
        force_invalidate_mcp_client(client)
        connect = getattr(client, "connect", None)
        if not callable(connect):
            raise RuntimeError("MCP client has no connect() for session recovery")
        ok = await connect()
        if not ok:
            raise RuntimeError(
                f"MCP client reconnect failed ({reason}): "
                f"{getattr(client, '_server_path', '?')}"
            )
        logger.warning(
            "[mcp-timeout] MCP client reconnected after %s: %s",
            reason,
            getattr(client, "_server_path", "?"),
        )

    async def _ensure_connected(client: Any) -> None:
        """上一轮超时 / 作废后会话可能为空；下次调用前强制重连。"""
        session = getattr(client, "_session", None)
        disconnected = bool(getattr(client, "_is_disconnected", False))
        if session is not None and not disconnected:
            return
        await _connect_fresh(client, reason="missing-session")

    async def _teardown_after_timeout(
        client: Any, *, cls_name: str, method: str, timeout: float
    ) -> None:
        try:
            await _safe_disconnect(
                client, context=f"{cls_name}.{method} after timeout"
            )
        finally:
            # disconnect 成功也要换新 stack；失败更要清掉残留 _session（TC_MCP_CALL_014）。
            force_invalidate_mcp_client(client)
            logger.warning(
                "[mcp-timeout] %s.%s session invalidated after %gs timeout "
                "(next call will reconnect): %s",
                cls_name,
                method,
                timeout,
                getattr(client, "_server_path", "?"),
            )

    def _timeout_error(cls_name: str, method: str, timeout: float) -> TimeoutError:
        # 空 TimeoutError 会导致 SSE error=''；给出可识别文案。
        return TimeoutError(
            f"MCP {cls_name}.{method} timed out after {timeout:g}s"
        )

    def _transport_cancel_error(cls_name: str, method: str) -> RuntimeError:
        return RuntimeError(
            f"MCP {cls_name}.{method} interrupted by transport/timeout "
            "(not user cancel)"
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

            async def _invoke_once():
                # anyio.fail_after (cancel scope) — NOT asyncio.wait_for: the
                # latter runs the coroutine in a separate asyncio Task, which
                # breaks anyio's cancel-scope invariants inside the MCP SDK
                # transport and corrupts the session on healthy calls.
                with anyio.fail_after(timeout):
                    return await orig(self, *args, **kwargs)

            async def _handle_timeout() -> None:
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

            async def _handle_spurious_cancel(*, phase: str) -> None:
                """Transport / cancel-scope CancelledError → 可捕获的工具错误。"""
                logger.warning(
                    "[mcp-timeout] %s.%s got CancelledError without outer cancel "
                    "(%s); treating as transport/timeout error: %s",
                    cls.__name__,
                    name,
                    phase,
                    getattr(self, "_server_path", "?"),
                )
                await _teardown_after_timeout(
                    self, cls_name=cls.__name__, method=name, timeout=timeout
                )

            try:
                return await _invoke_once()
            except TimeoutError:
                try:
                    await _handle_timeout()
                except asyncio.CancelledError:
                    # teardown 内偶发 CancelledError 不应冒泡成「用户取消」
                    if _is_outer_cancellation():
                        raise
                    force_invalidate_mcp_client(self)
                    logger.warning(
                        "[mcp-timeout] %s.%s teardown raised CancelledError "
                        "without outer cancel; still reporting timeout: %s",
                        cls.__name__,
                        name,
                        getattr(self, "_server_path", "?"),
                    )
                raise _timeout_error(cls.__name__, name, timeout) from None
            except asyncio.CancelledError:
                # fail_after / SSE disconnect 可能直接冒出 CancelledError；
                # MCPTool.invoke 捕不到 → 误判用户取消且无 tool_result。
                if _is_outer_cancellation():
                    raise
                logger.warning(
                    "[mcp-timeout] %s.%s got CancelledError without outer cancel "
                    "(invoke); reconnecting once: %s",
                    cls.__name__,
                    name,
                    getattr(self, "_server_path", "?"),
                )
                # 连接中断类：作废后重连并重试一次（对齐 SSE mock 重启场景）
                try:
                    await _connect_fresh(self, reason="spurious-cancel")
                    return await _invoke_once()
                except TimeoutError:
                    try:
                        await _handle_timeout()
                    except asyncio.CancelledError:
                        if _is_outer_cancellation():
                            raise
                        force_invalidate_mcp_client(self)
                    raise _timeout_error(cls.__name__, name, timeout) from None
                except asyncio.CancelledError:
                    if _is_outer_cancellation():
                        raise
                    force_invalidate_mcp_client(self)
                    raise _transport_cancel_error(cls.__name__, name) from None
                except Exception:
                    force_invalidate_mcp_client(self)
                    raise _transport_cancel_error(cls.__name__, name) from None
            except Exception as exc:
                # MCP 服务重启后协议会话失效：进程级客户端仍持有旧 session id，
                # 新旧聊天会话都会 Session terminated / 404。作废后重连并重试一次。
                if not is_retryable_mcp_dead_session_error(exc):
                    raise
                logger.warning(
                    "[mcp-timeout] %s.%s hit dead session (%s), reconnecting once: %s",
                    cls.__name__,
                    name,
                    exc,
                    getattr(self, "_server_path", "?"),
                )
                try:
                    await _connect_fresh(self, reason="dead-session")
                except asyncio.CancelledError:
                    if _is_outer_cancellation():
                        raise
                    await _handle_spurious_cancel(phase="reconnect")
                    raise _transport_cancel_error(cls.__name__, name) from None
                except Exception as reconnect_exc:
                    logger.warning(
                        "[mcp-timeout] %s.%s reconnect after dead session failed: %r",
                        cls.__name__,
                        name,
                        reconnect_exc,
                    )
                    raise exc from reconnect_exc
                try:
                    return await _invoke_once()
                except TimeoutError:
                    await _handle_timeout()
                    raise _timeout_error(cls.__name__, name, timeout) from None
                except asyncio.CancelledError:
                    if _is_outer_cancellation():
                        raise
                    await _handle_spurious_cancel(phase="retry")
                    raise _timeout_error(cls.__name__, name, timeout) from None

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

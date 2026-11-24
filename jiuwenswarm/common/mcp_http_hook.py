# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Per-request MCP HTTP auth hook (``before_mcp_http``).

Some MCP gateways need a credential that changes *per HTTP request* (short-lived
tokens, per-call signatures, one-time tickets). The chat-request boundary is too
coarse for that: one turn reuses one MCP connection for many HTTP requests.

This module exposes the same pattern the model path already uses
(``before_model_http``):

1. ``apply_mcp_http_auth_patch()`` wraps the openjiuwen header/query auth
   provider once per process. Every outgoing MCP HTTP request therefore calls a
   registered extension handler before headers are applied.
2. The handler receives ``McpHttpHookContext`` (server name, JSON-RPC method,
   request/session scope) and writes into ``ctx.headers``.
3. Headers are validated (name syntax, no CR/LF/NUL, length) and protocol
   headers (``Content-Type``, ``Accept``, ``Mcp-Session-Id`` ...) cannot be
   overridden. Hook failures fall back to the static configured headers.

Request scope is carried through a ContextVar set by the MCP worker around each
tool call; see ``mcp_config``. When no scope is bound, the hook still runs with
empty ``request_id`` / ``session_id``.
"""

from __future__ import annotations

import contextvars
import json
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

_MCP_REQUEST_SCOPE: contextvars.ContextVar[tuple[str, str] | None] = contextvars.ContextVar(
    "jiuwenswarm_mcp_request_scope", default=None
)

_HEADER_NAME_RE = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
_MAX_HEADER_VALUE_LEN = 32768

# Host-owned protocol/transport headers: an extension must never rewrite them,
# otherwise the MCP handshake (content negotiation, session id) breaks.
PROTECTED_HEADERS = frozenset(
    {
        "accept",
        "accept-encoding",
        "connection",
        "content-encoding",
        "content-length",
        "content-type",
        "host",
        "mcp-protocol-version",
        "mcp-session-id",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)

_PATCHED = False


def set_mcp_request_scope(scope: tuple[str, str] | None):
    """Bind the current MCP call's ``(request_id, session_id)``; returns a token."""

    return _MCP_REQUEST_SCOPE.set(scope)


def reset_mcp_request_scope(token: Any) -> None:
    """Reset the scope bound by :func:`set_mcp_request_scope`.

    A token created in another context cannot be reset; leaving the current
    value alone is the only safe outcome (the worker task owns its own scope).
    """

    try:
        _MCP_REQUEST_SCOPE.reset(token)
    except (ValueError, RuntimeError):
        logger.debug("[mcp_http_hook] request scope token belongs to another context")


def current_mcp_request_scope() -> tuple[str, str] | None:
    return _MCP_REQUEST_SCOPE.get()


def _sanitize_headers(raw: Any) -> dict[str, str]:
    """Keep only well-formed, non-protected headers; drop everything else."""

    if not isinstance(raw, dict):
        return {}
    out: dict[str, str] = {}
    for raw_name, raw_value in raw.items():
        name = str(raw_name or "").strip()
        low = name.lower()
        if not name or not _HEADER_NAME_RE.match(name):
            continue
        if low in PROTECTED_HEADERS:
            continue
        if not isinstance(raw_value, str) or not raw_value:
            continue
        if any(ch in raw_value for ch in "\r\n\0"):
            continue
        if len(raw_value) > _MAX_HEADER_VALUE_LEN:
            continue
        # HTTP/1.1 header values are latin-1. A value outside it would raise
        # inside httpx while encoding the request — that would surface as a
        # broken MCP call, not as a hook fallback. Drop it instead.
        try:
            raw_value.encode("latin-1")
        except UnicodeEncodeError:
            logger.warning(
                "[mcp_http_hook] dropped header %s: value is not latin-1 encodable",
                name,
            )
            continue
        out[name] = raw_value
    return out


def _mcp_http_event() -> str:
    from jiuwenswarm.extensions.hook_event import AgentServerHookEvents

    return AgentServerHookEvents.BEFORE_MCP_HTTP


def _has_before_mcp_http_subscriber(registry: Any) -> bool | None:
    """Whether the event has any subscriber; ``None`` when unknowable."""

    framework = getattr(registry, "callback_framework", None)
    checker = getattr(framework, "has_subscribers", None)
    if not callable(checker):
        return None
    try:
        return bool(checker(_mcp_http_event()))
    except Exception:  # noqa: BLE001 - never block MCP on introspection
        return None


async def apply_mcp_http_hook(server_name: str, request: Any) -> dict[str, str]:
    """Trigger ``before_mcp_http`` for one outgoing request; return extra headers.

    ``request`` is the outgoing ``httpx.Request``. The JSON-RPC method is parsed
    from its body only when someone actually subscribed, so a process without a
    hook pays nothing beyond one registry lookup.

    Never raises: extension failures must not break MCP calls. Returns ``{}``
    when the framework is unavailable, nobody subscribed, or no header survived
    validation.
    """

    try:
        from jiuwenswarm.extensions.hooks_context import McpHttpHookContext
        from jiuwenswarm.extensions.registry import ExtensionRegistry
    except Exception:  # noqa: BLE001 - extension framework is optional
        return {}

    try:
        registry = ExtensionRegistry.get_instance()
    except RuntimeError:
        return {}
    subscribed = _has_before_mcp_http_subscriber(registry)
    if subscribed is False:
        return {}

    scope = current_mcp_request_scope()
    request_id, session_id = scope if scope else ("", "")
    ctx = McpHttpHookContext(
        server_name=str(server_name or ""),
        method=_infer_jsonrpc_method(request),
        request_id=request_id,
        session_id=session_id,
    )
    try:
        await registry.trigger(_mcp_http_event(), ctx)
    except Exception:  # hook failure falls back to static headers
        logger.warning(
            "[mcp_http_hook] before_mcp_http handler failed: server=%s method=%s",
            ctx.server_name,
            ctx.method,
            exc_info=True,
        )
        return {}
    return _sanitize_headers(ctx.headers)


def _infer_jsonrpc_method(request: Any) -> str:
    """Best-effort JSON-RPC method from the outgoing request for hook context."""

    try:
        http_method = str(getattr(request, "method", "") or "").upper()
    except Exception:  # noqa: BLE001
        http_method = ""
    if http_method == "GET":
        return "stream"
    try:
        raw = getattr(request, "content", b"") or b""
        if isinstance(raw, (bytes, bytearray)):
            payload = json.loads(bytes(raw).decode("utf-8"))
            if isinstance(payload, dict):
                method = str(payload.get("method") or "").strip()
                if method:
                    return method
    except Exception:  # noqa: BLE001 - streamed/absent bodies are fine
        logger.debug("[mcp_http_hook] outgoing request body is not JSON-RPC")
    return http_method.lower() or "post"


def _sdk_auth_surface_usable(
    provider_cls: Any,
    strategy_cls: Any,
) -> bool:
    """Both wrapper targets must exist with the methods we patch.

    A half-installed patch is worse than none: the flow wrapper without the
    ``authenticate`` wrapper leaves the provider without ``server_name``, and
    attribution silently degrades. Missing classes yield ``None`` attributes,
    so the callable checks cover them too.
    """

    flow = getattr(provider_cls, "async_auth_flow", None)
    authenticate = getattr(strategy_cls, "authenticate", None)
    return callable(flow) and callable(authenticate)


def apply_mcp_http_auth_patch() -> None:
    """Wrap the SDK header/query auth provider once per process (idempotent)."""

    global _PATCHED
    if _PATCHED:
        return
    try:
        from openjiuwen.core.foundation.tool.auth import auth_callback
    except Exception:  # noqa: BLE001 - SDK without the auth callback module
        logger.debug("[mcp_http_hook] auth_callback unavailable; hook disabled")
        return

    provider_cls = getattr(auth_callback, "AuthHeaderAndQueryProvider", None)
    strategy_cls = getattr(auth_callback, "HeaderQueryAuthStrategy", None)
    if not _sdk_auth_surface_usable(provider_cls, strategy_cls):
        logger.warning("[mcp_http_hook] unexpected SDK auth surface; hook disabled")
        return

    original_flow = provider_cls.async_auth_flow
    original_authenticate = strategy_cls.authenticate

    def _apply_extra_headers(signed: Any, extra: dict[str, str]) -> None:
        """Merge hook headers, dropping anything the transport rejects.

        Mirrors ``_merge_model_http_hook_ctx``: a bad header must degrade to
        "send without it", never to a failed MCP call.
        """

        for name, value in extra.items():
            try:
                signed.headers[name] = value
            except Exception:  # noqa: BLE001 - transport-level value rejection
                logger.warning(
                    "[mcp_http_hook] dropped header rejected by transport: %s", name
                )

    async def _hooked_async_auth_flow(self: Any, request: Any):
        generator = original_flow(self, request)
        server_name = str(getattr(self, "server_name", "") or "")
        try:
            try:
                signed = await generator.__anext__()
            except StopAsyncIteration:
                return
            _apply_extra_headers(signed, await apply_mcp_http_hook(server_name, signed))
            # Proxy the whole flow: httpx feeds the response back into this
            # generator, so forward it with ``asend`` instead of driving the
            # inner generator with ``__anext__`` (which would send ``None``).
            response = yield signed
            while True:
                try:
                    signed = await generator.asend(response)
                except StopAsyncIteration:
                    return
                _apply_extra_headers(
                    signed, await apply_mcp_http_hook(server_name, signed)
                )
                response = yield signed
        finally:
            try:
                await generator.aclose()
            except Exception:  # noqa: BLE001 - generator already finished
                logger.debug("[mcp_http_hook] auth flow generator close failed")

    async def _hooked_authenticate(self: Any, auth_config: Any, **kwargs: Any):
        result = await original_authenticate(self, auth_config, **kwargs)
        try:
            auth_data = getattr(result, "auth_data", None)
            provider = auth_data.get("auth_provider") if isinstance(auth_data, dict) else None
            if provider is not None:
                # openjiuwen passes the server name as ``tool_type`` for MCP
                # clients; ``tool_id`` is the fallback.
                provider.server_name = str(
                    getattr(auth_config, "tool_type", "")
                    or getattr(auth_config, "tool_id", "")
                    or ""
                )
        except Exception:  # noqa: BLE001 - attribution is best-effort
            logger.debug("[mcp_http_hook] auth provider attribution failed")
        return result

    provider_cls.async_auth_flow = _hooked_async_auth_flow
    strategy_cls.authenticate = _hooked_authenticate
    _PATCHED = True
    logger.info("[mcp_http_hook] before_mcp_http auth patch applied")


__all__ = [
    "PROTECTED_HEADERS",
    "apply_mcp_http_auth_patch",
    "apply_mcp_http_hook",
    "current_mcp_request_scope",
    "reset_mcp_request_scope",
    "set_mcp_request_scope",
]

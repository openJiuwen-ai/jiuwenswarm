# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.

"""Unique WebChannel inbound dispatch (WS and HTTP share this pipeline)."""

from __future__ import annotations

import inspect
import logging
from typing import Any

from jiuwenswarm.common.request_identity import (
    merge_routing_into_params,
    web_routing_identity,
)
from jiuwenswarm.common.schema.message import Message
from jiuwenswarm.edition import is_enterprise

logger = logging.getLogger(__name__)

# These methods are handled inside Gateway and therefore do not pass through
# AgentServer's E2A normalization. Local handlers only receive ``params``, so
# merge ``metadata.routing`` into the handler params copy (not Message.params).
_LOCAL_ROUTING_IDENTITY_PREFIXES = ("cron.", "skills.enterprise.")
_LOCAL_ROUTING_IDENTITY_METHODS = frozenset({"models.list", "project.get_sessions", "project.get_cron_sessions"})

_ENTERPRISE_BLOCKED_EXACT = frozenset({
    "config.set", "config.save_all", "models.replace_all", "models.save",
    "models.remove", "models.set_active", "path.set", "updater.download",
    "updater.upgrade", "updater.reset_source", "updater.set_conf",
})
_ENTERPRISE_BLOCKED_PREFIXES = ("agents.", "teams.", "extensions.", "plugins.")
_ENTERPRISE_A2A_ALLOWED = frozenset({
    "a2a.outbound.list",
    "a2a.outbound.enabled.update",
    "a2a.outbound.dispatch.list",
    "a2a.outbound.dispatch.get",
})
_ENTERPRISE_SKILL_ALLOWED = frozenset({
    "skills.list",
    "skills.get",
    "skills.toggle",
    "skills.source.providers",
    "skills.source.search",
    "skills.source.install",
    "skills.updates.check",
    "skills.update",
    "skills.enterprise.list",
    "skills.enterprise.install",
    "skills.enterprise.uninstall",
    "skills.enterprise.source.providers",
    "skills.enterprise.source.search",
})


def is_enterprise_write_forbidden(method: str) -> bool:
    """Central guard shared by WS and HTTP; Cron and locale remain user-writable."""
    if not is_enterprise():
        return False
    if method in _ENTERPRISE_BLOCKED_EXACT or method.startswith(_ENTERPRISE_BLOCKED_PREFIXES):
        return True
    if method.startswith("a2a.") and method not in _ENTERPRISE_A2A_ALLOWED:
        return True
    if method.startswith("channel.") and (method.endswith(".set_conf") or method.endswith(".unbind")):
        return True
    if method.startswith("skills.") and method not in _ENTERPRISE_SKILL_ALLOWED:
        return True
    return False


async def dispatch_web_request(
    channel: Any,
    *,
    method: str,
    params: dict[str, Any],
    request_id: str,
    outbound: Any,
    session_id: str,
    user_message: Message,
) -> None:
    """Run HANDLER_BEFORE → on_message (or bus) → local handler / METHOD_NOT_FOUND.

    ``outbound`` is the reply target: a real WebSocket peer or request-scoped
    HTTP Outbound (``HttpJsonOutbound`` / ``HttpSseOutbound``).
    """
    # Lazy import: avoid import cycle with web_connect (which calls this module).
    from jiuwenswarm.gateway.channel_manager.web.web_rpc_host import (
        HANDLER_BEFORE_CALLBACK_METHODS as _HANDLER_BEFORE_CALLBACK_METHODS,
        MethodHandlerInvocation as _MethodHandlerInvocation,
    )

    if is_enterprise_write_forbidden(method):
        await channel.send_response(
            outbound, request_id, ok=False,
            error="企业版配置由管理面统一下发", code="FORBIDDEN",
        )
        return

    handler = channel.rpc.method_handlers.get(method)
    # 路由身份已由 WS/HTTP 入口写入 metadata.routing；此处只读、不再二次归一化。
    routing = web_routing_identity(
        user_message.metadata if isinstance(user_message.metadata, dict) else None
    )
    if not routing.get("bot_id"):
        logger.warning(
            "[WebChannel] missing bot_id in metadata.routing method=%s request_id=%s routing=%s",
            method,
            request_id,
            routing,
        )
    handler_params = params
    if (
        method in _LOCAL_ROUTING_IDENTITY_METHODS
        or method.startswith(_LOCAL_ROUTING_IDENTITY_PREFIXES)
    ):
        # Gateway 本地 handler 只有 params：把 routing 合并进调用副本。
        handler_params = merge_routing_into_params(
            params,
            user_message.metadata,
            override=True,
        )
    handler_already_called = False
    if method in _HANDLER_BEFORE_CALLBACK_METHODS and handler is not None:
        handler_already_called = await channel.rpc.invoke_method_handler(
            _MethodHandlerInvocation(
                outbound,
                method,
                request_id,
                handler_params,
                session_id,
                handler,
            ),
        )
        if not handler_already_called:
            return

    handled_by_callback = False
    on_message_cb = channel.rpc.on_message_cb
    if on_message_cb is not None:
        result = on_message_cb(user_message)
        if inspect.isawaitable(result):
            result = await result
        handled_by_callback = bool(result)
    else:
        bus = getattr(channel, "bus", None)
        if bus is not None and hasattr(bus, "publish_user_messages"):
            await bus.publish_user_messages(user_message)

    if handled_by_callback:
        return
    if handler_already_called:
        return

    if handler is not None:
        await channel.rpc.invoke_method_handler(
            _MethodHandlerInvocation(
                outbound,
                method,
                request_id,
                handler_params,
                session_id,
                handler,
            ),
        )
    else:
        await channel.send_response(
            outbound,
            request_id,
            ok=False,
            error=f"unknown method: {method}",
            code="METHOD_NOT_FOUND",
        )

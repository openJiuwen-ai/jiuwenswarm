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
from gateway.enterprise import is_enterprise

logger = logging.getLogger(__name__)

# These methods are handled inside Gateway and therefore do not pass through
# AgentServer's E2A normalization. Local handlers only receive ``params``, so
# merge ``metadata.routing`` into the handler params copy (not Message.params).
_LOCAL_ROUTING_IDENTITY_PREFIXES = ("cron.", "skills.enterprise.", "workspace.")
_LOCAL_ROUTING_IDENTITY_METHODS = frozenset({
    "models.list",
    "project.get_sessions",
    "project.get_cron_sessions",
})

_ENTERPRISE_BLOCKED_EXACT = frozenset({
    "config.set", "config.save_all", "models.replace_all", "models.save",
    "models.remove", "models.set_active", "path.set", "updater.download",
    "updater.upgrade", "updater.reset_source", "updater.set_conf",
})
_ENTERPRISE_BLOCKED_PREFIXES = ("agents.", "teams.", "extensions.", "plugins.", "mcp.server.")
_ENTERPRISE_SKILL_ALLOWED = frozenset({
    "skills.list",
    "skills.get",
    "skills.toggle",
    # 技能详情只读接口：文件预览 / 版本列表（与 skills.get 同属查看已安装 skill）。
    "skills.files.list",
    "skills.files.get",
    "skills.versions.list",
    "skills.source.providers",
    "skills.source.search",
    "skills.source.install",
    # 用户安装的 skill 卸载；SkillManager 会拦截 builtin/prebuilt。
    "skills.uninstall",
    "skills.updates.check",
    "skills.update",
    # 聊天产物「保存」走 download_token → import_local；SkillManager 已有企业版路径。
    "skills.import_local",
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
    if method.startswith("a2a."):
        return True
    if method.startswith("channel.") and (method.endswith(".set_conf") or method.endswith(".unbind")):
        return True
    if method.startswith("skills.") and method not in _ENTERPRISE_SKILL_ALLOWED:
        return True
    return False


def _method_handlers(channel: Any) -> dict[str, Any]:
    rpc = getattr(channel, "rpc", None)
    if rpc is not None and hasattr(rpc, "method_handlers"):
        return rpc.method_handlers
    return getattr(channel, "_method_handlers", {})


def _on_message_cb(channel: Any) -> Any:
    rpc = getattr(channel, "rpc", None)
    if rpc is not None and hasattr(rpc, "on_message_cb"):
        return rpc.on_message_cb
    return getattr(channel, "_on_message_cb", None)


async def _invoke_handler(
    channel: Any,
    *,
    outbound: Any,
    method: str,
    request_id: str,
    params: dict[str, Any],
    session_id: str,
    handler: Any,
) -> bool:
    from gateway.channel_manager.web.web_connect import (
        _MethodHandlerInvocation,
    )

    invocation = _MethodHandlerInvocation(
        outbound, method, request_id, params, session_id, handler,
    )
    rpc = getattr(channel, "rpc", None)
    if rpc is not None and hasattr(rpc, "invoke_method_handler"):
        return await rpc.invoke_method_handler(invocation)
    return await channel.invoke_method_handler(invocation)


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
    # Lazy import: avoid import cycle with web_connect.
    from gateway.channel_manager.web.web_connect import (
        _HANDLER_BEFORE_CALLBACK_METHODS,
    )

    if is_enterprise_write_forbidden(method):
        await channel.send_response(
            outbound, request_id, ok=False,
            error="企业版配置由管理面统一下发", code="FORBIDDEN",
        )
        return

    handler = _method_handlers(channel).get(method)
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
        handler_already_called = await _invoke_handler(
            channel,
            outbound=outbound,
            method=method,
            request_id=request_id,
            params=handler_params,
            session_id=session_id,
            handler=handler,
        )
        if not handler_already_called:
            return

    handled_by_callback = False
    on_message_cb = _on_message_cb(channel)
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
        await _invoke_handler(
            channel,
            outbound=outbound,
            method=method,
            request_id=request_id,
            params=handler_params,
            session_id=session_id,
            handler=handler,
        )
    else:
        await channel.send_response(
            outbound,
            request_id,
            ok=False,
            error=f"unknown method: {method}",
            code="METHOD_NOT_FOUND",
        )

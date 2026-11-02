"""Optional host capabilities injected into the transport-neutral Runtime.

The process CLI deliberately leaves these capabilities unset.  AgentServer may
install a push handler for its service lifetime without making Runtime import or
discover a Server/Gateway singleton.
"""

from __future__ import annotations

import inspect
import threading
from collections.abc import Awaitable, Callable
from contextlib import contextmanager
from typing import Any

RuntimePushHandler = Callable[
    [dict[str, Any]],
    Awaitable[bool | None] | bool | None,
]
RuntimeWakeHandler = Callable[[Any], Awaitable[None]]
RuntimeXiaoyiChannelProvider = Callable[[str], Any]

_runtime_push_handler: RuntimePushHandler | None = None
_runtime_wake_handler: RuntimeWakeHandler | None = None
_runtime_xiaoyi_channel_provider: RuntimeXiaoyiChannelProvider | None = None
_runtime_push_handlers: list[RuntimePushHandler] = []
_runtime_wake_handlers: list[RuntimeWakeHandler] = []
_runtime_xiaoyi_channel_providers: list[RuntimeXiaoyiChannelProvider] = []
_runtime_host_handlers_lock = threading.RLock()
_request_push_handlers: dict[tuple[str, ...], RuntimePushHandler] = {}


def _push_route(message: dict[str, Any]) -> tuple[str, ...]:
    from jiuwenswarm.common.request_identity import web_routing_identity

    identity = web_routing_identity(message.get("metadata"))
    return tuple(
        str(message.get(key) or "")
        for key in ("request_id", "channel_id", "session_id")
    ) + tuple(
        identity.get(key, "") for key in ("user_id", "group_id", "bot_id", "gateway_id")
    )


@contextmanager
def scoped_runtime_push_handler(message: dict[str, Any], handler: RuntimePushHandler):
    """Route in-turn delivery even from reused SDK tasks with older ContextVars."""
    key = _push_route(message)
    with _runtime_host_handlers_lock:
        if key in _request_push_handlers:
            raise RuntimeError("duplicate request push route")
        _request_push_handlers[key] = handler
    try:
        yield
    finally:
        with _runtime_host_handlers_lock:
            _request_push_handlers.pop(key, None)


def _remove_handler_owner(handlers: list[Any], handler: Any) -> None:
    """Remove one installed owner without reviving an already removed owner."""
    for index in range(len(handlers) - 1, -1, -1):
        candidate = handlers[index]
        if candidate is handler:
            handlers.pop(index)
            return
    # Bound methods compare equal across repeated attribute access.  Retain the
    # old equality compatibility when the exact callable object is unavailable.
    for index in range(len(handlers) - 1, -1, -1):
        try:
            matches = handlers[index] == handler
        except Exception:
            matches = False
        if matches:
            handlers.pop(index)
            return


def install_runtime_push_handler(
    handler: RuntimePushHandler,
) -> RuntimePushHandler | None:
    """Install a host-owned push handler and return the previous handler."""
    global _runtime_push_handler

    with _runtime_host_handlers_lock:
        previous = _runtime_push_handler
        if not _runtime_push_handlers and previous is not None:
            _runtime_push_handlers.append(previous)
        _runtime_push_handlers.append(handler)
        _runtime_push_handler = handler
        return previous


def restore_runtime_push_handler(
    handler: RuntimePushHandler,
    previous: RuntimePushHandler | None,
) -> None:
    """Remove one push owner and activate the newest remaining owner."""
    global _runtime_push_handler

    _ = previous  # retained for source/API compatibility
    with _runtime_host_handlers_lock:
        _remove_handler_owner(_runtime_push_handlers, handler)
        _runtime_push_handler = (
            _runtime_push_handlers[-1] if _runtime_push_handlers else None
        )


async def send_runtime_push(message: dict[str, Any]) -> bool:
    """Send through the optional host adapter; return False when unavailable.

    HTTP 聊天会挂一个「仅转发 chat.file」的请求级 handler。cron / 其它
    server_push 若命中同一路由键却被它拒绝（返回 False），必须再回退到进程级
    ``send_push``（WebSocket 或 ``events/stream`` hub），否则企业版 runtime
    模式下定时任务永远送不到 Gateway。
    """
    with _runtime_host_handlers_lock:
        scoped = _request_push_handlers.get(_push_route(message))
        host = _runtime_push_handler
    handlers: list[RuntimePushHandler] = []
    if scoped is not None:
        handlers.append(scoped)
    if host is not None and host is not scoped:
        handlers.append(host)
    for handler in handlers:
        result = handler(message)
        if inspect.isawaitable(result):
            result = await result
        if result is None:
            return True
        if isinstance(result, bool):
            if result:
                return True
            continue
        return True
    return False


class RuntimeHostPushTransport:
    """Structural push transport backed only by an explicitly installed host."""

    async def send_push(self, message: dict[str, Any]) -> None:
        if not await send_runtime_push(message):
            raise RuntimeError("runtime push is unavailable without a resident host")


def install_runtime_wake_handler(
    handler: RuntimeWakeHandler,
) -> RuntimeWakeHandler | None:
    """Install a host-owned wake dispatcher and return its predecessor."""
    global _runtime_wake_handler

    with _runtime_host_handlers_lock:
        previous = _runtime_wake_handler
        if not _runtime_wake_handlers and previous is not None:
            _runtime_wake_handlers.append(previous)
        _runtime_wake_handlers.append(handler)
        _runtime_wake_handler = handler
        return previous


def restore_runtime_wake_handler(
    handler: RuntimeWakeHandler,
    previous: RuntimeWakeHandler | None,
) -> None:
    """Remove one wake owner and activate the newest remaining owner."""
    global _runtime_wake_handler

    _ = previous  # retained for source/API compatibility
    with _runtime_host_handlers_lock:
        _remove_handler_owner(_runtime_wake_handlers, handler)
        _runtime_wake_handler = (
            _runtime_wake_handlers[-1] if _runtime_wake_handlers else None
        )


async def send_runtime_wake(message: Any) -> bool:
    """Dispatch a wake request through an explicitly installed host adapter."""
    with _runtime_host_handlers_lock:
        handler = _runtime_wake_handler
    if handler is None:
        return False
    result = handler(message)
    if inspect.isawaitable(result):
        await result
    return True


def install_runtime_xiaoyi_channel_provider(
    provider: RuntimeXiaoyiChannelProvider,
) -> RuntimeXiaoyiChannelProvider | None:
    """Install the optional Gateway-owned Xiaoyi channel lookup adapter."""
    global _runtime_xiaoyi_channel_provider

    with _runtime_host_handlers_lock:
        previous = _runtime_xiaoyi_channel_provider
        if not _runtime_xiaoyi_channel_providers and previous is not None:
            _runtime_xiaoyi_channel_providers.append(previous)
        _runtime_xiaoyi_channel_providers.append(provider)
        _runtime_xiaoyi_channel_provider = provider
        return previous


def restore_runtime_xiaoyi_channel_provider(
    provider: RuntimeXiaoyiChannelProvider,
    previous: RuntimeXiaoyiChannelProvider | None,
) -> None:
    """Remove one Xiaoyi provider owner, including out-of-order shutdowns."""
    global _runtime_xiaoyi_channel_provider

    _ = previous  # retained for the same install/restore contract as push/wake
    with _runtime_host_handlers_lock:
        _remove_handler_owner(_runtime_xiaoyi_channel_providers, provider)
        _runtime_xiaoyi_channel_provider = (
            _runtime_xiaoyi_channel_providers[-1]
            if _runtime_xiaoyi_channel_providers
            else None
        )


def get_runtime_xiaoyi_channel(channel_id: str = "xiaoyi") -> Any:
    with _runtime_host_handlers_lock:
        provider = _runtime_xiaoyi_channel_provider
    return provider(channel_id) if provider is not None else None


__all__ = [
    "scoped_runtime_push_handler",
    "RuntimePushHandler",
    "RuntimeHostPushTransport",
    "RuntimeWakeHandler",
    "RuntimeXiaoyiChannelProvider",
    "get_runtime_xiaoyi_channel",
    "install_runtime_wake_handler",
    "install_runtime_push_handler",
    "install_runtime_xiaoyi_channel_provider",
    "restore_runtime_wake_handler",
    "restore_runtime_push_handler",
    "restore_runtime_xiaoyi_channel_provider",
    "send_runtime_push",
    "send_runtime_wake",
]

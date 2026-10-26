from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable


@dataclass
class GatewayRouteBinding:
    """统一描述 Gateway 路由及其附加安装动作。"""

    path: str
    channel_id: str
    forward_methods: frozenset[str] = frozenset()
    forward_no_local_handler_methods: frozenset[str] = frozenset()
    inbound_interceptor: Callable[..., Awaitable[bool]] | None = None
    outbound_interceptor: Callable[..., Awaitable[bool]] | None = None
    cleanup_handler: Callable[..., Any] | None = None
    disconnect_handler: Callable[..., Any] | None = None
    session_bind_handler: Callable[..., Any] | None = None
    install: Callable[[Any], None] | None = None
    # V2: 委托 ws 注册的外部 Channel（tui 的 TuiChannel）。None = 不委托。
    ws_channel: Any = None

# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Web proxy: transparently proxy HTTP/WS requests to 3rd-agent containers."""

from __future__ import annotations

from typing import TYPE_CHECKING

__all__ = [
    "WebProxyChannelConfig",
    "attach_web_proxy_routes",
    "RESERVED_AGENT_TYPES",
]

if TYPE_CHECKING:
    from jiuwenswarm.gateway.channel_manager.protocol.web_proxy.web_proxy_connect import (
        RESERVED_AGENT_TYPES,
        WebProxyChannelConfig,
        attach_web_proxy_routes,
    )


def __getattr__(name: str):
    if name in __all__:
        from jiuwenswarm.gateway.channel_manager.protocol.web_proxy import (
            web_proxy_connect,
        )

        return getattr(web_proxy_connect, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

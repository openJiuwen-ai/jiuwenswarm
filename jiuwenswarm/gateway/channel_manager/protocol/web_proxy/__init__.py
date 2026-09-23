# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Web proxy: transparently proxy HTTP/WS requests to 3rd-agent containers."""

from __future__ import annotations

from typing import TYPE_CHECKING

__all__ = [
    "WebProxyChannelConfig",
    "WebPortManager",
    "register_3rdagent_web_method",
]

if TYPE_CHECKING:
    from jiuwenswarm.gateway.channel_manager.protocol.web_proxy.web_proxy_connect import (
        WebProxyChannelConfig,
    )
    from jiuwenswarm.gateway.channel_manager.protocol.web_proxy.web_proxy_listen import (
        WebPortManager,
        register_3rdagent_web_method,
    )


def __getattr__(name: str):
    if name in __all__:
        from jiuwenswarm.gateway.channel_manager.protocol.web_proxy import (
            web_proxy_connect,
            web_proxy_listen,
        )

        for module in (web_proxy_connect, web_proxy_listen):
            if hasattr(module, name):
                return getattr(module, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

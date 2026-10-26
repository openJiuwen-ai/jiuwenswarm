# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""传输层：WebSocket / HTTP / SSE 的对等实现，共享传输无关的业务层。

各传输在 ``AgentWebSocketServer._dispatch_parsed_request`` 汇合，此后的分发与
业务处理两侧共用；本包只负责「把响应帧写回各自的连接」这一段差异。

- :mod:`~jiuwenswarm.server.transports.sink` —— ``ResponseSink`` 协议与
  ``WSSink`` / ``UnaryHTTPSink`` / ``SSESink`` 三个实现，表内 handler 的出口；
- :mod:`~jiuwenswarm.server.transports.push_registry` —— 服务端主动推送的订阅者
  注册表，WS 与 SSE 订阅者都在其中。
"""

from jiuwenswarm.server.transports.sink import (
    ResponseSink,
    SSESink,
    UnaryHTTPSink,
    WSSink,
)

__all__ = [
    "ResponseSink",
    "SSESink",
    "UnaryHTTPSink",
    "WSSink",
]

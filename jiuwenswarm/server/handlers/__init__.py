# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""传输无关的业务 handler，按域分模块。

每个 handler 签名为 ``async def handle_xxx(ctx: RequestContext) -> None``，
只通过 ``ctx.sink`` 输出，因此同一份代码同时服务 WebSocket 与 HTTP/SSE，
并且可以只用一个假 sink 做单测。

``dispatch.HANDLERS`` 的每一条表项都指向本包的自由函数，``agent_ws_server``
上没有分发表 handler（守护：``test_fn_handlers_do_not_remain_on_server``）。

``_shared`` 存放跨域共享依赖：``agent_ws_server → dispatch → handlers.*`` 是导入期
依赖，本包**不能反向 import ``agent_ws_server``**，共享状态一律经 ``_shared`` 中转。
"""

from jiuwenswarm.server.handlers import (
    agents,
    bootstrap,
    chat,
    commands,
    extensions,
    file_transfer,
    mcp,
    ops,
    permissions,
    sandbox,
    schedule,
    session,
    team,
)

__all__ = [
    "agents",
    "bootstrap",
    "chat",
    "commands",
    "extensions",
    "file_transfer",
    "mcp",
    "ops",
    "permissions",
    "sandbox",
    "schedule",
    "session",
    "team",
]

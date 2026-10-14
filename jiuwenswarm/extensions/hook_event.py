# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""兼容别名：hook 事件已归 ``jiuwenswarm.gateway.extensions.hook_event``。"""

from jiuwenswarm.gateway.extensions.hook_event import (  # noqa: F401
    AgentServerHookEvents,
    GatewayHookEvents,
)

__all__ = ["AgentServerHookEvents", "GatewayHookEvents"]

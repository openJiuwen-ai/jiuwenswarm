# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""兼容别名：hook 事件已下沉 ``jiuwenswarm.common.hook_event``（gateway/server 共享类型）。"""

from jiuwenswarm.common.hook_event import (  # noqa: F401
    AgentServerHookEvents,
    GatewayHookEvents,
)

__all__ = ["AgentServerHookEvents", "GatewayHookEvents"]

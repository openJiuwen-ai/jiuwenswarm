# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""WebChannel 按业务拆分的 method 注册模块。"""

from __future__ import annotations

from typing import Any

from jiuwenswarm.gateway.channel_manager.web.web_method_register.long_horizon import (
    register_long_horizon_web_methods,
)
from jiuwenswarm.gateway.channel_manager.web.web_method_register.workspace import (
    register_workspace_web_methods,
)

__all__ = [
    "register_long_horizon_web_methods",
    "register_modular_web_methods",
    "register_workspace_web_methods",
]


def register_modular_web_methods(channel: Any, *, agent_client: Any) -> None:
    register_long_horizon_web_methods(channel)
    register_workspace_web_methods(channel, agent_client=agent_client)

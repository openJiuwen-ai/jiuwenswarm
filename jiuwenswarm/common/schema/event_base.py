# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
"""转发别名（过渡形态）：HookEventBase 契约已迁 ``gateway_protocol.hooks``。

与 openjiuwen 0.1.9+ ``openjiuwen.core.runner.callback.events`` 中 EventBase 行为对齐。
放在 schema 子包内避免执行 ``extensions/__init__.py`` 引发循环依赖的历史原因不变；
gateway 独立仓就绪后本别名随 common 收敛删除。
"""

from __future__ import annotations

from gateway_protocol.hooks import (  # noqa: F401
    DEFAULT_SCOPE,
    HookEventBase,
    build_event_name,
    parse_event_name,
)

__all__ = [
    "DEFAULT_SCOPE",
    "HookEventBase",
    "build_event_name",
    "parse_event_name",
]

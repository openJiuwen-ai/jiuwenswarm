# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
"""转发别名（过渡形态）：ThirdAgent 契约已迁 ``gateway_protocol.third_agent``。

ThirdAgent ABC、UnsupportedThirdAgent 兜底实现与 get_unsupported_third_agent
均以 protocol 包为 source of truth；gateway 树内 ``gateway/routing/third_agent.py``
对本模块的 re-export 链不受影响。
"""

from __future__ import annotations

from gateway_protocol.third_agent import (  # noqa: F401
    ThirdAgent,
    UnsupportedThirdAgent,
    get_unsupported_third_agent,
)

__all__ = ["ThirdAgent", "UnsupportedThirdAgent", "get_unsupported_third_agent"]

# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""ThirdAgent - 第三方 Agent list/switch 能力接口.

实现已下沉 ``jiuwenswarm.common.client.third_agent``（保留侧与 Gateway 仓共用契约）；
此处 re-export 保持既有 import 路径兼容。
"""

from __future__ import annotations

from jiuwenswarm.common.client.third_agent import (
    ThirdAgent,
    UnsupportedThirdAgent,
    get_unsupported_third_agent,
)

__all__ = ["ThirdAgent", "UnsupportedThirdAgent", "get_unsupported_third_agent"]
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""转发别名：实现已迁 ``gateway.common.client.third_agent``。"""

from gateway.common.client.third_agent import (  # noqa: F401
    ThirdAgent,
    UnsupportedThirdAgent,
    get_unsupported_third_agent,
)

__all__ = ["ThirdAgent", "UnsupportedThirdAgent", "get_unsupported_third_agent"]

# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""兼容别名：MCP 异常组转换已下沉 ``jiuwenswarm.common.mcp_exc_group``。"""

from jiuwenswarm.common.mcp_exc_group import (  # noqa: F401
    reraise_as_exception,
)

__all__ = ["reraise_as_exception"]

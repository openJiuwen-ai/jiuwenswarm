# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""专家包 HTTP 的线上路径。

实现已收口到 ``openjiuwen_protocol.expert_http_routes``（gateway 与
AgentServer 共用的唯一来源）。本模块仅 re-export，保留既有导入路径兼容；
只保留方法、动词和路径，不读产品形态，不含北向查询参数或说明文字。
"""

from __future__ import annotations

from openjiuwen_protocol.expert_http_routes import (
    EXPERT_TEMPLATE_HTTP_ROUTES,
    ExpertHttpRoute,
    expert_http_routes,
)

__all__ = [
    "EXPERT_TEMPLATE_HTTP_ROUTES",
    "ExpertHttpRoute",
    "expert_http_routes",
]

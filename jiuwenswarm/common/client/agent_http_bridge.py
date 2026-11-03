# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""转发别名：实现已迁 ``gateway.common.client.agent_http_bridge``。

Gateway → AgentServer 的受认证 HTTP bridge（下载/上传基址推导、令牌解析、上传
执行）唯一实现在 gateway 包，避免两仓各持一份推导逻辑导致端点/环境变量语义漂移。
母仓调用方（Web 静态服务）经本别名继续使用同一实现。
"""

from __future__ import annotations

from gateway.common.client.agent_http_bridge import (  # noqa: F401
    UPLOAD_TIMEOUT_SECONDS,
    resolve_agent_host_port,
    resolve_agent_http_base,
    resolve_agent_http_base_for_token,
    resolve_agent_upload_base,
    set_agent_http_base_resolver,
    upload_file_bytes,
)

__all__ = [
    "UPLOAD_TIMEOUT_SECONDS",
    "resolve_agent_host_port",
    "resolve_agent_http_base",
    "resolve_agent_http_base_for_token",
    "resolve_agent_upload_base",
    "set_agent_http_base_resolver",
    "upload_file_bytes",
]

# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Gateway Config Receiver 对外可访问 endpoint 解析。"""

from __future__ import annotations

from jiuwenswarm.common.security.link_mtls import LinkMTLSConfig

from .config import Settings, get_settings


def resolve_public_endpoint(cfg: Settings | None = None) -> str:
    """Manager 回调用的 Gateway Manager Config Base URL（无尾斜杠）。

    与统一 HTTP 宿主共用端口：优先 ``WEB_PORT``，其次废弃别名
    ``GATEWAY_CONFIG_HTTP_PORT``（默认 19000）。
    """
    import os

    cfg = cfg or get_settings()
    web_port = (os.getenv("WEB_PORT") or "").strip()
    if web_port.isdigit():
        port = int(web_port)
    else:
        port = int(cfg.gateway_config_http_port or 19000)
    host = (cfg.gateway_config_public_host or "").strip() or "127.0.0.1"
    scheme = "https" if LinkMTLSConfig.from_env().enforced else cfg.gateway_config_public_scheme
    return f"{scheme}://{host}:{port}"

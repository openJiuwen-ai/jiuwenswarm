# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Gateway Config Receiver 对外可访问 endpoint 解析。"""

from __future__ import annotations

from .config import Settings, get_settings


def resolve_public_endpoint(cfg: Settings | None = None) -> str:
    """Manager 回调用的 Gateway Config Receiver Base URL（无尾斜杠）。

    由 ``GATEWAY_CONFIG_PUBLIC_SCHEME``、``GATEWAY_CONFIG_PUBLIC_HOST`` 和
    ``GATEWAY_CONFIG_HTTP_PORT`` 拼接；host 未配置时默认 ``127.0.0.1``。
    """
    cfg = cfg or get_settings()
    port = int(cfg.gateway_config_http_port or 8775)
    host = (cfg.gateway_config_public_host or "").strip() or "127.0.0.1"
    return f"{cfg.gateway_config_public_scheme}://{host}:{port}"

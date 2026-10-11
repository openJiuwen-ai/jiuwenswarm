# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""WebSocket 这扇门开不开。默认开着，和现在的启动方式一样。

关掉时，进程仍会把公共服务拉起来，但不会在 WebSocket 端口上等人来连。
"""

from __future__ import annotations

import os
from typing import Any


def _as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"1", "true", "yes", "on"}:
            return True
        if text in {"0", "false", "no", "off"}:
            return False
    return default


def resolve_websocket_enabled(
    *,
    env: dict[str, str] | None = None,
    config_section: Any = None,
) -> bool:
    """WebSocket 门是否打开。环境变量优先，都没写时是开的。"""
    source = os.environ if env is None else env
    raw = source.get("AGENT_WS_ENABLED")
    if raw is not None and str(raw).strip() != "":
        return _as_bool(raw, True)
    if isinstance(config_section, dict) and "enabled" in config_section:
        return _as_bool(config_section.get("enabled"), True)
    return True

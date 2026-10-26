# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""审计写出闸门：有库配置才允许 emit。

与 OTEL / telemetry runtime 解耦，避免业务打点与冷加载互相拖依赖。
"""

from __future__ import annotations

# 仅当 Gateway 本地库存在有效 audit_log_config 并完成 apply 后为 True。
_audit_config_enabled = False


def is_audit_config_enabled() -> bool:
    """是否已加载有效审计配置（空库 / DELETE 后为 False）。"""
    return _audit_config_enabled


def set_audit_config_enabled(enabled: bool) -> None:
    """由 ``apply_audit_log_config_payload`` 在有/无库配置时切换写出闸门。"""
    global _audit_config_enabled
    _audit_config_enabled = bool(enabled)


__all__ = [
    "is_audit_config_enabled",
    "set_audit_config_enabled",
]

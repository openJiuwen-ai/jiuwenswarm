# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Web 配置面板用的 A2UI 字段（无 server 依赖）。

gateway 拆仓后进程内不再 import ``server.runtime.a2ui.integration``，但
``config.get`` / ``config.set`` 仍需读写 ``a2ui_enabled``。这些辅助放在
common，gateway 与 AgentServer 均可直接调用，避免 ``provider("a2ui")``
未注册时整页 ``config.get`` 失败。
"""

from __future__ import annotations

import os
from typing import Any

_WEB_CONFIG_KEY_MAP: dict[str, str] = {
    "a2ui_enabled": "enabled",
}

_A2UI_CONFIG_DEFAULT_PAYLOAD: dict[str, str] = {
    "a2ui_enabled": "false",
}


def _to_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default


def _bool_text(value: bool) -> str:
    return "true" if value else "false"


def _a2ui_enabled_from_raw(raw_config: dict[str, object] | None) -> bool:
    raw = raw_config if isinstance(raw_config, dict) else {}
    section = raw.get("a2ui")
    section = section if isinstance(section, dict) else {}
    enabled = _to_bool(section.get("enabled"), False)
    env_enabled = os.getenv("JIUWENSWARM_A2UI_ENABLED")
    if env_enabled is not None:
        enabled = _to_bool(env_enabled, enabled)
    # Preserve the runtime parser's supported-protocol contract without importing
    # server modules into the Gateway process.
    protocol_version = str(
        os.getenv("JIUWENSWARM_A2UI_PROTOCOL_VERSION")
        or section.get("protocol_version")
        or "0.8"
    ).strip()
    if protocol_version != "0.8":
        raise ValueError(f"Unsupported A2UI protocol version: {protocol_version}")
    return enabled


def get_a2ui_config_payload(raw_config: dict[str, object] | None) -> dict[str, str]:
    """返回配置面板可见的 A2UI 字段。"""
    return {"a2ui_enabled": _bool_text(_a2ui_enabled_from_raw(raw_config))}


def get_default_a2ui_config_payload() -> dict[str, str]:
    """配置加载失败时的 A2UI 默认字段。"""
    return dict(_A2UI_CONFIG_DEFAULT_PAYLOAD)


def validate_a2ui_config_update(
    param_key: str,
    value: object,
) -> tuple[bool, dict[str, object], str]:
    """校验 Web 侧 A2UI 配置项，并映射为 config.yaml ``a2ui`` 段键。"""
    config_key = _WEB_CONFIG_KEY_MAP.get(param_key)
    if config_key is None:
        return False, {}, f"Unknown A2UI config key: {param_key}"
    return True, {config_key: _to_bool(value)}, ""

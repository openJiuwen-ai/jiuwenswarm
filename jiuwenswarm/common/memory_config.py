# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Pure memory configuration readers shared by routing and assembly."""
from typing import Any


def get_builtin_memory_provider(config: dict[str, Any] | None) -> str:
    """Read the provider without loading configuration or importing assembly code."""
    memory = (config or {}).get("memory", {}) if isinstance(config, dict) else {}
    builtin = memory.get("builtin", {}) if isinstance(memory, dict) else {}
    provider = builtin.get("provider") if isinstance(builtin, dict) else None
    return str(provider or "").strip()


def is_memory_portal_enabled(config: dict[str, Any] | None = None) -> bool:
    """Return whether the Portal UI is enabled, independently of memory operations."""
    if config is None:
        from jiuwenswarm.common.config import get_config

        config = get_config()
    memory = (config or {}).get("memory", {})
    builtin = memory.get("builtin", {}) if isinstance(memory, dict) else {}
    jiuwen = builtin.get("jiuwen", {}) if isinstance(builtin, dict) else {}
    portal = jiuwen.get("portal", {}) if isinstance(jiuwen, dict) else {}
    value = portal.get("enabled", False) if isinstance(portal, dict) else False
    return value is True or value == "true"

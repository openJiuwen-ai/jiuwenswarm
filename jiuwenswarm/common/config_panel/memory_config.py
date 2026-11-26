# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""The Portal UI switch; memory functionality keeps its existing configuration."""
from __future__ import annotations

from typing import Any

from jiuwenswarm.common.memory_config import is_memory_portal_enabled

MEMORY_FIELDS = {"memory_portal_enabled": (("memory", "builtin", "jiuwen", "portal", "enabled"), False)}


def flatten_memory_config(config: dict[str, Any]) -> dict[str, str]:
    return {"memory_portal_enabled": str(is_memory_portal_enabled(config)).lower()}


def parse_memory_updates(params: dict[str, Any]) -> dict[str, bool]:
    if "memory_portal_enabled" not in params:
        return {}
    value = params["memory_portal_enabled"]
    if not isinstance(value, bool) and value not in ("true", "false"):
        raise ValueError("memory_portal_enabled must be a boolean")
    return {"memory_portal_enabled": value is True or value == "true"}


def save_memory_updates(updates: dict[str, Any]) -> list[str]:
    if not updates:
        return []
    from jiuwenswarm.common import config

    data = config.load_yaml_round_trip(config.CONFIG_YAML_PATH)
    section = data
    for key in ("memory", "builtin", "jiuwen", "portal"):
        if not isinstance(section.get(key), dict):
            section[key] = {}
        section = section[key]
    section["enabled"] = updates["memory_portal_enabled"]
    config.dump_yaml_round_trip(config.CONFIG_YAML_PATH, data)
    return ["memory_portal_enabled"]

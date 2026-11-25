# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Pure memory configuration readers shared by routing and assembly."""
from typing import Any


def get_builtin_memory_provider(config: dict[str, Any] | None) -> str:
    """Read the provider without loading configuration or importing assembly code."""
    memory = (config or {}).get("memory", {}) if isinstance(config, dict) else {}
    builtin = memory.get("builtin", {}) if isinstance(memory, dict) else {}
    provider = builtin.get("provider") if isinstance(builtin, dict) else None
    return str(provider or "").strip()

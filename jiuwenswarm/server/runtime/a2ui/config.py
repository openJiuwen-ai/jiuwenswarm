# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Configuration helpers for the A2UI feature.

Dict/env parsing lives in Control so Front config queries never import Runtime.
``get_current_a2ui_config`` stays here because it reads the Runtime config cache.
"""

from __future__ import annotations

from jiuwenswarm.server.control.a2ui_config import (
    A2UIConfig,
    SUPPORTED_A2UI_PROTOCOL_VERSIONS,
    get_a2ui_config,
    is_a2ui_enabled,
)

__all__ = [
    "A2UIConfig",
    "SUPPORTED_A2UI_PROTOCOL_VERSIONS",
    "get_a2ui_config",
    "get_current_a2ui_config",
    "is_a2ui_enabled",
]


def get_current_a2ui_config() -> A2UIConfig:
    """Read A2UI config from the active agent snapshot, or local YAML."""
    from jiuwenswarm.enterprise.config.effective.apply_agent_capabilities import (
        resolved_agent_config_or_yaml,
    )

    return get_a2ui_config(resolved_agent_config_or_yaml())

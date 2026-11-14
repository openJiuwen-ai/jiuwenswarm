# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Task permission mode checks shared by Gateway config and the agent runtime.

These helpers project permission settings without loading Runtime configuration.
Runtime normalization stays in ``jiuwenswarm.agents``.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

logger = logging.getLogger(__name__)

MANUAL_PERMISSION_MODE = "manual"
AUTO_PERMISSION_MODE = "auto"
_VALID_RUNTIME_MODES = {MANUAL_PERMISSION_MODE, AUTO_PERMISSION_MODE}


def resolve_permission_runtime_mode(permission_config: Mapping[str, Any]) -> str:
    """Return the task permission runtime mode."""
    raw_mode = (
        str(permission_config.get("mode") or MANUAL_PERMISSION_MODE).strip().lower()
    )
    if raw_mode in _VALID_RUNTIME_MODES:
        return raw_mode
    logger.warning("Unknown permissions.mode %r; falling back to manual", raw_mode)
    return MANUAL_PERMISSION_MODE


def is_auto_permission_mode(permission_config: Mapping[str, Any]) -> bool:
    """Return whether task-level auto permission mode is selected."""
    return resolve_permission_runtime_mode(permission_config) == AUTO_PERMISSION_MODE


def permission_profile(permission_config: object) -> str:
    """Return the Web permission profile shared by config reads and writes."""
    if not isinstance(permission_config, dict) or permission_config.get("enabled") is not True:
        return "full_access"
    return "automatic" if is_auto_permission_mode(permission_config) else "default"


def canonical_permission_facade(profile: str) -> dict[str, str]:
    """Project both Web permission fields from the same canonical profile."""
    return {
        "permissions_profile": profile,
        "permissions_enabled": "false" if profile == "full_access" else "true",
    }


def is_permission_boundary_enabled(permission_config: Mapping[str, Any]) -> bool:
    """Return whether the permission boundary is explicitly enabled."""
    return permission_config.get("enabled") is True


def is_auto_permission_enabled(permission_config: Mapping[str, Any]) -> bool:
    """Return whether task-level auto permission is eligible to activate."""
    return is_permission_boundary_enabled(permission_config) and is_auto_permission_mode(
        permission_config
    )

# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Task permission mode checks shared by Gateway config and the agent runtime.

These helpers only read the ``permissions.mode`` string. Runtime normalization
stays in ``jiuwenswarm.agents``.
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


def is_permission_boundary_enabled(permission_config: Mapping[str, Any]) -> bool:
    """Return whether the permission boundary is explicitly enabled."""
    return permission_config.get("enabled") is True


def is_auto_permission_enabled(permission_config: Mapping[str, Any]) -> bool:
    """Return whether task-level auto permission is eligible to activate."""
    return is_permission_boundary_enabled(permission_config) and is_auto_permission_mode(
        permission_config
    )

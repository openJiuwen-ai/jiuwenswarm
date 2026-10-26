# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Runtime patch: honor LOG_TO_FILE_ENABLED for openjiuwen structured logging."""

from __future__ import annotations

import logging

logger = logging.getLogger("jiuwenswarm.openjiuwen_log_patch")

_LOG_TO_FILE_PATCH_APPLIED = False


def apply_openjiuwen_log_to_file_setting() -> None:
    """When ``LOG_TO_FILE_ENABLED=false``, keep openjiuwen console output only."""
    global _LOG_TO_FILE_PATCH_APPLIED
    if _LOG_TO_FILE_PATCH_APPLIED:
        return
    try:
        from openjiuwen.core.common.logging.log_config import (
            configure_log_config,
            get_log_config_snapshot,
        )
        from jiuwenswarm.infrastructure.config import Settings
    except ImportError:
        return

    if Settings().log_to_file_enabled:
        _LOG_TO_FILE_PATCH_APPLIED = True
        return

    config = get_log_config_snapshot()
    changed = False
    for key in ("output", "interface_output", "performance_output"):
        if config.get(key) != ["console"]:
            config[key] = ["console"]
            changed = True
    if changed:
        configure_log_config(config)
    _LOG_TO_FILE_PATCH_APPLIED = True
    logger.info("openjiuwen logging forced to console-only (LOG_TO_FILE_ENABLED=false)")

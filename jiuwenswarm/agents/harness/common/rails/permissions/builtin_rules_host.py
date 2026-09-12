# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Inline agent-core package builtin command rules into host permission snapshots.

Persist-time merge evaluates the snapshot without ``prepare_permissions_for_engine``.
Stamp ``layer: builtin`` from the openjiuwen package YAML before evaluate so
「永久记住」 still sees ASK. Do not vendor that YAML in swarm.
"""

from __future__ import annotations

import logging
from copy import deepcopy
from typing import Any

logger = logging.getLogger(__name__)


def _has_builtin_layer(rules: Any) -> bool:
    if not isinstance(rules, list):
        return False
    return any(isinstance(item, dict) and item.get("layer") == "builtin" for item in rules)


def with_package_builtin_rules(
    permissions: dict[str, Any] | None,
) -> dict[str, Any]:
    """Return a permissions dict that includes inlined package builtin command rules."""
    cfg = deepcopy(permissions) if isinstance(permissions, dict) else {}
    if _has_builtin_layer(cfg.get("rules")):
        return cfg
    try:
        from openjiuwen.harness.security.permission_engine.toolguard.builtin_rules import (
            inline_package_command_rules,
        )
    except ImportError:
        logger.debug(
            "[InterruptHelpers] permission.builtin_rules.package_import_failed",
            exc_info=True,
        )
        return cfg
    return inline_package_command_rules(cfg)

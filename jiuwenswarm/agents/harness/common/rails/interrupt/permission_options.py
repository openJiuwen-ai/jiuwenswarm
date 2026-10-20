# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Compatibility imports for extension permission answers."""

from jiuwenswarm.extensions.sdk.permission_options import (
    ALLOW_ONCE,
    ALWAYS_ALLOW,
    REJECT,
    SESSION_ALLOW,
    is_keep_planning_value,
    normalize_option_value,
    resolve_permission_action,
)

__all__ = [
    "ALLOW_ONCE",
    "ALWAYS_ALLOW",
    "REJECT",
    "SESSION_ALLOW",
    "is_keep_planning_value",
    "normalize_option_value",
    "resolve_permission_action",
]

# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Timeout policy helpers for browser runtime calls."""

from __future__ import annotations

import os
from typing import Optional


def allow_short_timeout_override() -> bool:
    value = (os.getenv("BROWSER_ALLOW_SHORT_TIMEOUT_OVERRIDE") or "").strip().lower()
    return value in {"1", "true", "yes", "on"}


def resolve_browser_task_timeout(
    requested_timeout_s: Optional[int],
    default_timeout_s: int,
) -> int:
    effective_default = max(1, int(default_timeout_s))
    parsed_timeout: Optional[int] = None
    if requested_timeout_s is not None:
        try:
            candidate = int(requested_timeout_s)
            if candidate > 0:
                parsed_timeout = candidate
        except (TypeError, ValueError):
            parsed_timeout = None

    if parsed_timeout is None:
        return effective_default

    if allow_short_timeout_override():
        return parsed_timeout

    return max(parsed_timeout, effective_default)

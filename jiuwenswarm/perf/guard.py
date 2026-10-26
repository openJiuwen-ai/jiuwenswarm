# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Best-effort perf helpers — failures must never break the agent path."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


def run_perf_safe(
    component: str,
    action: str,
    callback: Callable[[], T],
    *,
    default: T | None = None,
) -> T | None:
    """Run a perf-side effect; log and return default instead of raising."""
    try:
        return callback()
    except Exception as exc:
        logger.warning("[%s] %s failed: %s", component, action, exc)
        return default

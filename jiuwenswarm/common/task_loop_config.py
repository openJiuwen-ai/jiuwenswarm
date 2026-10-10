# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Task-loop configuration helpers."""

from __future__ import annotations

import math
from typing import Any


def resolve_task_loop_completion_timeout(config: dict[str, Any]) -> float | None:
    """Resolve the per-round timeout; an omitted/null value means no limit."""
    raw = config.get("completion_timeout")
    if raw is None or raw == "":
        return None
    if isinstance(raw, bool):
        raise ValueError("react.completion_timeout must be a positive number or null")
    try:
        timeout = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "react.completion_timeout must be a positive number or null"
        ) from exc
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("react.completion_timeout must be greater than zero or null")
    return timeout

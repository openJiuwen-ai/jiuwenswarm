# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Guard Team terminal deletion ordering around Session-owned KVC actions."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)
StopRuntime = Callable[..., Awaitable[bool]]


def is_enabled() -> bool:
    try:
        from jiuwenswarm.server.runtime.session.kv_cache.kv_cache_model_provider import (
            is_kv_cache_affinity_enabled,
        )

        return is_kv_cache_affinity_enabled()
    except Exception as exc:
        logger.warning(
            "[TeamKVC] affinity gate failed; preserving lifecycle: %s",
            exc,
        )
        return False


async def stop_runtime_before_terminal_delete(
    stop_runtime: StopRuntime,
    *,
    session_id: str,
    reason: str,
) -> bool:
    """Drain work but retain the live model until Session KVC release runs."""
    if is_enabled():
        return await stop_runtime(session_id, reason=reason, stop_runner=False)
    return await stop_runtime(session_id, reason=reason)


__all__ = ["is_enabled", "stop_runtime_before_terminal_delete"]

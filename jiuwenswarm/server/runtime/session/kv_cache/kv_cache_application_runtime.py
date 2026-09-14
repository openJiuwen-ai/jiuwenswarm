# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Application ownership for the process-local Agent-Core KVC runtime."""

from __future__ import annotations

import asyncio
import logging

from openjiuwen.core.kv_cache.kv_cache_config import KVC_TERMINAL_CLEANUP_TIMEOUT_SECONDS
from openjiuwen.core.kv_cache.kv_cache_runtime import KVCacheRuntime

logger = logging.getLogger(__name__)

_runtime: KVCacheRuntime | None = None
_default_model = None


def _get_default_model():
    global _default_model
    if _default_model is None:
        from jiuwenswarm.server.runtime.session.kv_cache.kv_cache_model_provider import (
            create_default_kv_cache_model,
        )

        _default_model = create_default_kv_cache_model()
    return _default_model


def get_kv_cache_runtime() -> KVCacheRuntime | None:
    try:
        from jiuwenswarm.server.runtime.session.kv_cache.kv_cache_model_provider import (
            is_kv_cache_affinity_enabled,
        )

        affinity_enabled = is_kv_cache_affinity_enabled()
    except Exception as exc:
        logger.warning("KVC runtime configuration failed; keep KVC disabled: %s", exc)
        return None
    if not affinity_enabled:
        return None

    global _runtime
    try:
        if _runtime is None or _runtime.closed:
            _runtime = KVCacheRuntime(binding_provider=_get_default_model)
    except Exception as exc:
        logger.warning("KVC runtime initialization failed; keep KVC disabled: %s", exc)
        return None
    return _runtime


async def close_kv_cache_runtime() -> None:
    global _default_model, _runtime
    runtime, _runtime = _runtime, None
    _default_model = None
    if runtime is None:
        return
    task = asyncio.create_task(runtime.close(), name="kvc-runtime-close")
    try:
        # asyncio.wait keeps the budget bounded even if provider cleanup is
        # slow to acknowledge cancellation.
        done, _pending = await asyncio.wait(
            {task}, timeout=KVC_TERMINAL_CLEANUP_TIMEOUT_SECONDS
        )
        if done:
            # Cancellation of this child task is a KVC result, not a request
            # to cancel server.stop(). Cancellation of our caller still
            # propagates from asyncio.wait above.
            _consume_shutdown_result(task)
        else:
            logger.warning("KVC runtime shutdown timed out; continue server shutdown")
    except Exception as exc:
        logger.warning("KVC runtime shutdown failed; continue server shutdown: %s", exc)
    finally:
        if not task.done():
            task.cancel()
            task.add_done_callback(_consume_shutdown_result)


def _consume_shutdown_result(task: asyncio.Task[None]) -> None:
    try:
        task.result()
    except asyncio.CancelledError:
        pass
    except Exception as exc:
        logger.warning("KVC background shutdown failed: %s", exc)


__all__ = ["close_kv_cache_runtime", "get_kv_cache_runtime"]

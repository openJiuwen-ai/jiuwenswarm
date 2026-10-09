# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""工作区用量后台校准：定时全量 du，离开对话热路径。"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from jiuwenswarm.common.workspace.quota import (
    list_usage_reconcile_targets,
    measure_and_cache,
    reconcile_interval_seconds,
)

logger = logging.getLogger(__name__)

_TASK: asyncio.Task | None = None
_STOP: asyncio.Event | None = None


async def _push_gateway_usage(
    *,
    user_id: str,
    group_id: str,
    bot_id: str,
    used_bytes: int,
) -> None:
    if not (user_id and bot_id):
        return
    try:
        from jiuwenswarm.gateway.workspace.quota import upsert_usage_cache

        await upsert_usage_cache(
            user_id=user_id,
            group_id=group_id,
            bot_id=bot_id,
            used_bytes=used_bytes,
        )
    except Exception:  # noqa: BLE001
        logger.debug(
            "[workspace.quota] gateway usage upsert skipped",
            exc_info=True,
        )


async def _reconcile_one(entry: Any) -> None:
    root = Path(entry.root)
    try:
        used = await asyncio.to_thread(
            measure_and_cache,
            root,
            user_id=entry.user_id,
            group_id=entry.group_id,
            bot_id=entry.bot_id,
        )
    except Exception:  # noqa: BLE001
        logger.warning(
            "[workspace.quota] reconcile du failed root=%s",
            root,
            exc_info=True,
        )
        return
    await _push_gateway_usage(
        user_id=entry.user_id,
        group_id=entry.group_id,
        bot_id=entry.bot_id,
        used_bytes=used,
    )
    logger.debug(
        "[workspace.quota] reconciled root=%s used=%s",
        root,
        used,
    )


async def _reconcile_loop(stop: asyncio.Event) -> None:
    logger.info(
        "[workspace.quota] usage reconciler started interval=%ss",
        reconcile_interval_seconds(),
    )
    while not stop.is_set():
        try:
            targets = list_usage_reconcile_targets()
            for entry in targets[:8]:
                if stop.is_set():
                    break
                await _reconcile_one(entry)
        except Exception:  # noqa: BLE001
            logger.warning(
                "[workspace.quota] reconcile loop iteration failed",
                exc_info=True,
            )
        interval = reconcile_interval_seconds()
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except asyncio.TimeoutError:
            continue
    logger.info("[workspace.quota] usage reconciler stopped")


def start_usage_reconciler() -> asyncio.Task | None:
    """幂等启动后台校准任务；无运行中 loop 时返回 None。"""
    global _TASK, _STOP
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.debug("[workspace.quota] no running loop; reconciler not started")
        return None
    if _TASK is not None and not _TASK.done():
        return _TASK
    _STOP = asyncio.Event()
    _TASK = loop.create_task(_reconcile_loop(_STOP), name="workspace-usage-reconciler")
    return _TASK


async def stop_usage_reconciler() -> None:
    """停止后台校准任务。"""
    global _TASK, _STOP
    task = _TASK
    stop = _STOP
    _TASK = None
    _STOP = None
    if stop is not None:
        stop.set()
    if task is None:
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    except Exception:  # noqa: BLE001
        logger.debug("[workspace.quota] reconciler stop error", exc_info=True)

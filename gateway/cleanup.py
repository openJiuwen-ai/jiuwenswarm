# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.

"""Periodic background cleanup for old session data and file_ops logs.

Mirrors claude-code's ``cleanup.ts`` approach: periodically check directory mtime
and remove entries older than the configured retention period.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path
from typing import Callable

from gateway.common.config import get_config
from gateway.common.paths import (
    get_agent_sessions_dir,
    get_agent_workspace_dir,
    get_user_workspace_dir,
)

logger = logging.getLogger(__name__)

DEFAULT_CLEANUP_PERIOD_DAYS = 30
RECURRING_CLEANUP_INTERVAL_S = 24 * 60 * 60
FIRST_CLEANUP_DELAY_S = 10 * 60
AGENT_ID = "jiuwenswarm"


def _get_cleanup_period_days() -> int:
    """Resolve session retention; zero disables automatic cleanup.

    The legacy environment variable takes precedence over configuration.
    Invalid or negative values fall back to the historical default.
    """
    section = get_config().get("session_cleanup", {})
    configured = section.get("retention_days") if isinstance(section, dict) else None
    value = os.getenv("CLEANUP_PERIOD_DAYS") or configured
    try:
        days = int(value)
    except (ValueError, TypeError):
        return DEFAULT_CLEANUP_PERIOD_DAYS
    return days if days >= 0 else DEFAULT_CLEANUP_PERIOD_DAYS


def _get_cutoff_timestamp() -> float:
    days = _get_cleanup_period_days()
    return time.time() - days * 24 * 60 * 60


def _rmtree(path: Path) -> None:
    import shutil
    shutil.rmtree(path, ignore_errors=True)


def cleanup_old_sessions() -> dict[str, int]:
    """Remove expired session directories unless retention is disabled.

    Returns:
        { "removed": N, "errors": N }
    """
    if _get_cleanup_period_days() == 0:
        return {"removed": 0, "errors": 0}
    cutoff = _get_cutoff_timestamp()
    sessions_dir = get_agent_sessions_dir()
    if not sessions_dir.is_dir():
        return {"removed": 0, "errors": 0}

    removed = 0
    errors = 0

    for entry in sessions_dir.iterdir():
        if not entry.is_dir():
            continue
        try:
            if entry.stat().st_mtime >= cutoff:
                continue
            _rmtree(entry)
            removed += 1
            logger.info("cleanup_old_sessions: removed %s", entry.name)
        except OSError:
            errors += 1

    if removed or errors:
        logger.info(
            "cleanup_old_sessions: removed=%d errors=%d cutoff_ts=%.0f",
            removed, errors, cutoff,
        )
    return {"removed": removed, "errors": errors}


def cleanup_orphan_file_ops() -> dict[str, int]:
    """Remove orphan file-operation logs unless retention is disabled.

    Returns:
        { "removed": N, "errors": N }
    """
    if _get_cleanup_period_days() == 0:
        return {"removed": 0, "errors": 0}
    sessions_dir = get_agent_sessions_dir()
    existing_sessions: set[str] = set()
    if sessions_dir.is_dir():
        for entry in sessions_dir.iterdir():
            if entry.is_dir():
                existing_sessions.add(entry.name)

    removed = 0
    errors = 0

    for base_dir in (get_agent_workspace_dir(), get_user_workspace_dir()):
        hist_dir = base_dir / ".agent_history"
        if not hist_dir.is_dir():
            continue
        for f in hist_dir.iterdir():
            if not f.is_file():
                continue
            name = f.name
            if not name.startswith(f"file_ops_{AGENT_ID}_") or not name.endswith(".json"):
                continue
            # 检查文件名中是否包含某个现存 session 的 id
            orphaned = True
            for sid in existing_sessions:
                if sid in name:
                    orphaned = False
                    break
            if not orphaned:
                continue
            try:
                f.unlink()
                removed += 1
                logger.info("cleanup_orphan_file_ops: removed %s", f.name)
            except OSError:
                errors += 1

    if removed or errors:
        logger.info(
            "cleanup_orphan_file_ops: removed=%d errors=%d",
            removed, errors,
        )
    return {"removed": removed, "errors": errors}


async def run_cleanup() -> None:
    """执行一次完整的清理周期。"""
    loop = asyncio.get_running_loop()
    try:
        await loop.run_in_executor(None, cleanup_old_sessions)
    except Exception:
        logger.warning("cleanup_old_sessions failed", exc_info=True)

    try:
        await loop.run_in_executor(None, cleanup_orphan_file_ops)
    except Exception:
        logger.warning("cleanup_orphan_file_ops failed", exc_info=True)


async def cleanup_loop(
    stop_event: asyncio.Event,
    on_first_done: Callable[[], None] | None = None,
) -> None:
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=FIRST_CLEANUP_DELAY_S)
        return
    except asyncio.TimeoutError:
        pass

    first = True
    while not stop_event.is_set():
        logger.info("background_cleanup: starting cleanup cycle")
        await run_cleanup()
        if first and on_first_done is not None:
            on_first_done()
            first = False
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=RECURRING_CLEANUP_INTERVAL_S)
            return
        except asyncio.TimeoutError:
            pass


def start_background_cleanup(
    on_first_done: Callable[[], None] | None = None,
) -> asyncio.Task:
    stop_event = asyncio.Event()
    task = asyncio.create_task(
        cleanup_loop(stop_event, on_first_done=on_first_done),
        name="background-cleanup",
    )
    task.add_done_callback(lambda _: stop_event.set())
    logger.info(
        "background_cleanup: started (first_delay=%ds, interval=%ds, retention=%dd)",
        FIRST_CLEANUP_DELAY_S,
        RECURRING_CLEANUP_INTERVAL_S,
        _get_cleanup_period_days(),
    )
    return task

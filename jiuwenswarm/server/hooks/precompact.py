# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""PreCompact hook dispatch, fired before context compaction (manual or auto)."""

from __future__ import annotations

import asyncio
import logging

from jiuwenswarm.common.hooks_config import HookEvent, load_hooks_config
from jiuwenswarm.server.hooks.executor import HookExecutor, HookResult

logger = logging.getLogger(__name__)

# Keep references to fire-and-forget tasks so they are not garbage collected.
_background_tasks: set[asyncio.Task] = set()


async def fire_pre_compact_hooks(
    trigger: str,
    session_id: str,
    custom_instructions: str = "",
) -> list[HookResult]:
    """Run PreCompact hooks matching the trigger ("manual" or "auto").

    Never raises: any failure is logged and returns [] so a broken hook
    setup cannot break a compaction path.
    """
    try:
        hooks_config = load_hooks_config()
        hook_configs = hooks_config.match(HookEvent.PRE_COMPACT.value, query=trigger)
        if not hook_configs:
            return []

        results = await HookExecutor().run_all(
            hook_configs,
            hook_input={
                "event": HookEvent.PRE_COMPACT.value,
                "trigger": trigger,
                "custom_instructions": custom_instructions,
                "session_id": session_id,
            },
            session_id=session_id,
        )
        for r in results:
            if r.outcome == "blocking":
                logger.info(
                    "PreCompact hook BLOCKED compaction trigger=%s reason=%s",
                    trigger, r.error,
                )
        return results
    except Exception:  # noqa: BLE001
        logger.warning("PreCompact hook dispatch failed (trigger=%s)", trigger, exc_info=True)
        return []


def fire_pre_compact_hooks_background(trigger: str, session_id: str) -> None:
    """Fire-and-forget variant for auto compaction (cannot block)."""
    task = asyncio.create_task(fire_pre_compact_hooks(trigger, session_id))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)

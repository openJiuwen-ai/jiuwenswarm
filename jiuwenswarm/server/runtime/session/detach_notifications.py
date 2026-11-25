# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Bounded Worker notifications after the platform commits a view release."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import logging
from typing import TYPE_CHECKING

from openjiuwen.core.events import Event, SessionContext

from jiuwenswarm.extensions_v2.event_types.session_detach import (
    SESSION_DETACH_EVENT,
    DetachInvocation,
    SessionDetachInput,
    bind_detach_invocation,
)

if TYPE_CHECKING:
    from openjiuwen.core.events import ExtensionEventBus
    from jiuwenswarm.extensions_v2.management import ExtensionManager

logger = logging.getLogger(__name__)


async def notify_session_detach(
    value: SessionDetachInput,
    context: SessionContext,
    is_released: Callable[[], bool],
    *,
    timeout_seconds: float,
    tasks: set[asyncio.Task],
    task_done: Callable[[asyncio.Task], None],
) -> None:
    """Observe committed removal; failure cannot restore an old association."""
    from jiuwenswarm.extensions_v2.assembly import get_extension_bus, get_extension_manager

    bus, manager = get_extension_bus(), get_extension_manager()
    if bus is None or manager is None or not manager.initialized:
        return
    state = DetachInvocation(value, is_released, context.session_id)
    task = asyncio.create_task(_dispatch(bus, manager, state, context), name="extension-session-detach")
    tasks.add(task)
    task.add_done_callback(task_done)
    try:
        done, _pending = await asyncio.wait({task}, timeout=timeout_seconds)
    except BaseException:
        state.active = False
        task.cancel()
        raise
    if not done:
        state.active = False
        task.cancel()
        logger.warning("[ExtensionsV2] session.detach timed out; release remains: client=%s", value.clientId)
        return
    try:
        task.result()
    except asyncio.CancelledError:
        logger.warning("[ExtensionsV2] session.detach cancelled; release remains: client=%s", value.clientId)
    except Exception:
        logger.warning("[ExtensionsV2] session.detach notification failed: client=%s", value.clientId, exc_info=True)


async def _dispatch(
    bus: ExtensionEventBus,
    manager: ExtensionManager,
    state: DetachInvocation,
    context: SessionContext,
) -> None:
    runtime = manager.registry.runtime
    packages = tuple(record.package_id for record in runtime.active_packages())
    runtime.begin_calls(packages)
    try:
        with bind_detach_invocation(state):
            result = await bus.trigger(Event(SESSION_DETACH_EVENT, state.original), context)
            if not state.is_released() or state.receipt is None or result != state.receipt:
                raise RuntimeError("session.detach requires the actual CORE release receipt")
    finally:
        runtime.end_calls(packages)

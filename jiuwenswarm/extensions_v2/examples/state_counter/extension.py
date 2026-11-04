# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""A persistent counter updated by clicks and completed conversation rounds."""

import asyncio
import logging
from collections import deque
from dataclasses import replace

from openjiuwen.core.events import Event, EventHandlerOps, EventNext
from openjiuwen.core.extension import ExtensionRegistrar
from openjiuwen.core.extension.data import StateRef, StateSetResult

from jiuwenswarm.extensions_v2.event_types.context_usage import (
    CONTEXT_USAGE_EVENT, ContextUsageOutput, ContextUsageUpdate,
)

from jiuwenswarm.extensions_v2.webui.models import (
    RenderMode, RenderNode, RenderStatus, RenderTarget, UiActionOutput, UiRenderInput,
)

PACKAGE_ID = "sample.counter"
SLOT = "conversation.input.below"
INSTANCE = "counter.panel"
COUNT = StateRef(PACKAGE_ID, "count")
PANEL = {"target.package_id": PACKAGE_ID, "target.slot_id": SLOT, "target.instance_key": INSTANCE}
logger = logging.getLogger(__name__)


class StateCounter:
    """Expose the state API through the host's existing ready and click events."""

    def __init__(self) -> None:
        self._increment_lock = asyncio.Lock()
        self._completed: deque[tuple[str, str]] = deque(maxlen=256)

    def register(self, registrar: ExtensionRegistrar) -> None:
        registrar.register_event_handler("ui.ready", self.on_ready, {"target.slot_id": SLOT}, "user")
        registrar.register_event_handler("ui.render", self.on_render, PANEL, "user")
        registrar.register_event_handler("ui.click", self.on_increment, PANEL, "user")
        registrar.register_event_handler(CONTEXT_USAGE_EVENT, self.on_round_completed, {}, "user")

    async def on_ready(self, event: Event, ops: EventHandlerOps, next_handler: EventNext) -> object:
        receipt = await next_handler(event)
        if not receipt.accepted or ops.session_context is None:
            return receipt
        snapshot = await ops.state.get(COUNT)
        if not snapshot.exists:
            initialized = await ops.state.set(COUNT, 0, expected_version=snapshot.version)
            if not initialized.is_set and initialized.reason != "version_conflict":
                return UiActionOutput(False, initialized.reason or "initialize_failed")
        target = RenderTarget(ops.session_context.session_id, SLOT, PACKAGE_ID, INSTANCE)
        output = await ops.ui.render(UiRenderInput(target))
        if output is None or output.status is not RenderStatus.DELIVERED:
            return UiActionOutput(False, "render_failed")
        return receipt

    async def on_render(self, event: Event, ops: EventHandlerOps, next_handler: EventNext) -> object:
        request = event.event_input
        if request.mode is RenderMode.CLEAR or request.root is not None:
            return await next_handler(event)
        snapshot = await ops.state.get(COUNT)
        count = snapshot.value if snapshot.exists else 0
        tree = RenderNode("Box", {"direction": "row", "gap": "sm"}, children=(
            RenderNode("Text", {"value": f"Count: {count} (state version {snapshot.version})"}),
            RenderNode("Button", {"label": "+1"}, events={"ui.click": {"id": "increment", "args": {}}}),
        ))
        return await next_handler(Event("ui.render", replace(request, root=tree)))

    async def on_increment(self, event: Event, ops: EventHandlerOps, next_handler: EventNext) -> object:
        receipt = await next_handler(event)
        if not receipt.accepted or event.event_input.payload.get("action_id") != "increment":
            return receipt
        async with self._increment_lock:
            result = await self._increment(ops)
        return receipt if result.is_set else UiActionOutput(False, result.reason or "save_failed")

    async def on_round_completed(self, event: Event, ops: EventHandlerOps, next_handler: EventNext) -> object:
        receipt = await next_handler(event)
        update = event.event_input
        if (not isinstance(receipt, ContextUsageOutput) or not receipt.accepted
                or not isinstance(update, ContextUsageUpdate) or ops.session_context is None
                or update.session_id != ops.session_context.session_id):
            return receipt
        identity = (update.session_id, update.request_id)
        async with self._increment_lock:
            if identity in self._completed:
                return receipt
            result = await self._increment(ops)
            if result.is_set:
                self._completed.append(identity)
            else:
                logger.warning("[state-counter] completed round state update failed: %s", result.reason)
        return receipt

    @staticmethod
    async def _increment(ops: EventHandlerOps) -> StateSetResult:
        snapshot = await ops.state.get(COUNT)
        return await ops.state.set(
            COUNT, (snapshot.value if snapshot.exists else 0) + 1, expected_version=snapshot.version,
        )


def create_extension() -> StateCounter:
    return StateCounter()

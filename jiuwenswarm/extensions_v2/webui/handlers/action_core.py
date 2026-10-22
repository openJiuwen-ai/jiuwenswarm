# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Upstream ui.* CORE handler: check instance activity and return a receipt."""

from __future__ import annotations

import logging

from openjiuwen.core.events import Event, EventHandler, EventHandlerOps, EventNext

from jiuwenswarm.extensions_v2.webui.constants import CODE_TARGET_INACTIVE, SLOT_READY_EVENT
from jiuwenswarm.extensions_v2.webui.models import UiActionInput, UiActionOutput
from jiuwenswarm.extensions_v2.webui.registry import ActiveRenderRegistry

logger = logging.getLogger(__name__)


def build_action_core_handler(registry: ActiveRenderRegistry) -> EventHandler:
    """Build the CORE handler shared by all upstream events."""

    async def ui_action_core(
        event: Event,
        ops: EventHandlerOps | None,
        next_handler: EventNext,
    ) -> UiActionOutput:
        del ops, next_handler
        request = event.event_input
        if not isinstance(request, UiActionInput):
            raise TypeError(f"{event.event_type} requires UiActionInput")

        if request.event_type == SLOT_READY_EVENT:
            # A slot mount report has no instance yet; the ready handler creates one.
            return UiActionOutput(accepted=True, status="ready")

        if not registry.contains(request.target):
            logger.debug("[WebUI] ignoring upstream event for inactive target: %s", request.target.key)
            return UiActionOutput(accepted=False, status="inactive", code=CODE_TARGET_INACTIVE)

        return UiActionOutput(accepted=True, status="handled")

    return ui_action_core
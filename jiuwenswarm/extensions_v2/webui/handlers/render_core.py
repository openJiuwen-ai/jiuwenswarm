# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""``ui.render`` CORE handler: validate, assign revision and deliver one frame."""

from __future__ import annotations

import logging
from dataclasses import replace

from openjiuwen.core.events import Event, EventHandler, EventHandlerOps, EventNext

from jiuwenswarm.extensions_v2.webui.constants import CODE_SCHEMA_ERROR
from jiuwenswarm.extensions_v2.webui.models import (
    ClearFrame,
    RenderFrame,
    RenderMode,
    RenderStatus,
    UiRenderInput,
    UiRenderOutput,
)
from jiuwenswarm.extensions_v2.webui.registry import ActiveRenderRegistry, RenderInstance, log_eviction
from jiuwenswarm.extensions_v2.webui.schema import frame_payload_bytes, validate_request
from jiuwenswarm.extensions_v2.webui.transport import RenderTransport

logger = logging.getLogger(__name__)


def build_render_core_handler(
    transport: RenderTransport,
    registry: ActiveRenderRegistry,
) -> EventHandler:
    """Build the single CORE handler for ui.render."""

    async def ui_render_core(
        event: Event,
        ops: EventHandlerOps | None,
        next_handler: EventNext,
    ) -> UiRenderOutput:
        del ops, next_handler
        request = event.event_input
        if not isinstance(request, UiRenderInput):
            raise TypeError("ui.render requires UiRenderInput")

        report = validate_request(request)
        if not report.ok:
            await transport.send_error(request.target, report.code or CODE_SCHEMA_ERROR, report.message)
            return UiRenderOutput(status=RenderStatus.REJECTED)

        async with registry.claim(request.target) as claim:
            previous = replace(claim.instance)
            preview = RenderFrame(
                target=claim.instance.target,
                revision=claim.instance.revision + 1,
                mode=request.mode,
                root=request.root,
            )
            report = frame_payload_bytes(preview)
            if not report.ok:
                if claim.created:
                    registry.drop_instance(claim.instance.target)
                await transport.send_error(
                    request.target,
                    report.code or CODE_SCHEMA_ERROR,
                    report.message,
                    revision=preview.revision,
                )
                return UiRenderOutput(status=RenderStatus.REJECTED)

            frame = registry.commit(claim.instance, request)
            if request.mode is RenderMode.CLEAR:
                delivered = await transport.send_clear(
                    ClearFrame(target=request.target, revision=frame.revision, reason="requested")
                )
            else:
                delivered = await transport.send_frame(frame)

            if not delivered:
                logger.warning("[WebUI] frame delivery failed: %s", request.target.key)
                registry.restore(claim.instance, previous)
                if claim.created:
                    registry.drop_instance(request.target)
                return UiRenderOutput(
                    status=RenderStatus.DELIVERY_FAILED,
                    revision=previous.revision,
                )

            if request.mode is RenderMode.CLEAR:
                registry.drop_instance(request.target)

            for evicted in claim.evicted:
                async with registry.evict(evicted) as current:
                    if current and await transport.send_clear(clear_frame(evicted, reason="evicted")):
                        registry.drop_instance(evicted.target)
                        log_eviction(evicted)

        return UiRenderOutput(status=RenderStatus.DELIVERED, revision=frame.revision)

    return ui_render_core


def clear_frame(instance: RenderInstance, *, reason: str = "") -> ClearFrame:
    """Build a clear frame so the browser removes this instance."""

    return ClearFrame(target=instance.target, revision=instance.revision + 1, reason=reason)

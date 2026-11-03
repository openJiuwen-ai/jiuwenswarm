# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""``ui.render`` CORE handler: validate, assign revision and deliver one frame.

Copied from ``jiuwenswarm/extensions_v2/webui/core_handlers/render_core.py``.
The trusted connection id comes from the event's ``SessionContext`` metadata
instead of an AgentServer context variable, because the Gateway owns the socket.
"""

from __future__ import annotations

import logging
from dataclasses import replace

from openjiuwen.core.events import Event, EventHandler, EventHandlerOps, EventNext

from jiuwenswarm.gateway.extensions_v2.webui.call_context import trusted_connection
from jiuwenswarm.gateway.extensions_v2.webui.constants import CODE_SCHEMA_ERROR, SlotScope, slot_spec
from jiuwenswarm.gateway.extensions_v2.webui.models import (
    ClearFrame,
    RenderFrame,
    RenderMode,
    RenderStatus,
    RenderTarget,
    UiRenderInput,
    UiRenderOutput,
)
from jiuwenswarm.gateway.extensions_v2.webui.registry import (
    ActiveRenderRegistry,
    RenderInstance,
    log_eviction,
)
from jiuwenswarm.gateway.extensions_v2.webui.schema import (
    apply_slot_scope,
    frame_payload_bytes,
    validate_request,
)
from jiuwenswarm.gateway.extensions_v2.webui.transport import GatewayTransport

logger = logging.getLogger(__name__)


def build_render_core_handler(
    transport: GatewayTransport,
    registry: ActiveRenderRegistry,
) -> EventHandler:
    """Build the single CORE handler for ui.render."""

    async def ui_render_core(
        event: Event,
        ops: EventHandlerOps | None,
        next_handler: EventNext,
    ) -> UiRenderOutput:
        del next_handler
        request = event.event_input
        if not isinstance(request, UiRenderInput):
            raise TypeError("ui.render requires UiRenderInput")

        trusted = trusted_connection(ops)
        bound, report = apply_slot_scope(request.target, trusted)
        if not report.ok:
            await transport.send_error(
                _error_target(bound, trusted),
                report.code or CODE_SCHEMA_ERROR,
                report.message,
            )
            return UiRenderOutput(status=RenderStatus.REJECTED)
        request = replace(request, target=bound)

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
                logger.warning("[GatewayWebUI] frame delivery failed: %s", request.target.key)
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


def _error_target(target: RenderTarget, trusted_connection_id: str) -> RenderTarget:
    """Route rejected targets according to the slot's scope."""

    spec = slot_spec(target.slot_id)
    if spec is not None and spec.scope is SlotScope.SESSION:
        return replace(target, scope=SlotScope.SESSION.value, connection_id="")
    if spec is not None and spec.scope is SlotScope.APPLICATION and trusted_connection_id:
        return replace(
            target,
            scope=SlotScope.APPLICATION.value,
            session_id="",
            connection_id=trusted_connection_id,
        )
    return target


def clear_frame(instance: RenderInstance, *, reason: str = "") -> ClearFrame:
    """Build a clear frame so the browser removes this instance."""

    return ClearFrame(target=instance.target, revision=instance.revision + 1, reason=reason)

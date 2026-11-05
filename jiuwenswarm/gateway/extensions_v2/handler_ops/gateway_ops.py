# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""``ops.build`` handler that exposes the ``gateway`` namespace."""

from __future__ import annotations

from openjiuwen.core.events import (
    Event,
    EventHandler,
    EventHandlerOps,
    EventHandlerOpsBuilder,
    EventNext,
    ExtensionEventBus,
)

from jiuwenswarm.gateway.extensions_v2.models import EVENT_GATEWAY_REQUEST, GatewayRequestInput


def build_gateway_ops_build_handler(bus: ExtensionEventBus) -> EventHandler:
    """Publish ``ops.gateway.request``."""

    async def gateway_request(ops_view: EventHandlerOps, request: GatewayRequestInput) -> object:
        return await bus.trigger(Event(EVENT_GATEWAY_REQUEST, request), ops_view.session_context)

    async def gateway_ops_build(
        event: Event,
        ops: EventHandlerOps | None,
        next_handler: EventNext,
    ) -> EventHandlerOpsBuilder:
        del ops
        builder = await next_handler(event)
        if not isinstance(builder, EventHandlerOpsBuilder):
            raise TypeError("ops.build must return EventHandlerOpsBuilder")
        builder.add_namespace("gateway")
        builder.add_method("gateway", "request", gateway_request)
        return builder

    return gateway_ops_build

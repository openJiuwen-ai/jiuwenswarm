# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""``gateway.request`` CORE handler: validate the frame and default to forward."""

from __future__ import annotations

from openjiuwen.core.events import Event, EventHandler, EventHandlerOps, EventNext

from jiuwenswarm.gateway.extensions_v2.models import GatewayRequestInput, GatewayRequestOutput, GatewayRoute

CODE_BAD_REQUEST = "BAD_REQUEST"


def build_request_core_handler() -> EventHandler:
    """Build the CORE handler shared by every ``gateway.request`` call."""

    async def gateway_request_core(
        event: Event,
        ops: EventHandlerOps | None,
        next_handler: EventNext,
    ) -> GatewayRequestOutput:
        del ops, next_handler
        request = event.event_input
        if not isinstance(request, GatewayRequestInput):
            raise TypeError("gateway.request requires GatewayRequestInput")
        if not isinstance(request.method, str) or not request.method.strip():
            return GatewayRequestOutput(
                handled=True,
                route=GatewayRoute.DENY.value,
                error={"code": CODE_BAD_REQUEST, "message": "method is required"},
            )
        # No platform decision: the frame keeps the native path unless a
        # handler replaced this result.
        return GatewayRequestOutput(handled=False, route=GatewayRoute.FORWARD.value)

    return gateway_request_core

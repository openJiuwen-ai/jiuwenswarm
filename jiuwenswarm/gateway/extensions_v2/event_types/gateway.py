# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""``gateway.request`` event type: one WS frame before native dispatch."""

from __future__ import annotations

from openjiuwen.core.events import EventHandler, ExtensionEventBus

from jiuwenswarm.gateway.extensions_v2.event_types.matching import match_pattern_subset
from jiuwenswarm.gateway.extensions_v2.models import EVENT_GATEWAY_REQUEST


def register_gateway_request_events(bus: ExtensionEventBus, core_handler: EventHandler) -> None:
    """Register ``gateway.request`` with its CORE handler."""

    bus.register_event_type(EVENT_GATEWAY_REQUEST, match_pattern_subset, core_handler)

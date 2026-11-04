# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Register Gateway event types, CORE handlers and BUILTIN handlers.

The Gateway mirrors AgentServer's bootstrap: one function adds event types
together with their CORE handler, a second adds the platform BUILTIN handlers
that observe existing event types. A host calls both before the runtime
publishes packages, so a package always meets a fully registered bus.

The WebUI family (``ui.render``) is registered by
:class:`GatewayWebUIRuntime`, exactly like AgentServer delegates its ``ui.*``
events to ``WebUIRuntime``. The ``http`` namespace is added here because
outbound requests carry no WebUI state.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from openjiuwen.core.events import OPS_BUILD_EVENT_TYPE, ExtensionEventBus, HandlerLevel

from jiuwenswarm.gateway.extensions_v2.builtin.auth_gate import build_auth_gate_handler
from jiuwenswarm.gateway.extensions_v2.core_handlers.request_core import build_request_core_handler
from jiuwenswarm.gateway.extensions_v2.event_types.gateway import register_gateway_request_events
from jiuwenswarm.gateway.extensions_v2.handler_ops.gateway_ops import build_gateway_ops_build_handler
from jiuwenswarm.gateway.extensions_v2.handler_ops.http_ops import build_http_ops_build_handler
from jiuwenswarm.gateway.extensions_v2.models import EVENT_GATEWAY_REQUEST, EVENT_GATEWAY_DISCONNECT, EVENT_UI_RENDER
from jiuwenswarm.gateway.extensions_v2.event_types.matching import match_pattern_subset

if TYPE_CHECKING:
    from jiuwenswarm.gateway.extensions_v2.runtime import GatewayRuntime


def register_event_types(bus: ExtensionEventBus, runtime: GatewayRuntime) -> None:
    """Register event types and their CORE handlers."""

    runtime.webui.register_event_types(bus)
    register_gateway_request_events(bus, build_request_core_handler())
    runtime.browser.register_events(bus)
    async def disconnect_core(event, ops, next_handler):
        del event, ops, next_handler
        return None
    bus.register_event_type(EVENT_GATEWAY_DISCONNECT, match_pattern_subset, disconnect_core)


def register_handlers(bus: ExtensionEventBus, runtime: GatewayRuntime) -> list[str]:
    """Register BUILTIN handlers and return their ids for shutdown."""

    handler_ids = [
        bus.register_event_handler(
            EVENT_UI_RENDER,
            runtime.browser.build_render_guard_handler(),
            {},
            HandlerLevel.BUILTIN,
        ),
        bus.register_event_handler(
            OPS_BUILD_EVENT_TYPE,
            runtime.browser.build_ops_handler(),
            {},
            HandlerLevel.BUILTIN,
        ),
        bus.register_event_handler(
            OPS_BUILD_EVENT_TYPE,
            build_gateway_ops_build_handler(bus),
            {},
            HandlerLevel.BUILTIN,
        ),
    ]
    handler_ids.extend(runtime.webui.register_handlers(bus))
    handler_ids.append(
        bus.register_event_handler(
            OPS_BUILD_EVENT_TYPE,
            build_http_ops_build_handler(runtime.config.http),
            {},
            HandlerLevel.BUILTIN,
        )
    )
    handler_ids.append(
        bus.register_event_handler(
            EVENT_GATEWAY_REQUEST,
            runtime.browser.build_request_dispatch_handler(),
            {},
            HandlerLevel.BUILTIN,
        )
    )
    handler_ids.append(
        bus.register_event_handler(
            EVENT_GATEWAY_REQUEST,
            build_auth_gate_handler(
                policy=runtime.config.auth,
                enforce=lambda: runtime.has_auth_provider,
            ),
            {},
            HandlerLevel.BUILTIN,
        )
    )
    return handler_ids

# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""``ops.build`` handler that exposes the WebUI capability namespace."""

from __future__ import annotations

from openjiuwen.core.events import (
    Event,
    EventHandler,
    EventHandlerOps,
    EventHandlerOpsBuilder,
    EventNext,
    ExtensionEventBus,
)

from jiuwenswarm.extensions_v2.webui.models import UiRenderInput


def build_webui_ops_build_handler(bus: ExtensionEventBus, render_event_type: str) -> EventHandler:
    """Build the BUILTIN handler that adds ``ui.render`` to the Ops catalog."""

    async def ui_render(ops_view: EventHandlerOps, request: UiRenderInput) -> object:
        # Verb: start a new ui.render chain. The current chain is not advanced.
        return await bus.trigger(Event(render_event_type, request), ops_view.session_context)

    async def webui_ops_build(
        event: Event,
        ops: EventHandlerOps | None,
        next_handler: EventNext,
    ) -> EventHandlerOpsBuilder:
        del ops
        builder = await next_handler(event)
        if not isinstance(builder, EventHandlerOpsBuilder):
            raise TypeError("ops.build must return EventHandlerOpsBuilder")
        builder.add_namespace("ui")
        builder.add_method("ui", "render", ui_render)
        return builder

    return webui_ops_build
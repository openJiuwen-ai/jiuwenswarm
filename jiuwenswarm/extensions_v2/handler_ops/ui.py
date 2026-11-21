# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""``ops.build`` handler that exposes the WebUI capability namespace."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from openjiuwen.core.events import (
    Event,
    EventHandler,
    EventHandlerOps,
    EventHandlerOpsBuilder,
    EventNext,
    ExtensionEventBus,
    SessionContext,
)

from jiuwenswarm.extensions_v2.webui.models import UiRenderInput
from jiuwenswarm.extensions_v2.webui.resources import WebUIResourceService


def build_webui_ops_build_handler(
    bus: ExtensionEventBus,
    render_event_type: str,
    *,
    render: Callable[[UiRenderInput, SessionContext | None], Awaitable[object]] | None = None,
    resources: WebUIResourceService | None = None,
) -> EventHandler:
    """Build the BUILTIN handler that adds ``ui.render`` to the Ops catalog."""

    async def ui_render(ops_view: EventHandlerOps, request: UiRenderInput) -> object:
        # Verb: start a new ui.render chain. The current chain is not advanced.
        if render is not None:
            return await render(request, ops_view.session_context)
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
        if resources is not None:
            builder.add_method("ui", "create_resource_dir", resources.create_directory)
            builder.add_method("ui", "asset_url", resources.asset_url)
            builder.add_method("ui", "release_resources", resources.release)
        return builder

    return webui_ops_build

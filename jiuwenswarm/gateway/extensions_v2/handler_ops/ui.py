# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""``ops.build`` handler that exposes the Gateway WebUI namespaces."""

from __future__ import annotations

from dataclasses import replace

from openjiuwen.core.events import (
    Event,
    EventHandler,
    EventHandlerOps,
    EventHandlerOpsBuilder,
    EventNext,
    ExtensionEventBus,
)

from jiuwenswarm.gateway.extensions_v2.models import EVENT_UI_RENDER
from jiuwenswarm.gateway.extensions_v2.webui.call_context import RenderCheckFactory, render_scope
from jiuwenswarm.gateway.extensions_v2.webui.models import RenderStatus, UiRenderInput, UiRenderOutput


def build_ui_ops_build_handler(
    bus: ExtensionEventBus, *, render_check: RenderCheckFactory | None = None,
) -> EventHandler:
    """Build the BUILTIN handler that adds the ``ops.ui`` namespace."""

    async def ui_render(ops_view: EventHandlerOps, request: UiRenderInput) -> object:
        # Verb: start a new ui.render chain. The current chain is not advanced.
        owner = ops_view.package_metadata.package_id if ops_view.package_metadata else ""
        if owner:
            if request.target.package_id and request.target.package_id != owner:
                raise ValueError("render target does not belong to the calling package")
            request = replace(request, target=replace(request.target, package_id=owner))
        check = render_check(ops_view) if render_check is not None else None
        if check is not None and not check(request.target):
            return UiRenderOutput(status=RenderStatus.REJECTED)
        with render_scope(check):
            return await bus.trigger(Event(EVENT_UI_RENDER, request), ops_view.session_context)

    async def ui_ops_build(
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

    return ui_ops_build

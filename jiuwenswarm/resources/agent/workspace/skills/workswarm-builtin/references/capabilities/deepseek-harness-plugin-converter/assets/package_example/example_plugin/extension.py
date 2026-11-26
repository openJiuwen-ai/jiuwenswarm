"""Reuse ui.ready and ops.ui.render without adding platform capabilities."""

from __future__ import annotations

from openjiuwen.core.events import Event, EventHandlerOps, EventNext, HandlerLevel
from openjiuwen.core.extension import ExtensionRegistrar

from jiuwenswarm.extensions_v2.webui.constants import CODE_DELIVERY_FAILED, WebUIEvent
from jiuwenswarm.extensions_v2.webui.models import (
    RenderNode,
    RenderStatus,
    RenderTarget,
    UiActionInput,
    UiActionOutput,
    UiRenderInput,
    UiRenderOutput,
)

MESSAGE = "转换后的系统插件已生效"
SLOT_ID = "conversation.input.above"
INSTANCE_KEY = "converted.main"


class ConvertedExampleExtension:
    """Register one USER handler for an existing WebUI event."""

    def register(self, registrar: ExtensionRegistrar) -> None:
        registrar.register_event_handler(
            WebUIEvent.READY.value,
            self.on_ready,
            {"target.slot_id": SLOT_ID},
            HandlerLevel.USER,
        )

    async def on_ready(
        self,
        event: Event,
        ops: EventHandlerOps | None,
        next_handler: EventNext,
    ) -> UiActionOutput:
        request = event.event_input
        if not isinstance(request, UiActionInput):
            raise TypeError("ui.ready requires UiActionInput")

        receipt = await next_handler(event)
        if not isinstance(receipt, UiActionOutput):
            raise TypeError("ui.ready must return UiActionOutput")
        if not receipt.accepted:
            return receipt
        if ops is None or ops.package_metadata is None or ops.session_context is None:
            return UiActionOutput(accepted=False, status="context_unavailable")

        target = RenderTarget(
            session_id=ops.session_context.session_id,
            slot_id=SLOT_ID,
            package_id=ops.package_metadata.package_id,
            instance_key=INSTANCE_KEY,
        )
        output = await ops.ui.render(
            UiRenderInput(target=target, root=RenderNode("Text", {"value": MESSAGE}))
        )
        if not isinstance(output, UiRenderOutput) or output.status is not RenderStatus.DELIVERED:
            return UiActionOutput(accepted=False, status="render_failed", code=CODE_DELIVERY_FAILED)
        return receipt


def create_extension() -> ConvertedExampleExtension:
    return ConvertedExampleExtension()

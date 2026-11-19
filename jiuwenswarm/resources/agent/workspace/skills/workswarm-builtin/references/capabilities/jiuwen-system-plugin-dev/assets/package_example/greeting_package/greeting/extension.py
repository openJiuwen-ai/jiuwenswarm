"""Reuse ui.ready and ops.ui.render; install no new platform entry."""

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

GREETING = "你好，系统插件已生效"
SLOT_ID = "conversation.input.above"
INSTANCE_KEY = "greeting.main"


class GreetingExtension:
    """One activation registers a USER handler for the existing slot-ready event."""

    def register(self, registrar: ExtensionRegistrar) -> None:
        # 系统插件身份由平台在发布时注入，这里不传 package_id，也不登记新 EventType。
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

        # 沿用当前链的默认处理。默认拒绝时保持该结果，不渲染成功界面。
        receipt = await next_handler(event)
        if not isinstance(receipt, UiActionOutput):
            raise TypeError("ui.ready must return UiActionOutput")
        if not receipt.accepted:
            return receipt
        if ops is None or ops.package_metadata is None or ops.session_context is None:
            return UiActionOutput(accepted=False, status="context_unavailable")

        # 使用可信系统插件身份与会话；同一会话重建同一 instance，不积累重复面板。
        target = RenderTarget(
            session_id=ops.session_context.session_id,
            slot_id=SLOT_ID,
            package_id=ops.package_metadata.package_id,
            instance_key=INSTANCE_KEY,
        )
        output = await ops.ui.render(
            UiRenderInput(target=target, root=RenderNode("Text", {"value": GREETING}))
        )
        # RenderNode 的真实文本字段是 value。实际输出来自本系统插件后端，前端不写死文本。
        if not isinstance(output, UiRenderOutput) or output.status is not RenderStatus.DELIVERED:
            return UiActionOutput(accepted=False, status="render_failed", code=CODE_DELIVERY_FAILED)
        return receipt


def create_extension() -> GreetingExtension:
    return GreetingExtension()

# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Downstream adapter: send render frames and errors through the AgentServer push channel."""

from __future__ import annotations

import logging
import uuid
from typing import Any, Protocol, runtime_checkable

from jiuwenswarm.extensions_v2.webui.constants import (
    DEFAULT_CHANNEL_ID,
    FRAME_CLEAR,
    FRAME_RENDER,
    FRAME_RENDER_ERROR,
    SlotScope,
)
from jiuwenswarm.extensions_v2.webui.models import ClearFrame, RenderFrame, RenderTarget

logger = logging.getLogger(__name__)


@runtime_checkable
class PushTransport(Protocol):
    """Minimal protocol shared with server/gateway_push."""

    async def send_push(self, msg: dict[str, Any]) -> bool: ...


class RenderTransport:
    """Build push messages and deliver them; delivery failure is returned, not raised."""

    def __init__(self, *, channel_id: str = DEFAULT_CHANNEL_ID, push: PushTransport | None = None) -> None:
        self._channel_id = channel_id
        self._push = push

    async def send_frame(self, frame: RenderFrame) -> bool:
        return await self._send(frame.target, FRAME_RENDER, frame.to_payload())

    async def send_clear(self, frame: ClearFrame) -> bool:
        return await self._send(frame.target, FRAME_CLEAR, frame.to_payload())

    async def send_error(
        self,
        target: RenderTarget,
        code: str,
        message: str = "",
        *,
        revision: int = 0,
    ) -> bool:
        return await self._send(
            target,
            FRAME_RENDER_ERROR,
            {"target": target.to_payload(), "revision": revision, "code": code, "message": message},
        )

    async def _send(self, target: RenderTarget, event_type: str, payload: dict[str, Any]) -> bool:
        message: dict[str, Any] = {
            "request_id": f"webui-{event_type}-{uuid.uuid4().hex[:12]}",
            "channel_id": self._channel_id,
            "payload": {"event_type": event_type, **payload},
        }
        # Application frames name the web socket. They do not carry a chat id,
        # so the gateway cannot fall through to another channel or another tab.
        if target.resolved_scope == SlotScope.APPLICATION.value:
            message["metadata"] = {"ws_id": target.connection_id}
        else:
            message["session_id"] = target.session_id
        try:
            return bool(await self._resolve_push().send_push(message))
        except Exception as exc:  # noqa: BLE001 - a delivery failure must not break the event chain
            logger.warning("[WebUI] failed to send %s: %s", event_type, exc)
            return False

    async def notify_packages_changed(
        self, connection_id: str, session_id: str, package_ids: list[str],
    ) -> bool:
        """Notify exactly the browser that previously declared ready ownership."""
        message = {
            "request_id": f"extension-changed-{uuid.uuid4().hex[:12]}",
            "channel_id": self._channel_id,
            "session_id": session_id,
            "metadata": {"ws_id": connection_id},
            "payload": {"event_type": "extensions.v2.installed", "package_ids": package_ids,
                        "session_id": session_id},
        }
        try:
            return bool(await self._resolve_push().send_push(message))
        except Exception:  # noqa: BLE001 - UI notification does not undo installation
            logger.warning("[WebUI] package installation notification failed")
            return False

    def _resolve_push(self) -> PushTransport:
        if self._push is None:
            from jiuwenswarm.server.gateway_push.transport import WebSocketGatewayPushTransport

            self._push = WebSocketGatewayPushTransport()
        return self._push

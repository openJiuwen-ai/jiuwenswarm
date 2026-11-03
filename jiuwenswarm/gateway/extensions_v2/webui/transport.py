# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Deliver Gateway frames through the existing WebChannel.

The Gateway owns the browser socket, so a frame is addressed by ``ws_id`` and
sent with ``WebChannel.send_event``; it never travels through AgentServer.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from jiuwenswarm.gateway.extensions_v2.webui.constants import (
    FRAME_CLEAR,
    FRAME_RENDER,
    FRAME_RENDER_ERROR,
)
from jiuwenswarm.gateway.extensions_v2.webui.models import (
    ClearFrame,
    RenderFrame,
    RenderTarget,
)

logger = logging.getLogger(__name__)


class GatewayTransport:
    """Send frames to one browser connection; failure is returned, not raised."""

    def __init__(self, channel: Any | None = None) -> None:
        self._channel = channel

    def bind(self, channel: Any) -> None:
        """Point the transport at the channel that accepted the connection."""

        self._channel = channel

    async def send_frame(self, frame: RenderFrame) -> bool:
        return await self._send(frame.target.connection_id, FRAME_RENDER, frame.to_payload())

    async def send_clear(self, frame: ClearFrame) -> bool:
        return await self._send(frame.target.connection_id, FRAME_CLEAR, frame.to_payload())

    async def send_error(
        self,
        target: RenderTarget,
        code: str,
        message: str = "",
        *,
        revision: int = 0,
    ) -> bool:
        return await self._send(
            target.connection_id,
            FRAME_RENDER_ERROR,
            {"target": target.to_payload(), "revision": revision, "code": code, "message": message},
        )

    async def _send(self, connection_id: str, event: str, payload: Mapping[str, Any]) -> bool:
        channel = self._channel
        if channel is None or not connection_id:
            return False
        lookup = getattr(channel, "ws_by_id", None)
        ws = lookup(connection_id) if callable(lookup) else None
        if ws is None:
            return False
        try:
            await channel.send_event(ws, event, dict(payload))
        except Exception as exc:  # noqa: BLE001 - a delivery failure must not break the chain
            logger.warning("[GatewayExtensions] failed to send %s: %s", event, exc)
            return False
        return True

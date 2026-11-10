# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Deliver Gateway frames through the existing WebChannel.

The Gateway owns the browser socket, so a frame is addressed by ``ws_id`` and
sent with ``WebChannel.send_event``; it never travels through AgentServer.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from typing import Any

from jiuwenswarm.gateway.extensions_v2.webui.constants import (
    FRAME_APPEARANCE,
    FRAME_CLEAR,
    FRAME_DOWNLOAD,
    FRAME_RENDER,
    FRAME_RENDER_ERROR,
    FRAME_SPLASH_PLAY,
)
from jiuwenswarm.gateway.extensions_v2.webui.models import (
    ClearFrame,
    RenderFrame,
    RenderTarget,
)

logger = logging.getLogger(__name__)


class GatewayTransport:
    """Send frames to one browser connection; failure is returned, not raised."""

    def __init__(
        self, channel: Any | None = None, *, generation: Callable[[str, str | None], str | None] | None = None,
    ) -> None:
        self._channel = channel
        self._generation = generation

    def bind(self, channel: Any) -> None:
        """Point the transport at the channel that accepted the connection."""

        self._channel = channel

    async def send_frame(self, frame: RenderFrame, *, generation: str | None = None) -> bool:
        return await self._send(frame.target.connection_id, FRAME_RENDER, frame.to_payload(), generation)

    async def send_clear(self, frame: ClearFrame, *, generation: str | None = None) -> bool:
        return await self._send(frame.target.connection_id, FRAME_CLEAR, frame.to_payload(), generation)

    async def send_appearance(self, connection_id: str, payload: Mapping[str, Any]) -> bool:
        """Send one appearance contribution or revocation (S_08)."""

        return await self._send(connection_id, FRAME_APPEARANCE, payload)

    async def send_download(self, connection_id: str, payload: Mapping[str, Any]) -> bool:
        """Offer one export to the connection that asked for it (S_10 section 7)."""

        return await self._send(connection_id, FRAME_DOWNLOAD, payload)

    async def send_splash_play(self, connection_id: str, payload: Mapping[str, Any]) -> bool:
        """Ask one connection to preview a package's splash (S_09 section 3.3)."""

        return await self._send(connection_id, FRAME_SPLASH_PLAY, payload)

    async def send_presentation_start(self, connection_id: str, payload: Mapping[str, Any]) -> bool:
        """Request one explicit presentation start; the caller separately awaits its receipt."""
        return await self._send(connection_id, "webui.presentation.start", payload)

    async def send_error(
        self,
        target: RenderTarget,
        code: str,
        message: str = "",
        *,
        revision: int = 0,
        generation: str | None = None,
    ) -> bool:
        return await self._send(
            target.connection_id,
            FRAME_RENDER_ERROR,
            {"target": target.to_payload(), "revision": revision, "code": code, "message": message},
            generation,
        )

    async def _send(self, connection_id: str, event: str, payload: Mapping[str, Any],
                    generation: str | None = None) -> bool:
        """Deliver one frame; ``generation`` is the calling activation's, or None for the host's own frames."""
        channel = self._channel
        if channel is None or not connection_id:
            return False
        lookup = getattr(channel, "ws_by_id", None)
        ws = lookup(connection_id) if callable(lookup) else None
        if ws is None:
            return False
        body = dict(payload)
        if self._generation is not None:
            package_id = body.get("target", {}).get("package_id", "")
            stamped = self._generation(package_id, generation)
            if stamped:
                body["activation_generation"] = stamped
        try:
            await channel.send_event(ws, event, body)
        except Exception as exc:  # noqa: BLE001 - a delivery failure must not break the chain
            logger.warning("[GatewayExtensions] failed to send %s: %s", event, exc)
            return False
        return True

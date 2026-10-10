# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Front-owned Gateway stream writer.

Runtime must not own the Gateway socket. Phase 1 still lets the in-process
backend attach the same connection for send_push compatibility, but Front
writes control/health/warming frames through this forwarder.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from jiuwenswarm.server.ws_send import send_wire_payload

logger = logging.getLogger(__name__)


class EventForwarder:
    """Send already-encoded E2A frames on the Front-owned connection."""

    def __init__(self) -> None:
        self._ws: Any = None
        self._send_lock: asyncio.Lock | None = None

    def attach(self, ws: Any, send_lock: asyncio.Lock) -> None:
        self._ws = ws
        self._send_lock = send_lock

    def detach(self, ws: Any | None = None) -> None:
        if ws is not None and self._ws is not ws:
            return
        self._ws = None
        self._send_lock = None

    @property
    def current_ws(self) -> Any:
        return self._ws

    @property
    def current_send_lock(self) -> asyncio.Lock | None:
        return self._send_lock

    async def send(self, payload: dict[str, Any], *, ws: Any = None, send_lock: Any = None) -> bool:
        target_ws = ws if ws is not None else self._ws
        lock = send_lock if send_lock is not None else self._send_lock
        if target_ws is None or lock is None:
            logger.warning("[Front] event forwarder has no Gateway connection")
            return False
        async with lock:
            return await send_wire_payload(target_ws, payload)

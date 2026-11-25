# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Proactive management; resources remain owned by its injected host."""

# These domain providers and their compatibility facade are one Worker
# implementation, sharing host-owned lifecycle state rather than a public API.
# pylint: disable=protected-access

from __future__ import annotations

import logging
from typing import Any
import asyncio

from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.agent import AgentResponse

logger = logging.getLogger(__name__)


class ProactiveManagement:
    def __init__(self, host):
        self.host = host

    async def _handle_proactive_tick(self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None:
        """Soft no-op: proactive recommendation removed; keep gateway cron sync happy."""
        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=True,
            payload={"status": "skipped", "success": False, "reason": "proactive_removed"},
        )
        wire = self.host.services.encode_response(resp, response_id=request.request_id)
        async with send_lock:
            await self.host.services.send(ws, wire)

    async def _handle_proactive_feedback(self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None:
        """Soft no-op: proactive recommendation feedback path removed."""
        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=True,
            payload={"status": "skipped", "reason": "proactive_removed"},
        )
        wire = self.host.services.encode_response(resp, response_id=request.request_id)
        async with send_lock:
            await self.host.services.send(ws, wire)

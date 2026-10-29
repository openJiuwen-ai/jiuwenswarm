# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

from jiuwenswarm.common.e2a.wire_codec import encode_agent_response_for_wire
from jiuwenswarm.server.ws_send import send_wire_payload

import asyncio
import logging
from typing import Any
from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponse

logger = logging.getLogger(__name__)


class TrajectoryManagement:
    def __init__(self, host):
        self.host = host

    def _get_trajectory_host(self) -> Any:
        """Lazily build the trajectory read host once."""
        host = self.host
        if getattr(host, "_trajectory_host", None) is None:
            from jiuwenswarm.server.trajectory.host import TrajectoryHost

            host._trajectory_host = TrajectoryHost()
        return host._trajectory_host

    async def _handle_trajectory_request(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        """Answer one ``trajectory.*`` read proxied by the Gateway."""
        host = self.host
        try:
            resp = await host._get_trajectory_host().handle(request)
        except Exception as exc:
            logger.exception("[AgentServer] trajectory request failed: %s", exc)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={
                    "error": "trajectory query failed",
                    "code": "TRAJECTORY_QUERY_FAILED",
                    "status": 500,
                },
            )

        wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
        async with send_lock:
            await send_wire_payload(ws, wire)



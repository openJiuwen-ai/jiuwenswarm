# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

from jiuwenswarm.common.e2a.wire_codec import encode_agent_response_for_wire
from jiuwenswarm.server.ws_send import send_wire_payload

import asyncio
import logging
from typing import Any
from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponse

logger = logging.getLogger(__name__)


class RecommendationsManagement:
    def __init__(self, host):
        self.host = host

    async def _handle_recommendation(self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None:
        host = self.host
        params = dict(request.params) if isinstance(request.params, dict) else {}
        action = str(params.get("action") or "")
        result = await host._recommendation_host_instance().handle(
            action=action,
            params=params,
            session_id=str(request.session_id or ""),
            request_id=str(request.request_id or ""),
        )
        if result.ok:
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload=dict(result.payload or {}),
                metadata=request.metadata,
            )
        else:
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": result.error or "recommendation failed", "code": result.code or "BAD_REQUEST"},
                metadata=request.metadata,
            )
        wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
        async with send_lock:
            await send_wire_payload(ws, wire)
        for event_type, event_payload in result.events:
            await host._push_recommendation_event(event_type, event_payload)

    async def _push_recommendation_event(self, event_type: str, payload: dict[str, Any]) -> None:
        host = self.host
        body = dict(payload)
        body["event_type"] = event_type
        await host.send_push({
            "channel_id": "",
            "session_id": "",
            "payload": body,
        })

    def _recommendation_active_request_ids(self) -> frozenset[str]:
        host = self.host
        runtime = host._execution_runtime()
        pending = getattr(runtime, "_pending_chat_requests", None) or {}
        return frozenset(
            request_id
            for bucket in pending.values()
            for request_id in bucket
        )

    def _recommendation_host_instance(self) -> Any:
        owner = self.host
        from jiuwenswarm.server.runtime.recommendation_host import RecommendationHost

        host = getattr(owner, "_recommendation_host", None)
        if host is None:
            host = RecommendationHost()
            host.bind_active_requests(owner._recommendation_active_request_ids)
            host.bind_push(owner._push_recommendation_event)
            owner._recommendation_host = host
        return host

    async def _start_recommendation_host(self) -> None:
        host = self.host
        from jiuwenswarm.server.runtime.recommendation_host import (
            activate_recommendation_host,
        )

        try:
            host = host._recommendation_host_instance()
            activate_recommendation_host(host)
            await host.start()
        except Exception:  # noqa: BLE001 - recommendation must not block server start
            logger.warning(
                "[AgentWebSocketServer] recommendation host failed to start",
                exc_info=True,
            )

    async def _stop_recommendation_host(self) -> None:
        owner = self.host
        from jiuwenswarm.server.runtime.recommendation_host import (
            deactivate_recommendation_host,
        )

        host = getattr(owner, "_recommendation_host", None)
        if host is None:
            return
        deactivate_recommendation_host(host)
        owner._recommendation_host = None
        try:
            await host.shutdown()
        except Exception:  # noqa: BLE001 - recommendation must not block server stop
            logger.warning(
                "[AgentWebSocketServer] recommendation host failed to stop",
                exc_info=True,
            )







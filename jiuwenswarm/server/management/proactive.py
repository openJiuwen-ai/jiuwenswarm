# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Proactive management; resources remain owned by its injected host."""

from __future__ import annotations

import logging
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.agent import AgentResponse
from typing import Any
import asyncio

logger = logging.getLogger(__name__)


class ProactiveManagement:
    def __init__(self, host):
        self.host = host

    async def _handle_proactive_tick(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        """Handle proactive.tick request from CronScheduler.

        This is called by Gateway's CronScheduler to trigger a recommendation tick.
        Respects cooldown and daily limits.
        """
        host = self.host
        if host._proactive_engine is None:
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": "ProactiveEngine not initialized"},
            )
        else:
            try:
                params = request.params or {}
                target_channel = params.get("target_channel")
                success = await host._proactive_engine.tick_now(
                    target_channel=target_channel
                )
                status = "tick_executed" if success else "no_recommendation"
                last_tick = host._proactive_engine.last_tick_at
                if last_tick > 0:
                    status = f"{status} (last_tick_at={last_tick:.0f})"
                resp = AgentResponse(
                    request_id=request.request_id,
                    channel_id=request.channel_id,
                    ok=True,
                    payload={"status": status, "success": success},
                )
            except Exception as e:
                resp = AgentResponse(
                    request_id=request.request_id,
                    channel_id=request.channel_id,
                    ok=False,
                    payload={"error": str(e)},
                )
        wire = host.services.encode_response(resp, response_id=request.request_id)
        async with send_lock:
            await host.services.send(ws, wire)

    async def _handle_proactive_feedback(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        """Handle proactive.feedback request from frontend.

        Receives user feedback (like/dislike) on proactive recommendations.
        Stores feedback in buffer for next tick to update strategy gradients.
        """
        host = self.host
        try:
            params = request.params or {}
            rec_id = params.get("rec_id")
            feedback_type = params.get("feedback_type")
            rec_type = str(params.get("rec_type") or params.get("proactive_type") or "")
            rec_target = str(
                params.get("rec_target") or params.get("proactive_target") or ""
            )
            if not rec_id or feedback_type not in ("explicit_like", "explicit_dislike"):
                resp = AgentResponse(
                    request_id=request.request_id,
                    channel_id=request.channel_id,
                    ok=False,
                    payload={
                        "error": "Invalid params: rec_id and feedback_type (explicit_like|explicit_dislike) required"
                    },
                )
            else:
                from jiuwenswarm.agents.harness.common.recommendation.feedback_collector import (
                    RecMeta,
                    record_explicit_feedback,
                )

                record_explicit_feedback(
                    rec_id,
                    feedback_type,
                    meta=RecMeta(rec_type=rec_type, rec_target=rec_target),
                )
                resp = AgentResponse(
                    request_id=request.request_id,
                    channel_id=request.channel_id,
                    ok=True,
                    payload={"status": "feedback_recorded", "rec_id": rec_id},
                )
        except Exception as e:
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(e)},
            )
        wire = host.services.encode_response(resp, response_id=request.request_id)
        async with send_lock:
            await host.services.send(ws, wire)

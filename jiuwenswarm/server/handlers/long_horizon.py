# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Long-horizon control RPC (I2b / I2c): Gateway never writes task JSON."""

from __future__ import annotations

import logging
from typing import Any

from jiuwenswarm.common.e2a.wire_codec import encode_agent_response_for_wire
from jiuwenswarm.common.schema.agent import AgentResponse
from jiuwenswarm.server.context import RequestContext
from jiuwenswarm.server.handlers._shared import _agent_workspace_dir_for_request
from jiuwenswarm.server.runtime.tenant_agent_pool import TenantAgentPool

logger = logging.getLogger(__name__)


def _runtime_tenant_ids_from_request(request: Any) -> tuple[str, str]:
    agent_id, service_id, _workspace_key = TenantAgentPool.extract_ids(request)
    return (
        str(service_id or "default").strip() or "default",
        str(agent_id or "default").strip() or "default",
    )


async def handle_long_horizon(ctx: RequestContext) -> None:
    """Handle ``long_horizon`` unary RPC: list / inbox / stage_action / mark_due."""
    request = ctx.request
    params = dict(request.params or {})
    action = str(params.get("action") or "").strip().lower()
    service_id, agent_id = _runtime_tenant_ids_from_request(request)
    workspace = str(_agent_workspace_dir_for_request(request) or "")
    result: dict[str, Any]
    try:
        if action == "mark_due":
            from jiuwenswarm.agents.harness.common.long_horizon.runtime import (
                mark_stage_due,
            )

            result = await mark_stage_due(
                workspace,
                str(params.get("task_id") or ""),
                str(params.get("stage_id") or ""),
                job_id=str(params.get("job_id") or ""),
            )
        else:
            from jiuwenswarm.agents.harness.common.long_horizon.tools import (
                LongHorizonActions,
            )

            result = await LongHorizonActions(
                workspace,
                service_id=service_id,
                agent_id=agent_id,
            ).handle(params)
    except Exception as exc:
        logger.warning("[long_horizon] rpc failed action=%s: %s", action, exc)
        result = {"success": False, "error": str(exc)}

    ok = bool(result.get("success"))
    resp = AgentResponse(
        request_id=request.request_id,
        channel_id=request.channel_id,
        ok=ok,
        payload=result,
    )
    wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
    await ctx.sink.send_wire(wire)

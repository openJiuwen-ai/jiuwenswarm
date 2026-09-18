# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Long-horizon control RPC (I2b / I2c): Gateway never writes task JSON."""

from __future__ import annotations

import logging
from typing import Any

from jiuwenswarm.common.e2a.wire_codec import encode_agent_response_for_wire
from jiuwenswarm.common.schema.agent import AgentResponse
from jiuwenswarm.server.context import RequestContext

logger = logging.getLogger(__name__)


def _workspace() -> str:
    from jiuwenswarm.common.utils import get_agent_workspace_dir

    return str(get_agent_workspace_dir() or "")


async def handle_long_horizon(ctx: RequestContext) -> None:
    """Handle ``long_horizon`` unary RPC: list / inbox / stage_action / mark_due."""
    request = ctx.request
    params = dict(request.params or {})
    action = str(params.get("action") or "").strip().lower()
    workspace = _workspace()
    result: dict[str, Any]
    try:
        if action == "mark_due":
            from jiuwenswarm.agents.harness.common.long_horizon.core import (
                get_long_horizon_task,
            )
            from jiuwenswarm.agents.harness.common.long_horizon.runtime import (
                _ensure_session_metadata,
                mark_stage_due,
            )

            result = await mark_stage_due(
                workspace,
                str(params.get("task_id") or ""),
                str(params.get("stage_id") or ""),
                job_id=str(params.get("job_id") or ""),
            )
            if result.get("success"):
                task = get_long_horizon_task(workspace, str(params.get("task_id") or ""))
                if task is not None:
                    await _ensure_session_metadata(
                        task,
                        channel_id=str(request.channel_id or "web"),
                    )
        else:
            from jiuwenswarm.agents.harness.common.long_horizon.tools import (
                LongHorizonActions,
            )

            # No CronBackend: schedule changes go out via I2a server_push.
            result = await LongHorizonActions(workspace).handle(params)
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

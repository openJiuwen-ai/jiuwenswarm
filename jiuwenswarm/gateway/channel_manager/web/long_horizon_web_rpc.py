# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Web RPC for long-horizon toast actions. Forwards to AgentServer (I2c)."""

from __future__ import annotations

import logging
from typing import Any

from jiuwenswarm.gateway.long_horizon.agent_call import call_agent_long_horizon

logger = logging.getLogger(__name__)


def _params_dict(params: Any) -> dict[str, Any]:
    return params if isinstance(params, dict) else {}


def _channel_id(context: Any) -> str:
    return str(getattr(context, "channel_id", None) or "web")


async def run_long_horizon_list(
    context: Any, params: Any = None
) -> dict[str, Any]:
    _ = _params_dict(params)
    result = await call_agent_long_horizon(
        action="list",
        params={},
        channel_id=_channel_id(context),
        context=context,
    )
    return {
        "type": "long_horizon_list_result",
        "success": bool(result.get("success")),
        "tasks": result.get("tasks") or [],
        "count": int(result.get("count") or 0),
        **(
            {"error": result.get("error")}
            if not result.get("success") and result.get("error")
            else {}
        ),
    }


async def run_long_horizon_inbox(
    context: Any, params: Any = None
) -> dict[str, Any]:
    _ = _params_dict(params)
    result = await call_agent_long_horizon(
        action="inbox",
        params={},
        channel_id=_channel_id(context),
        context=context,
    )
    return {
        "type": "long_horizon_inbox_result",
        "success": bool(result.get("success")),
        "inbox": result.get("inbox") or [],
        **(
            {"error": result.get("error")}
            if not result.get("success") and result.get("error")
            else {}
        ),
    }


async def run_long_horizon_stage_action(
    context: Any, params: Any = None
) -> dict[str, Any]:
    p = _params_dict(params)
    result = await call_agent_long_horizon(
        action="stage_action",
        params={
            "task_id": p.get("task_id") or p.get("id"),
            "stage_id": p.get("stage_id"),
            "stage_action": p.get("stage_action") or p.get("action"),
            "snooze_hours": p.get("snooze_hours"),
            "conclusion": p.get("conclusion"),
            "source": "user",
        },
        channel_id=_channel_id(context),
        context=context,
    )
    payload = {
        "type": "long_horizon_stage_action_result",
        "success": bool(result.get("success")),
        **{k: v for k, v in result.items() if k != "success"},
    }
    if not payload["success"] and "error" not in payload:
        payload["error"] = "stage_action_failed"
    return payload


def register_long_horizon_web_methods(channel: Any) -> None:
    """Register underscore RPC methods used by the Web toast."""

    async def _list(ws, req_id, params, session_id):
        _ = session_id
        response = await run_long_horizon_list(channel, params)
        ok = bool(response.get("success"))
        await channel.send_response(
            ws,
            req_id,
            ok=ok,
            payload=response if ok else None,
            error=None if ok else str(response.get("error") or "list_failed"),
            code=None if ok else "INTERNAL_ERROR",
        )

    async def _inbox(ws, req_id, params, session_id):
        _ = session_id
        response = await run_long_horizon_inbox(channel, params)
        ok = bool(response.get("success"))
        await channel.send_response(
            ws,
            req_id,
            ok=ok,
            payload=response if ok else None,
            error=None if ok else str(response.get("error") or "inbox_failed"),
            code=None if ok else "INTERNAL_ERROR",
        )

    async def _stage_action(ws, req_id, params, session_id):
        _ = session_id
        response = await run_long_horizon_stage_action(channel, params)
        ok = bool(response.get("success"))
        await channel.send_response(
            ws,
            req_id,
            ok=ok,
            payload=response if ok else None,
            error=None if ok else str(response.get("error") or "stage_action_failed"),
            code=None if ok else "BAD_REQUEST",
        )

    channel.register_method("long_horizon_list", _list)
    channel.register_method("long_horizon_inbox", _inbox)
    channel.register_method("long_horizon_stage_action", _stage_action)

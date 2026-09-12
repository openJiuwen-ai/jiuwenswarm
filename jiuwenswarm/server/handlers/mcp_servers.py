# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""MCP Server 注册表 CRUD（mcp.server.*），与 command.mcp（config.yaml）分离。"""

from __future__ import annotations

import logging
from typing import Any

from jiuwenswarm.common.e2a.wire_codec import encode_agent_response_for_wire
from jiuwenswarm.common.mcp_server_registry import get_mcp_server_registry
from jiuwenswarm.common.schema.agent import AgentResponse
from jiuwenswarm.server.context import RequestContext

logger = logging.getLogger(__name__)


def _params(ctx: RequestContext) -> dict[str, Any]:
    return dict(ctx.request.params or {})


async def _send(ctx: RequestContext, *, ok: bool, payload: dict[str, Any]) -> None:
    request = ctx.request
    resp = AgentResponse(
        request_id=request.request_id,
        channel_id=request.channel_id,
        ok=ok,
        payload=payload,
    )
    wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
    await ctx.sink.send_wire(wire)


async def handle_mcp_server_add(ctx: RequestContext) -> None:
    params = _params(ctx)
    servers = params.get("servers")
    if not isinstance(servers, list):
        await _send(ctx, ok=False, payload={"error": "servers must be a list"})
        return
    try:
        results = await get_mcp_server_registry().add_servers(servers)
        await _send(ctx, ok=True, payload={"results": results})
    except Exception as exc:
        logger.exception("[mcp.server.add] failed: %s", exc)
        await _send(ctx, ok=False, payload={"error": str(exc)})


async def handle_mcp_server_remove(ctx: RequestContext) -> None:
    params = _params(ctx)
    names = params.get("names")
    if not isinstance(names, list):
        await _send(ctx, ok=False, payload={"error": "names must be a list"})
        return
    try:
        results = await get_mcp_server_registry().remove_servers(
            [str(n) for n in names]
        )
        await _send(ctx, ok=True, payload={"results": results})
    except Exception as exc:
        logger.exception("[mcp.server.remove] failed: %s", exc)
        await _send(ctx, ok=False, payload={"error": str(exc)})


async def handle_mcp_server_update(ctx: RequestContext) -> None:
    params = _params(ctx)
    servers = params.get("servers")
    if not isinstance(servers, list):
        await _send(ctx, ok=False, payload={"error": "servers must be a list"})
        return
    try:
        results = await get_mcp_server_registry().update_servers(servers)
        await _send(ctx, ok=True, payload={"results": results})
    except Exception as exc:
        logger.exception("[mcp.server.update] failed: %s", exc)
        await _send(ctx, ok=False, payload={"error": str(exc)})


async def handle_mcp_server_list(ctx: RequestContext) -> None:
    try:
        items = await get_mcp_server_registry().list_servers()
        await _send(ctx, ok=True, payload={"servers": items})
    except Exception as exc:
        logger.exception("[mcp.server.list] failed: %s", exc)
        await _send(ctx, ok=False, payload={"error": str(exc)})


async def handle_mcp_server_get(ctx: RequestContext) -> None:
    params = _params(ctx)
    name = str(params.get("name") or "").strip()
    if not name:
        await _send(ctx, ok=False, payload={"error": "name is required"})
        return
    try:
        item = await get_mcp_server_registry().get_server(name)
        if item is None:
            await _send(ctx, ok=False, payload={"error": f"not found: {name}"})
            return
        await _send(ctx, ok=True, payload={"server": item})
    except Exception as exc:
        logger.exception("[mcp.server.get] failed: %s", exc)
        await _send(ctx, ok=False, payload={"error": str(exc)})

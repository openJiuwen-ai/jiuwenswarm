# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""MCP Server 注册表 CRUD（mcp.server.*），与 command.mcp（config.yaml）分离。

个人版：允许注册本地 stdio 与远程连接器。
企业版：整组方法禁止（与本地 /mcp 一致），避免绕过管理端模板。
"""

from __future__ import annotations

import logging
from typing import Any

from jiuwenswarm.common.e2a.wire_codec import encode_agent_response_for_wire
from jiuwenswarm.common.mcp_server_registry import (
    get_mcp_server_registry,
    mcp_registry_capabilities,
)
from jiuwenswarm.common.schema.agent import AgentResponse
from jiuwenswarm.edition import is_enterprise
from jiuwenswarm.server.context import RequestContext
from jiuwenswarm.server.handlers.mcp import _MCP_ENTERPRISE_FORBIDDEN

logger = logging.getLogger(__name__)


def _params(ctx: RequestContext) -> dict[str, Any]:
    return dict(ctx.request.params or {})


def _owner_id(ctx: RequestContext) -> str:
    return str(getattr(ctx, "connection_id", "") or "")


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


async def _reject_if_enterprise(ctx: RequestContext, action: str) -> bool:
    """CRUD 是管理面（方案 §10.4），企业版与本地 /mcp 一样禁止，避免绕过模板下发。"""

    if not is_enterprise():
        return False
    logger.info("[mcp.server.%s] reject in enterprise", action)
    await _send(
        ctx,
        ok=False,
        payload={
            "error": _MCP_ENTERPRISE_FORBIDDEN,
            "code": "MCP_FORBIDDEN",
            "action": action,
        },
    )
    return True


async def handle_mcp_server_add(ctx: RequestContext) -> None:
    if await _reject_if_enterprise(ctx, "add"):
        return
    params = _params(ctx)
    servers = params.get("servers")
    if not isinstance(servers, list):
        await _send(ctx, ok=False, payload={"error": "servers must be a list"})
        return
    try:
        results = await get_mcp_server_registry().add_servers(servers, owner_id=_owner_id(ctx))
        await _send(ctx, ok=True, payload={"results": results})
    except Exception as exc:
        logger.exception("[mcp.server.add] failed: %s", exc)
        await _send(ctx, ok=False, payload={"error": str(exc)})


async def handle_mcp_server_remove(ctx: RequestContext) -> None:
    if await _reject_if_enterprise(ctx, "remove"):
        return
    params = _params(ctx)
    names = params.get("names")
    if not isinstance(names, list):
        await _send(ctx, ok=False, payload={"error": "names must be a list"})
        return
    try:
        results = await get_mcp_server_registry().remove_servers(
            [str(n) for n in names], owner_id=_owner_id(ctx)
        )
        await _send(ctx, ok=True, payload={"results": results})
    except Exception as exc:
        logger.exception("[mcp.server.remove] failed: %s", exc)
        await _send(ctx, ok=False, payload={"error": str(exc)})


async def handle_mcp_server_update(ctx: RequestContext) -> None:
    if await _reject_if_enterprise(ctx, "update"):
        return
    params = _params(ctx)
    servers = params.get("servers")
    if not isinstance(servers, list):
        await _send(ctx, ok=False, payload={"error": "servers must be a list"})
        return
    try:
        results = await get_mcp_server_registry().update_servers(servers, owner_id=_owner_id(ctx))
        await _send(ctx, ok=True, payload={"results": results})
    except Exception as exc:
        logger.exception("[mcp.server.update] failed: %s", exc)
        await _send(ctx, ok=False, payload={"error": str(exc)})


async def handle_mcp_server_rescan(ctx: RequestContext) -> None:
    if await _reject_if_enterprise(ctx, "rescan"):
        return
    params = _params(ctx)
    names = params.get("names")
    scan_params = params.get("scan_params")
    if not isinstance(names, list):
        await _send(ctx, ok=False, payload={"error": "names must be a list"})
        return
    if scan_params is not None and not isinstance(scan_params, dict):
        await _send(ctx, ok=False, payload={"error": "scan_params must be an object"})
        return
    try:
        results = await get_mcp_server_registry().rescan_servers(
            [str(n) for n in names], scan_params
        )
        await _send(ctx, ok=True, payload={"results": results})
    except Exception as exc:
        logger.exception("[mcp.server.rescan] failed")
        await _send(ctx, ok=False, payload={"error": str(exc)})


async def handle_mcp_server_list(ctx: RequestContext) -> None:
    if await _reject_if_enterprise(ctx, "list"):
        return
    try:
        items = await get_mcp_server_registry().list_servers()
        await _send(
            ctx, ok=True, payload={"servers": items, "capabilities": mcp_registry_capabilities()}
        )
    except Exception as exc:
        logger.exception("[mcp.server.list] failed: %s", exc)
        await _send(ctx, ok=False, payload={"error": str(exc)})


async def handle_mcp_server_get(ctx: RequestContext) -> None:
    if await _reject_if_enterprise(ctx, "get"):
        return
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

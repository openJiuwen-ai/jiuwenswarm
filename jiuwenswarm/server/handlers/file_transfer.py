# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.

"""AgentServer handlers for Gateway → Agent chunked file transfer."""

from __future__ import annotations

import logging

from jiuwenswarm.common.e2a.constants import (
    FILE_TRANSFER_CHUNK,
    FILE_TRANSFER_COMPLETE,
    FILE_TRANSFER_START,
)
from jiuwenswarm.common.e2a.wire_codec import encode_agent_response_for_wire
from jiuwenswarm.common.file_transfer_types import FileTransferStartParams
from jiuwenswarm.common.schema.agent import AgentResponse
from jiuwenswarm.server.context import RequestContext
from jiuwenswarm.server.file_transfer_manager import get_file_transfer_manager

logger = logging.getLogger(__name__)


async def handle_file_transfer(ctx: RequestContext) -> None:
    """Handle file.transfer.start|chunk|complete from Gateway."""
    request = ctx.request
    params = request.params if isinstance(request.params, dict) else {}
    event_type = str(params.get("event_type") or "").strip()
    if not event_type and request.req_method is not None:
        event_type = str(request.req_method.value)
    transfer_id = str(params.get("transfer_id") or "").strip()

    ft_manager = get_file_transfer_manager()
    if not ft_manager.enabled:
        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=False,
            payload={
                "error": "file transfer not enabled (distributed mode required)",
                "event_type": event_type,
            },
        )
        wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
        await ctx.sink.send_wire(wire)
        return

    try:
        if event_type == FILE_TRANSFER_START:
            ft_params = FileTransferStartParams(
                transfer_id=transfer_id,
                filename=str(params.get("filename") or "unnamed"),
                file_size=int(params.get("file_size") or 0),
                sha256=str(params.get("sha256") or ""),
                total_chunks=int(params.get("total_chunks") or 0),
                chunk_size=int(params.get("chunk_size") or 65536),
                mime_type=str(params.get("mime_type") or ""),
                session_id=request.session_id or "",
            )
            result = await ft_manager.handle_transfer_start(ft_params)
        elif event_type == FILE_TRANSFER_CHUNK:
            result = await ft_manager.handle_transfer_chunk(
                transfer_id=transfer_id,
                chunk_index=int(params.get("chunk_index") or 0),
                base64_data=str(params.get("base64_data") or ""),
            )
        elif event_type == FILE_TRANSFER_COMPLETE:
            result = await ft_manager.handle_transfer_complete(
                transfer_id=transfer_id,
                sha256=str(params.get("sha256") or ""),
            )
        else:
            result = {
                "accepted": False,
                "error": f"unknown event_type: {event_type}",
            }

        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=bool(result.get("success", result.get("accepted", False))),
            payload={"event_type": event_type, **result},
        )
    except Exception as exc:
        logger.exception("[file_transfer] handle failed: %s", exc)
        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=False,
            payload={"event_type": event_type, "error": str(exc)},
        )

    wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
    await ctx.sink.send_wire(wire)

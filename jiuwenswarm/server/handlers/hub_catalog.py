# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Serve the management Hub catalog without an Agent execution dependency."""

from jiuwenswarm.common.schema.agent import AgentResponse
from jiuwenswarm.server.context import RequestContext
from jiuwenswarm.server.runtime.equipment_hub_catalog import list_hub_equipment_catalog


async def handle_hub_equipment_catalog(ctx: RequestContext) -> None:
    """Return the unfiltered Hub directory; Gateway applies management policy."""
    payload = await list_hub_equipment_catalog()
    await ctx.sink.send_response(
        AgentResponse(
            request_id=ctx.request.request_id,
            channel_id=ctx.request.channel_id,
            ok=True,
            payload=payload,
            metadata=ctx.request.metadata,
        )
    )

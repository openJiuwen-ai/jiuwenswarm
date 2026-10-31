# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Serve the management Hub catalog without an Agent execution dependency."""

from jiuwenswarm.server.context import RequestContext
from jiuwenswarm.server.control.hub_catalog_service import handle_hub_catalog_request


async def handle_hub_equipment_catalog(ctx: RequestContext) -> None:
    """Use the same process-level catalog service as the WebSocket Front."""
    await ctx.sink.send_response(await handle_hub_catalog_request(ctx.request))

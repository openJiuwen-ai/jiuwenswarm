# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Process-level Hub catalog shared by HTTP and WS control requests."""

from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponse
from jiuwenswarm.server.runtime.equipment_hub_catalog import list_hub_equipment_catalog


async def handle_hub_catalog_request(request: AgentRequest) -> AgentResponse:
    """Read the public catalog without binding a user session or Agent runtime."""
    payload = await list_hub_equipment_catalog()
    return AgentResponse(
        request_id=request.request_id,
        channel_id=request.channel_id,
        ok=True,
        payload=payload,
        metadata=request.metadata,
    )

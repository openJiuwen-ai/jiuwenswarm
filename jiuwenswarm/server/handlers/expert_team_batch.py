# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Wire adapter for explicit expert-Team batch invitations."""

import logging

from jiuwenswarm.common.schema.agent import AgentResponse
from jiuwenswarm.common.utils import mask_sensitive
from jiuwenswarm.server.context import RequestContext
from jiuwenswarm.server.runtime.expert_team_batch import add_expert_teams

logger = logging.getLogger(__name__)


async def handle_expert_teams_add(ctx: RequestContext) -> None:
    try:
        payload = await add_expert_teams(ctx.request)
        ok = True
    except Exception as exc:
        logger.exception("Expert batch request failed")
        ok = False
        payload = {"error": mask_sensitive(str(exc)), "code": "EXPERT_TEAMS_ADD_FAILED"}
    await ctx.sink.send_response(AgentResponse(
        request_id=ctx.request.request_id, channel_id=ctx.request.channel_id,
        ok=ok, payload=payload,
    ))

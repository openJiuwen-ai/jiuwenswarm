# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""权限配置。permissions.* 这一组请求共用一个函数。"""

from __future__ import annotations

import logging

from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.context import RequestContext

logger = logging.getLogger(__name__)

_READ_ONLY_METHODS = frozenset(
    {
        ReqMethod.PERMISSIONS_TOOLS_GET,
        ReqMethod.PERMISSIONS_RULES_GET,
        ReqMethod.PERMISSIONS_APPROVAL_OVERRIDES_GET,
    }
)


async def handle_permissions_config(ctx: RequestContext) -> None:
    """处理 permissions.* 请求。改完配置后，在后台重新加载，不挡住这次回包。"""
    from jiuwenswarm.agents.harness.common.rails.permissions.permissions_config_rpc import (
        dispatch_permissions_config_request,
    )
    from jiuwenswarm.common.config import get_config

    request = ctx.request
    resp = dispatch_permissions_config_request(request)
    if resp.ok and request.req_method not in _READ_ONLY_METHODS:
        ctx.services.agent_manager.schedule_permissions_reload(get_config())
    await ctx.sink.send_response(resp)

# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Gateway proxies for the Designer execution-graph RPCs."""

from __future__ import annotations

from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.gateway.routing.e2a_proxy import proxy_unary_request

_DESIGNER_METHODS: tuple[tuple[ReqMethod, float | None], ...] = (
    (ReqMethod.DESIGNER_WORKSPACE_CREATE, None),
    (ReqMethod.DESIGNER_WORKSPACE_GET, None),
    (ReqMethod.DESIGNER_WORKSPACE_SESSION_CREATE, None),
    (ReqMethod.DESIGNER_WORKSPACE_SESSION_COMPOSE, None),
    (ReqMethod.DESIGNER_GRAPH_GET, None),
    (ReqMethod.DESIGNER_GRAPH_LIST, None),
    (ReqMethod.DESIGNER_GRAPH_SAVE, None),
    (ReqMethod.DESIGNER_GRAPH_PATCH, None),
    (ReqMethod.DESIGNER_MEDIA_CAPABILITIES, None),
    (ReqMethod.DESIGNER_RUN_START, None),
    (ReqMethod.DESIGNER_RUN_GET, None),
    (ReqMethod.DESIGNER_RUN_PAUSE, None),
    (ReqMethod.DESIGNER_RUN_CANCEL, None),
)


def register_designer_handlers(channel, resolve_client):
    def proxy(req_method: ReqMethod, timeout_seconds: float | None):
        async def handler(ws, req_id, params, session_id, user_id=None):
            await proxy_unary_request(
                channel=channel,
                agent_client=resolve_client(),
                ws=ws,
                req_id=req_id,
                params=params if isinstance(params, dict) else {},
                session_id=session_id,
                user_id=user_id,
                req_method=req_method,
                label=req_method.value,
                timeout_seconds=timeout_seconds,
            )

        return handler

    for req_method, timeout_seconds in _DESIGNER_METHODS:
        channel.register_method(req_method.value, proxy(req_method, timeout_seconds))

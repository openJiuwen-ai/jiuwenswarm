# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Gateway proxies for the WebUI extension dispatch method.

``webui.dispatch`` 是上行 ui.* 事件的唯一入口，参数与回执由 AgentServer 的
``WebUIDispatchAdapter`` 处理；Gateway 只做薄代理（与 designer_handlers 同形）。
"""

from __future__ import annotations

from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.gateway.routing.e2a_proxy import proxy_unary_request

_WEBUI_METHODS: tuple[tuple[ReqMethod, float | None], ...] = (
    (ReqMethod.WEBUI_DISPATCH, 30.0),
)


def register_webui_handlers(channel, resolve_client):
    """注册 WebUI 上行方法到目标 AgentServer 的转发 handler。"""

    for req_method, timeout_seconds in _WEBUI_METHODS:

        def handler(
            ws,
            req_id,
            params,
            session_id,
            user_id=None,
            _req_method=req_method,
            _timeout_seconds=timeout_seconds,
        ):
            return proxy_unary_request(
                channel=channel,
                agent_client=resolve_client(),
                ws=ws,
                req_id=req_id,
                params=params if isinstance(params, dict) else {},
                session_id=session_id,
                user_id=user_id,
                req_method=_req_method,
                label=_req_method.value,
                timeout_seconds=_timeout_seconds,
            )

        channel.register_method(req_method.value, handler)


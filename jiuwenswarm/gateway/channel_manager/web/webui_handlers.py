# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Gateway proxies for the WebUI extension dispatch method.

``webui.dispatch`` 是上行 ui.* 事件的唯一入口，参数与回执由 AgentServer 的
``WebUIDispatchAdapter`` 处理；Gateway 只做薄代理（与 designer_handlers 同形）。
``webui.settings.catalog`` 取 AgentServer 的目录后追加 Gateway 域包的设置声明
（AgentServer S_18、Gateway S_10 第 2 节），两侧各自列出自己的包。
"""

from __future__ import annotations

import logging

from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.gateway.routing.e2a_proxy import fetch_agent_unary, proxy_unary_request

logger = logging.getLogger(__name__)

_WEBUI_METHODS: tuple[tuple[ReqMethod, float | None], ...] = (
    (ReqMethod.WEBUI_DISPATCH, 30.0),
)
_CATALOG_TIMEOUT_SECONDS = 30.0


def _gateway_settings() -> list[dict]:
    """Settings declarations of active Gateway packages; empty without the extension runtime."""
    from jiuwenswarm.gateway.extensions_v2.assembly import get_gateway_runtime

    runtime = get_gateway_runtime()
    return runtime.browser.settings_contributions() if runtime is not None else []


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
            ws_id = str(getattr(ws, "_jiuwen_ws_id", "") or "").strip()
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
                metadata={"ws_id": ws_id} if ws_id else None,
            )

        channel.register_method(req_method.value, handler)

    def settings_catalog(ws, req_id, params, session_id, user_id=None):
        from jiuwenswarm.gateway.extensions_v2.webui.settings_contributions import merge_catalog

        ws_id = str(getattr(ws, "_jiuwen_ws_id", "") or "").strip()
        # Same proxy as before; on success the Gateway appends its own packages (S_18, Gateway S_10 section 2).
        return proxy_unary_request(
            channel=channel,
            agent_client=resolve_client(),
            ws=ws,
            req_id=req_id,
            params=params if isinstance(params, dict) else {},
            session_id=session_id,
            user_id=user_id,
            req_method=ReqMethod.WEBUI_SETTINGS_CATALOG,
            label=ReqMethod.WEBUI_SETTINGS_CATALOG.value,
            timeout_seconds=_CATALOG_TIMEOUT_SECONDS,
            metadata={"ws_id": ws_id} if ws_id else None,
            transform_payload=lambda payload: merge_catalog(payload, _gateway_settings()),
        )

    channel.register_method(ReqMethod.WEBUI_SETTINGS_CATALOG.value, settings_catalog)

    async def on_disconnect(ws, session_ids):
        del session_ids
        ws_id = str(getattr(ws, "_jiuwen_ws_id", "") or "").strip()
        if not ws_id:
            return
        resolve_user = getattr(channel, "connection_user_id", None)
        user_id = resolve_user(ws) if callable(resolve_user) else None
        ok, payload = await fetch_agent_unary(
            agent_client=resolve_client(),
            req_method=ReqMethod.WEBUI_CONNECTION_CLOSED,
            params={},
            session_id=None,
            user_id=user_id,
            channel_id=channel.channel_id,
            metadata={"ws_id": ws_id},
            label="webui connection cleanup",
            timeout_seconds=5.0,
        )
        if not ok:
            logger.warning("WebUI connection cleanup failed: ws_id=%s code=%s", ws_id, payload.get("code"))

    register_disconnect = getattr(channel, "on_disconnect", None)
    if callable(register_disconnect):
        register_disconnect(on_disconnect)


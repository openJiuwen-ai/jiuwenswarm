# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Forward V2 package-management requests to their owning AgentServer."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.gateway.routing.e2a_proxy import proxy_unary_request


def register_extension_package_handlers(channel: Any, resolve_client: Callable[[], Any]) -> None:
    """Register thin proxies; the Gateway never opens package files."""

    def create_handler(method: ReqMethod) -> Callable[..., Awaitable[None]]:
        async def handler(
            ws: Any,
            req_id: str,
            params: dict[str, Any],
            session_id: str | None,
            user_id: str | None = None,
        ) -> None:
            await proxy_unary_request(
                channel=channel,
                agent_client=resolve_client(),
                ws=ws,
                req_id=req_id,
                params=params,
                session_id=session_id,
                user_id=user_id,
                req_method=method,
                label=method.value,
                timeout_seconds=120.0,
                drop_params=("session_id",),
                preserve_error_payload=True,
            )

        return handler

    for method in (
        ReqMethod.EXTENSIONS_V2_QUERY,
        ReqMethod.EXTENSIONS_V2_INSTALL,
        ReqMethod.EXTENSIONS_V2_UNINSTALL,
        ReqMethod.EXTENSIONS_V2_RELOAD,
    ):
        channel.register_method(method.value, create_handler(method))

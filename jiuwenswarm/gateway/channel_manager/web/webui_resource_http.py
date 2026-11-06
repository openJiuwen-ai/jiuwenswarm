# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Serve AgentServer WebUI files through the existing extension-assets proxy."""

from __future__ import annotations

import base64
import binascii
import re
from typing import TYPE_CHECKING

from fastapi import FastAPI, Request
from fastapi.responses import Response

from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.common.webui_resources import (
    MAX_RESOURCE_BYTES,
    RESOURCE_MIME_TYPES,
    RESOURCE_PREFIX,
    decode_resource_route,
    resource_path,
)
from jiuwenswarm.gateway.routing.e2a_proxy import fetch_agent_unary

if TYPE_CHECKING:
    from jiuwenswarm.gateway.channel_manager.web.web_connect import WebChannel


_RESOURCE_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": "sandbox allow-scripts allow-forms allow-popups allow-modals allow-downloads",
}


def attach_webui_resource_routes(app: FastAPI, channel: WebChannel) -> None:
    """Register before Gateway package assets; never read the Gateway filesystem."""

    async def resource(request: Request, route: str, resource_id: str, path: str) -> Response:
        try:
            user_id, session_id, connection_id = decode_resource_route(route)
            resource_path(path)
            if re.fullmatch(r"[A-Za-z0-9_-]{43}", resource_id) is None:
                raise ValueError("Invalid resource ID")
        except ValueError:
            return Response(status_code=404, headers=_RESOURCE_HEADERS)
        ok, result = await fetch_agent_unary(
            agent_client=channel.agent_client, req_method=ReqMethod.WEBUI_RESOURCE_READ,
            params={"resource_id": resource_id, "path": path, "connection_id": connection_id},
            session_id=session_id or None, user_id=user_id or None, channel_id="web",
            label="WebUI resources",
        )
        if not ok:
            status = 404 if result.get("code") == "NOT_FOUND" else 503
            return Response(status_code=status, headers=_RESOURCE_HEADERS)
        encoded = result.get("data")
        media_type = result.get("media_type")
        if (not isinstance(encoded, str) or len(encoded) > 4 * ((MAX_RESOURCE_BYTES + 2) // 3)
                or media_type not in RESOURCE_MIME_TYPES.values()):
            return Response(status_code=502, headers=_RESOURCE_HEADERS)
        try:
            data = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error):
            return Response(status_code=502, headers=_RESOURCE_HEADERS)
        if len(data) > MAX_RESOURCE_BYTES:
            return Response(status_code=502, headers=_RESOURCE_HEADERS)
        headers = {**_RESOURCE_HEADERS, "Content-Type": media_type, "Content-Length": str(len(data))}
        return Response(content=b"" if request.method == "HEAD" else data, headers=headers)

    app.add_api_route(f"{RESOURCE_PREFIX}/{{route}}/{{resource_id}}/{{path:path}}", resource, methods=["GET", "HEAD"])

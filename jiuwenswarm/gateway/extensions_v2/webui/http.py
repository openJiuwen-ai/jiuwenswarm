# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""HTTP header events and read-only assets for the local browser bridge."""

from __future__ import annotations

import asyncio
from types import MappingProxyType

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from openjiuwen.core.events import Event, SessionContext

from jiuwenswarm.gateway.extensions_v2.assembly import get_gateway_runtime
from jiuwenswarm.gateway.extensions_v2.models import EVENT_GATEWAY_REQUEST, GatewayRequestInput, GatewayRequestOutput


def attach_browser_routes(app: FastAPI, runtime_provider=get_gateway_runtime) -> None:
    """Attach generic request interception without encoding a login policy."""

    @app.middleware("http")
    async def extension_http(request: Request, call_next):
        runtime = runtime_provider()
        if runtime is None or not runtime.browser.enabled:
            return await call_next(request)
        context = MappingProxyType({"headers": dict(request.headers)})
        request_input = GatewayRequestInput(
            method=request.method, request_context=context, transport="http", path=request.url.path,
            headers=MappingProxyType(dict(request.headers)),
        )
        try:
            result = await asyncio.wait_for(runtime.bus.trigger(
                Event(EVENT_GATEWAY_REQUEST, request_input), SessionContext("gateway:http"),
            ), timeout=5.0)
        except Exception:
            return JSONResponse({"code": "EXTENSION_FAILED", "message": "extension request failed"}, status_code=503)
        if not isinstance(result, GatewayRequestOutput):
            return JSONResponse({"code": "EXTENSION_UNHANDLED"}, status_code=503)
        if result.route == "forward":
            return await call_next(request)
        if not result.handled:
            return JSONResponse({"code": "EXTENSION_UNHANDLED"}, status_code=503)
        if result.route == "consume":
            return JSONResponse(dict(result.response or {}))
        if result.route == "deny":
            error = dict(result.error or {})
            status = error.pop("status", 403)
            if not isinstance(status, int) or isinstance(status, bool) or not 400 <= status <= 599:
                status = 403
            return JSONResponse(error, status_code=status)
        return JSONResponse({"code": "UNSUPPORTED_HTTP_ROUTE"}, status_code=400)

    async def package_asset(package_id: str, generation: str, path: str):
        runtime = runtime_provider()
        if runtime is None:
            return JSONResponse({"code": "NOT_FOUND"}, status_code=404)
        try:
            resource = runtime.browser.asset_file(package_id, generation, path)
        except (ValueError, StopIteration):
            return JSONResponse({"code": "NOT_FOUND"}, status_code=404)
        return FileResponse(resource, headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})

    app.add_api_route("/extension-assets/{package_id}/{generation}/{path:path}", package_asset, methods=["GET", "HEAD"])

# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""HTTP header events and read-only assets for the local browser bridge."""

from __future__ import annotations

import asyncio
from types import MappingProxyType

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from urllib.parse import quote
from openjiuwen.core.events import Event, SessionContext

from jiuwenswarm.gateway.extensions_v2.assembly import get_gateway_runtime
from jiuwenswarm.gateway.extensions_v2.models import EVENT_GATEWAY_REQUEST, GatewayRequestInput, GatewayRequestOutput
from jiuwenswarm.gateway.extensions_v2.webui.bridge import ASSET_PREFIX
from jiuwenswarm.gateway.extensions_v2.webui.transfers import DOWNLOAD_PREFIX, UPLOAD_PREFIX

# Assets are documents of an opaque origin even when opened directly, so they
# can never read the frontend's storage or act as the signed-in page. The
# sandbox keeps the abilities the Iframe component grants (forms, popups,
# modals, downloads) but never allow-same-origin; sandboxed pages fetch other
# assets cross-origin, hence the CORS header.
CORS_HEADERS = MappingProxyType({"Access-Control-Allow-Origin": "*"})
ASSET_HEADERS = MappingProxyType({
    **CORS_HEADERS,
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "sandbox allow-scripts allow-forms allow-popups allow-modals allow-downloads",
})
#: Exports are saved, never rendered (S_10 section 7.2).
DOWNLOAD_HEADERS = MappingProxyType({
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "sandbox",
})
#: Ticket routes: the one-time ticket decides access instead of request policy.
TICKET_PREFIXES = (UPLOAD_PREFIX, DOWNLOAD_PREFIX)


def attach_browser_routes(app: FastAPI, runtime_provider=get_gateway_runtime) -> None:
    """Attach generic request interception without encoding a login policy."""

    @app.middleware("http")
    async def extension_http(request: Request, call_next):
        runtime = runtime_provider()
        if runtime is None or not runtime.browser.enabled:
            return await call_next(request)
        if request.url.path.startswith(TICKET_PREFIXES):
            # Imports and exports carry one-time tickets issued to a connection whose
            # request the policy already continued; the ticket decides access.
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
            return JSONResponse({"code": "NOT_FOUND"}, status_code=404, headers=dict(CORS_HEADERS))
        try:
            resource = runtime.browser.asset_file(package_id, generation, path)
        except (ValueError, StopIteration):
            return JSONResponse({"code": "NOT_FOUND"}, status_code=404, headers=dict(CORS_HEADERS))
        return FileResponse(resource, headers=dict(ASSET_HEADERS))

    app.add_api_route(ASSET_PREFIX + "{package_id}/{generation}/{path:path}", package_asset, methods=["GET", "HEAD"])

    async def upload_file(upload_id: str, path: str, request: Request):
        runtime = runtime_provider()
        if runtime is None:
            return JSONResponse({"code": "NOT_FOUND"}, status_code=404)
        status = await runtime.browser.transfers.receive(
            upload_id, path, request.stream(), request.client.host if request.client else None,
        )
        return Response(status_code=status, headers={"Cache-Control": "no-store"})

    async def download_file(token: str, request: Request):
        runtime = runtime_provider()
        offer = None if runtime is None else runtime.browser.transfers.download(
            token, request.client.host if request.client else None, consume=request.method == "GET",
        )
        if offer is None:
            return JSONResponse({"code": "NOT_FOUND"}, status_code=404)
        headers = {**DOWNLOAD_HEADERS,
                   "Content-Disposition": f"attachment; filename*=UTF-8''{quote(offer.filename, safe='')}"}
        return FileResponse(offer.path, media_type="application/octet-stream", headers=headers)

    app.add_api_route(UPLOAD_PREFIX + "{upload_id}/{path:path}", upload_file, methods=["PUT"])
    app.add_api_route(DOWNLOAD_PREFIX + "{token}", download_file, methods=["GET", "HEAD"])

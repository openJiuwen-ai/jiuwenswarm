# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""File-api HTTP entry must continue a caller-supplied trace."""

from __future__ import annotations

from uuid import UUID

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport
from starlette.requests import Request

from jiuwenswarm.gateway import telemetry
from jiuwenswarm.gateway.channel_manager.web.container_file_http import (
    _bind_file_api_request_trace,
    _incoming_trace_context,
)


def _request(headers: dict[str, str]) -> Request:
    return Request({
        "type": "http",
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/file-api/upload",
        "raw_path": b"/file-api/upload",
        "query_string": b"",
        "headers": [
            (name.lower().encode("latin-1"), value.encode("latin-1"))
            for name, value in headers.items()
        ],
        "client": ("127.0.0.1", 12345),
        "server": ("127.0.0.1", 19000),
    })


def test_incoming_trace_context_collects_only_trace_headers() -> None:
    context = _incoming_trace_context(_request({
        "Traceparent": "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
        "Tracestate": "vendor=value",
        "X-Trace-Id": "4906fe76-a145-4693-b5b9-585038270249",
        "X-User-Id": "ignored",
    }))

    assert context == {
        "traceparent": "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
        "tracestate": "vendor=value",
        "x-trace-id": "4906fe76-a145-4693-b5b9-585038270249",
    }


@pytest.mark.asyncio
async def test_file_api_trace_prefers_traceparent() -> None:
    telemetry.configure_gateway_telemetry({"telemetry": {"enabled": True, "exporter": "none"}})
    request = _request({
        "traceparent": "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
        "tracestate": "vendor=value",
        "X-Trace-Id": "4906fe76-a145-4693-b5b9-585038270249",
    })

    async with _bind_file_api_request_trace(request) as operation:
        assert operation is not None
        assert operation.trace_id == "4bf92f3577b34da6a3ce929d0e0e4736"
        assert operation.span.parent is not None
        assert operation.span.parent.is_remote
        assert telemetry.current_request_trace() is operation

        outbound = telemetry.inject_request_headers({})
        assert outbound["X-Trace-Id"] == str(UUID(hex=operation.trace_id))
        assert outbound["traceparent"].split("-")[1] == operation.trace_id

        child = telemetry.start_gateway_trace("gw.agent.create")
        assert child is not None
        assert child.trace_id == operation.trace_id
        child.finish("success")

    assert telemetry.current_request_trace() is None
    assert operation.finished


@pytest.mark.asyncio
async def test_file_api_trace_falls_back_to_x_trace_id() -> None:
    telemetry.configure_gateway_telemetry({"telemetry": {"enabled": True, "exporter": "none"}})
    request = _request({
        "X-Trace-Id": "4906fe76-a145-4693-b5b9-585038270249",
    })

    async with _bind_file_api_request_trace(request) as operation:
        assert operation is not None
        assert operation.trace_id == "4906fe76a1454693b5b9585038270249"
        assert operation.span.parent is None
        assert telemetry.current_request_trace() is operation

    assert telemetry.current_request_trace() is None
    assert operation.finished


@pytest.mark.asyncio
async def test_file_api_trace_keeps_existing_operation() -> None:
    telemetry.configure_gateway_telemetry({"telemetry": {"enabled": True, "exporter": "none"}})
    existing = telemetry.start_request_trace(
        "outer", "upload", channel="file-api", connection_id="c",
    )
    assert existing is not None
    with telemetry.bind_request_trace(existing):
        async with _bind_file_api_request_trace(_request({
            "X-Trace-Id": "4906fe76-a145-4693-b5b9-585038270249",
        })) as operation:
            assert operation is existing
    existing.finish("success")


@pytest.mark.asyncio
async def test_file_api_trace_middleware_reaches_route() -> None:
    telemetry.configure_gateway_telemetry({"telemetry": {"enabled": True, "exporter": "none"}})
    app = FastAPI()

    @app.middleware("http")
    async def _middleware(request, call_next):
        async with _bind_file_api_request_trace(request):
            return await call_next(request)

    @app.post("/file-api/upload")
    async def _upload():
        operation = telemetry.current_request_trace()
        return {"trace_id": operation.trace_id if operation is not None else ""}

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
    ) as client:
        response = await client.post(
            "/file-api/upload",
            headers={
                "traceparent": "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
                "X-Trace-Id": "4906fe76-a145-4693-b5b9-585038270249",
            },
        )

    assert response.status_code == 200
    assert response.json()["trace_id"] == "4bf92f3577b34da6a3ce929d0e0e4736"

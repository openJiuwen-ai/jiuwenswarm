# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Management catalog traverses Gateway, routeless E2A and Server HTTP."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from fastapi import FastAPI

from gateway.enterprise.config.manager_config.routers.application_config_routers import application_config_router
from jiuwenswarm.common.e2a.models import E2AEnvelope
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.extensions.runtime_management_extension.pod_http_client import HttpSseAgentServerClient
from jiuwenswarm.extensions.runtime_management_extension.runtime_routed_client import RuntimeRoutedAgentClient
from jiuwenswarm.server.agent_http_routes import build_http_app
from jiuwenswarm.server.app_services import AppServices
from jiuwenswarm.server.context import AgentServerServices


@pytest.mark.asyncio
@pytest.mark.parametrize("hub_failed", [False, True])
async def test_catalog_full_http_chain_without_session_or_agent(monkeypatch, hub_failed):
    monkeypatch.delenv("JIUWENSWARM_EDITION", raising=False)
    monkeypatch.setenv("GATEWAY_RUNTIME_DEFAULT_HTTP_BASE", "http://server")
    payload = {"items": [], "error": "Hub down"} if hub_failed else {
        "items": [
            {"kind": "agent_template", "id": "asset-a"},
            {"kind": "agent_group", "id": "32c223d69b834c35b40bc3935c6f9f67"},
        ],
        "error": "",
    }
    query = AsyncMock(return_value=payload)
    monkeypatch.setattr("jiuwenswarm.server.control.hub_catalog_service.list_hub_equipment_catalog", query)

    execution_runtime = Mock(side_effect=AssertionError("must not initialize Agent execution"))
    monkeypatch.setattr(AgentServerServices, "execution_runtime", execution_runtime)
    server = build_http_app(AppServices())
    route = SimpleNamespace(route=AsyncMock(), touch=AsyncMock())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server)) as server_http:
        http_client = HttpSseAgentServerClient(http_client=server_http)
        client = RuntimeRoutedAgentClient(route_client=route, http_client=http_client)
        await client.connect("")
        gateway = FastAPI()
        gateway.state.manager_agent_channel = SimpleNamespace(agent_client=client)
        gateway.include_router(application_config_router, prefix="/api/v1")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=gateway), base_url="http://gateway") as web:
            response = await web.get("/api/v1/package-allowlists/catalog")
    assert response.status_code == 200
    body = response.json()
    assert body["code"] == 200
    assert body["data"]["error"] == payload["error"]
    assert body["data"]["items"] == ([] if hub_failed else [{"kind": "agent_template", "id": "asset-a"}])
    query.assert_awaited_once_with()
    execution_runtime.assert_not_called()
    route.route.assert_not_awaited()
    route.touch.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["missing", "offline", "disconnected"])
async def test_catalog_server_unavailable_keeps_management_response(state):
    gateway = FastAPI()
    if state != "missing":
        client = SimpleNamespace(
            server_ready=state != "offline",
            send_request=AsyncMock(side_effect=ConnectionError("Server disconnected")),
        )
        gateway.state.manager_agent_channel = SimpleNamespace(agent_client=lambda: client)
    gateway.include_router(application_config_router, prefix="/api/v1")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=gateway), base_url="http://gateway") as web:
        response = await web.get("/api/v1/package-allowlists/catalog")
    assert response.status_code == 200
    assert response.json()["data"]["items"] == []
    assert response.json()["data"]["error"]


@pytest.mark.asyncio
async def test_catalog_runtime_uses_default_http_without_routing(monkeypatch):
    monkeypatch.setenv("GATEWAY_RUNTIME_DEFAULT_HTTP_BASE", "http://management-server:8766/")
    route = SimpleNamespace(route=AsyncMock(), touch=AsyncMock())
    response = SimpleNamespace(ok=True, payload={"items": [], "error": ""})
    http = SimpleNamespace(send_request=AsyncMock(return_value=response))
    client = RuntimeRoutedAgentClient(route_client=route, http_client=http)
    await client.connect("")
    envelope = E2AEnvelope(request_id="catalog", channel="gateway", method=ReqMethod.HUB_EQUIPMENT_CATALOG.value, params={})
    assert await client.send_request(envelope) is response
    http.send_request.assert_awaited_once_with(envelope, base_url="http://management-server:8766")
    route.route.assert_not_awaited()
    route.touch.assert_not_awaited()

"""Business validation remains on AgentServer after Gateway becomes opaque."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.front.router import MethodRouter
from jiuwenswarm.server.http_invoke import invoke_stream, invoke_unary
from jiuwenswarm.server.request_policy import request_policy_error


@pytest.mark.parametrize("method", [
    ReqMethod.SWARMFLOW_PAUSE, ReqMethod.SWARMFLOW_RESUME, ReqMethod.SWARMFLOW_STOP,
])
@pytest.mark.parametrize("params", [
    {"run_id": "r" * 257}, {"workflow_run_id": "r" * 257},
    {"run_id": "run-1", "workflow_run_id": "run-2"},
    {"run_id": []}, {"run_id": " leading"}, {"run_id": "bad" + chr(127)},
])
async def test_front_rejects_workflow_ids_before_runtime(method, params, monkeypatch):
    monkeypatch.setenv("JIUWENSWARM_EDITION", "personal")
    admission = SimpleNamespace(dispatch=AsyncMock())
    router = MethodRouter(None, admission)
    ws = SimpleNamespace(send=AsyncMock())
    request = AgentRequest(request_id="policy", channel_id="web", req_method=method, params=params)
    await router.dispatch(ws, request, asyncio.Lock())
    admission.dispatch.assert_not_awaited()
    ws.send.assert_awaited_once()
    assert "BAD_REQUEST" in ws.send.await_args.args[0]


@pytest.mark.parametrize("params", [
    {"run_id": "r" * 256}, {"workflow_run_id": "run-1"},
    {"run_id": "run-1", "workflow_run_id": "run-1"},
])
async def test_front_accepts_valid_workflow_ids(params, monkeypatch):
    monkeypatch.setenv("JIUWENSWARM_EDITION", "personal")
    admission = SimpleNamespace(dispatch=AsyncMock())
    router = MethodRouter(None, admission)
    ws = SimpleNamespace(send=AsyncMock())
    lock = asyncio.Lock()
    request = AgentRequest(request_id="policy", channel_id="web", req_method=ReqMethod.SWARMFLOW_PAUSE, params=params)
    await router.dispatch(ws, request, lock)
    admission.dispatch.assert_awaited_once_with(ws, request, lock)
    ws.send.assert_not_awaited()


@pytest.mark.parametrize("method", ["skills.import_upload", "agents.create", "plugins.install", "mcp.server.add"])
@pytest.mark.parametrize("invoke", [invoke_unary, invoke_stream])
async def test_enterprise_http_rejects_writes_before_handler(method, invoke, monkeypatch):
    monkeypatch.setenv("JIUWENSWARM_EDITION", "enterprise")
    request = AgentRequest(request_id="write", channel_id="web", req_method=method, params={})
    sink = SimpleNamespace(send_response=AsyncMock(), close=AsyncMock())
    handler = AsyncMock()
    await invoke(SimpleNamespace(request=request, sink=sink), handler)
    handler.assert_not_awaited()
    sink.send_response.assert_awaited_once()
    response = sink.send_response.await_args.args[0]
    assert response.payload["code"] == "FORBIDDEN"
    assert not response.ok


@pytest.mark.parametrize("method", [
    "skills.files.list", "skills.files.get", "skills.versions.list", "skills.source.install",
    "skills.toggle", "skills.uninstall", "skills.import_local", "skills.update", "future.execute",
])
def test_enterprise_allowed_business_methods(method, monkeypatch):
    monkeypatch.setenv("JIUWENSWARM_EDITION", "enterprise")
    request = AgentRequest(request_id="read", channel_id="web", req_method=method, params={})
    assert request_policy_error(request, web_http=True) is None


@pytest.mark.parametrize("channel", ["gateway", "__cron__", "tui", "http"])
def test_enterprise_internal_calls_are_not_web_writes(channel, monkeypatch):
    monkeypatch.setenv("JIUWENSWARM_EDITION", "enterprise")
    request = AgentRequest(request_id="internal", channel_id=channel, req_method="agents.create")
    assert request_policy_error(request, web_http=True) is None


def test_web_http_guard_survives_e2a_but_does_not_expand_to_websocket(monkeypatch):
    monkeypatch.setenv("JIUWENSWARM_EDITION", "enterprise")
    request = AgentRequest(request_id="web", channel_id="web", req_method="skills.import_upload")
    assert request_policy_error(request) is None
    request.metadata = {"transport": "web-http"}
    assert request_policy_error(request).payload["code"] == "FORBIDDEN"

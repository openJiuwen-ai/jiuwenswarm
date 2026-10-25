"""Persistent refresh is observable background work, never a model-save barrier."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer
from jiuwenswarm.server.jiuwenmate.web_adapter import JiuwenMateWebAdapter
from jiuwenswarm.common.schema.message import ReqMethod
from tests.unit_tests.server.test_gateway_adapter import _request


@pytest.mark.asyncio
async def test_model_save_returns_before_persistent_refresh_completes():
    started, release = asyncio.Event(), asyncio.Event()
    adapter = JiuwenMateWebAdapter()

    async def refresh(_payload):
        started.set()
        await release.wait()
        return True

    adapter.refresh_models = refresh
    server = SimpleNamespace(
        _agent_manager=SimpleNamespace(reload_agents_config=AsyncMock()),
        _jiuwenmate_web=adapter,
    )
    task = asyncio.create_task(AgentWebSocketServer._reload_after_config_save(
        server, {"models.defaults"}, reload_options={"reload_scopes": ["model"]},
    ))
    try:
        await asyncio.wait_for(started.wait(), 1)
        assert task.done(), "model-save acknowledgement still waits for Persistent republishing"
        assert task.result() is True
        assert adapter.model_refresh_status()["status"] == "pending"
    finally:
        release.set()
        await task
        await adapter.aclose()


@pytest.mark.asyncio
async def test_background_refresh_serializes_and_coalesces_to_latest_saved_payload():
    started, release = asyncio.Event(), asyncio.Event()
    adapter = JiuwenMateWebAdapter()
    seen = []

    async def refresh(payload):
        seen.append(payload["models"]["window"])
        if len(seen) == 1:
            started.set()
            await release.wait()
        return payload["models"]["window"] == 3000

    adapter.refresh_models = refresh
    schedule = getattr(adapter, "schedule_model_refresh", None)
    assert callable(schedule), "missing nonblocking refresh scheduler"
    schedule({"models": {"window": 1000}})
    await started.wait()
    schedule({"models": {"window": 2000}})
    latest = {"models": {"window": 3000}}
    schedule(latest)
    latest["models"]["window"] = 9999
    try:
        assert seen == [1000]
        assert adapter.model_refresh_status() == {"status": "pending", "generation": 3}
        release.set()
        await adapter.wait_model_refresh()
        assert seen == [1000, 3000]
        assert adapter.model_refresh_status() == {"status": "applied", "generation": 3}
    finally:
        release.set()
        await adapter.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("raises", [False, True])
async def test_background_failure_is_reported_without_leaking_credentials(raises):
    adapter = JiuwenMateWebAdapter()

    async def refresh(_payload):
        if raises:
            raise RuntimeError("api-key-secret-must-not-be-visible")
        return False

    adapter.refresh_models = refresh
    schedule = getattr(adapter, "schedule_model_refresh", None)
    assert callable(schedule), "missing nonblocking refresh scheduler"
    schedule({})
    await adapter.wait_model_refresh()
    try:
        status = adapter.model_refresh_status()
        assert status["status"] == "failed"
        assert status["generation"] == 1
        assert status["detail"]
        assert "api-key-secret" not in str(status)
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
async def test_refresh_status_does_not_open_a_store_or_runtime():
    def unavailable():
        pytest.fail("status query must not initialize Persistent data")

    adapter = JiuwenMateWebAdapter(service_factory=unavailable, manager_factory=unavailable)
    method = ReqMethod("models.refresh_status")
    assert method.value in adapter.methods
    response = await adapter.handle(_request(method, {}))
    assert response.ok
    assert response.payload == {"status": "applied", "generation": 0}


@pytest.mark.asyncio
async def test_adapter_can_refresh_again_after_runtime_close():
    adapter = JiuwenMateWebAdapter()
    adapter.refresh_models = AsyncMock(return_value=True)
    adapter.schedule_model_refresh({})
    await adapter.aclose()
    adapter.schedule_model_refresh({})
    await adapter.wait_model_refresh()
    assert adapter.refresh_models.await_count == 2
    assert adapter.model_refresh_status()["status"] == "applied"
    await adapter.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("remote", [False, True])
async def test_status_is_proxied_to_owning_worker_in_both_gateway_modes(monkeypatch, remote):
    from jiuwenswarm.common.e2a.agent_compat import e2a_to_agent_request
    from jiuwenswarm.gateway.channel_manager.web.app_web_handlers import WebHandlersBindParams, _register_web_handlers
    from jiuwenswarm.gateway.routing.agent_client import WebSocketAgentServerClient
    from jiuwenswarm.server.runtime.gateway_adapter.base import AdapterRegistry

    adapter = JiuwenMateWebAdapter()
    adapter.refresh_models = AsyncMock(return_value=False)
    adapter.schedule_model_refresh({})
    await adapter.wait_model_refresh()
    registry = AdapterRegistry()
    registry.register(adapter)
    received = []

    class Client:
        server_ready = True

        async def send_request(self, envelope):
            request = e2a_to_agent_request(envelope)
            assert request.req_method is ReqMethod.MODELS_REFRESH_STATUS
            assert request.user_id == "authenticated-user"
            assert request.session_id == "chat"
            received.append(request)
            return await registry.get(request.req_method.value).handle(request)

    client = Client() if remote else WebSocketAgentServerClient()
    if not remote:
        client._server_ready = True
        monkeypatch.setattr(client, "send_request", Client().send_request)
    channel = SimpleNamespace(
        channel_id="web", methods={}, send_response=AsyncMock(),
        on_connect=lambda _: None, on_disconnect=lambda _: None,
    )
    channel.register_method = lambda method, callback: channel.methods.update({method: callback})
    _register_web_handlers(WebHandlersBindParams(channel=channel, agent_client=client))
    await channel.methods["models.refresh_status"](object(), "status", {}, "chat", "authenticated-user")
    assert channel.send_response.await_args.kwargs["payload"] == adapter.model_refresh_status()
    assert channel.send_response.await_args.kwargs["ok"] is True
    assert len(received) == 1
    assert adapter._service is None and adapter._manager is None
    await adapter.aclose()


@pytest.mark.asyncio
async def test_latest_generation_remains_pending_and_can_recover_after_failure():
    first_started, first_release = asyncio.Event(), asyncio.Event()
    second_started, second_release = asyncio.Event(), asyncio.Event()
    adapter = JiuwenMateWebAdapter()

    async def refresh(payload):
        if payload["models"]["number"] == 1:
            first_started.set()
            await first_release.wait()
            return True
        second_started.set()
        await second_release.wait()
        return False

    adapter.refresh_models = refresh
    adapter.schedule_model_refresh({"models": {"number": 1}})
    await first_started.wait()
    adapter.schedule_model_refresh({"models": {"number": 2}})
    try:
        first_release.set()
        await second_started.wait()
        assert adapter.model_refresh_status() == {"status": "pending", "generation": 2}
        second_release.set()
        await adapter.wait_model_refresh()
        assert adapter.model_refresh_status()["status"] == "failed"
        adapter.refresh_models = AsyncMock(return_value=True)
        adapter.schedule_model_refresh({"models": {"number": 3}})
        await adapter.wait_model_refresh()
        assert adapter.model_refresh_status() == {"status": "applied", "generation": 3}
    finally:
        first_release.set()
        second_release.set()
        await adapter.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("reload_payload", [None, {}, {"permissions": {"enabled": False}}])
async def test_reload_without_models_preserves_latest_saved_snapshot(tmp_path, reload_payload):
    from jiuwenswarm.server.jiuwenmate.web_adapter import WEB_NAMESPACE
    from tests.unit_tests.server.jiuwenmate.test_persistent_agent_service import FakeModels, make_service, settings
    from tests.unit_tests.server.test_config_adapter_permission_reload import model_payload

    service = make_service(tmp_path, FakeModels(window=1048576))
    colleague = service.create_colleague(WEB_NAMESPACE, settings())
    adapter = JiuwenMateWebAdapter(service_factory=lambda: service)
    adapter._service = service
    server = SimpleNamespace(
        _agent_manager=SimpleNamespace(reload_agents_config=AsyncMock()), _jiuwenmate_web=adapter,
    )
    try:
        for payload in [model_payload(131072), reload_payload]:
            assert await AgentWebSocketServer._reload_after_config_save(
                server, {"models.defaults"}, config_payload=payload,
                reload_options={"reload_scopes": ["model"]},
            ) is True
        await adapter.wait_model_refresh()
        assert adapter.model_refresh_status() == {"status": "applied", "generation": 2}
        current = service.current_snapshot(WEB_NAMESPACE, colleague.agent_id).load_spec()
        assert current.model.model_request_config.context_window == 131072
    finally:
        await adapter.aclose()

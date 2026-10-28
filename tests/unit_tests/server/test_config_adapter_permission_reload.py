"""Config saves acknowledge legacy reload, then publish Persistent snapshots in background."""

from __future__ import annotations

import asyncio
import ast
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
import uuid

import pytest

from jiuwenswarm.common.e2a.wire_codec import encode_agent_response_for_wire, parse_agent_server_wire_unary
from jiuwenswarm.common.schema.agent import AgentResponse
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer
from jiuwenswarm.server.jiuwenmate.web_adapter import WEB_NAMESPACE, JiuwenMateWebAdapter
from jiuwenswarm.server.runtime.gateway_adapter.config_adapter import (
    ConfigAdapter,
    reload_runtime_after_config_save,
)
from tests.unit_tests.server.test_gateway_adapter import _request


@pytest.mark.asyncio
async def test_config_adapter_awaits_save_callback(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    def fake_register(channel, **kwargs):
        seen["on_config_saved"] = kwargs.get("on_config_saved")

        async def _config_set(ws, req_id, params, session_id):
            _ = (ws, params, session_id)
            applied = True
            callback = kwargs.get("on_config_saved")
            if callback is not None:
                applied = bool(await callback(
                    {"permissions_enabled"},
                    env_updates={},
                    config_payload={"permissions": {"enabled": False}},
                    reload_options={"reload_scopes": ["permissions"], "target_channel_id": "web"},
                ))
            await channel.send_response(
                ws, req_id, ok=True,
                payload={"updated": ["permissions_enabled"], "applied_without_restart": applied},
            )

        channel.register_method("config.set", _config_set)

    monkeypatch.setattr(
        "jiuwenswarm.common.config_panel.config_set_handlers.register_config_set_handlers",
        fake_register,
    )
    callback = AsyncMock(return_value=False)
    response = await ConfigAdapter(on_config_saved=callback).handle(
        _request(ReqMethod.CONFIG_SET, {"permissions_enabled": "false"})
    )

    assert seen["on_config_saved"] is callback
    callback.assert_awaited_once()
    assert response.payload["applied_without_restart"] is False


@pytest.mark.asyncio
async def test_permission_save_reloads_every_channel() -> None:
    manager = SimpleNamespace(reload_agents_config=AsyncMock())

    applied = await reload_runtime_after_config_save(
        manager,
        {"permissions_enabled"},
        env_updates={},
        config_payload={"permissions": {"enabled": False}},
        reload_options={"reload_scopes": ["permissions"], "target_channel_id": "web"},
    )

    assert applied is True
    manager.reload_agents_config.assert_awaited_once_with(
        {"permissions": {"enabled": False}},
        {},
        target_channel_id=None,
        reload_scopes={"permissions"},
    )


@pytest.mark.asyncio
async def test_non_permission_save_keeps_target_channel() -> None:
    manager = SimpleNamespace(reload_agents_config=AsyncMock())

    await reload_runtime_after_config_save(
        manager,
        {"models.defaults"},
        env_updates={"MODEL_NAME": "m1"},
        config_payload={"models": {}},
        reload_options={"reload_scopes": ["model"], "target_channel_id": "web"},
    )

    manager.reload_agents_config.assert_awaited_once_with(
        {"models": {}},
        {"MODEL_NAME": "m1"},
        target_channel_id="web",
        reload_scopes={"model"},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("scopes,expected_window", [(["model"], 131072), ([], 131072), (["permissions"], 1048576)])
async def test_config_save_refreshes_persistent_models_without_starting_a_runtime(tmp_path, scopes, expected_window):
    from tests.unit_tests.server.jiuwenmate.test_persistent_agent_service import FakeModels, make_service, settings

    models = FakeModels(window=1048576)
    service = make_service(tmp_path, models)
    colleague = service.create_colleague(WEB_NAMESPACE, settings())
    adapter = JiuwenMateWebAdapter(service_factory=lambda: service)
    # The database exists, but the Web/Feishu execution runtime has not started.
    adapter._service = service
    server = SimpleNamespace(
        _agent_manager=SimpleNamespace(reload_agents_config=AsyncMock()),
        _jiuwenmate_web=adapter,
    )
    models.window = 131072

    applied = await AgentWebSocketServer._reload_after_config_save(
        server, {"models.defaults"}, reload_options={"reload_scopes": scopes},
    )

    assert applied is True
    await adapter.wait_model_refresh()
    current = service.current_snapshot(WEB_NAMESPACE, colleague.agent_id).load_spec()
    assert current.model.model_request_config.context_window == expected_window
    assert adapter._manager is None


@pytest.mark.asyncio
async def test_model_reload_does_not_create_persistent_data_for_personal_agents(tmp_path, monkeypatch):
    monkeypatch.setattr("jiuwenswarm.server.jiuwenmate.service.default_data_dir", lambda: tmp_path / "absent")
    server = SimpleNamespace(
        _agent_manager=SimpleNamespace(reload_agents_config=AsyncMock()),
        _jiuwenmate_web=JiuwenMateWebAdapter(),
    )

    assert await AgentWebSocketServer._reload_after_config_save(
        server, {"models.defaults"}, reload_options={"reload_scopes": ["model"]},
    ) is True
    await server._jiuwenmate_web.wait_model_refresh()
    assert server._jiuwenmate_web._service is None
    assert not (tmp_path / "absent").exists()


@pytest.mark.asyncio
async def test_model_reload_reports_persistent_failure_separately_from_save():
    adapter = JiuwenMateWebAdapter()
    adapter.refresh_models = AsyncMock(return_value=False)
    server = SimpleNamespace(
        _agent_manager=SimpleNamespace(reload_agents_config=AsyncMock()),
        _jiuwenmate_web=adapter,
    )

    applied = await AgentWebSocketServer._reload_after_config_save(
        server, {"models.defaults"}, reload_options={"reload_scopes": ["model"]},
    )

    assert applied is True
    await adapter.wait_model_refresh()
    assert adapter.model_refresh_status()["status"] == "failed"


@pytest.mark.asyncio
async def test_model_reload_opens_an_existing_persistent_store_once(tmp_path, monkeypatch):
    from tests.unit_tests.server.jiuwenmate.test_persistent_agent_service import FakeModels, make_service, settings

    models = FakeModels(window=1048576)
    service = make_service(tmp_path, models)
    colleague = service.create_colleague(WEB_NAMESPACE, settings())
    created = []

    def service_factory():
        created.append(service)
        return service

    monkeypatch.setattr("jiuwenswarm.server.jiuwenmate.service.default_data_dir", lambda: tmp_path)
    adapter = JiuwenMateWebAdapter(service_factory=service_factory)
    server = SimpleNamespace(
        _agent_manager=SimpleNamespace(reload_agents_config=AsyncMock()),
        _jiuwenmate_web=adapter,
    )
    models.window = 131072

    for _ in range(2):
        assert await AgentWebSocketServer._reload_after_config_save(
            server, {"models.defaults"}, reload_options={"reload_scopes": ["model"]},
        ) is True

    await adapter.wait_model_refresh()
    assert created == [service]
    current = service.current_snapshot(WEB_NAMESPACE, colleague.agent_id)
    assert current.config_version == "v2"
    assert current.load_spec().model.model_request_config.context_window == 131072
    assert adapter._manager is None


@pytest.mark.asyncio
@pytest.mark.parametrize("pending_edits", [False, True])
async def test_explicit_reload_updates_persistent_models_or_reports_unapplied_drafts(
    tmp_path, monkeypatch, pending_edits,
):
    from tests.unit_tests.server.jiuwenmate.test_persistent_agent_service import FakeModels, make_service, settings

    models = FakeModels(window=1048576)
    service = make_service(tmp_path, models)
    colleague = service.create_colleague(WEB_NAMESPACE, settings())
    if pending_edits:
        service.update_settings(WEB_NAMESPACE, colleague.agent_id, settings(name="Draft"))
    adapter = JiuwenMateWebAdapter(service_factory=lambda: service)
    adapter._service = service
    server = SimpleNamespace(
        _agent_manager=SimpleNamespace(reload_agents_config=AsyncMock()),
        _jiuwenmate_web=adapter,
        _schedule_tokenizer_warmup=Mock(),
        schedule_image_modality_warmup=Mock(),
        _proactive_engine=None,
    )
    monkeypatch.setattr("jiuwenswarm.server.runtime.opencode_zen.warm_zen_free_models", AsyncMock())
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.stop_all_paused_team_session_runtimes_across_managers",
        AsyncMock(return_value=0),
    )
    send = AsyncMock()
    monkeypatch.setattr("jiuwenswarm.server.agent_ws_server.send_wire_payload", send)
    await AgentWebSocketServer._handle_agent_reload_config(
        server, object(),
        _request(ReqMethod.AGENT_RELOAD_CONFIG, {"config": model_payload(131072), "reload_scopes": ["model"]}),
        asyncio.Lock(),
    )

    wire = send.await_args.args[1]
    assert wire["response_kind"] == "e2a.complete"
    response = parse_agent_server_wire_unary(wire)
    assert response.ok
    assert response.payload["persistent_model_refresh"]["status"] == "pending"
    await adapter.wait_model_refresh()
    assert adapter.model_refresh_status()["status"] == ("failed" if pending_edits else "applied")
    # Runtime initialization repeats this refresh without a payload.
    assert service.refresh_published(WEB_NAMESPACE) is not pending_edits
    current = service.current_snapshot(WEB_NAMESPACE, colleague.agent_id)
    expected_window = 1048576 if pending_edits else 131072
    assert current.load_spec().model.model_request_config.context_window == expected_window
    if pending_edits:
        assert service.get_colleague(WEB_NAMESPACE, colleague.agent_id).has_unpublished_changes
        service.publish(WEB_NAMESPACE, colleague.agent_id)
        published = service.current_snapshot(WEB_NAMESPACE, colleague.agent_id).load_spec()
        assert published.model.model_request_config.context_window == 131072


@pytest.mark.asyncio
async def test_persistent_refresh_exception_returns_unapplied_without_creating_a_runtime(tmp_path, monkeypatch):
    (tmp_path / "agents.sqlite3").touch()
    monkeypatch.setattr("jiuwenswarm.server.jiuwenmate.service.default_data_dir", lambda: tmp_path)

    def unavailable_service():
        raise RuntimeError("store unavailable")

    adapter = JiuwenMateWebAdapter(service_factory=unavailable_service)
    assert await adapter.refresh_models() is False
    assert adapter._service is None and adapter._manager is None


def model_payload(window):
    return {"models": {"defaults": [{
        "model_client_config": {
            "model_name": "deepseek", "client_provider": "OpenAI",
            "api_key": "sk-test", "api_base": "https://example.invalid/v1",
        },
        "model_config_obj": {"context_window": window},
        "is_default": True,
    }]}}


@pytest.mark.asyncio
async def test_proxied_model_save_notifies_persistent_agents_with_the_saved_config(tmp_path, monkeypatch):
    from tests.unit_tests.server.jiuwenmate.test_persistent_agent_service import FakeModels, make_service, settings

    service = make_service(tmp_path, FakeModels(window=1048576))
    colleague = service.create_colleague(WEB_NAMESPACE, settings())
    adapter = JiuwenMateWebAdapter(service_factory=lambda: service)
    adapter._service = service
    server = SimpleNamespace(
        _agent_manager=SimpleNamespace(reload_agents_config=AsyncMock()), _jiuwenmate_web=adapter,
    )
    saved = model_payload(131072)
    monkeypatch.setattr("jiuwenswarm.common.config.get_config", lambda: saved)
    monkeypatch.setattr("jiuwenswarm.common.config_panel.models_handlers.get_config", lambda: saved)
    monkeypatch.setattr(
        "jiuwenswarm.common.config_panel.model_resource_handlers._resource_payload",
        AsyncMock(return_value={"model": {"model_id": "test-model"}}),
    )

    async def on_saved(*args, **kwargs):
        return await AgentWebSocketServer._reload_after_config_save(server, *args, **kwargs)

    response = await ConfigAdapter(on_config_saved=on_saved).handle(_request(ReqMethod.MODELS_UPSERT, {"model": {}}))

    assert response.ok and response.payload["applied_without_restart"] is True
    await adapter.wait_model_refresh()
    current = service.current_snapshot(WEB_NAMESPACE, colleague.agent_id).load_spec()
    assert current.model.model_request_config.context_window == 131072


@pytest.mark.asyncio
async def test_partial_persistent_reload_does_not_retry_or_restart_the_gateway():
    # Execute the real nested save callback without constructing the Gateway.
    source = Path(__file__).resolve().parents[3] / "jiuwenswarm/gateway/app_gateway.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    callback = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == "_on_config_saved")
    response = parse_agent_server_wire_unary(encode_agent_response_for_wire(AgentResponse(
        request_id="model-reload", channel_id="web", ok=False,
        payload={"code": "PERSISTENT_MODELS_NOT_APPLIED", "error": "unpublished edits"},
    ), response_id="model-reload"))
    client = SimpleNamespace(
        set_or_update_server_config=Mock(),
        send_request=AsyncMock(return_value=response),
    )
    restart = Mock()
    scope = {
        "Any": object, "client": client, "ReqMethod": ReqMethod, "uuid_module": uuid,
        "e2a_from_agent_fields": lambda **fields: fields, "logger": logging.getLogger(__name__),
        "_schedule_gateway_restart": restart, "restart_request": object(), "asyncio": asyncio,
    }
    exec(compile(ast.Module(body=[callback], type_ignores=[]), str(source), "exec"), scope)

    assert await scope["_on_config_saved"]({"models.defaults"}, config_payload=model_payload(131072)) is False
    assert client.send_request.await_count == 1
    restart.assert_not_called()

"""Verify the production transport handler's feature gate and service seam."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.handlers import rsi
from jiuwenswarm.common.e2a.models import E2AResponse
from jiuwenswarm.common.e2a.gateway_normalize import e2a_response_to_agent_response


def context(method):
    return SimpleNamespace(
        request=SimpleNamespace(req_method=method, params={}),
        request_id="rsi-request", channel_id="web", sink=SimpleNamespace(send_wire=AsyncMock()),
        services=SimpleNamespace(get_rsi_handlers=lambda: pytest.fail("Disabled service was constructed")),
    )


@pytest.mark.asyncio
async def test_disabled_capabilities_do_not_construct_the_engine(monkeypatch):
    monkeypatch.setattr(rsi, "get_config", lambda: {})
    ctx = context(ReqMethod.RSI_CAPABILITIES)
    await rsi.handle_rsi(ctx)
    wire = ctx.sink.send_wire.call_args.args[0]
    value = json.loads(wire) if isinstance(wire, (str, bytes)) else wire
    response = e2a_response_to_agent_response(E2AResponse.from_dict(value))
    assert response.payload["enabled"] is False
    assert response.payload["scenarios"] == ["HARNESS"]


@pytest.mark.asyncio
async def test_disabled_execution_is_rejected_before_materialization(monkeypatch):
    monkeypatch.setattr(rsi, "get_config", lambda: {"rsi": {"enabled": False}})
    ctx = context(ReqMethod.RSI_TRAINING_START)
    await rsi.handle_rsi(ctx)
    wire = ctx.sink.send_wire.call_args.args[0]
    value = json.loads(wire) if isinstance(wire, (str, bytes)) else wire
    response = e2a_response_to_agent_response(E2AResponse.from_dict(value))
    assert response.ok is False
    assert response.payload["code"] == "RSI_DISABLED"


@pytest.mark.asyncio
async def test_enabled_handler_awaits_the_service_result(monkeypatch):
    monkeypatch.setattr(rsi, "get_config", lambda: {"rsi": {"enabled": True}})
    ctx = context(ReqMethod.RSI_TASK_LIST)
    handler = SimpleNamespace(handle_async=AsyncMock(return_value={"ok": True, "payload": {"tasks": []}}))
    ctx.services.get_rsi_handlers = lambda: handler
    await rsi.handle_rsi(ctx)
    handler.handle_async.assert_awaited_once_with(ctx.request)


def test_artifact_execution_is_rejected_by_the_task_service(tmp_path):
    from jiuwenswarm.agents.harness.common.rsi import build_rsi_service_context
    from jiuwenswarm.agents.harness.common.rsi.errors import RsiScenarioNotSupported
    service = build_rsi_service_context(tmp_path).task_service
    with pytest.raises(RsiScenarioNotSupported, match="Only Harness"):
        service.create({"scenario": "ARTIFACT", "artifact_type": "PAPER"})


def test_rsi_methods_route_from_web_gateway_to_stable_dispatch():
    from jiuwenswarm.gateway.channel_manager.web.app_web_handlers import (
        _FORWARD_NO_LOCAL_HANDLER_METHODS,
        _FORWARD_REQ_METHODS,
    )
    from jiuwenswarm.server.dispatch import HANDLERS

    for method in ReqMethod:
        if method.value.startswith("rsi."):
            assert method.value in _FORWARD_REQ_METHODS
            assert method.value in _FORWARD_NO_LOCAL_HANDLER_METHODS
            assert HANDLERS[method].fn is rsi.handle_rsi


@pytest.mark.asyncio
async def test_live_adapter_switches_and_restores_stable_harness_resources(tmp_path):
    from unittest.mock import MagicMock
    from openjiuwen.harness import create_deep_agent
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter

    def package(name, content):
        path = tmp_path / name
        path.mkdir()
        (path / "manifest.json").write_text(json.dumps({
            "id": name, "prompt_sections": [{"name": "rsi.stable.policy", "content": content}],
        }), encoding="utf-8")
        return str(path)

    first = package("first", "First tested policy")
    second = package("second", "Second tested policy")
    invalid = package("invalid", ["invalid prompt content"])
    adapter = JiuWenSwarmDeepAdapter(workspace_dir=str(tmp_path / "workspace"))
    agent = create_deep_agent(model=MagicMock(), workspace=str(tmp_path / "workspace"),
                              agent_ras=False, auto_create_workspace=False)
    adapter._instance = agent
    try:
        await adapter.apply_rsi_harness_install("activate", config_path=first, installation_id="first")
        assert "First tested policy" in agent.system_prompt_builder.build()
        with pytest.raises(ValueError):
            await adapter.apply_rsi_harness_install("activate", config_path=invalid, installation_id="invalid")
        assert adapter._rsi_harness_install_id == "first"
        assert "First tested policy" in agent.system_prompt_builder.build()
        await adapter.apply_rsi_harness_install("activate", config_path=second, installation_id="second")
        assert "Second tested policy" in agent.system_prompt_builder.build()
        assert "First tested policy" not in agent.system_prompt_builder.build()
        await adapter.apply_rsi_harness_install("deactivate", config_path="", installation_id="")
        assert "Second tested policy" not in agent.system_prompt_builder.build()
    finally:
        agent.ability_manager.teardown_tools()


@pytest.mark.asyncio
async def test_session_harness_selection_survives_reload_and_tracks_new_default(tmp_path, monkeypatch):
    from unittest.mock import AsyncMock
    from jiuwenswarm.agents.harness.common.rsi import context
    from jiuwenswarm.agents.harness.common.rsi.harness_activation import RsiHarnessActivationStore
    from jiuwenswarm.server.runtime.agent_adapter import interface_deep
    from jiuwenswarm.server.runtime.session import session_metadata

    tasks_root = tmp_path / "rsi" / "tasks"
    store = RsiHarnessActivationStore(tasks_root)
    versions = {}
    for name in ("default", "selected", "new-default"):
        runtime = tasks_root / "versions" / name
        runtime.mkdir(parents=True)
        versions[name] = {"installation_id": name, "runtime_path": str(runtime), "sha256": "a" * 64}
    for name in ("selected", "default"):
        store.commit(versions[name])
    monkeypatch.setattr(context, "get_rsi_workspace_root", lambda: tasks_root.parent)
    monkeypatch.setattr(interface_deep, "get_config", lambda: {"rsi": {"enabled": True}})
    monkeypatch.setattr(interface_deep, "resolve_tenant_sessions_dir", lambda *args, **kwargs: tmp_path / "sessions")

    def adapter():
        result = interface_deep.JiuWenSwarmDeepAdapter(workspace_dir=str(tmp_path / "workspace"))
        result.mark_as_session_scoped("session-a")
        result._replace_rsi_harness = AsyncMock()
        return result

    field = "rsi_harness_installation_id"
    request = SimpleNamespace(req_method=ReqMethod.CHAT_SEND, session_id="session-a",
                              channel_id="web", metadata={}, params={field: "selected"})
    first = adapter()
    await first._sync_rsi_session_harness(request)
    first._replace_rsi_harness.assert_awaited_once_with(
        "activate", config_path=versions["selected"]["runtime_path"], installation_id="selected",
    )
    persisted = session_metadata.get_session_metadata("session-a", cache_bust=True, sessions_root=tmp_path / "sessions")
    assert persisted[field] == "selected"
    request.params = {field: "missing"}
    with pytest.raises(ValueError, match="不可用"):
        await first._sync_rsi_session_harness(request)
    assert first._rsi_session_harness_id == "selected"

    reloaded = adapter()
    request.params = {}
    await reloaded._sync_rsi_session_harness(request)
    assert reloaded._rsi_session_harness_id == "selected"
    store.commit(versions["new-default"])
    result = await reloaded.apply_rsi_harness_install(
        "activate", config_path=versions["new-default"]["runtime_path"], installation_id="new-default",
    )
    assert result["status"] == "SKIPPED"
    request.params = {field: ""}
    await reloaded._sync_rsi_session_harness(request)
    reloaded._replace_rsi_harness.assert_awaited_with(
        "activate", config_path=versions["new-default"]["runtime_path"], installation_id="new-default",
    )
    persisted = session_metadata.get_session_metadata("session-a", cache_bust=True, sessions_root=tmp_path / "sessions")
    assert persisted[field] == ""

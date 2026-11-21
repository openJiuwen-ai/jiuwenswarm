"""Verify the production transport handler's feature gate and service seam."""

import json
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.agents.harness.common.rsi.errors import RsiBadRequest
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
    assert response.payload["scenarios"] == ["HARNESS", "ARTIFACT"]
    assert response.payload["artifact_types"] == ["PAPER", "PROGRAM"]


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


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_stage", ["config", "assembly", "execution"])
async def test_rsi_unexpected_errors_keep_a_wire_error_code(monkeypatch, failure_stage):
    def fail():
        raise ValueError("controlled failure")

    monkeypatch.setattr(rsi, "get_config", fail if failure_stage == "config" else lambda: {"rsi": {"enabled": True}})
    ctx = context(ReqMethod.RSI_TASK_LIST)
    handler = SimpleNamespace(handle_async=AsyncMock(side_effect=ValueError("controlled failure")))
    ctx.services.get_rsi_handlers = fail if failure_stage == "assembly" else lambda: handler
    await rsi.handle_rsi(ctx)
    wire = ctx.sink.send_wire.call_args.args[0]
    response = e2a_response_to_agent_response(E2AResponse.from_dict(wire))
    assert response.ok is False
    assert response.payload == {"code": "INTERNAL_ERROR", "error": "controlled failure"}


@pytest.mark.asyncio
async def test_rsi_domain_errors_keep_their_code_and_cancellation_propagates(monkeypatch):
    monkeypatch.setattr(rsi, "get_config", lambda: {"rsi": {"enabled": True}})
    ctx = context(ReqMethod.RSI_TASK_CREATE)
    handler = SimpleNamespace(handle_async=AsyncMock(side_effect=RsiBadRequest("invalid selection")))
    ctx.services.get_rsi_handlers = lambda: handler
    await rsi.handle_rsi(ctx)
    wire = ctx.sink.send_wire.call_args.args[0]
    response = e2a_response_to_agent_response(E2AResponse.from_dict(wire))
    assert response.payload == {"code": "BAD_REQUEST", "error": "invalid selection"}
    ctx.sink.send_wire.reset_mock()
    handler.handle_async.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await rsi.handle_rsi(ctx)
    ctx.sink.send_wire.assert_not_called()


@pytest.mark.parametrize("active,baseline", [(None, None), (None, "baseline"), ("active", None)])
def test_harness_source_preserves_missing_baseline_and_prefers_active(tmp_path, monkeypatch, active, baseline):
    from jiuwenswarm.agents.harness.common.rsi import context as context_module, harness_activation
    from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer

    monkeypatch.setattr(context_module, "get_rsi_workspace_root", lambda: tmp_path)
    monkeypatch.setattr(harness_activation.RsiHarnessActivationStore, "resolve_active_runtime_path", lambda self: active)
    baseline_path = tmp_path / baseline if baseline else None
    monkeypatch.setattr(harness_activation, "resolve_native_harness_baseline", lambda: baseline_path)
    expected = active or (str(baseline_path) if baseline_path is not None else None)
    assert AgentWebSocketServer._rsi_harness_refs_provider() == expected


def test_rsi_registration_rejects_duplicates_without_overwriting_handlers():
    from jiuwenswarm.server import dispatch

    original = dict(dispatch.HANDLERS)
    with pytest.raises(RuntimeError, match="duplicate handler registration"):
        dispatch._register_rsi_methods()
    assert dispatch.HANDLERS == original


@pytest.mark.asyncio
@pytest.mark.parametrize("config", [None, {}, {"rsi": None}, {"rsi": {"enabled": False}}])
async def test_disabled_session_harness_does_not_access_request_or_adapter_state(monkeypatch, config):
    from jiuwenswarm.server.runtime.agent_adapter import interface_deep

    monkeypatch.setattr(interface_deep, "get_config", lambda: config)
    adapter = object.__new__(interface_deep.JiuWenSwarmDeepAdapter)
    await adapter._sync_rsi_session_harness(object())


@pytest.mark.parametrize("artifact_type", ["PAPER", "PROGRAM"])
def test_artifact_creation_keeps_the_selected_branch(tmp_path, artifact_type):
    from jiuwenswarm.agents.harness.common.rsi import build_rsi_service_context
    ctx = build_rsi_service_context(tmp_path / "tasks")
    ctx.install_mock_rsi_adapters()
    artifact = tmp_path / "source"
    artifact.mkdir()
    created = ctx.task_service.create({
        "name": "artifact regression", "scenario": "ARTIFACT", "artifact_type": artifact_type,
        "artifact_path": str(artifact), "model_refs": {"optimizer": "selected-model"},
        "max_iterations": 1,
    })
    task = ctx.store.get(created["task_id"])
    assert task.scenario == "ARTIFACT"
    assert task.artifact_type == artifact_type
    assert task.model_refs["optimizer"] == "selected-model"
    assert not task.model_refs.get("tester")
    assert ctx.adapter_for_task(task.task_id).artifact_type == artifact_type


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

"""Trusted Sandbox routing and timing at the actual prepared SDK sender."""
import asyncio
import json
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.server.worker import execution_requests as server_module
from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter
from jiuwenswarm.runtime.execution_telemetry import sdk_entry_sink


@pytest.mark.parametrize("platform,mode,kind,enabled,wait", [
    ("win32", "internal", "jiuwenbox", True, False),
    ("linux", None, "jiuwenbox", False, False),
    ("linux", "external", "jiuwenbox", True, False),
    ("linux", "internal", "adx-environment", True, False),
    ("linux", "internal", "jiuwenbox", False, True),
    ("linux", "internal", "jiuwenbox", True, True),
])
def test_chat_waits_only_for_trusted_internal_bootstrap(monkeypatch, platform, mode, kind, enabled, wait):
    monkeypatch.setattr(server_module.sys, "platform", platform)
    monkeypatch.setattr(server_module, "get_sandbox_startup_mode_explicit", lambda: mode)
    monkeypatch.setattr(server_module, "get_sandbox_endpoint", lambda: {"type": kind})
    # Caller parameters never bypass a trusted configured Sandbox.
    request = SimpleNamespace(req_method=ReqMethod.CHAT_SEND, params={"sandbox": enabled})
    assert AgentWebSocketServer._request_requires_sandbox_bootstrap(request) is wait


def test_unknown_sandbox_configuration_preserves_barrier(monkeypatch):
    monkeypatch.setattr(server_module.sys, "platform", "linux")

    def failed():
        raise OSError("unreadable config")

    monkeypatch.setattr(server_module, "get_sandbox_startup_mode_explicit", failed)
    assert AgentWebSocketServer._request_requires_sandbox_bootstrap(SimpleNamespace(req_method=ReqMethod.CHAT_SEND))
    assert not AgentWebSocketServer._request_requires_sandbox_bootstrap(SimpleNamespace(req_method=ReqMethod.SESSION_LIST))


@pytest.mark.asyncio
async def test_explicit_sandbox_mutation_waits_for_bootstrap(monkeypatch):
    server = AgentWebSocketServer.__new__(AgentWebSocketServer)
    entered, release = asyncio.Event(), asyncio.Event()

    async def bootstrap():
        entered.set()
        await release.wait()

    server._sandbox_start_task = asyncio.create_task(bootstrap())
    server._handle_command_sandbox = AsyncMock()
    request = AgentRequest(request_id="sandbox-disable", req_method=ReqMethod.COMMAND_SANDBOX,
                           channel_id="web", session_id="", params={"action": "disable"})
    dispatch = asyncio.create_task(server.dispatch_parsed_request(None, request, asyncio.Lock()))
    await entered.wait()
    await asyncio.sleep(0)
    server._handle_command_sandbox.assert_not_awaited()
    release.set()
    await dispatch
    server._handle_command_sandbox.assert_awaited_once()


@pytest.mark.asyncio
async def test_sdk_timestamp_uses_permission_prepared_request(monkeypatch, tmp_path):
    destination = tmp_path / "entries.jsonl"
    monkeypatch.setenv("JIUWENSWARM_SDK_ENTRY_LOG", str(destination))
    adapter = JiuWenSwarmDeepAdapter.__new__(JiuWenSwarmDeepAdapter)
    adapter._enable_auto_permission = True
    actual = SimpleNamespace(request_id="prepared", inputs={"secret": "never logged"})
    seen = []

    async def sender(request):
        # Timestamp must exist at actual SDK entry, after preparation.
        seen.append(json.loads(destination.read_text(encoding="utf-8")))
        assert request is actual

    class PermissionDispatch:
        async def send(self, request, send):
            assert not destination.exists()
            await send(actual)
            return True

    adapter._permission_dispatch = PermissionDispatch()
    adapter._instance = SimpleNamespace(send_input=sender)
    assert await adapter._send_input_with_permission_resume_guard(SimpleNamespace(request_id="before"))
    assert seen[0]["request_id"] == "prepared"
    assert "secret" not in destination.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_disabled_diagnostic_keeps_original_sender(monkeypatch):
    monkeypatch.delenv("JIUWENSWARM_SDK_ENTRY_LOG", raising=False)
    adapter = JiuWenSwarmDeepAdapter.__new__(JiuWenSwarmDeepAdapter)
    adapter._enable_auto_permission = True
    sender = AsyncMock()
    adapter._instance = SimpleNamespace(send_input=sender)
    adapter._permission_dispatch = SimpleNamespace(send=AsyncMock(return_value=False))
    request = SimpleNamespace(request_id="ordinary")
    assert not await adapter._send_input_with_permission_resume_guard(request)
    adapter._permission_dispatch.send.assert_awaited_once_with(request, sender)


@pytest.mark.asyncio
async def test_minimum_ready_observed_after_permission_handoff_without_diagnostic(monkeypatch):
    monkeypatch.delenv("JIUWENSWARM_SDK_ENTRY_LOG", raising=False)
    adapter = JiuWenSwarmDeepAdapter.__new__(JiuWenSwarmDeepAdapter)
    adapter._enable_auto_permission = True
    events = []

    async def sender(request):
        events.append("sdk")

    class PermissionDispatch:
        async def send(self, request, send):
            assert events == []
            events.append("prepared")
            await send(request)
            return True

    adapter._instance = SimpleNamespace(send_input=sender)
    adapter._permission_dispatch = PermissionDispatch()
    token = sdk_entry_sink.set(lambda: events.append("minimum"))
    try:
        await adapter._send_input_with_permission_resume_guard(SimpleNamespace(request_id="actual"))
    finally:
        sdk_entry_sink.reset(token)
    assert events == ["prepared", "minimum", "sdk"]


@pytest.mark.asyncio
async def test_failed_permission_handoff_does_not_report_execution_ready(monkeypatch):
    monkeypatch.delenv("JIUWENSWARM_SDK_ENTRY_LOG", raising=False)
    adapter = JiuWenSwarmDeepAdapter.__new__(JiuWenSwarmDeepAdapter)
    adapter._enable_auto_permission = True
    actual_sender = AsyncMock()
    adapter._instance = SimpleNamespace(send_input=actual_sender)
    adapter._permission_dispatch = SimpleNamespace(send=AsyncMock(side_effect=RuntimeError("handoff failed")))
    observed = []
    token = sdk_entry_sink.set(lambda: observed.append("minimum"))
    try:
        with pytest.raises(RuntimeError, match="handoff failed"):
            await adapter._send_input_with_permission_resume_guard(SimpleNamespace(request_id="failed"))
    finally:
        sdk_entry_sink.reset(token)
    assert observed == []
    actual_sender.assert_not_awaited()


@pytest.mark.asyncio
async def test_custom_sender_is_not_reported_as_sdk_and_timing_failure_is_nonfatal(monkeypatch, tmp_path):
    destination = tmp_path / "entries.jsonl"
    monkeypatch.setenv("JIUWENSWARM_SDK_ENTRY_LOG", str(destination))
    adapter = JiuWenSwarmDeepAdapter.__new__(JiuWenSwarmDeepAdapter)
    adapter._enable_auto_permission = False
    sender = AsyncMock()
    adapter._instance = SimpleNamespace(send_input=sender)
    request = SimpleNamespace(request_id="actual")
    await adapter._send_input_with_permission_resume_guard(request, send=sender)
    assert not destination.exists()
    monkeypatch.setenv("JIUWENSWARM_SDK_ENTRY_LOG", str(tmp_path))
    await adapter._send_input_with_permission_resume_guard(request)
    assert sender.await_count == 2


@pytest.mark.parametrize('action', ['status', 'enable'])
@pytest.mark.asyncio
async def test_managed_sandbox_command_is_read_only(monkeypatch, action):
    from jiuwenswarm.server.management import sandbox as server

    monkeypatch.setattr(server, '_require_sandbox_supported', lambda: None)
    monkeypatch.setattr(server, 'get_sandbox_endpoint', lambda: {'type': 'adx-environment'})
    view = {'type': 'adx-environment', 'runtime': 'conch', 'enabled': True, 'mounts': []}
    status = Mock(return_value=view)
    monkeypatch.setattr(server, 'build_adx_sandbox_status_view', status)
    services = SimpleNamespace(encode_response=lambda response, **_: response, send=AsyncMock())
    request = SimpleNamespace(params={'sub': action}, request_id='sandbox-request', channel_id='web')
    handler = server.SandboxManagement(SimpleNamespace(services=services))
    await handler._handle_command_sandbox(None, request, asyncio.Lock())
    services.send.assert_awaited_once()
    response = services.send.await_args.args[1]
    if action == 'status':
        assert response.ok and response.payload == view
        status.assert_called_once_with()
    else:
        assert not response.ok
        assert response.payload['code'] == 'SANDBOX_BAD_REQUEST'
        assert 'subcommands are disabled' in response.payload['error']
        status.assert_not_called()


@pytest.mark.parametrize('files_changed,isolation_key', [(False, 'scope'), (True, 'scope'), (True, None)])
@pytest.mark.asyncio
async def test_runtime_patch_uses_native_provider_reclamation(monkeypatch, files_changed, isolation_key):
    from jiuwenswarm.server.runtime.agent_adapter import interface_deep as adapter
    from jiuwenswarm.server.runtime.agent_adapter import sysop_builder as builder
    delete = AsyncMock(return_value=['tools-1'])
    shared_key = Mock(return_value='native-scope')
    monkeypatch.setitem(sys.modules, 'openjiuwen.extensions.sys_operation.sandbox.providers.adx',
                        SimpleNamespace(build_adx_shared_scope_key=shared_key, delete_adx_sandbox=delete))
    policy = Mock()
    monkeypatch.setattr(adapter, 'build_filesystem_policy', policy)
    launcher = SimpleNamespace(sandbox_type='adx-environment', base_url='https://worker.example:8443', extra_params={})
    card = SimpleNamespace(mode=builder.OperationMode.SANDBOX,
                           gateway_config=SimpleNamespace(launcher_config=launcher))
    instance = SimpleNamespace(_sys_operation_card=card, _sys_operation_isolation_key=lambda _: isolation_key)
    await adapter.JiuWenSwarmDeepAdapter.apply_sandbox_runtime_patch(instance, {'files': {}}, files_changed=files_changed)
    policy.assert_not_called()
    assert launcher.extra_params == {}
    if files_changed:
        delete.assert_awaited_once_with(shared_key='native-scope' if isolation_key else None,
                                       base_url=None if isolation_key else 'https://worker.example:8443',
                                       reason='files_changed')
        if isolation_key:
            shared_key.assert_called_once_with('https://worker.example:8443', isolation_key)
    else:
        delete.assert_not_awaited()

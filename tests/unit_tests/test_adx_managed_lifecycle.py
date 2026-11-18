"""Managed ADX tools retain the former external-provider lifecycle contract."""
import asyncio
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest


@pytest.mark.parametrize('mode', [None, 'external', 'internal'])
@pytest.mark.asyncio
async def test_managed_tools_never_bootstrap_local_jiuwenbox(monkeypatch, mode):
    from jiuwenswarm.server import agent_ws_server as server
    monkeypatch.setattr(server.sys, 'platform', 'linux')
    monkeypatch.setattr(server, 'get_sandbox_startup_mode_explicit', lambda: mode)
    monkeypatch.setattr(server, 'get_sandbox_endpoint', lambda: {
        'type': 'adx-environment', 'url': 'https://worker.example:8443'})
    resolve = Mock()
    monkeypatch.setattr(server, 'resolve_sandbox_policy_path', resolve)
    await server.AgentWebSocketServer._bootstrap_internal_jiuwenbox(SimpleNamespace())
    resolve.assert_not_called()


@pytest.mark.parametrize('action', ['status', 'enable', 'disable', 'exclude.add', 'files.allow'])
@pytest.mark.asyncio
async def test_managed_sandbox_command_is_read_only(monkeypatch, action):
    from jiuwenswarm.server import agent_ws_server as server
    monkeypatch.setattr(server, '_require_sandbox_supported', lambda: None)
    monkeypatch.setattr(server, 'get_sandbox_endpoint', lambda: {'type': 'adx-environment'})
    view = {'type': 'adx-environment', 'runtime': 'conch', 'enabled': True, 'mounts': []}
    status = Mock(return_value=view)
    monkeypatch.setattr(server, 'build_adx_sandbox_status_view', status)
    monkeypatch.setattr(server, 'encode_agent_response_for_wire', lambda response, **_: response)
    sent = []
    async def send(ws, response):
        sent.append(response)
    monkeypatch.setattr(server, 'send_wire_payload', send)
    request = SimpleNamespace(params={'sub': action}, request_id='sandbox-request', channel_id='web')
    await server.AgentWebSocketServer._handle_command_sandbox(SimpleNamespace(), None, request, asyncio.Lock())
    assert len(sent) == 1
    if action == 'status':
        assert sent[0].ok and sent[0].payload == view
        status.assert_called_once_with()
    else:
        assert not sent[0].ok
        assert sent[0].payload['code'] == 'SANDBOX_BAD_REQUEST'
        assert 'subcommands are disabled' in sent[0].payload['error']
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

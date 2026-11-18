"""Bounded AgentServer restart configuration and native create requests."""
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.extensions.agentos.agentos_router.adx.config import load_router_config
from jiuwenswarm.extensions.agentos.agentos_router.adx.control_client import AdxControlClient, AdxControlError


@pytest.mark.asyncio
@pytest.mark.parametrize('policy', ['default', None, {
    'maxAttempts': 5, 'initialBackoffSeconds': 2, 'maxBackoffSeconds': 10,
}])
async def test_agentserver_restart_defaults_and_overrides_reach_create(monkeypatch, tmp_path, policy):
    monkeypatch.setenv('RESTART_TEST_KEY', 'unit-key')
    options = {'ingress_url': 'https://adx.example', 'tenant': 'test', 'token_env': 'RESTART_TEST_KEY'}
    if policy != 'default':
        options['restart_policy'] = policy
    config = load_router_config({'gateway': {'agent_client': {'type': 'agentos_router'}, 'adx': options,
        'agentos': {'workspace_root': str(tmp_path)}}})
    client = AdxControlClient(config)
    try:
        body = client._creation_body('native', 'alice+jiuwenswarm', '/workspace', {}, None, None)
        if policy is None:
            assert 'restartPolicy' not in body
        else:
            assert body['restartPolicy'] == (policy if policy != 'default' else {
                'maxAttempts': 3, 'initialBackoffSeconds': 1, 'maxBackoffSeconds': 30,
            })
        # Disabling remains an explicit option, independent of other dataclass defaults.
        assert replace(config, restart_policy=None).restart_policy is None
    finally:
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('terminal', [False, True])
async def test_agentserver_waits_for_adx_restart_and_reports_exhaustion(monkeypatch, tmp_path, terminal):
    monkeypatch.setenv('RESTART_TEST_KEY', 'unit-key')
    config = load_router_config({'gateway': {'agent_client': {'type': 'agentos_router'},
        'adx': {'ingress_url': 'https://adx.example', 'tenant': 'test', 'token_env': 'RESTART_TEST_KEY'},
        'agentos': {'workspace_root': str(tmp_path)}}})
    client = AdxControlClient(config)
    pending = {'id': 'test-agent', 'status': 'failed', 'restart_pending': True}
    final = {'id': 'test-agent', 'status': 'failed' if terminal else 'running', 'restart_pending': False}
    observe = AsyncMock(side_effect=[pending, final])
    monkeypatch.setattr(client, 'get_agent_info', observe)
    sleep = AsyncMock()
    monkeypatch.setattr('jiuwenswarm.extensions.agentos.agentos_router.adx.control_client.asyncio.sleep', sleep)
    try:
        if terminal:
            with pytest.raises(AdxControlError, match='not available'):
                await client.wait_until_running('test-agent')
        else:
            assert await client.wait_until_running('test-agent') == final
        assert observe.await_count == 2
        assert all(call.args == ('test-agent',) for call in observe.await_args_list)
        sleep.assert_awaited_once()
    finally:
        await client.close()

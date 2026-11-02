"""ADX tool environments use the AgentOS workspace and deployment configuration."""
from pathlib import Path

import pytest

from test_adx_router import _config, _router


def test_adx_router_preserves_explicit_tool_configuration(monkeypatch):
    router, _ = _router(_config(monkeypatch))
    router.set_or_update_server_config(config={
        'sandbox': {'enabled': True, 'type': 'adx-environment', 'url': 'https://adx.example'},
    })
    assert router._server_config['sandbox']['enabled'] is True
    assert router._server_config['sandbox']['type'] == 'adx-environment'


def test_adx_tool_card_uses_shared_agentos_workspace(monkeypatch, tmp_path):
    from jiuwenswarm.server.runtime.agent_adapter import sysop_builder as builder

    monkeypatch.setenv('JIUWENSWARM_RUNTIME_PROFILE', 'agentos')
    monkeypatch.setenv('JIUWENSWARM_USER_DIRECTORY', '/home/agentos/users/alice')
    monkeypatch.setenv('HOME', '/home/agentos')
    monkeypatch.setattr(builder, 'get_user_home', lambda: tmp_path / 'agentos', raising=False)
    monkeypatch.setattr(builder, '_resolve_workspace_dir', lambda: Path('/home/agentos/.jiuwenswarm/agent/workspace'))
    monkeypatch.setattr(builder, '_sandbox_isolation_custom_id', lambda _: 'project-test')
    monkeypatch.setattr(builder, 'get_sandbox_endpoint', lambda: {
        'type': 'adx-environment', 'url': 'https://adx.example', 'image': 'tools:v1',
        'runtime': 'conch', 'idle_timeout': 60, 'user': '1000', 'group': '1000',
    })
    card = builder.create_sandbox_sysop_card('https://adx.example', 'adx-environment')
    assert card is not None
    launch = card.gateway_config.launcher_config
    assert launch.sandbox_type == 'adx-environment'
    assert launch.extra_params['runtime'] == 'conch'
    assert launch.extra_params['idle_timeout'] == 60
    assert launch.extra_params['user'] == '1000'
    assert launch.extra_params['group'] == '1000'
    assert launch.extra_params['mounts'] == [
        {'host_path': '/home/agentos/users/alice/.jiuwenswarm/agent/workspace',
         'target': '/home/agentos/.jiuwenswarm/agent/workspace', 'options': ['rw']},
        {'host_path': '/home/agentos/users/alice/Documents/JiuwenSwarm',
         'target': str(tmp_path / 'agentos/Documents/JiuwenSwarm'), 'options': ['rw']},
    ]


@pytest.mark.parametrize('user_dir', ['', '/', 'relative/path', '/home/users/../other'])
def test_adx_tool_rejects_invalid_workspace_source(monkeypatch, user_dir):
    from jiuwenswarm.server.runtime.agent_adapter import sysop_builder as builder

    monkeypatch.setenv('JIUWENSWARM_RUNTIME_PROFILE', 'agentos')
    monkeypatch.setenv('JIUWENSWARM_USER_DIRECTORY', user_dir)
    monkeypatch.setattr(builder, '_resolve_workspace_dir', lambda: Path('/home/agentos/.jiuwenswarm/agent/workspace'))
    monkeypatch.setattr(builder, 'get_sandbox_endpoint', lambda: {'type': 'adx-environment'})
    with pytest.raises(ValueError, match='user directory'):
        builder._build_adx_extra_params()


def test_adx_tool_rejects_workspace_mount_override(monkeypatch):
    from jiuwenswarm.server.runtime.agent_adapter import sysop_builder as builder

    monkeypatch.setenv('JIUWENSWARM_RUNTIME_PROFILE', 'agentos')
    monkeypatch.setenv('JIUWENSWARM_USER_DIRECTORY', '/home/agentos/users/alice')
    monkeypatch.setattr(builder, '_resolve_workspace_dir', lambda: Path('/home/agentos/.jiuwenswarm/agent/workspace'))
    monkeypatch.setattr(builder, 'get_sandbox_endpoint', lambda: {
        'type': 'adx-environment',
        'mounts': [{'host_path': '/other', 'target': '/home/agentos/.jiuwenswarm/agent/workspace'}],
    })
    with pytest.raises(ValueError, match='cannot replace'):
        builder._build_adx_extra_params()


def test_adx_tool_endpoint_preserves_runtime_settings(monkeypatch):
    from jiuwenswarm.common import config

    monkeypatch.setattr(config, 'get_config', lambda: {'sandbox': {
        'type': 'adx-environment', 'url': 'https://adx.example', 'image': 'tools:v1',
        'runtime': 'conch', 'idle_timeout': 60, 'user': 1000, 'group': 1000,
    }})
    endpoint = config.get_sandbox_endpoint()
    assert endpoint['image'] == 'tools:v1'
    assert endpoint['runtime'] == 'conch'
    assert endpoint['idle_timeout'] == 60
    assert endpoint['user'] == endpoint['group'] == 1000

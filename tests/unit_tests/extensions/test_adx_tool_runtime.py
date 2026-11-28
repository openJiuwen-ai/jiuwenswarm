"""ADX tool environments use the AgentOS workspace and deployment configuration."""
from pathlib import Path

import pytest


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


def test_adx_tool_endpoint_preserves_idle_lifecycle(monkeypatch, tmp_path):
    """Idle pause, its checkpoint policy and failover must reach create params."""
    from jiuwenswarm.common import config
    from jiuwenswarm.server.runtime.agent_adapter import sysop_builder as builder

    lifecycle = {
        'idle_action': 'pause',
        'pause_policy': {'retention': {'kind': 'ttl', 'ttlSeconds': 86400},
                         'timeoutSeconds': 300, 'storage': 'configured'},
        'failover': True,
    }
    source = {'type': 'adx-environment', 'url': 'https://adx.example', 'runtime': 'runsc',
              'idle_timeout': 900, **lifecycle}
    monkeypatch.setattr(config, 'get_config', lambda: {'sandbox': source})
    endpoint = config.get_sandbox_endpoint()
    assert endpoint['idle_timeout'] == 900
    for key, value in lifecycle.items():
        assert endpoint[key] == value
    monkeypatch.setattr(builder, 'get_sandbox_endpoint', lambda: endpoint)
    monkeypatch.setattr(builder, '_resolve_workspace_dir', lambda: tmp_path / 'workspace')
    monkeypatch.delenv('JIUWENSWARM_RUNTIME_PROFILE', raising=False)
    extra = builder._build_adx_extra_params()
    for key, value in lifecycle.items():
        assert extra[key] == value

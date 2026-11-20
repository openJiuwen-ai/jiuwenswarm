"""ADX replaces the previous provider without a mounted-tool feature prerequisite."""
from copy import deepcopy

import pytest

from jiuwenswarm.common import config
from jiuwenswarm.server.runtime.agent_adapter import sysop_builder as builder


def test_retired_tool_provider_requires_deployment_migration(monkeypatch):
    monkeypatch.setattr(config, 'get_config', lambda: {'sandbox': {'type': 'yuanrong'}})
    with pytest.raises(ValueError, match='deployment migration'):
        config.get_sandbox_endpoint()


@pytest.fixture
def tool_workspace(monkeypatch, tmp_path):
    home = tmp_path / 'agentserver-home'
    project = home / 'workspace'
    project.mkdir(parents=True)
    workspace = home / '.jiuwenswarm/agent/workspace'
    workspace.mkdir(parents=True)
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.setenv('JIUWENSWARM_RUNTIME_PROFILE', 'agentos')
    monkeypatch.setenv('JIUWENSWARM_USER_DIRECTORY', '/home/agentos/users/alice')
    monkeypatch.setattr(builder, '_resolve_workspace_dir', lambda: workspace)
    monkeypatch.setenv('JIUWENSWARM_TASKS_DIR', str(home / 'Documents/JiuwenSwarm'))
    for key in ('TOOL_SANDBOX_CPU', 'TOOL_SANDBOX_MEMORY', 'TOOL_SANDBOX_IDLE_TIMEOUT'):
        monkeypatch.delenv(key, raising=False)
    return home, workspace, project


@pytest.mark.parametrize('runtime', ['runsc', 'conch'])
def test_adx_tool_card_reads_configuration_and_maps_existing_user_workspace(monkeypatch, tool_workspace, runtime):
    home, workspace, project = tool_workspace
    source = {'type': 'adx-environment', 'url': 'https://worker.example:8443',
              'runtime': runtime, 'image': 'existing-tools:v1', 'cpu': 750, 'memory': 1024,
              'idle_timeout': 60, 'user': '1001', 'group': '1002',
              'restart_policy': {'maxAttempts': 5, 'initialBackoffSeconds': 2, 'maxBackoffSeconds': 10},
              'connection': {'token': 'test-token'}, 'env': {'CUSTOM_TOOL_ENV': 'retained'},
              'network': {'block_network': True}, 'endpoint_file': '/run/agentos/node.json',
              'mounts': [{'host_path': '/shared/data', 'target': '/data', 'options': ['ro']}]}
    if runtime == 'runsc':
        source['rootfs'] = {'type': 'local', 'path': '/objects/offline/rootfs'}
    original = deepcopy(source)
    monkeypatch.setattr(config, 'get_config', lambda: {'sandbox': source})
    card = builder.create_sandbox_sysop_card(source['url'], source['type'])
    assert card is not None
    assert card.mode.value == 'sandbox'
    launch = card.gateway_config.launcher_config
    assert launch.sandbox_type == 'adx-environment'
    assert str(launch.base_url).rstrip('/') == source['url']
    assert card.gateway_config.isolation.custom_id == 'default'
    extra = launch.extra_params
    for name in ('runtime', 'image', 'cpu', 'memory', 'idle_timeout', 'user', 'group',
                 'connection', 'env', 'network', 'endpoint_file', 'restart_policy'):
        assert extra[name] == source[name]
    if runtime == 'runsc':
        assert extra['rootfs'] == source['rootfs']
    assert extra['workdir'] == str(workspace)
    assert extra['mounts'] == [
        {'host_path': '/home/agentos/users/alice/.jiuwenswarm/agent/workspace',
         'target': str(workspace), 'options': ['rw']},
        {'host_path': '/home/agentos/users/alice/workspace', 'target': str(project), 'options': ['rw']},
        {'host_path': '/home/agentos/users/alice/Documents/JiuwenSwarm',
         'target': str(home / 'Documents/JiuwenSwarm'), 'options': ['rw']},
        {'host_path': '/shared/data', 'target': '/data', 'options': ['ro']},
    ]
    assert source == original


def test_tool_card_preserves_explicit_restart_disable(monkeypatch, tool_workspace):
    source = {'type': 'adx-environment', 'url': 'https://worker.example:8443', 'restart_policy': None}
    monkeypatch.setattr(config, 'get_config', lambda: {'sandbox': source})
    card = builder.create_sandbox_sysop_card(source['url'], source['type'])
    assert 'restart_policy' in card.gateway_config.launcher_config.extra_params
    assert card.gateway_config.launcher_config.extra_params['restart_policy'] is None


def test_adx_tool_keeps_explicit_workdir_and_deduplicates_workspace_target(monkeypatch, tool_workspace):
    _, workspace, _ = tool_workspace
    source = {'type': 'adx-environment', 'workdir': '/data', 'mounts': [
        {'host_path': '/home/agentos/users/alice/.jiuwenswarm/agent/workspace',
         'target': str(workspace), 'options': ['rw']}]}
    monkeypatch.setattr(config, 'get_config', lambda: {'sandbox': source})
    extra = builder._build_adx_extra_params()
    assert extra['workdir'] == '/data'
    assert len([entry for entry in extra['mounts'] if entry['target'] == str(workspace)]) == 1


def test_adx_tool_does_not_add_a_missing_project_directory(monkeypatch, tool_workspace):
    _, _, project = tool_workspace
    project.rmdir()
    monkeypatch.setattr(config, 'get_config', lambda: {'sandbox': {'type': 'adx-environment'}})
    mounts = builder._build_adx_extra_params()['mounts']
    assert str(project) not in [entry['target'] for entry in mounts]
    assert len(mounts) == 2


def test_adx_tool_mount_covers_projectless_sessions_and_outputs(monkeypatch, tool_workspace):
    home, _, _ = tool_workspace
    tasks = home / 'custom-tasks'
    monkeypatch.setenv('JIUWENSWARM_TASKS_DIR', str(tasks))
    monkeypatch.setattr(config, 'get_config', lambda: {'sandbox': {'type': 'adx-environment'}})
    extra = builder._build_adx_extra_params()
    mount = next(entry for entry in extra['mounts'] if entry['target'] == str(tasks))
    assert mount['host_path'] == '/home/agentos/users/alice/custom-tasks'
    assert mount['options'] == ['rw']
    assert tasks.is_dir()
    for relative in ('2026-11-16/chat-1/work', '2026-11-17/chat-2/outputs'):
        assert (tasks / relative).is_relative_to(mount['target'])


def test_adx_tool_tasks_override_reuses_existing_workspace_mount(monkeypatch, tool_workspace):
    _, workspace, _ = tool_workspace
    monkeypatch.setenv('JIUWENSWARM_TASKS_DIR', str(workspace))
    monkeypatch.setattr(config, 'get_config', lambda: {'sandbox': {'type': 'adx-environment'}})
    extra = builder._build_adx_extra_params()
    assert len([entry for entry in extra['mounts'] if entry['target'] == str(workspace)]) == 1


def test_adx_tool_management_overrides_preserve_higher_retained_limits(monkeypatch, tool_workspace):
    monkeypatch.setattr(config, 'get_config', lambda: {'sandbox': {
        'type': 'adx-environment', 'cpu': 500, 'cpu_limit': 1000, 'memory': 512,
        'mem_limit': 8192, 'idle_timeout': 600}})
    monkeypatch.setenv('TOOL_SANDBOX_CPU', '2000')
    monkeypatch.setenv('TOOL_SANDBOX_MEMORY', '2048')
    monkeypatch.setenv('TOOL_SANDBOX_IDLE_TIMEOUT', '60')
    extra = builder._build_adx_extra_params()
    assert (extra['cpu'], extra['cpu_limit']) == (2000, 2000)
    assert (extra['memory'], extra['mem_limit']) == (2048, 8192)
    assert extra['idle_timeout'] == 60


def test_standalone_adx_tool_retains_application_identity_directory(monkeypatch, tmp_path):
    root = tmp_path / 'application'
    root.mkdir()
    monkeypatch.setenv('JIUWENSWARM_RUNTIME_PROFILE', 'standalone')
    monkeypatch.setattr(builder, 'get_agent_root_dir', lambda: root)
    monkeypatch.setattr(config, 'get_config', lambda: {'sandbox': {'type': 'adx-environment'}})
    extra = builder._build_adx_extra_params()
    assert extra['workdir'] == str(root)
    assert extra['mounts'] == [{'host_path': str(root), 'target': str(root), 'options': ['rw']}]


def test_adx_lifecycle_options_reach_environment_create_params(monkeypatch, tool_workspace):
    """Idle pause, its checkpoint policy and failover must survive config filtering."""
    lifecycle = {
        'idle_action': 'pause',
        'pause_policy': {'retention': {'kind': 'ttl', 'ttlSeconds': 86400},
                         'timeoutSeconds': 300, 'storage': 'configured'},
        'failover': True,
    }
    source = {'type': 'adx-environment', 'runtime': 'runsc', 'idle_timeout': 900, **deepcopy(lifecycle)}
    monkeypatch.setattr(config, 'get_config', lambda: {'sandbox': source})
    endpoint = config.get_sandbox_endpoint()
    extra = builder._build_adx_extra_params()
    assert endpoint['idle_timeout'] == 900
    for key, value in lifecycle.items():
        assert endpoint[key] == value
        assert extra[key] == value

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


@pytest.mark.parametrize('runtime', ['runsc', 'conch'])
def test_adx_tool_card_uses_shared_agentos_workspace(monkeypatch, tmp_path, runtime):
    from jiuwenswarm.server.runtime.agent_adapter import sysop_builder as builder
    from openjiuwen.agent_teams import paths

    monkeypatch.setenv('JIUWENSWARM_RUNTIME_PROFILE', 'agentos')
    monkeypatch.setenv('JIUWENSWARM_USER_DIRECTORY', '/home/agentos/users/alice')
    monkeypatch.setenv('HOME', str(tmp_path / 'agentos'))
    monkeypatch.setattr(builder, 'get_user_home', lambda: tmp_path / 'agentos', raising=False)
    monkeypatch.setattr(builder, 'get_agent_root_dir', lambda: tmp_path / 'agentos/.jiuwenswarm/agent')
    monkeypatch.setattr(paths, 'get_agent_teams_home', lambda: tmp_path / 'agentos/.jiuwenswarm/.agent_teams')
    monkeypatch.setattr(builder, '_resolve_workspace_dir', lambda: tmp_path / 'agentos/.jiuwenswarm/agent/workspace')
    monkeypatch.setattr(builder, '_sandbox_isolation_custom_id', lambda _: 'project-test')
    monkeypatch.setattr(builder, 'get_sandbox_endpoint', lambda: {
        'type': 'adx-environment', 'url': 'https://adx.example', 'image': 'tools:v1',
        'rootfs_template': 'local-digest', 'runtime': runtime, 'idle_timeout': 60, 'user': '1000', 'group': '1000',
        'env': {'SSL_CERT_FILE': '/etc/tool-ca.pem'},
    })
    card = builder.create_sandbox_sysop_card('https://adx.example', 'adx-environment')
    assert card is not None
    launch = card.gateway_config.launcher_config
    assert launch.sandbox_type == 'adx-environment'
    assert launch.extra_params['rootfs_template'] == 'local-digest'
    assert launch.extra_params['runtime'] == runtime
    assert launch.extra_params['idle_timeout'] == 60
    assert launch.extra_params['user'] == '1000'
    assert launch.extra_params['group'] == '1000'
    assert launch.extra_params['env'] == {
        'HOME': str(tmp_path / 'agentos'), 'SSL_CERT_FILE': '/etc/tool-ca.pem',
    }
    assert launch.extra_params['mounts'] == [
        {'host_path': '/home/agentos/users/alice/.jiuwenswarm/agent/workspace',
         'target': str(tmp_path / 'agentos/.jiuwenswarm/agent/workspace'), 'options': ['rw']},
        {'host_path': '/home/agentos/users/alice/Documents/JiuwenSwarm',
         'target': str(tmp_path / 'agentos/Documents/JiuwenSwarm'), 'options': ['rw']},
        {'host_path': '/home/agentos/users/alice/.jiuwenswarm/agent/sessions',
         'target': str(tmp_path / 'agentos/.jiuwenswarm/agent/sessions'), 'options': ['ro']},
        {'host_path': '/home/agentos/users/alice/.jiuwenswarm/.agent_teams',
         'target': str(tmp_path / 'agentos/.jiuwenswarm/.agent_teams'), 'options': ['rw']},
    ]
    assert all(Path(mount['target']).is_relative_to(Path(launch.extra_params['env']['HOME']))
               for mount in launch.extra_params['mounts'])


@pytest.mark.parametrize('configured_home', [None, '/image/tool-home'])
def test_adx_tool_home_defaults_to_workdir_and_preserves_explicit_env(monkeypatch, tmp_path, configured_home):
    from jiuwenswarm.server.runtime.agent_adapter import sysop_builder as builder

    workspace = str(tmp_path / 'workspace')
    environment = {'TOOL_SETTING': 'kept'}
    if configured_home is not None:
        environment['HOME'] = configured_home
    monkeypatch.setenv('JIUWENSWARM_RUNTIME_PROFILE', '')
    monkeypatch.setattr(builder, '_resolve_workspace_dir', lambda: Path(workspace))
    monkeypatch.setattr(builder, 'get_sandbox_endpoint', lambda: {
        'type': 'adx-environment', 'env': environment,
        'mounts': [{'host_path': '/shared/workspace', 'target': workspace, 'options': ['rw']}],
    })
    extra = builder._build_adx_extra_params()
    assert extra['env'] == {'HOME': configured_home or workspace, 'TOOL_SETTING': 'kept'}
    assert extra['workdir'] == extra['mounts'][0]['target'] == workspace
    assert environment == ({'TOOL_SETTING': 'kept', 'HOME': configured_home}
                           if configured_home is not None else {'TOOL_SETTING': 'kept'})


@pytest.mark.parametrize('user_dir', ['', '/', 'relative/path', '/home/users/../other'])
def test_adx_tool_rejects_invalid_workspace_source(monkeypatch, user_dir):
    from jiuwenswarm.server.runtime.agent_adapter import sysop_builder as builder

    monkeypatch.setenv('JIUWENSWARM_RUNTIME_PROFILE', 'agentos')
    monkeypatch.setenv('JIUWENSWARM_USER_DIRECTORY', user_dir)
    monkeypatch.setattr(builder, '_resolve_workspace_dir', lambda: Path('/home/agentos/.jiuwenswarm/agent/workspace'))
    monkeypatch.setattr(builder, 'get_sandbox_endpoint', lambda: {'type': 'adx-environment'})
    with pytest.raises(ValueError, match='user directory'):
        builder._build_adx_extra_params()


def test_adx_tool_rejects_workspace_mount_override(monkeypatch, tmp_path):
    from jiuwenswarm.server.runtime.agent_adapter import sysop_builder as builder

    monkeypatch.setattr(builder, 'get_user_home', lambda: tmp_path / 'agentos')
    monkeypatch.setattr(builder, 'get_agent_root_dir', lambda: tmp_path / 'agentos/.jiuwenswarm/agent')

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


def test_adx_tool_rejects_session_upload_mount_override(monkeypatch, tmp_path):
    from jiuwenswarm.server.runtime.agent_adapter import sysop_builder as builder

    home = tmp_path / 'agentos'
    sessions = home / '.jiuwenswarm/agent/sessions'
    monkeypatch.setenv('JIUWENSWARM_RUNTIME_PROFILE', 'agentos')
    monkeypatch.setenv('JIUWENSWARM_USER_DIRECTORY', '/home/agentos/users/alice')
    monkeypatch.setattr(builder, 'get_user_home', lambda: home)
    monkeypatch.setattr(builder, 'get_agent_root_dir', lambda: home / '.jiuwenswarm/agent')
    monkeypatch.setattr(builder, '_resolve_workspace_dir', lambda: home / '.jiuwenswarm/agent/workspace')
    monkeypatch.setattr(builder, 'get_sandbox_endpoint', lambda: {
        'type': 'adx-environment',
        'mounts': [{'host_path': '/other', 'target': str(sessions), 'options': ['rw']}],
    })
    with pytest.raises(ValueError, match='cannot replace'):
        builder._build_adx_extra_params()


def test_adx_tool_rejects_session_root_outside_agentos_home(monkeypatch, tmp_path):
    from jiuwenswarm.server.runtime.agent_adapter import sysop_builder as builder

    home = tmp_path / 'agentos'
    monkeypatch.setenv('JIUWENSWARM_RUNTIME_PROFILE', 'agentos')
    monkeypatch.setenv('JIUWENSWARM_USER_DIRECTORY', '/home/agentos/users/alice')
    monkeypatch.setattr(builder, 'get_user_home', lambda: home)
    monkeypatch.setattr(builder, 'get_agent_root_dir', lambda: tmp_path / 'other-agent')
    monkeypatch.setattr(builder, '_resolve_workspace_dir', lambda: home / '.jiuwenswarm/agent/workspace')
    monkeypatch.setattr(builder, 'get_sandbox_endpoint', lambda: {'type': 'adx-environment'})
    with pytest.raises(ValueError, match='session uploads.*AgentOS home'):
        builder._build_adx_extra_params()


def test_adx_team_tools_mount_user_team_workspaces(monkeypatch, tmp_path):
    from jiuwenswarm.server.runtime.agent_adapter import sysop_builder as builder
    from openjiuwen.agent_teams import paths

    home = tmp_path / 'agentos'
    team_home = home / '.jiuwenswarm/.agent_teams'
    monkeypatch.setenv('JIUWENSWARM_RUNTIME_PROFILE', 'agentos')
    monkeypatch.setenv('JIUWENSWARM_USER_DIRECTORY', '/home/agentos/users/alice')
    monkeypatch.setattr(builder, 'get_user_home', lambda: home)
    monkeypatch.setattr(builder, 'get_agent_root_dir', lambda: home / '.jiuwenswarm/agent')
    monkeypatch.setattr(builder, '_resolve_workspace_dir', lambda: home / '.jiuwenswarm/agent/workspace')
    monkeypatch.setattr(paths, 'get_agent_teams_home', lambda: team_home)
    monkeypatch.setattr(builder, 'get_sandbox_endpoint', lambda: {
        'type': 'adx-environment', 'url': 'https://adx.example', 'runtime': 'conch', 'idle_timeout': 60,
    })
    monkeypatch.setattr(builder, 'get_sandbox_runtime', lambda: {'enabled': True})
    spec = builder.build_adx_team_sysop_spec('team-a', 'teammate')
    mounts = spec.gateway_config.launcher_config.extra_params['mounts']
    parent = builder.create_sandbox_sysop_card('https://adx.example', 'adx-environment')
    assert parent.gateway_config.launcher_config.extra_params['mounts'] == mounts
    assert {'host_path': '/home/agentos/users/alice/.jiuwenswarm/.agent_teams',
            'target': str(team_home), 'options': ['rw']} in mounts
    assert next(m for m in mounts if m['target'].endswith('/agent/sessions'))['options'] == ['ro']
    monkeypatch.setattr(paths, 'get_agent_teams_home', lambda: tmp_path / 'outside')
    with pytest.raises(ValueError, match='Team workspace.*AgentOS home'):
        builder._build_adx_extra_params()


def test_adx_equivalent_historical_workspace_mount_is_deduplicated():
    from jiuwenswarm.server.runtime.agent_adapter.sysop_builder import _deduplicate_adx_workspace_mount

    expected = {"host_path": "/shared/alice", "target": "/workspace", "options": ["rw"]}
    mounts = [dict(expected, type="bind"), dict(expected)]
    _deduplicate_adx_workspace_mount(mounts, expected)
    assert mounts == []
    mounts = [dict(expected, options=["ro"])]
    with pytest.raises(ValueError, match="cannot replace"):
        _deduplicate_adx_workspace_mount(mounts, expected)


def test_management_resource_override_keeps_retained_limits_valid(monkeypatch, tmp_path):
    from jiuwenswarm.server.runtime.agent_adapter import sysop_builder as builder

    monkeypatch.setenv("JIUWENSWARM_RUNTIME_PROFILE", "")
    monkeypatch.setenv("TOOL_SANDBOX_CPU", "2000")
    monkeypatch.setenv("TOOL_SANDBOX_MEMORY", "4096")
    monkeypatch.setattr(builder, "_resolve_workspace_dir", lambda: tmp_path)
    monkeypatch.setattr(builder, "get_sandbox_endpoint", lambda: {
        "type": "adx-environment", "cpu": 500, "cpu_limit": 500,
        "memory": 512, "mem_limit": 8192,
    })
    result = builder._build_adx_extra_params()
    assert result["cpu"] == result["cpu_limit"] == 2000
    assert result["memory"] == 4096
    assert result["mem_limit"] == 8192


@pytest.mark.parametrize('backend', ['adx-environment', 'jiuwenbox-conch'])
def test_shared_parent_replaces_team_scopes(monkeypatch, backend):
    from types import SimpleNamespace
    from openjiuwen.harness.schema.deep_agent_spec import SysOperationSpec
    from openjiuwen.core.sys_operation import OperationMode
    from openjiuwen.core.sys_operation.config import SandboxGatewayConfig, PreDeployLauncherConfig
    from jiuwenswarm.agents.swarm import assembly

    def operation(name, backend, target):
        return SysOperationSpec(id=name, mode=OperationMode.SANDBOX,
            gateway_config=SandboxGatewayConfig(launcher_config=PreDeployLauncherConfig(
                base_url='http://node:8080', sandbox_type=backend,
                extra_params={'mounts': [{'host_path':target,'target':target,'options':['rw']}]})))

    parent = operation('parent', backend, '/workspace')
    team = operation('adx_team_a', 'adx-environment', '/team-workspace')
    class Member:
        def __init__(self, sys_operation):self.sys_operation=sys_operation
        def model_copy(self, *, update):return Member(update['sys_operation'])
    spec = SimpleNamespace(agents={'teammate':Member(team), 'fallback':Member(None)},reviewer_agent=Member(team))
    monkeypatch.setattr(assembly, '_shared_sandbox_sys_operation_spec', lambda _:parent)
    assembly.assign_shared_sandbox_sys_operation(spec, object())
    assert spec.agents['teammate'].sys_operation is parent
    assert spec.reviewer_agent.sys_operation is parent
    assert spec.agents['fallback'].sys_operation is parent


def test_adx_projects_and_team_roles_reuse_one_environment_until_idle_retirement(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    import adx_environment
    from openjiuwen.core.sys_operation import SysOperation, SysOperationCard
    from openjiuwen.core.sys_operation.sandbox.gateway.gateway import SandboxEndpoint
    from openjiuwen.extensions.sys_operation.sandbox.providers import adx_environment as provider
    from jiuwenswarm.server.runtime.agent_adapter import sysop_builder as builder

    monkeypatch.setattr(builder, '_build_adx_extra_params', lambda: {
        'idle_timeout': 60, 'connection': {'token': 'test-key'},
    })
    monkeypatch.setattr(builder, 'get_sandbox_endpoint', lambda: {
        'type': 'adx-environment', 'url': 'https://adx.example',
    })
    monkeypatch.setattr(builder, 'get_sandbox_runtime', lambda: {'enabled': True})
    monkeypatch.setattr(builder, 'is_agentos_runtime', lambda: False)
    monkeypatch.setattr(builder, '_sandbox_isolation_custom_id', lambda project: str(project))
    monkeypatch.setattr(provider._AdxEnvironmentBackend, '_instances', {})
    monkeypatch.setattr(provider._AdxEnvironmentBackend, '_incarnations', {})
    monkeypatch.setenv('JIUWENSWARM_ADX_AGENT_KEY', 'alice-agent')
    environments = [MagicMock(), MagicMock(), MagicMock()]
    for environment in environments:
        environment.get_info.return_value = SimpleNamespace(state='running')
    factory = MagicMock(side_effect=environments)
    monkeypatch.setattr(adx_environment, 'Environment', factory)
    cards = [builder.create_sandbox_sysop_card('https://adx.example', 'adx-environment', project_dir=project)
             for project in ('/project-a', '/project-b')]
    for team, role in (('team-a', 'leader'), ('team-a', 'teammate'), ('team-b', 'reviewer')):
        spec = builder.build_adx_team_sysop_spec(team, role)
        cards.append(SysOperationCard(id=spec.id, mode=spec.mode,
                                     work_config=spec.work_config, gateway_config=spec.gateway_config))
    providers = []
    for card in cards:
        operation = SysOperation(card)
        endpoint = SandboxEndpoint(base_url='https://adx.example',
                                   isolation_key=operation._run_config.isolation_key_template)
        providers.append(provider.AdxEnvironmentFSProvider(endpoint, card.gateway_config))
    for backend in providers:
        assert backend._get_environment() is environments[0]
    assert factory.call_count == 1
    assert factory.call_args.kwargs['idle_timeout'] == 60
    environments[0].get_info.return_value = SimpleNamespace(state='stopped')
    assert providers[-1]._get_environment() is environments[1]
    assert providers[0]._get_environment() is environments[1]
    assert factory.call_count == 2
    monkeypatch.setenv('JIUWENSWARM_ADX_AGENT_KEY', 'bob-agent')
    assert providers[0]._get_environment() is environments[2]
    assert factory.call_count == 3

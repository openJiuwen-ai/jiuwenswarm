"""Shared mounted-provider tool configuration for AgentOS and Team recovery."""
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.parametrize('runtime', ['runsc', 'conch'])
def test_mounted_provider_reads_an_imported_skill_from_the_workspace(monkeypatch, tmp_path, runtime):
    from jiuwenswarm.server.runtime.agent_adapter import sysop_builder as builder
    from openjiuwen.agent_teams import paths

    user_root = tmp_path / 'shared/alice'
    installed = user_root / '.jiuwenswarm/agent/workspace/skills/browser-import'
    script = installed / 'scripts/probe.py'
    script.parent.mkdir(parents=True)
    script.write_text("print('IMPORTED_SKILL_EXECUTED')\n")
    home = tmp_path / 'tool-visible-home'
    workspace = home / '.jiuwenswarm/agent/workspace'
    monkeypatch.setenv('JIUWENSWARM_RUNTIME_PROFILE', 'agentos')
    monkeypatch.setenv('JIUWENSWARM_USER_DIRECTORY', str(user_root))
    monkeypatch.setattr(builder, 'get_user_home', lambda: home)
    monkeypatch.setattr(builder, 'get_agent_root_dir', lambda: home / '.jiuwenswarm/agent')
    monkeypatch.setattr(builder, '_resolve_workspace_dir', lambda: workspace)
    monkeypatch.setattr(paths, 'get_agent_teams_home', lambda: home / '.jiuwenswarm/.agent_teams')
    monkeypatch.setattr(builder, 'get_sandbox_endpoint', lambda: {
        'type': 'example-provider', 'url': 'https://sandbox.example', 'runtime': runtime,
        'launcher_config': {'workspace_mounts': True, 'shared_scope': 'default'},
    })
    card = builder.create_sandbox_sysop_card('https://sandbox.example', 'example-provider')
    mount = next(item for item in card.gateway_config.launcher_config.extra_params['mounts']
                 if item['target'] == str(workspace))
    mapped = Path(mount['host_path']) / 'skills/browser-import/scripts/probe.py'
    assert mapped == script
    executed = subprocess.run([sys.executable, str(mapped)], capture_output=True, text=True, check=True)
    assert executed.stdout.strip() == 'IMPORTED_SKILL_EXECUTED'


@pytest.mark.parametrize('runtime', ['runsc', 'conch'])
def test_mounted_tool_card_uses_shared_agentos_workspace(monkeypatch, tmp_path, runtime):
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
        'launcher_config': {'workspace_mounts': True, 'shared_scope': 'default'},
        'type': 'example-provider', 'url': 'https://sandbox.example', 'image': 'tools:v1',
        'rootfs_template': 'local-digest', 'runtime': runtime, 'idle_timeout': 60, 'user': '1000', 'group': '1000',
        'env': {'SSL_CERT_FILE': '/etc/tool-ca.pem'},
    })
    card = builder.create_sandbox_sysop_card('https://sandbox.example', 'example-provider')
    assert card is not None
    launch = card.gateway_config.launcher_config
    assert launch.sandbox_type == 'example-provider'
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
def test_mounted_tool_home_defaults_to_workdir_and_preserves_explicit_env(monkeypatch, tmp_path, configured_home):
    from jiuwenswarm.server.runtime.agent_adapter import sysop_builder as builder

    workspace = str(tmp_path / 'workspace')
    environment = {'TOOL_SETTING': 'kept'}
    if configured_home is not None:
        environment['HOME'] = configured_home
    monkeypatch.setenv('JIUWENSWARM_RUNTIME_PROFILE', '')
    monkeypatch.setattr(builder, '_resolve_workspace_dir', lambda: Path(workspace))
    monkeypatch.setattr(builder, 'get_sandbox_endpoint', lambda: {
        'type': 'example-provider', 'env': environment,
        'mounts': [{'host_path': '/shared/workspace', 'target': workspace, 'options': ['rw']}],
    })
    extra = builder._build_mounted_sandbox_extra_params()
    assert extra['env'] == {'HOME': configured_home or workspace, 'TOOL_SETTING': 'kept'}
    assert extra['workdir'] == extra['mounts'][0]['target'] == workspace
    assert environment == ({'TOOL_SETTING': 'kept', 'HOME': configured_home}
                           if configured_home is not None else {'TOOL_SETTING': 'kept'})


@pytest.mark.parametrize('user_dir', ['', '/', 'relative/path', '/home/users/../other'])
def test_mounted_tool_rejects_invalid_workspace_source(monkeypatch, user_dir):
    from jiuwenswarm.server.runtime.agent_adapter import sysop_builder as builder

    monkeypatch.setenv('JIUWENSWARM_RUNTIME_PROFILE', 'agentos')
    monkeypatch.setenv('JIUWENSWARM_USER_DIRECTORY', user_dir)
    monkeypatch.setattr(builder, '_resolve_workspace_dir', lambda: Path('/home/agentos/.jiuwenswarm/agent/workspace'))
    monkeypatch.setattr(builder, 'get_sandbox_endpoint', lambda: {'type': 'example-provider'})
    with pytest.raises(ValueError, match='user directory'):
        builder._build_mounted_sandbox_extra_params()


def test_mounted_tool_rejects_workspace_mount_override(monkeypatch, tmp_path):
    from jiuwenswarm.server.runtime.agent_adapter import sysop_builder as builder

    monkeypatch.setattr(builder, 'get_user_home', lambda: tmp_path / 'agentos')
    monkeypatch.setattr(builder, 'get_agent_root_dir', lambda: tmp_path / 'agentos/.jiuwenswarm/agent')

    monkeypatch.setenv('JIUWENSWARM_RUNTIME_PROFILE', 'agentos')
    monkeypatch.setenv('JIUWENSWARM_USER_DIRECTORY', '/home/agentos/users/alice')
    monkeypatch.setattr(builder, '_resolve_workspace_dir', lambda: Path('/home/agentos/.jiuwenswarm/agent/workspace'))
    monkeypatch.setattr(builder, 'get_sandbox_endpoint', lambda: {
        'type': 'example-provider',
        'mounts': [{'host_path': '/other', 'target': '/home/agentos/.jiuwenswarm/agent/workspace'}],
    })
    with pytest.raises(ValueError, match='cannot replace'):
        builder._build_mounted_sandbox_extra_params()


def test_mounted_tool_endpoint_preserves_runtime_settings(monkeypatch):
    from jiuwenswarm.common import config

    monkeypatch.setattr(config, 'get_config', lambda: {'sandbox': {
        'launcher_config': {'workspace_mounts': True, 'shared_scope': 'default'},
        'type': 'example-provider', 'url': 'https://sandbox.example', 'image': 'tools:v1',
        'runtime': 'conch', 'idle_timeout': 60, 'user': 1000, 'group': 1000,
        'endpoint_file': '/run/provider-node.json',
    }})
    endpoint = config.get_sandbox_endpoint()
    assert endpoint['image'] == 'tools:v1'
    assert endpoint['runtime'] == 'conch'
    assert endpoint['idle_timeout'] == 60
    assert endpoint['user'] == endpoint['group'] == 1000
    assert endpoint['endpoint_file'] == '/run/provider-node.json'


def test_mounted_tool_rejects_session_upload_mount_override(monkeypatch, tmp_path):
    from jiuwenswarm.server.runtime.agent_adapter import sysop_builder as builder

    home = tmp_path / 'agentos'
    sessions = home / '.jiuwenswarm/agent/sessions'
    monkeypatch.setenv('JIUWENSWARM_RUNTIME_PROFILE', 'agentos')
    monkeypatch.setenv('JIUWENSWARM_USER_DIRECTORY', '/home/agentos/users/alice')
    monkeypatch.setattr(builder, 'get_user_home', lambda: home)
    monkeypatch.setattr(builder, 'get_agent_root_dir', lambda: home / '.jiuwenswarm/agent')
    monkeypatch.setattr(builder, '_resolve_workspace_dir', lambda: home / '.jiuwenswarm/agent/workspace')
    monkeypatch.setattr(builder, 'get_sandbox_endpoint', lambda: {
        'type': 'example-provider',
        'mounts': [{'host_path': '/other', 'target': str(sessions), 'options': ['rw']}],
    })
    with pytest.raises(ValueError, match='cannot replace'):
        builder._build_mounted_sandbox_extra_params()


def test_mounted_tool_rejects_session_root_outside_agentos_home(monkeypatch, tmp_path):
    from jiuwenswarm.server.runtime.agent_adapter import sysop_builder as builder

    home = tmp_path / 'agentos'
    monkeypatch.setenv('JIUWENSWARM_RUNTIME_PROFILE', 'agentos')
    monkeypatch.setenv('JIUWENSWARM_USER_DIRECTORY', '/home/agentos/users/alice')
    monkeypatch.setattr(builder, 'get_user_home', lambda: home)
    monkeypatch.setattr(builder, 'get_agent_root_dir', lambda: tmp_path / 'other-agent')
    monkeypatch.setattr(builder, '_resolve_workspace_dir', lambda: home / '.jiuwenswarm/agent/workspace')
    monkeypatch.setattr(builder, 'get_sandbox_endpoint', lambda: {'type': 'example-provider'})
    with pytest.raises(ValueError, match='session uploads.*AgentOS home'):
        builder._build_mounted_sandbox_extra_params()


def test_mounted_team_tools_mount_user_team_workspaces(monkeypatch, tmp_path):
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
        'launcher_config': {'workspace_mounts': True, 'shared_scope': 'default'},
        'type': 'example-provider', 'url': 'https://sandbox.example', 'runtime': 'conch', 'idle_timeout': 60,
    })
    monkeypatch.setattr(builder, 'get_sandbox_runtime', lambda: {'enabled': True})
    spec = builder.build_shared_sandbox_sysop_spec('team-a', 'teammate')
    mounts = spec.gateway_config.launcher_config.extra_params['mounts']
    parent = builder.create_sandbox_sysop_card('https://sandbox.example', 'example-provider')
    assert parent.gateway_config.launcher_config.extra_params['mounts'] == mounts
    assert {'host_path': '/home/agentos/users/alice/.jiuwenswarm/.agent_teams',
            'target': str(team_home), 'options': ['rw']} in mounts
    assert next(m for m in mounts if m['target'].endswith('/agent/sessions'))['options'] == ['ro']
    monkeypatch.setattr(paths, 'get_agent_teams_home', lambda: tmp_path / 'outside')
    with pytest.raises(ValueError, match='Team workspace.*AgentOS home'):
        builder._build_mounted_sandbox_extra_params()


def test_mounted_equivalent_historical_workspace_mount_is_deduplicated():
    from jiuwenswarm.server.runtime.agent_adapter.sysop_builder import _deduplicate_workspace_mount

    expected = {"host_path": "/shared/alice", "target": "/workspace", "options": ["rw"]}
    mounts = [dict(expected, type="bind"), dict(expected)]
    _deduplicate_workspace_mount(mounts, expected)
    assert mounts == []
    mounts = [dict(expected, options=["ro"])]
    with pytest.raises(ValueError, match="cannot replace"):
        _deduplicate_workspace_mount(mounts, expected)


def test_management_resource_override_keeps_retained_limits_valid(monkeypatch, tmp_path):
    from jiuwenswarm.server.runtime.agent_adapter import sysop_builder as builder

    monkeypatch.setenv("JIUWENSWARM_RUNTIME_PROFILE", "")
    monkeypatch.setenv("TOOL_SANDBOX_CPU", "2000")
    monkeypatch.setenv("TOOL_SANDBOX_MEMORY", "4096")
    monkeypatch.setattr(builder, "_resolve_workspace_dir", lambda: tmp_path)
    monkeypatch.setattr(builder, "get_sandbox_endpoint", lambda: {
        "type": "example-provider", "cpu": 500, "cpu_limit": 500,
        "memory": 512, "mem_limit": 8192,
    })
    result = builder._build_mounted_sandbox_extra_params()
    assert result["cpu"] == result["cpu_limit"] == 2000
    assert result["memory"] == 4096
    assert result["mem_limit"] == 8192


@pytest.mark.parametrize('backend', ['example-provider', 'jiuwenbox-conch'])
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
    team = operation('adx_team_a', 'example-provider', '/team-workspace')
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

    adx_environment = pytest.importorskip("adx_environment")
    pytest.importorskip("openjiuwen.extensions.sys_operation.sandbox.providers.adx_environment")
    from openjiuwen.core.sys_operation import SysOperation, SysOperationCard
    from openjiuwen.core.sys_operation.sandbox.gateway.gateway import SandboxEndpoint
    from openjiuwen.extensions.sys_operation.sandbox.providers import adx_environment as provider
    from jiuwenswarm.server.runtime.agent_adapter import sysop_builder as builder

    monkeypatch.setattr(builder, '_build_adx_extra_params', lambda: {
        'idle_timeout': 60, 'connection': {'token': 'test-key'},
    })
    monkeypatch.setattr(builder, '_build_mounted_sandbox_extra_params', lambda: {'env': {}, 'mounts': []})
    monkeypatch.setattr(builder, 'get_sandbox_endpoint', lambda: {
        'launcher_config': {'workspace_mounts': True, 'shared_scope': 'default'},
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
        spec = builder.build_shared_sandbox_sysop_spec(team, role)
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

def test_shared_card_failure_preserves_team_configuration(monkeypatch):
    from jiuwenswarm.server.runtime.agent_adapter import sysop_builder as builder

    monkeypatch.setattr(builder, 'get_sandbox_runtime', lambda: {'enabled': True})
    monkeypatch.setattr(builder, 'get_sandbox_endpoint', lambda: {
        'type': 'external', 'url': 'http://sandbox', 'launcher_config': {'workspace_mounts': True, 'shared_scope': 's'},
    })
    monkeypatch.setattr(builder, 'create_sandbox_sysop_card', lambda *_: None)
    assert builder.build_shared_sandbox_sysop_spec('team', 'leader') is None


def test_shared_provider_preserves_command_policy_and_legacy_mount(monkeypatch, tmp_path):
    from jiuwenswarm.server.runtime.agent_adapter import sysop_builder as builder

    monkeypatch.setattr(builder, 'is_agentos_runtime', lambda: False)
    monkeypatch.setattr(builder, '_resolve_workspace_dir', lambda: tmp_path)
    endpoint = {'type': 'external', 'mounts': [{'source': '/data', 'target': '/models', 'readonly': True}],
                'launcher_config': {'workspace_mounts': True, 'shared_scope': 'test-scope'}}
    monkeypatch.setattr(builder, 'get_sandbox_endpoint', lambda: endpoint)
    monkeypatch.setattr(builder, 'build_filesystem_policy', lambda *_args, **_kw: (
        {'filesystem_policy': {'read_only': ['/models']}}, [{'source': 'attachment'}]))
    card = builder.create_sandbox_sysop_card('http://sandbox', 'external', excluded_commands=['blocked'],
                                            fallback_on_failure=True, idle_check_interval=2)
    params = card.gateway_config.launcher_config.extra_params
    assert card.gateway_config.isolation.custom_id == 'test-scope'
    assert params['excluded_commands'] == ['blocked']
    assert params['idle_check_interval'] == 2
    assert params['fallback_on_failure'] is True
    assert params['policy']['filesystem_policy']['read_only'] == ['/models']
    assert params['preserve_files_upload'] == [{'source': 'attachment'}]
    assert {'host_path': '/data', 'target': '/models', 'options': ['ro']} in params['mounts']


def test_shared_scope_keeps_native_conch_template_and_identity(monkeypatch, tmp_path):
    from jiuwenswarm.server.runtime.agent_adapter import sysop_builder as builder

    endpoint = {'type': 'jiuwenbox-conch', 'template_name': 'native-template', 'user': '1001', 'group': '2001',
                'launcher_config': {'workspace_mounts': True, 'shared_scope': 'test-scope'}}
    monkeypatch.setattr(builder, 'get_sandbox_endpoint', lambda: endpoint)
    bind = {'host_path': str(tmp_path), 'sandbox_path': '/workspace', 'mode': 'rw'}
    monkeypatch.setattr(builder, 'build_filesystem_policy', lambda *_args, **_kw: (
        {'filesystem_policy': {'bind_mounts': [bind]}}, []))
    card = builder.create_sandbox_sysop_card('http://sandbox', 'jiuwenbox-conch')
    launcher = card.gateway_config.launcher_config
    assert launcher.sandbox_type == 'jiuwenbox'
    assert launcher.extra_params['api_sandbox_runtime'] == 'conch'
    config = launcher.extra_params['policy']['conch']
    assert (config['template_name'], config['run_as_user'], config['run_as_group']) == (
        'native-template', '1001', '2001')
    assert config['filesystem_policy']['bind_mounts'] == [bind]

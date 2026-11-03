import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import Mock

import pytest

ROOT = Path(__file__).resolve().parents[3]
SPEC = importlib.util.spec_from_file_location('swarm_deploy', ROOT / 'deploy/adx/configure.py')
deploy = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(deploy)


@pytest.fixture
def settings(tmp_path):
    workspace = tmp_path / 'users'
    workspace.mkdir()
    token = tmp_path / 'token'
    token.write_text('fixture-token')
    return {'profile': 'standalone', 'python': '/venv/bin/python',
            'token_file': str(token), 'ca_file': str(tmp_path / 'ca'),
            'tenant': 'tenant', 'namespace': 'namespace',
            'ingress_url': 'https://adx.example:8443', 'node_ingress_url': 'https://node.example:8443',
            'workspace_root': str(workspace), 'runtime': {'agent_image': 'agent:v1', 'tool_image': 'tool:v1'},
            'gateway': {'home': str(tmp_path), 'host': '127.0.0.1', 'web_host': '127.0.0.1',
                        'port': 29001, 'web_port': 29000, 'static_port': 25173}}


def test_standalone_does_not_inherit_platform_services(settings, monkeypatch):
    config = deploy.data_directory(settings) / 'config/config.yaml'
    deploy.write_yaml(config, {'models': [{'name': 'keep'}], 'gateway': {'agentos': {
        'auth_enabled': True, 'auth_service_url': 'http://unreachable-iam',
        'registry': {'endpoint': 'http://unreachable-registry'}}}})
    monkeypatch.setattr(deploy, 'check_business', lambda settings: None)
    monkeypatch.setattr(deploy.subprocess, 'run', lambda *args, **kwargs: pytest.fail('unexpected init'))
    deploy.configure(settings)
    actual = deploy.read_yaml(config)
    assert actual['gateway']['agentos']['registry']['endpoint'] == ''
    assert actual['gateway']['agentos']['auth_enabled'] is False
    assert actual['models'] == [{'name': 'keep'}]
    assert actual['sandbox']['type'] == 'adx-environment'


def test_instance_initialization_uses_private_data_directory(settings, monkeypatch):
    settings['gateway']['instance_name'] = 'isolated'
    expected = deploy.data_directory(settings)
    def init(command, **kwargs):
        assert kwargs['env']['JIUWENSWARM_DATA_DIR'] == str(expected)
        assert '-f' not in command
        deploy.write_yaml(expected / 'config/config.yaml', {})
    monkeypatch.setattr(deploy, 'check_business', lambda settings: None)
    monkeypatch.setattr(deploy.subprocess, 'run', init)
    envfile = deploy.configure(settings)
    assert envfile.stat().st_mode & 0o777 == 0o600
    from dotenv import dotenv_values

    assert envfile == expected / 'config/.env'
    assert dotenv_values(envfile)['JIUWENSWARM_ADX_NODE_INGRESS_URL'] == settings['node_ingress_url']
    assert not (Path(settings['gateway']['home']) / '.jiuwenswarm').exists()


@pytest.mark.parametrize('field', ['host', 'web_host'])
def test_standalone_rejects_public_listeners(settings, tmp_path, field):
    settings['gateway'][field] = '0.0.0.0'
    path = tmp_path / 'input.yaml'
    deploy.write_yaml(path, settings)
    with pytest.raises(ValueError, match='loopback'):
        deploy.load_settings(path)


def test_explicit_platform_configuration(settings, monkeypatch):
    settings['profile'] = 'agentos'
    settings['gateway']['agentos'] = {'auth_enabled': True, 'auth_service_url': 'http://iam',
                                      'registry': {'endpoint': 'http://registry'}}
    config = deploy.data_directory(settings) / 'config/config.yaml'
    deploy.write_yaml(config, {})
    monkeypatch.setattr(deploy, 'check_business', lambda settings: None)
    deploy.configure(settings)
    actual = deploy.read_yaml(config)['gateway']['agentos']
    assert actual['registry']['endpoint'] == 'http://registry'
    assert actual['auth_enabled'] is True


@pytest.mark.parametrize('exit_code', [0, 17])
def test_initialization_uses_configured_python_without_console_script(settings, tmp_path, monkeypatch, exit_code):
    package = tmp_path / 'modules/jiuwenswarm'
    package.mkdir(parents=True)
    (package / '__init__.py').write_text('')
    (package / 'init_workspace.py').write_text(
        'import os, sys\nfrom pathlib import Path\n'
        'home = Path(os.environ["JIUWENSWARM_HOME"])\n'
        'with (home / "init-calls").open("a") as stream: stream.write("called\\n")\n'
        f'if {exit_code}: sys.exit({exit_code})\n'
        'config = home / ".jiuwenswarm/config/config.yaml"\n'
        'config.parent.mkdir(parents=True, exist_ok=True)\n'
        'config.write_text(\'{"models": [{"name": "retained-model"}]}\')\n'
    )
    # No console script exists beside this interpreter or on PATH.
    interpreter = tmp_path / 'python-only/python'
    interpreter.parent.mkdir()
    interpreter.symlink_to(sys.executable)
    settings['python'] = str(interpreter)
    monkeypatch.setenv('PATH', str(interpreter.parent))
    monkeypatch.setenv('PYTHONPATH', str(package.parent))
    monkeypatch.delenv('JIUWENSWARM_DATA_DIR', raising=False)
    monkeypatch.setattr(deploy, 'check_business', lambda settings: None)
    sentinel = tmp_path / 'history.db'
    sentinel.write_bytes(b'retained-history')
    if exit_code:
        with pytest.raises(subprocess.CalledProcessError) as error:
            deploy.configure(settings)
        assert error.value.returncode == exit_code
        assert not (deploy.data_directory(settings) / 'config/.env').exists()
    else:
        deploy.configure(settings)
        deploy.configure(settings)
        assert deploy.read_yaml(deploy.data_directory(settings) / 'config/config.yaml')['models'] == [
            {'name': 'retained-model'}]
    assert (tmp_path / 'init-calls').read_text() == 'called\n'
    assert sentinel.read_bytes() == b'retained-history'

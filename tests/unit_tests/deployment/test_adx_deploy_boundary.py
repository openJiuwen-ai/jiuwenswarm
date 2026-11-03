import importlib.util
import json
import os
from pathlib import Path
import subprocess
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
    assert deploy.read_environment(envfile)['JIUWENSWARM_DATA_DIR'] == str(expected)
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

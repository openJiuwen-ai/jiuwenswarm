"""Stopping an application must not require its runtime startup configuration."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / 'deploy/yuanrong/service.py'
SPEC = importlib.util.spec_from_file_location('swarm_identity', SCRIPT)
deploy = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(deploy)


@pytest.fixture
def settings_file(tmp_path):
    path = tmp_path / 'settings.yaml'
    path.write_text('''profile: agentos
business_env_file: ${ADX_BUSINESS_ENV_FILE}
token_file: ${ADX_TOKEN_FILE}
ingress_url: ${ADX_INGRESS_URL}
gateway:
  home: ${HOME}
  instance_name: isolated-stop
  static_port: 25173
''')
    return path


@pytest.mark.parametrize('content', [None, '', 'UNRELATED="old"\n', 'unrelated-malformed-binding'])
def test_identity_cli_does_not_read_unrelated_bindings(settings_file, tmp_path, content):
    bindings = tmp_path / 'deployment.env'
    if content is not None:
        bindings.write_text(content)
    env = {'PATH': os.environ['PATH'], 'HOME': str(tmp_path), 'PYTHONDONTWRITEBYTECODE': '1'}
    result = subprocess.run([sys.executable, str(SCRIPT), 'identity', '--settings', str(settings_file),
                             '--bindings', str(bindings)], env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert 'isolated-stop' in result.stdout
    assert '25173' in result.stdout
    assert 'ADX_TOKEN_FILE' not in result.stdout


def test_service_settings_ignore_platform_startup_inputs(settings_file, tmp_path):
    actual = deploy.load_settings(settings_file)
    assert actual["gateway"]["port"] == 19001
    assert actual["gateway"]["instance_name"] == "isolated-stop"


def test_identity_reads_only_required_old_binding(settings_file, tmp_path, monkeypatch):
    settings_file.write_text(settings_file.read_text().replace('${HOME}', '${DEPLOY_HOME}'))
    bindings = tmp_path / 'old.env'
    bindings.write_text('DEPLOY_HOME=' + json.dumps(str(tmp_path / 'old-home')) + '\n')
    actual = deploy.load_identity(settings_file, bindings)
    assert actual['gateway']['home'] == str(tmp_path / 'old-home')
    assert actual['gateway']['instance_name'] == 'isolated-stop'


def test_explicit_platform_identity_works_without_settings_or_bindings(tmp_path, monkeypatch):
    monkeypatch.setenv('SWARM_INSTANCE_NAME', '')
    monkeypatch.setenv('SWARM_HOME', str(tmp_path))
    actual = deploy.load_identity(tmp_path / 'missing.yaml', tmp_path / 'missing.env')
    assert actual['gateway']['instance_name'] == ''
    assert actual['gateway']['home'] == str(tmp_path)


def test_missing_custom_identity_fails_without_selecting_default(tmp_path, monkeypatch):
    monkeypatch.delenv('SWARM_INSTANCE_NAME', raising=False)
    path = tmp_path / 'identity.yaml'
    path.write_text('gateway:\n  instance_name: ${UNAVAILABLE_INSTANCE}\n')
    with pytest.raises(ValueError, match='service identity'):
        deploy.load_identity(path, tmp_path / 'missing.env')


@pytest.mark.parametrize('name', ['bad/name', '../other', 'bad;name'])
def test_invalid_identity_cannot_select_or_execute_another_service(tmp_path, name):
    path = tmp_path / 'identity.yaml'
    path.write_text(yaml.safe_dump({'gateway': {'instance_name': name}}))
    with pytest.raises(ValueError, match='instance_name'):
        deploy.load_identity(path)


def test_service_variables_preserve_prepared_configuration(tmp_path):
    settings = tmp_path / "settings.yaml"
    settings.write_text("profile: agentos\ngateway:\n  home: " + str(tmp_path) +
                        "\n  host: 0.0.0.0\n  web_host: 0.0.0.0\n  web_port: 29100\n"
                        "  agentos: {registry: {endpoint: '${UNRESOLVED_REGISTRY}'}}\n"
                        "runtime: {node_ingress_url: '${UNRESOLVED_NODE}'}\n")
    config = tmp_path / ".jiuwenswarm/config/config.yaml"
    config.parent.mkdir(parents=True)
    config.write_text("# prepared business configuration\nsandbox: {type: adx-environment}\n")
    env = config.with_name(".env")
    env.write_text("MODEL_NAME=preserved\n")
    before = {p: p.read_bytes() for p in (config, env, settings)}
    result = subprocess.run([sys.executable, str(SCRIPT), "variables", "--settings", str(settings)],
                            capture_output=True, text=True, check=True)
    assert "29100" in result.stdout
    assert "0.0.0.0" in result.stdout
    assert "UNRESOLVED" not in result.stdout
    assert {p: p.read_bytes() for p in before} == before

"""AgentServer consumes generic launch bindings without backend-specific code."""
import copy
import json
import os

import pytest

from jiuwenswarm.common.runtime_settings import bind_runtime_sandbox, prepare_runtime_environment
from jiuwenswarm.dotenv_early import load_dotenv_runtime


def test_arbitrary_backend_credentials_and_node_binding_are_generic(monkeypatch, tmp_path):
    monkeypatch.setenv('JIUWENSWARM_RUNTIME_PROFILE', 'agentos')
    monkeypatch.setenv('TEST_LAUNCH_TOKEN', 'current-secret')
    monkeypatch.setenv('TEST_NODE_URL', 'https://worker.example:8443')
    monkeypatch.setenv('JIUWENSWARM_RUNTIME_SETTINGS', json.dumps({
        'credentials': {'TEST_LAUNCH_TOKEN': 'TEST_PROVIDER_TOKEN'},
        'preserve_env': ['TEST_NODE_URL', 'TEST_PROVIDER_TOKEN'],
    }))
    deployment = {'type': 'example-provider', 'managed_connection': {
        'url_env': 'TEST_NODE_URL', 'context_file_env': 'TEST_NODE_CONTEXT',
        'replace_connection_fields': ['server_address', 'token'],
        'credential_fields': ['TEST_PROVIDER_TOKEN'],
    }, 'launcher_config': {'workspace_mounts': True, 'shared_scope': 'default'}}
    sandbox = {'type': 'example-provider', 'runtime': 'retained-runtime', 'image': 'retained-image',
               'connection': {'server_address': 'previous-node', 'token': 'old', 'timeout': 19},
               'env': {'TEST_PROVIDER_TOKEN': 'old', 'USER_OPTION': 'kept'}}
    before = copy.deepcopy(sandbox)
    prepare_runtime_environment()
    dotenv = tmp_path / '.env'
    dotenv.write_text('TEST_PROVIDER_TOKEN=stale\nTEST_NODE_URL=https://old.example\nMODEL_NAME=new\n')
    load_dotenv_runtime(dotenv)
    actual = bind_runtime_sandbox(sandbox, deployment)
    assert os.environ['TEST_PROVIDER_TOKEN'] == 'current-secret'
    assert 'TEST_LAUNCH_TOKEN' not in os.environ
    assert sandbox == before and actual['runtime'] == sandbox['runtime'] and actual['image'] == sandbox['image']
    assert actual['url'] == 'https://worker.example:8443'
    assert actual['connection'] == {'timeout': 19, 'use_tls': True, 'verify_tls': True}
    assert actual['env'] == {'USER_OPTION': 'kept'}
    assert actual['launcher_config'] == deployment['launcher_config']


@pytest.mark.parametrize('runtime', ['conch', 'runsc'])
def test_gateway_bindings_preserve_existing_workspace_and_team_tool_semantics(monkeypatch, tmp_path, runtime):
    from jiuwenswarm.extensions.agentos.agentos_router.adx.config import load_router_config
    from jiuwenswarm.extensions.agentos.agentos_router.adx.control_client import AdxControlClient

    monkeypatch.setenv('ROUTER_BACKEND_TEST_TOKEN', 'test-only-token')
    control = AdxControlClient(load_router_config({'gateway': {'adx': {
        'ingress_url': 'https://master.example', 'tenant': 'test', 'token_env': 'ROUTER_BACKEND_TEST_TOKEN',
    }}}))
    body = control._creation_body('test', 'alice+jiuwenswarm', '/shared/alice', {}, {}, [], builtin=True)
    for key, value in body['env'].items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv('JIUWENSWARM_RUNTIME_PROFILE', 'agentos')
    monkeypatch.setenv('JIUWENSWARM_USER_DIRECTORY', '/shared/alice')
    context = tmp_path / 'node.json';context.write_text(json.dumps({'ingress_url': 'https://worker.example:8443'}))
    monkeypatch.setenv('JIUWENSWARM_ADX_NODE_CONTEXT_FILE', str(context))
    prepare_runtime_environment()
    static = {'type': 'adx-environment', 'runtime': runtime,
              'cpu': 750, 'memory': 1024, 'idle_timeout': 60, 'user': '1000', 'group': '1000',
              'connection': {'server_address': 'previous-node', 'token': 'old'}, 'mounts': []}
    if runtime == 'runsc':
        static['rootfs'] = {'type': 'local', 'path': '/objects/retained/rootfs'}
    before = copy.deepcopy(static)
    bound = bind_runtime_sandbox(static, json.loads(body['env']['JIUWENSWARM_AGENTOS_SANDBOX_CONFIG']))
    assert static == before
    assert bound['url'] == 'https://worker.example:8443'
    for key in ('runtime', 'cpu', 'memory', 'user', 'group', 'mounts'):
        assert bound[key] == static[key]
    if runtime == 'runsc':
        assert bound['rootfs'] == static['rootfs']
    assert bound['endpoint_file'] == str(context)
    assert bound['connection'] == {'use_tls': True, 'verify_tls': True}
    assert os.environ['ADX_TOKEN'] == 'test-only-token'

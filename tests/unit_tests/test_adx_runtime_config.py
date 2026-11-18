"""AgentServer binds node connectivity without migrating stored tool settings."""
import copy
import json

import pytest

from jiuwenswarm.common.runtime_settings import bind_runtime_sandbox


@pytest.mark.parametrize("sandbox_type", ["adx-environment", " ADX-Environment "])
def test_runtime_keeps_static_workspace_settings_and_rebinds_only_connection(
    monkeypatch, tmp_path, adx_workspace_binding, sandbox_type,
):
    sandbox = {'type': sandbox_type, 'runtime': 'conch', 'image': 'custom-template',
               'cpu': 750, 'idle_timeout': 31, 'mounts': [{'host_path': '/shared', 'target': '/data'}],
               'url': 'https://previous-node:8443',
               'connection': {'server_address': 'previous-node:8443', 'token': 'old-secret',
                              'verify_tls': False, 'timeout': 19}}
    before = copy.deepcopy(sandbox)
    context = tmp_path / 'node.json'
    context.write_text(json.dumps({'ingress_url': 'https://current-node:8443'}))
    monkeypatch.setenv('JIUWENSWARM_ADX_NODE_CONTEXT_FILE', str(context))
    actual = bind_runtime_sandbox(sandbox, adx_workspace_binding)
    assert sandbox == before
    assert actual['runtime'] == 'conch'
    assert actual['image'] == 'custom-template'
    assert actual['cpu'] == 750
    assert actual['mounts'] == sandbox['mounts']
    assert actual['url'] == 'https://current-node:8443'
    assert actual['endpoint_file'] == str(context)
    assert actual['connection'] == {'timeout': 19, 'verify_tls': True, 'use_tls': True}


@pytest.mark.parametrize('sandbox', [{}, {'type': 'yuanrong', 'executor': 'docker'},
                                     {'type': 'jiuwenbox-conch', 'template_name': 'old'}])
def test_runtime_requires_an_already_migrated_workspace(sandbox, adx_workspace_binding):
    with pytest.raises(ValueError, match='deployment migration'):
        bind_runtime_sandbox(sandbox, adx_workspace_binding)


def test_runtime_protocol_follows_current_node_and_does_not_resolve_images(monkeypatch, adx_workspace_binding):
    monkeypatch.setenv('JIUWENSWARM_ADX_NODE_INGRESS_URL', 'http://10.88.4.1:8080')
    adx_workspace_binding['managed_connection']['credential_fields'] = None
    actual = bind_runtime_sandbox({
        'type': 'adx-environment', 'runtime': 'runsc',
        'rootfs': {'type': 'local', 'path': '/objects/prepared/rootfs'},
        'env': {'TOOL_LANGUAGE': 'en'},
    }, adx_workspace_binding)
    assert actual['rootfs'] == {'type': 'local', 'path': '/objects/prepared/rootfs'}
    assert actual['connection'] == {'verify_tls': False, 'use_tls': False}
    assert actual['env'] == {'TOOL_LANGUAGE': 'en'}

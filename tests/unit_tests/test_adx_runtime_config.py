"""AgentServer binds node connectivity without migrating stored tool settings."""
import copy

import pytest

from jiuwenswarm.extensions.adx.runtime_config import bind_adx_runtime


def test_runtime_keeps_static_workspace_settings_and_rebinds_only_connection():
    sandbox = {'type': 'adx-environment', 'runtime': 'conch', 'image': 'custom-template',
               'cpu': 750, 'idle_timeout': 31, 'mounts': [{'host_path': '/shared', 'target': '/data'}],
               'url': 'https://previous-node:8443',
               'connection': {'server_address': 'previous-node:8443', 'token': 'old-secret',
                              'verify_tls': False, 'timeout': 19}}
    before = copy.deepcopy(sandbox)
    actual = bind_adx_runtime(sandbox, 'https://current-node:8443', '/run/agentos/node.json')
    assert sandbox == before
    assert actual['runtime'] == 'conch'
    assert actual['image'] == 'custom-template'
    assert actual['cpu'] == 750
    assert actual['mounts'] == sandbox['mounts']
    assert actual['url'] == 'https://current-node:8443'
    assert actual['endpoint_file'] == '/run/agentos/node.json'
    assert actual['connection'] == {'timeout': 19, 'verify_tls': True, 'use_tls': True}


@pytest.mark.parametrize('sandbox', [{}, {'type': 'yuanrong', 'executor': 'docker'},
                                     {'type': 'jiuwenbox-conch', 'template_name': 'old'}])
def test_runtime_requires_an_already_migrated_workspace(sandbox):
    with pytest.raises(ValueError, match='deployment migration'):
        bind_adx_runtime(sandbox, 'http://10.88.4.1:8080')


def test_runtime_protocol_follows_current_node_and_does_not_resolve_images():
    actual = bind_adx_runtime({'type': 'adx-environment', 'runtime': 'runsc',
                              'image': 'newly-configured-image', 'rootfs_template': 'prepared-rootfs'},
                             'http://10.88.4.1:8080')
    assert actual['image'] == 'newly-configured-image'
    assert actual['rootfs_template'] == 'prepared-rootfs'
    assert actual['connection'] == {'verify_tls': False, 'use_tls': False}

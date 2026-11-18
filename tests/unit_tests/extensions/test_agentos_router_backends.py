"""One AgentOS Router extension selects the configured runtime backend."""
from pathlib import Path
from types import SimpleNamespace

import pytest

from jiuwenswarm.extensions.agentos.agentos_router import extension
from jiuwenswarm.extensions.agentos.agentos_router.adx.router_client import AdxRouterClient
from jiuwenswarm.extensions.agentos.agentos_router.router_client import AgentOSRouterClient
from jiuwenswarm.extensions.loader import ExtensionLoader


@pytest.mark.asyncio
async def test_loader_registers_one_canonical_router_for_adx_backend(monkeypatch):
    monkeypatch.setenv('ROUTER_BACKEND_TEST_TOKEN', 'test-only-token')
    settings = {'gateway': {'agent_client': {'type': 'agentos_router'}, 'adx': {
        'ingress_url': 'https://node.example', 'tenant': 'test', 'token_env': 'ROUTER_BACKEND_TEST_TOKEN',
    }}}
    monkeypatch.setattr(extension, 'get_config', lambda: settings)
    clients = []; third = []
    registry = SimpleNamespace(config={}, register_agent_server_client=clients.append,
                               register_third_agent=third.append)
    loader = ExtensionLoader(registry)
    package = Path(extension.__file__).parents[2]
    loader.add_search_path(package)
    roots = loader.discover_extension_roots()
    assert package / 'adx' not in roots
    loaded = await loader.load_extension(package / 'agentos')
    try:
        assert len(loaded) == 1 and loaded == clients == third
        assert loaded[0].metadata.id == 'agentos_router'
        assert isinstance(loaded[0].get_client(), AdxRouterClient)
    finally:
        await loaded[0].shutdown()


@pytest.mark.asyncio
async def test_router_requires_adx_settings_instead_of_selecting_a_retired_backend(monkeypatch):
    monkeypatch.setattr(extension, 'get_config', lambda: {'gateway': {'agent_client': {
        'type': 'agentos_router', 'frontend_endpoint': 'http://retired.example:22767',
        'function_version_urn': 'test-function:latest',
    }}})
    registered = []
    with pytest.raises(ValueError, match="ingress_url"):
        await extension.register_extensions(SimpleNamespace(
            register_agent_server_client=registered.append, register_third_agent=registered.append))
    assert registered == []


def test_retained_compact_runtime_overlay_keeps_current_node_binding(monkeypatch):
    from jiuwenswarm.extensions.agentos.agentos_router.adx.runtime_config import bind_runtime_sandbox
    monkeypatch.setenv('JIUWENSWARM_ADX_NODE_INGRESS_URL', 'https://current-node.example')
    monkeypatch.delenv('JIUWENSWARM_ADX_NODE_CONTEXT_FILE', raising=False)
    retained = {'type': 'adx-environment', 'runtime': 'conch', 'image': 'existing-tools',
                'url': 'https://old-node.example'}
    bound = bind_runtime_sandbox(retained, {'type': 'adx-environment'})
    assert bound['url'] == 'https://current-node.example'
    assert bound['image'] == retained['image'] and bound['runtime'] == retained['runtime']
    assert retained['url'] == 'https://old-node.example'


@pytest.mark.asyncio
async def test_selected_adx_cannot_fall_back_when_credentials_are_missing(monkeypatch):
    monkeypatch.delenv('MISSING_ROUTER_CREDENTIAL', raising=False)
    monkeypatch.setattr(extension, 'get_config', lambda: {'gateway': {
        'agent_client': {'type': 'agentos_router'},
        'adx': {'ingress_url': 'https://node.example', 'tenant': 'test',
                'token_env': 'MISSING_ROUTER_CREDENTIAL'},
    }})
    calls = []
    with pytest.raises(ValueError):
        await extension.register_extensions(SimpleNamespace(register_agent_server_client=calls.append))
    assert calls == []


@pytest.mark.asyncio
async def test_removed_router_type_does_not_register_an_extension(monkeypatch):
    from jiuwenswarm.extensions.agentos.agentos_router.config import agentos_router_selected

    settings = {'gateway': {'agent_client': {'type': 'adx_router'}, 'adx': {}}}
    assert not agentos_router_selected(settings)
    monkeypatch.setattr(extension, 'get_config', lambda: settings)
    registered = []
    registry = SimpleNamespace(register_agent_server_client=registered.append,
                               register_third_agent=registered.append)
    assert await extension.register_extensions(registry) == []
    assert registered == []

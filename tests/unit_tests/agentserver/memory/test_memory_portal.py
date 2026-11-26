# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

import asyncio
import copy

import pytest

from jiuwenswarm.agents.harness.common.memory.jiuwen_rail_builder import memory_portal_configuration
from jiuwenswarm.agents.harness.common.memory.portal_registry import ProviderRegistry
from jiuwenswarm.common.config_panel.memory_config import flatten_memory_config, parse_memory_updates
from jiuwenswarm.server.runtime.memory_portal import MemoryPortalService


class Provider:
    def __init__(self, *, fail=False):
        self.is_initialized = False
        self.closed = 0
        self.links = 0
        self.fail = fail
        self.identity = None

    async def initialize(self, **identity):
        await asyncio.sleep(0)
        self.identity = identity
        if self.fail:
            raise RuntimeError('initialization failed')
        self.is_initialized = True

    async def shutdown(self):
        self.is_initialized = False
        self.closed += 1

    async def open_portal(self):
        self.links += 1
        return f'http://127.0.0.1:12345/portal/#access=one-use-{self.links}'


@pytest.fixture
def config(tmp_path, monkeypatch):
    monkeypatch.setattr('jiuwenswarm.common.utils.get_user_workspace_dir', lambda: tmp_path)
    monkeypatch.setattr(
        'jiuwenswarm.agents.harness.common.memory.jiuwen_rail_builder._inject_sdk_creds_and_root',
        lambda sdk, _: sdk,
    )
    return {
        'react': {'agent_name': 'runtime-agent-name'},
        'memory': {'mode': 'local', 'engine': 'builtin', 'builtin': {
            'provider': 'jiuwen', 'jiuwen': {
                'mode': 'sdk', 'scope_id': 'org', 'user_id': 'alice', 'portal': {'enabled': True},
            },
        }},
    }


@pytest.mark.asyncio
async def test_default_agent_preferred_then_binding_is_stable(config):
    registry = ProviderRegistry()
    service = MemoryPortalService(registry, factory=lambda *_: pytest.fail('must borrow'))
    key = memory_portal_configuration(config)
    providers = [Provider() for _ in range(3)]
    for provider, agent in zip(providers, ['other', 'main_agent', 'main_agent']):
        await provider.initialize()
        registry.register(provider, key, agent)
    result = await service.open(config)
    assert result['agent'] == 'main_agent'
    assert [provider.links for provider in providers] == [0, 1, 0]
    await service.open(config)
    assert providers[1].links == 2
    await service.close()
    assert [provider.closed for provider in providers] == [0, 0, 0]


@pytest.mark.asyncio
async def test_other_agent_is_not_selected_when_no_matching_provider_exists(config):
    registry = ProviderRegistry()
    other, owned = Provider(), Provider()
    await other.initialize()
    registry.register(other, memory_portal_configuration(config), 'team-expert')
    service = MemoryPortalService(registry, lambda *_: owned)
    try:
        result = await service.open(config)
        assert result['agent'] == 'main_agent'
        assert other.links == 0
        assert owned.identity['agent_id'] == 'main_agent'
    finally:
        await service.close()
    assert other.closed == 0
    assert owned.closed == 1


@pytest.mark.asyncio
async def test_concurrent_opens_initialize_once_and_close_on_config_change(config):
    made = []
    def factory(*_):
        provider = Provider()
        made.append(provider)
        return provider
    service = MemoryPortalService(ProviderRegistry(), factory)
    results = await asyncio.gather(*(service.open(config) for _ in range(12)))
    assert len(made) == 1
    assert len({result['url'] for result in results}) == 12
    assert made[0].identity == {
        'user_id': 'alice', 'scope_id': 'org', 'agent_id': 'main_agent',
        'session_id': '', 'space': '',
    }
    changed = copy.deepcopy(config)
    changed['memory']['builtin']['jiuwen']['space'] = 'other-space'
    await service.configure(changed)
    assert made[0].closed == 1
    await service.open(changed)
    assert len(made) == 2
    await service.close()
    assert made[1].closed == 1


@pytest.mark.asyncio
async def test_failed_initialization_cleans_resources_and_can_retry(config):
    failed, success = Provider(fail=True), Provider()
    candidates = iter([failed, success])
    service = MemoryPortalService(ProviderRegistry(), lambda *_: next(candidates))
    assert (await service.open(config))['status'] == 'unavailable'
    assert failed.closed == 1
    assert (await service.open(config))['status'] == 'ready'
    await service.close()


@pytest.mark.asyncio
async def test_transient_failure_does_not_switch_agent(config):
    class Unavailable(Provider):
        async def open_portal(self):
            raise TimeoutError()
    first, second = Unavailable(), Provider()
    registry = ProviderRegistry()
    key = memory_portal_configuration(config)
    for provider, agent in [(first, 'main_agent'), (second, 'other')]:
        await provider.initialize()
        registry.register(provider, key, agent)
    service = MemoryPortalService(registry)
    for _ in range(2):
        result = await service.open(config)
        assert result['code'] == 'timeout'
        assert result['agent'] == 'main_agent'
    assert second.links == 0


@pytest.mark.asyncio
async def test_wrong_identity_not_borrowed_and_toggle_reuses_owned(config):
    registry = ProviderRegistry()
    stranger, owned = Provider(), Provider()
    await stranger.initialize()
    other = copy.deepcopy(config)
    other['memory']['builtin']['jiuwen']['user_id'] = 'bob'
    registry.register(stranger, memory_portal_configuration(other), 'main_agent')
    service = MemoryPortalService(registry, lambda *_: owned)
    try:
        opened = await service.open(config)
        assert stranger.links == 0
        off = copy.deepcopy(config)
        off['memory']['builtin']['jiuwen']['portal'] = {'enabled': False}
        for _ in range(3):
            links = owned.links
            await service.configure(off)
            assert (await service.open(off))['status'] == 'disabled'
            assert owned.links == links
            assert owned.closed == 0
            await service.configure(config)
            reopened = await service.open(config)
            assert reopened['status'] == 'ready'
            assert reopened['url'] != opened['url']
            assert owned.closed == 0
            opened = reopened
        off['memory']['builtin']['jiuwen']['space'] = 'other-space'
        await service.configure(off)
        assert owned.closed == 1
    finally:
        await service.close()
        await stranger.shutdown()
    assert owned.closed == 1
    assert stranger.links == 0


@pytest.mark.parametrize('value', [None, '', 'yes', 0, 1, [], {}])
def test_invalid_portal_switch_rejected(value):
    with pytest.raises(ValueError):
        parse_memory_updates({'memory_portal_enabled': value})


@pytest.mark.asyncio
async def test_missing_portal_switch_defaults_to_disabled(config):
    config['memory']['builtin']['jiuwen'].pop('portal')
    assert flatten_memory_config({}) == {'memory_portal_enabled': 'false'}
    assert flatten_memory_config(config) == {'memory_portal_enabled': 'false'}
    service = MemoryPortalService(ProviderRegistry(), factory=lambda *_: pytest.fail('must not create'))
    try:
        assert (await service.open(config))['status'] == 'disabled'
    finally:
        await service.close()


def test_settings_expose_only_portal_switch(config):
    config['memory']['builtin']['jiuwen']['api_key'] = 'do-not-return'
    assert flatten_memory_config(config) == {'memory_portal_enabled': 'true'}
    assert parse_memory_updates({'memory_portal_enabled': 'false'}) == {'memory_portal_enabled': False}
    assert parse_memory_updates({'memory_portal_enabled': True}) == {'memory_portal_enabled': True}


def test_config_save_preserves_connection_and_mode_options(tmp_path, monkeypatch):
    from jiuwenswarm.common import config as config_module
    from jiuwenswarm.common.config_panel import config_set_handlers as handlers

    path = tmp_path / 'config.yaml'
    path.write_text('memory:\n  builtin:\n    provider: jiuwen\n    jiuwen:\n      mode: sdk\n      sdk:\n        globals:\n          untouched: true\n')
    monkeypatch.setattr(config_module, 'CONFIG_YAML_PATH', path)
    monkeypatch.setattr(handlers, 'get_config_raw', lambda: config_module.load_yaml_round_trip(path))
    original = copy.deepcopy(config_module.load_yaml_round_trip(path)['memory']['builtin'])
    for enabled in ('false', 'true'):
        changed = handlers.apply_config_payload({'memory_portal_enabled': enabled})
        assert set(changed.yaml_updated) == {'memory_portal_enabled'}
        saved = config_module.load_yaml_round_trip(path)
        builtin = copy.deepcopy(saved['memory']['builtin'])
        assert builtin['jiuwen'].pop('portal')['enabled'] is (enabled == 'true')
        assert builtin == original
        assert 'enabled' not in saved['memory']
        changes = handlers.ConfigChangeSet(changed.env_updates, changed.yaml_updated)
        assert changes.reload_options['reload_scopes'] == ['web_ui']
        assert flatten_memory_config(saved) == {'memory_portal_enabled': enabled}


@pytest.mark.asyncio
async def test_portal_rpc_uses_server_identity_not_request_parameters(config, monkeypatch):
    from jiuwenswarm.common.schema.agent import AgentRequest
    from jiuwenswarm.common.schema.message import ReqMethod
    from jiuwenswarm.server.runtime.gateway_adapter.config_adapter import ConfigAdapter

    provider = Provider()
    service = MemoryPortalService(ProviderRegistry(), lambda *_: provider)
    monkeypatch.setattr('jiuwenswarm.common.config.get_config', lambda: config)
    monkeypatch.setattr('jiuwenswarm.server.runtime.memory_portal.get_memory_portal_service', lambda: service)
    request = AgentRequest(
        request_id='portal-open', channel_id='web', session_id='untrusted-session',
        user_id='untrusted-user', req_method=ReqMethod.MEMORY_PORTAL_OPEN,
        params={'user_id': 'bob', 'agent_id': 'another-agent'},
    )
    try:
        response = await ConfigAdapter().handle(request)
        assert response.ok and response.payload['status'] == 'ready'
        assert provider.identity['user_id'] == 'alice'
        assert provider.identity['agent_id'] == 'main_agent'
        assert provider.identity['session_id'] == ''
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_portal_save_does_not_reload_agents_or_stop_teams(config, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from jiuwenswarm.server.runtime.gateway_adapter.config_adapter import reload_runtime_after_config_save

    service = SimpleNamespace(configure=AsyncMock())
    manager = SimpleNamespace(reload_agents_config=AsyncMock())
    monkeypatch.setattr('jiuwenswarm.common.config.get_config', lambda: config)
    monkeypatch.setattr('jiuwenswarm.server.runtime.memory_portal.get_memory_portal_service', lambda: service)
    stop_paused = AsyncMock(return_value=0)
    monkeypatch.setattr(
        'jiuwenswarm.agents.harness.team.stop_all_paused_team_session_runtimes_across_managers',
        stop_paused,
    )
    await reload_runtime_after_config_save(
        manager, {'memory_portal_enabled'}, config_payload={'modes': {}},
        reload_options={'reload_scopes': ['web_ui'], 'target_channel_id': 'web'},
    )
    service.configure.assert_awaited_once_with(config)
    stop_paused.assert_not_awaited()
    manager.reload_agents_config.assert_not_awaited()
    await reload_runtime_after_config_save(
        manager, {'memory_portal_enabled', 'models.defaults'}, config_payload=config,
        reload_options={'reload_scopes': ['web_ui', 'model'], 'target_channel_id': 'web'},
    )
    manager.reload_agents_config.assert_awaited_once()
    assert manager.reload_agents_config.await_args.kwargs['target_channel_id'] == 'web'
    stop_paused.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('entry', ['save', 'rpc'])
@pytest.mark.parametrize('reload_fails', [False, True])
async def test_portal_failure_does_not_change_agent_reload_result(config, monkeypatch, caplog, entry, reload_fails):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from jiuwenswarm.server.handlers.ops import handle_agent_reload_config
    from jiuwenswarm.server.runtime.gateway_adapter.config_adapter import reload_runtime_after_config_save

    service = SimpleNamespace(configure=AsyncMock(side_effect=RuntimeError('portal-fake-secret')))
    reload = AsyncMock(side_effect=RuntimeError('agent reload failed') if reload_fails else None)
    manager = SimpleNamespace(reload_agents_config=reload)
    monkeypatch.setattr('jiuwenswarm.server.runtime.memory_portal.get_memory_portal_service', lambda: service)
    monkeypatch.setattr(
        'jiuwenswarm.agents.harness.team.stop_all_paused_team_session_runtimes_across_managers',
        AsyncMock(return_value=0),
    )
    if entry == 'save':
        operation = reload_runtime_after_config_save(
            manager, {'permissions_enabled'}, config_payload=config,
            reload_options={'reload_scopes': ['permissions']},
        )
        if reload_fails:
            with pytest.raises(RuntimeError, match='agent reload failed'):
                await operation
        else:
            assert await operation is True
    else:
        sink = SimpleNamespace(send_response=AsyncMock())
        ctx = SimpleNamespace(
            request=SimpleNamespace(request_id='reload-test', channel_id='web', params={
                'config': config, 'reload_scopes': ['permissions'],
            }),
            services=SimpleNamespace(agent_manager=manager), sink=sink,
        )
        await handle_agent_reload_config(ctx)
        assert sink.send_response.await_args.args[0].ok is not reload_fails
    service.configure.assert_awaited_once_with(config)
    reload.assert_awaited_once()
    assert 'Memory Portal configuration update failed (RuntimeError)' in caplog.text
    assert 'portal-fake-secret' not in caplog.text


@pytest.mark.asyncio
async def test_borrowed_provider_is_called_on_its_initialization_loop(config):
    from threading import Thread

    loop = asyncio.new_event_loop()
    thread = Thread(target=loop.run_forever, daemon=True)
    thread.start()
    registry = ProviderRegistry()

    class LoopProvider(Provider):
        async def open_portal(self):
            assert asyncio.get_running_loop() is loop
            return await super().open_portal()

    provider = LoopProvider()

    async def initialize():
        await provider.initialize()
        registry.register(provider, memory_portal_configuration(config), 'main_agent')

    service = MemoryPortalService(registry, lambda *_: pytest.fail('must borrow'))
    try:
        await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(initialize(), loop))
        assert (await service.open(config))['status'] == 'ready'
        await service.close()
        assert provider.closed == 0
        await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(provider.shutdown(), loop))
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=2)
        loop.close()


@pytest.mark.asyncio
async def test_shutdown_failure_does_not_prevent_other_provider_cleanup(config, monkeypatch):
    from jiuwenswarm.server.runtime import memory_portal

    class RetryShutdown(Provider):
        async def shutdown(self):
            if self.fail:
                self.fail = False
                raise RuntimeError('temporary close error')
            await super().shutdown()

    failed, good = RetryShutdown(), Provider()
    first = MemoryPortalService(ProviderRegistry(), lambda *_: failed)
    second = MemoryPortalService(ProviderRegistry(), lambda *_: good)
    await first.open(config)
    await second.open(config)
    failed.fail = True
    services = {'first': first, 'second': second}
    monkeypatch.setattr(memory_portal, '_services', services)
    await memory_portal.close_memory_portal_services()
    assert good.closed == 1
    assert list(services) == ['first']
    await memory_portal.close_memory_portal_services()
    assert failed.closed == 1
    assert not services


@pytest.mark.asyncio
async def test_portal_is_independent_of_single_agent_engine_and_memory_switches(config):
    config['memory'].update(engine='external', mode='cloud')
    config['modes'] = {'agent': {'memory': {'enabled': False}}, 'code': {'memory': {'enabled': False}}}
    provider = Provider()
    service = MemoryPortalService(ProviderRegistry(), lambda *_: provider)
    try:
        assert (await service.open(config))['status'] == 'ready'
        config['memory']['builtin']['jiuwen']['portal'] = {'enabled': False}
        assert (await service.open(config))['status'] == 'disabled'
        assert provider.closed == 0
    finally:
        await service.close()
    assert provider.closed == 1


def test_portal_preserves_configured_identity_instead_of_runtime_name(config):
    from jiuwenswarm.agents.harness.common.memory.jiuwen_rail_builder import memory_provider_identity

    config['memory']['builtin']['jiuwen'].update(agent_id='configured-agent', session_id='memory-session')
    assert memory_provider_identity(config, 'runtime-agent-name')['agent_id'] == 'configured-agent'
    assert memory_provider_identity(config, 'another-runtime')['session_id'] == 'memory-session'


def test_factory_reuses_connection_holder_without_per_session_portal_ids(config, monkeypatch):
    from types import SimpleNamespace
    adapter = pytest.importorskip('jiuwen_memory_adapter.jiuwenswarm.agent_memory_provider')
    from jiuwenswarm.agents.harness.common.memory import jiuwen_rail_builder as builder

    class CaptureProvider:
        def __init__(self, **options):
            self.options = options

        def is_available(self):
            return True

    holder = SimpleNamespace()
    monkeypatch.setattr(adapter, 'AgentMemoryMemoryProvider', CaptureProvider)
    monkeypatch.setattr(builder, 'memory_connection_options', lambda _: ({'document_runtime_holder': holder}, None))
    first = builder.build_jiuwen_memory_provider(config, 'runtime-a')
    second = builder.build_jiuwen_memory_provider(config, 'runtime-b')
    assert first.options['document_runtime_holder'] is second.options['document_runtime_holder'] is holder
    assert first.options['agent_id'] == second.options['agent_id'] == 'main_agent'
    assert 'instance_id' not in first.options['portal']
    assert 'port' not in first.options['portal']


@pytest.mark.parametrize('scene', ['agent', 'code', 'team'])
def test_portal_switch_does_not_block_rail_construction(config, scene, monkeypatch):
    from types import SimpleNamespace
    from jiuwenswarm.agents.harness.common.memory import jiuwen_rail_builder as builder

    config['memory']['enabled'] = False
    config['memory']['builtin']['jiuwen']['portal'] = {'enabled': False}
    provider = SimpleNamespace(is_available=lambda: True)
    rail = object()
    spec = SimpleNamespace(provider_options={}, rail_options={}, rail_factory=lambda *args, **kwargs: rail)
    monkeypatch.setitem(builder.SCENE_FACTORIES, scene, lambda *_: spec)
    monkeypatch.setattr(builder, 'memory_connection_options', lambda *args, **kwargs: ({}, None))
    monkeypatch.setattr(builder, '_instantiate_memory_provider', lambda *args, **kwargs: provider)
    assert builder.build_jiuwen_memory_rail(config, scene=scene) is rail
    assert builder.build_jiuwen_memory_provider(config) is provider


@pytest.mark.parametrize('scene', ['agent', 'code', 'team'])
@pytest.mark.asyncio
async def test_existing_provider_keeps_memory_operations_when_portal_is_disabled(config, scene, monkeypatch):
    from unittest.mock import AsyncMock
    from jiuwenswarm.agents.harness.common.memory import jiuwen_rail_builder as builder
    adapter = pytest.importorskip('jiuwen_memory_adapter.jiuwenswarm.agent_memory_provider')

    class Base:
        def __init__(self, **kwargs):
            pass
        prefetch = AsyncMock(return_value='stored fact')
        sync_turn = AsyncMock(return_value={'saved': True})
        handle_tool_call = AsyncMock(return_value='{"success": true}')
        on_session_end = AsyncMock()
        open_portal = AsyncMock(return_value='http://localhost:1234/portal/')
        def get_tool_schemas(self):
            return [{'name': 'read'}]

    monkeypatch.setattr(adapter, 'AgentMemoryMemoryProvider', Base)
    monkeypatch.setattr('jiuwenswarm.common.config.get_config', lambda: config)
    provider = builder._instantiate_memory_provider(
        config, options={}, markdown_root=None, scene=scene, provider_options={}, agent_id='main_agent',
    )
    fingerprint = builder.jiuwen_builtin_config_fingerprint(config)
    config['memory']['enabled'] = False
    config['memory']['builtin']['jiuwen']['portal'] = {'enabled': False}
    assert builder.jiuwen_builtin_config_fingerprint(config) == fingerprint
    assert provider.get_tool_schemas() == [{'name': 'read'}]
    assert await provider.prefetch('query') == 'stored fact'
    assert await provider.sync_turn('user', 'answer') == {'saved': True}
    assert await provider.handle_tool_call('read', {}) == '{"success": true}'
    await provider.on_session_end([])
    assert await provider.open_portal() is None
    for method in (Base.prefetch, Base.sync_turn, Base.handle_tool_call, Base.on_session_end):
        method.assert_awaited_once()
    Base.open_portal.assert_not_awaited()
    config['memory']['builtin']['jiuwen']['portal']['enabled'] = True
    assert await provider.open_portal() == 'http://localhost:1234/portal/'
    Base.open_portal.assert_awaited_once()


def test_portal_switch_preserves_memory_mode_and_engine_gates(config, monkeypatch):
    from types import SimpleNamespace
    from jiuwenswarm.agents.harness.common.memory.config import is_memory_enabled
    from jiuwenswarm.agents.harness.common.memory.external_memory_config import (
        is_builtin_memory_allowed, is_external_memory_allowed,
    )
    from jiuwenswarm.common.team_memory_binding import uses_jiuwen_team_memory
    from jiuwenswarm.agents.swarm.providers import team_memory

    config['memory']['enabled'] = False
    config['memory']['builtin']['jiuwen']['portal'] = {'enabled': False}
    config['memory']['engine'] = 'both'
    config['modes'] = {'agent': {'memory': {'enabled': True}}, 'code': {'memory': {'enabled': True}}}
    for mode in ('agent.work.normal', 'agent.code.normal'):
        assert is_memory_enabled(mode, config)
    assert is_builtin_memory_allowed(config)
    assert is_external_memory_allowed(config)
    rail = object()
    monkeypatch.setattr(team_memory, 'build_jiuwen_memory_rail', lambda *args, **kwargs: rail)
    for mode in ('team.work.normal', 'team.code.normal'):
        ctx = SimpleNamespace(config=config, mode=mode, memory_team='team-key', extras={},
                              memory_project='project', member_name='leader', memory_group='',
                              memory_experts={}, role='leader')
        assert uses_jiuwen_team_memory(config, mode)
        assert team_memory.build_team_memory({}, ctx)[-1] is rail
    config['modes']['code']['memory']['enabled'] = False
    assert not is_memory_enabled('agent.code.normal', config)
    assert is_memory_enabled('agent.work.normal', config)


def test_frame_origin_option_is_no_longer_forwarded(config):
    from jiuwenswarm.agents.harness.common.memory.jiuwen_rail_builder import memory_portal_options
    config['memory']['builtin']['jiuwen']['portal'] = {'enabled': True, 'allowed_frame_origins': ['https://old.example']}
    assert memory_portal_options(config) == {'enabled': True}


@pytest.mark.asyncio
@pytest.mark.parametrize('blocked_stage', ['lock', 'configuration'])
async def test_open_timeout_includes_lock_and_configuration(config, monkeypatch, blocked_stage):
    provider = Provider()
    service = MemoryPortalService(ProviderRegistry(), lambda *_: provider)
    unblock = asyncio.Event()
    if blocked_stage == 'lock':
        await service._lock.acquire()
    else:
        assert (await service.open(config))['status'] == 'ready'
        original_shutdown = provider.shutdown

        async def blocked_shutdown():
            await unblock.wait()
            await original_shutdown()

        monkeypatch.setattr(provider, 'shutdown', blocked_shutdown)
        config['memory']['builtin']['jiuwen']['space'] = 'changed'
    original_timeout = asyncio.timeout
    monkeypatch.setattr(asyncio, 'timeout', lambda _: original_timeout(0.02))
    try:
        result = await asyncio.wait_for(service.open(config), timeout=1)
        assert result['status'] == 'unavailable'
        assert result['code'] == 'timeout'
    finally:
        unblock.set()
        if blocked_stage == 'lock':
            service._lock.release()
        monkeypatch.setattr(asyncio, 'timeout', original_timeout)
        await service.close()
    assert (await service.open(config))['status'] == 'ready'
    await service.close()

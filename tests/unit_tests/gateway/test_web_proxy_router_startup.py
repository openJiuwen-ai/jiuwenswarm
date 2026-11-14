# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Exercise the Gateway's nested web-proxy startup callback in isolation."""
import ast
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.extensions.agentos.registry_watcher import service
from jiuwenswarm.gateway.channel_manager.protocol.web_proxy.web_proxy_connect import WebProxyChannelConfig
from jiuwenswarm.gateway.cron import factory


def _startup_callback(namespace):
    source = Path(__file__).resolve().parents[3] / 'jiuwenswarm/gateway/app_gateway.py'
    tree = ast.parse(source.read_text())
    # The production callback is a closure in _run. Execute its whole web-proxy
    # branch, including router selection and registry wiring, without booting IMs.
    branch = next(node for node in ast.walk(tree) if isinstance(node, ast.If)
                  and isinstance(node.test, ast.Compare)
                  and isinstance(node.test.left, ast.Constant) and node.test.left.value == 'web_proxy'
                  and isinstance(node.test.comparators[0], ast.Name)
                  and node.test.comparators[0].id == 'changed_channels')
    function = ast.AsyncFunctionDef(name='apply', args=ast.arguments(posonlyargs=[], args=[],
        kwonlyargs=[], kw_defaults=[], defaults=[]), body=[branch], decorator_list=[])
    module = ast.fix_missing_locations(ast.Module(body=[function], type_ignores=[]))
    exec(compile(module, str(source), 'exec'), namespace)
    return namespace['apply']


@pytest.mark.asyncio
@pytest.mark.parametrize('router,enabled,resolver,starts', [
    ('adx_router', True, True, True),
    ('agentos_router', True, True, True),
    ('websocket', True, True, False),
    ('adx_router', False, True, False),
    ('adx_router', True, False, False),
])
async def test_web_proxy_starts_registry_driven_listeners(monkeypatch, router, enabled, resolver, starts):
    managers = []
    watchers = []
    previous_watcher = SimpleNamespace(stop=AsyncMock())
    previous_manager = SimpleNamespace(close_all=AsyncMock())
    channel = SimpleNamespace(web_registry_watcher=previous_watcher, web_port_manager=previous_manager)
    extension = SimpleNamespace(resolve_web_endpoint=AsyncMock() if resolver else None,
                                release_web_endpoint=AsyncMock(), acquire_web_endpoint=AsyncMock())

    class Manager:
        def __init__(self, web_channel, config):
            assert web_channel is channel
            self.started = False
            self.desired = {}
            managers.append(self)

        def start(self):
            self.started = True

        async def reconcile(self, desired):
            self.desired = desired

    class Watcher:
        def __init__(self, *, etcd_endpoints, on_desired):
            assert etcd_endpoints == ['http://etcd.test:2379']
            self.on_desired = on_desired
            self.enabled = True
            self.started = False
            watchers.append(self)

        async def start(self):
            self.started = True
            await self.on_desired({'registered-agent': 19107})

    monkeypatch.setattr(service, 'RegistryWatcherService', Watcher)
    monkeypatch.setattr(factory, 'load_cron_store_settings',
                        lambda config: SimpleNamespace(endpoints=['http://etcd.test:2379']))
    callback = _startup_callback({
        'changed_channels': ['web_proxy'], 'conf': {'web_proxy': {'enabled': enabled}},
        'web_channel': channel, 'agent_server_ext': extension,
        '_is_channel_enabled': lambda config, fields: (config['enabled'], 'enabled=false'),
        'get_config': lambda: {'gateway': {'agent_client': {'type': router}}},
        'WebProxyChannelConfig': WebProxyChannelConfig, 'WebPortManager': Manager,
        'logger': logging.getLogger(__name__),
    })
    await callback()
    previous_watcher.stop.assert_awaited_once()
    previous_manager.close_all.assert_awaited_once()
    assert channel.web_proxy_enabled is starts
    assert len(managers) == len(watchers) == int(starts)
    if starts:
        assert managers[0].started and watchers[0].started
        assert managers[0].desired == {'registered-agent': 19107}
        assert channel.web_registry_watcher is watchers[0]
        await watchers[0].on_desired({})
        assert managers[0].desired == {}

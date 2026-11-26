# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Real adapter, Markdown/shadow storage and authenticated browser HTTP calls."""

import asyncio
import json
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit
from urllib.request import ProxyHandler, Request, build_opener

import pytest

adapter = pytest.importorskip('jiuwen_memory_adapter.jiuwenswarm.agent_memory_provider')
if not hasattr(adapter.AgentMemoryMemoryProvider, 'open_portal'):
    pytest.skip('agent-memory with Portal support is required', allow_module_level=True)

from jiuwenswarm.agents.harness.common.memory.jiuwen_rail_builder import (
    build_jiuwen_memory_provider,
    memory_provider_identity,
)
from jiuwenswarm.server.runtime.memory_portal import MemoryPortalService


def document_config(root):
    config = adapter._agent_default_config()
    config['globals'].update(embedder_dim=64, watch_document=False)
    config['llm'] = {'default': 'echo'}
    config['extractor'] = {'default': 'keyword'}
    config['tokenizer'] = {'default': 'whitespace'}
    config['embedder'] = {'default': {'target': 'hashing', 'params': {'tokenizer': 'default'}}}
    config['fulltext_store'] = {'default': 'memory'}
    config['markdown_store'] = {'default': {'target': 'local', 'params': {'root': str(root)}}}
    config['shadow_index']['default']['params']['db_path'] = str(root / 'shadow.db')
    return config


@pytest.fixture
def config(tmp_path, monkeypatch):
    monkeypatch.setattr('jiuwenswarm.common.utils.get_user_workspace_dir', lambda: tmp_path)
    config = {
        'react': {'agent_name': 'portal-work'},
        'memory': {'mode': 'local', 'engine': 'builtin', 'builtin': {
            'provider': 'jiuwen', 'jiuwen': {
                'mode': 'sdk', 'scope_id': 'portal-org', 'user_id': 'portal-user', 'agent_id': 'portal-work',
                'sdk': document_config(tmp_path), 'portal': {'enabled': True},
            },
        }},
    }
    monkeypatch.setattr('jiuwenswarm.common.config.get_config', lambda: config)
    return config


class Browser:
    def __init__(self, url):
        parts = urlsplit(url)
        self.origin = f'{parts.scheme}://{parts.netloc}'
        self.token = parse_qs(parts.fragment)['access'][0]
        self.opener = build_opener(ProxyHandler({}))
        self.session = None
        self.session = self.post('bootstrap', {'token': self.token, 'embedded': True})['session_token']

    def post(self, action, body):
        headers = {'Origin': self.origin, 'Content-Type': 'application/json', 'X-Memory-Portal-Browser': '1'}
        if self.session:
            headers['X-Memory-Portal-Session'] = self.session
        prefix = '/portal/api/' if action == 'bootstrap' else '/v1/'
        request = Request(self.origin + prefix + action, json.dumps(body).encode(), headers=headers)
        try:
            with self.opener.open(request, timeout=10) as response:
                return json.load(response)
        except HTTPError as exc:
            exc.add_note(exc.read().decode())
            raise


@pytest.mark.asyncio
async def test_sdk_portal_shares_runtime_and_releasing_page_keeps_other_provider_alive(config):
    made = []
    def factory(*args):
        provider = build_jiuwen_memory_provider(*args)
        made.append(provider)
        return provider

    service = MemoryPortalService(factory=factory)
    sibling = None
    try:
        opened = await service.open(config)
        assert opened['status'] == 'ready', opened
        first = made[0]
        runtime = first.runtime
        sibling = build_jiuwen_memory_provider(config, 'different-runtime-name')
        await sibling.initialize(**memory_provider_identity(config))
        assert sibling.runtime is runtime
        assert sibling.bound_scope().agent == 'portal-work'
        link = await sibling.open_portal()
        assert urlsplit(link).port == urlsplit(opened['url']).port
        browser = await asyncio.to_thread(Browser, link)
        listing = await asyncio.to_thread(browser.post, 'read_config', {})
        assert len(listing['instances']) == 1
        await service.close()
        assert sibling.is_initialized
        assert sibling.runtime is runtime
        assert await sibling.open_portal()
    finally:
        await service.close()
        if sibling is not None:
            await sibling.shutdown()


@pytest.mark.asyncio
async def test_sdk_portal_toggle_keeps_listener_and_issues_fresh_links(config):
    service = MemoryPortalService()
    try:
        opened = await service.open(config)
        assert opened['status'] == 'ready', opened
        browser = await asyncio.to_thread(Browser, opened['url'])
        for _ in range(3):
            config['memory']['builtin']['jiuwen']['portal']['enabled'] = False
            await service.configure(config)
            assert (await service.open(config))['status'] == 'disabled'
            assert (await asyncio.to_thread(browser.post, 'read_config', {}))['instances']
            config['memory']['builtin']['jiuwen']['portal']['enabled'] = True
            await service.configure(config)
            reopened = await service.open(config)
            assert reopened['status'] == 'ready', reopened
            assert urlsplit(reopened['url']).netloc == urlsplit(opened['url']).netloc
            assert reopened['url'] != opened['url']
            browser = await asyncio.to_thread(Browser, reopened['url'])
            assert (await asyncio.to_thread(browser.post, 'read_config', {}))['instances']
            opened = reopened
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_http_portal_uses_remote_runtime_and_leaves_service_alive(config):
    from http.server import ThreadingHTTPServer
    from threading import Thread

    from jiuwen_memory.api import Scope, build_configured_security_runtime
    from jiuwen_memory.common.security.types import Role
    from jiuwen_memory_entry.core.profiles import load_config
    from jiuwen_memory_entry.http_server.__main__ import HttpServer

    security = build_configured_security_runtime({
        'security': {'default': {'target': 'standard', 'params': {
            'authenticator': {'target': 'api_key'},
        }}},
    })
    actor = Scope(org='portal-org', user='portal-user')
    key = security.authenticator.key_store.issue(actor, role=Role.USER)
    server = HttpServer.build_personal(
        load_config([{
            'memory_api': config['memory']['builtin']['jiuwen']['sdk'],
        }]),
        llm_instances=['default'],
        authorize=lambda context, operation: context.auth.actor == actor,
        security_runtime=security,
    )
    httpd = ThreadingHTTPServer(('127.0.0.1', 0), server.handler_cls())
    thread = Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base_url = f'http://127.0.0.1:{httpd.server_port}'
    jiuwen = config['memory']['builtin']['jiuwen']
    jiuwen.update(mode='http', base_url=base_url, api_key=key,
                  identity_org=actor.org, identity_user=actor.user)
    providers = []
    def factory(*args):
        provider = build_jiuwen_memory_provider(*args)
        providers.append(provider)
        return provider
    service = MemoryPortalService(factory=factory)
    try:
        opened = await service.open(config)
        assert opened['status'] == 'ready', opened
        assert key not in json.dumps(opened)
        assert providers[0].runtime is None
        browser = await asyncio.to_thread(Browser, opened['url'])
        added = await asyncio.to_thread(browser.post, 'add', {
            'content': 'remote portal acceptance', 'scope': {},
        })
        assert added[0]['scope']['agent'] == 'portal-work'
        await service.close()
        # Closing the Swarm client cannot stop the HTTP service or its browser session.
        page = await asyncio.to_thread(browser.post, 'list', {'extensions': {'personal': {}}})
        assert page['count'] == 1
        unauthenticated = Request(base_url + '/portal/api/launch', b'{}', headers={
            'Origin': base_url, 'Content-Type': 'application/json',
        })
        with pytest.raises(HTTPError) as denied:
            await asyncio.to_thread(build_opener(ProxyHandler({})).open, unauthenticated, timeout=5)
        assert denied.value.code == 401
    finally:
        await service.close()
        await asyncio.to_thread(httpd.shutdown)
        httpd.server_close()
        thread.join(timeout=2)
        server.close(wait=True)

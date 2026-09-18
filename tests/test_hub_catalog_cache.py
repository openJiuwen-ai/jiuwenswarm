import asyncio
import pytest
from jiuwenswarm.server.runtime.marketplace.hub_catalog_cache import HubCatalogCache

@pytest.mark.asyncio
async def test_cold_progress_dedup_and_failure_keeps_old(tmp_path):
    cache = HubCatalogCache(tmp_path / 'catalog.sqlite', ttl=0)
    gate = asyncio.Event()
    calls = []
    async def loader():
        calls.append(1)
        yield {'items': [{'asset_id': 'one'}], 'complete': False, 'has_more': True}
        await gate.wait()
        yield {'items': [{'asset_id': 'one'}, {'asset_id': 'two'}], 'complete': True, 'has_more': False}
    data, state = cache.read('public-key', loader)
    assert data is None and state['state'] == 'miss' and state['refreshing']
    await asyncio.sleep(.01)
    data, state = cache.read('public-key', loader)
    assert len(data['items']) == 1 and not state['complete'] and len(calls) == 1
    gate.set()
    await asyncio.sleep(.01)
    async def broken():
        yield {'items': [{'asset_id': 'new'}], 'complete': False, 'has_more': True}
        raise RuntimeError('secret error must not leak')
    data, state = cache.read('public-key', broken)
    assert len(data['items']) == 2
    await asyncio.sleep(.01)
    data, state = cache.read('public-key', broken)
    assert len(data['items']) == 2 and state['state'] == 'stale' and state['error'] == 'refresh_failed'
    await cache.close()

@pytest.mark.asyncio
async def test_disk_public_only_and_safe_metadata(tmp_path):
    path = tmp_path / 'catalog.sqlite'
    cache = HubCatalogCache(path)
    async def loader():
        yield {'items': [{'asset_id': 'ok', 'token': 'secret', 'connection_state': 'connected', 'icon': 'https://x/a?signature=secret', 'credentials': {'key': 'secret'}}], 'complete': True}
    cache.read('public', loader)
    cache.read('private', loader, public=False)
    await asyncio.sleep(.02)
    await cache.close()
    cache = HubCatalogCache(path)
    data, state = cache.read('public', loader)
    assert data['items'] == [{'asset_id': 'ok'}]
    data, state = cache.read('private', loader, public=False)
    assert data is None
    await cache.close()

@pytest.mark.asyncio
async def test_corrupt_disk_and_valid_empty(tmp_path):
    path = tmp_path / 'catalog.sqlite'
    path.write_bytes(b'bad sqlite')
    cache = HubCatalogCache(path)
    async def empty():
        yield {'items': [], 'complete': True, 'has_more': False}
    cache.read('empty', empty)
    await asyncio.sleep(.02)
    data, state = cache.read('empty', empty)
    assert data['items'] == [] and state['state'] == 'fresh' and state['complete']
    await cache.close()

@pytest.mark.asyncio
async def test_asset_preload_extends_all_pages_and_deduplicates(tmp_path, monkeypatch):
    import jiuwenswarm.server.runtime.marketplace.hub_catalog_cache as module
    from jiuwenswarm.server.runtime.marketplace.hub_asset_port import HubAssetSummary, HubSearchPage
    cache = HubCatalogCache(tmp_path / 'cache.sqlite')
    monkeypatch.setattr(module, 'get_hub_catalog_cache', lambda: cache)
    class Port:
        base_url = 'https://public.test'
        calls = []
        async def search_assets(self, request):
            self.calls.append(request.page)
            item = HubAssetSummary('mcp', str(request.page), 'name', 'desc', '1', '', (), str(request.page))
            return HubSearchPage((item,), 3, request.page, 1)
    port = Port()
    items, state = await module.cached_asset_catalog(port, 'mcp', preload=True)
    assert not items and state['refreshing']
    await asyncio.sleep(.01)
    assert port.calls == [1, 2]
    items, state = await module.cached_asset_catalog(port, 'mcp')
    assert len(items) == 2 and state['has_more'] and state['refreshing']
    await asyncio.sleep(.01)
    items, state = await module.cached_asset_catalog(port, 'mcp')
    assert len(items) == 3 and state['complete'] and port.calls == [1, 2, 1, 2, 3]
    await cache.close()

@pytest.mark.asyncio
async def test_recommend_cache_real_entry_and_session_neutral_prewarm(tmp_path, monkeypatch):
    import jiuwenswarm.server.runtime.marketplace.hub_catalog_cache as module
    from jiuwenswarm.server.runtime.skill.skill_manager import SkillManager
    cache = HubCatalogCache(tmp_path / 'cache.sqlite')
    monkeypatch.setattr(module, 'get_hub_catalog_cache', lambda: cache)
    manager = SkillManager(workspace_dir=str(tmp_path))
    monkeypatch.setattr(manager, '_resolve_teamskills_hub_auth_with_env', lambda params: {})
    calls = []
    async def post(path, **kwargs):
        calls.append(path)
        return {'items': [{'asset_id': 'skill', 'name': 'Skill', 'plugin_type': 'skill'}]}
    monkeypatch.setattr(manager, '_team_skills_hub_http_post_data', post)
    first = await manager.handle_skills_swarm_skills_hub_recommend({'top_k': 50, 'cache_mode': 'prefer_cache', '_catalog_anonymous': True})
    assert first['cache']['state'] == 'miss'
    await asyncio.sleep(.01)
    result = await manager.handle_skills_swarm_skills_hub_recommend({'top_k': 50, 'cache_mode': 'prefer_cache', 'session_id': 'second-session'})
    assert result['skills'][0]['asset_id'] == 'skill'
    assert result['cache']['state'] == 'fresh' and calls == ['/api/v1/recommend']
    # Unverified credentials bypass public reuse, including an already warm key.
    monkeypatch.setattr(manager, '_resolve_teamskills_hub_auth_with_env', lambda params: {'token': 'private'})
    private = await manager.handle_skills_swarm_skills_hub_recommend({'top_k': 50, 'cache_mode': 'prefer_cache'})
    assert private['cache']['state'] == 'miss'
    await asyncio.sleep(.01)
    assert len(calls) == 2
    await cache.close()

@pytest.mark.asyncio
async def test_online_search_caches_hub_source_not_other_sources(tmp_path, monkeypatch):
    import jiuwenswarm.server.runtime.marketplace.hub_catalog_cache as module
    from jiuwenswarm.server.runtime.skill.skill_manager import SkillManager
    cache = HubCatalogCache(tmp_path / 'cache.sqlite')
    monkeypatch.setattr(module, 'get_hub_catalog_cache', lambda: cache)
    manager = SkillManager(workspace_dir=str(tmp_path))
    monkeypatch.setattr(manager, '_get_clawhub_token', lambda: 'configured')
    calls = []
    async def get(path, **kwargs):
        calls.append('hub')
        return {'items': [{'asset_id': 'skill', 'name': 'Skill', 'plugin_type': 'skill'}]}
    async def other(params):
        calls.append('other')
        return {'success': True, 'skills': []}
    monkeypatch.setattr(manager, '_team_skills_hub_http_get_data', get)
    monkeypatch.setattr(manager, 'handle_skills_clawhub_search', other)
    await manager.handle_skills_online_search({'query': 'test', 'cache_mode': 'prefer_cache'})
    await asyncio.sleep(.01)
    result = await manager.handle_skills_online_search({'query': 'test', 'cache_mode': 'prefer_cache'})
    assert result['cache']['state'] == 'fresh'
    assert calls.count('hub') == 1 and calls.count('other') == 2
    await cache.close()

@pytest.mark.asyncio
async def test_retry_after_bounds_concurrency_and_lru(tmp_path):
    cache = HubCatalogCache(tmp_path / 'cache.sqlite', max_entries=2)
    gate = asyncio.Event()
    async def blocked():
        await gate.wait()
        yield {'items': [], 'complete': True}
    cache.read('one', blocked)
    cache.read('two', blocked)
    _, state = cache.read('three', blocked)
    assert not state['refreshing'] and len(cache.tasks) == 2
    gate.set()
    await asyncio.sleep(.01)
    cache.read('three', blocked)
    await asyncio.sleep(.01)
    assert len(cache.entries) == 2
    attempts = []
    async def limited():
        attempts.append(1)
        error = RuntimeError('limited')
        error.retry_after = '120'
        raise error
        yield
    cache.read('rate', limited)
    await asyncio.sleep(.01)
    _, state = cache.read('rate', limited, refresh=True)
    assert state['state'] == 'error' and not state['refreshing'] and attempts == [1]
    import time
    assert next(iter(cache.failures.values()))[1] > time.time() + 110
    await cache.close()

@pytest.mark.asyncio
async def test_signed_url_array_leaves_never_persist(tmp_path):
    path = tmp_path / 'safe.sqlite'
    cache = HubCatalogCache(path)
    async def load():
        yield {'items': [{'asset_id': 'one', 'tags': ['safe', 'https://example.com/icon?signature=secret', ['https://example.com/a?token=nestedsecret']]}], 'complete': True}
    cache.read('public', load)
    await asyncio.sleep(.01)
    data, _ = cache.read('public', load)
    assert 'secret' not in str(data)
    await cache.close()
    assert b'secret' not in path.read_bytes()

@pytest.mark.asyncio
async def test_malformed_card_and_description_url_do_not_break_catalog(tmp_path, monkeypatch):
    import jiuwenswarm.server.runtime.marketplace.hub_catalog_cache as module
    from jiuwenswarm.server.runtime.marketplace.hub_asset_port import HubAssetSummary, HubSearchPage
    cache = HubCatalogCache(tmp_path / 'safe.sqlite')
    monkeypatch.setattr(module, 'get_hub_catalog_cache', lambda: cache)
    class Port:
        base_url = 'https://public.test'
        async def search_assets(self, request):
            return HubSearchPage((HubAssetSummary('mcp', 'one', 'name', 'https://example.com/docs?lang=en', '1', '', (), 'one'),), 1, 1, 100)
    port = Port()
    await module.cached_asset_catalog(port, 'mcp')
    await asyncio.sleep(.01)
    items, _ = await module.cached_asset_catalog(port, 'mcp')
    assert len(items) == 1 and isinstance(items[0].short_description, str)
    key, (updated, payload) = next(iter(cache.entries.items()))
    payload['items'].insert(0, {'kind': 'mcp'})
    items, _ = await module.cached_asset_catalog(port, 'mcp')
    assert len(items) == 1 and items[0].asset_id == 'one'
    await cache.close()

@pytest.mark.asyncio
async def test_authenticated_clients_share_memory_scope_across_instances(tmp_path, monkeypatch):
    import jiuwenswarm.server.runtime.marketplace.hub_catalog_cache as module
    from jiuwenswarm.server.runtime.marketplace.hub_client import HubClient
    from jiuwenswarm.server.runtime.marketplace.hub_asset_port import HubAssetSummary, HubSearchPage
    cache = HubCatalogCache(tmp_path / 'scope.sqlite')
    monkeypatch.setattr(module, 'get_hub_catalog_cache', lambda: cache)
    calls = []
    async def search(self, request):
        calls.append(self.transport.system_token)
        return HubSearchPage((HubAssetSummary('mcp', 'one', 'name', '', '1', '', (), 'one'),), 1, 1, 100)
    monkeypatch.setattr(HubClient, 'search_assets', search)
    await module.cached_asset_catalog(HubClient(base_url='https://hub.test', system_token='server-secret'), 'mcp', preload=True)
    await asyncio.sleep(.01)
    items, state = await module.cached_asset_catalog(HubClient(base_url='https://hub.test', system_token='server-secret'), 'mcp')
    assert len(items) == 1 and state['state'] == 'fresh' and calls == ['server-secret']
    assert cache.db.execute('SELECT COUNT(*) FROM catalog').fetchone()[0] == 0
    items, state = await module.cached_asset_catalog(HubClient(base_url='https://hub.test', system_token='changed-secret'), 'mcp')
    assert items == [] and state['state'] == 'miss'
    await cache.close()

@pytest.mark.asyncio
async def test_server_authenticated_recommendation_is_nonblocking_memory_only(tmp_path, monkeypatch):
    import jiuwenswarm.server.runtime.marketplace.hub_catalog_cache as module
    from jiuwenswarm.server.runtime.skill.skill_manager import SkillManager
    cache = HubCatalogCache(tmp_path / 'scope.sqlite')
    monkeypatch.setattr(module, 'get_hub_catalog_cache', lambda: cache)
    monkeypatch.setenv('TEAM_SKILLS_HUB_SYSTEM_TOKEN', 'server-secret')
    manager = SkillManager(workspace_dir=str(tmp_path))
    gate = asyncio.Event()
    calls = []
    async def post(path, **kwargs):
        calls.append(kwargs['headers'].get('X-System-Token'))
        await gate.wait()
        return {'items': [{'asset_id': 'one', 'name': 'one', 'plugin_type': 'skill'}]}
    monkeypatch.setattr(manager, '_team_skills_hub_http_post_data', post)
    first = await asyncio.wait_for(manager.handle_skills_swarm_skills_hub_recommend({'cache_mode': 'prefer_cache', 'top_k': 50}), .05)
    assert first['cache']['state'] == 'miss'
    gate.set()
    await asyncio.sleep(.01)
    result = await manager.handle_skills_swarm_skills_hub_recommend({'cache_mode': 'prefer_cache', 'top_k': 50, 'session_id': 'new-session'})
    assert result['cache']['state'] == 'fresh' and result['skills'][0]['asset_id'] == 'one'
    assert calls == ['server-secret'] and cache.db.execute('SELECT COUNT(*) FROM catalog').fetchone()[0] == 0
    monkeypatch.setenv('TEAM_SKILLS_HUB_SYSTEM_TOKEN', 'changed-secret')
    result = await manager.handle_skills_swarm_skills_hub_recommend({'cache_mode': 'prefer_cache', 'top_k': 50})
    assert result['cache']['state'] == 'miss'
    await cache.close()

@pytest.mark.asyncio
async def test_invalidation_only_marks_matching_kind(tmp_path, monkeypatch):
    import jiuwenswarm.server.runtime.marketplace.hub_catalog_cache as module
    cache = HubCatalogCache(tmp_path / 'kinds.sqlite')
    monkeypatch.setattr(module, '_caches', {'test': cache})
    async def load():
        yield {'items': [], 'complete': True}
    cache.read('mcp', load, kind='mcp')
    cache.read('skill', load, kind='skill', public=False)
    await asyncio.sleep(.01)
    module.invalidate_hub_catalog('skill')
    _, mcp = cache.read('mcp', load, kind='mcp')
    _, skill = cache.read('skill', load, kind='skill', public=False)
    assert mcp['state'] == 'fresh' and skill['state'] == 'stale'
    await cache.close()

@pytest.mark.asyncio
async def test_explicit_recommendation_credentials_and_sessions_are_isolated(tmp_path, monkeypatch):
    import jiuwenswarm.server.runtime.marketplace.hub_catalog_cache as module
    from jiuwenswarm.server.runtime.skill.skill_manager import SkillManager
    cache = HubCatalogCache(tmp_path / 'scopes.sqlite')
    monkeypatch.setattr(module, 'get_hub_catalog_cache', lambda: cache)
    manager = SkillManager(workspace_dir=str(tmp_path))
    async def post(path, **kwargs):
        return {'items': [{'asset_id': kwargs['headers']['Authorization'], 'name': 'one', 'plugin_type': 'skill'}]}
    monkeypatch.setattr(manager, '_team_skills_hub_http_post_data', post)
    params = {'cache_mode': 'prefer_cache', 'token': 'first', 'session_id': 'session-a'}
    await manager.handle_skills_swarm_skills_hub_recommend(params)
    await asyncio.sleep(.01)
    hit = await manager.handle_skills_swarm_skills_hub_recommend(params)
    assert hit['cache']['state'] == 'fresh'
    for change in ({'token': 'second'}, {'session_id': 'session-b'}, {'user_id': 'another'}):
        miss = await manager.handle_skills_swarm_skills_hub_recommend({**params, **change})
        assert miss['cache']['state'] == 'miss' and miss['skills'] == []
    assert cache.db.execute('SELECT COUNT(*) FROM catalog').fetchone()[0] == 0
    await cache.close()

@pytest.mark.asyncio
async def test_startup_uses_configured_recommendation_credentials(tmp_path, monkeypatch):
    import jiuwenswarm.server.runtime.marketplace.hub_catalog_cache as module
    from jiuwenswarm.server.runtime.skill.skill_manager import SkillManager
    cache = HubCatalogCache(tmp_path / 'startup.sqlite')
    monkeypatch.setattr(module, 'get_hub_catalog_cache', lambda: cache)
    monkeypatch.setenv('TEAM_SKILLS_HUB_SYSTEM_TOKEN', 'startup-secret')
    manager = SkillManager(workspace_dir=str(tmp_path))
    kinds = []
    async def asset(port, kind, **kwargs):
        kinds.append(kind)
        return [], {}
    async def post(path, **kwargs):
        assert kwargs['headers']['X-System-Token'] == 'startup-secret'
        return {'items': []}
    monkeypatch.setattr(module, 'cached_asset_catalog', asset)
    monkeypatch.setattr(manager, '_team_skills_hub_http_post_data', post)
    await module.start_hub_catalog_preload(manager)
    await asyncio.sleep(.01)
    result = await manager.handle_skills_swarm_skills_hub_recommend({'cache_mode': 'prefer_cache', 'top_k': 50, 'session_id': 'page'})
    assert result['cache']['state'] == 'fresh' and kinds == ['agent_template', 'agent_group', 'plugin', 'mcp']
    assert cache.db.execute('SELECT COUNT(*) FROM catalog').fetchone()[0] == 0
    await cache.close()

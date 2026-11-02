"""Real native SDK RPC against the research-only authorization boundary."""
from types import SimpleNamespace
from unittest.mock import AsyncMock
import socket
from urllib.parse import urlsplit

import pytest

from jiuwenswarm.agents.harness.common.tools.deepresearch.mcp_search import SearchMcpServer, search_mcp_gateway
from test_deepresearch_mcp_search import SERVER


@pytest.fixture(autouse=True)
def without_legacy_middleware_decorator(monkeypatch):
    """Exercise the Starlette 1.x API surface even on older installations."""
    from starlette.applications import Starlette
    monkeypatch.delattr(Starlette, 'middleware', raising=False)


@pytest.mark.asyncio
async def test_sdk_gateway_preserves_default_blacklist_and_enforces_calls():
    from openjiuwen_deepsearch.framework.openjiuwen.tools.mcp.client import McpClient
    from openjiuwen_deepsearch.framework.openjiuwen.tools.mcp.tool_builder import build_mcp_local_functions
    state = {'enabled': True}
    called = AsyncMock(return_value={'result': '{"search_results":[{"title":"MCP source",'
                                               '"url":"https://example.org/fact",'
                                               '"content":"UNIQUE-GATEWAY-7342"}]}'})
    tool = SimpleNamespace(card=SimpleNamespace(name='search', description='Search temporary corpus',
                                               input_params={'type': 'object', 'properties': {
                                                   'query': {'type': 'string'}}, 'required': ['query']}))

    async def allowed(name, args):
        # Default builtin-only blacklist does not disable a remote search tool.
        return state['enabled'] and name not in {'search_skill', 'install_skill', 'uninstall_skill', 'code'} \
            and args.get('query') != 'denied'

    grant = SearchMcpServer({**SERVER, 'server_name': 'user-uuid:' + 'x' * 100}, [tool], allowed, called)
    async with search_mcp_gateway([grant]) as native:
        endpoint = native[0]
        port = urlsplit(endpoint['server_url']).port
        client = McpClient(endpoint)
        await client.connect()
        try:
            tools = await build_mcp_local_functions(client, endpoint['server_name'])
            assert len(tools) == 1
            assert len(tools[0].card.name) <= 64
            result = await tools[0].invoke({'query': 'allowed'})
            assert result['search_results'][0]['content'] == 'UNIQUE-GATEWAY-7342'
            assert called.await_count == 1
            result = await tools[0].invoke({'query': 'denied'})
            assert result.get('error')
            assert called.await_count == 1
            # Unknown tools cannot be executed even when named directly in an RPC.
            result = await client.call_tool('new_ungranted_tool', {'query': 'allowed'})
            assert result.isError
            assert called.await_count == 1
            state['enabled'] = False
            assert await client.list_tools() == []
            assert (await tools[0].invoke({'query': 'allowed'})).get('error')
            assert called.await_count == 1
        finally:
            await client.close()
    with pytest.raises(OSError):
        socket.create_connection(('127.0.0.1', port), timeout=.2)


@pytest.mark.asyncio
@pytest.mark.parametrize('authorization', [None, 'Bearer incorrect', 'Bearer 无效'])
async def test_gateway_rejects_missing_or_invalid_credentials(authorization):
    import httpx
    called = AsyncMock()
    tool = SimpleNamespace(card=SimpleNamespace(name='search', description='Search', input_params={}))
    grant = SearchMcpServer(SERVER, [tool], AsyncMock(return_value=True), called)
    async with search_mcp_gateway([grant]) as native:
        # Bytes permit malformed non-ASCII credentials to reach the HTTP boundary.
        headers = {} if authorization is None else {'Authorization': authorization.encode('utf-8')}
        async with httpx.AsyncClient() as client:
            response = await client.post(native[0]['server_url'], headers=headers, json={})
        assert response.status_code == 401
        called.assert_not_awaited()


@pytest.mark.asyncio
async def test_plain_connection_configuration_cannot_bypass_gateway_authorization():
    with pytest.raises(ValueError, match='mcp_authorization_missing'):
        async with search_mcp_gateway([SERVER]):
            pytest.fail('unbound configuration must not be delegated')


@pytest.mark.asyncio
async def test_no_mcp_does_not_start_a_service():
    async with search_mcp_gateway(None) as native:
        assert native == []


@pytest.mark.asyncio
async def test_gateway_transport_budget_does_not_truncate_slow_authorized_search():
    import asyncio
    from openjiuwen_deepsearch.framework.openjiuwen.tools.mcp.client import McpClient
    from openjiuwen_deepsearch.framework.openjiuwen.tools.mcp.tool_builder import build_mcp_local_functions
    tool = SimpleNamespace(card=SimpleNamespace(name='search', description='Slow search', input_params={'type': 'object'}))

    async def allowed(name, args):
        return True

    async def slow_call(tool, args):
        await asyncio.sleep(.4)
        return {'result': '{"accepted":true}'}

    grant = SearchMcpServer({**SERVER, 'timeout': 120.0}, [tool], allowed, slow_call)
    async with search_mcp_gateway([grant]) as native:
        # Scale 30/120 seconds to .3/1.2 so the real HTTP read-timeout regression
        # is exercised quickly, instead of waiting for a 35-second query.
        client = McpClient({**native[0], 'timeout': native[0]['timeout'] / 100})
        await client.connect()
        try:
            tools = await build_mcp_local_functions(client, native[0]['server_name'])
            assert (await tools[0].invoke({}))['accepted'] is True
        finally:
            await client.close()


@pytest.mark.asyncio
async def test_cancelled_research_closes_private_endpoint():
    import asyncio
    ready = asyncio.Event()
    endpoint = {}
    tool = SimpleNamespace(card=SimpleNamespace(name='search', description='Search', input_params={}))

    async def allowed(name, args):
        return True

    async def research():
        grant = SearchMcpServer(SERVER, [tool], allowed, AsyncMock())
        async with search_mcp_gateway([grant]) as native:
            endpoint.update(native[0])
            ready.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(research())
    await asyncio.wait_for(ready.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=5)
    with pytest.raises(OSError):
        socket.create_connection(('127.0.0.1', urlsplit(endpoint['server_url']).port), timeout=.2)

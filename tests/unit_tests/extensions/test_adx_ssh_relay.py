# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Native SSH CONNECT authentication, cleanup and extension wiring."""
import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from gateway.extensions.adx.config import load_router_config
from gateway.extensions.adx.extension import AdxRouter
from gateway.extensions.adx.ssh_relay import AdxSshRelay


def settings(monkeypatch, tmp_path):
    monkeypatch.setenv('ADX_UNIT_KEY', 'unit-platform-key')
    return load_router_config({'gateway':{
        'agent_client':{'type':'adx_router'},
        'adx':{'ingress_url':'https://adx.test','tenant':'test','token_env':'ADX_UNIT_KEY',
               'ssh_known_hosts':str(tmp_path/'known_hosts')},
        'agentos':{'workspace_root':str(tmp_path)},
    }})


class Writer:
    def __init__(self):
        self.data=bytearray()
        self.closed=False

    def write(self, data):
        self.data.extend(data)

    async def drain(self):
        pass

    def close(self):
        self.closed=True

    async def wait_closed(self):
        pass


@pytest.mark.asyncio
async def test_ssh_connect_rejection_never_opens_ssh_or_exposes_credentials(monkeypatch,tmp_path):
    import gateway.extensions.adx.ssh_relay as module
    config=settings(monkeypatch,tmp_path)
    relay=AdxSshRelay(config)
    monkeypatch.setattr(relay,'_resolve_client_keys',lambda user:['unit-key'])
    reader=asyncio.StreamReader();reader.feed_data(b'HTTP/1.1 403 Forbidden\r\n\r\n')
    writer=Writer()
    connect=AsyncMock()
    monkeypatch.setattr(module,'_import_asyncssh',lambda:SimpleNamespace(connect=connect))
    open_connection=AsyncMock(return_value=(reader,writer))
    monkeypatch.setattr(module.asyncio,'open_connection',open_connection)
    with pytest.raises(ValueError,match='HTTP 403') as error:
        await relay._connect('test-alice+http','alice')
    assert 'unit-platform-key' not in str(error.value)
    assert writer.closed
    connect.assert_not_called()
    assert b'CONNECT test-alice+http:2222 HTTP/1.1' in writer.data
    assert b'Authorization: Bearer unit-platform-key\r\n' in writer.data
    assert open_connection.call_args.kwargs['ssl'].verify_mode.name=='CERT_REQUIRED'


@pytest.mark.asyncio
async def test_ssh_connect_uses_pinned_host_and_closes_both_pumps(monkeypatch,tmp_path):
    import gateway.extensions.adx.ssh_relay as module
    config=settings(monkeypatch,tmp_path);relay=AdxSshRelay(config)
    monkeypatch.setattr(relay,'_resolve_client_keys',lambda user:['unit-key'])
    ingress_reader=asyncio.StreamReader();ingress_reader.feed_data(b'HTTP/1.1 200 OK\r\n\r\n')
    ingress_writer=Writer();peer_writer=Writer();peer_reader=asyncio.StreamReader()
    bridge_sockets=[]
    async def open_connection(*args,**kwargs):
        if 'sock' in kwargs:
            bridge_sockets.append(kwargs['sock'])
            return peer_reader,peer_writer
        return ingress_reader,ingress_writer
    connection=SimpleNamespace(close=Mock(),wait_closed=AsyncMock())
    sockets=[]
    async def connect(*args,**kwargs):
        assert args==('test-alice+http',)
        assert kwargs['known_hosts']==config.ssh_known_hosts
        assert kwargs['username']=='agentos'
        sockets.append(kwargs['sock'])
        return connection
    monkeypatch.setattr(module.asyncio,'open_connection',open_connection)
    monkeypatch.setattr(module,'_import_asyncssh',lambda:SimpleNamespace(connect=connect))
    wrapped=await relay._connect('test-alice+http','alice')
    wrapped.close();await wrapped.wait_closed()
    assert ingress_writer.closed and peer_writer.closed
    assert all(task.done() for task in wrapped._pumps)
    for sock in sockets+bridge_sockets:sock.close()


@pytest.mark.asyncio
async def test_ssh_rejects_unpinned_hosts_and_header_injection(monkeypatch,tmp_path):
    relay=AdxSshRelay(replace(settings(monkeypatch,tmp_path),ssh_known_hosts=''))
    with pytest.raises(ValueError,match='host verification'):
        await relay._connect('test-alice','alice')
    with pytest.raises(ValueError,match='Environment ID'):
        await relay._connect('alice\r\nAuthorization: bad','alice')


def test_adx_extension_delegates_third_party_and_key_issuer(monkeypatch,tmp_path):
    import gateway.extensions.adx.extension as module
    router=Mock()
    monkeypatch.setattr(module,'AdxRouterClient',lambda *args,**kwargs:router)
    extension=AdxRouter(settings(monkeypatch,tmp_path))
    issuer=object();extension.set_key_issuer(issuer,ephemeral_key_ttl_sec=120)
    router.set_key_issuer.assert_called_once_with(issuer,ephemeral_key_ttl_sec=120)
    assert extension.get_third_agent() is extension._third_agent


@pytest.mark.asyncio
async def test_restart_policy_and_rootfs_mounts_are_forwarded_only_when_configured(monkeypatch,tmp_path):
    from gateway.extensions.adx.control_client import AdxControlClient
    policy={'maxAttempts':3,'initialBackoffSeconds':1,'maxBackoffSeconds':5}
    config=replace(settings(monkeypatch,tmp_path),restart_policy=policy)
    control=AdxControlClient(config)
    mount={'source':'/ssh-config','target':'/__ssh','readonly':True}
    body=control._creation_body('native','user+agent','/workspace',
        {'rootfs':{'imageurl':'ssh:1','mounts':[mount]}},None,[mount])
    assert body['restartPolicy']==policy
    assert len(body['mounts'])==2
    assert body['mounts'][1]['options']==['rbind','ro']
    await control.close()
    control=AdxControlClient(replace(config,restart_policy=None))
    body=control._creation_body('native','user+agent','/workspace',{'rootfs':{'imageurl':'ssh:1'}},None,None)
    assert 'restartPolicy' not in body
    await control.close()


@pytest.mark.asyncio
async def test_delete_evidence_requires_acknowledged_native_response(monkeypatch,tmp_path,caplog):
    import httpx
    from gateway.extensions.adx.control_client import AdxControlClient,AdxControlError
    from gateway.extensions.adx.control_client import logger
    caplog.set_level('INFO',logger=logger.name)
    # The full suite configures non-propagating application loggers.
    monkeypatch.setattr(logger,'handlers',[caplog.handler])
    monkeypatch.setattr(logger,'propagate',False)
    client=AdxControlClient(settings(monkeypatch,tmp_path))
    await client.http.aclose()
    requests=[]
    def handle(request):
        requests.append(request)
        return httpx.Response(200,json={'success':True})
    client.http=httpx.AsyncClient(transport=httpx.MockTransport(handle))
    await client.delete_sandbox('test-owned')
    assert requests[0].method=='DELETE'
    assert requests[0].url.path=='/api/environment/v1/environments/test-owned'
    assert 'environment.delete.ok instance_id=test-owned' in caplog.text
    caplog.clear()
    await client.http.aclose()
    def lost(request):
        raise httpx.ReadTimeout('outcome unknown',request=request)
    client.http=httpx.AsyncClient(transport=httpx.MockTransport(lost))
    with pytest.raises(AdxControlError):
        await client.delete_sandbox('test-unknown')
    assert 'environment.delete.ok' not in caplog.text
    await client.close()


@pytest.mark.asyncio
async def test_authenticated_switch_waits_for_background_ssh_setup(monkeypatch, tmp_path):
    from gateway.extensions.adx.router_client import AdxRouterClient
    from gateway.extensions.agentos.agentos_router.config import load_ssh_channel_endpoint

    config = settings(monkeypatch, tmp_path)
    endpoint = load_ssh_channel_endpoint({'channels': {'ssh': {
        'enabled': True, 'listen_host': '127.0.0.1', 'listen_port': 2222,
        'auth': {'enabled': True},
    }}})
    client = AdxRouterClient(replace(config, router=replace(config.router, ssh_channel=endpoint)))
    create = AsyncMock()
    monkeypatch.setattr(client._agent_manager, 'get_or_create_agent', create)
    monkeypatch.setattr(client, '_uses_direct_yuanrong', lambda _: True)
    task = asyncio.create_task(client.thirdagent_switch(user_id='alice', agent_type='jiuwenswarm'))
    try:
        await asyncio.sleep(0)
        assert not task.done(), 'Authenticated switch must wait for its SSH issuer'
        create.assert_not_awaited()
        issuer = SimpleNamespace(issue_ephemeral_key=lambda **kwargs: 'ephemeral-test-key')
        client.set_key_issuer(issuer)
        result = await asyncio.wait_for(task, timeout=1)
        assert result['ok'] and result['payload']['ssh_private_key'] == 'ephemeral-test-key'
        client.set_key_issuer(None)
        monkeypatch.setattr(client, '_ssh_key_ready_timeout', 0.01)
        result = await client.thirdagent_switch(user_id='alice', agent_type='jiuwenswarm')
        assert not result['ok'] and result['code'] == 'SSH_NOT_READY'
        create.assert_not_awaited()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await client.shutdown()

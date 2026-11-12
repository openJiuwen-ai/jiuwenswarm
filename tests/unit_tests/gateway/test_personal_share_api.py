# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Exercise personal share HTTP adapters without browsers or deployment tools."""

import asyncio
import io
import json
import struct
import zlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import httpx
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from starlette.requests import Request

from agent_gateway.channel_manager.web import personal_share_api as api
from agent_gateway.channel_manager.web import share_snapshot
from agent_gateway.channel_manager.web.share_image_export import ShareImageExportManager


def _png():
    def chunk(kind, data):
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data))
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', 2250, 1, 8, 6, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(b'\x00' + b'\x00\x00\x00\xff' * 2250)) + chunk(b'IEND', b''))


@pytest.fixture
def client(monkeypatch):
    manager = ShareImageExportManager(renderer=lambda **kwargs: pytest.fail('Gateway must not launch a server browser'))
    monkeypatch.setattr(api, '_manager', lambda: manager)
    monkeypatch.setattr(api, 'get_share_session_metadata', AsyncMock(return_value={'title': 'Export'}))
    monkeypatch.setattr(api, 'build_share_snapshot', AsyncMock(return_value=(
        {'session_id': 'session', 'records': [{'role': 'assistant', 'content': 'retained'}]}, 'share.png')))
    app = FastAPI()

    @app.get('/unrelated')
    def unrelated():
        raise HTTPException(418, 'unchanged')

    channel = SimpleNamespace(authenticate_handshake=AsyncMock(return_value=None))
    api.register_personal_share_routes(app, channel)
    with TestClient(app) as client:
        client.manager = manager
        client.channel = channel
        yield client


def _create(client):
    response = client.post('/share-api/jobs', json={'session_id': 'session', 'allow_browser_render': True})
    assert response.status_code == 202, response.text
    assert response.headers['cache-control'] == 'no-store'
    return response.json()


def test_personal_no_job_is_null_and_other_errors_unchanged(client):
    assert client.get('/share-api/jobs?session_id=session').json() is None
    assert client.get('/share-api/jobs').status_code == 400
    assert client.get('/unrelated').json() == {'detail': 'unchanged'}


def test_personal_browser_export_upload_and_actual_download(client):
    created = _create(client)
    job, token = created['job_id'], created['client_token']
    assert created['renderer'] == 'browser'
    status = client.get(f'/share-api/jobs/{job}').json()
    assert 'client_token' not in status
    assert client.get(f'/share-api/jobs/{job}/snapshot').json()['snapshot']['records'][0]['content'] == 'retained'
    update = {'client_token': token, 'claim': True, 'claim_id': 'a' * 32, 'state': 'running', 'phase': 'loading_snapshot'}
    assert client.post(f'/share-api/jobs/{job}/browser', json=update).status_code == 200
    denied = client.post(f'/share-api/jobs/{job}/browser-result?filename=share.png', content=_png())
    assert denied.status_code == 403
    uploaded = client.post(f'/share-api/jobs/{job}/browser-result?filename=share.png', content=_png(),
                           headers={'X-Share-Export-Token': token})
    assert uploaded.status_code == 200, uploaded.text
    assert uploaded.json()['state'] == 'completed'
    assert client.head(f'/share-api/jobs/{job}/download').status_code == 200
    assert client.manager.get_status(job)['phase'] == 'completed'
    assert client.get(f'/share-api/jobs/{job}/download').status_code == 409
    claim = client.post(f'/share-api/jobs/{job}/browser', json={'client_token': token, 'claim_download': True}).json()
    url = f'/share-api/jobs/{job}/download?download_attempt_id=' + claim['download_attempt_id']
    response = client.get(url, headers={'Range': 'bytes=0-9'})
    assert response.status_code == 200
    assert response.content == _png()
    assert client.manager.get_status(job)['phase'] == 'download_sent'
    assert client.get(url).status_code == 409


def test_personal_resume_keeps_snapshot_and_wrong_session_cannot_resume(client):
    created = _create(client)
    resumed = client.post('/share-api/jobs', json={'session_id': 'session', 'job_id': created['job_id']})
    assert resumed.json()['client_token'] == created['client_token']
    assert resumed.json()['reused'] is True
    assert client.post('/share-api/jobs', json={'session_id': 'other', 'job_id': created['job_id']}).status_code == 404
    assert api.build_share_snapshot.await_count == 1


def test_personal_refused_session_does_not_create_job(client, monkeypatch):
    monkeypatch.setattr(api, 'get_share_session_metadata', AsyncMock(side_effect=HTTPException(403, 'denied')))
    assert client.post('/share-api/jobs', json={'session_id': 'secret', 'allow_browser_render': True}).status_code == 403
    api.build_share_snapshot.assert_not_awaited()


def test_personal_job_reads_and_resume_require_owner_cookie(client):
    created = _create(client)
    job, token = created['job_id'], created['client_token']
    cookie = client.cookies.get(api._ACCESS_COOKIE)
    assert 'HttpOnly' in client.post(
        '/share-api/jobs', json={'session_id': 'other', 'allow_browser_render': True},
    ).headers['set-cookie']
    client.cookies.clear()
    paths = [
        '/share-api/jobs?session_id=session', f'/share-api/jobs/{job}',
        f'/share-api/jobs/{job}/snapshot', f'/share-api/jobs/{job}/download',
    ]
    for path in paths:
        assert client.get(path).status_code == 403
    assert client.head(f'/share-api/jobs/{job}/download').status_code == 403
    assert client.post('/share-api/jobs', json={
        'session_id': 'session', 'job_id': job,
    }).status_code == 403
    assert client.post('/share-api/jobs', json={
        'session_id': 'session', 'allow_browser_render': True,
    }).status_code == 403
    assert client.post(f'/share-api/jobs/{job}/browser', json={
        'client_token': token, 'claim_download': True,
    }).status_code == 403
    assert client.post(
        f'/share-api/jobs/{job}/browser-result?filename=share.png', content=_png(),
        headers={'X-Share-Export-Token': token},
    ).status_code == 403
    client.cookies.set(api._ACCESS_COOKIE, '0' * 64)
    assert client.get(f'/share-api/jobs/{job}/snapshot').status_code == 403
    client.cookies.clear()
    client.cookies.set(api._ACCESS_COOKIE, cookie)
    assert client.get(f'/share-api/jobs/{job}').status_code == 200
    assert client.get(f'/share-api/jobs/{job}/snapshot').status_code == 200


def test_personal_read_checks_routed_session_access(client, monkeypatch):
    created = _create(client)
    monkeypatch.setattr(api, 'get_share_session_metadata', AsyncMock(side_effect=HTTPException(403, 'denied')))
    job = created['job_id']
    for path in [
        '/share-api/jobs?session_id=session', f'/share-api/jobs/{job}',
        f'/share-api/jobs/{job}/snapshot', f'/share-api/jobs/{job}/download',
    ]:
        assert client.get(path).status_code == 403
    assert api.get_share_session_metadata.await_count == 4


def test_personal_reuses_web_auth_and_binds_the_authenticated_principal(client):
    auth = client.channel.authenticate_handshake
    auth.return_value = SimpleNamespace(success=True, user_id='alice')
    created = _create(client)
    job = created['job_id']
    assert client.get(f'/share-api/jobs/{job}/snapshot').status_code == 200
    auth.return_value = SimpleNamespace(success=True, user_id='bob')
    assert client.get(f'/share-api/jobs/{job}/snapshot').status_code == 403
    auth.return_value = SimpleNamespace(success=False, user_id='')
    assert client.get(f'/share-api/jobs/{job}').status_code == 401
    assert client.post('/share-api/jobs', json={'session_id': 'session'}).status_code == 401
    assert auth.call_args.kwargs['channel'] == 'web'


@pytest.mark.asyncio
async def test_personal_json_disconnect_is_a_controlled_error():
    async def receive():
        return {'type': 'http.disconnect'}

    request = Request({'type': 'http', 'headers': []}, receive=receive)
    with pytest.raises(HTTPException) as caught:
        await api._json_body(request)
    assert caught.value.status_code == 400
    assert caught.value.detail == 'client_disconnected'


@pytest.mark.asyncio
async def test_personal_upload_has_a_total_deadline(monkeypatch):
    monkeypatch.setattr(api, '_UPLOAD_TIMEOUT_SECONDS', 0.02)
    monkeypatch.setattr(api, '_UPLOAD_IDLE_SECONDS', 1)

    async def receive():
        await asyncio.sleep(0.005)
        return {'type': 'http.request', 'body': b'x', 'more_body': True}

    request = Request({'type': 'http', 'headers': []}, receive=receive)
    with pytest.raises(TimeoutError):
        await api._read_upload(request, io.BytesIO(), 1024 * 1024)


@pytest.mark.asyncio
async def test_personal_rejects_uploads_before_buffering_and_releases_capacity(client, monkeypatch):
    monkeypatch.setattr(api, '_MAX_UPLOADS', 1)
    first = _create(client)
    second = client.post('/share-api/jobs', json={
        'session_id': 'second', 'allow_browser_render': True,
    }).json()
    started, release = asyncio.Event(), asyncio.Event()

    async def read_upload(request, stream, size):
        started.set()
        await release.wait()
        stream.write(_png())

    reader = AsyncMock(side_effect=read_upload)
    monkeypatch.setattr(api, '_read_upload', reader)
    transport = httpx.ASGITransport(app=client.app)
    async with httpx.AsyncClient(transport=transport, base_url='http://testserver', cookies=client.cookies) as web:
        async def upload(job):
            return await web.post(
                f"/share-api/jobs/{job['job_id']}/browser-result?filename=share.png",
                content=_png(), headers={'X-Share-Export-Token': job['client_token']},
            )

        pending = asyncio.create_task(upload(first))
        try:
            await asyncio.wait_for(started.wait(), timeout=2)
            assert (await upload(first)).status_code == 409
            assert (await upload(second)).status_code == 429
            assert reader.await_count == 1
        finally:
            release.set()
            result = await pending
        assert result.status_code == 200
        assert (await upload(second)).status_code == 200


@pytest.mark.parametrize('body', [[], None, 'text'])
def test_personal_rejects_non_object_json(client, body):
    assert client.post('/share-api/jobs', content=json.dumps(body)).status_code == 400


@pytest.mark.parametrize('body', [b'{', b'\xff'])
def test_personal_rejects_malformed_json_and_invalid_encoding(client, body):
    response = client.post('/share-api/jobs', content=body)
    assert response.status_code == 400
    assert response.json() == {'error': 'invalid_json'}
    api.build_share_snapshot.assert_not_awaited()


@pytest.mark.parametrize('error', [OSError('storage unavailable'), TimeoutError('storage timed out')])
def test_personal_upload_io_failures_return_controlled_errors(client, monkeypatch, error):
    created = _create(client)
    job, token = created['job_id'], created['client_token']

    def unavailable_file():
        raise error

    monkeypatch.setattr(api.tempfile, 'TemporaryFile', unavailable_file)
    response = client.post(
        f'/share-api/jobs/{job}/browser-result?filename=share.png',
        content=_png(),
        headers={'X-Share-Export-Token': token},
    )
    assert response.status_code == 400
    assert response.json() == {'error': str(error)}
    assert token not in response.text


def test_personal_invalid_upload_releases_job_and_keeps_tokens_private(client):
    created = _create(client)
    job, token = created['job_id'], created['client_token']
    response = client.post(f'/share-api/jobs/{job}/browser-result?filename=share.png', content=b'invalid',
                           headers={'X-Share-Export-Token': token})
    assert response.status_code == 400
    assert client.get(f'/share-api/jobs/{job}').json()['state'] == 'failed'
    assert token not in response.text
    assert client.get(f'/share-api/jobs/{job}/download').status_code == 409


def _event(payload):
    return {'type': 'event', 'event': 'history.message', 'payload': payload}


@pytest.mark.asyncio
async def test_snapshot_collects_all_pages_and_reassembles_long_finals(monkeypatch):
    calls = []
    channel = SimpleNamespace(unregister_request_outbound=AsyncMock())
    long_text = '完整正文🙂' * 20000
    fragments = [long_text[:50000], long_text[50000:]]
    pages = [
        [_event({'message': {'role': 'assistant', 'event_type': 'chat.final', 'content': value,
                             '_part': {'record_id': 'final', 'part_idx': index, 'total_parts': 2}}})
         for index, value in enumerate(fragments)]
        + [_event({'status': 'done', 'has_more': True, 'next_cursor': 'older'})],
        [_event({'message': {'role': 'user', 'content': 'first'}}), _event({'status': 'done', 'has_more': False})],
    ]

    async def dispatch(_channel, **kwargs):
        calls.append(kwargs)
        frames = pages.pop(0)

        async def iterate(*args, **options):
            for frame in frames:
                yield frame

        return SimpleNamespace(iter_sse_frames=iterate), 'rid', 'session'

    monkeypatch.setattr(share_snapshot, 'dispatch_http_request', dispatch)
    request = Request({'type': 'http', 'headers': [(b'cookie', b'jiuwenswarm_auth=existing')], 'client': ('127.0.0.1', 1)})
    snapshot, _ = await share_snapshot.build_share_snapshot(request, channel, 'session', metadata={'title': 'Custom title'})
    assert snapshot['metadata']['title'] == 'Custom title'
    assert [item['role'] for item in snapshot['records']] == ['user', 'assistant']
    assert snapshot['records'][1]['content'] == long_text
    assert '_part' not in snapshot['records'][1]
    assert calls[0]['params']['cursor'] is None
    assert calls[1]['params']['cursor'] == 'older'
    assert calls[0]['headers']['cookie'] == 'jiuwenswarm_auth=existing'
    assert channel.unregister_request_outbound.await_count == 2


@pytest.mark.asyncio
async def test_snapshot_incomplete_stream_is_not_published(monkeypatch):
    channel = SimpleNamespace(unregister_request_outbound=AsyncMock())

    async def iterate(*args, **kwargs):
        yield _event({'message': {'role': 'assistant', 'content': 'partial'}})

    monkeypatch.setattr(share_snapshot, 'dispatch_http_request', AsyncMock(return_value=(
        SimpleNamespace(iter_sse_frames=iterate), 'rid', 'session')))
    request = Request({'type': 'http', 'headers': []})
    with pytest.raises(HTTPException, match='share_export_history_incomplete'):
        await share_snapshot.build_share_snapshot(request, channel, 'session', metadata={})
    assert channel.unregister_request_outbound.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('texts,expected', [
    (['  \n ', 'Useful question'], 'Useful question'),
    (['  ', '\n'], 'session'),
])
async def test_snapshot_title_skips_blank_user_messages(monkeypatch, texts, expected):
    records = [{'role': 'user', 'content': text} for text in reversed(texts)]
    monkeypatch.setattr(share_snapshot, '_history_page', AsyncMock(return_value=(records, {'has_more': False})))
    snapshot, _ = await share_snapshot.build_share_snapshot(
        Request({'type': 'http', 'headers': []}), object(), 'session', metadata={},
    )
    assert snapshot['metadata']['title'] == expected


@pytest.mark.asyncio
@pytest.mark.parametrize('fragments', [
    [(10, 11), (0, 2)],
    [(0, 2), (1, 3), (2, 3)],
])
async def test_snapshot_rejects_inconsistent_part_totals(monkeypatch, fragments):
    channel = SimpleNamespace(unregister_request_outbound=AsyncMock())

    async def iterate(*args, **kwargs):
        for index, total in fragments:
            yield _event({'message': {
                'content': 'fragment',
                '_part': {'record_id': 'final', 'part_idx': index, 'total_parts': total},
            }})

    monkeypatch.setattr(share_snapshot, 'dispatch_http_request', AsyncMock(return_value=(
        SimpleNamespace(iter_sse_frames=iterate), 'rid', 'session')))
    with pytest.raises(HTTPException, match='share_export_history_part_invalid'):
        await share_snapshot.build_share_snapshot(
            Request({'type': 'http', 'headers': []}), channel, 'session', metadata={},
        )
    assert channel.unregister_request_outbound.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('key,index,total', [
    ('', 0, 1), (None, 0, 1), ('final', '0', 1), ('final', 0, '1'),
    ('final', True, 1), ('final', 0, False), ('final', -1, 2), ('final', 1, 1),
])
async def test_snapshot_rejects_invalid_part_metadata(monkeypatch, key, index, total):
    channel = SimpleNamespace(unregister_request_outbound=AsyncMock())

    async def iterate(*args, **kwargs):
        yield _event({'message': {
            'role': 'assistant', 'content': 'invalid fragment',
            '_part': {'record_id': key, 'part_idx': index, 'total_parts': total},
        }})

    monkeypatch.setattr(share_snapshot, 'dispatch_http_request', AsyncMock(return_value=(
        SimpleNamespace(iter_sse_frames=iterate), 'rid', 'session')))
    request = Request({'type': 'http', 'headers': []})
    with pytest.raises(HTTPException, match='share_export_history_part_invalid') as caught:
        await share_snapshot.build_share_snapshot(request, channel, 'session', metadata={})
    assert caught.value.status_code == 502
    assert channel.unregister_request_outbound.await_count == 1

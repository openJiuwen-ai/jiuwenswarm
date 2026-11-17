# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Attachment size, rather than the selected transport, chooses pre-upload."""
import base64
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.gateway.channel_manager.web import app_web_handlers as handlers
from jiuwenswarm.gateway.routing import agent_http_bridge as bridge


@pytest.mark.asyncio
@pytest.mark.parametrize('native', [False, True])
@pytest.mark.parametrize('kind', ['document', 'image'])
@pytest.mark.parametrize('size', [8, 17])
async def test_attachment_budget_is_independent_of_transport(monkeypatch, native, kind, size):
    monkeypatch.setattr(bridge, 'E2A_PAYLOAD_MAX_BYTES', 16)
    client = SimpleNamespace(native_file_streaming=native)
    item = {'type': kind, 'filename': 'note.md' if kind == 'document' else 'image.png',
            'mimeType': 'text/markdown' if kind == 'document' else 'image/png',
            'base64Data': base64.b64encode(b'x' * size).decode()}
    persisted = {'type': kind, 'path': '/user/agent/sessions/s/uploads/item', '_persisted': True}
    upload = AsyncMock(return_value=persisted)
    if kind == 'document':
        monkeypatch.setattr(handlers, '_upload_document_item_via_http', upload)
        method, key = handlers._pre_persist_large_documents, 'documents'
    else:
        monkeypatch.setattr(handlers, '_upload_media_item_via_http', upload)
        method, key = handlers._pre_persist_large_media, 'media_items'
    result = await method({key: [item]}, session_id='s', agent_client=client, user_id='alice')
    if size <= 16:
        upload.assert_not_awaited()
        assert result[key] == [item]
    else:
        upload.assert_awaited_once()
        assert result[key] == [persisted]


@pytest.mark.asyncio
@pytest.mark.parametrize('native', [False, True])
@pytest.mark.parametrize('kind', ['document', 'image'])
async def test_failed_preupload_keeps_the_existing_inline_fallback(monkeypatch, native, kind):
    from jiuwenswarm.gateway.routing import e2a_proxy
    monkeypatch.setattr(bridge, 'E2A_PAYLOAD_MAX_BYTES', 16)
    monkeypatch.setattr(e2a_proxy, 'is_agentos_routing_client', lambda client: True)
    upload = AsyncMock(return_value=(False, {'error': 'upstream unavailable', 'code': 'SERVICE_UNAVAILABLE'}))
    monkeypatch.setattr(bridge, 'upload_file_bytes_via_e2a', upload)
    item = {'type': kind, 'filename': 'note.md' if kind == 'document' else 'image.png',
            'mimeType': 'text/markdown' if kind == 'document' else 'image/png',
            'base64Data': base64.b64encode(b'x' * 17).decode()}
    method, key = ((handlers._pre_persist_large_documents, 'documents') if kind == 'document'
                   else (handlers._pre_persist_large_media, 'media_items'))
    result = await method({key: [item]}, session_id='s',
                          agent_client=SimpleNamespace(native_file_streaming=native), user_id='alice')
    upload.assert_awaited_once()
    assert result[key] == [item]
    assert not result[key][0].get('_persisted')

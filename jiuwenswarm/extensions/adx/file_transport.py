# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Authenticated binary file transport over the native ADX data plane."""
from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from urllib.parse import quote

import httpx

from jiuwenswarm.extensions.adx.control_client import AdxControlError


logger = logging.getLogger(__name__)


async def _check_response(response: httpx.Response) -> None:
    if response.status_code < 300:
        return
    # Never relay an unbounded backend error body or authentication headers.
    body = bytearray()
    async for chunk in response.aiter_bytes(chunk_size=4096):
        body.extend(chunk[:4096 - len(body)])
        if len(body) >= 4096:
            break
    code = {400: 'BAD_REQUEST', 401: 'UNAUTHORIZED', 403: 'FORBIDDEN',
            404: 'NOT_FOUND', 416: 'BAD_REQUEST'}.get(response.status_code, 'SERVICE_UNAVAILABLE')
    detail = body.decode('utf-8', errors='replace')
    try:
        value = json.loads(detail)
    except ValueError:
        value = None
    error = value.get('error') if isinstance(value, dict) else None
    structured = error.get('code') if isinstance(error, dict) else value.get('error_code') if isinstance(value, dict) else None
    if str(structured or '').upper() == 'FILE_NOT_FOUND':
        code = 'file_not_found'
    elif isinstance(error, dict) and str(structured or '').upper() in ('NOT_FOUND', 'INSTANCE_NOT_FOUND'):
        code = 'instance_not_found'
    elif response.status_code == 400 and ('not found' in detail.lower() or 'no such file' in detail.lower()):
        code = 'file_not_found'
    raise AdxControlError(f'ADX file transfer returned HTTP {response.status_code}: {detail}', code=code)


@asynccontextmanager
async def file_request(control, environment: str, method: str, path: str, *, content=None, byte_range=None):
    """Keep the native response open until the caller finishes or cancels."""
    endpoint = 'upload' if method == 'POST' else 'download'
    url = control.config.ingress_url.rstrip('/') + '/direct/' + quote(environment, safe='') + '/' + endpoint
    headers = {'Authorization': 'Bearer ' + control.config.token, 'Accept-Encoding': 'identity'}
    if method == 'POST':
        headers['Content-Type'] = 'application/octet-stream'
    if byte_range:
        headers['Range'] = byte_range
    try:
        async with control.http.stream(method, url, params={'path': path, 'type': 'file'},
                                       headers=headers, content=content) as response:
            await _check_response(response)
            logger.info('[ADX] file.%s.native environment=%s status=%s', endpoint, environment, response.status_code)
            yield response
    except httpx.RequestError as exc:
        raise AdxControlError('ADX file transfer interrupted; outcome is unknown') from exc

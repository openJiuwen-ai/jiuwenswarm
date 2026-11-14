# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Bind prepared workspace settings to the current AgentServer node."""
from __future__ import annotations

import copy
import json
import os
from urllib.parse import urlsplit


def bind_adx_runtime(sandbox: dict | None, ingress_url: str, endpoint_file: str | None = None) -> dict:
    """Keep persisted tool settings and replace only deployment-owned connectivity."""
    if not isinstance(sandbox, dict) or sandbox.get('type') != 'adx-environment':
        raise ValueError('AgentOS workspace sandbox requires deployment migration before startup')
    parsed = urlsplit(ingress_url)
    if (parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username
            or parsed.password or parsed.path not in ('', '/') or parsed.query or parsed.fragment):
        raise ValueError('managed ADX ingress must be an HTTP(S) origin')
    result = copy.deepcopy(sandbox)
    connection = result.get('connection') or {}
    if not isinstance(connection, dict):
        raise ValueError('sandbox.connection must be a mapping')
    dynamic = {'server_address', 'gateway_address', 'token', 'token_provider',
               'use_tls', 'gateway_use_tls', 'verify_tls'}
    connection = {key: value for key, value in connection.items() if key not in dynamic}
    use_tls = parsed.scheme == 'https'
    connection.update(use_tls=use_tls, verify_tls=use_tls)
    result.update(url=ingress_url, connection=connection)
    result.pop('endpoint_file', None)
    if endpoint_file:
        result['endpoint_file'] = endpoint_file
    if isinstance(result.get('env'), dict):
        credentials = {'ADX_TOKEN', 'ADX_GATEWAY_TOKEN', 'JIUWENSWARM_ADX_API_TOKEN'}
        result['env'] = {key: value for key, value in result['env'].items() if key not in credentials}
    return result


def bind_runtime_sandbox(sandbox: dict | None, deployment: dict) -> dict:
    """Apply AgentOS connectivity, retaining the existing merge for other backends."""
    if deployment.get('type') != 'adx-environment':
        return {**deployment, **sandbox} if isinstance(sandbox, dict) else deployment
    context_file = os.getenv('JIUWENSWARM_ADX_NODE_CONTEXT_FILE', '').strip()
    if context_file:
        with open(context_file, encoding='utf-8') as stream:
            node = json.load(stream)
        ingress = node.get('ingress_url')
        if not isinstance(ingress, str) or not ingress:
            raise ValueError('ADX node context requires an ingress_url')
    else:
        ingress = os.getenv('JIUWENSWARM_ADX_NODE_INGRESS_URL', '').strip()
    if not ingress:
        raise ValueError('Managed ADX tools require launcher-injected JIUWENSWARM_ADX_NODE_INGRESS_URL')
    return bind_adx_runtime(sandbox, ingress, context_file or None)

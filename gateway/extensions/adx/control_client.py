# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Adapt the existing Gateway sandbox contract to public Environment APIs."""
from __future__ import annotations

import asyncio
import base64
import copy
import hashlib
import json
import uuid
from typing import Any
from urllib.parse import quote, urljoin

import httpx

from gateway.extensions.adx.config import AdxRouterConfig
from jiuwenswarm.extensions.yuanrong_frontend_client import (
    SandboxInfo, YuanrongAgentApiError, YuanrongAgentTimeoutError,
)


class AdxControlError(YuanrongAgentApiError):
    """Environment failure compatible with the existing Gateway lifecycle contract."""
    def __init__(self, message: str, *, code: str = 'SERVICE_UNAVAILABLE') -> None:
        super().__init__(message)
        self.code = code


class AdxCreateTimeout(AdxControlError, YuanrongAgentTimeoutError):
    """An unknown create outcome must retain its instance and request identities."""


class AdxControlClient:
    def __init__(self, config: AdxRouterConfig, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.config = config
        self.agent_namespace = config.router.agent_namespace
        self.frontend_endpoint = config.ingress_url
        self.server_ready = False
        self.http = httpx.AsyncClient(
            timeout=config.connect_timeout_seconds, follow_redirects=False, transport=transport,
        )
        self._creates: dict[str, tuple[str, str]] = {}
        self._deletes: dict[str, str] = {}

    async def connect(self, uri: str) -> None:
        del uri
        self.server_ready = True

    async def disconnect(self) -> None:
        self.server_ready = False
        await self.close()

    async def close(self) -> None:
        await self.http.aclose()

    @staticmethod
    def set_or_update_server_config(*, config: dict, env: dict | None = None) -> None:
        # The business router owns server defaults and includes them in launch specs.
        del config, env

    async def _request(self, method: str, path: str, *, body: dict | None = None,
                       request_id: str | None = None, params: dict | None = None,
                       missing_ok: bool = False) -> Any:
        headers = {'Authorization': 'Bearer ' + self.config.token}
        if request_id:
            headers['X-Request-Id'] = request_id
        try:
            response = await self.http.request(method, urljoin(self.config.ingress_url, path),
                                               headers=headers, json=body, params=params)
        except httpx.RequestError as exc:
            error = AdxCreateTimeout if method == 'POST' and path.endswith('/environments') else AdxControlError
            raise error('ADX operation outcome is unknown; reuse the same instance') from exc
        if missing_ok and response.status_code == 404:
            return None
        try:
            value = response.json()
        except ValueError as exc:
            raise AdxControlError('Invalid ADX response') from exc
        if response.status_code >= 300:
            error = value.get('error') if isinstance(value, dict) else None
            code = error.get('code', 'SERVICE_UNAVAILABLE') if isinstance(error, dict) else 'SERVICE_UNAVAILABLE'
            raise AdxControlError(f'ADX {method} returned HTTP {response.status_code}', code=code)
        if isinstance(value, dict) and 'data' in value and 'code' in value:
            if value['code'] not in (200, 201):
                raise AdxControlError('ADX operation failed')
            if value['data'] is None:
                return None
            try:
                return json.loads(base64.b64decode(value['data'], validate=True))
            except (ValueError, TypeError) as exc:
                raise AdxControlError('Invalid ADX response envelope') from exc
        return value

    async def _lookup(self, instance: str) -> dict | None:
        value = await self._request('GET', '/api/instances', params={'instance_id': instance}, missing_ok=True)
        if value is None or value == []:
            return None
        if not isinstance(value, list) or len(value) != 1:
            raise AdxControlError('ADX Environment identity or tenant mismatch', code='CONFLICT')
        entry = value[0]
        if (not isinstance(entry, dict) or entry.get('id') != instance
                or entry.get('tenant_id') != self.config.tenant):
            raise AdxControlError('ADX Environment identity or tenant mismatch', code='CONFLICT')
        return entry

    async def get_agent_info(self, instance_id: str, *, trace_id: str | None = None) -> dict:
        del trace_id
        value = await self._lookup(instance_id)
        if value is None:
            raise AdxControlError('ADX Environment not found', code='NOT_FOUND')
        return value

    async def wait_until_running(self, instance_id: str, *, trace_id: str | None = None) -> dict:
        del trace_id
        try:
            async with asyncio.timeout(self.config.connect_timeout_seconds):
                while True:
                    value = await self.get_agent_info(instance_id)
                    if value.get('status') in ('running', 'paused', 'pausing', 'resuming'):
                        return value
                    if value.get('status') in ('failed', 'deleted', 'deleting'):
                        raise AdxControlError('ADX Environment is not available')
                    await asyncio.sleep(self.config.router.probes.wait_running_interval_seconds)
        except TimeoutError as exc:
            raise AdxCreateTimeout('Timed out waiting for the ADX Environment to become available') from exc

    def _creation_body(self, namespace: str, name: str, workspace: str, runtime_spec: dict,
                       env_vars: dict | None, mounts: list | None) -> dict:
        spec = copy.deepcopy(runtime_spec)
        rootfs = spec.get('rootfs') or {}
        image = rootfs.get('imageurl') or rootfs.get('image_url')
        if name.endswith('+jiuwenswarm') and image == 'jiuwenswarm-agent-runtime:latest':
            image = self.config.builtin_image
        if not image:
            raise ValueError('runtime_spec.rootfs.imageurl is required')
        body = {'namespace': namespace, 'name': name,
                'rootfs': {'type': 'image', 'imageurl': image, 'runtime': self.config.runtime_class},
                'inheritEntrypoint': True, 'cpu': spec.get('cpu', 1000), 'memory': spec.get('memory', 2048),
                'env': dict(env_vars or {}),
                'mounts': [{'type': 'bind', 'host_path': workspace, 'target': self.config.workspace_target,
                            'options': ['rbind', 'rw']}]}
        process = {}
        if spec.get('cmds') is not None:
            process['commands'] = spec['cmds']
        if rootfs.get('user'):
            process['user'] = rootfs['user']
        if spec.get('code_path'):
            process['cwd'] = spec['code_path']
        if process:
            body['process'] = process
        for mount in mounts or []:
            body['mounts'].append({'type': 'bind', 'host_path': mount['source'], 'target': mount['target'],
                                   'options': ['rbind', 'ro' if mount.get('readonly') else 'rw']})
        ports = []
        for value in rootfs.get('ports', []):
            text = str(value)
            protocol, _, port = text.rpartition(':')
            if protocol and protocol.lower() != 'tcp':
                raise ValueError('ADX only supports TCP workload ports')
            ports.append(str(int(port or text)))
        probes = spec.get('probes') or {}
        startup = probes.get('startup') or probes.get('readiness')
        if startup:
            readiness = copy.deepcopy(startup)
            attempts = readiness.pop('failureThreshold', 1)
            body['initCallTimeoutSeconds'] = max(30, readiness.get('initialDelaySeconds', 0)
                + readiness.get('periodSeconds', 2) * attempts + readiness.get('timeoutSeconds', 1))
            body['readinessProbe'] = readiness
        if probes.get('liveness'):
            body['healthcheckProbe'] = copy.deepcopy(probes['liveness'])
        for probe in [body.get('readinessProbe'), body.get('healthcheckProbe')]:
            if probe:
                action = probe.get('tcpSocket') or probe.get('httpGet')
                if action and 'port' in action:
                    ports.append(str(action['port']))
        body['ports'] = sorted(set(ports))
        return body

    async def create_sandbox(self, *, namespace: str, name: str, workspace: str, runtime_spec: dict,
                             env_vars: dict | None = None, mounts: list | None = None,
                             trace_id: str | None = None) -> SandboxInfo:
        del trace_id
        instance = f'{namespace}-{name}'
        existing = await self._lookup(instance)
        if existing is not None:
            return SandboxInfo(instance, metadata=existing)
        if instance in self._deletes:
            # A previous DELETE lost its response. Absence is the confirmation
            # that permits a new create identity for this stable Environment ID.
            self._deletes.pop(instance, None)
            self._creates.pop(instance, None)
        body = self._creation_body(namespace, name, workspace, runtime_spec, env_vars, mounts)
        digest = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        operation, original_digest = self._creates.setdefault(instance, ('create-' + uuid.uuid4().hex, digest))
        if digest != original_digest:
            raise AdxControlError('Create specification changed while its outcome is unknown', code='CONFLICT')
        result = await self._request('POST', '/api/environment/v1/environments', body=body, request_id=operation)
        if not isinstance(result, dict) or result.get('environmentId') != instance:
            raise AdxControlError('ADX create returned an unexpected Environment ID')
        return SandboxInfo(instance, metadata=result)

    async def delete_sandbox(self, sandbox_id: str, *, trace_id: str | None = None) -> None:
        del trace_id
        operation = self._deletes.setdefault(sandbox_id, 'delete-' + uuid.uuid4().hex)
        await self._request(
            'DELETE', f'/api/environment/v1/environments/{quote(sandbox_id, safe="")}',
            request_id=operation, missing_ok=True,
        )
        self._deletes.pop(sandbox_id, None)
        self._creates.pop(sandbox_id, None)

    async def invoke(self, environment: str, action: str, args: dict) -> dict:
        rid = 'invoke-' + uuid.uuid4().hex
        try:
            response = await self.http.post(
                urljoin(self.config.ingress_url, f'/direct/{quote(environment, safe="")}/invoke'),
                headers={'Authorization': 'Bearer ' + self.config.token, 'X-ADX-Request-ID': rid},
                json={'action': action, 'args': args, 'requestId': rid},
            )
        except httpx.RequestError as exc:
            raise AdxControlError('ADX operation outcome is unknown; operation was not replayed') from exc
        try:
            result = response.json()
        except ValueError as exc:
            raise AdxControlError('Invalid ADX operation response') from exc
        if not isinstance(result, dict):
            raise AdxControlError('Invalid ADX operation response')
        if result.get('error') and response.status_code in (200, 400, 404):
            message = str(result['error'])
            missing = 'not found' in message.lower() or 'no such file' in message.lower()
            raise AdxControlError(message, code='NOT_FOUND' if missing else 'INTERNAL_ERROR')
        if response.status_code >= 300:
            raise AdxControlError(f'ADX operation returned HTTP {response.status_code}')
        return result

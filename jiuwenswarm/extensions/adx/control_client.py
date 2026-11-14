# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Adapt the existing Gateway sandbox contract to public Environment APIs."""
from __future__ import annotations

import asyncio
import base64
import copy
import hashlib
import json
import logging
import math
import uuid
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import quote, urljoin

import httpx

from jiuwenswarm.extensions.adx.config import AdxRouterConfig
from jiuwenswarm.extensions.yuanrong_frontend_client import (
    SandboxInfo, YuanrongAgentApiError, YuanrongAgentTimeoutError,
    AGENT_GENERATION_ENV, YuanrongAgentNotFoundError, YuanrongAgentNotVisibleError,
    YuanrongAgentGenerationMismatchError, YuanrongAgentFailedError, YuanrongAgentCreateUncertainError,
    TRACE_ID_HEADER, apply_trace_header, bind_southbound_trace_id,
)
from jiuwenswarm.gateway.telemetry import inject_request_headers


logger = logging.getLogger(__name__)


def trace_headers(headers: dict[str, str], trace_id: str | None = None) -> dict[str, str]:
    """Propagate business tracing independently of ADX operation identities."""
    carrier = inject_request_headers(headers)
    return apply_trace_header(carrier, carrier.get(TRACE_ID_HEADER) or trace_id)


def _resolve_trace_id(trace_id: str | None = None) -> str:
    return bind_southbound_trace_id(inject_request_headers({}).get(TRACE_ID_HEADER) or trace_id)


class AdxControlError(YuanrongAgentApiError):
    """Environment failure compatible with the existing Gateway lifecycle contract."""
    def __init__(self, message: str, *, code: str = 'SERVICE_UNAVAILABLE',
                 outcome: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.outcome = outcome


class AdxCreateTimeout(AdxControlError, YuanrongAgentTimeoutError):
    """An unknown create outcome must retain its instance and request identities."""


class AdxCreateUncertain(AdxControlError, YuanrongAgentCreateUncertainError):
    """Preserve the attempted generation when ADX cannot confirm creation."""


class AdxNotFound(AdxControlError, YuanrongAgentNotFoundError):
    """Confirmed native absence satisfies the Gateway retirement fence."""


class AdxControlClient:
    def __init__(self, config: AdxRouterConfig, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.config = config
        self.agent_namespace = config.router.agent_namespace
        self.frontend_endpoint = config.ingress_url
        self.server_ready = False
        self.agent_idle_timeout_seconds = config.router.sandbox_idle_timeout_seconds
        self.http = httpx.AsyncClient(
            timeout=config.connect_timeout_seconds, follow_redirects=False, transport=transport,
        )
        self._creates: dict[str, tuple[str, str]] = {}
        self._confirmed_creates: set[str] = set()
        self._terminal_creates: set[str] = set()
        self._deletes: dict[str, str] = {}
        self._network_locks: dict[str, asyncio.Lock] = {}
        self._network_applied: dict[str, str] = {}
        self._network_pending: dict[str, tuple[str, str, dict]] = {}

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
                       request_id: str | None = None, operation_id: str | None = None, params: dict | None = None,
                       missing_ok: bool = False, trace_id: str | None = None) -> Any:
        headers = trace_headers({'Authorization': 'Bearer ' + self.config.token}, trace_id)
        if request_id:
            headers['X-Request-Id'] = request_id
        if operation_id:
            headers['X-ADX-Request-ID'] = operation_id
        try:
            response = await self.http.request(method, urljoin(self.config.ingress_url, path),
                                               headers=headers, json=body, params=params)
        except httpx.RequestError as exc:
            error = AdxCreateTimeout if method == 'POST' and path.endswith('/environments') else AdxControlError
            raise error('ADX operation outcome is unknown; reuse the same instance', outcome='unknown') from exc
        if missing_ok and response.status_code == 404:
            return None
        try:
            value = response.json()
        except ValueError as exc:
            raise AdxControlError('Invalid ADX response') from exc
        if response.status_code >= 300:
            error = value.get('error') if isinstance(value, dict) else None
            code = error.get('code', 'SERVICE_UNAVAILABLE') if isinstance(error, dict) else 'SERVICE_UNAVAILABLE'
            outcome = error.get('outcome') if isinstance(error, dict) else None
            message = value.get('message') if isinstance(value, dict) else None
            response_request_id = error.get('requestId') if isinstance(error, dict) else None
            detail = f'ADX {method} returned HTTP {response.status_code}'
            if isinstance(message, str) and message.strip():
                detail = f'{detail}: {message}'
            correlation_id = response_request_id or request_id
            if correlation_id:
                detail = f'{detail} (request ID: {correlation_id})'
            raise AdxControlError(detail, code=code, outcome=outcome)
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

    async def _lookup(self, instance: str, *, trace_id: str | None = None) -> dict | None:
        value = await self._request('GET', '/api/instances', params={'instance_id': instance},
                                    missing_ok=True, trace_id=trace_id)
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
        trace_id = _resolve_trace_id(trace_id)
        value = await self._lookup(instance_id, trace_id=trace_id)
        if value is None:
            raise AdxNotFound('ADX Environment not found', code='NOT_FOUND')
        return value

    async def _instance_generation(self, instance: str, value: dict, *, trace_id: str | None = None) -> str | None:
        generation = (value.get('env_vars') or {}).get(AGENT_GENERATION_ENV)
        if generation is not None or value.get('status') != 'running':
            return generation
        # Tenant instance views may redact env_vars. Read only the lifecycle
        # marker from this exact running Environment, without an admin key.
        try:
            result = await self.invoke(instance, 'cmd.run', {
                'cmd': f'printf %s "${AGENT_GENERATION_ENV}"', 'timeout': 5,
            }, trace_id=trace_id)
        except AdxControlError:
            return None
        if result.get('exit_code') != 0:
            return None
        generation = result.get('stdout')
        return generation.strip() or None if isinstance(generation, str) else None

    async def wait_until_running(self, instance_id: str, *, trace_id: str | None = None,
                                 expected_generation: str | None = None, deadline: float | None = None) -> dict:
        trace_id = _resolve_trace_id(trace_id)
        loop = asyncio.get_running_loop()
        expires = loop.time() + self.config.connect_timeout_seconds
        if deadline is not None:
            expires = min(expires, deadline)
        observed = False
        try:
            async with asyncio.timeout_at(expires):
                while True:
                    try:
                        value = await self.get_agent_info(instance_id, trace_id=trace_id)
                    except AdxNotFound:
                        if expected_generation is None:
                            raise
                        await asyncio.sleep(self.config.router.probes.wait_running_interval_seconds)
                        continue
                    observed = True
                    if expected_generation is not None:
                        actual = await self._instance_generation(instance_id, value, trace_id=trace_id)
                        if actual is not None and actual != expected_generation:
                            raise YuanrongAgentGenerationMismatchError('ADX Environment generation mismatch')
                        if actual is None:
                            await asyncio.sleep(self.config.router.probes.wait_running_interval_seconds)
                            continue
                    if value.get('status') in ('running', 'paused', 'pausing', 'resuming'):
                        return value
                    if value.get('status') in ('failed', 'deleted', 'deleting'):
                        if expected_generation is not None:
                            raise YuanrongAgentFailedError('ADX replacement Environment is not available')
                        raise AdxControlError('ADX Environment is not available')
                    await asyncio.sleep(self.config.router.probes.wait_running_interval_seconds)
        except TimeoutError as exc:
            if expected_generation is not None and not observed:
                raise YuanrongAgentNotVisibleError('ADX replacement Environment is not visible yet') from exc
            raise AdxCreateTimeout('Timed out waiting for the ADX Environment to become available') from exc

    def _creation_body(self, namespace: str, name: str, workspace: str, runtime_spec: dict,
                       env_vars: dict | None, mounts: list | None, *, builtin: bool | None = None) -> dict:
        spec = copy.deepcopy(runtime_spec)
        rootfs = spec.get('rootfs') or {}
        image = rootfs.get('imageurl') or rootfs.get('image_url')
        if builtin is None:
            builtin = name.endswith('+jiuwenswarm') and image == 'jiuwenswarm-agent-runtime:latest'
        if builtin:
            image = self.config.builtin_image
        body = {'namespace': namespace, 'name': name,
                'cpu': spec.get('cpu', 1000), 'memory': spec.get('memory', 2048),
                'env': dict(env_vars or {}),
                'mounts': [{'type': 'bind', 'host_path': workspace, 'target': self.config.workspace_target,
                            'options': ['rbind', 'rw']}]}
        if image:
            if self.config.local_images is not None:
                template = self.config.local_images.get(image)
                if not template:
                    raise ValueError(f'Image {image} has no prepared local rootfs; offline deployment must prepare it first')
                body['rootfs'] = {'type': 'template', 'name': template, 'runtime': self.config.runtime_class}
            else:
                body['rootfs'] = {'type': 'image', 'imageurl': image, 'runtime': self.config.runtime_class}
            body['inheritEntrypoint'] = True
        if self.config.runtime_class == 'runsc':
            timeout = self.agent_idle_timeout_seconds
            if not math.isfinite(timeout):
                raise ValueError('Agent idle timeout must be finite')
            seconds = math.ceil(max(0, timeout))
            if seconds >= 1 << 64:
                raise ValueError('Agent idle timeout exceeds uint64')
            body['idleTimeoutSeconds'] = seconds
        if self.config.restart_policy is not None:
            body['restartPolicy'] = dict(self.config.restart_policy)
        process = {}
        if spec.get('cmds') is not None:
            process['commands'] = spec['cmds']
        if rootfs.get('user'):
            process['user'] = rootfs['user']
        if spec.get('code_path'):
            process['cwd'] = spec['code_path']
        # Some deployments own the builtin command and identity in the image
        # binding. Only that explicit mode omits per-create process overrides.
        if process and not (builtin and image and self.config.builtin_process_from_image):
            body['process'] = process
        for mount in [*rootfs.get('mounts', []), *(mounts or [])]:
            binding = {'type': 'bind', 'host_path': mount['source'], 'target': mount['target'],
                       'options': ['rbind', 'ro' if mount.get('readonly') else 'rw']}
            if binding not in body['mounts']:
                body['mounts'].append(binding)
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
        if builtin and self.config.builtin_launch:
            launch = self.config.builtin_launch
            if self.config.runtime_class == 'runsc' and 'workspace_mount_options' in launch:
                options = launch['workspace_mount_options']
                allowed = {'rw', 'ro', 'nosuid', 'nodev', 'noexec', 'disable_file_handle_sharing'}
                error = 'builtin workspace_mount_options must be unique writable bind options'
                if not isinstance(options, list) or not options:
                    raise ValueError(error)
                if any(not isinstance(value, str) or value not in allowed for value in options):
                    raise ValueError(error)
                if len(set(options)) != len(options) or 'rw' not in options or 'ro' in options:
                    raise ValueError(error)
                body['mounts'][0]['options'] = ['rbind', *options]
            body['process'] = copy.deepcopy(launch['process'])
            body['mounts'].extend(copy.deepcopy(launch.get('mounts', [])))
            body['env'].update(copy.deepcopy(launch.get('env', {})))
            if 'network' in launch:
                from jiuwenswarm.extensions.adx.network import agent_policy, public_policy, workspace_model_origins
                network = launch['network']
                body['network'] = public_policy(agent_policy({'ingress_url': network['ingress_url'], 'ingress_policy_peer': network.get('ingress_policy_peer')},
                    workspace_model_origins(workspace), network.get('allowed_internal_origins', []),
                    profile=network.get('profile', 'N2'), blocked_cidrs=network.get('blocked_cidrs', [])))
        if builtin:
            body['env']['HOME'] = self.config.workspace_target
            body['env']['JIUWENSWARM_HOME'] = self.config.workspace_target
            # Both Supervisor and runsc AgentServers execute authenticated tool
            # SDK calls. Never put this credential into custom agents or tools.
            body['env']['JIUWENSWARM_ADX_API_TOKEN'] = self.config.token
            if self.config.workspace_config_template:
                target = '/run/agentos/workspace-config.yaml'
                body['mounts'].append({'type': 'bind', 'host_path': self.config.workspace_config_template,
                                       'target': target, 'options': ['ro']})
                body['mounts'].append({'type': 'bind',
                    'host_path': str(PurePosixPath(self.config.workspace_config_template).with_suffix('.env')),
                    'target': '/run/agentos/workspace-config.env', 'options': ['ro']})
                body['env']['JIUWENSWARM_AGENTOS_CONFIG_TEMPLATE'] = target
        return body

    async def ensure_builtin_network(self, instance: str, workspace: str) -> None:
        launch = self.config.builtin_launch or {}
        if 'network' not in launch:
            return
        from jiuwenswarm.extensions.adx.network import agent_policy, public_policy, workspace_model_origins
        network = launch['network']
        desired = public_policy(agent_policy({'ingress_url': network['ingress_url'], 'ingress_policy_peer': network.get('ingress_policy_peer')},
            workspace_model_origins(workspace), network.get('allowed_internal_origins', []),
            profile=network.get('profile', 'N2'), blocked_cidrs=network.get('blocked_cidrs', [])))
        digest = hashlib.sha256(json.dumps(desired, sort_keys=True).encode()).hexdigest()
        async with self._network_locks.setdefault(instance, asyncio.Lock()):
            # Reconcile an ambiguous previous replacement before submitting a
            # different policy. A model switch must not reorder old mutations.
            if instance in self._network_pending:
                await self._apply_pending_network(instance)
            if self._network_applied.get(instance) == digest:
                return
            self._network_pending[instance] = (digest, 'network-' + uuid.uuid4().hex, desired)
            await self._apply_pending_network(instance)

    async def _apply_pending_network(self, instance: str) -> None:
        digest, operation, policy = self._network_pending[instance]
        try:
            await self._request('PUT', f'/api/environment/v1/environments/{quote(instance, safe="")}/network',
                                body=policy, request_id=operation, operation_id=operation)
        except AdxControlError as error:
            if error.outcome in ('terminal', 'not_started') or error.code in ('INVALID_ARGUMENT', 'PERMISSION_DENIED', 'NOT_FOUND'):
                self._network_pending.pop(instance, None)
            raise
        self._network_applied[instance] = digest
        self._network_pending.pop(instance, None)

    async def create_sandbox(self, *, namespace: str, name: str, workspace: str, runtime_spec: dict,
                             env_vars: dict | None = None, mounts: list | None = None,
                             trace_id: str | None = None, builtin: bool | None = None) -> SandboxInfo:
        trace_id = _resolve_trace_id(trace_id)
        instance = f'{namespace}-{name}'
        existing = await self._lookup(instance, trace_id=trace_id)
        if existing is not None:
            expected_generation = (env_vars or {}).get(AGENT_GENERATION_ENV)
            if (expected_generation is not None
                    and await self._instance_generation(instance, existing, trace_id=trace_id) != expected_generation):
                raise AdxControlError('ADX Environment name belongs to another Agent generation',
                                      code='CONFLICT', outcome='not_started')
            return await self._ready_sandbox_info(instance, trace_id=trace_id)
        if (instance in self._deletes or instance in self._confirmed_creates
                or instance in self._terminal_creates):
            # Confirmed retirement or terminal failure plus absence permits a
            # new attempt. Unknown outcomes retain their original identity.
            self._deletes.pop(instance, None)
            self._creates.pop(instance, None)
            self._confirmed_creates.discard(instance)
            self._terminal_creates.discard(instance)
        body = self._creation_body(namespace, name, workspace, runtime_spec, env_vars, mounts, builtin=builtin)
        digest = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        operation, original_digest = self._creates.setdefault(instance, ('create-' + uuid.uuid4().hex, digest))
        if digest != original_digest:
            raise AdxControlError('Create specification changed while its outcome is unknown', code='CONFLICT')
        try:
            result = await self._request('POST', '/api/environment/v1/environments', body=body,
                                         request_id=operation, trace_id=trace_id)
        except AdxControlError as error:
            if error.outcome in ('terminal', 'not_started') or error.code in (
                'INVALID_ARGUMENT', 'PERMISSION_DENIED', 'UNAUTHENTICATED', 'NOT_FOUND',
            ):
                self._terminal_creates.add(instance)
                raise
            raise AdxCreateUncertain(str(error), code=error.code, outcome='unknown') from error
        if not isinstance(result, dict) or result.get('environmentId') != instance:
            raise AdxCreateUncertain('ADX create returned an unexpected Environment ID', outcome='unknown')
        return await self._ready_sandbox_info(instance, trace_id=trace_id)

    async def _ready_sandbox_info(self, instance: str, *, trace_id: str | None = None) -> SandboxInfo:
        # Registry requires placement on the initial POST. Read the actual ADX
        # runtime before publishing the sandbox to the business router.
        trace_id = _resolve_trace_id(trace_id)
        view = await self.wait_until_running(instance, trace_id=trace_id)
        self._confirmed_creates.add(instance)
        metadata = {**view, 'node': view.get('node_ip') or '',
                    'address': view.get('sandbox_ip') or '', 'trace_id': trace_id}
        return SandboxInfo(instance, metadata=metadata)

    async def delete_sandbox(self, sandbox_id: str, *, trace_id: str | None = None) -> None:
        trace_id = _resolve_trace_id(trace_id)
        # Keep policy results from repopulating the cache after deletion.
        async with self._network_locks.setdefault(sandbox_id, asyncio.Lock()):
            operation = self._deletes.setdefault(sandbox_id, 'delete-' + uuid.uuid4().hex)
            await self._request(
                'DELETE', f'/api/environment/v1/environments/{quote(sandbox_id, safe="")}',
                request_id=operation, missing_ok=True, trace_id=trace_id,
            )
            self._network_applied.pop(sandbox_id, None)
            self._network_pending.pop(sandbox_id, None)
            logger.info('[ADX] environment.delete.ok instance_id=%s request_id=%s trace_id=%s',
                        sandbox_id, operation, trace_id)
            self._deletes.pop(sandbox_id, None)
            self._creates.pop(sandbox_id, None)
            self._confirmed_creates.discard(sandbox_id)
            self._terminal_creates.discard(sandbox_id)

    async def invoke(self, environment: str, action: str, args: dict, *, trace_id: str | None = None) -> dict:
        rid = 'invoke-' + uuid.uuid4().hex
        try:
            response = await self.http.post(
                urljoin(self.config.ingress_url, f'/direct/{quote(environment, safe="")}/invoke'),
                headers=trace_headers({'Authorization': 'Bearer ' + self.config.token, 'X-ADX-Request-ID': rid}, trace_id),
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
            error = result['error']
            message = str(error.get('message') or result.get('message') or error) if isinstance(error, dict) else str(error)
            error_code = str(error.get('code') or '') if isinstance(error, dict) else str(result.get('error_code') or '')
            if error_code.upper() == 'FILE_NOT_FOUND':
                code = 'file_not_found'
            elif isinstance(error, dict) and error_code.upper() in ('NOT_FOUND', 'INSTANCE_NOT_FOUND'):
                code = 'instance_not_found'
            elif (action.startswith('file.') and response.status_code in (200, 400)
                  and ('not found' in message.lower() or 'no such file' in message.lower())):
                # Older Execd file actions report path errors without a structured code.
                code = 'file_not_found'
            else:
                code = error_code or 'INTERNAL_ERROR'
            raise AdxControlError(message, code=code)
        if response.status_code >= 300:
            raise AdxControlError(f'ADX operation returned HTTP {response.status_code}')
        return result

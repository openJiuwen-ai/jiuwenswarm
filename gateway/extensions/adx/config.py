# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Deployment settings for Gateway-owned ADX Environments."""
from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from gateway.extensions.agentos.agentos_router.config import (
    RouterConfig,
)
from gateway.extensions.agentos.agentos_router.config import (
    load_router_config as load_agentos_config,
)


@dataclass(frozen=True)
class AdxRouterConfig:
    ingress_url: str
    tenant: str
    token: str = field(repr=False)
    router: RouterConfig = field(repr=False)
    builtin_image: str = 'jiuwenswarm-agent-runtime:latest'
    runtime_class: str = 'runc'
    workspace_target: str = '/home/agentos'
    ssh_user: str = 'agentos'
    ssh_known_hosts: str = ''
    restart_policy: dict[str, int] | None = None
    connect_timeout_seconds: float = 60.0
    builtin_process_from_image: bool = False


def adx_router_selected(config: dict) -> bool:
    gateway = config.get('gateway') or {}
    client = gateway.get('agent_client') or {}
    return str(client.get('type') or '').strip().lower() == 'adx_router'


def _text_setting(settings: dict, key: str, default: str | None = None) -> str:
    value = settings.get(key, default)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f'gateway.adx.{key} must be a nonempty value')
    if value != value.strip() or any(ord(char) < 32 for char in value):
        raise ValueError(f'gateway.adx.{key} must be a nonempty value')
    return value


def _restart_policy(value: object) -> dict[str, int] | None:
    if value is None:
        return None
    error = 'gateway.adx.restart_policy requires attempts and ordered positive backoff seconds'
    fields = {'maxAttempts', 'initialBackoffSeconds', 'maxBackoffSeconds'}
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError(error)
    for number in value.values():
        if isinstance(number, bool) or not isinstance(number, int) or number <= 0:
            raise ValueError(error)
    if value['maxAttempts'] > 4294967295 or value['maxBackoffSeconds'] > 18446744073709551615:
        raise ValueError(error)
    if value['initialBackoffSeconds'] > value['maxBackoffSeconds']:
        raise ValueError(error)
    return dict(value)


def load_router_config(config: dict) -> AdxRouterConfig:
    settings = (config.get('gateway') or {}).get('adx') or {}
    if not isinstance(settings, dict) or settings.keys() - {
        'ingress_url', 'tenant', 'token_env', 'builtin_image', 'runtime_class', 'workspace_target',
        'connect_timeout_seconds', 'ssh_user', 'ssh_known_hosts', 'restart_policy',
        'builtin_process_from_image',
    }:
        raise ValueError('gateway.adx contains invalid settings')
    url = settings.get('ingress_url', '')
    origin_error = 'gateway.adx.ingress_url must be an HTTPS origin'
    if not isinstance(url, str) or any(ord(char) < 33 for char in url):
        raise ValueError(origin_error)
    parsed = urlsplit(url)
    if parsed.scheme != 'https' or not parsed.hostname:
        raise ValueError(origin_error)
    if parsed.username is not None or parsed.password is not None:
        raise ValueError(origin_error)
    if parsed.path not in ('', '/') or '?' in url or '#' in url:
        raise ValueError(origin_error)
    parsed.port
    env_name = settings.get('token_env')
    token = os.environ.get(env_name, '') if isinstance(env_name, str) else ''
    if not token or not token.isascii() or any(char.isspace() for char in token):
        raise ValueError('Missing or invalid ADX credential environment variable')
    timeout = float(settings.get('connect_timeout_seconds', 60))
    if not math.isfinite(timeout) or not 0 < timeout <= 86400:
        raise ValueError('gateway.adx.connect_timeout_seconds must be in (0, 86400]')
    tenant = _text_setting(settings, 'tenant')
    builtin_image = _text_setting(settings, 'builtin_image', 'jiuwenswarm-agent-runtime:latest')
    builtin_process_from_image = settings.get('builtin_process_from_image', False)
    if not isinstance(builtin_process_from_image, bool):
        raise ValueError('gateway.adx.builtin_process_from_image must be a boolean')
    runtime_class = _text_setting(settings, 'runtime_class', 'runc')
    workspace_target = _text_setting(settings, 'workspace_target', '/home/agentos')
    if not workspace_target.startswith('/') or any(part in ('.', '..') for part in workspace_target.split('/')):
        raise ValueError('gateway.adx.workspace_target must be an absolute canonical path')
    ssh_user = _text_setting(settings, 'ssh_user', 'agentos')
    if not ssh_user.isascii() or any(not (char.isalnum() or char in '_-.') for char in ssh_user):
        raise ValueError('gateway.adx.ssh_user must be a valid username')
    ssh_known_hosts = settings.get('ssh_known_hosts', '')
    if not isinstance(ssh_known_hosts, str) or any(ord(char) < 32 for char in ssh_known_hosts):
        raise ValueError('gateway.adx.ssh_known_hosts must be a local known_hosts path')
    restart_policy = _restart_policy(settings.get('restart_policy'))
    return AdxRouterConfig(
        ingress_url=url.rstrip('/'), tenant=tenant, token=token,
        router=load_agentos_config(config, require_frontend_endpoint=False),
        builtin_image=builtin_image, runtime_class=runtime_class,
        builtin_process_from_image=builtin_process_from_image,
        workspace_target=workspace_target, connect_timeout_seconds=timeout, ssh_user=ssh_user,
        ssh_known_hosts=ssh_known_hosts, restart_policy=restart_policy,
    )

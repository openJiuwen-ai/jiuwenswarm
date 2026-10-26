# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Deployment settings for Gateway-owned ADX Environments."""
from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from jiuwenswarm.extensions.agentos.agentos_router.config import RouterConfig, load_router_config as load_agentos_config


@dataclass(frozen=True)
class AdxRouterConfig:
    ingress_url: str
    tenant: str
    token: str = field(repr=False)
    router: RouterConfig = field(repr=False)
    builtin_image: str = 'jiuwenswarm-agent-runtime:latest'
    runtime_class: str = 'runc'
    workspace_target: str = '/home/agentos'
    connect_timeout_seconds: float = 60.0


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


def load_router_config(config: dict) -> AdxRouterConfig:
    settings = (config.get('gateway') or {}).get('adx') or {}
    if not isinstance(settings, dict) or settings.keys() - {
        'ingress_url', 'tenant', 'token_env', 'builtin_image', 'runtime_class', 'workspace_target',
        'connect_timeout_seconds',
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
    runtime_class = _text_setting(settings, 'runtime_class', 'runc')
    workspace_target = _text_setting(settings, 'workspace_target', '/home/agentos')
    if not workspace_target.startswith('/') or any(part in ('.', '..') for part in workspace_target.split('/')):
        raise ValueError('gateway.adx.workspace_target must be an absolute canonical path')
    return AdxRouterConfig(
        ingress_url=url.rstrip('/'), tenant=tenant, token=token,
        router=load_agentos_config(config, require_frontend_endpoint=False),
        builtin_image=builtin_image, runtime_class=runtime_class,
        workspace_target=workspace_target, connect_timeout_seconds=timeout,
    )

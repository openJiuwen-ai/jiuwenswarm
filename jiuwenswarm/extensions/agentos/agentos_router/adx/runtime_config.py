# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""ADX-specific launch bindings owned by the AgentOS Router implementation."""
from __future__ import annotations

from jiuwenswarm.common.runtime_settings import (
    bind_runtime_sandbox as bind_managed_sandbox, bind_sandbox_connection,
)


def runtime_environment_settings() -> dict:
    return {
        'credentials': {'JIUWENSWARM_ADX_API_TOKEN': 'ADX_TOKEN'},
        'preserve_env': ['JIUWENSWARM_ADX_NODE_INGRESS_URL', 'JIUWENSWARM_ADX_NODE_CONTEXT_FILE',
                         'ADX_TOKEN'],
    }


def managed_sandbox_settings() -> dict:
    return {
        'type': 'adx-environment',
        'managed_connection': {
            'url_env': 'JIUWENSWARM_ADX_NODE_INGRESS_URL',
            'context_file_env': 'JIUWENSWARM_ADX_NODE_CONTEXT_FILE',
            'replace_connection_fields': ['server_address', 'gateway_address', 'token', 'token_provider',
                                           'use_tls', 'gateway_use_tls', 'verify_tls'],
            'credential_fields': ['ADX_TOKEN', 'ADX_GATEWAY_TOKEN', 'JIUWENSWARM_ADX_API_TOKEN'],
        },
        'launcher_config': {'workspace_mounts': True, 'shared_scope': 'default'},
    }


def bind_adx_runtime(sandbox: dict | None, ingress_url: str, endpoint_file: str | None = None) -> dict:
    """Retain the existing connection-binding API for deployment callers."""
    settings = managed_sandbox_settings()
    if not isinstance(sandbox, dict) or sandbox.get('type') != settings['type']:
        raise ValueError('AgentOS workspace sandbox requires deployment migration before startup')
    return bind_sandbox_connection(sandbox, ingress_url, endpoint_file or '', settings['managed_connection'])


def bind_runtime_sandbox(sandbox: dict | None, deployment: dict) -> dict:
    """Accept retained deployment callers using the previous compact overlay."""
    if deployment.get('type') == 'adx-environment':
        deployment = {**deployment, **managed_sandbox_settings()}
    return bind_managed_sandbox(sandbox, deployment)

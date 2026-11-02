# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from jiuwenswarm.extensions.agentos.agentos_router.agent_manager import (
    DEFAULT_AGENT_KEY_FIELDS,
    normalize_agent_key_fields,
)
from jiuwenswarm.extensions.agentos.agentos_router.registry_client import RegistryConfig
from jiuwenswarm.extensions.agentos.agentos_router.ssh_relay import (
    YuanrongSshSettings,
    load_yuanrong_ssh_settings,
)

DEFAULT_AGENT_WORKSPACE_ROOT = "/home/agentos/users"
# Env override for gateway.agentos.sandbox_idle_timeout_seconds (vibeskill-aligned).
SANDBOX_IDLE_TIMEOUT_ENV = "SANDBOX_IDLE_TIMEOUT_SECONDS"


@dataclass(frozen=True)
class SshChannelEndpoint:
    """Northbound ``channels.ssh`` listen address for ``3rdagent.switch``."""

    ip: str = ""
    port: int = 0


@dataclass(frozen=True)
class RouterConfig:
    frontend_endpoint: str
    function_version_urn: str
    concurrency: int
    invoke_timeout_s: float
    registry: RegistryConfig
    agent_namespace: str = "default"
    agent_timeout_s: float = 300.0
    creating_timeout_seconds: float = 60.0
    agent_key_fields: tuple[str, ...] = DEFAULT_AGENT_KEY_FIELDS
    workspace_root: str = DEFAULT_AGENT_WORKSPACE_ROOT
    # Idle sandbox reclamation: delete the YuanRong instance once an agent
    # has no held tasks (chat/SSH) for this long. <= 0 disables reclamation.
    sandbox_idle_timeout_seconds: float = 600.0
    sandbox_idle_check_interval_seconds: float = 30.0
    ssh: YuanrongSshSettings = YuanrongSshSettings()
    ssh_channel: SshChannelEndpoint | None = None
    auth_service_url: str = ""
    timeout: float = 10.0
    auth_enabled: bool = False


def agentos_router_selected(config: dict[str, Any]) -> bool:
    gateway = config.get("gateway") if isinstance(config, dict) else {}
    if not isinstance(gateway, dict):
        return False
    agent_client = gateway.get("agent_client")
    if not isinstance(agent_client, dict):
        agent_client = {}
    return (
        str(agent_client.get("type") or "websocket").strip().lower()
        == "agentos_router"
    )


def load_ssh_channel_endpoint(config: dict[str, Any]) -> SshChannelEndpoint | None:
    """Load northbound SSH listen ip/port from ``channels.ssh``.

    Returns ``None`` when the channel is disabled or listen address is incomplete.
    """
    channels = config.get("channels") if isinstance(config, dict) else None
    if not isinstance(channels, dict):
        return None
    ssh = channels.get("ssh")
    if not isinstance(ssh, dict):
        return None
    if not bool(ssh.get("enabled", False)):
        return None
    ip = str(ssh.get("listen_host") or "").strip()
    try:
        port = int(ssh.get("listen_port") or 0)
    except (TypeError, ValueError):
        return None
    if not ip or port <= 0:
        return None
    return SshChannelEndpoint(ip=ip, port=port)


def _read_float(section: dict[str, Any], key: str, default: float) -> float:
    """Read a float honoring explicit ``0`` (``or default`` would swallow it)."""
    raw = section.get(key)
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return default
    return float(raw)


def _read_float_env(name: str) -> float | None:
    """Parse a float env var; empty / unset → None; invalid → raise ValueError."""
    raw = os.getenv(name)
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    return float(text)


def load_router_config(config: dict[str, Any]) -> RouterConfig:
    gateway = config.get("gateway") if isinstance(config, dict) else {}
    if not isinstance(gateway, dict):
        gateway = {}
    agent_client = gateway.get("agent_client")
    if not isinstance(agent_client, dict):
        agent_client = {}
    agentos = gateway.get("agentos")
    if not isinstance(agentos, dict):
        agentos = {}
    registry = agentos.get("registry")
    if not isinstance(registry, dict):
        registry = {}

    frontend_endpoint = str(agent_client.get("frontend_endpoint") or "").strip()
    function_version_urn = str(
        agent_client.get("function_version_urn") or ""
    ).strip()
    if not frontend_endpoint or not function_version_urn:
        raise ValueError(
            "gateway.agent_client.frontend_endpoint and function_version_urn "
            "are required in agentos_router mode"
        )

    auth_service_url = str(agentos.get("auth_service_url") or "").strip()
    timeout = float(agentos.get("timeout") or 10)
    auth_enabled = str(agentos.get("auth_enabled", "false")).strip().lower() in ("true", "1", "yes")

    # Env wins over yaml (incl. explicit 0 to disable), same as vibeskill.
    idle_timeout_env = _read_float_env(SANDBOX_IDLE_TIMEOUT_ENV)
    sandbox_idle_timeout_seconds = (
        idle_timeout_env
        if idle_timeout_env is not None
        else _read_float(agentos, "sandbox_idle_timeout_seconds", 600.0)
    )

    return RouterConfig(
        frontend_endpoint=frontend_endpoint,
        function_version_urn=function_version_urn,
        concurrency=int(agent_client.get("concurrency") or 1),
        invoke_timeout_s=float(agent_client.get("invoke_timeout_s") or 60.0),
        agent_namespace=str(agent_client.get("agent_namespace") or "default").strip() or "default",
        agent_timeout_s=float(agent_client.get("agent_timeout_s") or 300.0),
        registry=RegistryConfig(
            endpoint=str(registry.get("endpoint") or "").strip(),
            request_timeout_s=float(registry.get("request_timeout_s") or 10.0),
            node=str(registry.get("node") or "").strip(),
        ),
        creating_timeout_seconds=float(
            agentos.get("creating_timeout_seconds") or 60.0
        ),
        agent_key_fields=normalize_agent_key_fields(
            agentos.get("agent_key_fields")
        ),
        workspace_root=str(
            agentos.get("workspace_root") or DEFAULT_AGENT_WORKSPACE_ROOT
        ).strip()
        or DEFAULT_AGENT_WORKSPACE_ROOT,
        sandbox_idle_timeout_seconds=sandbox_idle_timeout_seconds,
        sandbox_idle_check_interval_seconds=_read_float(
            agentos, "sandbox_idle_check_interval_seconds", 30.0
        ),
        ssh=load_yuanrong_ssh_settings(agentos.get("ssh")),
        ssh_channel=load_ssh_channel_endpoint(config),
        auth_service_url=auth_service_url,
        timeout=timeout,
        auth_enabled=auth_enabled,
    )

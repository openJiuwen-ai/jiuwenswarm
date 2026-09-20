# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, replace
from typing import Any

from jiuwenswarm.extensions.agentos.agentos_router.agent_manager import (
    DEFAULT_AGENT_KEY_FIELDS,
    normalize_agent_key_fields,
)
from jiuwenswarm.extensions.agentos.agentos_router.logutil import log_agentos
from jiuwenswarm.extensions.agentos.agentos_router.registry_client import RegistryConfig
from jiuwenswarm.extensions.agentos.agentos_router.ssh_relay import (
    YuanrongSshSettings,
    load_yuanrong_ssh_settings,
)
from jiuwenswarm.extensions.yuanrong_frontend_client import (
    DEFAULT_RUNTIME_PROBE_SETTINGS,
    DEFAULT_THIRD_AGENT_PROBE_SETTINGS,
    RuntimeProbeSettings,
)

DEFAULT_AGENT_WORKSPACE_ROOT = "/home/agentos/users"
# Env override for gateway.agentos.sandbox_idle_timeout_seconds (vibeskill-aligned).
SANDBOX_IDLE_TIMEOUT_ENV = "SANDBOX_IDLE_TIMEOUT_SECONDS"
# Env override for gateway.agentos.disconnect_cleanup_timeout_seconds.
DISCONNECT_CLEANUP_TIMEOUT_ENV = "DISCONNECT_CLEANUP_TIMEOUT_SECONDS"
# Env overrides for YuanRong TCP probes / GET wait (win over yaml).
WAIT_RUNNING_TIMEOUT_ENV = "AGENTOS_WAIT_RUNNING_TIMEOUT_SECONDS"
WAIT_RUNNING_INTERVAL_ENV = "AGENTOS_WAIT_RUNNING_INTERVAL_SECONDS"
PROBE_STARTUP_INITIAL_DELAY_ENV = "AGENTOS_PROBE_STARTUP_INITIAL_DELAY_SECONDS"
PROBE_STARTUP_PERIOD_ENV = "AGENTOS_PROBE_STARTUP_PERIOD_SECONDS"
PROBE_STARTUP_TIMEOUT_ENV = "AGENTOS_PROBE_STARTUP_TIMEOUT_SECONDS"
PROBE_STARTUP_FAILURE_THRESHOLD_ENV = "AGENTOS_PROBE_STARTUP_FAILURE_THRESHOLD"
PROBE_LIVENESS_TIMEOUT_ENV = "AGENTOS_PROBE_LIVENESS_TIMEOUT_SECONDS"
PROBE_LIVENESS_FAILURE_THRESHOLD_ENV = "AGENTOS_PROBE_LIVENESS_FAILURE_THRESHOLD"

logger = logging.getLogger(__name__)


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
    # Channel-disconnect cleanup: when a user has zero live channels, wait this
    # long before deleting their jiuwenswarm agent (gives a chance to reconnect
    # and gives in-flight chat/SSH time to finish). Reuses the same
    # pop_if_idle safety check as the idle reaper (READY + task_count==0 +
    # last_active_at beyond this window). <= 0 disables disconnect cleanup.
    disconnect_cleanup_timeout_seconds: float = 60.0
    # Web/TUI connect warmup: create the builtin jiuwenswarm sandbox and open
    # the instance WS in the background so the first chat is not blocked on
    # create + cold-start 502 retries. Failure never drops the connection.
    connect_warmup_enabled: bool = True
    probes: RuntimeProbeSettings = DEFAULT_RUNTIME_PROBE_SETTINGS
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


def _read_bool(section: dict[str, Any], key: str, default: bool) -> bool:
    """Read a bool; missing / blank keeps *default*; explicit false/0/no/off disables."""
    raw = section.get(key)
    if raw is None or (isinstance(raw, str) and not str(raw).strip()):
        return default
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def _read_int(section: dict[str, Any], key: str, default: int) -> int:
    """Read an int honoring explicit ``0`` (``or default`` would swallow it)."""
    raw = section.get(key)
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return default
    return int(raw)


def _read_int_env(name: str) -> int | None:
    """Parse an int env var; empty / unset → None; invalid → raise ValueError."""
    raw = os.getenv(name)
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    return int(text)


def _overlay_int_env(current: int, name: str) -> int:
    override = _read_int_env(name)
    return current if override is None else override


def _require_non_negative(value: int | float, *, name: str) -> None:
    if value < 0:
        raise ValueError(f"{name} must be >= 0, got {value}")


def _require_positive(value: int | float, *, name: str) -> None:
    if value <= 0:
        raise ValueError(f"{name} must be > 0, got {value}")


def _load_probe_settings(agentos: dict[str, Any]) -> RuntimeProbeSettings:
    """Load ``gateway.agentos.probes``; env wins over yaml."""
    defaults = DEFAULT_RUNTIME_PROBE_SETTINGS
    probes = agentos.get("probes")
    if not isinstance(probes, dict):
        probes = {}
    startup = probes.get("startup")
    if not isinstance(startup, dict):
        startup = {}
    liveness = probes.get("liveness")
    if not isinstance(liveness, dict):
        liveness = {}

    wait_running_env = _read_float_env(WAIT_RUNNING_TIMEOUT_ENV)
    wait_running_timeout_seconds = (
        wait_running_env
        if wait_running_env is not None
        else _read_float(
            agentos,
            "wait_running_timeout_seconds",
            defaults.wait_running_timeout_seconds,
        )
    )
    wait_interval_env = _read_float_env(WAIT_RUNNING_INTERVAL_ENV)
    wait_running_interval_seconds = (
        wait_interval_env
        if wait_interval_env is not None
        else _read_float(
            agentos,
            "wait_running_interval_seconds",
            defaults.wait_running_interval_seconds,
        )
    )

    settings = RuntimeProbeSettings(
        startup_initial_delay_seconds=_overlay_int_env(
            _read_int(
                startup,
                "initial_delay_seconds",
                defaults.startup_initial_delay_seconds,
            ),
            PROBE_STARTUP_INITIAL_DELAY_ENV,
        ),
        startup_period_seconds=_overlay_int_env(
            _read_int(
                startup, "period_seconds", defaults.startup_period_seconds
            ),
            PROBE_STARTUP_PERIOD_ENV,
        ),
        startup_timeout_seconds=_overlay_int_env(
            _read_int(
                startup, "timeout_seconds", defaults.startup_timeout_seconds
            ),
            PROBE_STARTUP_TIMEOUT_ENV,
        ),
        startup_failure_threshold=_overlay_int_env(
            _read_int(
                startup,
                "failure_threshold",
                defaults.startup_failure_threshold,
            ),
            PROBE_STARTUP_FAILURE_THRESHOLD_ENV,
        ),
        liveness_timeout_seconds=_overlay_int_env(
            _read_int(
                liveness, "timeout_seconds", defaults.liveness_timeout_seconds
            ),
            PROBE_LIVENESS_TIMEOUT_ENV,
        ),
        liveness_failure_threshold=_overlay_int_env(
            _read_int(
                liveness,
                "failure_threshold",
                defaults.liveness_failure_threshold,
            ),
            PROBE_LIVENESS_FAILURE_THRESHOLD_ENV,
        ),
        wait_running_timeout_seconds=wait_running_timeout_seconds,
        wait_running_interval_seconds=wait_running_interval_seconds,
    )
    _require_non_negative(
        settings.startup_initial_delay_seconds,
        name="probes.startup.initial_delay_seconds",
    )
    _require_positive(
        settings.startup_period_seconds, name="probes.startup.period_seconds"
    )
    _require_positive(
        settings.startup_timeout_seconds, name="probes.startup.timeout_seconds"
    )
    _require_positive(
        settings.startup_failure_threshold,
        name="probes.startup.failure_threshold",
    )
    _require_positive(
        settings.liveness_timeout_seconds, name="probes.liveness.timeout_seconds"
    )
    _require_positive(
        settings.liveness_failure_threshold,
        name="probes.liveness.failure_threshold",
    )
    _require_positive(
        settings.wait_running_timeout_seconds,
        name="wait_running_timeout_seconds",
    )
    _require_positive(
        settings.wait_running_interval_seconds,
        name="wait_running_interval_seconds",
    )
    builtin_budget = settings.startup_budget_seconds()
    if settings.same_tcp_timings(DEFAULT_RUNTIME_PROBE_SETTINGS):
        third_budget = DEFAULT_THIRD_AGENT_PROBE_SETTINGS.startup_budget_seconds()
    else:
        third_budget = builtin_budget
    min_wait = max(builtin_budget, third_budget)
    if settings.wait_running_timeout_seconds < min_wait:
        log_agentos(
            logger,
            logging.WARNING,
            "config.wait_running.clamp",
            configured=settings.wait_running_timeout_seconds,
            builtin_budget=builtin_budget,
            third_agent_budget=third_budget,
            clamped=min_wait,
        )
        settings = replace(
            settings, wait_running_timeout_seconds=float(min_wait)
        )
    return settings


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

    disconnect_cleanup_env = _read_float_env(DISCONNECT_CLEANUP_TIMEOUT_ENV)
    disconnect_cleanup_timeout_seconds = (
        disconnect_cleanup_env
        if disconnect_cleanup_env is not None
        else _read_float(agentos, "disconnect_cleanup_timeout_seconds", 60.0)
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
        disconnect_cleanup_timeout_seconds=disconnect_cleanup_timeout_seconds,
        connect_warmup_enabled=_read_bool(agentos, "connect_warmup_enabled", True),
        probes=_load_probe_settings(agentos),
        ssh=load_yuanrong_ssh_settings(agentos.get("ssh")),
        ssh_channel=load_ssh_channel_endpoint(config),
        auth_service_url=auth_service_url,
        timeout=timeout,
        auth_enabled=auth_enabled,
    )

# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Config-gated lifecycle for single-agent / coding-agent observability.

The non-team counterpart of ``sync_team_observability`` /
``shutdown_team_observability`` in
``jiuwenswarm.agents.harness.team.team_manager``, and symmetric with it: this
module only reads this platform's config and toggles the runtime, while the
tracing mechanics — run root span, the session-keyed fallback that keeps it
reachable from supervisor tasks, agent-tier rail wiring and the sub-agent
dispatch hook — live in the SDK under ``openjiuwen.harness.observability``.

It is kept in a **separate file with its own state and config section** on
purpose, so the existing team scenario is not affected.

Shared-provider caveat (important):
    OpenTelemetry allows exactly ONE global ``TracerProvider`` per process, and
    initialization is a no-op if one already exists. In a process where BOTH
    team and agent observability are enabled, whichever runs first wins; the
    other silently reuses it (its exporter/endpoint/service_name are ignored).
    Provider demands are coordinated inside the SDK, so agent shutdown never
    tears down a provider the team subsystem depends on.
"""

from __future__ import annotations

import logging

from openjiuwen.harness.observability import (
    acquire_observability,
    release_observability,
)

from jiuwenswarm.agents.harness.observability_runtime import build_observability_config
from jiuwenswarm.common.config import (
    get_config,
    get_skill_evolution_enabled,
)
from jiuwenswarm.common.utils import get_user_workspace_dir
from jiuwenswarm.observability.config import load_trajectory_store_settings
from jiuwenswarm.observability.runtime import (
    shutdown_trajectory_runtime,
    sync_trajectory_runtime,
)

logger = logging.getLogger(__name__)

# Tracks whether observability is currently active so we can detect config
# toggles (enabled -> disabled or vice-versa) and init / shutdown accordingly
# on each single-agent request.
_agent_observability_active: bool = False

# Sticky flag: once any single-agent request has force-enabled observability
# (e.g. a ``/debug`` run with ``debug_trace.<mode>.otel_enabled``), we never
# auto-teardown the provider for the rest of the process. OTel allows only one
# global TracerProvider and re-init after shutdown is fragile, so a /debug
# toggle must not churn init/shutdown across alternating requests. The normal
# config-gated path (agent_observability.enabled hot-reload) is unaffected
# unless force was ever used.
_force_ever_enabled: bool = False


def sync_agent_observability(*, force: bool = False) -> None:
    """Synchronize single-agent observability state with current config.

    Called before each ``Runner.run_agent_streaming`` / ``Runner.run_agent`` so
    that hot-reloading the ``agent_observability.enabled`` flag takes effect
    immediately:

    * disabled -> enabled : acquire the provider (or reuse if already up)
    * enabled -> disabled : ``shutdown_agent_observability()``
    * unchanged           : no-op

    Evolution also requests the provider when the explicit switch is disabled.

    ``force=True`` (set by a ``/debug`` run when ``debug_trace.<mode>.otel_enabled``
    is true) treats ``want_enabled`` as true regardless of config, so a debug
    request can pull up OTel even when ``agent_observability.enabled`` is false.
    Once force is ever used, the provider stays up for the process (sticky — see
    ``_force_ever_enabled``) to avoid init/shutdown churn across alternating
    requests; the normal config hot-reload teardown is unchanged when evolution
    is disabled.
    """
    global _agent_observability_active, _force_ever_enabled

    config = get_config()
    cfg = config.get("agent_observability", {}) or {}
    trajectory_settings = load_trajectory_store_settings(config)
    evolution_requested = get_skill_evolution_enabled(config)
    want_enabled = (
        bool(cfg.get("enabled", False))
        or trajectory_settings.enabled
        or evolution_requested
        or force
        or _force_ever_enabled
    )
    if force:
        _force_ever_enabled = True

    if not want_enabled:
        try:
            sync_trajectory_runtime(trajectory_settings, demand="agent")
        except Exception as exc:
            logger.warning("[AgentObservability] trajectory runtime stop failed: %s", exc)
        if _agent_observability_active:
            shutdown_agent_observability()
        return

    try:
        traces_dir = str(cfg.get("traces_dir") or get_user_workspace_dir() / ".trace")
        obs_cfg = build_observability_config(
            cfg,
            service_name="jiuwenswarm-agent",
            default_backend="otlp",
            traces_dir=traces_dir,
        )
        provider_existed = acquire_observability(obs_cfg)
        was_active = _agent_observability_active
        _agent_observability_active = True
        try:
            sync_trajectory_runtime(trajectory_settings, demand="agent")
        except Exception as exc:
            # The trajectory read store is an optional fan-out. Existing file,
            # OTLP and Langfuse exporters must keep the Agent path available.
            logger.warning("[AgentObservability] trajectory runtime init failed: %s", exc)
        if not was_active:
            if provider_existed:
                logger.info(
                    "[AgentObservability] reusing existing observability provider "
                    "(owned by another subsystem)"
                )
            elif cfg.get("exporter", "otlp_grpc") == "file":
                logger.info(
                    "[AgentObservability] enabled: exporter=%s traces_dir=%s",
                    cfg.get("exporter", "otlp_grpc"),
                    traces_dir,
                )
            else:
                logger.info(
                    "[AgentObservability] enabled: exporter=%s endpoint=%s",
                    cfg.get("exporter", "otlp_grpc"),
                    cfg.get("endpoint", "http://localhost:4317"),
                )
    except Exception as exc:
        _agent_observability_active = False
        if evolution_requested:
            raise RuntimeError(
                "Agent evolution observability initialization failed"
            ) from exc
        logger.warning("[AgentObservability] init failed: %s", exc)


def shutdown_agent_observability() -> None:
    """Shutdown single-agent observability (on disable or process exit)."""
    global _agent_observability_active
    try:
        if not shutdown_trajectory_runtime(demand="agent"):
            logger.warning("[AgentObservability] trajectory runtime did not drain cleanly")
    except Exception as exc:
        logger.warning("[AgentObservability] trajectory runtime shutdown failed: %s", exc)
    if not _agent_observability_active:
        return
    try:
        release_observability()
        _agent_observability_active = False
        logger.info("[AgentObservability] disabled")
    except Exception as exc:
        logger.warning("[AgentObservability] shutdown failed: %s", exc)

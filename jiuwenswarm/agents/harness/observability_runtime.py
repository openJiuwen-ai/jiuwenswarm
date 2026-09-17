# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Shared process-level observability and trajectory processor access."""

from __future__ import annotations

from collections.abc import Mapping
import threading
from typing import Any

_RUNTIMES = frozenset({"agent", "team"})
_ACTIVE_RUNTIMES: set[str] = set()
_DEMAND_LOCK = threading.RLock()
# Only shut down a provider that this coordinator created. A provider may be
# initialized by an RL collector or another SDK entry point before either
# runtime acquires a demand; those callers retain ownership of its lifecycle.
_PROVIDER_OWNED = False
_TRAJECTORY_SPAN_PROCESSOR: Any | None = None


def get_trajectory_span_processor() -> Any:
    """Return the trajectory processor shared by all JiuwenClaw runtimes."""
    global _TRAJECTORY_SPAN_PROCESSOR
    with _DEMAND_LOCK:
        if _TRAJECTORY_SPAN_PROCESSOR is not None:
            return _TRAJECTORY_SPAN_PROCESSOR

        from openjiuwen.agent_evolving.trajectory.processor import (
            TrajectorySpanProcessor,
        )

        _TRAJECTORY_SPAN_PROCESSOR = TrajectorySpanProcessor()
        return _TRAJECTORY_SPAN_PROCESSOR


def build_observability_config(
    config: Mapping[str, Any],
    *,
    service_name: str,
    default_exporter: str = "otlp_grpc",
    default_endpoint: str = "http://localhost:4317",
    traces_dir: str,
) -> Any:
    """Build the SDK config for one runtime without initializing the provider."""
    from openjiuwen.agent_teams.observability import ObservabilityConfig

    return ObservabilityConfig(
        enabled=True,
        service_name=config.get("service_name", service_name),
        exporter=config.get("exporter", default_exporter),
        endpoint=config.get("endpoint", default_endpoint),
        sample_rate=config.get("sample_rate", 1.0),
        attribute_value_max_length=config.get("attribute_value_max_length", 10240),
        redact_prompts=config.get("redact_prompts", False),
        redact_completions=config.get("redact_completions", False),
        langfuse_public_key=config.get("langfuse_public_key", ""),
        langfuse_secret_key=config.get("langfuse_secret_key", ""),
        traces_dir=config.get("traces_dir") or traces_dir,
        file_retention_days=config.get("file_retention_days", 7),
    )


def acquire_observability_demand(
    runtime: str,
    *,
    observability_config: Any,
) -> bool:
    """Acquire one runtime's demand for the shared provider.

    Initialization errors are deliberately propagated so the caller can
    distinguish an optional tracing failure from a required evolution capture
    failure.
    """
    global _PROVIDER_OWNED
    with _DEMAND_LOCK:
        if runtime not in _RUNTIMES:
            raise ValueError(f"unknown observability runtime: {runtime}")

        from openjiuwen.agent_teams.observability import is_initialized

        if runtime in _ACTIVE_RUNTIMES and is_initialized():
            return True

        from openjiuwen.agent_teams.observability import init_observability

        provider_existed = is_initialized()
        init_observability(
            observability_config,
            additional_span_processors=(get_trajectory_span_processor(),),
        )
        if not is_initialized():
            raise RuntimeError(
                f"{runtime} observability initialization did not create a provider"
            )
        if not provider_existed:
            _PROVIDER_OWNED = True
        _ACTIVE_RUNTIMES.add(runtime)
        return provider_existed


def release_observability_demand(runtime: str) -> None:
    """Release a runtime demand during process shutdown."""
    global _PROVIDER_OWNED
    with _DEMAND_LOCK:
        if runtime not in _RUNTIMES:
            raise ValueError(f"unknown observability runtime: {runtime}")
        _ACTIVE_RUNTIMES.discard(runtime)
        if not _ACTIVE_RUNTIMES and _PROVIDER_OWNED:
            from openjiuwen.agent_teams.observability import (
                is_initialized,
                shutdown_observability,
            )

            if is_initialized():
                shutdown_observability()
            _PROVIDER_OWNED = False


def reset_observability_demands() -> None:
    """Reset demand bookkeeping for isolated tests."""
    global _PROVIDER_OWNED
    with _DEMAND_LOCK:
        _ACTIVE_RUNTIMES.clear()
        _PROVIDER_OWNED = False

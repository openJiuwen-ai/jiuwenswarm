# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Shared JiuwenSwarm observability policy and SDK config mapping.

Provider lifecycle itself lives in the SDK: each runtime acquires and releases
its own demand (``openjiuwen.harness.observability`` for single agent,
``openjiuwen.agent_teams.observability`` for Team) and the shared coordinator in
``openjiuwen.extensions.observability.demand`` keeps one runtime's shutdown from
tearing down a provider the other still needs. What remains here is the part the
SDK cannot own: mapping ``config.yaml`` to ``ObservabilityConfig`` and applying
its unified export switch to all demands acquired by this platform.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Mapping
from typing import Any

logger = logging.getLogger(__name__)

# Serialize both platform entry points, including plugin leases and shutdown.
# Never take an entry-point-specific lock while holding this lock.
observability_state_lock = threading.RLock()
# Only successfully acquired Swarm demands belong here. SDK callers outside
# Swarm retain their own export intent. Values are independent debug overrides.
_export_overrides: dict[str, bool] = {}


def sync_observability_export_policy(enabled: bool) -> None:
    """Refresh every acquired Swarm demand from the shared config switch.

    Caller holds ``observability_state_lock`` through acquire/release as well,
    so another entry point cannot publish an older config halfway through sync.
    """
    from openjiuwen.extensions.observability.demand import set_export_demand

    for runtime, force_export in _export_overrides.items():
        set_export_demand(runtime, enabled or force_export)


def remember_observability_demand(runtime: str, *, force_export: bool = False) -> None:
    """Record a successful acquire; caller holds the shared state lock."""
    _export_overrides[runtime] = force_export


def forget_observability_demand(runtime: str) -> None:
    """Forget a successfully released demand under the shared state lock."""
    _export_overrides.pop(runtime, None)


def build_observability_config(
    config: Mapping[str, Any],
    *,
    service_name: str,
    default_exporter: str = "otlp_grpc",
    default_endpoint: str = "http://localhost:4317",
    traces_dir: str,
) -> Any:
    """Build the SDK config for one runtime without initializing the provider."""
    from openjiuwen.extensions.observability.config import ObservabilityConfig

    if "backend" in config:
        # ``backend`` once overrode ``exporter`` in the SDK; the SDK no longer
        # has the field. Say so instead of silently ignoring a stale config.
        logger.warning(
            "observability config key 'backend' has been removed and is ignored; "
            "select the transport with 'exporter' (e.g. exporter: langfuse)"
        )

    return ObservabilityConfig(
        enabled=True,
        service_name=config.get("service_name", service_name),
        exporter=config.get("exporter", default_exporter),
        endpoint=config.get("endpoint", default_endpoint),
        sample_rate=config.get("sample_rate", 1.0),
        max_attributes=config.get("max_attributes", 200),
        attribute_value_max_length=config.get("attribute_value_max_length", 10240),
        redact_prompts=config.get("redact_prompts", False),
        redact_completions=config.get("redact_completions", False),
        langfuse_public_key=config.get("langfuse_public_key", ""),
        langfuse_secret_key=config.get("langfuse_secret_key", ""),
        traces_dir=config.get("traces_dir") or traces_dir,
        file_retention_days=config.get("file_retention_days", 7),
    )

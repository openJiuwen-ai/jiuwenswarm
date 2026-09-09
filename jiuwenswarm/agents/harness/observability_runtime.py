# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Mapping from JiuwenSwarm settings to the SDK observability config.

Provider lifecycle itself lives in the SDK: each runtime acquires and releases
its own demand (``openjiuwen.harness.observability`` for single agent,
``openjiuwen.agent_teams.observability`` for Team) and the shared coordinator in
``openjiuwen.extensions.observability.demand`` keeps one runtime's shutdown from
tearing down a provider the other still needs. What remains here is the part the
SDK cannot own: turning this platform's ``config.yaml`` section into an
``ObservabilityConfig``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def build_observability_config(
    config: Mapping[str, Any],
    *,
    service_name: str,
    default_exporter: str = "otlp_grpc",
    default_endpoint: str = "http://localhost:4317",
    default_backend: str = "langfuse",
    traces_dir: str,
) -> Any:
    """Build the SDK config for one runtime without initializing the provider."""
    from openjiuwen.extensions.observability.config import ObservabilityConfig

    return ObservabilityConfig(
        enabled=True,
        service_name=config.get("service_name", service_name),
        exporter=config.get("exporter", default_exporter),
        endpoint=config.get("endpoint", default_endpoint),
        backend=config.get("backend", default_backend),
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

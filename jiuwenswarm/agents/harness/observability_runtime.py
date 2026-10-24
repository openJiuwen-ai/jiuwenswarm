# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Map JiuwenSwarm settings and bind an active AgentArts provider to the SDK.

Provider lifecycle itself lives in the SDK: each runtime acquires and releases
its own demand (``openjiuwen.harness.observability`` for single agent,
``openjiuwen.agent_teams.observability`` for Team) and the shared coordinator in
``openjiuwen.extensions.observability.demand`` keeps one runtime's shutdown from
tearing down a provider the other still needs. What remains here is the part the
SDK cannot own: turning this platform's ``config.yaml`` section into an
``ObservabilityConfig`` and selecting the provider installed by its host probe.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any


def bind_agentarts_tracer_provider(config: Any) -> bool:
    """Keep AgentCore parents on the provider installed by the AgentArts wheel.

    AgentCore otherwise creates a private provider even when a global provider
    exists. The wheel's LLM/tool spans then reference parents exported only to
    the private provider's local destination.
    """
    if os.getenv("agentarts_agentops_otel_agent_apm_enable", "false").strip().lower() != "true":
        return False

    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from openjiuwen.extensions.observability.setup import (
        init_observability,
        is_initialized,
    )

    provider = trace.get_tracer_provider()
    if not isinstance(provider, TracerProvider):
        return False
    if is_initialized():
        # The SDK validates provider identity/ownership without reinitializing.
        # Return True for an existing host binding so Team can reacquire file
        # output after a toggle or join an Agent that initialized first.
        try:
            init_observability(config, tracer_provider_override=provider, owns_provider=False)
        except RuntimeError:
            # An existing private/other provider keeps its original lifecycle.
            return False
        return True
    # The wheel owns export and shutdown. SDK demand registration below still
    # installs the agent/team callbacks and trajectory processors as usual.
    init_observability(config, tracer_provider_override=provider, owns_provider=False)
    return True


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

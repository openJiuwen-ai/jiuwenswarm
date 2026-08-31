"""ModelRoutingRail package — capability-based model routing with async health check."""
from __future__ import annotations

from .capability import (
    ModelCapability,
    build_capability_table_from_config,
    _capability_rank,
    _map_model_group_provider,
)
from .classifier import (
    ensure_routing_state_files,
    load_mapper_config,
    load_classifier_impl,
    validate_score,
    task_score,
)
from .stats import (
    _ModelUsageStats,
    get_stats_store,
    reset_stats_store_for_test,
)
from .routing import _decide_and_select, _detect_model_type
from .health_check import ModelHealthChecker, HealthCheckConfig, HealthStatus
from .types import (
    PriorModelCall,
    TaskAnalysis,
    RoutingDecision,
    _new_trace_id,
    _new_span_id,
    _extract_prompt_text,
    _message_text,
    _agent_model_name,
    _extract_agent_info,
    _get_session_id,
)
from .model_routing_rail import ModelRoutingRail

__all__ = [
    "ModelRoutingRail",
    "ModelCapability",
    "build_capability_table_from_config",
    "ensure_routing_state_files",
    "load_mapper_config",
    "load_classifier_impl",
    "validate_score",
    "get_stats_store",
    "reset_stats_store_for_test",
]

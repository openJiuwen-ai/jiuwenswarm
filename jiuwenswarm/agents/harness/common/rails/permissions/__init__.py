"""Security and permission integration for AgentServer.

This package hosts jiuwenswarm-side glue code for openjiuwen security rails,
owner-scoped policies, and persistence helpers.
"""

from __future__ import annotations

from jiuwenswarm.edition import is_enterprise
from jiuwenswarm.agents.harness.common.rails.permissions.config_loader import (
    PERMISSIONS_AGENTS_KEY,
    SEED_PERMISSIONS_AGENT_IDS,
    apply_permissions_config_payload,
    clear_permissions_config_cache,
    clear_session_permissions_overlay,
    get_base_permissions_config,
    get_effective_permissions_config,
    get_global_permissions_config,
    get_permissions_agent_base,
    get_permissions_session_id,
    get_shipped_template_permissions_config,
    get_yaml_agent_permissions_overlay,
    lookup_standard_permissions_agent_id,
    merge_agent_permissions_overlay,
    merge_session_permissions_overlay,
    normalize_permissions_agent_id,
    persist_permissions_mutate,
    reload_permissions_from_gateway_db,
    reset_permissions_agent_base,
    reset_permissions_session_scope,
    resolve_permissions_body_from_enterprise,
    resolve_permissions_persist_target,
    resolve_yaml_agent_permissions_body,
    sanitize_agent_permissions_body,
    setup_permissions_agent_base,
    setup_permissions_session_scope,
    strip_permissions_agents,
)

__all__ = [
    "PERMISSIONS_AGENTS_KEY",
    "SEED_PERMISSIONS_AGENT_IDS",
    "apply_permissions_config_payload",
    "clear_permissions_config_cache",
    "clear_session_permissions_overlay",
    "get_base_permissions_config",
    "get_effective_permissions_config",
    "get_global_permissions_config",
    "get_permissions_agent_base",
    "get_permissions_session_id",
    "get_shipped_template_permissions_config",
    "get_yaml_agent_permissions_overlay",
    "is_enterprise",
    "lookup_standard_permissions_agent_id",
    "merge_agent_permissions_overlay",
    "merge_session_permissions_overlay",
    "normalize_permissions_agent_id",
    "persist_permissions_mutate",
    "reload_permissions_from_gateway_db",
    "reset_permissions_agent_base",
    "reset_permissions_session_scope",
    "resolve_permissions_body_from_enterprise",
    "resolve_permissions_persist_target",
    "resolve_yaml_agent_permissions_body",
    "sanitize_agent_permissions_body",
    "setup_permissions_agent_base",
    "setup_permissions_session_scope",
    "strip_permissions_agents",
]

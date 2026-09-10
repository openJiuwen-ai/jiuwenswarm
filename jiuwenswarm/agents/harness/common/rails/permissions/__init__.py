"""Security and permission integration for AgentServer.

This package hosts jiuwenswarm-side glue code for openjiuwen security rails,
owner-scoped policies, and persistence helpers.
"""

from __future__ import annotations

from jiuwenswarm.edition import is_enterprise
from jiuwenswarm.agents.harness.common.rails.permissions.config_loader import (
    apply_permissions_config_payload,
    clear_permissions_config_cache,
    clear_session_permissions_overlay,
    get_base_permissions_config,
    get_effective_permissions_config,
    get_permissions_agent_base,
    get_permissions_session_id,
    merge_session_permissions_overlay,
    reload_permissions_from_gateway_db,
    reset_permissions_agent_base,
    reset_permissions_session_scope,
    resolve_permissions_body_from_enterprise,
    setup_permissions_agent_base,
    setup_permissions_session_scope,
)

__all__ = [
    "apply_permissions_config_payload",
    "clear_permissions_config_cache",
    "clear_session_permissions_overlay",
    "get_base_permissions_config",
    "get_effective_permissions_config",
    "get_permissions_agent_base",
    "get_permissions_session_id",
    "is_enterprise",
    "merge_session_permissions_overlay",
    "reload_permissions_from_gateway_db",
    "reset_permissions_agent_base",
    "reset_permissions_session_scope",
    "resolve_permissions_body_from_enterprise",
    "setup_permissions_agent_base",
    "setup_permissions_session_scope",
]

"""Security and permission integration for AgentServer.

This package hosts jiuwenswarm-side glue code for openjiuwen security rails,
owner-scoped policies, and persistence helpers.
"""

from __future__ import annotations

from importlib import import_module

_PACKAGE = "jiuwenswarm.agents.harness.common.rails.permissions"
_EXPORT_GROUPS = {
    "audit": ("emit_permission_audit",),
    "auto_config": (
        "AUTO_PERMISSION_MODE",
        "MANUAL_PERMISSION_MODE",
        "is_auto_permission_mode",
        "normalize_permissions_for_runtime",
        "resolve_permission_runtime_mode",
    ),
    "auto_permission_rail": ("AutoPermissionInterruptRail",),
    "openjiuwen_contract": (
        "OpenJiuwenPermissionContract",
        "build_denied_permission_response",
        "build_manual_approval_required_response",
        "build_rejected_permission_response",
        "classify_permission_result",
        "is_allow_result",
        "is_denied_result",
        "is_interrupt_result",
        "is_user_rejection_result",
        "load_openjiuwen_permission_contract",
    ),
    "sandbox_profile": ("SandboxDescriptor", "build_sandbox_profile"),
    "tool_decision_facts": ("ToolDecisionFacts", "build_tool_decision_facts"),
    "session_deny": ("SessionDenyRecord", "SessionDenyStore", "evaluate_session_deny"),
    "tool_capabilities": (
        "ToolCapability",
        "classify_tool",
        "normalize_tool_name",
    ),
}
_EXPORTS = {
    name: (f"{_PACKAGE}.{module_name}", name)
    for module_name, names in _EXPORT_GROUPS.items()
    for name in names
}

__all__ = [
    "AUTO_PERMISSION_MODE",
    "AutoPermissionInterruptRail",
    "MANUAL_PERMISSION_MODE",
    "OpenJiuwenPermissionContract",
    "SandboxDescriptor",
    "SessionDenyRecord",
    "SessionDenyStore",
    "ToolCapability",
    "ToolDecisionFacts",
    "build_tool_decision_facts",
    "build_denied_permission_response",
    "build_manual_approval_required_response",
    "build_rejected_permission_response",
    "build_sandbox_profile",
    "classify_tool",
    "classify_permission_result",
    "emit_permission_audit",
    "evaluate_session_deny",
    "is_auto_permission_mode",
    "is_allow_result",
    "is_denied_result",
    "is_interrupt_result",
    "is_user_rejection_result",
    "load_openjiuwen_permission_contract",
    "normalize_tool_name",
    "normalize_permissions_for_runtime",
    "resolve_permission_runtime_mode",
]


def __getattr__(name: str) -> object:
    """Resolve public permission exports without eager owner imports."""

    try:
        module_name, attribute_name = _EXPORTS[name]
    except KeyError as exc:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from exc
    value = getattr(import_module(module_name), attribute_name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    """Include lazy public exports in interactive module discovery."""

    return sorted(set(globals()) | set(__all__))

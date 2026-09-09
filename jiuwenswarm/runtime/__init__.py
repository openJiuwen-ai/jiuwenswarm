# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Lazy public surface for the transport-independent JiuwenSwarm Runtime."""

from __future__ import annotations

from importlib import import_module
from typing import Any

_EXPORTS = {
    "AgentRuntime": ("jiuwenswarm.runtime.service", "AgentRuntime"),
    "RuntimeStateError": ("jiuwenswarm.runtime.service", "RuntimeStateError"),
    "RuntimeSessionProvisioner": (
        "jiuwenswarm.runtime.session_provisioner",
        "RuntimeSessionProvisioner",
    ),
    "PreparedSessionProvision": (
        "jiuwenswarm.runtime.session_provisioner",
        "PreparedSessionProvision",
    ),
    "SessionCreateInput": (
        "jiuwenswarm.runtime.session_provisioner",
        "SessionCreateInput",
    ),
    "SessionCreateResult": (
        "jiuwenswarm.runtime.session_provisioner",
        "SessionCreateResult",
    ),
    "SessionDeleteLifecycle": (
        "jiuwenswarm.runtime.session_provisioner",
        "SessionDeleteLifecycle",
    ),
    "SessionDeleteResult": (
        "jiuwenswarm.runtime.session_provisioner",
        "SessionDeleteResult",
    ),
    "SessionForkInput": (
        "jiuwenswarm.runtime.session_provisioner",
        "SessionForkInput",
    ),
    "SessionForkResult": (
        "jiuwenswarm.runtime.session_provisioner",
        "SessionForkResult",
    ),
    "SessionProvisionCommitContext": (
        "jiuwenswarm.runtime.session_provisioner",
        "SessionProvisionCommitContext",
    ),
    "SessionProvisionCommitTiming": (
        "jiuwenswarm.runtime.session_provisioner",
        "SessionProvisionCommitTiming",
    ),
    "SessionProvisionError": (
        "jiuwenswarm.runtime.session_provisioner",
        "SessionProvisionError",
    ),
    "SessionProvisionInput": (
        "jiuwenswarm.runtime.session_provisioner",
        "SessionProvisionInput",
    ),
    "SessionProvisionResult": (
        "jiuwenswarm.runtime.session_provisioner",
        "SessionProvisionResult",
    ),
    "SessionProvisionState": (
        "jiuwenswarm.runtime.session_provisioner",
        "SessionProvisionState",
    ),
    "SessionProvisionStateError": (
        "jiuwenswarm.runtime.session_provisioner",
        "SessionProvisionStateError",
    ),
    "SessionProvisionerContract": (
        "jiuwenswarm.runtime.session_provisioner",
        "SessionProvisionerContract",
    ),
    "SessionSwitchInput": (
        "jiuwenswarm.runtime.session_provisioner",
        "SessionSwitchInput",
    ),
    "SessionSwitchResult": (
        "jiuwenswarm.runtime.session_provisioner",
        "SessionSwitchResult",
    ),
    "RuntimeExecutionContext": (
        "jiuwenswarm.runtime.context",
        "RuntimeExecutionContext",
    ),
    "get_current_agent_manager": (
        "jiuwenswarm.runtime.context",
        "get_current_agent_manager",
    ),
    "get_current_runtime": (
        "jiuwenswarm.runtime.context",
        "get_current_runtime",
    ),
    "get_runtime_context": (
        "jiuwenswarm.runtime.context",
        "get_runtime_context",
    ),
    "apply_resolved_mode_to_request": (
        "jiuwenswarm.runtime.request",
        "apply_resolved_mode_to_request",
    ),
    "resolve_agent_request_mode": (
        "jiuwenswarm.runtime.request",
        "resolve_agent_request_mode",
    ),
    "resolve_request_project_dir": (
        "jiuwenswarm.runtime.request",
        "resolve_request_project_dir",
    ),
    "resolve_request_runtime_mode": (
        "jiuwenswarm.runtime.request",
        "resolve_request_runtime_mode",
    ),
}


def __getattr__(name: str) -> Any:
    target = _EXPORTS.get(name)
    if target is None:
        raise AttributeError(name)
    module_name, attribute = target
    value = getattr(import_module(module_name), attribute)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_EXPORTS))


__all__ = list(_EXPORTS)

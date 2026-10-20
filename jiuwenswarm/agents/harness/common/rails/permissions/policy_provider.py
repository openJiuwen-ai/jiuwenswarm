"""Compatibility imports for the extension permission policy."""

from jiuwenswarm.extensions.sdk.policy_provider import (
    PermissionPolicyProvider,
    get_permission_policy_provider,
    set_permission_policy_provider,
)

__all__ = [
    "PermissionPolicyProvider",
    "get_permission_policy_provider",
    "set_permission_policy_provider",
]

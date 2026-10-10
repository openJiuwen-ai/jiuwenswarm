"""Optional permission policy contributed by an installed extension."""

from typing import Any, Protocol


class PermissionPolicyProvider(Protocol):
    def deny_tool(self, tool_name: str) -> str | None: ...

    def narrow_config(self, permissions: dict[str, Any]) -> dict[str, Any]: ...

    def prepare_persist(
        self, permissions: dict[str, Any], session_id: str | None
    ) -> dict[str, Any]: ...


_provider: PermissionPolicyProvider | None = None


def set_permission_policy_provider(provider: PermissionPolicyProvider | None) -> None:
    global _provider
    _provider = provider


def get_permission_policy_provider() -> PermissionPolicyProvider | None:
    return _provider

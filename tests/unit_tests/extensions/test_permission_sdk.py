"""Legacy permission imports share the extension SDK state."""

from jiuwenswarm.agents.harness.common.rails.interrupt import (
    permission_options as legacy_options,
)
from jiuwenswarm.agents.harness.common.rails.permissions import (
    policy_provider as legacy_policy,
)
from jiuwenswarm.agents.harness.common.rails.permissions import (
    tool_permission_context as legacy_context,
)
from jiuwenswarm.extensions.sdk import (
    permission_options,
    policy_provider,
    tool_permission_context,
)
from jiuwenswarm.extensions.sdk.permission_layers import operator_approval_overrides


def test_context_and_option_identity() -> None:
    assert legacy_context.TOOL_PERMISSION_CHANNEL_ID is tool_permission_context.TOOL_PERMISSION_CHANNEL_ID
    assert legacy_context.TOOL_PERMISSION_CHAT_ID is tool_permission_context.TOOL_PERMISSION_CHAT_ID
    assert legacy_context.SKILLS_REBUILD_SILENT is tool_permission_context.SKILLS_REBUILD_SILENT
    assert legacy_options.resolve_permission_action is permission_options.resolve_permission_action


def test_policy_registration_is_shared() -> None:
    class Policy:
        def deny_tool(self, tool_name: str) -> str | None:
            return None

        def narrow_config(self, permissions: dict) -> dict:
            return permissions

        def prepare_persist(self, permissions: dict, session_id: str | None) -> dict:
            return permissions

    policy = Policy()
    policy_provider.set_permission_policy_provider(policy)
    try:
        assert legacy_policy.get_permission_policy_provider() is policy
    finally:
        legacy_policy.set_permission_policy_provider(None)


def test_operator_overrides_read_all_layers(monkeypatch) -> None:
    from jiuwenswarm.agents.harness.common.rails.permissions import permissions_layers

    monkeypatch.setattr(permissions_layers, "load_global_permissions", lambda: {"approval_overrides": [{"id": "global"}]})
    monkeypatch.setattr(permissions_layers, "load_user_permissions", lambda: {"approval_overrides": [{"id": "user"}]})
    monkeypatch.setattr(
        permissions_layers,
        "load_session_permissions",
        lambda session_id: {"approval_overrides": [{"id": session_id}]},
    )

    assert operator_approval_overrides("session-1") == [
        {"id": "global"},
        {"id": "user"},
        {"id": "session-1"},
    ]

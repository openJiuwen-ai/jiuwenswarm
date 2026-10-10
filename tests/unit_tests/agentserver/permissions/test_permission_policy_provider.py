import asyncio
from types import SimpleNamespace

from jiuwenswarm.agents.harness.common.rails.interrupt.interrupt_helpers import (
    build_permission_rail,
)
from jiuwenswarm.agents.harness.common.rails.permissions.policy_provider import (
    set_permission_policy_provider,
)


def test_contributed_policy_denies_narrows_and_preserves_persistence(monkeypatch):
    from jiuwenswarm.agents.harness.common.rails.permissions import permissions_layers

    saved = []
    monkeypatch.setattr(
        permissions_layers,
        "persist_user_overlay_from_effective",
        lambda permissions, session_id=None: saved.append(permissions) or True,
    )

    class Policy:
        def deny_tool(self, tool_name):
            return "[PERMISSION_DENIED] contributed policy" if tool_name == "bash" else None

        def narrow_config(self, permissions):
            return {**permissions, "tools": {"bash": "deny"}}

        def prepare_persist(self, permissions, session_id):
            return {**permissions, "approval_overrides": [{"id": "operator"}]}

    set_permission_policy_provider(Policy())
    try:
        rail = build_permission_rail({"permissions": {"enabled": True}})
        host = rail._host
        result = asyncio.run(host.permission_scene_hook(
            SimpleNamespace(normalized_tool_name="bash", user_input=None)
        ))
        assert result == ("reject", "[PERMISSION_DENIED] contributed policy")
        assert host.get_permissions_snapshot()["tools"]["bash"] == "deny"
        assert host.persist_allow_rule({"approval_overrides": []})
        assert saved[0]["approval_overrides"] == [{"id": "operator"}]
    finally:
        set_permission_policy_provider(None)

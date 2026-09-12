"""Builder and real Host callbacks use the admitted permission snapshot."""

import asyncio
from copy import deepcopy
from types import SimpleNamespace

import pytest
from openjiuwen.harness.rails.security.tool_security_rail import PermissionInterruptRail
from openjiuwen.harness.security import PermissionSceneHookInput

from jiuwenswarm.agents.harness.common.rails.interrupt.interrupt_helpers import build_permission_rail
from jiuwenswarm.agents.harness.common.rails.permissions import permissions_layers as layers
from jiuwenswarm.agents.harness.common.rails.permissions.auto_config import normalize_permissions_for_runtime
from jiuwenswarm.agents.harness.common.rails.permissions.auto_permission_rail import AutoPermissionInterruptRail
from jiuwenswarm.agents.harness.common.rails.permissions.owner_scopes import (
    cleanup_permission_context,
    setup_permission_context,
)
from jiuwenswarm.common import config


def _policy():
    return {
        "enabled": True,
        "mode": "auto",
        "defaults": {"*": "allow"},
        "tools": {"bash": "ask", "read_file": "allow"},
        "file_guard": {"enabled": False},
        "owner_scopes": {"web": {"principal": {"tools": {"bash": "deny"}}}},
        "auto": {"reviewer_timeout_ms": 4321},
    }


@pytest.fixture
def overlays(tmp_path, monkeypatch):
    user_path = tmp_path / "user_permissions.yaml"
    session_path = tmp_path / "session_permissions.yaml"
    monkeypatch.setattr(layers, "user_permissions_path", lambda: user_path)
    monkeypatch.setattr(layers, "session_permissions_path", lambda _: session_path)
    config.dump_yaml_round_trip(user_path, {"allow_tools": ["user_tool"]})
    config.dump_yaml_round_trip(session_path, {"allow_tools": ["session_tool"]})
    return user_path, session_path


def test_installed_snapshot_owns_base_auto_and_host_after_inputs_change(tmp_path, overlays):
    snapshot = _policy()
    caller = {"permissions": _policy()}
    caller["permissions"]["tools"] = {"caller_only": "deny"}
    expected = normalize_permissions_for_runtime(deepcopy(snapshot))
    rail = build_permission_rail(
        caller, session_id="bound", enable_auto_permission=True,
        installed_permissions=snapshot, workspace_root=tmp_path,
    )
    assert isinstance(rail, AutoPermissionInterruptRail)
    assert rail.base_rail.installed_permission_config() == expected
    assert rail.installed_permission_config() == expected
    assert rail.auto_options["reviewer_timeout_ms"] == 4321

    snapshot["tools"]["bash"] = "allow"
    snapshot["owner_scopes"]["web"]["principal"]["tools"]["bash"] = "allow"
    caller["permissions"]["enabled"] = False
    caller["permissions"]["tools"]["caller_only"] = "allow"
    for path in overlays:
        config.dump_yaml_round_trip(path, {"deny_tools": ["bash"]})

    host = rail.base_rail._host
    assert host.get_permissions_snapshot(session_id="different") == expected
    host_snapshot = host.get_permissions_snapshot()
    host_snapshot["tools"]["bash"] = "deny"
    assert host.get_permissions_snapshot() == expected
    assert rail.base_rail.installed_permission_config() == expected
    assert rail.installed_permission_config() == expected


def test_installed_snapshot_never_reloads_overlays(tmp_path, monkeypatch):
    def unexpected_read(*_args, **_kwargs):
        raise AssertionError("installed snapshot must not reload any layer")

    monkeypatch.setattr(layers, "load_user_permissions", unexpected_read)
    monkeypatch.setattr(layers, "load_session_permissions", unexpected_read)
    monkeypatch.setattr(config, "get_config", unexpected_read)
    rail = build_permission_rail(
        {"permissions": _policy()}, enable_auto_permission=True,
        installed_permissions=_policy(), workspace_root=tmp_path,
    )
    assert rail.base_rail._host.get_permissions_snapshot()["tools"]["bash"] == "ask"
    token = setup_permission_context(SimpleNamespace(
        channel_id="web", metadata={"principal_user_id": "principal"},
    ))
    try:
        outcome = asyncio.run(rail.base_rail._host.permission_scene_hook(PermissionSceneHookInput(
            ctx=SimpleNamespace(session=None),
            tool_call=SimpleNamespace(id="call", name="bash", arguments={}),
            user_input=None, normalized_tool_name="bash", tool_args={}, engine=None,
        )))
    finally:
        cleanup_permission_context(token)
    assert outcome == ("reject", "[PERMISSION_DENIED] 该工具未被授权 (owner_scopes: deny)")


def test_manual_builder_retains_sdk_factory_and_dynamic_overlay_host(overlays):
    caller = {"permissions": {"enabled": True, "mode": "manual", "tools": {}}}
    rail = build_permission_rail(caller, session_id="manual")
    assert type(rail) is PermissionInterruptRail
    first = rail._host.get_permissions_snapshot()
    assert first["tools"]["user_tool"] == "allow"
    assert first["tools"]["session_tool"] == "allow"
    config.dump_yaml_round_trip(overlays[0], {"deny_tools": ["user_tool"]})
    config.dump_yaml_round_trip(overlays[1], {"allow_tools": ["later_session_tool"]})
    current = rail._host.get_permissions_snapshot()
    assert current["tools"]["user_tool"] == "deny"
    assert current["tools"]["later_session_tool"] == "allow"
    assert "session_tool" not in current["tools"]


def test_auto_without_installed_snapshot_keeps_composed_build(overlays, tmp_path):
    rail = build_permission_rail(
        {"permissions": _policy()}, session_id="auto", enable_auto_permission=True,
        workspace_root=tmp_path,
    )
    before = rail.base_rail._host.get_permissions_snapshot()
    assert "user_tool" in before["tools"] and "session_tool" in before["tools"]
    config.dump_yaml_round_trip(overlays[0], {"deny_tools": ["user_tool"]})
    assert rail.base_rail._host.get_permissions_snapshot() == before


@pytest.mark.parametrize("snapshot", [[], "policy", 1, False])
def test_builder_rejects_non_dict_installed_snapshot(snapshot):
    with pytest.raises(TypeError, match="installed_permissions_must_be_dict"):
        build_permission_rail(
            {"permissions": _policy()}, enable_auto_permission=True,
            installed_permissions=snapshot,
        )


@pytest.mark.parametrize("caller", [_policy(), {"enabled": False}, {"enabled": True, "mode": "manual"}])
def test_manual_builder_rejects_snapshot_even_if_disabled(caller):
    with pytest.raises(ValueError, match="installed_permissions_requires_auto_permission"):
        build_permission_rail({"permissions": caller}, installed_permissions=_policy())


@pytest.mark.parametrize("invalid", [
    {"enabled": False, "mode": "auto"},
    {"enabled": "true", "mode": "auto"},
    {"enabled": True, "mode": "manual"},
    {},
])
@pytest.mark.parametrize("invalid_side", ["caller", "snapshot"])
def test_builder_requires_consistent_enabled_auto_activation(invalid, invalid_side):
    caller = invalid if invalid_side == "caller" else _policy()
    snapshot = invalid if invalid_side == "snapshot" else _policy()
    with pytest.raises(ValueError, match="requires_enabled_auto_mode"):
        build_permission_rail(
            {"permissions": caller}, enable_auto_permission=True,
            installed_permissions=snapshot,
        )


def test_manual_factory_failure_still_returns_none(monkeypatch):
    import openjiuwen.harness.security as security

    def fail_factory(**_kwargs):
        raise RuntimeError("factory failure")

    monkeypatch.setattr(security, "build_permission_interrupt_rail", fail_factory)
    assert build_permission_rail({"permissions": {"enabled": True}}) is None

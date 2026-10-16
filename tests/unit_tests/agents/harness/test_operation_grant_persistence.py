"""Exercise real YAML/overlay writes, reload and the permission engine together."""

from copy import deepcopy

import pytest

from jiuwenswarm.common import config
from jiuwenswarm.agents.harness.common.rails.permissions import (
    permissions_persist as persist,
)
from openjiuwen.harness.rails.security.tool_security_rail import PermissionInterruptRail
from openjiuwen.harness.security.host import ToolPermissionHost
from openjiuwen.harness.security.permission_engine.core import PermissionEngine


@pytest.mark.asyncio
async def test_session_and_permanent_grants_reload_without_promoting_session_rules(
    tmp_path, monkeypatch
):
    path = tmp_path / "config.yaml"
    path.write_text(
        "permissions:\n  enabled: true\n  package_builtin_rules: false\n  defaults:\n    '*': allow\n"
        "  net_guard:\n    enabled: true\n    defaults: ask\n    urls: {}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "CONFIG_YAML_PATH", path)
    monkeypatch.setattr(config, "_CONFIG_YAML_PATH", path)
    monkeypatch.setattr(config, "get_config_file", lambda: path)
    monkeypatch.setattr(
        "jiuwenswarm.common.utils.get_agent_sessions_dir", lambda: tmp_path / "sessions"
    )

    def snapshot(session_id=None):
        return persist.get_permissions_with_session_overlay(
            deepcopy(config.load_yaml_round_trip(path)["permissions"]),
            session_id=session_id,
        )

    host = ToolPermissionHost(
        get_permissions_snapshot=snapshot,
        persist_session_allow_rule=persist.persist_session_allow_rule,
        persist_allow_rule=persist.persist_merged_allow_rule_snapshot,
    )
    rail = PermissionInterruptRail(config=snapshot(), host=host)
    tool = "mcp_fetch_webpage"
    session_args, permanent_args = (
        {"url": "https://api.example.com/session"},
        {"url": "https://other.org/permanent"},
    )
    assert rail._persist_session_allow(
        tool,
        session_args,
        session_id="session-a",
        authorization_mode="allow_with_scope",
        authorization_scope="domain",
    )
    assert rail._persist_allow_always(tool, permanent_args, session_id="session-a")
    other_session = PermissionEngine(snapshot("session-b"))
    original_session = PermissionEngine(snapshot("session-a"))
    assert (
        await original_session.check_permission(
            tool, {"url": "https://sub.example.com/other"}
        )
    ).is_allowed
    assert (await other_session.check_permission(tool, session_args)).needs_approval
    assert (await other_session.check_permission(tool, permanent_args)).is_allowed
    assert (
        await other_session.check_permission(tool, {"url": "https://other.org/another"})
    ).needs_approval
    assert (
        len(config.load_yaml_round_trip(path)["permissions"]["approval_overrides"]) == 1
    )
    assert persist.session_permissions_overlay_path("session-a").is_file()


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", ["session", "always"])
async def test_strict_shell_grants_survive_reload_without_crossing_scope(tmp_path, monkeypatch, scope):
    """Exercise shell UNDETERMINED grants through real YAML and session files."""
    from jiuwenswarm.common.behavior_security import runtime_permission_config

    path = tmp_path / "config.yaml"
    path.write_text(
        "permissions:\n  enabled: true\n  package_builtin_rules: false\n"
        "  tools:\n    bash: allow\n  rules: []\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "CONFIG_YAML_PATH", path)
    monkeypatch.setattr(config, "_CONFIG_YAML_PATH", path)
    monkeypatch.setattr(config, "get_config_file", lambda: path)
    monkeypatch.setattr("jiuwenswarm.common.utils.get_agent_sessions_dir", lambda: tmp_path / "sessions")
    monkeypatch.setenv("CLAW_BEHAVIOR_SECURITY", "1")
    monkeypatch.setenv("CLAW_SKILL_TOKEN", "test-token")

    def snapshot(session_id=None):
        permissions = persist.get_permissions_with_session_overlay(
            deepcopy(config.load_yaml_round_trip(path)["permissions"]), session_id=session_id,
        )
        return runtime_permission_config({
            "permissions": permissions,
            "xiaoyi_work_security": {"cloud_authorization": {"enabled": True}},
        })

    args = {"command": "printf TEST_GRANT", "workdir": str(tmp_path), "shell_type": "bash"}
    assert (await PermissionEngine(snapshot("session-a")).check_permission("bash", args)).is_undetermined
    host = ToolPermissionHost(
        get_permissions_snapshot=snapshot,
        persist_session_allow_rule=persist.persist_session_allow_rule,
        persist_allow_rule=persist.persist_merged_allow_rule_snapshot,
    )
    rail = PermissionInterruptRail(config=snapshot(), host=host)
    save = rail._persist_session_allow if scope == "session" else rail._persist_allow_always
    assert save("bash", args, session_id="session-a")

    reloaded = PermissionEngine(snapshot("session-a"))
    assert (await reloaded.check_permission("bash", args)).is_allowed
    other_session = await PermissionEngine(snapshot("session-b")).check_permission("bash", args)
    assert other_session.is_allowed if scope == "always" else other_session.is_undetermined
    for changed in (
        {**args, "command": "printf TEST_DIFFERENT"},
        {**args, "workdir": str(tmp_path / "other")},
        {**args, "shell_type": "powershell"},
    ):
        assert (await reloaded.check_permission("bash", changed)).is_undetermined

    persisted = config.load_yaml_round_trip(path)["permissions"]
    assert len(persisted.get("approval_overrides", [])) == (1 if scope == "always" else 0)
    assert "strict_tool_command_merge" not in persisted
    for restriction in ("tool", "command"):
        cfg = snapshot("session-a")
        if restriction == "tool":
            cfg["tools"]["bash"] = "deny"
        else:
            cfg["rules"] = [{"tools": ["bash"], "pattern": args["command"], "action": "deny"}]
        assert (await PermissionEngine(cfg).check_permission("bash", args)).is_denied

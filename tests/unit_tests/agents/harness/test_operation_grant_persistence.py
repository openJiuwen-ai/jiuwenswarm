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

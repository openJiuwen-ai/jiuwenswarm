"""Product deletion policies through the actual Core permission engine."""

from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from openjiuwen.harness.security.permission_engine.core import PermissionEngine
from jiuwenswarm.common.behavior_security import runtime_permission_config
from jiuwenswarm.common.permission_profile import resolve_permission_profile_rules


@pytest.fixture
def product_config(monkeypatch):
    monkeypatch.setenv("CLAW_BEHAVIOR_SECURITY", "1")
    monkeypatch.setenv("CLAW_SKILL_TOKEN", "test-token")
    path = Path(__file__).resolve().parents[3] / "jiuwenswarm/resources/config.yaml"
    config = yaml.safe_load(path.read_text(encoding="utf-8"))
    # Isolate command policy; path merge is verified separately below.
    config["permissions"]["file_guard"] = {"enabled": False}
    config["permissions"]["net_guard"] = {"enabled": False}
    return config


@pytest.mark.asyncio
@pytest.mark.parametrize("cloud_enabled", [False, True])
@pytest.mark.parametrize("enabled", [True, False])
@pytest.mark.parametrize("tool", ["bash", "powershell", "core.powershell", "mcp_exec_command", "create_terminal"])
@pytest.mark.parametrize("command", [
    "rm a.txt", "rm -rf ./dist", "rm -rf C:/temp/build", "rm *.log",
    "Remove-Item ./a.txt", "Remove-Item -Recurse -Force ./dist",
    "ri ./a.txt", "del a.txt", "erase a.txt", "rd /s /q ./dist",
    "rmdir ./dist", "unlink a.txt", "rm.exe a.txt",
    "rm a.txt; del b.txt", "rm a.txt && rm b.txt",
    "del/q a.txt", "erase/s/q C:/temp/*.txt", "rd/s/q C:/temp",
    "rmdir/s/q C:/temp", "cmd /c del/q a.txt",
])
async def test_ordinary_deletion_profile_matrix(product_config, cloud_enabled, enabled, tool, command):
    product_config["xiaoyi_work_security"]["cloud_authorization"]["enabled"] = cloud_enabled
    product_config["permissions"]["enabled"] = enabled
    result = await PermissionEngine(runtime_permission_config(product_config)).check_permission(
        tool, {"command": command},
    )
    expected = "ask" if enabled else "allow"
    if cloud_enabled and not enabled:
        # Full-access command ALLOW still needs an applicable tool ALLOW.
        expected = {
            "bash": "allow", "mcp_exec_command": "allow", "create_terminal": "ask",
            "powershell": "undetermined", "core.powershell": "undetermined",
        }[tool]
    assert result.permission.value == expected, result.matched_rule
    assert "shell_delete_files" in result.matched_rule


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [True, False])
@pytest.mark.parametrize("tool", ["bash", "powershell", "core.powershell", "mcp_exec_command", "create_terminal"])
@pytest.mark.parametrize("command", [
    "rm -rf /", 'rm -rf "/"', "rm -rf C:", "rm -rf C:/",
    "Remove-Item -Recurse -Force C:/", "del -Recurse -Force C:/",
    "rd /s /q C:\\", "echo ready; rm -rf C:/",
    "Remove-Item -Fo -Recurse C:\\", "Remove-Item -Rec -Fo C:\\*",
    "rm -rf /./", "rm -rf C:/.", "rm -rf \\\\?\\C:\\",
    "Remove-Item -LiteralPath '\\\\?\\C:\\' -Force -Recurse",
    "Remove-Item -LiteralPath '\\\\.\\C:\\' -Fo -Rec",
    "del /s /q C:\\*", "erase/s/q C:\\*.*",
    "rd/s/q C:\\", "rmdir/s/q C:\\", "rd/q/s C:\\",
])
async def test_root_deletion_still_asks(product_config, enabled, tool, command):
    product_config["permissions"]["enabled"] = enabled
    result = await PermissionEngine(runtime_permission_config(product_config)).check_permission(
        tool, {"command": command},
    )
    assert result.permission.value == "ask", result.matched_rule
    assert "builtin" in result.matched_rule


@pytest.mark.asyncio
@pytest.mark.parametrize("cloud_enabled,expected", [(False, "allow"), (True, "undetermined")])
@pytest.mark.parametrize("command", ["delta a.txt", "rmdir_backup a.txt", "unlinker a.txt"])
async def test_delete_rule_does_not_match_longer_command_names(product_config, command, cloud_enabled, expected):
    product_config["xiaoyi_work_security"]["cloud_authorization"]["enabled"] = cloud_enabled
    result = await PermissionEngine(runtime_permission_config(product_config)).check_permission(
        "bash", {"command": command},
    )
    assert result.permission.value == expected, result.matched_rule
    assert "shell_delete_files" not in result.matched_rule


@pytest.mark.asyncio
async def test_full_access_does_not_override_path_deny(product_config, tmp_path):
    product_config["permissions"]["enabled"] = False
    product_config["permissions"]["file_guard"] = {
        "enabled": True, "defaults": {"read": "allow", "write": "allow", "exec": "allow"},
        "paths": [{"path": tmp_path.as_posix(), "match": "prefix", "write": "deny"}],
    }
    target = (tmp_path / "secret.txt").as_posix()
    result = await PermissionEngine(runtime_permission_config(product_config), workspace_root=tmp_path).check_permission(
        "powershell", {"command": f'Remove-Item "{target}"'},
    )
    assert result.permission.value == "deny", result.matched_rule


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["ask", "deny"])
async def test_full_access_delete_does_not_allow_other_compound_operations(product_config, action):
    product_config["permissions"]["enabled"] = False
    product_config["permissions"]["rules"].append({
        "tools": ["shell"], "pattern": "other-command *", "action": action,
    })
    result = await PermissionEngine(runtime_permission_config(product_config)).check_permission(
        "powershell", {"command": "rm a.txt; other-command x"},
    )
    assert result.permission.value == action, result.matched_rule


def test_resolving_rules_preserves_input_and_other_rules():
    cfg = {"enabled": False, "rules": [
        {"action": "ask", "profile_actions": {"full_access": "allow"}},
        {"action": "deny"},
        {"action": "ask", "profile_actions": {"full_access": []}},
        {"layer": "builtin", "action": "ask", "profile_actions": {"full_access": "allow"}},
    ]}
    before = deepcopy(cfg)
    assert [r["action"] for r in resolve_permission_profile_rules(cfg)["rules"]] == [
        "allow", "deny", "ask", "ask",
    ]
    assert cfg == before
    cfg["enabled"] = True
    assert resolve_permission_profile_rules(cfg)["rules"][0]["action"] == "ask"


@pytest.mark.asyncio
@pytest.mark.parametrize("cloud_enabled", [False, True])
async def test_existing_rail_snapshot_and_reload_follow_profile(product_config, monkeypatch, cloud_enabled):
    from jiuwenswarm.agents.harness.common.rails.interrupt.interrupt_helpers import build_permission_rail
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter
    product_config["xiaoyi_work_security"]["cloud_authorization"]["enabled"] = cloud_enabled
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.common.rails.permissions.permissions_persist.get_permissions_with_session_overlay",
        lambda **_: deepcopy(product_config["permissions"]),
    )
    rail = build_permission_rail(product_config)
    adapter = SimpleNamespace(_permission_rail=rail, _model=None)
    for enabled in (True, False, True, False):
        product_config["permissions"]["enabled"] = enabled
        original = deepcopy(product_config)
        JiuWenSwarmDeepAdapter._update_permission_rail(adapter, product_config)
        expected = "ask" if enabled else "allow"
        if cloud_enabled and not enabled:
            expected = "undetermined"  # The shipped template has no powershell tool ALLOW.
        for cfg in (rail._engine.config, rail._host.get_permissions_snapshot()):
            engine = PermissionEngine(cfg)
            result = await engine.check_permission("powershell", {"command": "Remove-Item ./a.txt"})
            assert result.permission.value == expected, result.matched_rule
            root = await engine.check_permission("powershell", {"command": "rm -rf C:"})
            assert root.permission.value == "ask", root.matched_rule
        assert product_config == original

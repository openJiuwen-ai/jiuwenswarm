# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Verify Swarm's pinned agent-core owns generic command-risk policy."""

from __future__ import annotations

from pathlib import Path

import yaml

from openjiuwen.harness.security import PermissionEngine, PermissionLevel
from openjiuwen.harness.security.permission_engine.toolguard.builtin_rules import (
    get_package_builtin_rules_path,
)


def _engine() -> PermissionEngine:
    return PermissionEngine(
        {
            "enabled": True,
            "package_builtin_rules": True,
            "defaults": {"*": "allow"},
            "tools": {"bash": "allow", "powershell": "allow"},
            "rules": [],
        }
    )


def test_agent_core_package_builtin_rules_are_installed() -> None:
    assert get_package_builtin_rules_path().is_file()


def test_swarm_default_config_enables_agent_core_permission_rules() -> None:
    config_path = Path(__file__).resolve().parents[4] / "jiuwenswarm" / "resources" / "config.yaml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    permissions = config["permissions"]

    assert permissions["enabled"] is True
    assert permissions["package_builtin_rules"] is True


def test_agent_core_package_rules_own_generic_shell_risks() -> None:
    engine = _engine()
    cases = (
        ("bash", "rm -rf /tmp/workspace-dist", PermissionLevel.ASK),
        ("powershell", "del /f /s /q C:\\temp\\build", PermissionLevel.ASK),
        ("powershell", "rd /s /q C:\\temp\\build", PermissionLevel.ASK),
        ("powershell", "Remove-Item -Recurse -Force C:\\temp\\build", PermissionLevel.ASK),
        ("powershell", "format C:", PermissionLevel.DENY),
        ("bash", "shutdown -h now", PermissionLevel.DENY),
        ("bash", "reboot", PermissionLevel.DENY),
        ("powershell", "diskpart", PermissionLevel.DENY),
        ("powershell", "reg delete HKCU\\Software\\Example", PermissionLevel.DENY),
        ("bash", "mkfs.ext4 /dev/sdb1", PermissionLevel.DENY),
    )

    for tool_name, command, expected in cases:
        permission, matched_rule = engine.check_tool_permission_directly(
            tool_name, {"command": command}
        )
        assert permission == expected, (command, permission, matched_rule)

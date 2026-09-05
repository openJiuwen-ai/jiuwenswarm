# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Product user shell rules must apply to the powershell tool, not only bash."""

from __future__ import annotations

from pathlib import Path

import yaml

from openjiuwen.harness.security import PermissionLevel
from openjiuwen.harness.security.permission_engine.toolguard.tool_policy import (
    evaluate_tiered_policy,
)

from jiuwenswarm.agents.harness.common.rails.permissions.permission_compose import (
    compose_host_effective_permissions,
)

_REPO_ROOT = Path(__file__).resolve().parents[4]
_PRODUCT_CONFIGS = (
    _REPO_ROOT / "jiuwenswarm" / "resources" / "config.yaml",
    _REPO_ROOT / "jiuwenswarm" / "resources" / "config.team.distributed.leader.yaml",
    _REPO_ROOT / "jiuwenswarm" / "resources" / "config.team.distributed.teammate.yaml",
)


def _load_permissions(path: Path) -> dict:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    perms = data.get("permissions")
    assert isinstance(perms, dict), path
    return perms


def test_product_shell_rules_cover_powershell() -> None:
    for path in _PRODUCT_CONFIGS:
        perms = _load_permissions(path)
        assert perms.get("tools", {}).get("powershell") == "allow", path.name
        rules = perms.get("rules") or []
        assert rules, path.name
        for rule in rules:
            tools = rule.get("tools") or []
            assert "powershell" in tools, (path.name, rule.get("id"))


def test_product_cd_rule_allows_powershell_cd() -> None:
    effective = compose_host_effective_permissions(
        global_permissions=_load_permissions(_PRODUCT_CONFIGS[0]),
    )
    level, matched = evaluate_tiered_policy(
        effective, "powershell", {"command": "cd workspace"},
    )
    assert level == PermissionLevel.ALLOW
    assert "shell_allow_cd" in matched


_PS_ALLOW_CASES = (
    (
        'Set-Location "C:/Users/hanzhibin/.jiuwenswarm/agent/workspace"',
        "shell_allow_set_location",
    ),
    ("Push-Location ..", "shell_allow_push_location"),
    ("Pop-Location", "shell_allow_pop_location"),
    ("Get-Location", "shell_allow_get_location"),
    ("Get-ChildItem -Name *.docx", "shell_allow_get_childitem"),
    ("Get-Item README.md", "shell_allow_get_item"),
    ("Get-Content notes.txt", "shell_allow_get_content"),
    ("Write-Output hello", "shell_allow_write_output"),
    ("Write-Host hello", "shell_allow_write_host"),
    ("Select-Object Name, Length", "shell_allow_select_object"),
    ("sl .", "shell_allow_sl"),
    ("gci -Name", "shell_allow_gci"),
    ("gc notes.txt", "shell_allow_gc"),
)


def test_product_powershell_cmdlet_allow_list() -> None:
    effective = compose_host_effective_permissions(
        global_permissions=_load_permissions(_PRODUCT_CONFIGS[0]),
    )
    for command, rule_id in _PS_ALLOW_CASES:
        level, matched = evaluate_tiered_policy(
            effective, "powershell", {"command": command},
        )
        assert level == PermissionLevel.ALLOW, command
        assert rule_id in matched, (command, matched)


def test_product_powershell_location_pipeline_allows() -> None:
    effective = compose_host_effective_permissions(
        global_permissions=_load_permissions(_PRODUCT_CONFIGS[0]),
    )
    level, matched = evaluate_tiered_policy(
        effective,
        "powershell",
        {"command": "Get-ChildItem | Select-Object Name"},
    )
    assert level == PermissionLevel.ALLOW
    assert "shell_allow_get_childitem" in matched
    assert "shell_allow_select_object" in matched


def test_product_powershell_unlisted_cmdlets_follow_tool_allow() -> None:
    effective = compose_host_effective_permissions(
        global_permissions=_load_permissions(_PRODUCT_CONFIGS[0]),
    )
    for command in ("mkdir test", "New-Item test"):
        level, matched = evaluate_tiered_policy(
            effective, "powershell", {"command": command},
        )
        assert level == PermissionLevel.ALLOW, (command, matched)
        assert "tools.powershell" in matched, (command, matched)


_PS_DELETE_ASK_CASES = (
    ("Remove-Item test", "shell_ask_remove_item"),
    ("Remove-Item -Recurse -Force tmp", "shell_ask_remove_item"),
    ("ri test.txt", "shell_ask_ri"),
    ("rmdir olddir", "shell_ask_rmdir"),
    ("erase temp.log", "shell_ask_erase"),
    ("del temp.log", "shell_ask_del"),
    ("rd olddir", "shell_ask_rd"),
    ("rm temp.log", "shell_ask_rm"),
)


def test_product_powershell_delete_cmdlets_ask() -> None:
    effective = compose_host_effective_permissions(
        global_permissions=_load_permissions(_PRODUCT_CONFIGS[0]),
    )
    for command, rule_id in _PS_DELETE_ASK_CASES:
        level, matched = evaluate_tiered_policy(
            effective, "powershell", {"command": command},
        )
        assert level == PermissionLevel.ASK, (command, matched)
        assert rule_id in matched, (command, matched)

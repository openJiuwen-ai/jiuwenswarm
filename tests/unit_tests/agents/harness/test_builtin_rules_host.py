from pathlib import Path

import pytest

from jiuwenswarm.agents.harness.common.rails.permissions.builtin_rules_host import (
    with_package_builtin_rules,
)


def _package_builtin_yaml_present() -> bool:
    try:
        from openjiuwen.harness.security.permission_engine.toolguard.builtin_rules import (
            get_package_builtin_rules_path,
        )
    except ImportError:
        return False
    return get_package_builtin_rules_path().is_file()


def test_with_package_builtin_rules_does_not_load_swarm_yaml(
    monkeypatch, tmp_path: Path
) -> None:
    missing = tmp_path / "missing_builtin_rules.yaml"
    monkeypatch.setattr(
        "openjiuwen.harness.security.permission_engine.toolguard.builtin_rules.get_package_builtin_rules_path",
        lambda: missing,
    )
    monkeypatch.setattr(
        "openjiuwen.harness.security.permission_engine.toolguard.builtin_rules.inline_package_command_rules",
        lambda permissions: permissions,
    )
    cfg = with_package_builtin_rules(
        {
            "enabled": True,
            "tools": {"bash": "allow"},
            "defaults": {"*": "allow"},
            "rules": [],
        }
    )
    builtin = [
        rule
        for rule in (cfg.get("rules") or [])
        if isinstance(rule, dict) and rule.get("layer") == "builtin"
    ]
    assert builtin == []


@pytest.mark.skipif(
    not _package_builtin_yaml_present(),
    reason="installed openjiuwen does not ship harness/resources/builtin_rules.yaml",
)
def test_with_package_builtin_rules_inlines_find_delete() -> None:
    cfg = with_package_builtin_rules(
        {
            "enabled": True,
            "tools": {"bash": "allow"},
            "defaults": {"*": "allow"},
            "rules": [],
        }
    )
    builtin = [
        rule
        for rule in (cfg.get("rules") or [])
        if isinstance(rule, dict) and rule.get("layer") == "builtin"
    ]
    assert any(rule.get("id") == "shell_fs_recursive_or_forced_delete" for rule in builtin)


@pytest.mark.skipif(
    not _package_builtin_yaml_present(),
    reason="installed openjiuwen does not ship harness/resources/builtin_rules.yaml",
)
def test_persist_remember_writes_override_for_find_delete() -> None:
    from openjiuwen.harness.security.permission_engine.approve.persist_rule_merge import (
        merge_permission_allow_rule_into_permissions,
    )

    cmd = (
        'cd "C:/Users/hanzhibin/Documents/JiuwenSwarm/2026-09-04/chat-1/work" '
        '&& find ./logs -name "*.log" -delete && echo "done"'
    )
    cfg = with_package_builtin_rules(
        {
            "enabled": True,
            "tools": {"bash": "allow"},
            "defaults": {"*": "allow"},
            "rules": [],
            "approval_overrides": [],
        }
    )
    merged, applied = merge_permission_allow_rule_into_permissions(
        cfg, "bash", {"command": cmd}
    )
    assert applied is True
    overrides = merged.get("approval_overrides") or []
    assert any(
        isinstance(item, dict)
        and "find ./logs -name" in str(item.get("pattern") or "")
        and "-delete" in str(item.get("pattern") or "")
        for item in overrides
    )

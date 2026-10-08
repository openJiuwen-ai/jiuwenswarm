"""Command-only ShellGuard configuration, preserving other permission policies."""

from copy import deepcopy


def get_shell_guard_rules():
    from openjiuwen.harness.security.permission_engine.toolguard.builtin_rules import load_package_command_rules
    from openjiuwen.harness.security.permission_engine.toolguard.tool_categories import shell_tools_from_config
    from jiuwenswarm.common.config import get_config

    permissions = (get_config() or {}).get("permissions") or {}
    names = shell_tools_from_config(permissions) | {"shell"}
    rules = []
    for rule in permissions.get("rules") or []:
        if not isinstance(rule, dict) or rule.get("layer") == "builtin":
            continue
        targets = rule.get("tools") or []
        targets = [targets] if isinstance(targets, str) else targets
        if targets and all(target in names for target in targets):
            rules.append(deepcopy(rule))
    return {"rules": rules, "builtin_rules": deepcopy(load_package_command_rules())}


def get_shell_guard_config():
    from jiuwenswarm.common.config import get_config

    permissions = (get_config() or {}).get("permissions") or {}
    guard = permissions.get("shell_guard") or {}
    return {"builtin_rules_enabled": guard.get("builtin_rules_enabled", True)}


def update_shell_guard_config(patch):
    from jiuwenswarm.common.config import update_config

    if not isinstance(patch, dict) or set(patch) - {"builtin_rules_enabled"}:
        raise ValueError("patch accepts only builtin_rules_enabled")
    if any(not isinstance(value, bool) for value in patch.values()):
        raise ValueError("builtin_rules_enabled must be boolean")

    def mutate(data):
        guard = data.setdefault("permissions", {}).setdefault("shell_guard", {})
        guard.update(deepcopy(patch))
        return data

    data = update_config(mutate)
    guard = data["permissions"]["shell_guard"]
    return {"builtin_rules_enabled": guard.get("builtin_rules_enabled", True)}

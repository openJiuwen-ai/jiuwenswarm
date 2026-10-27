"""Explicit NetGuard → sandbox domain/default synchronization. ASK is skipped."""

from copy import deepcopy


def project_net_guard_to_sandbox(guard):
    from jiuwenswarm.server.sandbox_policy_render import _validate_domain

    allow, deny, skipped = [], [], []
    for pattern, action in (guard.get("urls") or {}).items():
        if action not in {"allow", "deny"}:
            skipped.append(
                {"pattern": pattern, "reason": "询问仅在工具调用前处理，不同步到沙箱"}
            )
            continue
        try:
            domain = _validate_domain(pattern)
        except ValueError:
            skipped.append(
                {
                    "pattern": pattern,
                    "reason": "沙箱仅支持域名或 *.域名，不支持 URL、端口及其他通配符",
                }
            )
            continue
        target = allow if action == "allow" else deny
        if domain not in target:
            target.append(domain)
    default = guard.get("defaults", "allow")
    if default == "ask":
        skipped.append(
            {
                "pattern": "defaults",
                "reason": "沙箱不支持默认询问，仅同步域名规则，默认允许",
            }
        )
    return {
        "default": "deny" if default == "deny" else "allow",
        "allow_domains": allow,
        "deny_domains": deny,
    }, skipped


def sync_net_guard_to_sandbox():
    from jiuwenswarm.common.behavior_security import runtime_permission_config
    from jiuwenswarm.common.config import update_config

    skipped = []

    def mutate(data):
        permissions = runtime_permission_config(data)
        guard = permissions.get("net_guard") or {}
        if (data.get("sandbox") or {}).get("type", "jiuwenbox") != "jiuwenbox":
            raise ValueError("sandbox.network.sync currently supports jiuwenbox only")
        if not permissions.get("enabled") or not guard.get("enabled"):
            raise ValueError("NetGuard must be enabled before synchronization")
        network, omitted = project_net_guard_to_sandbox(guard)
        skipped.extend(omitted)
        # A normalized domain with conflicting actions remains denied.
        urls = {domain: "allow" for domain in network["allow_domains"]}
        urls.update({domain: "deny" for domain in network["deny_domains"]})
        data.setdefault("sandbox", {})["urls"] = urls
        data["sandbox"]["network_default"] = network["default"]
        return data

    data = update_config(mutate)
    return {
        "urls": deepcopy(data["sandbox"]["urls"]),
        "default": data["sandbox"]["network_default"],
        "skipped": skipped,
        "restart_required": True,
    }


def validate_sandbox_urls(urls):
    """Validate saved sandbox rules without silently dropping invalid policy."""
    from jiuwenswarm.server.sandbox_policy_render import _validate_domain

    if not isinstance(urls, dict):
        raise ValueError("sandbox.urls must be an object {domain: allow|deny}")
    normalized = {}
    for pattern, action in urls.items():
        if not isinstance(pattern, str) or action not in ("allow", "deny"):
            raise ValueError("sandbox.urls accepts domain keys and allow|deny actions only")
        domain = _validate_domain(pattern)
        if normalized.get(domain) != "deny":
            normalized[domain] = action
    return normalized


def render_saved_sandbox_urls(policy_path):
    """Materialize saved config only when starting/applying the sandbox service."""
    from pathlib import Path

    from jiuwenswarm.common.config import get_config
    from jiuwenswarm.server.sandbox_policy_render import (
        _is_windows,
        _linux_runtime_copy_path,
        _runtime_copy_path,
        get_native_sandbox_network_config,
        get_sandbox_network_config,
        has_native_sandbox_network_config,
        set_sandbox_network_config,
    )

    sandbox = get_config().get("sandbox") or {}
    urls = validate_sandbox_urls(sandbox.get("urls", {}))
    default = sandbox.get("network_default")
    expected = _runtime_copy_path() if _is_windows() else _linux_runtime_copy_path()
    if policy_path is None or Path(policy_path).resolve() != expected.resolve():
        raise ValueError("sandbox.urls requires the platform runtime policy copy")
    current = get_native_sandbox_network_config()
    native_allowed = current["allow_domains"] if has_native_sandbox_network_config() else []
    native_denied = current["deny_domains"] if has_native_sandbox_network_config() else []
    allowed = list(dict.fromkeys([
        *native_allowed,
        *(domain for domain, action in urls.items() if action == "allow"),
    ]))
    denied = list(dict.fromkeys([
        *native_denied,
        *(domain for domain, action in urls.items() if action == "deny"),
    ]))
    allowed = [domain for domain in allowed if domain not in denied]
    stored = set_sandbox_network_config(
        current["disable_all"],
        allowed,
        denied,
        default=default,
    )
    if get_sandbox_network_config() != stored:
        raise OSError("Sandbox network configuration could not be saved")

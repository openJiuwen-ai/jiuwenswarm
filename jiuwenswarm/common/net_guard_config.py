"""Explicit NetGuard → sandbox domain synchronization. ASK is skipped."""

from copy import deepcopy
import hashlib
import json
import uuid


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
    if guard.get("defaults", "allow") != "allow":
        skipped.append(
            {
                "pattern": "defaults",
                "reason": "沙箱域名名单不支持同步默认询问或默认拒绝",
            }
        )
    return {"allow_domains": allow, "deny_domains": deny}, skipped


def sync_net_guard_to_sandbox():
    from jiuwenswarm.common.config import update_config

    skipped = []

    def mutate(data):
        permissions = data.get("permissions") or {}
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
        data["sandbox"]["urls_revision"] = uuid.uuid4().hex
        return data

    data = update_config(mutate)
    return {
        "urls": deepcopy(data["sandbox"]["urls"]),
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
    """Apply the saved snapshot under its own origin, preserving other writers.

    Missing urls means there is no sync snapshot. An explicit empty snapshot
    clears only records previously imported by this synchronization path.
    """
    from pathlib import Path

    from jiuwenswarm.common.config import get_config
    from jiuwenswarm.server.sandbox_policy_render import (
        _is_windows,
        _linux_runtime_copy_path,
        _runtime_copy_path,
        get_sandbox_network_config,
        _panel_records,
    )
    from jiuwenswarm.agents.harness.common.rails.security_lists import audit, store
    from jiuwenswarm.server.security_lists_render import (
        collect_sandbox_lists, render_linux_copy, render_sandbox_copy,
    )

    sandbox = get_config().get("sandbox") or {}
    expected = _runtime_copy_path() if _is_windows() else _linux_runtime_copy_path()
    if policy_path is None or Path(policy_path).resolve() != expected.resolve():
        raise ValueError("sandbox.urls requires the platform runtime policy copy")
    if "urls" in sandbox:
        urls = validate_sandbox_urls(sandbox["urls"])
        stamp = hashlib.sha256(json.dumps(
            [urls, sandbox.get("urls_revision")], sort_keys=True,
        ).encode("utf-8")).hexdigest()
        cfg = get_config()
        previous = ((cfg.get("security_lists") or {}).get("migrations") or {}).get("sandbox_urls_stamp")
        if stamp != previous:
            result = store.replace_records_by_origin(
                origin="sandbox_network_sync", list_type="domain",
                records=_panel_records("domain", list(urls.items())),
                reject_conflicts=True,
            )
            audit.log_event(audit.AUDIT_CHANGE, op="sandbox.network.apply", **result)
            if result["skipped"]:
                # Keep a failed apply retryable; no stamp until the entire
                # snapshot can be represented under its own editable origin.
                raise ValueError(f"Sandbox domain rules owned by security_lists: {result['skipped']}")
            from jiuwenswarm.common.config import update_config

            def mark_applied(data):
                section = data.setdefault("security_lists", {})
                section.setdefault("migrations", {})["sandbox_urls_stamp"] = stamp
                return data

            update_config(mark_applied)
    render_sandbox_copy()
    render_linux_copy()
    wanted = collect_sandbox_lists()
    current = get_sandbox_network_config()
    if (set(current["allow_domains"]) != set(wanted["allowed_domains"])
            or set(current["deny_domains"]) != set(wanted["blocked_domains"])):
        raise OSError("Sandbox network configuration could not be saved")
    from jiuwenswarm.server.security_lists_rpc import _publish_enforcement

    _publish_enforcement()

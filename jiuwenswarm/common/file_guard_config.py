"""FileGuard configuration and explicit, save-only sandbox synchronization."""

from copy import deepcopy
from pathlib import Path
import sys


def validate_paths(paths):
    if not isinstance(paths, list):
        raise ValueError("paths must be a list")
    result = []
    seen = set()
    for rule in paths:
        if not isinstance(rule, dict) or set(rule) - {"path", "read", "write", "exec", "match"}:
            raise ValueError("path rules accept only path/read/write/exec/match")
        path = rule.get("path")
        if not isinstance(path, str) or not path.strip() or any(ord(c) < 32 for c in path):
            raise ValueError("path must be a non-empty path without control characters")
        match = rule.get("match", "prefix")
        if match not in ("prefix", "glob"):
            raise ValueError("match must be prefix or glob")
        key = (match, path)
        if key in seen:
            raise ValueError(f"duplicate path rule: {path}")
        seen.add(key)
        for axis in ("read", "write", "exec"):
            if axis in rule and rule[axis] not in ("allow", "ask", "deny"):
                raise ValueError(f"{axis} must be allow, ask or deny")
        normalized = deepcopy(rule)
        if normalized.get("read") == "deny":
            normalized.update(write="deny", exec="deny")
        result.append(normalized)
    return result


def get_file_guard_config():
    from jiuwenswarm.agents.harness.common.rails.security_lists.legacy_compat import guard_view
    return guard_view("file_guard")


def update_file_guard_config(patch):
    if not isinstance(patch, dict) or set(patch) - {"enabled", "defaults", "workspace", "paths"}:
        raise ValueError("patch accepts only enabled/defaults/workspace/paths")
    if "enabled" in patch and not isinstance(patch["enabled"], bool):
        raise ValueError("enabled must be boolean")
    for section in ("defaults", "workspace"):
        if section in patch:
            axes = patch[section]
            if not isinstance(axes, dict) or set(axes) - {"read", "write", "exec"}:
                raise ValueError(f"{section} accepts read/write/exec only")
            if any(value not in ("allow", "ask", "deny") for value in axes.values()):
                raise ValueError(f"invalid {section} permission")
    if "paths" in patch:
        validate_paths(patch["paths"])

    from jiuwenswarm.agents.harness.common.rails.security_lists.legacy_compat import update_guard
    normalized = deepcopy(patch)
    if "paths" in normalized:
        normalized["paths"] = validate_paths(normalized["paths"])
    return update_guard("file_guard", normalized)


def sandbox_file_buckets(files):
    """Compile concrete path read/write rules for the sandbox backend."""
    if not isinstance(files, list):
        raise ValueError("sandbox.files must be a list; use sandbox.files.sync")
    buckets = {"allow": [], "deny": []}
    blocked = []
    for rule in validate_paths(files):
        path = rule["path"]
        if rule.get("match", "prefix") != "prefix" or any(c in path for c in "*?["):
            raise ValueError(f"sandbox requires a concrete path: {path}")
        if not Path(path).is_absolute() or not Path(path).exists():
            raise ValueError(f"sandbox requires an existing absolute path: {path}")
        read, write = rule.get("read"), rule.get("write")
        if read not in ("allow", "deny") or write not in ("allow", "deny"):
            raise ValueError(f"sandbox requires explicit allow/deny read and write: {path}")
        canonical = str(Path(path).resolve())
        if read == "deny":
            if sys.platform != "win32":
                # This restriction must never be silently removed from a policy.
                raise NotImplementedError(f"sandbox read deny is unsupported on this platform: {path}")
            blocked.append(canonical)
        else:
            buckets["allow" if write == "allow" else "deny"].append(canonical)
    # An explicit deny must not silently override a more permissive child rule.
    for restricted in blocked + buckets["deny"]:
        for allowed in buckets["allow"] + (buckets["deny"] if restricted in blocked else []):
            if Path(allowed).is_relative_to(Path(restricted)):
                raise ValueError(f"conflicting sandbox rules: {restricted} and {allowed}")
    return buckets, blocked


def project_file_guard_to_sandbox(guard):
    """Project the compatibility subset; return unsupported entries explicitly."""
    skipped = []
    paths = validate_paths(guard.get("paths", []))
    defaults = guard.get("defaults") or {}
    files = []
    candidates = []
    for rule in paths:
        # FileGuard supports rules the sandbox cannot represent. Report those
        # rules without preventing the supported paths from being synchronized.
        candidate = {"path": rule["path"]}
        for axis in ("read", "write"):
            candidate[axis] = rule.get(axis, defaults.get(axis, "ask"))
        if candidate["read"] == "deny":
            candidate["write"] = "deny"
        try:
            sandbox_file_buckets([{**candidate, "match": rule.get("match", "prefix")}])
        except ValueError as exc:
            skipped.append({"path": rule["path"], "reason": str(exc)})
            continue
        candidates.append(candidate)
    # Restrictive rules win regardless of their order in FileGuard.
    candidates.sort(key=lambda r: (r["read"] != "deny", r["write"] != "deny"))
    for candidate in candidates:
        try:
            sandbox_file_buckets([*files, candidate])
        except ValueError as exc:
            skipped.append({"path": candidate["path"], "reason": str(exc)})
            continue
        files.append(candidate)
    return files, skipped


def sync_file_guard_to_sandbox():
    from jiuwenswarm.common.config import update_config
    from jiuwenswarm.server.runtime.agent_adapter.sysop_builder import build_filesystem_policy

    skipped = []

    def mutate(data):
        from jiuwenswarm.agents.harness.common.rails.security_lists import store
        store._ensure_section(data)
        permissions = data.get("permissions") or {}
        if (data.get("sandbox") or {}).get("type", "jiuwenbox") != "jiuwenbox":
            raise ValueError("sandbox.files.sync currently supports jiuwenbox only")
        from jiuwenswarm.agents.harness.common.rails.security_lists.legacy_compat import guard_view
        guard = guard_view("file_guard", data=data, owned_only=True)
        if not permissions.get("enabled") or guard.get("enabled") is False:
            raise ValueError("FileGuard must be enabled before synchronization")
        files, omitted = project_file_guard_to_sandbox(guard)
        skipped.extend(omitted)
        build_filesystem_policy(files)
        data.setdefault("sandbox", {})["files"] = files
        data["sandbox"]["files_from_security_lists"] = True
        return data

    data = update_config(mutate)
    return {"files": deepcopy(data["sandbox"]["files"]), "skipped": skipped, "restart_required": True}

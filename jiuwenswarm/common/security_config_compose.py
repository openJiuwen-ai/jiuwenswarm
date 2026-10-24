"""Validate security sources and build a candidate native permissions snapshot.

The candidate is not published until the matching core and runtime apply path
understand ``permissions.composition``.
"""

from copy import deepcopy
from typing import Any

from openjiuwen.harness.security.permission_engine.policy_sources import (
    DEFAULT_SOURCE_ORDER,
    SOURCE_USER,
    validate_policy_sources,
)

from jiuwenswarm.common.config import _validate_tools_map
from jiuwenswarm.common.file_guard_config import validate_paths


DEFAULT_SOURCES = DEFAULT_SOURCE_ORDER
_LEVELS = frozenset({"allow", "ask", "deny"})
_RULE_FIELDS = frozenset({"id", "tools", "pattern", "severity", "action", "description", "match_type", "profile_actions"})
_PROFILE_ACTIONS = frozenset({"default", "full_access"})


def validate_settings(settings: Any) -> dict[str, Any]:
    if not isinstance(settings, dict):
        raise ValueError("settings must be an object")
    security = settings.get("security", {})
    if not isinstance(security, dict):
        raise ValueError("security must be an object")
    if set(security) - {"policy_sources", "fail_closed"}:
        raise ValueError("unknown security setting")
    sources = security.get("policy_sources", [{"id": name, "enabled": True} for name in DEFAULT_SOURCES])
    validate_policy_sources(sources)
    fail_closed = security.get("fail_closed", {"enabled": False})
    if not isinstance(fail_closed, dict) or set(fail_closed) != {"enabled"} or not isinstance(fail_closed["enabled"], bool):
        raise ValueError("fail_closed.enabled must be boolean")
    normalized = deepcopy(settings)
    normalized["security"] = {**security, "policy_sources": sources, "fail_closed": fail_closed}
    return normalized


def validate_source(source: Any, *, cloud: bool = False) -> dict[str, Any]:
    if not isinstance(source, dict) or set(source) - ({"permissions", "policy_version"} if cloud else {"permissions"}):
        raise ValueError("source accepts permissions only" + (" and policy_version" if cloud else ""))
    if cloud and (type(source.get("policy_version")) is not int or source["policy_version"] < 1):
        raise ValueError("policy_version must be a positive integer")
    permissions = source.get("permissions", {})
    if not isinstance(permissions, dict) or set(permissions) - {"tools", "rules", "file_guard", "net_guard", "approval_overrides"}:
        raise ValueError("source contains unsupported permission settings")
    if cloud and "approval_overrides" in permissions:
        raise ValueError("cloud cannot provide approval overrides")
    normalized = deepcopy(source)
    perms = normalized.setdefault("permissions", {})
    if "tools" in perms:
        perms["tools"] = _validate_tools_map(perms["tools"])
    if "rules" in perms:
        if not isinstance(perms["rules"], list):
            raise ValueError("rules must be a list")
        ids = set()
        from jiuwenswarm.common.config import _normalize_rule_severity_action, _normalize_rule_tools, _validate_rule_regex
        for rule in perms["rules"]:
            if not isinstance(rule, dict) or set(rule) - _RULE_FIELDS or "layer" in rule:
                raise ValueError("invalid rule fields")
            rid = rule.get("id")
            if not isinstance(rid, str) or not rid.strip() or rid in ids:
                raise ValueError("rule id must be unique and non-empty")
            ids.add(rid)
            rule["tools"] = _normalize_rule_tools(rule.get("tools"))
            if not rule["tools"] or not isinstance(rule.get("pattern"), str) or not rule["pattern"].strip():
                raise ValueError("rule requires tools and pattern")
            _normalize_rule_severity_action(rule)
            if "profile_actions" in rule:
                actions = rule["profile_actions"]
                if (not isinstance(actions, dict) or not actions or set(actions) - _PROFILE_ACTIONS
                        or any(not isinstance(action, str) or action not in _LEVELS for action in actions.values())):
                    raise ValueError("invalid rule profile_actions")
            _validate_rule_regex(rule)
    if "file_guard" in perms:
        guard = perms["file_guard"]
        if not isinstance(guard, dict) or set(guard) != {"paths"}:
            raise ValueError("source file_guard accepts paths only")
        guard["paths"] = validate_paths(guard["paths"])
    if "net_guard" in perms:
        guard = perms["net_guard"]
        if not isinstance(guard, dict) or set(guard) != {"urls"} or not isinstance(guard["urls"], dict):
            raise ValueError("source net_guard accepts urls only")
        for pattern, action in guard["urls"].items():
            if not isinstance(pattern, str) or not pattern.strip() or not isinstance(action, str) or action not in _LEVELS:
                raise ValueError("invalid network URL rule")
    if "approval_overrides" in perms and (not isinstance(perms["approval_overrides"], list) or any(not isinstance(item, dict) for item in perms["approval_overrides"])):
        raise ValueError("approval_overrides must be a list of objects")
    return normalized


def compose_candidate(settings: dict[str, Any], sources: dict[str, dict[str, Any]], revision: str) -> dict[str, Any]:
    """Produce original permission fields plus minimal source metadata; never apply it here."""
    settings = validate_settings(settings)
    selected = [entry["id"] for entry in settings["security"]["policy_sources"] if entry["enabled"]]
    for source_id in selected:
        if source_id not in sources:
            raise ValueError(f"enabled source unavailable: {source_id}")
    effective = deepcopy(settings.get("permissions", {}))
    if not isinstance(effective, dict):
        raise ValueError("permissions settings must be an object")
    tools, rules, paths, urls, url_layers = {}, [], [], {}, {}
    for source_id in selected:
        source = sources[source_id]
        perms = source.get("permissions", {})
        for name, level in perms.get("tools", {}).items():
            if name not in tools or level == "deny":
                tools[name] = level
        for rule in perms.get("rules", []):
            rules.append({**deepcopy(rule), "layer": source_id})
        for path in perms.get("file_guard", {}).get("paths", []):
            paths.append({**deepcopy(path), "layer": source_id})
        for pattern, level in perms.get("net_guard", {}).get("urls", {}).items():
            if pattern not in urls or level == "deny":
                urls[pattern], url_layers[pattern] = level, source_id
    effective["tools"] = tools
    effective["rules"] = rules
    effective.setdefault("file_guard", {})["paths"] = paths
    effective.setdefault("net_guard", {})["urls"] = urls
    effective["approval_overrides"] = deepcopy(sources.get(SOURCE_USER, {}).get("permissions", {}).get("approval_overrides", [])) if SOURCE_USER in selected else []
    effective["composition"] = {
        "schema_version": 1, "revision": revision, "source_order": selected, "net_url_layers": url_layers,
    }
    return effective

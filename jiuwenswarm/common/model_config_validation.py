"""Validation for the model catalog and model-group request patches."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

from jiuwenswarm.common.model_errors import MODEL_GROUP_INVALID, ModelSelectionError

PLACEHOLDER_API_BASES = frozenset({"https://example.com/compatible-mode/v1"})
EXAMPLE_DOMAINS = frozenset({"example.com", "example.org", "example.net"})
PLACEHOLDER_MODEL_NAMES = frozenset({"your-model-name"})
PLACEHOLDER_API_KEYS = frozenset({"sk-xxxxxxxxx"})


def is_placeholder_api_base(api_base: str) -> bool:
    value = str(api_base or "").strip()
    if not value:
        return False
    if value in PLACEHOLDER_API_BASES:
        return True
    try:
        host = urlparse(value).hostname or ""
    except Exception:
        return False
    return bool(host) and any(host == domain or host.endswith(f".{domain}") for domain in EXAMPLE_DOMAINS)


def is_placeholder_model_entry(mcc: dict | None) -> bool:
    mcc = mcc or {}
    return (
        is_placeholder_api_base(str(mcc.get("api_base", "") or "").strip())
        or str(mcc.get("model_name", "") or "").strip() in PLACEHOLDER_MODEL_NAMES
        or str(mcc.get("api_key", "") or "").strip() in PLACEHOLDER_API_KEYS
    )


def model_client_config_view(mcc: Any) -> dict[str, Any]:
    if isinstance(mcc, dict):
        return {key: mcc.get(key, "") or "" for key in ("api_base", "api_key", "model_name")}
    return {key: getattr(mcc, key, None) or "" for key in ("api_base", "api_key", "model_name")}


FORBIDDEN_REQUEST_KEYS = frozenset({
    "model", "messages", "tools", "stream", "api_key", "api_base",
    "provider", "client_provider", "client_id", "strategy", "num_retries",
    "timeout_seconds", "enable_health_check", "health_check_interval_seconds",
    "enable_observability", "context_window", "_source",
})


def _validate_patch(value: Any, location: str, errors: list[str]) -> None:
    if value is None:
        return
    if not isinstance(value, dict):
        errors.append(f"{location} must be an object")
        return
    forbidden = sorted(set(value).intersection(FORBIDDEN_REQUEST_KEYS))
    if forbidden:
        errors.append(f"{location} contains forbidden fields: {', '.join(forbidden)}")


def validate_models_config(models: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    defaults = models.get("defaults") or []
    agentos = models.get("agentos") or []
    groups = models.get("groups") or []
    if not all(isinstance(v, list) for v in (defaults, agentos, groups)):
        return ["models.defaults, models.agentos and models.groups must be arrays"]

    model_ids: set[str] = set()
    default_models = 0
    for source, entries in (("defaults", defaults), ("agentos", agentos)):
        for index, entry in enumerate(entries):
            if not isinstance(entry, dict):
                errors.append(f"{source}[{index}] must be an object")
                continue
            model_id = str(entry.get("model_id") or "").strip()
            if not model_id:
                errors.append(f"{source}[{index}].model_id is required")
            elif model_id in model_ids:
                errors.append(f"duplicate model_id: {model_id}")
            else:
                model_ids.add(model_id)
            if entry.get("is_default") is True:
                if source == "agentos":
                    errors.append(f"agentos[{index}] cannot be default")
                else:
                    default_models += 1
    if default_models > 1:
        errors.append("at most one default model is allowed")

    group_ids: set[str] = set()
    default_groups = 0
    for index, group in enumerate(groups):
        if not isinstance(group, dict):
            errors.append(f"groups[{index}] must be an object")
            continue
        group_id = str(group.get("model_group_id") or "").strip()
        if not group_id:
            errors.append(f"groups[{index}].model_group_id is required")
        elif group_id in group_ids:
            errors.append(f"duplicate model_group_id: {group_id}")
        else:
            group_ids.add(group_id)
        if group.get("is_default") is True:
            default_groups += 1
            if group.get("enabled") is False:
                errors.append(f"default model group {group_id!r} is disabled")
        routes = group.get("routes")
        if not isinstance(routes, list) or not routes:
            errors.append(f"model group {group_id!r} must contain at least one route")
            routes = []
        route_ids: set[str] = set()
        for route_index, route in enumerate(routes):
            if not isinstance(route, dict):
                errors.append(f"group {group_id!r} route[{route_index}] must be an object")
                continue
            route_id = str(route.get("route_id") or "").strip()
            model_id = str(route.get("model_id") or "").strip()
            if not route_id:
                errors.append(f"group {group_id!r} route[{route_index}].route_id is required")
            elif route_id in route_ids:
                errors.append(f"group {group_id!r} has duplicate route_id {route_id!r}")
            route_ids.add(route_id)
            if model_id not in model_ids:
                errors.append(f"group {group_id!r} references missing model_id {model_id!r}")
            _validate_patch(route.get("request_overrides"), f"group {group_id!r} route {route_id!r}", errors)
        _validate_patch(group.get("request_config"), f"group {group_id!r}.request_config", errors)
    if default_groups > 1:
        errors.append("at most one default model group is allowed")
    return errors


def raise_if_invalid(models: dict[str, Any]) -> None:
    errors = validate_models_config(models)
    if errors:
        raise ModelSelectionError(MODEL_GROUP_INVALID, "; ".join(errors), errors=errors)

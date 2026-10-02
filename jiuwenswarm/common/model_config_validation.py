"""Validation for the model catalog and model-group request patches."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from jiuwenswarm.common.model_errors import MODEL_GROUP_INVALID, ModelSelectionError

logger = logging.getLogger(__name__)

PLACEHOLDER_API_BASES = frozenset({"https://example.com/compatible-mode/v1"})
EXAMPLE_DOMAINS = frozenset({"example.com", "example.org", "example.net"})
PLACEHOLDER_MODEL_NAMES = frozenset({"your-model-name"})
PLACEHOLDER_API_KEYS = frozenset({"sk-xxxxxxxxx"})


@dataclass(frozen=True)
class ModelProbeResult:
    """Successful model connection probe and its user-visible content."""

    response: Any
    content: str


def _model_probe_output(response: Any) -> tuple[str, bool]:
    """Extract display content and decide whether a probe produced output."""
    if hasattr(response, "content"):
        content = response.content
    elif isinstance(response, dict):
        content = response.get("content", "")
    else:
        content = "" if response is None else str(response)

    if isinstance(response, dict):
        reasoning_content = response.get("reasoning_content")
    else:
        reasoning_content = getattr(response, "reasoning_content", None)

    # Reasoning models may spend the small probe budget entirely on
    # reasoning_content and leave the normal content field empty.
    has_text_output = any(
        isinstance(value, str) and bool(value.strip())
        for value in (content, reasoning_content)
    )

    # Some backends report thinking in a field the client does not map (for
    # example, Ollama's "reasoning"). Generated-token usage still proves that
    # the endpoint, credentials, and model are valid.
    usage = (
        response.get("usage_metadata")
        if isinstance(response, dict)
        else getattr(response, "usage_metadata", None)
    )
    output_tokens = (
        usage.get("output_tokens")
        if isinstance(usage, dict)
        else getattr(usage, "output_tokens", None)
    )
    has_output = has_text_output or (
        isinstance(output_tokens, (int, float)) and output_tokens > 0
    )
    return content if isinstance(content, str) else "", has_output


async def probe_model_connection(
    model: Any,
    *,
    token_limits: Sequence[int] = (3, 16),
    invoke_kwargs: Mapping[str, Any] | None = None,
    timeout_seconds: float | None = None,
    log_context: str = "model connection probe",
) -> ModelProbeResult:
    """Probe a model using each output-token allowance until one succeeds.

    Both an invocation exception and a response without content, reasoning, or
    generated-token usage count as a failed attempt. The final failure is
    raised to the caller so each subsystem can format its own error response.
    ``timeout_seconds`` bounds each complete invocation even when a model
    client ignores its own timeout setting.
    """
    extra_kwargs = dict(invoke_kwargs or {})
    limits = tuple(token_limits)
    if not limits:
        raise ValueError("token_limits must contain at least one value")

    for attempt, max_tokens in enumerate(limits):
        try:
            invocation = model.invoke(
                messages=[{"role": "user", "content": "Hi"}],
                max_tokens=max_tokens,
                **extra_kwargs,
            )
            response = (
                await invocation
                if timeout_seconds is None
                else await asyncio.wait_for(invocation, timeout=timeout_seconds)
            )
            content, has_output = _model_probe_output(response)
            if not has_output:
                raise ValueError("Empty response from model")
            return ModelProbeResult(response=response, content=content)
        except Exception as exc:  # noqa: BLE001
            if attempt + 1 >= len(limits):
                raise
            next_max_tokens = limits[attempt + 1]
            logger.info(
                "[%s] max_tokens=%d failed, retrying with %d: %s",
                log_context,
                max_tokens,
                next_max_tokens,
                exc,
            )

    raise AssertionError("model probe attempts unexpectedly exhausted")


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


def _validate_model_detail(entry: dict[str, Any], location: str, errors: list[str]) -> None:
    detail = entry.get("model_detail")
    if detail is None:
        return
    if not isinstance(detail, dict):
        errors.append(f"{location}.model_detail must be an object")
        return
    for key in ("fallback_tag", "model_description"):
        value = detail.get(key)
        if value is not None and not isinstance(value, str):
            errors.append(f"{location}.model_detail.{key} must be a string")


def _validate_routing(value: Any, location: str, errors: list[str]) -> None:
    if value is None:
        return
    if not isinstance(value, dict):
        errors.append(f"{location} must be an object")
        return
    strategy = value.get("strategy")
    if strategy not in (None, "ordered-failover", "tag-filtered"):
        errors.append(f"{location}.strategy is invalid")
    retries = value.get("num_retries")
    if retries is not None:
        invalid_type = not isinstance(retries, int) or isinstance(retries, bool)
        if invalid_type or retries < 0:
            errors.append(f"{location}.num_retries must be a non-negative integer")
    strategy_kwargs = value.get("strategy_kwargs")
    if strategy_kwargs is not None and not isinstance(strategy_kwargs, dict):
        errors.append(f"{location}.strategy_kwargs must be an object")
    # Recognized legacy strategies are accepted for upgrade compatibility,
    # but are neither executed nor forwarded to Core in model-pool mode.


def validate_models_config(models: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    defaults = models.get("defaults") or []
    agentos = models.get("agentos") or []
    groups = models.get("groups") or []
    if not all(isinstance(v, list) for v in (defaults, agentos, groups)):
        return ["models.defaults, models.agentos and models.groups must be arrays"]

    model_ids: set[str] = set()
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
            # Legacy defaults are scoped to each model name, not the entire
            # catalog. Preserve those flags when adding stable IDs.
            if source == "agentos" and entry.get("is_default") is True:
                errors.append(f"agentos[{index}] cannot be default")
            _validate_model_detail(entry, f"{source}[{index}]", errors)

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
            if "enabled" in route and not isinstance(route["enabled"], bool):
                errors.append(f"group {group_id!r} route {route_id!r}.enabled must be a boolean")
            if not route_id:
                errors.append(f"group {group_id!r} route[{route_index}].route_id is required")
            elif route_id in route_ids:
                errors.append(f"group {group_id!r} has duplicate route_id {route_id!r}")
            route_ids.add(route_id)
            if model_id not in model_ids:
                errors.append(f"group {group_id!r} references missing model_id {model_id!r}")
            _validate_patch(route.get("request_overrides"), f"group {group_id!r} route {route_id!r}", errors)
        _validate_patch(group.get("request_config"), f"group {group_id!r}.request_config", errors)
        _validate_routing(group.get("routing"), f"group {group_id!r}.routing", errors)
    if default_groups > 1:
        errors.append("at most one default model group is allowed")
    return errors


def raise_if_invalid(models: dict[str, Any]) -> None:
    errors = validate_models_config(models)
    if errors:
        raise ModelSelectionError(MODEL_GROUP_INVALID, "; ".join(errors), errors=errors)

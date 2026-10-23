# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Collaboration switches and interval; Jev uses the shared model connection."""

from __future__ import annotations

from copy import deepcopy
import re
from typing import Any, Callable

from jiuwenswarm.common.browser_settings import resolve_jev_connection

DEFAULT_MOA_INTERVAL = 3

# Paths are relative to experimental; the old top-level moa section is archival.
MOA_CONFIG_SPECS = {
    "multi_model_collaboration_enabled": (("multi_model_collaboration_enabled",), False),
    "moa_jev_enabled": (("multi_model_collaboration", "jev_enabled"), False),
    "moa_verifier_interval": (("multi_model_collaboration", "verifier_interval"), DEFAULT_MOA_INTERVAL),
}
# Reject obsolete writes explicitly instead of silently storing an unused connection.
_LEGACY_JEV_KEYS = frozenset({
    "moa_jev_api_base", "moa_jev_model_name", "moa_jev_api_key", "moa_jev_api_key_clear",
})
MOA_CONFIG_KEYS = frozenset({*MOA_CONFIG_SPECS, *_LEGACY_JEV_KEYS})


def _section(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _nested(source: dict[str, Any], path: tuple[str, ...], default: Any) -> Any:
    value: Any = source
    for key in path:
        if not isinstance(value, dict) or key not in value:
            return default
        value = value[key]
    return default if value is None else value


def _bool(value: Any, field: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in {"true", "false"}:
        return value.strip().lower() == "true"
    raise ValueError(f"{field} must be a boolean")


def flatten_moa_for_config_panel(
    raw: dict[str, Any], *, resolve_value: Callable[[Any], Any] | None = None,
) -> dict[str, str]:
    """Expose experiment settings without reading or exposing old MoA roles."""
    experimental = _section(raw.get("experimental"))
    result = {}
    for key, (path, default) in MOA_CONFIG_SPECS.items():
        value = _nested(experimental, path, default)
        result[key] = (
            "true" if value is True else "false"
        ) if isinstance(default, bool) else str(value)
    return result


def build_moa_config_from_frontend(
    params: dict[str, Any], raw: dict[str, Any],
    *, resolve_value: Callable[[Any], Any] | None = None,
) -> dict[str, Any]:
    """Validate collaboration settings against the single shared Jev connection."""
    if _LEGACY_JEV_KEYS.intersection(params):
        raise ValueError("Jev connection settings moved to Models > Jev; update the shared configuration")
    candidate = deepcopy(_section(raw.get("experimental")))
    for key, (path, default) in MOA_CONFIG_SPECS.items():
        value = params.get(key, _nested(candidate, path, default))
        if key == "moa_verifier_interval":
            text = str(value if value is not None else "").strip()
            if not re.fullmatch(r"[0-9]+", text) or int(text) < 1:
                raise ValueError("moa_verifier_interval must be a positive integer")
            value = int(text)
        elif isinstance(default, bool):
            value = _bool(value, key)
        target = candidate
        for segment in path[:-1]:
            if not isinstance(target.get(segment), dict):
                target[segment] = {}
            target = target[segment]
        target[path[-1]] = value

    settings = candidate["multi_model_collaboration"]
    jev_edit = "moa_jev_enabled" in params
    if settings["jev_enabled"] and (candidate["multi_model_collaboration_enabled"] or jev_edit):
        resolve_jev_connection(raw, resolve_value=resolve_value)
    return candidate


def save_moa_config_from_frontend(params: dict[str, Any]) -> None:
    """Write all collaboration fields in one locked transaction."""
    from jiuwenswarm.common.config import resolve_env_vars, update_config

    def mutate(data: dict[str, Any]) -> dict[str, Any]:
        candidate = build_moa_config_from_frontend(params, data, resolve_value=resolve_env_vars)
        data["experimental"] = candidate
        return data

    update_config(mutate)


def redact_moa_credentials(value: Any) -> Any:
    """Redact the write-only key before transport payload debug logging."""
    if isinstance(value, dict):
        return {
            key: "****" if key == "moa_jev_api_key" else redact_moa_credentials(child)
            for key, child in value.items()
        }
    if isinstance(value, list):
        return [redact_moa_credentials(child) for child in value]
    return value

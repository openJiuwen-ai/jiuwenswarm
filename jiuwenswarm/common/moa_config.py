# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Experimental multi-model collaboration settings and write-only JEV credentials."""

from __future__ import annotations

from copy import deepcopy
import re
from typing import Any, Callable
from urllib.parse import urlsplit

DEFAULT_MOA_INTERVAL = 3
DEFAULT_JEV_API_BASE = "https://api.typesafe.ai"
DEFAULT_JEV_MODEL_NAME = "jev-latest"

# Paths are relative to experimental; the old top-level moa section is archival.
MOA_CONFIG_SPECS = {
    "multi_model_collaboration_enabled": (("multi_model_collaboration_enabled",), False),
    "moa_jev_enabled": (("multi_model_collaboration", "jev_enabled"), False),
    "moa_verifier_interval": (("multi_model_collaboration", "verifier_interval"), DEFAULT_MOA_INTERVAL),
    "moa_jev_api_base": (("multi_model_collaboration", "jev", "api_base"), DEFAULT_JEV_API_BASE),
    "moa_jev_model_name": (("multi_model_collaboration", "jev", "model_name"), DEFAULT_JEV_MODEL_NAME),
    "moa_jev_api_key": (("multi_model_collaboration", "jev", "api_key"), ""),
}
MOA_CONFIG_KEYS = frozenset({*MOA_CONFIG_SPECS, "moa_jev_api_key_clear"})


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


def _is_blank_secret(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def flatten_moa_for_config_panel(
    raw: dict[str, Any], *, resolve_value: Callable[[Any], Any] | None = None,
) -> dict[str, str]:
    """Expose experiment settings without reading or exposing old MoA roles."""
    experimental = _section(raw.get("experimental"))
    result = {}
    for key, (path, default) in MOA_CONFIG_SPECS.items():
        value = _nested(experimental, path, default)
        result[key] = "" if key == "moa_jev_api_key" else (
            "true" if value is True else "false"
        ) if isinstance(default, bool) else str(value)
    secret = _nested(experimental, MOA_CONFIG_SPECS["moa_jev_api_key"][0], "")
    if resolve_value is not None:
        secret = resolve_value(secret)
    result["moa_jev_api_key_configured"] = "true" if secret else "false"
    return result


def build_moa_config_from_frontend(
    params: dict[str, Any], raw: dict[str, Any],
    *, resolve_value: Callable[[Any], Any] | None = None,
) -> dict[str, Any]:
    """Validate one complete experimental candidate, retaining blank secrets."""
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
        elif key == "moa_jev_api_key" and (
            key not in params or _is_blank_secret(value)
        ):
            value = _nested(candidate, path, default)
        else:
            if not isinstance(value, str):
                raise ValueError(f"{key} must be a string")
            value = value.strip()
        target = candidate
        for segment in path[:-1]:
            if not isinstance(target.get(segment), dict):
                target[segment] = {}
            target = target[segment]
        target[path[-1]] = value

    settings = candidate["multi_model_collaboration"]
    if "moa_jev_api_key_clear" in params and _bool(params["moa_jev_api_key_clear"], "moa_jev_api_key_clear"):
        settings["jev"]["api_key"] = ""
        settings["jev_enabled"] = False
    jev_edit = any(
        key in params
        for key in ("moa_jev_enabled", "moa_jev_api_base", "moa_jev_model_name", "moa_jev_api_key")
    )
    if settings["jev_enabled"] and (candidate["multi_model_collaboration_enabled"] or jev_edit):
        jev = settings["jev"]
        if not jev["api_base"] or not jev["model_name"]:
            raise ValueError("JEV api_base and model_name are required")
        try:
            url = urlsplit(jev["api_base"])
            valid_url = url.scheme in {"http", "https"} and bool(url.hostname)
            valid_url = valid_url and url.username is None and url.password is None
        except ValueError:
            valid_url = False
        if not valid_url:
            raise ValueError("JEV api_base must be an HTTP(S) URL without embedded credentials")
        secret = jev["api_key"]
        if resolve_value is not None:
            secret = resolve_value(secret)
        if not secret:
            raise ValueError("JEV api_key is required")
    return candidate


def save_moa_config_from_frontend(
    params: dict[str, Any], *, encrypt_secret: Callable[[str], str] | None = None,
) -> None:
    """Write all collaboration fields in one locked transaction."""
    from jiuwenswarm.common.config import resolve_env_vars, update_config

    def mutate(data: dict[str, Any]) -> dict[str, Any]:
        candidate = build_moa_config_from_frontend(params, data, resolve_value=resolve_env_vars)
        new_secret = params.get("moa_jev_api_key")
        clearing = "moa_jev_api_key_clear" in params and _bool(params["moa_jev_api_key_clear"], "moa_jev_api_key_clear")
        if not clearing and isinstance(new_secret, str) and new_secret.strip():
            if encrypt_secret is not None:
                try:
                    candidate["multi_model_collaboration"]["jev"]["api_key"] = encrypt_secret(
                        new_secret.strip(),
                    )
                except Exception:
                    raise RuntimeError("failed to encrypt JEV credential") from None
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

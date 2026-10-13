# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""A2A duplex supervisor settings: flat Web keys <-> ``config.yaml`` ``duplex_router``.

The policy, the supervisor call and the safe interrupt live in agent-core
(``TeamAgentSpec.duplex``). This module only validates and maps the settings.
A typed API key is stored in ``.env`` through the shared ``CONFIG_SET_ENV_MAP`` flow
(``DUPLEX_ROUTER_API_KEY``). The jev format derives its defaults from the url host
(``jev_defaults``): TypeSafe uses ``/systemone``, every other JEV service the OpenRouter
generic ``/decisions``; typesafe.ai / openrouter.ai hosts fall back to the shared browser
Jev key. config.yaml never holds a key.
"""

from __future__ import annotations

import math
import os
from typing import Any
from urllib.parse import urlparse

from ruamel.yaml.scalarstring import DoubleQuotedScalarString

from jiuwenswarm.agents.harness.common.browser_config import JEV_PROVIDER_DEFAULTS
from jiuwenswarm.common.config_panel.models_handlers import ConfigPanelBadRequest

DUPLEX_ROUTER_API_KEY_ENV = "DUPLEX_ROUTER_API_KEY"
DUPLEX_ROUTER_API_KEY_PARAM = "duplex_router_api_key"
DUPLEX_ROUTER_DEFAULTS: dict[str, Any] = {
    "enabled": False,
    "mode": "off",
    "format": "openai",
    "api_base": "",
    "endpoint": "",
    "model": "",
    "api_key_env": "",
    "timeout_seconds": 2.0,
}
DUPLEX_ROUTER_FIELDS = ("enabled", "mode", "format", "api_base", "endpoint", "model", "timeout_seconds")
DUPLEX_ROUTER_PARAM_KEYS = frozenset(f"duplex_router_{field}" for field in DUPLEX_ROUTER_FIELDS)
_CHOICES = {
    "mode": ("off", "active"),
    "format": ("openai", "anthropic", "jev"),
}
_JEV_DEFAULT_BASE = JEV_PROVIDER_DEFAULTS["typesafe"]["api_base"]
_JEV_HOSTS = {"typesafe.ai": ("typesafe", "/systemone"), "openrouter.ai": ("openrouter", "/decisions")}
_MAX_TIMEOUT_SECONDS = 60.0


def jev_defaults(api_base: str) -> dict[str, str]:
    """JEV defaults by url host: only TypeSafe has its own endpoint; others use OpenRouter's."""
    host = (urlparse(str(api_base or "").strip() or _JEV_DEFAULT_BASE).hostname or "").lower()
    for domain, (provider, endpoint) in _JEV_HOSTS.items():
        if host == domain or host.endswith(f".{domain}"):
            defaults = JEV_PROVIDER_DEFAULTS[provider]
            return {"endpoint": endpoint, "model": defaults["model"], "api_key_env": defaults["api_key_env"]}
    return {"endpoint": "/decisions", "model": "", "api_key_env": DUPLEX_ROUTER_API_KEY_ENV}


def _is_plain_http_url(raw: str) -> bool:
    parsed = urlparse(raw)
    return (
        parsed.scheme in {"http", "https"}
        and bool(parsed.hostname)
        and not parsed.username
        and not parsed.password
        and not parsed.fragment
    )


def _parse_field(field: str, value: Any) -> Any:
    raw = str(value if value is not None else "").strip()
    if field == "enabled":
        return value if isinstance(value, bool) else raw.lower() in {"1", "true", "yes", "on"}
    if field in _CHOICES:
        if raw not in _CHOICES[field]:
            raise ConfigPanelBadRequest(f"invalid duplex_router.{field}: {raw!r}")
        # Quote it: YAML 1.1 readers parse a bare `off` as False.
        return DoubleQuotedScalarString(raw) if field == "mode" else raw
    if field == "timeout_seconds":
        try:
            seconds = float(raw)
        except ValueError as exc:
            raise ConfigPanelBadRequest("invalid duplex_router.timeout_seconds") from exc
        if not (math.isfinite(seconds) and 0 < seconds <= _MAX_TIMEOUT_SECONDS):
            raise ConfigPanelBadRequest("duplex_router.timeout_seconds must be in (0, 60]")
        return seconds
    if field == "api_base" and raw and not _is_plain_http_url(raw):
        raise ConfigPanelBadRequest("duplex_router.api_base must be an http(s) URL without credentials")
    if field == "endpoint" and raw and not raw.startswith("/"):
        raise ConfigPanelBadRequest("duplex_router.endpoint must start with /")
    return raw


def _host(api_base: Any) -> str:
    return (urlparse(str(api_base or "").strip()).hostname or "").lower()


def _target_changed(current: dict[str, Any], updates: dict[str, Any]) -> bool:
    """True when the save changes the format or the url host, so the stored key may belong elsewhere."""
    format_changed = "format" in updates and str(updates["format"]) != str(current["format"])
    host_changed = "api_base" in updates and _host(updates["api_base"]) != _host(current["api_base"])
    return format_changed or host_changed


def build_duplex_router_update(params: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    """Validate the duplex keys in ``params``; return the ``duplex_router`` patch.

    Raises ``ConfigPanelBadRequest`` before anything is written.
    """
    updates = {
        field: _parse_field(field, params[f"duplex_router_{field}"])
        for field in DUPLEX_ROUTER_FIELDS
        if f"duplex_router_{field}" in params
    }
    if not updates:
        return {}
    current = {**DUPLEX_ROUTER_DEFAULTS, **(current or {})}
    typed_key = params.get(DUPLEX_ROUTER_API_KEY_PARAM)
    if str(typed_key or "").strip():
        # A typed key always wins.
        updates["api_key_env"] = DUPLEX_ROUTER_API_KEY_ENV
    elif typed_key is not None or _target_changed(current, updates):
        # Cleared key, or a new format / url host: drop the old key, use the host default key.
        updates["api_key_env"] = ""
    merged = {**current, **updates}
    if merged["enabled"]:
        if merged["format"] == "jev":
            defaults = jev_defaults(merged["api_base"])
            key_env = merged["api_key_env"] or defaults["api_key_env"]
            required = () if defaults["model"] else ("model",)
        else:
            key_env, required = DUPLEX_ROUTER_API_KEY_ENV, ("api_base", "model")
        key = typed_key if key_env == DUPLEX_ROUTER_API_KEY_ENV and typed_key is not None else os.getenv(key_env)
        missing = [field for field in required if not str(merged.get(field) or "").strip()]
        if not str(key or "").strip():
            missing.append(f"api_key ({key_env})")
        if missing:
            raise ConfigPanelBadRequest(f"duplex_router is enabled but missing: {', '.join(missing)}")
    return updates


def duplex_router_settings_view(raw: dict[str, Any]) -> dict[str, str]:
    """Flat config.get view. The key itself is never returned, only whether it is set."""
    section = {**DUPLEX_ROUTER_DEFAULTS, **(raw.get("duplex_router") or {})}
    view = {f"duplex_router_{field}": str(section[field]) for field in DUPLEX_ROUTER_FIELDS}
    view["duplex_router_enabled"] = "true" if section["enabled"] is True else "false"
    view["duplex_router_api_key_env"] = str(section.get("api_key_env") or "")
    view["duplex_router_api_key_configured"] = _configured(DUPLEX_ROUTER_API_KEY_ENV)
    for provider, defaults in JEV_PROVIDER_DEFAULTS.items():
        view[f"duplex_router_jev_{provider}_model"] = defaults["model"]
        view[f"duplex_router_jev_{provider}_key_configured"] = _configured(defaults["api_key_env"])
    return view


def _configured(env_key: str) -> str:
    return "true" if os.getenv(env_key, "").strip() else "false"

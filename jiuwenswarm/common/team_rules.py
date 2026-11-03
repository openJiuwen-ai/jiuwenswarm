# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Group team rules settings (``config.yaml`` ``team_rules``).

The delivery policy, the supervisor call and the safe interrupt live in
agent-core (``TeamAgentSpec.team_rules``). This module only maps the settings page
to ``config.yaml`` and builds the ``team_rules`` dict handed to the team spec.

The section stores no secret, only the name of the environment variable that
holds the API key: ``TEAM_RULES_API_KEY`` when the user typed a key on the
settings page. A ``jev`` supervisor with no own key reuses the Jev connection
configured in the browser settings (``browser.decision``): its key, and its
address and model when those are left empty.

Kept free of ``jiuwenswarm.common.config`` so the Front process can import it.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

from jiuwenswarm.common.browser_settings import JEV_PROVIDER_DEFAULTS

TEAM_RULES_SECTION = "team_rules"
TEAM_RULES_API_KEY_ENV = "TEAM_RULES_API_KEY"
TEAM_RULES_API_KEY_PARAM = "team_rules_api_key"
TEAM_RULES_FORMATS = ("openai", "anthropic", "jev")
TEAM_RULES_DEFAULTS: dict[str, Any] = {
    "enabled": False,
    "format": "openai",
    "api_base": "",
    "model": "",
    "api_key_env": "",
}
# Settings fields stored in config.yaml; the key itself goes to .env.
TEAM_RULES_FIELDS = ("enabled", "format", "api_base", "model")
TEAM_RULES_PARAM_KEYS = frozenset(f"team_rules_{field}" for field in TEAM_RULES_FIELDS)


class TeamRulesSettingsError(ValueError):
    """A settings value the supervisor could not run with."""


def _section(config: Mapping[str, Any] | None) -> dict[str, Any]:
    raw = (config or {}).get(TEAM_RULES_SECTION)
    return {**TEAM_RULES_DEFAULTS, **(raw if isinstance(raw, dict) else {})}


def _text(value: Any) -> str:
    return str(value if value is not None else "").strip()


def _enabled(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return _text(value).lower() in {"1", "true", "yes", "on"}


def browser_jev_connection(config: Mapping[str, Any] | None) -> dict[str, str]:
    """The Jev connection from the browser settings: address, model, key variable, endpoint.

    Same resolution as ``resolve_jev_connection`` (MoA) without requiring the key:
    provider defaults overlaid with ``browser.decision``; a custom provider speaks
    the TypeSafe wire format.
    """
    browser = (config or {}).get("browser")
    decision = browser.get("decision") if isinstance(browser, dict) else None
    decision = decision if isinstance(decision, dict) else {}
    provider = decision.get("provider", "typesafe")
    if not isinstance(provider, str) or provider not in JEV_PROVIDER_DEFAULTS:
        provider = "typesafe"
    settings = {**JEV_PROVIDER_DEFAULTS[provider], **decision}
    return {
        "provider": provider,
        "api_base": _text(settings.get("api_base")).rstrip("/"),
        "model": _text(settings.get("model")),
        "api_key_env": _text(settings.get("api_key_env")),
        "endpoint": "/decisions" if provider == "openrouter" else "/systemone",
    }


def _key_set(env_name: str, environment: Mapping[str, str]) -> bool:
    return bool(env_name) and bool(_text(environment.get(env_name)))


def team_rules_panel_view(
    config: Mapping[str, Any] | None, environment: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Flat config.get fields for the settings page."""
    environment = os.environ if environment is None else environment
    section = _section(config)
    jev = browser_jev_connection(config)
    return {
        "team_rules_enabled": "true" if _enabled(section["enabled"]) else "false",
        "team_rules_format": _text(section["format"]) or "openai",
        "team_rules_api_base": _text(section["api_base"]),
        "team_rules_model": _text(section["model"]),
        "team_rules_jev_api_base": jev["api_base"],
        "team_rules_jev_model": jev["model"],
        "team_rules_jev_key_configured": "true" if _key_set(jev["api_key_env"], environment) else "false",
    }


def _valid_url(raw: str) -> bool:
    try:
        url = urlsplit(raw)
    except ValueError:
        return False
    return url.scheme in {"http", "https"} and bool(url.hostname) and not (
        url.username or url.password or url.fragment
    )


def _parse_team_rules_params(params: Mapping[str, Any]) -> dict[str, Any]:
    """The submitted ``team_rules_*`` fields, normalised and format-checked."""
    updates: dict[str, Any] = {}
    for field in TEAM_RULES_FIELDS:
        key = f"team_rules_{field}"
        if key not in params:
            continue
        value = params[key]
        if field == "enabled":
            updates[field] = _enabled(value)
            continue
        updates[field] = _text(value)
    if "format" in updates and updates["format"] not in TEAM_RULES_FORMATS:
        raise TeamRulesSettingsError(f"invalid team_rules.format: {updates['format']!r}")
    if updates.get("api_base") and not _valid_url(updates["api_base"]):
        raise TeamRulesSettingsError("team_rules.api_base must be an http(s) URL without credentials")
    return updates


def _missing_required(
    merged: Mapping[str, Any],
    config: Mapping[str, Any] | None,
    own_key: str,
    environment: Mapping[str, str],
) -> list[str]:
    """Required fields still empty for an enabled supervisor."""
    if merged["format"] == "jev":
        # Empty address / model / key: the browser Jev connection fills them in.
        jev = browser_jev_connection(config)
        return [] if own_key or _key_set(jev["api_key_env"], environment) else ["api_key"]
    missing = [field for field in ("api_base", "model") if not _text(merged[field])]
    if not own_key:
        missing.append("api_key")
    return missing


def build_team_rules_update(
    params: Mapping[str, Any],
    config: Mapping[str, Any] | None,
    environment: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Validate the ``team_rules_*`` keys in ``params``; return the section patch.

    Raises ``TeamRulesSettingsError`` before anything is written. An empty
    dict means the save does not touch the supervisor.
    """
    environment = os.environ if environment is None else environment
    touched = TEAM_RULES_PARAM_KEYS.intersection(params) or TEAM_RULES_API_KEY_PARAM in params
    if not touched:
        return {}
    updates = _parse_team_rules_params(params)
    typed_key = params.get(TEAM_RULES_API_KEY_PARAM)
    own_key = _text(typed_key) if typed_key is not None else _text(environment.get(TEAM_RULES_API_KEY_ENV))
    updates["api_key_env"] = TEAM_RULES_API_KEY_ENV if own_key else ""

    merged = {**_section(config), **updates}
    if not _enabled(merged["enabled"]):
        return updates
    missing = _missing_required(merged, config, own_key, environment)
    if missing:
        raise TeamRulesSettingsError(f"team_rules is enabled but missing: {', '.join(missing)}")
    return updates


def resolve_team_rules_spec(config: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """The ``TeamAgentSpec.team_rules`` dict, or ``None`` when the supervisor is off."""
    section = _section(config)
    if not _enabled(section["enabled"]):
        return None
    fmt = _text(section["format"]) or "openai"
    spec: dict[str, Any] = {
        "enabled": True,
        "mode": "active",
        "format": fmt,
        "api_base": _text(section["api_base"]),
        "model": _text(section["model"]),
        "api_key_env": _text(section["api_key_env"]) or TEAM_RULES_API_KEY_ENV,
    }
    if fmt != "jev":
        return spec
    jev = browser_jev_connection(config)
    if not _text(section["api_key_env"]):
        spec["api_key_env"] = jev["api_key_env"]
    if not spec["api_base"] or spec["api_base"].rstrip("/") == jev["api_base"]:
        # Same service as the browser: reuse its address and endpoint path.
        spec["api_base"] = jev["api_base"]
        spec["endpoint"] = jev["endpoint"]
    if not spec["model"]:
        spec["model"] = jev["model"]
    return spec

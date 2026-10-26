# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Lightweight browser settings projection shared by Front and Runtime."""

import os
from collections.abc import Mapping
from typing import Any, Callable
from urllib.parse import urlsplit

BROWSER_DECISION_MODES = ("llm", "shadow", "hybrid")
JEV_PROVIDER_DEFAULTS = {
    "openrouter": {
        "model": "typesafe/jev-1.13",
        "api_base": "https://openrouter.ai/api/alpha",
        "api_key_env": "OPENROUTER_API_KEY",
    },
    "typesafe": {
        "model": "jev-1.13.0",
        "api_base": "https://api.typesafe.ai/v1",
        "api_key_env": "TYPESAFE_API_KEY",
    },
    "custom": {
        "model": "jev-1.13.0",
        "api_base": "",
        "api_key_env": "JEV_CUSTOM_API_KEY",
    },
}


def resolve_jev_connection(
    config: dict[str, Any], *, resolve_value: Callable[[Any], Any] | None = None,
) -> dict[str, str]:
    """Read the shared Jev connection; browser and collaboration modes stay independent."""
    browser = config.get("browser") or {}
    if not isinstance(browser, dict):
        raise ValueError("browser must be a mapping")
    section = browser.get("decision") or {}
    if not isinstance(section, dict):
        raise ValueError("browser.decision must be a mapping")
    if resolve_value is not None:
        section = resolve_value(section)
    provider = section.get("provider", "typesafe")
    if not isinstance(provider, str) or provider not in JEV_PROVIDER_DEFAULTS:
        raise ValueError("Jev provider must be openrouter, typesafe or custom")
    settings = {**JEV_PROVIDER_DEFAULTS.get(provider, {}), **section}
    for name in ("api_base", "model", "api_key_env"):
        value = settings.get(name)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"Jev {name} is required in Models > Jev settings")
        settings[name] = value.strip()
    api_base = settings.get("api_base", "")
    try:
        url = urlsplit(api_base)
        valid_url = url.scheme == "https" and bool(url.hostname)
        valid_url = valid_url and not any((url.username, url.password, url.query, url.fragment))
    except ValueError:
        valid_url = False
    if not valid_url:
        raise ValueError("Jev API base must be an HTTPS URL without credentials, query or fragment")
    secret = os.environ.get(settings.get("api_key_env", ""), "").strip()
    if not secret:
        raise ValueError("Jev API key is required in Models > Jev settings")
    return {
        "api_base": api_base.rstrip("/"),
        "model_name": settings.get("model", ""),
        "api_key": secret,
        "endpoint_path": "/decisions" if provider == "openrouter" else "/systemone",
    }


def project_browser_decision(
    section: dict[str, Any], environment: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Project resolved settings without importing the SDK or exposing credentials."""
    provider = section.get("provider", "typesafe")
    if not isinstance(provider, str) or provider not in JEV_PROVIDER_DEFAULTS:
        provider = "typesafe"
    defaults = JEV_PROVIDER_DEFAULTS[provider]
    mode = section.get("mode", "llm")
    key_env = section.get("api_key_env") or defaults["api_key_env"]
    environment = os.environ if environment is None else environment
    return {
        "decision_mode": mode if mode in BROWSER_DECISION_MODES else "llm",
        "decision_provider": provider,
        "decision_model": section.get("model") or defaults["model"],
        "decision_api_base": section.get("api_base") or defaults["api_base"],
        "jev_key_configured": isinstance(key_env, str) and bool(environment.get(key_env, "").strip()),
    }

# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Lightweight browser settings projection shared by Front and Runtime."""

import os
from collections.abc import Mapping
from typing import Any

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

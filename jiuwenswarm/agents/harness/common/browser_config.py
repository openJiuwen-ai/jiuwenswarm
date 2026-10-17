# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Saved browser settings shared by the main agent and Swarm providers."""

from __future__ import annotations

import logging
import os
import sys
from dataclasses import replace
from typing import Any

from jiuwenswarm.common.config import resolve_env_vars

logger = logging.getLogger(__name__)

BROWSER_DECISION_MODES = ("llm", "shadow", "hybrid")
# Mirrors openjiuwen's BrowserDecisionConfig provider defaults. They are written out in
# full on a provider switch: dropping them would let the template migration restore
# OpenRouter's model and endpoint under a TypeSafe provider.
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
}
# openjiuwen's default when browser.decision names no provider.
_DEFAULT_JEV_PROVIDER = "typesafe"


def _decision_section(config: dict[str, Any] | None) -> dict[str, Any]:
    browser = config.get("browser", {}) if isinstance(config, dict) else {}
    raw = browser.get("decision") if isinstance(browser, dict) else None
    return resolve_env_vars(raw) if isinstance(raw, dict) else {}


def browser_decision_mode(config: dict[str, Any] | None) -> str:
    """The saved browser decision mode; anything unrecognised reads as llm."""
    mode = _decision_section(config).get("mode", "llm")
    return mode if mode in BROWSER_DECISION_MODES else "llm"


def browser_decision_provider(config: dict[str, Any] | None) -> str:
    provider = _decision_section(config).get("provider", _DEFAULT_JEV_PROVIDER)
    return provider if provider in JEV_PROVIDER_DEFAULTS else _DEFAULT_JEV_PROVIDER


def browser_decision_state(config: dict[str, Any] | None) -> dict[str, Any]:
    """What the Browser settings page shows; the key itself is never returned."""
    section = _decision_section(config)
    provider = browser_decision_provider(config)
    key_env = section.get("api_key_env") or JEV_PROVIDER_DEFAULTS[provider]["api_key_env"]
    return {
        "decision_mode": browser_decision_mode(config),
        "decision_provider": provider,
        "jev_key_configured": bool(str(os.environ.get(key_env, "")).strip()),
    }


def browser_decision_updates(
    mode: Any, provider: Any, config: dict[str, Any] | None
) -> tuple[dict[str, Any], str | None]:
    """The browser.decision fields to write, or an error when the result could not run.

    ``None`` leaves that setting unchanged. A Jev mode is refused when the installed
    openjiuwen has no Jev support or the provider's key variable is unset; switching
    to llm always succeeds.
    """
    if mode is not None and mode not in BROWSER_DECISION_MODES:
        return {}, "decision_mode must be llm, shadow or hybrid"
    if provider is not None and provider not in JEV_PROVIDER_DEFAULTS:
        return {}, "decision_provider must be openrouter or typesafe"
    # The page sends its whole state on every save; only real changes are checked, so an
    # unrelated save (e.g. the Chrome path) still works while a Jev key is missing.
    section = _decision_section(config)
    updates: dict[str, Any] = {}
    # Compare with the saved value so selecting llm can also repair an invalid mode.
    if mode is not None and mode != section.get("mode", "llm"):
        updates["mode"] = mode
    if provider is not None and provider != browser_decision_provider(config):
        updates.update({"provider": provider, **JEV_PROVIDER_DEFAULTS[provider]})
    section = {**section, **updates}
    if not updates or section.get("mode", "llm") == "llm":
        return updates, None
    try:
        from openjiuwen.harness.tools.browser_move.decision import BrowserDecisionConfig
    except ModuleNotFoundError:
        return {}, "installed openjiuwen has no Jev support"
    try:
        decision = BrowserDecisionConfig(**section)
    except (TypeError, ValueError) as exc:
        return {}, f"browser.decision is invalid: {exc}"
    if not os.environ.get(decision.api_key_env, "").strip():
        return {}, f"Jev API key is not configured ({decision.api_key_env})"
    return updates, None


def apply_browser_decision_config(spec: Any, config: dict[str, Any] | None) -> Any:
    """Apply optional policy without rebuilding runtime/security settings.

    Unknown saved modes follow the UI's llm fallback. Explicit Jev modes still
    require valid SDK configuration instead of silently disabling the policy.
    """
    browser = config.get("browser", {}) if isinstance(config, dict) else {}
    raw = browser.get("decision", {}) if isinstance(browser, dict) else {}
    if raw is None:
        return spec
    if not isinstance(raw, dict):
        raise ValueError("browser.decision must be a mapping")
    if not raw:
        return spec
    resolved = resolve_env_vars(raw)
    if browser_decision_mode(config) == "llm":
        if resolved.get("mode", "llm") != "llm":
            logger.warning("browser.decision.mode is unrecognized; using llm")
        kwargs = dict(spec.factory_kwargs or {})
        settings = kwargs.get("settings")
        previous = getattr(settings, "decision", None)
        if previous is not None and previous.mode != "llm":
            kwargs["settings"] = replace(
                settings, decision=replace(previous, mode="llm")
            )
            spec.factory_kwargs = kwargs
        return spec
    try:
        from openjiuwen.harness.tools.browser_move.decision import BrowserDecisionConfig
    except ModuleNotFoundError:
        # The installed openjiuwen predates Jev: keep the LLM-only browser agent.
        logger.warning(
            "browser.decision.mode=%s ignored: installed openjiuwen has no Jev support; using llm",
            resolved.get("mode"),
        )
        return spec

    try:
        decision = BrowserDecisionConfig(**resolved)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"browser.decision is invalid: {exc}") from exc
    kwargs = dict(spec.factory_kwargs or {})
    settings = kwargs.get("settings")
    if settings is None:
        raise ValueError(
            "browser decision policy requires the existing runtime settings"
        )
    kwargs["settings"] = replace(settings, decision=decision)
    spec.factory_kwargs = kwargs
    return spec


def resolve_chrome_path(config: dict[str, Any] | None) -> str:
    """Resolve a string or platform-specific Chrome path from saved config."""
    if not isinstance(config, dict):
        return ""
    browser = config.get("browser", {})
    if not isinstance(browser, dict):
        return ""
    chrome_path = resolve_env_vars(browser).get("chrome_path", "")
    if isinstance(chrome_path, str):
        return chrome_path.strip()
    if not isinstance(chrome_path, dict):
        return ""
    platform_key = {
        "win32": "windows",
        "cygwin": "windows",
        "darwin": "macos",
        "linux": "linux",
        "linux2": "linux",
    }.get(sys.platform, "default")
    for key in (platform_key, "default"):
        value = chrome_path.get(key, "")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""

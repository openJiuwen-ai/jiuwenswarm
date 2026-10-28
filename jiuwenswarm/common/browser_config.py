# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Saved browser settings shared by the main agent and Swarm providers."""

from __future__ import annotations

import logging
import os
import sys
from dataclasses import replace
from typing import Any
from urllib.parse import urlsplit

from jiuwenswarm.common.browser_settings import (
    BROWSER_DECISION_MODES,
    JEV_PROVIDER_DEFAULTS,
    project_browser_decision,
)
from jiuwenswarm.common.config import resolve_env_vars

logger = logging.getLogger(__name__)


def _decision_section(config: dict[str, Any] | None) -> dict[str, Any]:
    browser = config.get("browser", {}) if isinstance(config, dict) else {}
    raw = browser.get("decision") if isinstance(browser, dict) else None
    return resolve_env_vars(raw) if isinstance(raw, dict) else {}


def browser_decision_mode(config: dict[str, Any] | None) -> str:
    """The saved browser decision mode; anything unrecognised reads as llm."""
    mode = _decision_section(config).get("mode", "llm")
    return mode if mode in BROWSER_DECISION_MODES else "llm"


def browser_decision_provider(config: dict[str, Any] | None) -> str:
    return browser_decision_state(config)["decision_provider"]


def browser_decision_state(config: dict[str, Any] | None) -> dict[str, Any]:
    """What the Browser settings page shows; the key itself is never returned."""
    return project_browser_decision(_decision_section(config))


def _sdk_decision_section(section: dict[str, Any]) -> dict[str, Any]:
    # Custom endpoints use the TypeSafe Jev wire format, not chat completions.
    if section.get("provider") != "custom":
        return section
    resolved = {**JEV_PROVIDER_DEFAULTS["custom"], **section, "provider": "typesafe"}
    if not str(resolved.get("api_base") or "").strip():
        raise ValueError("custom Jev provider requires an API base URL")
    return resolved


def browser_decision_updates(
    mode: Any, provider: Any, config: dict[str, Any] | None,
    *, model: Any = None, api_base: Any = None,
) -> tuple[dict[str, Any], str | None]:
    """The browser.decision fields to write, or an error when the result could not run.

    ``None`` leaves that setting unchanged. A Jev mode is refused when the installed
    openjiuwen has no Jev support or the provider's key variable is unset; switching
    to llm always succeeds.
    """
    if mode is not None and mode not in BROWSER_DECISION_MODES:
        return {}, "decision_mode must be llm, shadow or hybrid"
    if provider is not None and (
        not isinstance(provider, str) or provider not in JEV_PROVIDER_DEFAULTS
    ):
        return {}, "decision_provider must be openrouter, typesafe or custom"
    # The page sends its whole state on every save; only real changes are checked, so an
    # unrelated save (e.g. the Chrome path) still works while a Jev key is missing.
    section = _decision_section(config)
    updates: dict[str, Any] = {}
    # Compare with the saved value so selecting llm can also repair an invalid mode.
    if mode is not None and mode != section.get("mode", "llm"):
        updates["mode"] = mode
    if provider is not None and provider != browser_decision_provider(config):
        updates.update({"provider": provider, **JEV_PROVIDER_DEFAULTS[provider]})
    for name, value in (("model", model), ("api_base", api_base)):
        if value is None:
            continue
        if not isinstance(value, str) or not value.strip():
            return {}, f"decision_{name} must be a non-empty string"
        value = value.strip()
        if value != updates.get(name, section.get(name)):
            updates[name] = value
    section = {**section, **updates}
    if not updates:
        return updates, None
    if updates == {"mode": "llm"}:
        return updates, None
    try:
        resolved = _sdk_decision_section(section)
        if api_base is not None or section.get("provider") == "custom":
            url = urlsplit(str(section.get("api_base") or ""))
            if url.scheme != "https" or not url.hostname:
                return {}, "decision_api_base must be an HTTPS base URL"
            if any((url.username, url.password, url.query, url.fragment)):
                return {}, "decision_api_base must not contain credentials, query or fragment"
    except ValueError as exc:
        return {}, f"browser.decision is invalid: {exc}"
    if section.get("mode", "llm") == "llm":
        return updates, None
    try:
        from openjiuwen.harness.tools.browser_move.decision import BrowserDecisionConfig
    except ModuleNotFoundError:
        return {}, "installed openjiuwen has no Jev support"
    try:
        decision = BrowserDecisionConfig(**resolved)
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
        decision = BrowserDecisionConfig(**_sdk_decision_section(resolved))
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

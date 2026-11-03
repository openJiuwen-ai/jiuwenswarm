# Gateway 侧副本，源文件 jiuwenswarm/common/browser_config.py；改动请同步两侧。
# 本副本仅保留 gateway 实际引用的符号；同步母仓时请勿整体覆盖。
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Saved browser settings shared by the main agent and Swarm providers."""

from __future__ import annotations

import logging
import os
from typing import Any
from urllib.parse import urlsplit

from gateway.common.browser_settings import (
    BROWSER_DECISION_MODES,
    JEV_PROVIDER_DEFAULTS,
    project_browser_decision,
)
from gateway.common.config import resolve_env_vars

logger = logging.getLogger(__name__)


def _decision_section(config: dict[str, Any] | None) -> dict[str, Any]:
    browser = config.get("browser", {}) if isinstance(config, dict) else {}
    raw = browser.get("decision") if isinstance(browser, dict) else None
    return resolve_env_vars(raw) if isinstance(raw, dict) else {}


def browser_decision_provider(config: dict[str, Any] | None) -> str:
    return browser_decision_state(config)["decision_provider"]


def browser_decision_state(config: dict[str, Any] | None) -> dict[str, Any]:
    """What the Browser settings page shows; the key itself is never returned."""
    return project_browser_decision(_decision_section(config))


def _sdk_decision_section(section: dict[str, Any]) -> dict[str, Any]:
    # Display metadata belongs to the application, not the SDK configuration.
    section = {key: value for key, value in section.items() if key != "display_name"}
    # Custom endpoints use the TypeSafe Jev wire format, not chat completions.
    if section.get("provider") != "custom":
        return section
    resolved = {**JEV_PROVIDER_DEFAULTS["custom"], **section, "provider": "typesafe"}
    if not str(resolved.get("api_base") or "").strip():
        raise ValueError("custom Jev provider requires an API base URL")
    return resolved


def browser_decision_updates(
    mode: Any, provider: Any, config: dict[str, Any] | None,
    *, model: Any = None, api_base: Any = None, display_name: Any = None,
) -> tuple[dict[str, Any], str | None]:
    """The browser.decision fields to write, or an error when the result could not run.

    ``None`` leaves that setting unchanged.
    A Jev mode is refused when the installed
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
    if display_name is not None:
        if not isinstance(display_name, str):
            return {}, "decision_display_name must be a non-empty string"
        display_name = display_name.strip()
        # Clearing metadata accompanies disabling Jev, never editing its connection.
        has_connection_edit = model is not None or api_base is not None or "provider" in updates
        if not display_name and (mode != "llm" or has_connection_edit):
            return {}, "decision_display_name must be a non-empty string"
        if len(display_name) > 100:
            return {}, "decision_display_name must not exceed 100 characters"
        if display_name != section.get("display_name"):
            updates["display_name"] = display_name
    section = {**section, **updates}
    if not updates:
        return updates, None
    if section.get("mode", "llm") == "llm" and not {"provider", "model", "api_base"}.intersection(updates):
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

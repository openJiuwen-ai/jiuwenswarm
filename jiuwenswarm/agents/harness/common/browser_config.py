# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Saved browser settings shared by the main agent and Swarm providers."""

from __future__ import annotations

import logging
import sys
from dataclasses import replace
from typing import Any

from jiuwenswarm.common.config import resolve_env_vars

logger = logging.getLogger(__name__)


def apply_browser_decision_config(spec: Any, config: dict[str, Any] | None) -> Any:
    """Apply the same optional policy to deep/code/swarm without rebuilding settings."""
    browser = config.get("browser", {}) if isinstance(config, dict) else {}
    raw = browser.get("decision", {}) if isinstance(browser, dict) else {}
    if raw is None:
        return spec
    if not isinstance(raw, dict):
        raise ValueError("browser.decision must be a mapping")
    if not raw:
        return spec
    resolved = resolve_env_vars(raw)
    if resolved.get("mode", "llm") == "llm":
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

    decision = BrowserDecisionConfig(**resolved)
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

# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Publish browser launch settings into ``os.environ`` for agent-core.

agent-core's browser runtime reads ``BROWSER_DRIVER`` and related keys with
``os.getenv``. Track B stores those keys in the tip and strips them from the
process environment, so a configured ``managed`` driver is invisible and the
runtime falls back to remote CDP (often an empty ``:9222``). This module is
the single place that copies the tip (and saved browser config) back into
``os.environ`` before a browser runtime is built.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from jiuwenswarm.common.local_env_config import read_env, read_env_if_set

logger = logging.getLogger(__name__)

_VALID_DRIVERS = frozenset({"remote", "managed", "extension"})
_TIP_COPY_KEYS = (
    "BROWSER_PROFILE_NAME",
    "BROWSER_TIMEOUT_S",
    "BROWSER_MANAGED_HOST",
    "BROWSER_MANAGED_PORT",
    "BROWSER_ALLOW_SHORT_TIMEOUT_OVERRIDE",
)
_STALE_REMOTE_CDP_KEYS = (
    "PLAYWRIGHT_CDP_URL",
    "PLAYWRIGHT_MCP_CDP_ENDPOINT",
)


def resolve_managed_browser_binary(config_base: dict[str, Any] | None = None) -> str:
    """Resolve managed-browser binary from saved browser config."""
    if config_base is None:
        from jiuwenswarm.common.config import get_config

        config_base = get_config()
    if not isinstance(config_base, dict):
        return ""
    from jiuwenswarm.common.config import resolve_env_vars

    config = resolve_env_vars(config_base)
    browser_cfg = config.get("browser", {}) if isinstance(config, dict) else {}
    if not isinstance(browser_cfg, dict):
        return ""
    chrome_path = browser_cfg.get("chrome_path", "")
    if isinstance(chrome_path, str):
        return chrome_path.strip()
    if not isinstance(chrome_path, dict):
        return ""
    platform_map = {
        "win32": "windows",
        "cygwin": "windows",
        "darwin": "macos",
        "linux": "linux",
        "linux2": "linux",
    }
    os_key = platform_map.get(os.sys.platform, "default")
    for key in (os_key, "default"):
        value = chrome_path.get(key, "")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def resolve_headless(config_base: dict[str, Any] | None = None) -> bool:
    """Read ``browser.headless`` from config (default True = headless)."""
    try:
        if config_base is None:
            from jiuwenswarm.common.config import get_config

            config_base = get_config()
        if not isinstance(config_base, dict):
            return True
        browser_cfg = config_base.get("browser", {})
        if not isinstance(browser_cfg, dict):
            return True
        headless = browser_cfg.get("headless", True)
        return bool(headless) if isinstance(headless, bool) else True
    except Exception:
        return True


def materialize_browser_driver_env() -> str:
    """Copy Track B browser driver settings into ``os.environ``.

    Tip wins over a leftover process value. When neither is set, default to
    ``managed`` so the runtime launches Chrome instead of attaching to a
    caller-owned CDP port. Managed mode drops stale remote CDP endpoints that
    would otherwise pin Playwright to an unowned ``:9222``. Copied tip keys are
    cleared when this namespace has no value, so a previous session cannot
    leave them in the process environment.
    """
    tip_driver = (read_env_if_set("BROWSER_DRIVER") or "").strip().lower()
    env_driver = (os.getenv("BROWSER_DRIVER") or "").strip().lower()
    driver = tip_driver or env_driver or "managed"
    if driver not in _VALID_DRIVERS:
        logger.warning("invalid BROWSER_DRIVER %r; defaulting to managed", driver)
        driver = "managed"
    os.environ["BROWSER_DRIVER"] = driver

    for key in _TIP_COPY_KEYS:
        value = read_env_if_set(key)
        text = (value or "").strip() if isinstance(value, str) else ""
        if text:
            os.environ[key] = text
        else:
            os.environ.pop(key, None)

    tip_binary = read_env_if_set("BROWSER_MANAGED_BINARY")
    binary = (tip_binary or "").strip() if isinstance(tip_binary, str) else ""
    if binary and not (os.getenv("BROWSER_MANAGED_BINARY") or "").strip():
        os.environ["BROWSER_MANAGED_BINARY"] = binary

    if driver == "managed":
        for key in _STALE_REMOTE_CDP_KEYS:
            os.environ.pop(key, None)
    return driver


def sync_browser_runtime_environment(
    config_base: dict[str, Any] | None = None,
) -> str:
    """Synchronize headless, Chrome path, and driver mode before browser start."""
    headless = resolve_headless(config_base)
    mcp_args_raw = (read_env("PLAYWRIGHT_MCP_ARGS") or "-y @playwright/mcp@latest").strip()
    mcp_args = mcp_args_raw.split() if mcp_args_raw else ["-y", "@playwright/mcp@latest"]
    mcp_args = [arg for arg in mcp_args if arg != "--headless"]
    if headless:
        mcp_args.append("--headless")
        os.environ["BROWSER_MANAGED_ARGS"] = "--headless=new"
    else:
        os.environ.pop("BROWSER_MANAGED_ARGS", None)
    os.environ["PLAYWRIGHT_MCP_ARGS"] = " ".join(mcp_args)

    chrome_path = resolve_managed_browser_binary(config_base)
    if chrome_path:
        os.environ["BROWSER_MANAGED_BINARY"] = chrome_path
    else:
        os.environ.pop("BROWSER_MANAGED_BINARY", None)

    driver = materialize_browser_driver_env()
    logger.info(
        "browser runtime config: headless=%s, chrome_path=%s, driver=%s",
        headless,
        chrome_path or "<auto>",
        driver,
    )
    return driver


__all__ = [
    "materialize_browser_driver_env",
    "resolve_headless",
    "resolve_managed_browser_binary",
    "sync_browser_runtime_environment",
]

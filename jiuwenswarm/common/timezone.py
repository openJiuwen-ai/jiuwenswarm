# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Single read-point for the default timezone (WorkSwarm).

When no explicit timezone is given, time/date data presented to (or accepted
from) the LLM uses this zone — including the message envelope timestamp that
tells the agent where its user is.

Resolution chain (first match wins):
    config.yaml top-level ``timezone:`` (primary source of truth) >
    env ``JIUWENSWARM_TIMEZONE`` (fallback when config is left blank) >
    process-local timezone (the zone the running instance/OS is in) >
    ``Asia/Shanghai`` (last resort when the local zone is unavailable)

Lazy + memoized: ``get_default_timezone.cache_clear()`` lets tests/settings
swap the source and re-resolve. Invalid IANA names fall back to the default
with a warning instead of raising at import time.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime
from functools import lru_cache
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

logger = logging.getLogger(__name__)

ENV_TIMEZONE = "JIUWENSWARM_TIMEZONE"
FALLBACK_TIMEZONE = "Asia/Shanghai"


@lru_cache(maxsize=1)
def get_default_timezone() -> ZoneInfo:
    """Return the default timezone for time/date data crossing the harness boundary."""
    name = _resolve_timezone_name()
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        logger.warning(
            "Invalid timezone %r; falling back to %s",
            name,
            FALLBACK_TIMEZONE,
        )
        return ZoneInfo(FALLBACK_TIMEZONE)


def _resolve_timezone_name() -> str:
    name = _config_timezone()
    if name:
        return name
    name = os.environ.get(ENV_TIMEZONE, "").strip()
    if name:
        return name
    name = _local_timezone_name()
    if name:
        return name
    return FALLBACK_TIMEZONE


def _local_timezone_name() -> str:
    """IANA key of the process-local (OS) timezone, best effort.

    Returns ``""`` when the local zone cannot be expressed as an IANA key —
    the caller then falls back to ``FALLBACK_TIMEZONE``.

    POSIX systems provide ``tz.key`` directly (e.g. ``Europe/Berlin``). Windows
    exposes a fixed-offset ``datetime.timezone`` with no key; translate it to
    the equivalent fixed ``Etc/GMT±N`` zone, which round-trips through
    ``ZoneInfo`` (note the IANA sign inversion: ``Etc/GMT+4`` is UTC-04:00).
    Partial-hour offsets (e.g. ``+05:30``) have no ``Etc/GMT`` zone and return
    ``""``.
    """
    try:
        tz = datetime.now().astimezone().tzinfo
    except Exception:  # pragma: no cover - extremely unusual hosts
        return ""
    if tz is None:
        return ""
    key = getattr(tz, "key", None)
    if key:
        return str(key)
    try:
        offset = tz.utcoffset(None)
    except Exception:  # pragma: no cover - defensive
        return ""
    if offset is None:
        return ""
    total = int(offset.total_seconds())
    if total % 3600:
        return ""
    if total == 0:
        return "UTC"
    hours = total // 3600
    sign = "+" if hours <= 0 else "-"
    return f"Etc/GMT{sign}{abs(hours)}"


def _config_timezone() -> str:
    """Read top-level ``timezone:`` from config.yaml, if present."""
    try:
        from jiuwenswarm.common.config import get_config
    except ImportError:  # pragma: no cover - config module always present
        return ""
    try:
        config = get_config()
    except Exception:  # pragma: no cover - a broken config must not kill startup
        return ""
    raw = config.get("timezone") if isinstance(config, dict) else None
    return str(raw).strip() if raw else ""


__all__ = ["ENV_TIMEZONE", "FALLBACK_TIMEZONE", "get_default_timezone"]
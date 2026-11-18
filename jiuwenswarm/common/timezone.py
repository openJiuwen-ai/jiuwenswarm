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

    Always returns a *real* IANA key (e.g. ``America/New_York``,
    ``Asia/Kolkata``) so DST transitions and half-hour offsets behave
    correctly — a UTC offset sampled "now" is never converted into a fixed
    ``Etc/GMT`` zone, because that would freeze the offset: a daily 09:00 job
    created in New York in winter would fire at 10:00 local time in summer.

    Returns ``""`` when no IANA key can be determined; the caller then falls
    back to ``FALLBACK_TIMEZONE``.

    Detection order:
      1. ``tzlocal`` (handles Windows via the CLDR Windows-zone mapping, macOS,
         and Linux), when importable — it is already present transitively.
      2. POSIX hints: ``TZ`` env var, ``/etc/timezone``, ``/etc/localtime``
         symlink target.
    """
    for candidate in (_tzlocal_name, _posix_timezone_name):
        try:
            name = candidate()
        except Exception:  # pragma: no cover - defensive; never kill startup
            continue
        if _is_valid_iana(name):
            return name
    return ""


def _is_valid_iana(name: str) -> bool:
    if not name or name.upper() in {"LOCAL", "UTC0"}:
        return False
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return False
    return True


def _tzlocal_name() -> str:
    try:
        import tzlocal
    except ImportError:
        return ""
    return str(tzlocal.get_localzone_name() or "")


def _posix_timezone_name() -> str:
    env_tz = os.environ.get("TZ", "").strip().lstrip(":")
    if env_tz:
        return env_tz
    try:
        with open("/etc/timezone", encoding="utf-8") as fh:
            name = fh.read().strip()
        if name:
            return name
    except OSError:
        pass
    try:
        target = os.path.realpath("/etc/localtime")
    except OSError:
        return ""
    marker = "zoneinfo/"
    idx = target.rfind(marker)
    return target[idx + len(marker):] if idx != -1 else ""


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
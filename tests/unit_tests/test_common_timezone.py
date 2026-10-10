"""Default-timezone resolution tests (fix plan v2, §6 fixtures).

The single read-point helper must resolve config > env > process-local >
Asia/Shanghai, using the machine's own timezone when nothing is configured,
and fall back with a warning on invalid IANA names. ``cache_clear()`` is
required between cases because the helper is memoized for the process lifetime.
"""

from datetime import datetime, timezone as dt_timezone
from zoneinfo import ZoneInfo

import jiuwenswarm.common.timezone as tz_mod
from jiuwenswarm.common.timezone import (
    ENV_TIMEZONE,
    FALLBACK_TIMEZONE,
    get_default_timezone,
)


def test_default_timezone_config_beats_env(monkeypatch):
    """The config key is the primary source of truth; env only fills blank config."""
    monkeypatch.setattr(tz_mod, "_config_timezone", lambda: "Europe/Berlin")
    monkeypatch.setenv(ENV_TIMEZONE, "America/New_York")
    get_default_timezone.cache_clear()
    try:
        assert get_default_timezone().key == "Europe/Berlin"
    finally:
        get_default_timezone.cache_clear()


def test_default_timezone_env_override(monkeypatch):
    monkeypatch.setattr(tz_mod, "_config_timezone", lambda: "")
    monkeypatch.setenv(ENV_TIMEZONE, "America/New_York")
    get_default_timezone.cache_clear()
    try:
        assert get_default_timezone() == ZoneInfo("America/New_York")
        assert get_default_timezone().key == "America/New_York"
    finally:
        get_default_timezone.cache_clear()


def test_default_timezone_unset_uses_process_local_zone(monkeypatch):
    """With no config and no env, the process-local (OS) timezone applies."""
    monkeypatch.setattr(tz_mod, "_config_timezone", lambda: "")
    monkeypatch.delenv(ENV_TIMEZONE, raising=False)
    monkeypatch.setattr(tz_mod, "_local_timezone_name", lambda: "Europe/Berlin")
    get_default_timezone.cache_clear()
    try:
        assert get_default_timezone().key == "Europe/Berlin"
    finally:
        get_default_timezone.cache_clear()


def test_default_timezone_local_unavailable_falls_back_to_asia_shanghai(monkeypatch):
    """Asia/Shanghai is the last resort when the local zone is unavailable."""
    monkeypatch.setattr(tz_mod, "_config_timezone", lambda: "")
    monkeypatch.delenv(ENV_TIMEZONE, raising=False)
    monkeypatch.setattr(tz_mod, "_local_timezone_name", lambda: "")
    get_default_timezone.cache_clear()
    try:
        assert get_default_timezone().key == FALLBACK_TIMEZONE
    finally:
        get_default_timezone.cache_clear()


def test_default_timezone_invalid_iana_falls_back(monkeypatch):
    monkeypatch.setattr(tz_mod, "_config_timezone", lambda: "")
    monkeypatch.setenv(ENV_TIMEZONE, "Not/AZone")
    get_default_timezone.cache_clear()
    try:
        assert get_default_timezone().key == FALLBACK_TIMEZONE
    finally:
        get_default_timezone.cache_clear()


def test_heartbeat_preview_renders_in_default_timezone(monkeypatch):
    """Lazy site: heartbeat preview falls back to the configured zone."""
    from jiuwenswarm.agents.harness.code.rails.heartbeat.scheduler import (
        HeartbeatSchedulerService,
    )

    monkeypatch.setattr(tz_mod, "_config_timezone", lambda: "")
    monkeypatch.setenv(ENV_TIMEZONE, "America/New_York")
    get_default_timezone.cache_clear()
    try:
        ts = datetime(2026, 9, 29, 12, 0, 0, tzinfo=dt_timezone.utc).timestamp()
        preview = HeartbeatSchedulerService._format_preview(ts)
        assert preview["iso"] == "2026-09-29T08:00:00-04:00"
    finally:
        get_default_timezone.cache_clear()
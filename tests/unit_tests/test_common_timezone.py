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

# ---------------------------------------------------------------------------
# Local-zone detection must yield real IANA keys (DST + half-hour zones)
# ---------------------------------------------------------------------------


def _local_name_from(monkeypatch, tzlocal_name="", posix_name=""):
    monkeypatch.setattr(tz_mod, "_tzlocal_name", lambda: tzlocal_name)
    monkeypatch.setattr(tz_mod, "_posix_timezone_name", lambda: posix_name)
    return tz_mod._local_timezone_name()


def test_local_zone_keeps_dst_aware_iana_key(monkeypatch):
    assert _local_name_from(monkeypatch, "America/New_York") == "America/New_York"
    assert _local_name_from(monkeypatch, "Europe/Berlin") == "Europe/Berlin"


def test_local_zone_half_hour_zone_is_not_shanghai(monkeypatch):
    """UTC+05:30 must resolve to Asia/Kolkata, not the Shanghai fallback."""
    assert _local_name_from(monkeypatch, "Asia/Kolkata") == "Asia/Kolkata"
    assert _local_name_from(monkeypatch, "Australia/Adelaide") == "Australia/Adelaide"


def test_local_zone_never_degrades_to_fixed_etc_gmt(monkeypatch):
    name = _local_name_from(monkeypatch, "America/New_York")
    assert not name.startswith("Etc/GMT")


def test_local_zone_falls_back_to_posix_hint(monkeypatch):
    assert _local_name_from(monkeypatch, "", "Asia/Kolkata") == "Asia/Kolkata"


def test_local_zone_rejects_invalid_and_placeholder_names(monkeypatch):
    assert _local_name_from(monkeypatch, "local", "Not/AZone") == ""
    assert _local_name_from(monkeypatch, "", "") == ""


def test_local_zone_survives_detector_exceptions(monkeypatch):
    def boom():
        raise RuntimeError("no tz database")

    monkeypatch.setattr(tz_mod, "_tzlocal_name", boom)
    monkeypatch.setattr(tz_mod, "_posix_timezone_name", lambda: "Europe/Berlin")
    assert tz_mod._local_timezone_name() == "Europe/Berlin"


def test_posix_name_reads_tz_env_var(monkeypatch):
    monkeypatch.setenv("TZ", ":Asia/Kolkata")
    assert tz_mod._posix_timezone_name() == "Asia/Kolkata"


def test_default_timezone_half_hour_local_zone_resolves_kolkata(monkeypatch):
    monkeypatch.setattr(tz_mod, "_config_timezone", lambda: "")
    monkeypatch.delenv(ENV_TIMEZONE, raising=False)
    monkeypatch.setattr(tz_mod, "_tzlocal_name", lambda: "Asia/Kolkata")
    get_default_timezone.cache_clear()
    try:
        assert get_default_timezone().key == "Asia/Kolkata"
    finally:
        get_default_timezone.cache_clear()


def test_daily_nine_am_job_stays_nine_am_across_dst(monkeypatch):
    """A 09:00 daily job created in a New York winter still fires at 09:00 local in summer."""
    from jiuwenswarm.runtime.cron.cron_expr import next_cron_datetime

    monkeypatch.setattr(tz_mod, "_config_timezone", lambda: "")
    monkeypatch.delenv(ENV_TIMEZONE, raising=False)
    monkeypatch.setattr(tz_mod, "_tzlocal_name", lambda: "America/New_York")
    get_default_timezone.cache_clear()
    try:
        tz = ZoneInfo(get_default_timezone().key)
        winter = datetime(2026, 1, 15, 10, 0, tzinfo=tz)
        summer = datetime(2026, 7, 15, 10, 0, tzinfo=tz)
        assert next_cron_datetime("0 9 * * *", winter).hour == 9
        nxt = next_cron_datetime("0 9 * * *", summer)
        assert (nxt.hour, nxt.minute) == (9, 0)
        assert nxt.utcoffset().total_seconds() == -4 * 3600  # EDT, not EST
    finally:
        get_default_timezone.cache_clear()


def test_half_hour_zone_cron_fires_on_local_wall_clock():
    from jiuwenswarm.runtime.cron.cron_expr import next_cron_datetime

    tz = ZoneInfo("Asia/Kolkata")
    nxt = next_cron_datetime("30 9 * * *", datetime(2026, 7, 15, 10, 0, tzinfo=tz))
    assert nxt.isoformat() == "2026-07-16T09:30:00+05:30"

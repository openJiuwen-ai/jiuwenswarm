"""Alarm tools must interpret zone-less wall-clock input in an explicit timezone.

``create_alarm`` / ``search_alarms`` / ``modify_alarm`` receive naive
``YYYYMMDD hhmmss`` strings (or hour/minute) and send absolute epoch
milliseconds to the device. The wall-clock -> epoch step used to be hardcoded to
Asia/Shanghai; it now takes the caller's IANA ``timezone`` (falling back to the
instance default) and rejects wall times that do not exist in that zone.
"""

from __future__ import annotations

from datetime import datetime, timezone as dt_timezone
from zoneinfo import ZoneInfo

import pytest

import jiuwenswarm.common.timezone as tz_mod
from jiuwenswarm.agents.harness.common.tools.xiaoyi_phone_tools import alarm_tools
from jiuwenswarm.agents.harness.common.tools.xiaoyi_phone_tools.utils import ToolInputError
from jiuwenswarm.common.timezone import get_default_timezone

_create_alarm = alarm_tools.create_alarm._func  # pylint: disable=protected-access
_search_alarms = alarm_tools.search_alarms._func  # pylint: disable=protected-access
_modify_alarm = alarm_tools.modify_alarm._func  # pylint: disable=protected-access


def _utc_ms(year, month, day, hour=0, minute=0, second=0) -> int:
    return int(datetime(year, month, day, hour, minute, second, tzinfo=dt_timezone.utc).timestamp() * 1000)


@pytest.fixture
def device(monkeypatch):
    """Capture the intent sent to the device instead of talking to a phone."""
    sent: list[dict] = []

    async def fake_execute(name, command):
        sent.append(command["payload"]["executeParam"]["intentParam"])
        return {"code": 0, "result": {"entityId": "e1", "alarmTime": sent[-1].get("alarmTime")}}

    monkeypatch.setattr(alarm_tools, "execute_device_command", fake_execute)
    return sent


@pytest.fixture
def default_tz_new_york(monkeypatch):
    monkeypatch.setattr(tz_mod, "_config_timezone", lambda: "")
    monkeypatch.setenv(tz_mod.ENV_TIMEZONE, "America/New_York")
    get_default_timezone.cache_clear()
    yield
    get_default_timezone.cache_clear()


# --- _resolve_alarm_timezone ------------------------------------------------


def test_explicit_timezone_wins_over_default(default_tz_new_york):
    assert alarm_tools._resolve_alarm_timezone("Asia/Kolkata") == ZoneInfo("Asia/Kolkata")


def test_missing_timezone_uses_instance_default(default_tz_new_york):
    assert alarm_tools._resolve_alarm_timezone(None).key == "America/New_York"
    assert alarm_tools._resolve_alarm_timezone("  ").key == "America/New_York"


def test_invalid_timezone_is_rejected():
    with pytest.raises(ToolInputError):
        alarm_tools._resolve_alarm_timezone("Not/AZone")


# --- wall clock -> epoch ----------------------------------------------------


def test_same_wall_clock_maps_to_different_instants_per_zone():
    shanghai = alarm_tools._parse_alarm_time_to_ms("20260715 090000", ZoneInfo("Asia/Shanghai"))
    new_york = alarm_tools._parse_alarm_time_to_ms("20260715 090000", ZoneInfo("America/New_York"))
    assert shanghai == _utc_ms(2026, 7, 15, 1, 0)  # UTC+8
    assert new_york == _utc_ms(2026, 7, 15, 13, 0)  # EDT, UTC-4


def test_half_hour_zone_offset_is_exact():
    kolkata = alarm_tools._parse_alarm_time_to_ms("20260715 090000", ZoneInfo("Asia/Kolkata"))
    assert kolkata == _utc_ms(2026, 7, 15, 3, 30)  # UTC+5:30


def test_dst_offset_applies_per_date():
    tz = ZoneInfo("America/New_York")
    winter = alarm_tools._parse_alarm_time_to_ms("20260115 090000", tz)
    summer = alarm_tools._parse_alarm_time_to_ms("20260715 090000", tz)
    assert winter == _utc_ms(2026, 1, 15, 14, 0)  # EST, UTC-5
    assert summer == _utc_ms(2026, 7, 15, 13, 0)  # EDT, UTC-4


def test_nonexistent_dst_gap_time_is_rejected_not_shifted():
    # 02:30 does not exist in New York on 2026-03-08 (clocks jump 02:00 -> 03:00).
    with pytest.raises(ToolInputError, match="不存在"):
        alarm_tools._parse_alarm_time_to_ms("20260308 023000", ZoneInfo("America/New_York"))


def test_malformed_alarm_time_still_returns_none():
    assert alarm_tools._parse_alarm_time_to_ms("not a time", ZoneInfo("UTC")) is None


def test_hour_minute_is_next_occurrence_in_given_zone():
    tz = ZoneInfo("Asia/Kolkata")
    ms = alarm_tools._alarm_time_ms_from_hour_minute(9, 30, tz)
    local = datetime.fromtimestamp(ms / 1000, tz)
    assert (local.hour, local.minute, local.second) == (9, 30, 0)
    assert ms > datetime.now(tz).timestamp() * 1000
    assert ms - datetime.now(tz).timestamp() * 1000 <= 24 * 3600 * 1000 + 1000


# --- tool entry points ------------------------------------------------------


@pytest.mark.asyncio
async def test_create_alarm_uses_explicit_timezone(device):
    result = await _create_alarm(alarm_time="20260715 090000", timezone="America/New_York")
    assert device[0]["alarmTime"] == _utc_ms(2026, 7, 15, 13, 0)
    assert "America/New_York" in str(result)
    assert "2026-07-15T09:00:00-04:00" in str(result)


@pytest.mark.asyncio
async def test_create_alarm_without_timezone_uses_instance_default(device, default_tz_new_york):
    await _create_alarm(alarm_time="20260715 090000")
    assert device[0]["alarmTime"] == _utc_ms(2026, 7, 15, 13, 0)


@pytest.mark.asyncio
async def test_create_alarm_rejects_invalid_timezone(device):
    with pytest.raises(ToolInputError):
        await _create_alarm(alarm_time="20260715 090000", timezone="Not/AZone")
    assert device == []


@pytest.mark.asyncio
async def test_create_alarm_rejects_dst_gap_time(device):
    with pytest.raises(ToolInputError, match="不存在"):
        await _create_alarm(alarm_time="20260308 023000", timezone="America/New_York")
    assert device == []


@pytest.mark.asyncio
async def test_search_alarms_interval_uses_timezone(device):
    await _search_alarms(
        start_time="20260715 000000", end_time="20260716 000000", timezone="Asia/Kolkata"
    )
    assert device[0]["timeInterval"] == [_utc_ms(2026, 7, 14, 18, 30), _utc_ms(2026, 7, 15, 18, 30)]


@pytest.mark.asyncio
async def test_modify_alarm_uses_timezone(device):
    await _modify_alarm(entity_id="e1", alarm_time="20260715 090000", timezone="Asia/Shanghai")
    assert device[0]["alarmTime"] == _utc_ms(2026, 7, 15, 1, 0)

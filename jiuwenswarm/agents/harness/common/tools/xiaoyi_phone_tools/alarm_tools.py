# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Alarm tools - 闹钟工具.

与 HarmonyOS 时钟 Intent 约定一致（intentParam 使用 camelCase 字段名）：
- create_alarm: entityName + alarmTime(毫秒) + 可选响铃/重复等
- search_alarms: rangeType / alarmState / daysOfWakeType / timeInterval
- modify_alarm: entityId + 与创建相同的可选字段
- delete_alarm: items[{ entityId }]

包含：
- create_alarm: 创建闹钟
- search_alarms: 搜索闹钟
- modify_alarm: 修改闹钟
- delete_alarm: 删除闹钟
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence, Union

from openjiuwen.core.foundation.tool import tool

from jiuwenswarm.common.utils import logger
from .utils import (
    execute_device_command,
    format_success_response,
    raise_if_device_error,
    ToolInputError,
)

# 设备侧允许的枚举取值（时钟 Intent）
_ALARM_SNOOZE_DURATION = frozenset({5, 10, 15, 20, 25, 30})
_ALARM_SNOOZE_TOTAL = frozenset({0, 1, 3, 5, 10})
_ALARM_RING_DURATION = frozenset({1, 5, 10, 15, 20, 30})
_DAYS_OF_WAKE_TYPE = frozenset({0, 1, 2, 3, 4})
_DAYS_OF_WEEK = ("Mon", "Tues", "Wed", "Thur", "Fri", "Sat", "Sun")
_ALARM_STATE = frozenset({0, 1})
_RANGE_TYPE = frozenset({"all", "next", "current"})


def _alarm_display_timezone():
    """闹钟挂钟时区：优先 Asia/Shanghai，不可用时为 UTC+8（均带 tzinfo）."""
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo("Asia/Shanghai")
    except Exception:
        return timezone(timedelta(hours=8))


def _is_valid_alarm_calendar_date(month: int, day: int) -> bool:
    """月、日是否在常见取值范围内（细粒度合法性由 datetime 构造校验）."""
    if month < 1 or month > 12:
        return False
    return 1 <= day <= 31


def _is_valid_alarm_clock_time(hour: int, minute: int, second: int) -> bool:
    """时、分、秒是否在 24 小时制取值范围内."""
    if hour < 0 or hour > 23:
        return False
    if minute < 0 or minute > 59:
        return False
    return 0 <= second <= 59


def _parse_alarm_time_to_ms(alarm_time: str) -> Optional[int]:
    """将 YYYYMMDD hhmmss 解析为毫秒时间戳（挂钟时区同 _alarm_display_timezone）."""
    trimmed = alarm_time.strip()
    if len(trimmed) < 13:
        return None
    date_part = trimmed[:8]
    time_part = trimmed[8:].strip()
    if len(date_part) != 8 or len(time_part) != 6:
        return None
    try:
        year = int(date_part[0:4])
        month = int(date_part[4:6])
        day = int(date_part[6:8])
        hour = int(time_part[0:2])
        minute = int(time_part[2:4])
        second = int(time_part[4:6])
    except ValueError:
        return None
    if not _is_valid_alarm_calendar_date(month, day):
        return None
    if not _is_valid_alarm_clock_time(hour, minute, second):
        return None
    tz = _alarm_display_timezone()
    dt = datetime(year, month, day, hour, minute, second, tzinfo=tz)
    ms = int(dt.timestamp() * 1000)
    return ms


def _alarm_time_ms_from_hour_minute(hour: int, minute: int) -> int:
    """仅提供时分时：取下一次该时刻（与 YYYYMMDD 解析同一挂钟时区，秒为 0）."""
    tz = _alarm_display_timezone()
    now = datetime.now(tz)
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return int(target.timestamp() * 1000)


def _normalize_days_of_week(
    raw: Union[str, Sequence[str], None],
) -> List[str]:
    """daysOfWeek：仅 daysOfWakeType=3 时有效；支持 JSON 字符串或列表."""
    if raw is None:
        return []
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as e:
            raise ToolInputError(f"days_of_week is not a valid JSON array string: {e}") from e
        if not isinstance(parsed, list):
            raise ToolInputError("days_of_week JSON must be an array")
        days = parsed
    else:
        days = list(raw)
    out: List[str] = []
    for d in days:
        if not isinstance(d, str) or d not in _DAYS_OF_WEEK:
            raise ToolInputError(
                f"days_of_week elements must be one of: {', '.join(_DAYS_OF_WEEK)}, got: {d!r}"
            )
        out.append(d)
    return out


def _normalize_delete_items(
    items: Optional[Union[str, List[Any]]],
    alarm_id: Optional[str],
) -> List[Dict[str, str]]:
    """解析 delete 的 items；支持 JSON 字符串或列表，元素含 entityId 或 entity_id."""
    if items is None and alarm_id:
        return [{"entityId": str(alarm_id)}]
    if items is None:
        raise ToolInputError("Must provide items (list or JSON string) or alarm_id (for single deletion)")

    parsed_list: List[Any]
    if isinstance(items, str):
        try:
            parsed = json.loads(items)
        except json.JSONDecodeError as e:
            raise ToolInputError(f"items is not a valid JSON array: {e}") from e
        if not isinstance(parsed, list):
            raise ToolInputError("items JSON must be an array")
        parsed_list = parsed
    elif isinstance(items, list):
        parsed_list = items
    else:
        raise ToolInputError("items must be a list or a JSON array string")

    if not parsed_list:
        raise ToolInputError("items cannot be empty")

    result: List[Dict[str, str]] = []
    for i, item in enumerate(parsed_list):
        if not isinstance(item, dict):
            raise ToolInputError(f"items[{i}] must be an object")
        eid = item.get("entityId") or item.get("entity_id")
        if not eid or not isinstance(eid, str):
            raise ToolInputError(f"items[{i}] is missing a valid entityId/entity_id")
        result.append({"entityId": eid})
    return result


@tool(
    name="create_alarm",
    description="""Creates an alarm on the user's device.

Time (provide one of the two; alarm_time takes precedence):
- alarm_time: string YYYYMMDD hhmmss (e.g. 20240315 143000)
- hour + minute: use when only hour/minute are known; the alarm is set to the "next" occurrence of that time (local time)

Optional:
- alarm_title / label: title, defaults to "闹钟" (label is an alias of alarm_title)
- alarm_snooze_duration: snooze interval (minutes), 5/10/15/20/25/30, default 10
- alarm_snooze_total: number of snooze repeats, 0/1/3/5/10, default 0
- alarm_ring_duration: ring duration (minutes), 1/5/10/15/20/30, default 5
- days_of_wake_type: 0 once 1 public holidays 2 every day 3 custom 4 workdays, default 0
- days_of_week: required only when days_of_wake_type=3; JSON string or single-element list, values Mon..Sun

Note: the operation times out after about 60 seconds; retry at most once on failure; confirm the current real date and time before creating.
""",
)
async def create_alarm(
    hour: Optional[int] = None,
    minute: Optional[int] = None,
    alarm_time: Optional[str] = None,
    alarm_title: Optional[str] = None,
    label: Optional[str] = None,
    alarm_snooze_duration: Optional[int] = None,
    alarm_snooze_total: Optional[int] = None,
    alarm_ring_duration: Optional[int] = None,
    days_of_wake_type: Optional[int] = None,
    days_of_week: Optional[Union[str, List[str]]] = None,
) -> Dict[str, Any]:
    """创建闹钟"""
    try:
        title = alarm_title if alarm_title is not None else label
        if title is None or title == "":
            title = "闹钟"

        snooze_d = 10 if alarm_snooze_duration is None else alarm_snooze_duration
        snooze_t = 0 if alarm_snooze_total is None else alarm_snooze_total
        ring_d = 5 if alarm_ring_duration is None else alarm_ring_duration
        wake_t = 0 if days_of_wake_type is None else days_of_wake_type

        if snooze_d not in _ALARM_SNOOZE_DURATION:
            raise ToolInputError(f"alarm_snooze_duration must be one of: {sorted(_ALARM_SNOOZE_DURATION)}")
        if snooze_t not in _ALARM_SNOOZE_TOTAL:
            raise ToolInputError(f"alarm_snooze_total must be one of: {sorted(_ALARM_SNOOZE_TOTAL)}")
        if ring_d not in _ALARM_RING_DURATION:
            raise ToolInputError(f"alarm_ring_duration must be one of: {sorted(_ALARM_RING_DURATION)}")
        if wake_t not in _DAYS_OF_WAKE_TYPE:
            raise ToolInputError(f"days_of_wake_type must be one of: {sorted(_DAYS_OF_WAKE_TYPE)}")

        alarm_ms: Optional[int] = None
        if alarm_time:
            alarm_ms = _parse_alarm_time_to_ms(alarm_time)
            if alarm_ms is None:
                raise ToolInputError(
                    "alarm_time must be in the format YYYYMMDD hhmmss (e.g. 20240315 143000)"
                )
        elif hour is not None and minute is not None:
            if not isinstance(hour, int) or hour < 0 or hour > 23:
                raise ToolInputError("hour must be an integer between 0-23")
            if not isinstance(minute, int) or minute < 0 or minute > 59:
                raise ToolInputError("minute must be an integer between 0-59")
            alarm_ms = _alarm_time_ms_from_hour_minute(hour, minute)
        else:
            raise ToolInputError("Provide alarm_time (YYYYMMDD hhmmss), or provide both hour and minute")

        week_list: List[str] = []
        if wake_t == 3:
            week_list = _normalize_days_of_week(days_of_week)
        elif days_of_week:
            logger.warning(
                "[ALARM_TOOL] 已忽略 days_of_week（仅当 days_of_wake_type=3 时有效），"
                "当前 days_of_wake_type=%s",
                wake_t,
            )

        intent_param: Dict[str, Any] = {
            "entityName": "Alarm",
            "alarmTime": alarm_ms,
            "alarmTitle": title,
            "alarmSnoozeDuration": snooze_d,
            "alarmSnoozeTotal": snooze_t,
            "alarmRingDuration": ring_d,
            "daysOfWakeType": wake_t,
        }
        if wake_t == 3 and week_list:
            intent_param["daysOfWeek"] = week_list

        logger.info("[ALARM_TOOL] Creating alarm, alarmTime(ms)=%s", alarm_ms)

        command = {
            "header": {"namespace": "Common", "name": "Action"},
            "payload": {
                "cardParam": {},
                "executeParam": {
                    "executeMode": "background",
                    "intentName": "CreateAlarm",
                    "bundleName": "com.huawei.hmos.clock",
                    "needUnlock": True,
                    "actionResponse": True,
                    "appType": "OHOS_APP",
                    "timeOut": 5,
                    "intentParam": intent_param,
                    "permissionId": [],
                    "achieveType": "INTENT",
                },
                "responses": [{"resultCode": "", "displayText": "", "ttsText": ""}],
                "needUploadResult": True,
                "noHalfPage": False,
                "pageControlRelated": False,
            },
        }

        outputs = await execute_device_command("CreateAlarm", command)

        raise_if_device_error(outputs, "Failed to create alarm")

        result = outputs.get("result", {})
        code = outputs.get("code")
        logger.info("[ALARM_TOOL] Alarm created successfully")

        return format_success_response(
            {
                "success": True,
                "alarm": {
                    "entityId": result.get("entityId"),
                    "entityName": result.get("entityName"),
                    "alarmTitle": result.get("alarmTitle"),
                    "alarmTime": result.get("alarmTime"),
                    "alarmState": result.get("alarmState"),
                    "alarmRingDuration": result.get("alarmRingDuration"),
                    "alarmSnoozeDuration": result.get("alarmSnoozeDuration"),
                    "alarmSnoozeTotal": result.get("alarmSnoozeTotal"),
                    "daysOfWakeType": result.get("daysOfWakeType"),
                },
                "code": code,
            },
            "Alarm created successfully",
        )

    except ToolInputError:
        raise
    except Exception as e:
        logger.error(f"[ALARM_TOOL] Failed to create alarm: {e}")
        raise RuntimeError(f"Failed to create alarm: {str(e)}") from e


@tool(
    name="search_alarms",
    description="""Searches alarms on the user's device. At least one search criterion must be met (by default, queries all).

Criteria (combinable):
- range_type: all / next / current
- alarm_state: 0 off 1 on
- days_of_wake_type: 0-4 (same meaning as when creating)
- start_time + end_time: time range, both in YYYYMMDD hhmmss format, must be provided as a pair;
  device side uses timeInterval: [startMs, endMs]

Note: the operation times out after about 60 seconds; confirm the current time before searching.
""",
)
async def search_alarms(
    range_type: Optional[str] = None,
    alarm_state: Optional[int] = None,
    days_of_wake_type: Optional[int] = None,
    start_time: Optional[str] = None,
    end_time: Optional[str] = None,
) -> Dict[str, Any]:
    """搜索闹钟."""
    try:
        has_range = range_type is not None
        has_as = alarm_state is not None
        has_wake = days_of_wake_type is not None
        has_start = start_time is not None
        has_end = end_time is not None

        if has_start != has_end:
            raise ToolInputError("start_time and end_time must be provided together or omitted together")

        if not (has_range or has_as or has_wake or (has_start and has_end)):
            range_type = "all"

        intent_param: Dict[str, Any] = {}
        if range_type is not None:
            if range_type not in _RANGE_TYPE:
                raise ToolInputError(f"range_type must be one of: {sorted(_RANGE_TYPE)}")
            intent_param["rangeType"] = range_type
        if alarm_state is not None:
            if alarm_state not in _ALARM_STATE:
                raise ToolInputError("alarm_state must be 0 or 1")
            intent_param["alarmState"] = alarm_state
        if days_of_wake_type is not None:
            if days_of_wake_type not in _DAYS_OF_WAKE_TYPE:
                raise ToolInputError(f"days_of_wake_type must be one of: {sorted(_DAYS_OF_WAKE_TYPE)}")
            intent_param["daysOfWakeType"] = days_of_wake_type
        if has_start and has_end:
            sm = _parse_alarm_time_to_ms(start_time or "")
            em = _parse_alarm_time_to_ms(end_time or "")
            if sm is None:
                raise ToolInputError("start_time must be in the format YYYYMMDD hhmmss")
            if em is None:
                raise ToolInputError("end_time must be in the format YYYYMMDD hhmmss")
            if sm >= em:
                raise ToolInputError("start_time must be earlier than end_time")
            intent_param["timeInterval"] = [sm, em]

        logger.info("[ALARM_TOOL] Searching alarms, intent keys=%s", list(intent_param.keys()))

        command = {
            "header": {"namespace": "Common", "name": "Action"},
            "payload": {
                "cardParam": {},
                "executeParam": {
                    "executeMode": "background",
                    "intentName": "SearchAlarm",
                    "bundleName": "com.huawei.hmos.clock",
                    "needUnlock": True,
                    "actionResponse": True,
                    "appType": "OHOS_APP",
                    "timeOut": 5,
                    "intentParam": intent_param,
                    "permissionId": [],
                    "achieveType": "INTENT",
                },
                "responses": [{"resultCode": "", "displayText": "", "ttsText": ""}],
                "needUploadResult": True,
                "noHalfPage": False,
                "pageControlRelated": False,
            },
        }

        outputs = await execute_device_command("SearchAlarm", command)

        raise_if_device_error(outputs, "Failed to search alarms")

        result = outputs.get("result", {})
        items = result.get("items", []) if isinstance(result, dict) else []

        parsed_alarms: List[Any] = []
        for item in items:
            if isinstance(item, str):
                try:
                    parsed_alarms.append(json.loads(item))
                except json.JSONDecodeError as e:
                    logger.warning(
                        "[ALARM_TOOL] 无法解析闹钟项: %s, error: %s",
                        item,
                        e,
                    )
            elif isinstance(item, dict):
                parsed_alarms.append(item)

        logger.info(f"[ALARM_TOOL] Found {len(parsed_alarms)} alarms")

        return format_success_response(
            {"alarms": parsed_alarms, "count": len(parsed_alarms)},
            f"Found {len(parsed_alarms)} alarms",
        )

    except ToolInputError:
        raise
    except Exception as e:
        logger.error(f"[ALARM_TOOL] Failed to search alarms: {e}")
        raise RuntimeError(f"Failed to search alarms: {str(e)}") from e


@tool(
    name="modify_alarm",
    description="""Modifies an existing alarm on the user's device. Must provide entity_id (the entityId returned by the device).

Optional fields (same as creation; only fill in the items to modify):
- alarm_time: YYYYMMDD hhmmss
- alarm_title: title
- alarm_state: 0 off 1 on
- alarm_snooze_duration / alarm_snooze_total / alarm_ring_duration: same enums as creation
- days_of_wake_type / days_of_week: custom weekday rules same as creation

Compatibility: alarm_id and entity_id are interchangeable (both are the device-side alarm entity ID).
enabled: true/false and alarm_state are interchangeable (enabled maps to 1/0).

Note: for fields not being modified, if you want to keep their original values, first use search_alarms to fetch the original values and pass them in as well, to avoid them being overwritten by defaults.
""",
)
async def modify_alarm(
    entity_id: Optional[str] = None,
    alarm_id: Optional[str] = None,
    alarm_time: Optional[str] = None,
    alarm_title: Optional[str] = None,
    alarm_state: Optional[int] = None,
    enabled: Optional[bool] = None,
    alarm_snooze_duration: Optional[int] = None,
    alarm_snooze_total: Optional[int] = None,
    alarm_ring_duration: Optional[int] = None,
    days_of_wake_type: Optional[int] = None,
    days_of_week: Optional[Union[str, List[str]]] = None,
) -> Dict[str, Any]:
    """修改闹钟."""
    try:
        eid = (entity_id or alarm_id or "").strip()
        if not eid:
            raise ToolInputError("Missing entity_id or alarm_id (device-side alarm entityId)")

        intent_param: Dict[str, Any] = {
            "entityName": "Alarm",
            "entityId": eid,
        }

        if alarm_time is not None:
            ms = _parse_alarm_time_to_ms(alarm_time)
            if ms is None:
                raise ToolInputError("alarm_time must be in the format YYYYMMDD hhmmss")
            intent_param["alarmTime"] = ms

        if alarm_title is not None:
            intent_param["alarmTitle"] = alarm_title

        eff_state = alarm_state
        if eff_state is None and enabled is not None:
            eff_state = 1 if enabled else 0
        if eff_state is not None:
            if eff_state not in _ALARM_STATE:
                raise ToolInputError("alarm_state must be 0 or 1")
            intent_param["alarmState"] = eff_state

        if alarm_snooze_duration is not None:
            if alarm_snooze_duration not in _ALARM_SNOOZE_DURATION:
                raise ToolInputError(f"alarm_snooze_duration must be one of: {sorted(_ALARM_SNOOZE_DURATION)}")
            intent_param["alarmSnoozeDuration"] = alarm_snooze_duration

        if alarm_snooze_total is not None:
            if alarm_snooze_total not in _ALARM_SNOOZE_TOTAL:
                raise ToolInputError(f"alarm_snooze_total must be one of: {sorted(_ALARM_SNOOZE_TOTAL)}")
            intent_param["alarmSnoozeTotal"] = alarm_snooze_total

        if alarm_ring_duration is not None:
            if alarm_ring_duration not in _ALARM_RING_DURATION:
                raise ToolInputError(f"alarm_ring_duration must be one of: {sorted(_ALARM_RING_DURATION)}")
            intent_param["alarmRingDuration"] = alarm_ring_duration

        if days_of_wake_type is not None:
            if days_of_wake_type not in _DAYS_OF_WAKE_TYPE:
                raise ToolInputError(f"days_of_wake_type must be one of: {sorted(_DAYS_OF_WAKE_TYPE)}")
            intent_param["daysOfWakeType"] = days_of_wake_type

        if days_of_week is not None:
            if days_of_wake_type != 3:
                if days_of_wake_type is None:
                    raise ToolInputError("When using days_of_week, also specify days_of_wake_type=3")
                logger.warning(
                    "[ALARM_TOOL] 已忽略 days_of_week（仅当 days_of_wake_type=3 时有效）"
                )
            else:
                week_list = _normalize_days_of_week(days_of_week)
                if week_list:
                    intent_param["daysOfWeek"] = week_list

        logger.info("[ALARM_TOOL] Modifying alarm entityId=%s", eid)

        command = {
            "header": {"namespace": "Common", "name": "Action"},
            "payload": {
                "cardParam": {},
                "executeParam": {
                    "executeMode": "background",
                    "intentName": "ModifyAlarm",
                    "bundleName": "com.huawei.hmos.clock",
                    "needUnlock": True,
                    "actionResponse": True,
                    "appType": "OHOS_APP",
                    "timeOut": 5,
                    "intentParam": intent_param,
                    "permissionId": [],
                    "achieveType": "INTENT",
                },
                "responses": [{"resultCode": "", "displayText": "", "ttsText": ""}],
                "needUploadResult": True,
                "noHalfPage": False,
                "pageControlRelated": False,
            },
        }

        outputs = await execute_device_command("ModifyAlarm", command)

        raise_if_device_error(outputs, "Failed to modify alarm")

        result = outputs.get("result", {})

        return format_success_response(
            {
                "entity_id": eid,
                "entityId": result.get("entityId"),
                "alarmTitle": result.get("alarmTitle"),
                "alarmTime": result.get("alarmTime"),
                "alarmState": result.get("alarmState"),
                "alarmRingDuration": result.get("alarmRingDuration"),
                "alarmSnoozeDuration": result.get("alarmSnoozeDuration"),
                "alarmSnoozeTotal": result.get("alarmSnoozeTotal"),
                "daysOfWakeType": result.get("daysOfWakeType"),
            },
            "Alarm modified successfully",
        )

    except ToolInputError:
        raise
    except Exception as e:
        logger.error(f"[ALARM_TOOL] Failed to modify alarm: {e}")
        raise RuntimeError(f"Failed to modify alarm: {str(e)}") from e


@tool(
    name="delete_alarm",
    description="""Deletes alarms on the user's device.

Parameters (provide one of the two):
- items: list of alarms to delete, each item containing entityId (or entity_id); may be a JSON array string
- alarm_id: the entity ID can be passed directly when deleting a single alarm

Note: deletion is irreversible; the operation times out after about 60 seconds.
""",
)
async def delete_alarm(
    items: Optional[Union[str, List[Dict[str, Any]]]] = None,
    alarm_id: Optional[str] = None,
) -> Dict[str, Any]:
    """删除闹钟."""
    try:
        norm_items = _normalize_delete_items(items, alarm_id)

        logger.info("[ALARM_TOOL] Deleting %s alarm(s)", len(norm_items))

        command = {
            "header": {"namespace": "Common", "name": "Action"},
            "payload": {
                "cardParam": {},
                "executeParam": {
                    "executeMode": "background",
                    "intentName": "DeleteAlarm",
                    "bundleName": "com.huawei.hmos.clock",
                    "needUnlock": True,
                    "actionResponse": True,
                    "appType": "OHOS_APP",
                    "timeOut": 5,
                    "intentParam": {"items": norm_items},
                    "permissionId": [],
                    "achieveType": "INTENT",
                },
                "responses": [{"resultCode": "", "displayText": "", "ttsText": ""}],
                "needUploadResult": True,
                "noHalfPage": False,
                "pageControlRelated": False,
            },
        }

        outputs = await execute_device_command("DeleteAlarm", command)

        raise_if_device_error(outputs, "Failed to delete alarm")

        result = outputs.get("result", {})

        return format_success_response(
            {
                "items": norm_items,
                "entityId": result.get("entityId"),
                "success": True,
            },
            "Alarm deleted",
        )

    except ToolInputError:
        raise
    except Exception as e:
        logger.error(f"[ALARM_TOOL] Failed to delete alarm: {e}")
        raise RuntimeError(f"Failed to delete alarm: {str(e)}") from e

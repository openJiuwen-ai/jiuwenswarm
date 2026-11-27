# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Calendar tools - 日历工具.

包含：
- create_calendar_event: 创建日程
- search_calendar_event: 检索日程
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional

from openjiuwen.core.foundation.tool import tool

from jiuwenclaw.utils import logger

from .utils import (
    execute_device_command,
    format_success_response,
    raise_if_device_error,
    ToolInputError,
)


def _format_timestamp_to_datetime(timestamp_ms: int) -> str:
    """将毫秒时间戳转换为 yyyy-mm-dd hh:mm:ss 格式.

    Args:
        timestamp_ms: 毫秒时间戳

    Returns:
        格式化的日期时间字符串
    """
    if not timestamp_ms:
        return ""
    dt = datetime.fromtimestamp(timestamp_ms / 1000)
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _convert_event_timestamps(event: Dict[str, Any]) -> Dict[str, Any]:
    """将事件中的时间戳转换为可读格式.

    Args:
        event: 日程事件字典

    Returns:
        转换后的事件字典
    """
    result = dict(event)
    if "dtStart" in result and result["dtStart"]:
        result["dtStart"] = _format_timestamp_to_datetime(result["dtStart"])
    if "dtEnd" in result and result["dtEnd"]:
        result["dtEnd"] = _format_timestamp_to_datetime(result["dtEnd"])
    return result


def _parse_time_string_ymd_hhmmss(time_str: str) -> int:
    """解析 YYYYMMDD hhmmss 格式为毫秒时间戳."""
    cleaned = " ".join(time_str.strip().split())
    parts = cleaned.split(" ")
    if len(parts) != 2:
        raise ValueError(
            f"Invalid time format: {time_str}. Expected format: YYYYMMDD hhmmss"
        )
    date_part, time_part = parts[0], parts[1]
    if len(date_part) != 8 or len(time_part) != 6:
        raise ValueError(
            f"Invalid time format: {time_str}. Expected format: YYYYMMDD hhmmss"
        )
    year = int(date_part[0:4])
    month = int(date_part[4:6])
    day = int(date_part[6:8])
    hours = int(time_part[0:2])
    minutes = int(time_part[2:4])
    seconds = int(time_part[4:6])
    dt = datetime(year, month, day, hours, minutes, seconds)
    return int(dt.timestamp() * 1000)


@tool(
    name="create_calendar_event",
    description=(
        "在用户设备上创建日程。需要提供日程标题、开始时间和结束时间。"
        "时间格式必须为：yyyy-mm-dd hh:mm:ss（例如：2024-01-15 14:30:00）。"
        "注意：该工具执行时间较长（最多60秒），请勿重复调用，超时或失败时最多重试一次。\n"
        "  注意事项：使用该工具之前需获取当前真实时间\n"
    ),
)
async def create_calendar_event(
    title: str,
    dt_start: str,
    dt_end: str,
) -> Dict[str, Any]:
    """创建日程.

    Args:
        title: 日程标题/名称，必填
        dt_start: 开始时间，格式 yyyy-mm-dd hh:mm:ss
        dt_end: 结束时间，格式 yyyy-mm-dd hh:mm:ss

    Returns:
        包含创建结果的响应字典
    """
    try:
        logger.info(
            f"[CALENDAR_TOOL] Create calendar event - title: {title}, "
            f"dt_start: {dt_start}, dt_end: {dt_end}"
        )

        # 验证参数
        if not title:
            raise ToolInputError("缺少必填参数 title（日程标题）")
        if not dt_start:
            raise ToolInputError("缺少必填参数 dt_start（开始时间）")
        if not dt_end:
            raise ToolInputError("缺少必填参数 dt_end（结束时间）")

        # 转换时间字符串为时间戳
        try:
            start_dt = datetime.strptime(dt_start, "%Y-%m-%d %H:%M:%S")
            end_dt = datetime.strptime(dt_end, "%Y-%m-%d %H:%M:%S")
            dt_start_ms = int(start_dt.timestamp() * 1000)
            dt_end_ms = int(end_dt.timestamp() * 1000)
        except ValueError:
            raise ToolInputError(
                "时间格式错误。必须使用：yyyy-mm-dd hh:mm:ss（例如：2024-01-15 14:30:00）"
            ) from ValueError

        intent_param = {
            "title": title,
            "dtStart": dt_start_ms,
            "dtEnd": dt_end_ms,
        }

        # CreateCalendarEvent：executeParam 不含 appType、permissionId
        command = {
            "header": {
                "namespace": "Common",
                "name": "ActionAndResult",
            },
            "payload": {
                "cardParam": {},
                "executeParam": {
                    "executeMode": "background",
                    "intentName": "CreateCalendarEvent",
                    "bundleName": "com.huawei.hmos.calendardata",
                    "dimension": "",
                    "needUnlock": True,
                    "actionResponse": True,
                    "timeOut": 5,
                    "intentParam": intent_param,
                    "achieveType": "INTENT",
                },
                "responses": [{"resultCode": "", "displayText": "", "ttsText": ""}],
                "needUploadResult": True,
                "noHalfPage": False,
                "pageControlRelated": False,
            },
        }

        # 执行命令
        result = await execute_device_command("CreateCalendarEvent", command)

        if isinstance(result, dict):
            raise_if_device_error(result, "创建日程失败")

        logger.info("[CALENDAR_TOOL] Calendar event created successfully")
        return format_success_response(
            {"title": title, "dt_start": dt_start, "dt_end": dt_end, "result": result},
            f"日程 '{title}' 创建成功",
        )

    except ToolInputError:
        raise
    except Exception as e:
        logger.error(f"[CALENDAR_TOOL] Failed to create calendar event: {e}")
        raise RuntimeError(f"创建日程失败: {str(e)}") from e


@tool(
    name="search_calendar_event",
    description="""检索用户日历中的日程安排。根据时间范围和可选的日程标题进行检索。时间格式必须为：YYYYMMDD hhmmss（例如：20240115 143000）。

时间范围说明：
- 查询某一天的日程：使用该天的 00:00:00 到 23:59:59（例如：20240115 000000 到 20240115 235959）
- 查询上午的日程：使用 06:00:00 到 12:00:00
- 查询下午的日程：使用 12:00:00 到 18:00:00
- 查询晚上的日程：使用 18:00:00 到 23:59:59
- 查询某个时刻附近的日程：使用该时刻前后1小时的区间（例如：查询3点左右的日程，使用 14:00:00 到 16:00:00）

注意：
a. 该工具执行时间较长（最多60秒），请勿重复调用，超时或失败时最多重试一次。
b. 使用该工具之前需获取当前真实时间
""",
)
async def search_calendar_event(
    start_time: str,
    end_time: str,
    title: Optional[str] = None,
) -> Dict[str, Any]:
    """检索日程.

    Args:
        start_time: 起始时间，格式 YYYYMMDD hhmmss
        end_time: 结束时间，格式 YYYYMMDD hhmmss
        title: 日程标题过滤（可选）

    Returns:
        包含日程列表的响应字典
    """
    try:
        logger.info(
            f"[SEARCH_CALENDAR_TOOL] Searching calendar events - "
            f"start_time: {start_time}, end_time: {end_time}, title: {title}"
        )

        if not start_time or not end_time:
            raise ToolInputError("缺少必填参数 start_time 与 end_time")

        try:
            start_time_ms = _parse_time_string_ymd_hhmmss(start_time)
            end_time_ms = _parse_time_string_ymd_hhmmss(end_time)
        except ValueError as e:
            raise ToolInputError(
                "时间格式错误。必须使用：YYYYMMDD hhmmss（例如：20240115 143000）。"
                f" {e}"
            ) from e

        intent_param: Dict[str, Any] = {
            "timeInterval": [start_time_ms, end_time_ms],
        }
        if title:
            intent_param["title"] = title

        command = {
            "header": {
                "namespace": "Common",
                "name": "Action",
            },
            "payload": {
                "cardParam": {},
                "executeParam": {
                    "executeMode": "background",
                    "intentName": "SearchCalendarEvent",
                    "bundleName": "com.huawei.hmos.calendardata",
                    "dimension": "",
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

        # 执行命令
        outputs = await execute_device_command("SearchCalendarEvent", command)

        raise_if_device_error(outputs, "检索日程失败")

        # 获取结果
        result = outputs.get("result", {})
        items = result.get("items", []) if isinstance(result, dict) else []

        # 转换时间戳为可读格式
        formatted_items = [_convert_event_timestamps(item) for item in items]

        logger.info(f"[SEARCH_CALENDAR_TOOL] Found {len(formatted_items)} events")

        return format_success_response(
            {"events": formatted_items, "count": len(formatted_items)},
            f"搜索到 {len(formatted_items)} 条日程",
        )

    except ToolInputError:
        raise
    except Exception as e:
        logger.error(f"[SEARCH_CALENDAR_TOOL] Failed to search calendar: {e}")
        raise RuntimeError(f"搜索日程失败: {str(e)}") from e

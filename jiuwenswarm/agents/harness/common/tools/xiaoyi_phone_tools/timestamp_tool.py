# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Timestamp tool - 时间戳转换工具.

包含：
- convert_timestamp_to_utc8_time: 将时间戳转换为 UTC+8 时间格式
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from openjiuwen.core.foundation.tool import tool

from jiuwenswarm.common.utils import logger
from .utils import ToolInputError


@tool(
    name="convert_timestamp_to_utc8_time",
    description="""Converts a timestamp to the standard UTC+8 time format. Supports second-level and millisecond-level timestamps.

Input parameter:
- timestamp: the timestamp (numeric type), either second-level (10 digits) or millisecond-level (13 digits)

Output format:
- YYYYMMDD hhmmss (e.g.: 20240315 143000 means March 15, 2024 14:30:00 Beijing time)

Important:
If results returned by tools such as the calendar search tool (search_calendar_event) and the alarm search tool (search_alarm) contain timestamps,
call this timestamp conversion tool first to convert them into the standard Beijing time format, then answer the user or take the next step based on the standard time.

Example:
- Input: 1710498600 (seconds) or 1710498600000 (milliseconds)
- Output: 20240315 143000""",
)
def convert_timestamp_to_utc8_time(timestamp: float) -> dict:
    """将时间戳转换为 UTC+8 时间格式."""
    if timestamp is None:
        raise ToolInputError("Missing required parameter: timestamp")

    if not isinstance(timestamp, (int, float)):
        raise ToolInputError("timestamp must be a numeric type")

    import math
    if math.isnan(timestamp) or math.isinf(timestamp):
        raise ToolInputError("timestamp is not a valid number")

    # 判断秒级还是毫秒级
    ts_abs = abs(timestamp)
    ts_str = str(int(ts_abs))

    if len(ts_str) == 13:
        timestamp_in_ms = timestamp
    elif len(ts_str) == 10:
        timestamp_in_ms = timestamp * 1000
    elif ts_abs > 1000000000000:
        timestamp_in_ms = timestamp
    else:
        timestamp_in_ms = timestamp * 1000

    # 转换为 UTC+8
    utc8_tz = timezone(timedelta(hours=8))
    try:
        dt = datetime.fromtimestamp(timestamp_in_ms / 1000, tz=utc8_tz)
    except (OSError, OverflowError, ValueError) as e:
        raise ToolInputError(f"Invalid timestamp, cannot convert to a date: {e}") from e

    formatted = dt.strftime("%Y%m%d %H%M%S")

    logger.info(
        "[TIMESTAMP_TOOL] Converted timestamp %s -> %s",
        timestamp,
        formatted,
    )

    return {
        "content": [
            {
                "type": "text",
                "text": formatted,
            }
        ]
    }

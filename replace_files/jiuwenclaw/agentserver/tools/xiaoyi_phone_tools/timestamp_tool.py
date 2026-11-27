# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Timestamp tool - 时间戳转换工具.

包含：
- convert_timestamp_to_utc8_time: 将时间戳转换为 UTC+8 时间格式
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from openjiuwen.core.foundation.tool import tool

from jiuwenclaw.utils import logger
from .utils import ToolInputError


@tool(
    name="convert_timestamp_to_utc8_time",
    description="""将时间戳转换为标准 UTC+8 时间格式。支持秒级时间戳和毫秒级时间戳。

输入参数：
- timestamp: 时间戳（数字类型），可以是秒级（10位）或毫秒级（13位）

输出格式：
- YYYYMMDD hhmmss（例如：20240315 143000 表示 2024年3月15日 14:30:00 北京时间）

重要说明：
搜索日程工具（search_calendar_event）和搜索闹钟工具（search_alarm）等工具中返回结果如果包含时间戳。
建议优先调用本时间戳转换工具，将时间戳转换为标准北京时间格式，再基于标准时间进行用户回答或下一步操作。

示例：
- 输入：1710498600（秒级）或 1710498600000（毫秒级）
- 输出：20240315 143000""",
)
def convert_timestamp_to_utc8_time(timestamp: float) -> dict:
    """将时间戳转换为 UTC+8 时间格式."""
    if timestamp is None:
        raise ToolInputError("缺少必需参数：timestamp")

    if not isinstance(timestamp, (int, float)):
        raise ToolInputError("timestamp 必须是数字类型")

    import math
    if math.isnan(timestamp) or math.isinf(timestamp):
        raise ToolInputError("timestamp 不是有效数字")

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
        raise ToolInputError(f"无效的时间戳，无法转换为日期: {e}") from e

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

# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Location tool - 获取手机当前定位.

通过 WebSocket 发送 GetCurrentLocation 指令到手机端，返回设备 outputs 的 JSON。
"""

from __future__ import annotations

import json
from typing import Any, Dict, Literal

from openjiuwen.core.foundation.tool import tool

from jiuwenswarm.common.utils import logger
from .utils import execute_device_command, raise_if_device_error


@tool(
    name="get_user_location",
    description=(
        "Gets the user's current location (latitude/longitude coordinates, WGS84 coordinate system). "
        "Requires the user's device to grant location access permission. "
        "Note: the operation timeout is 60 seconds; do not call this tool repeatedly; "
        "if it times out or fails, retry at most once."
    ),
)
async def get_user_location(request_type: Literal["current_location"], ) -> Dict[str, Any]:
    """获取用户当前地理位置.

    Returns:
        content[0].text 为设备 outputs 的 JSON 字符串
    """

    logger.info("[LOCATION_TOOL] Starting execution - Building GetCurrentLocation command...")
    command = {
        "header": {
            "namespace": "Common",
            "name": "Action",
        },
        "payload": {
            "cardParam": {},
            "executeParam": {
                "achieveType": "INTENT",
                "actionResponse": True,
                "bundleName": "com.huawei.hmos.aidispatchservice",
                "dimension": "",
                "executeMode": "background",
                "intentName": "GetCurrentLocation",
                "intentParam": {
                    "isNeedGeoAddress": True,
                },
                "needUnlock": True,
                "permissionId": [],
                "timeOut": 5,
            },
            "needUploadResult": True,
            "pageControlRelated": False,
            "responses": [
                {
                    "displayText": "",
                    "resultCode": "",
                    "ttsText": "",
                }
            ],
        },
    }

    logger.info("[LOCATION_TOOL] Waiting for location response...")
    outputs = await execute_device_command("GetCurrentLocation", command)

    if not isinstance(outputs, dict):
        outputs = {"value": outputs}

    raise_if_device_error(outputs, "Failed to get location")

    logger.info(
        f"[LOCATION_TOOL] Location retrieved successfully - outputs keys: {list(outputs.keys())}"
    )

    return {
        "content": [
            {
                "type": "text",
                "text": json.dumps(outputs, ensure_ascii=False),
            }
        ]
    }

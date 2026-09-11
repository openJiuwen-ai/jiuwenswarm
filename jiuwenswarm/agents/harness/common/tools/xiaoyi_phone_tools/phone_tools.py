# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Phone tools - 电话工具.

包含：
- call_phone: 拨打电话
"""

from __future__ import annotations

import json
from typing import Any, Dict, Optional

from openjiuwen.core.foundation.tool import tool

from jiuwenswarm.common.utils import logger
from .utils import (
    execute_device_command,
    raise_if_device_error,
    ToolInputError,
)


@tool(
    name="call_phone",
    description=(
        "Makes a phone call. Requires the phone number to dial. "
        "The slotId parameter is optional and defaults to 0 (primary SIM); set it to 1 if the user explicitly asks to use the secondary SIM. "
        "Note: the operation timeout is 60 seconds; do not call this tool repeatedly; "
        "if it times out or fails, retry at most once."
    ),
)
async def call_phone(
    phone_number: str,
    slot_id: Optional[int] = None,
) -> Dict[str, Any]:
    """拨打电话.

    Args:
        phone_number: 要拨打的电话号码，必填
        slot_id: SIM 卡槽（设备字段 slotId）；未传时按 0

    Returns:
        success、code、phoneNumber、slotId、message（与设备成功回调字段一致）
    """
    try:
        if slot_id is None:
            slot_id = 0

        logger.info(
            f"[CALL_PHONE_TOOL] Calling - phone_number: {phone_number}, slotId: {slot_id}"
        )

        if not phone_number or not isinstance(phone_number, str):
            raise ToolInputError("Missing required parameter phone_number (phone number)")

        phone_number = phone_number.strip()
        if not phone_number:
            raise ToolInputError("phone_number cannot be empty")

        if slot_id not in (0, 1):
            raise ToolInputError("slot_id must be 0 (primary SIM) or 1 (secondary SIM)")

        command = {
            "header": {
                "namespace": "Common",
                "name": "Action",
            },
            "payload": {
                "cardParam": {},
                "executeParam": {
                    "executeMode": "background",
                    "intentName": "StartCall",
                    "bundleName": "com.huawei.hmos.aidispatchservice",
                    "dimension": "",
                    "needUnlock": True,
                    "actionResponse": True,
                    "timeOut": 5,
                    "intentParam": {
                        "phoneNumber": phone_number,
                        "slotId": slot_id,
                    },
                    "achieveType": "INTENT",
                },
                "responses": [{"resultCode": "", "displayText": "", "ttsText": ""}],
                "needUploadResult": True,
                "noHalfPage": False,
                "pageControlRelated": False,
            },
        }

        outputs = await execute_device_command("StartCall", command)

        if not isinstance(outputs, dict):
            outputs = {}

        raise_if_device_error(outputs, "Failed to make the phone call")

        logger.info("[CALL_PHONE_TOOL] Call initiated successfully")

        return {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(outputs, ensure_ascii=False),
                }
            ]
        }

    except ToolInputError:
        raise
    except Exception as e:
        logger.error(f"[CALL_PHONE_TOOL] Failed to initiate call: {e}")
        raise RuntimeError(f"Failed to make the phone call: {str(e)}") from e

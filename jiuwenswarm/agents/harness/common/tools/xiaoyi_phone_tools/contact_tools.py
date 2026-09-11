# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Contact tools - 联系人工具.

命令结构要点：
- executeParam：intentName SearchContactLocal、bundleName aidispatchservice、appType OHOS_APP 等
- intentParam：name

包含：
- search_contact: 搜索联系人
"""

from __future__ import annotations

import json
from typing import Any, Dict

from openjiuwen.core.foundation.tool import tool

from jiuwenswarm.common.utils import logger
from .utils import (
    execute_device_command,
    format_success_response,
    ToolInputError,
)


@tool(
    name="search_contact",
    description=(
        "Search contact information on the user's device. Look up detailed contact information by name in the address book "
        "(including name, phone number, email, organization, job title, etc.). "
        "Note: the operation timeout is 60 seconds. Do not call this tool repeatedly; if it times out or fails, retry at most once."
    ),
)
async def search_contact(name: str) -> Dict[str, Any]:
    """搜索联系人

    Args:
        name: 联系人姓名，用于在通讯录中检索联系人信息

    Returns:
        content[0].text 为设备 outputs 的 JSON 字符串
    """
    try:
        if not isinstance(name, str) or not name.strip():
            raise ToolInputError("Missing required parameter: name")

        name_clean = name.strip()

        logger.info(
            "[SEARCH_CONTACT_TOOL] Searching contacts - name=%r",
            name_clean,
        )

        command = {
            "header": {
                "namespace": "Common",
                "name": "Action",
            },
            "payload": {
                "cardParam": {},
                "executeParam": {
                    "executeMode": "background",
                    "intentName": "SearchContactLocal",
                    "bundleName": "com.huawei.hmos.aidispatchservice",
                    "needUnlock": True,
                    "actionResponse": True,
                    "appType": "OHOS_APP",
                    "timeOut": 5,
                    "intentParam": {"name": name_clean},
                    "permissionId": [],
                    "achieveType": "INTENT",
                },
                "responses": [{"resultCode": "", "displayText": "", "ttsText": ""}],
                "needUploadResult": True,
                "noHalfPage": False,
                "pageControlRelated": False,
            },
        }

        outputs = await execute_device_command("SearchContactLocal", command)

        if not isinstance(outputs, dict):
            outputs = {"outputs": outputs}

        result = outputs.get("result")
        if not isinstance(result, dict):
            result = {}
        n = len(result.get("items", []))
        logger.info("[SEARCH_CONTACT_TOOL] found %s contacts", n)

        return format_success_response(
            dict(outputs),
            f"Found contact information ({n} entries)",
        )

    except ToolInputError:
        raise
    except Exception as e:
        logger.error(f"[SEARCH_CONTACT_TOOL] Failed to search contacts: {e}")
        raise RuntimeError(f"Failed to search contacts: {str(e)}") from e

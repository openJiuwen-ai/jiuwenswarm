# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Message tools - 短信/消息工具.

包含：
- send_sms: 发送短信
- search_message: 搜索短信
"""

from __future__ import annotations

import json
from typing import Any, Dict, Optional

from openjiuwen.core.foundation.tool import tool

from jiuwenswarm.common.utils import logger
from .utils import (
    _outputs_top_level_code_ok,
    execute_device_command,
    format_success_response,
    raise_if_device_error,
    ToolInputError,
)


def _mask_phone_for_log(phone: str) -> str:
    """日志中脱敏手机号."""
    p = (phone or "").strip()
    if len(p) <= 7:
        return "***"
    return f"{p[:3]}***{p[-4:]}"


def _nested_result_error_message(outputs: Dict[str, Any]) -> Optional[str]:
    """部分 Intent 在 result 子对象中带 code/errMsg，与顶层 outputs 分离."""
    r = outputs.get("result")
    if not isinstance(r, dict):
        return None
    c = r.get("code")
    if c is None or _outputs_top_level_code_ok(c):
        return None
    msg = r.get("errorMsg") or r.get("errMsg") or str(c)
    return str(msg)


def _log_outputs_summary(intent: str, outputs: Dict[str, Any]) -> None:
    try:
        blob = json.dumps(outputs, ensure_ascii=False)
    except Exception:
        blob = str(outputs)
    logger.info(
        "[%s_TOOL] outputs summary len=%s preview=%s",
        intent,
        len(blob),
        blob[:800],
    )


@tool(
    name="send_sms",
    description=(
        "Send an SMS message from the phone. Requires the recipient's phone number and the SMS content. "
        "A +86 prefix is added to the phone number automatically (if missing). "
        "Note: the operation timeout is 60 seconds. Do not call this tool repeatedly; if it times out or fails, retry at most once."
    ),
)
async def send_sms(phone_number: str, content: str) -> Dict[str, Any]:
    """发送短信.

    Args:
        phone_number: 接收方手机号码（会自动规范为 +86 前缀）
        content: 短信内容

    Returns:
        设备返回的完整 outputs，经 format_success_response 包装
    """
    try:
        logger.info(
            f"[SEND_MESSAGE_TOOL] Starting exec - phone_number: {phone_number}, content len: {len(content)}"
        )

        if not phone_number or not isinstance(phone_number, str):
            raise ToolInputError(
                "Missing required parameter: phone_number (recipient's phone number)"
            )

        if not content or not isinstance(content, str):
            raise ToolInputError("Missing required parameter: content (SMS text)")

        phone_number = phone_number.strip()
        content = content.strip()

        if not phone_number:
            raise ToolInputError("phone_number must not be empty")

        if not content:
            raise ToolInputError("content must not be empty")

        # 未带 +86 时：去掉前导 0、再去 86 前缀，再加 +86
        if not phone_number.startswith("+86"):
            if phone_number.startswith("0"):
                phone_number = phone_number[1:]
            if phone_number.startswith("86"):
                phone_number = phone_number[2:]
            phone_number = f"+86{phone_number}"

        logger.info(
            "[SEND_MESSAGE_TOOL] Normalized phone -> %s",
            _mask_phone_for_log(phone_number),
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
                    "intentName": "SendShortMessage",
                    "bundleName": "com.huawei.hmos.aidispatchservice",
                    "needUnlock": True,
                    "actionResponse": True,
                    "appType": "OHOS_APP",
                    "timeOut": 5,
                    "intentParam": {
                        "phoneNumber": phone_number,
                        "content": content,
                    },
                    "permissionId": [],
                    "achieveType": "INTENT",
                },
                "responses": [{"resultCode": "", "displayText": "", "ttsText": ""}],
                "needUploadResult": True,
                "noHalfPage": False,
                "pageControlRelated": False,
            },
        }

        outputs = await execute_device_command("SendShortMessage", command)

        if not isinstance(outputs, dict):
            outputs = {"outputs": outputs}

        _log_outputs_summary("SEND_MESSAGE", dict(outputs))
        raise_if_device_error(outputs, "Failed to send SMS")
        nested_err = _nested_result_error_message(outputs)
        if nested_err:
            logger.error(
                "[SEND_MESSAGE_TOOL] nested result error: %s",
                nested_err,
            )
            raise RuntimeError(f"Failed to send SMS: {nested_err}") from None

        logger.info("[SEND_MESSAGE_TOOL] Message send completed")

        return format_success_response(dict(outputs), f"SMS sent to {phone_number}")

    except ToolInputError:
        raise
    except Exception as e:
        logger.error(f"[SEND_MESSAGE_TOOL] Failed to send message: {e}")
        raise RuntimeError(f"Failed to send SMS: {str(e)}") from e


# Python import compatibility only. The exposed LLM tool name is ``send_sms``.
send_message = send_sms


@tool(
    name="search_message",
    description=(
        "Search SMS messages on the phone. Search SMS content by keyword. "
        "Note: the operation timeout is 60 seconds. Do not call this tool repeatedly; if it times out or fails, retry at most once."
    ),
)
async def search_message(
    content: str,
) -> Dict[str, Any]:
    """搜索短信

    Args:
        content: 搜索关键词，用于在短信内容中进行匹配

    Returns:
        content[0].text 为设备 outputs 的 JSON 字符串
    """
    try:
        if (
            not content
            or not isinstance(content, str)
            or content.strip() == ""
        ):
            raise ToolInputError(
                "Missing required parameter: content (must be a non-empty string)"
            )

        content = content.strip()
        logger.info(
            "[SEARCH_MESSAGE_TOOL] Searching messages - keyword=%r",
            content,
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
                    "intentName": "SearchMessage",
                    "bundleName": "com.huawei.hmos.aidispatchservice",
                    "needUnlock": True,
                    "actionResponse": True,
                    "appType": "OHOS_APP",
                    "timeOut": 5,
                    "intentParam": {
                        "content": content,
                    },
                    "permissionId": [],
                    "achieveType": "INTENT",
                },
                "responses": [{"resultCode": "", "displayText": "", "ttsText": ""}],
                "needUploadResult": True,
                "noHalfPage": False,
                "pageControlRelated": False,
            },
        }

        outputs = await execute_device_command("SearchMessage", command)

        if not isinstance(outputs, dict):
            outputs = {"outputs": outputs}

        _log_outputs_summary("SEARCH_MESSAGE", dict(outputs))

        # 与 search-message-tool.ts 一致：text 为完整 event.outputs 的 JSON
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
        logger.error(f"[SEARCH_MESSAGE_TOOL] Failed to search messages: {e}")
        raise RuntimeError(f"Failed to search SMS messages: {str(e)}") from e

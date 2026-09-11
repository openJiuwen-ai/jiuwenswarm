# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Note tools - 备忘录工具.

包含：
- create_note: 创建备忘录
- search_notes: 搜索备忘录
- modify_note: 修改备忘录
"""

from __future__ import annotations

from typing import Any, Dict

from openjiuwen.core.foundation.tool import tool

from jiuwenswarm.common.utils import logger
from .utils import (
    execute_device_command,
    format_success_response,
    raise_if_device_error,
    ToolInputError,
)


@tool(
    name="create_note",
    description="""Create a note on the user's device. Requires the note title and content.
  Note:
  a. The operation timeout is 60 seconds. Do not call this tool repeatedly.
  b. If any call failure occurs, retry at most once; do not call it repeatedly.
  c. Before calling the tool, carefully check that the call parameters meet the tool's requirements.
  """,
)
async def create_note(title: str, content: str) -> Dict[str, Any]:
    """创建备忘录.

    Args:
        title: 备忘录标题，必填
        content: 备忘录内容，必填

    Returns:
        设备返回的完整 outputs，经 format_success_response 包装
    """
    try:
        logger.info(f"[CREATE_NOTE_TOOL] Creating note - title: {title}")

        if not title or not isinstance(title, str):
            raise ToolInputError("Missing required parameter: title (note title)")
        if not content or not isinstance(content, str):
            raise ToolInputError("Missing required parameter: content (note content)")

        # CreateNote：executeParam 不含 appType、permissionId
        command = {
            "header": {
                "namespace": "Common",
                "name": "Action",
            },
            "payload": {
                "cardParam": {},
                "executeParam": {
                    "executeMode": "background",
                    "intentName": "CreateNote",
                    "bundleName": "com.huawei.hmos.notepad",
                    "dimension": "",
                    "needUnlock": True,
                    "actionResponse": True,
                    "timeOut": 5,
                    "intentParam": {
                        "title": title,
                        "content": content,
                    },
                    "achieveType": "INTENT",
                },
                "responses": [{"resultCode": "", "displayText": "", "ttsText": ""}],
                "needUploadResult": True,
                "noHalfPage": False,
                "pageControlRelated": False,
            },
        }

        outputs = await execute_device_command("CreateNote", command)

        if not isinstance(outputs, dict):
            outputs = {"outputs": outputs}

        raise_if_device_error(outputs, "Failed to create note")

        logger.info("[CREATE_NOTE_TOOL] Note create completed")

        return format_success_response(dict(outputs), f"Note '{title}' created successfully")

    except ToolInputError:
        raise
    except Exception as e:
        logger.error(f"[CREATE_NOTE_TOOL] Failed to create note: {e}")
        raise RuntimeError(f"Failed to create note: {str(e)}") from e


@tool(
    name="search_notes",
    description=(
        "Search notes on the user's device. Search by keyword across note titles, contents, and attachment names. "
        "Note: the operation timeout is 60 seconds. Do not call this tool repeatedly; if it times out or fails, retry at most once."
    ),
)
async def search_notes(query: str) -> Dict[str, Any]:
    """搜索备忘录.

    Args:
        query: 搜索关键词

    Returns:
        设备返回的完整 outputs，经 format_success_response 包装
    """
    try:
        logger.info(f"[SEARCH_NOTE_TOOL] Searching notes - query: {query}")

        if not query or not isinstance(query, str):
            raise ToolInputError("Missing required parameter: query (search keyword)")

        query = query.strip()
        if not query:
            raise ToolInputError("query must not be empty")

        # SearchNote：executeParam 不含 appType、permissionId
        command = {
            "header": {
                "namespace": "Common",
                "name": "Action",
            },
            "payload": {
                "cardParam": {},
                "executeParam": {
                    "executeMode": "background",
                    "intentName": "SearchNote",
                    "bundleName": "com.huawei.hmos.notepad",
                    "dimension": "",
                    "needUnlock": True,
                    "actionResponse": True,
                    "timeOut": 5,
                    "intentParam": {
                        "query": query,
                    },
                    "achieveType": "INTENT",
                },
                "responses": [{"resultCode": "", "displayText": "", "ttsText": ""}],
                "needUploadResult": True,
                "noHalfPage": False,
                "pageControlRelated": False,
            },
        }

        outputs = await execute_device_command("SearchNote", command)

        if not isinstance(outputs, dict):
            outputs = {"outputs": outputs}

        raise_if_device_error(outputs, "Failed to search notes")

        result = outputs.get("result")
        if not isinstance(result, dict):
            result = {}
        n = len(result.get("items", []))
        logger.info(f"[SEARCH_NOTE_TOOL] Search completed, items={n}")

        return format_success_response(dict(outputs), f"Found {n} notes")

    except ToolInputError:
        raise
    except Exception as e:
        logger.error(f"[SEARCH_NOTE_TOOL] Failed to search notes: {e}")
        raise RuntimeError(f"Failed to search notes: {str(e)}") from e


@tool(
    name="modify_note",
    description=(
        "Append new content to the specified note. Before use, you must first call the search_notes tool to obtain the note's entityId. "
        "Parameter description: entityId is the note's unique identifier (obtained from the search_notes tool), "
        "and text is the text content to append. "
        "Note: the operation timeout is 60 seconds. Do not call this tool repeatedly; if it times out or fails, retry at most once."
    ),
)
async def modify_note(
    entity_id: str,
    text: str,
) -> Dict[str, Any]:
    """修改备忘录（追加模式）.

    Args:
        entity_id: 备忘录实体 ID（设备侧字段名为 entityId）
        text: 要追加的文本

    Returns:
        设备返回的完整 outputs，经 format_success_response 包装
    """
    try:
        logger.info(f"[MODIFY_NOTE_TOOL] Modifying note - entity_id: {entity_id}")

        if not entity_id or not isinstance(entity_id, str):
            raise ToolInputError(
                "Missing required parameter: entity_id (device-side entityId)"
            )
        if not text or not isinstance(text, str):
            raise ToolInputError(
                "Missing required parameter: text (text content to append)"
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
                    "intentName": "ModifyNote",
                    "bundleName": "com.huawei.hmos.notepad",
                    "needUnlock": True,
                    "actionResponse": True,
                    "appType": "OHOS_APP",
                    "timeOut": 5,
                    "intentParam": {
                        "contentType": "1",
                        "text": text,
                        "entityId": entity_id,
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

        outputs = await execute_device_command("ModifyNote", command)

        if not isinstance(outputs, dict):
            outputs = {"outputs": outputs}

        raise_if_device_error(outputs, "Failed to modify note")

        logger.info("[MODIFY_NOTE_TOOL] Note modified successfully")

        return format_success_response(dict(outputs), "Note modified successfully")

    except ToolInputError:
        raise
    except Exception as e:
        logger.error(f"[MODIFY_NOTE_TOOL] Failed to modify note: {e}")
        raise RuntimeError(f"Failed to modify note: {str(e)}") from e

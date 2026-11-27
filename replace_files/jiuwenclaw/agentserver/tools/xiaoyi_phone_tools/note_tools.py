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

from jiuwenclaw.utils import logger
from .utils import (
    execute_device_command,
    format_success_response,
    raise_if_device_error,
    ToolInputError,
)


@tool(
    name="create_note",
    description="""在用户设备上创建备忘录。需要提供备忘录标题和内容。
  注意:
  a. 操作超时时间为60秒,请勿重复调用此工具
  b. 如果遇到各类调用失败场景,最多只能重试一次，不可以重复调用多次。
  c. 调用工具前需认真检查调用参数是否满足工具要求
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
            raise ToolInputError("缺少必填参数 title（备忘录标题）")
        if not content or not isinstance(content, str):
            raise ToolInputError("缺少必填参数 content（备忘录内容）")

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

        raise_if_device_error(outputs, "创建备忘录失败")

        logger.info("[CREATE_NOTE_TOOL] Note create completed")

        return format_success_response(dict(outputs), f"备忘录 '{title}' 创建成功")

    except ToolInputError:
        raise
    except Exception as e:
        logger.error(f"[CREATE_NOTE_TOOL] Failed to create note: {e}")
        raise RuntimeError(f"创建备忘录失败: {str(e)}") from e


@tool(
    name="search_notes",
    description=(
        "搜索用户设备上的备忘录内容。根据关键词在备忘录的标题、内容和附件名称中进行检索。"
        "注意:操作超时时间为60秒,请勿重复调用此工具,如果超时或失败,最多重试一次。"
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
            raise ToolInputError("缺少必填参数 query（搜索关键词）")

        query = query.strip()
        if not query:
            raise ToolInputError("query 不能为空")

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

        raise_if_device_error(outputs, "搜索备忘录失败")

        result = outputs.get("result")
        if not isinstance(result, dict):
            result = {}
        n = len(result.get("items", []))
        logger.info(f"[SEARCH_NOTE_TOOL] Search completed, items={n}")

        return format_success_response(dict(outputs), f"搜索到 {n} 条备忘录")

    except ToolInputError:
        raise
    except Exception as e:
        logger.error(f"[SEARCH_NOTE_TOOL] Failed to search notes: {e}")
        raise RuntimeError(f"搜索备忘录失败: {str(e)}") from e


@tool(
    name="modify_note",
    description=(
        "在指定备忘录中追加新内容。使用前必须先调用 search_notes 工具获取备忘录的 entityId。"
        "参数说明：entityId 是备忘录的唯一标识符（从 search_notes 工具获取），"
        "text 是要追加的文本内容。"
        "注意:操作超时时间为60秒,请勿重复调用此工具,如果超时或失败,最多重试一次。"
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
            raise ToolInputError("缺少必填参数 entity_id（设备侧 entityId）")
        if not text or not isinstance(text, str):
            raise ToolInputError("缺少必填参数 text（要追加的文本内容）")

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

        raise_if_device_error(outputs, "修改备忘录失败")

        logger.info("[MODIFY_NOTE_TOOL] Note modified successfully")

        return format_success_response(dict(outputs), "备忘录修改成功")

    except ToolInputError:
        raise
    except Exception as e:
        logger.error(f"[MODIFY_NOTE_TOOL] Failed to modify note: {e}")
        raise RuntimeError(f"修改备忘录失败: {str(e)}") from e

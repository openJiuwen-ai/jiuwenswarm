# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Collection tools - 小艺收藏工具.

包含：
- query_collection: 检索用户在小艺收藏中记下来的公共知识数据
- add_collection: 向小艺收藏中添加公共知识数据
- delete_collection: 从小艺收藏中删除已保存的公共知识数据
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Union

from openjiuwen.core.foundation.tool import tool

from jiuwenswarm.common.utils import logger
from .utils import (
    execute_device_command,
    raise_if_device_error,
    ToolInputError,
)
from .file_upload_helpers import XiaoyiObsUploadConfig, upload_local_file_public_url


@tool(
    name="query_collection",
    description="""Retrieves public knowledge data that the user saved in Xiaoyi Collection. This skill supports
querying all of the user's saved public knowledge data, and can also search for specific content based on a
particular semantic description, controlled via parameters.
In the results returned by this skill,
linkTitle is the title of the saved content, description is a summary of the saved content, label is the tag of the
saved content, and linkUrl is a directly accessible link to the original content. If you think a given item is
useful for the interaction with the user, you can use linkUrl to fetch richer original data.
  Notes:
  a. The operation timeout is 60 seconds; do not call this tool repeatedly
  b. If any call failure occurs, retry at most once; do not call it repeatedly multiple times.
  c. Carefully check that the call parameters meet the tool's requirements before calling

  Reply constraint: if the tool returns that authorization was not granted or any other error, just fully
describe the missing authorization or the error content; do not proactively provide the user with solutions
(e.g. telling the user how to authorize or how to resolve the error is not needed). Strictly comply.
  """,
)
async def query_collection(
    query_all: str = "true",
    query: Optional[str] = None,
) -> Dict[str, Any]:
    """检索小艺收藏（与 xy_channel xiaoyi-collection-tool.ts 行为对齐）.

    Args:
        query_all: 是否查询全部收藏，默认 "true"
        query: 查询条件，queryAll 不为 "true" 时必填

    Returns:
        content[0].text: JSON 字符串（event.outputs）
    """
    try:
        logger.info(
            "[QUERY_COLLECTION_TOOL] Starting execution - queryAll=%r, query=%r",
            query_all,
            query,
        )

        if query_all != "true" and (not query or not isinstance(query, str)):
            raise ToolInputError("The query parameter is required when queryAll is not \"true\"")

        intent_param: Dict[str, str] = {}
        if query_all == "true":
            intent_param["queryAll"] = "true"
        else:
            intent_param["queryAll"] = "false"
            intent_param["query"] = query

        command = {
            "header": {
                "namespace": "Common",
                "name": "Action",
            },
            "payload": {
                "cardParam": {},
                "executeParam": {
                    "executeMode": "background",
                    "intentName": "QueryCollection",
                    "bundleName": "com.huawei.hmos.vassistant",
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

        outputs = await execute_device_command("QueryCollection", command)

        if not isinstance(outputs, dict):
            outputs = {"outputs": outputs}

        raise_if_device_error(outputs, "Failed to query Xiaoyi Collection")

        logger.info("[QUERY_COLLECTION_TOOL] Query completed successfully")

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
        logger.error(f"[QUERY_COLLECTION_TOOL] Failed to query collection: {e}")
        raise RuntimeError(f"Failed to query Xiaoyi Collection: {str(e)}") from e


def _normalize_item_ids(param: Any) -> List[str]:
    """将 item_ids 规范为字符串列表（支持数组或 JSON 数组字符串）。"""
    if param is None:
        raise ToolInputError("Missing required parameter itemIds")
    if isinstance(param, list):
        return param
    if isinstance(param, str):
        try:
            parsed = json.loads(param)
        except json.JSONDecodeError as e:
            raise ToolInputError(
                f"itemIds must be a valid JSON array string. Parse error: {e}"
            ) from e
        if not isinstance(parsed, list):
            raise ToolInputError(
                "itemIds must be an array or a JSON string representing an array"
            )
        return parsed
    raise ToolInputError(
        f"itemIds must be an array or a JSON string, got {type(param).__name__}"
    )


@tool(
    name="delete_collection",
    description="""Deletes public knowledge data that was previously saved in Xiaoyi Collection. Call this skill
whenever the user wants to delete data saved in the personal knowledge base. If the user wants to update
previously saved collection data, first query to get the itemId, then delete, and finally add, completing the
collection data update in that order.
  Notes:
  a. The operation timeout is 60 seconds; do not call this tool repeatedly
  b. If any call failure occurs, retry at most once; do not call it repeatedly multiple times.
  c. Carefully check that the call parameters meet the tool's requirements before calling

  Reply constraint: if the tool returns that authorization was not granted or any other error, just fully
describe the missing authorization or the error content; do not proactively provide the user with solutions
(e.g. telling the user how to authorize or how to resolve the error is not needed). Strictly comply.
  """,
)
async def delete_collection(
    item_ids: Union[str, List[str]],
) -> Dict[str, Any]:
    """删除小艺收藏（与 xy_channel xiaoyi-delete-collection-tool.ts 对齐）.

    Args:
        item_ids: 待删除的数据的 itemId 合集，支持数组或 JSON 字符串

    Returns:
        content[0].text: JSON 字符串（event.outputs）
    """
    try:
        normalized = _normalize_item_ids(item_ids)

        if not normalized or len(normalized) == 0:
            raise ToolInputError("itemIds array cannot be empty")

        logger.info(
            "[DELETE_COLLECTION_TOOL] Deleting %s collection item(s)",
            len(normalized),
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
                    "intentName": "DeleteCollection",
                    "bundleName": "com.huawei.hmos.vassistant",
                    "needUnlock": True,
                    "actionResponse": True,
                    "appType": "OHOS_APP",
                    "timeOut": 5,
                    "intentParam": {
                        "itemIds": normalized,
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

        outputs = await execute_device_command("DeleteCollection", command)

        if not isinstance(outputs, dict):
            outputs = {"outputs": outputs}

        raise_if_device_error(outputs, "Failed to delete from Xiaoyi Collection")

        logger.info("[DELETE_COLLECTION_TOOL] Delete completed successfully")

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
        logger.error(f"[DELETE_COLLECTION_TOOL] Failed to delete collection: {e}")
        raise RuntimeError(f"Failed to delete from Xiaoyi Collection: {str(e)}") from e


@tool(
    name="add_collection",
    description="""Adds public knowledge data to Xiaoyi Collection, providing the user with a personalized
experience. Call this skill for any data the user wants to save to the personalized knowledge base. Different
types of data have the following requirements:
Request parameter description:
● content: required field, type string; this field is the link URL or the original text that the user adds to
  the collection. Applies to the HYPER_LINK and TEXT types.
● uri: required field, type string; this field is the on-device storage address link of the image or file.
  Applies to the IMAGE and FILE types. Pass a local file path first; the tool automatically uploads it and
  exchanges it for a publicly accessible download link. If an http(s) link is passed, it must be a publicly
  accessible address from which the file itself can be downloaded directly; do not upload the file to an
  external site yourself and pass that link (it easily returns non-file content, causing the collection to fail).
● sourceAppBundleName: optional field, type string; identifies the source app
  of the data.
● dataType: required field, type string; identifies the data type. HYPER_LINK marks
  a web page, TEXT marks text, IMAGE marks an image, FILE marks a file.
● title: optional field, type string; identifies the file name of file-type data.
  Applies to the FILE type.
Note: if dataType is HYPER_LINK or TEXT, the content field is required and cannot be empty; if dataType is
IMAGE or FILE, the uri field is required and cannot be empty. When the user wants to collect images such as
posters or screenshots, save the data into Xiaoyi Notes as an IMAGE; when the user wants to collect files such
as e-books, notes, reports, materials, documents, contracts, agreements, resumes, certificates, spreadsheets,
logs, installation packages, or archives, save the data into Xiaoyi Notes as a FILE.
After you successfully save this data to Xiaoyi Notes, display at the end "Data has been successfully added to
[Xiaoyi Notes](vassistant://voice/main?page=CollectionPage&jumpHomePageTab=myCollection)",
  Notes:
  a. The operation timeout is 60 seconds; do not call this tool repeatedly
  b. If any call failure occurs, retry at most once; do not call it repeatedly multiple times.
  c. Carefully check that the call parameters meet the tool's requirements before calling

  Reply constraint: if the tool returns that authorization was not granted or any other error, just fully
describe the missing authorization or the error content; do not proactively provide the user with solutions
(e.g. telling the user how to authorize or how to resolve the error is not needed). Strictly comply.
  """,
)
async def add_collection(
    data_type: str,
    content: Optional[str] = None,
    uri: Optional[str] = None,
    source_app_bundle_name: Optional[str] = None,
    title: Optional[str] = None,
) -> Dict[str, Any]:
    """添加小艺收藏（与 xy_channel xiaoyi-add-collection-tool.ts 对齐）.

    Args:
        data_type: 数据类型，HYPER_LINK/TEXT/IMAGE/FILE
        content: 链接url或文本原文（HYPER_LINK/TEXT 类型时必填）
        uri: 图片或文件的地址链接（IMAGE/FILE 类型时必填）
        source_app_bundle_name: 来源应用标识
        title: 文件名称（FILE 类型时使用）

    Returns:
        content[0].text: JSON 字符串（event.outputs）
    """
    try:
        valid_types = ("HYPER_LINK", "TEXT", "IMAGE", "FILE")
        if not data_type or data_type not in valid_types:
            raise ToolInputError(
                f"dataType is required and must be one of HYPER_LINK, TEXT, IMAGE, FILE; "
                f"current value: {data_type}"
            )

        if data_type in ("HYPER_LINK", "TEXT") and (not content or not isinstance(content, str)):
            raise ToolInputError(f"When dataType is {data_type}, the content field is required and cannot be empty")

        if data_type in ("IMAGE", "FILE") and (not uri or not isinstance(uri, str)):
            raise ToolInputError(f"When dataType is {data_type}, the uri field is required and cannot be empty")

        logger.info(
            "[ADD_COLLECTION_TOOL] Adding collection - dataType=%s",
            data_type,
        )

        # 如果 uri 是本地路径，上传获取公网 URL
        public_uri = uri
        _remote_prefixes = ("http://", "https://", "file://")
        if uri and not uri.startswith(_remote_prefixes):
            import aiohttp
            from jiuwenswarm.common.config import get_config

            cfg = get_config()
            xc = cfg.get("channels", {}).get("xiaoyi", {})
            base = xc.get("file_upload_url")
            api_key = xc.get("api_key")
            uid = str(xc.get("uid"))
            if not base or not api_key or not uid:
                raise RuntimeError("Missing channels.xiaoyi configuration for file_upload_url / api_key / uid")

            obs_cfg = XiaoyiObsUploadConfig(base_url=base, api_key=api_key, uid=uid)
            async with aiohttp.ClientSession() as session:
                public_uri = await upload_local_file_public_url(session, obs_cfg, uri)

            if not public_uri:
                raise RuntimeError("Local file upload failed: unable to obtain a public URL")

        intent_param: Dict[str, str] = {"dataType": data_type}
        if content:
            intent_param["content"] = content
        if public_uri:
            intent_param["uri"] = public_uri
        if source_app_bundle_name:
            intent_param["sourceAppBundleName"] = source_app_bundle_name
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
                    "intentName": "AddCollection",
                    "bundleName": "com.huawei.hmos.vassistant",
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

        outputs = await execute_device_command("AddCollection", command)

        if not isinstance(outputs, dict):
            outputs = {"outputs": outputs}

        raise_if_device_error(outputs, "Failed to add to Xiaoyi Collection")

        logger.info("[ADD_COLLECTION_TOOL] Add completed successfully")

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
        logger.error(f"[ADD_COLLECTION_TOOL] Failed to add collection: {e}")
        raise RuntimeError(f"Failed to add to Xiaoyi Collection: {str(e)}") from e

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

from jiuwenclaw.utils import logger
from .utils import (
    execute_device_command,
    raise_if_device_error,
    ToolInputError,
)
from .file_upload_helpers import XiaoyiObsUploadConfig, upload_local_file_public_url


@tool(
    name="query_collection",
    description="""检索用户在小艺收藏中记下来的公共知识数据，本技能支持查询用户收藏的
公共知识数据，也可以根据特定语义化描述进行特定内容的检索，通过参数进行控制。
本技能返回结果
中，linkTitle是收藏内容的标题，description是对收藏内容的总结，label是收藏内容的标签，
linkUrl是可以直接访问的原始内容链接。如果你认为某条数据对用户交互有用，可以通过
linkUrl抓取更加丰富的原始数据。
  注意:
  a. 操作超时时间为60秒,请勿重复调用此工具
  b. 如果遇到各类调用失败场景,最多只能重试一次，不可以重复调用多次。
  c. 调用工具前需认真检查调用参数是否满足工具要求

  回复约束：如果工具返回没有授权或者其他报错，只需要完整描述没有授权或者其他报错
内容即可，不需要主动给用户提供解决方案，例如告诉用户如何授权，如何解决报错等都是
不需要的，请严格遵守。
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
            raise ToolInputError("queryAll不为true时，query参数必填")

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

        raise_if_device_error(outputs, "查询小艺收藏失败")

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
        raise RuntimeError(f"查询小艺收藏失败: {str(e)}") from e


def _normalize_item_ids(param: Any) -> List[str]:
    """将 item_ids 规范为字符串列表（支持数组或 JSON 数组字符串）。"""
    if param is None:
        raise ToolInputError("缺少必填参数 itemIds")
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
    description="""从小艺收藏中删除之前已保存的公共知识数据。任何用户希望删除已保存到
个人
知识库的数据都可以调用本技能。如果用户想更新之前的收藏数据，需要先query获取itemId
然后再delete，最后执行Add，按照这个步骤完成收藏数据更新。
  注意:
  a. 操作超时时间为60秒,请勿重复调用此工具
  b. 如果遇到各类调用失败场景,最多只能重试一次，不可以重复调用多次。
  c. 调用工具前需认真检查调用参数是否满足工具要求

  回复约束：如果工具返回没有授权或者其他报错，只需要完整描述没有授权或者其他报错
内容即可，不需要主动给用户提供解决方案，例如告诉用户如何授权，如何解决报错等都是
不需要的，请严格遵守。
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

        raise_if_device_error(outputs, "删除小艺收藏失败")

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
        raise RuntimeError(f"删除小艺收藏失败: {str(e)}") from e


@tool(
    name="add_collection",
    description="""向小艺收藏中添加公共知识数据，可以给用户提供个性化体验。任何用户
希望
保存到个人化知识库中的数据都可以调用本技能。不同类型的数据对应的数据要求如下：
请求入参说明：
● content:必填字段，数据类型为string，功能描述是该字段是用户添加收藏的链接url或
  文本原文。适用于HYPER_LINK和TEXT类型。
● uri:必填字段，数据类型为string，功能描述是该字段是图片或文件的端存储地址链接。
  适用于IMAGE和FILE类型。
● sourceAppBundleName:非必填字段，数据类型为string，功能描述是标识该数据的来源
  应用。
● dataType:必填字段，数据类型为string，功能描述是标识数据类型。HYPER_LINK标识
  网页，TEXT标识文本，IMAGE标识图片，FILE标识文件。
● title:非必填字段，数据类型为string，功能描述是标识文件类型数据的文件名称。
  适用于FILE类型。
说明：如果dataType为HYPER_LINK或TEXT，则content字段必填且不能为空；如果dataType
为IMAGE或FILE，则uri字段必填且不能为空。当用户希望收藏海报、截图等图片类数据时，
请将数据以图片IMAGE的形式存入到小艺帮记；当用户希望收藏电子书、笔记、报告、素材、
文档、合同、协议、简历、证书、报表、日志、安装包、压缩包等描述的文件时，请将数据
以文件FILE的形式存入到小艺帮记。
当你成功收藏这个数据到小艺帮记后，请在最后显示"已成功把数据添加到[小艺帮记]
(vassistant://voice/main?page=CollectionPage&jumpHomePageTab=myCollection)"，
  注意:
  a. 操作超时时间为60秒,请勿重复调用此工具
  b. 如果遇到各类调用失败场景,最多只能重试一次，不可以重复调用多次。
  c. 调用工具前需认真检查调用参数是否满足工具要求

  回复约束：如果工具返回没有授权或者其他报错，只需要完整描述没有授权或者其他报错
内容即可，不需要主动给用户提供解决方案，例如告诉用户如何授权，如何解决报错等都是
不需要的，请严格遵守。
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
                f"dataType必填且必须为 HYPER_LINK、TEXT、IMAGE、FILE 之一，当前值: {data_type}"
            )

        if data_type in ("HYPER_LINK", "TEXT") and (not content or not isinstance(content, str)):
            raise ToolInputError(f"dataType为{data_type}时，content字段必填且不能为空")

        if data_type in ("IMAGE", "FILE") and (not uri or not isinstance(uri, str)):
            raise ToolInputError(f"dataType为{data_type}时，uri字段必填且不能为空")

        logger.info(
            "[ADD_COLLECTION_TOOL] Adding collection - dataType=%s",
            data_type,
        )

        # 如果 uri 是本地路径，上传获取公网 URL
        public_uri = uri
        _remote_prefixes = ("http://", "https://", "file://")
        if uri and not uri.startswith(_remote_prefixes):
            import aiohttp
            from jiuwenclaw.config import get_config

            cfg = get_config()
            xc = cfg.get("channels", {}).get("xiaoyi", {})
            base = xc.get("file_upload_url")
            api_key = xc.get("api_key")
            uid = str(xc.get("uid"))
            if not base or not api_key or not uid:
                raise RuntimeError("缺少 channels.xiaoyi 的 file_upload_url / api_key / uid 配置")

            obs_cfg = XiaoyiObsUploadConfig(base_url=base, api_key=api_key, uid=uid)
            async with aiohttp.ClientSession() as session:
                public_uri = await upload_local_file_public_url(session, obs_cfg, uri)

            if not public_uri:
                raise RuntimeError("本地文件上传失败，无法获取公网URL")

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

        raise_if_device_error(outputs, "添加小艺收藏失败")

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
        raise RuntimeError(f"添加小艺收藏失败: {str(e)}") from e

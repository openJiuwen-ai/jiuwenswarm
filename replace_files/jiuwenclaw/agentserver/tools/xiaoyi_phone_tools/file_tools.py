# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""File tools - 文件工具.

包含：
- search_file: 搜索手机文件
- upload_file: 上传手机文件获取公网 URL
- send_file_to_user: 将本地文件或公网文件传到用户手机
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
import uuid
from typing import Any, Dict, List, Optional, Union
from urllib.parse import urlparse

import aiohttp

from openjiuwen.core.foundation.tool import tool

from jiuwenclaw.utils import logger
from .utils import (
    ToolInputError,
    execute_device_command,
    format_success_response,
    raise_if_device_error,
)


def _normalize_file_infos(param: Any) -> List[Dict[str, Any]]:
    """将 fileInfos 规范为数组（支持数组或 JSON 数组字符串）。"""
    if param is None:
        raise ToolInputError("缺少必填参数 fileInfos")
    if isinstance(param, list):
        return param
    if isinstance(param, str):
        try:
            parsed = json.loads(param)
        except json.JSONDecodeError as e:
            raise ToolInputError(
                f"fileInfos 必须是合法 JSON 数组字符串。解析错误: {e}"
            ) from e
        if not isinstance(parsed, list):
            raise ToolInputError(
                "fileInfos 必须是数组或表示数组的 JSON 字符串（解析结果不是数组）"
            )
        return parsed
    raise ToolInputError(
        f"fileInfos 必须是数组或 JSON 数组字符串，当前类型: {type(param).__name__}"
    )


@tool(
    name="search_file",
    description="""搜索手机文件系统的文件。

【重要】使用约束：此工具仅在用户显著说明要从手机搜索时才执行，例如：
- "从我手机里面搜索xxxx"
- "从手机文件系统找一下xxxx"
- "在手机上查找文件xxxx"
- "搜索手机里的文件"

如果用户没有明确说明从手机搜索（如仅说"搜索文件"、"找一下xxxx"），应默认从 openclaw 本地的文件系统查询，不要调用此工具。

功能说明：根据关键词搜索文件名称或内容，返回匹配的文件列表（包括文件名、路径、大小、修改时间等信息）。

注意事项：操作超时时间为60秒，请勿重复调用此工具，如果超时或失败，最多重试一次。""",
)
async def search_file(
    query: str,
) -> Dict[str, Any]:
    """搜索文件.

    Args:
        query: 搜索关键词，用于匹配文件名称、后缀名或文件内容

    Returns:
        设备返回的完整 outputs（JSON 序列化后置于 content）
    """
    try:
        logger.info(f"[SEARCH_FILE_TOOL] Searching files - query: {query}")

        if not query or not isinstance(query, str):
            raise ToolInputError("缺少必填参数 query（搜索关键词）")

        query = query.strip()
        if not query:
            raise ToolInputError("query 不能为空")

        command = {
            "header": {
                "namespace": "Common",
                "name": "Action",
            },
            "payload": {
                "cardParam": {},
                "executeParam": {
                    "executeMode": "background",
                    "intentName": "SearchFile",
                    "bundleName": "com.huawei.hmos.aidispatchservice",
                    "needUnlock": True,
                    "actionResponse": True,
                    "appType": "OHOS_APP",
                    "timeOut": 5,
                    "intentParam": {
                        "query": query,
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

        # 成功时返回完整 outputs，不在此处按 code 拦截
        outputs = await execute_device_command("SearchFile", command)

        if not isinstance(outputs, dict):
            outputs = {"outputs": outputs}

        raise_if_device_error(outputs, "搜索文件失败")

        result = outputs.get("result")
        if not isinstance(result, dict):
            result = {}
        n = len(result.get("items", []))
        logger.info(f"[SEARCH_FILE_TOOL] Found {n} files")

        return format_success_response(dict(outputs), f"搜索到 {n} 个文件")

    except ToolInputError:
        raise
    except Exception as e:
        logger.error(f"[SEARCH_FILE_TOOL] Failed to search files: {e}")
        raise RuntimeError(f"搜索文件失败: {str(e)}") from e


@tool(
    name="upload_file",
    description="""工具能力描述：将手机本地文件上传并获取可公网访问的 URL。

  前置工具调用：此工具使用前必须先调用 search_file 或者 query_collection 工具获取文件的 uri

  工具参数说明：
  a. 入参中的file_Infos数组，每个元素必须包含mediaUri字段（对应于search_file工具或者query_collection返回结果中的uri），必须与search_file结果中对应的uri完全保持一致，不要自行修改。
  b. file_infos 中的timeout字段是可选的，表示上传文件超时时间，单位是毫秒，默认是20000（20秒）。
  c. file_infos 是文件在手机本地的信息数组（从 search_file 工具或者 query_collection 响应中获取）。限制：每次最多支持传入 5 条文件信息。

  注意事项：
  a. 操作超时时间为60秒,请勿重复调用此工具,如果超时或失败,最多重试一次。
  b. 此工具返回的文件链接为用户公网可访问的链接，如果需要对文件进行额外的操作，需要先根据返回的url下载文件，然后进行下一步处理。""",
)
async def upload_file(file_infos: Union[str, List[Dict[str, Any]]]) -> Dict[str, Any]:
    """上传文件

    Args:
        file_infos: 文件信息数组或 JSON 数组字符串；每项含 mediaUri（必需）、timeout（可选，默认 20000 毫秒）

    Returns:
        content[0].text 为 JSON：fileUrls、count、message
    """
    try:
        file_infos_list = _normalize_file_infos(file_infos)
        logger.info(
            "[UPLOAD_FILE_TOOL] Uploading files - fileInfos count: %s",
            len(file_infos_list),
        )

        if len(file_infos_list) == 0:
            raise ToolInputError("fileInfos 数组不能为空")

        if len(file_infos_list) > 5:
            raise ToolInputError(
                f"最多支持 5 条文件信息，当前提供了 {len(file_infos_list)} 条。请分批处理。"
            )

        for i, file_info in enumerate(file_infos_list):
            if not isinstance(file_info, dict):
                raise ToolInputError(
                    f"fileInfos[{i}] 必须是包含 mediaUri 的对象"
                )
            if not file_info.get("mediaUri") or not isinstance(
                file_info["mediaUri"], str
            ):
                raise ToolInputError(
                    f"fileInfos[{i}] 必须包含有效的 mediaUri 字符串"
                )
            if not file_info.get("timeout"):
                file_info["timeout"] = "20000"

        command = {
            "header": {
                "namespace": "Common",
                "name": "Action",
            },
            "payload": {
                "cardParam": {},
                "executeParam": {
                    "executeMode": "background",
                    "intentName": "FileUploadForClaw",
                    "bundleName": "com.huawei.hmos.vassistant",
                    "needUnlock": True,
                    "actionResponse": True,
                    "appType": "OHOS_APP",
                    "timeOut": 5,
                    "intentParam": {"fileInfos": file_infos_list},
                    "permissionId": [],
                    "achieveType": "INTENT",
                },
                "responses": [{"resultCode": "", "displayText": "", "ttsText": ""}],
                "needUploadResult": True,
                "noHalfPage": False,
                "pageControlRelated": False,
            },
        }

        outputs = await execute_device_command("FileUploadForClaw", command)

        if not isinstance(outputs, dict):
            outputs = {"outputs": outputs}

        raise_if_device_error(outputs, "获取文件 URL 失败")

        result = outputs.get("result", {}) if isinstance(outputs, dict) else {}
        file_urls: List[Any] = []
        if isinstance(result, dict):
            raw = result.get("fileUrls")
            if isinstance(raw, list):
                file_urls = raw

        decoded_urls: List[str] = []
        for url in file_urls:
            if not isinstance(url, str):
                logger.warning(
                    "[UPLOAD_FILE_TOOL] URL 不是字符串: %s",
                    type(url),
                )
                continue
            decoded = url.replace("\\u003d", "=").replace("\\u0026", "&")
            if decoded:
                decoded_urls.append(decoded)

        logger.info(
            "[UPLOAD_FILE_TOOL] Retrieved %s file URLs",
            len(decoded_urls),
        )

        # 与 upload-file-tool.ts 一致：content[0].text 仅为 { fileUrls, count, message }
        payload = {
            "fileUrls": decoded_urls,
            "count": len(decoded_urls),
            "message": f"成功获取 {len(decoded_urls)} 个文件的公网访问 URL",
        }
        return {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(payload, ensure_ascii=False),
                }
            ]
        }

    except ToolInputError:
        raise
    except Exception as e:
        logger.error(f"[UPLOAD_FILE_TOOL] Failed to upload files: {e}")
        raise RuntimeError(f"上传文件失败: {str(e)}") from e


# ---------------------------------------------------------------------------
# send_file_to_user - 将本地文件或公网文件传到用户手机
# ---------------------------------------------------------------------------

_FILE_TYPE_TO_MIME_TYPE: Dict[str, str] = {
    "txt": "text/plain",
    "html": "text/html",
    "css": "text/css",
    "js": "application/javascript",
    "json": "application/json",
    "png": "image/png",
    "jpeg": "image/jpeg",
    "jpg": "image/jpeg",
    "gif": "image/gif",
    "svg": "image/svg+xml",
    "pdf": "application/pdf",
    "zip": "application/zip",
    "doc": "application/msword",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xls": "application/vnd.ms-excel",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "ppt": "application/vnd.ms-powerpoint",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "mp3": "audio/mpeg",
    "mp4": "video/mp4",
}


def _get_mime_type(filename: str) -> str:
    """根据文件扩展名获取 MIME 类型."""
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    return _FILE_TYPE_TO_MIME_TYPE.get(ext, "text/plain")


async def _download_remote_file(url: str) -> str:
    """下载远程文件到临时文件，返回本地路径.

    Raises:
        RuntimeError: 下载失败
    """
    async with aiohttp.ClientSession() as session:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=120)) as resp:
            if not resp.ok:
                raise RuntimeError(f"HTTP {resp.status}: {resp.reason}")
            data = await resp.read()

    # 从 URL 提取文件名
    parsed = urlparse(url)
    raw_name = os.path.basename(parsed.path) or "downloaded_file"
    raw_name = raw_name.split("?")[0]

    suffix = os.path.splitext(raw_name)[1] or ""
    base_name = os.path.splitext(raw_name)[0] or "downloaded_file"
    unique_name = f"{base_name}_{int(time.time())}{suffix}"

    tmp_dir = tempfile.gettempdir()
    local_path = os.path.join(tmp_dir, unique_name)

    with open(local_path, "wb") as f:
        f.write(data)

    logger.info("[SEND_FILE_TO_USER] Downloaded remote file: %s -> %s", url, local_path)
    return local_path

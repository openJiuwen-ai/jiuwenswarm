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

from jiuwenswarm.common.utils import logger
from .utils import (
    ToolInputError,
    execute_device_command,
    format_success_response,
    raise_if_device_error,
)


def _normalize_file_infos(param: Any) -> List[Dict[str, Any]]:
    """将 fileInfos 规范为数组（支持数组或 JSON 数组字符串）。"""
    if param is None:
        raise ToolInputError("Missing required parameter: fileInfos")
    if isinstance(param, list):
        return param
    if isinstance(param, str):
        try:
            parsed = json.loads(param)
        except json.JSONDecodeError as e:
            raise ToolInputError(
                f"fileInfos must be a valid JSON array string. Parse error: {e}"
            ) from e
        if not isinstance(parsed, list):
            raise ToolInputError(
                "fileInfos must be an array or a JSON string representing an array (parsed result is not an array)"
            )
        return parsed
    raise ToolInputError(
        f"fileInfos must be an array or a JSON array string; got type: {type(param).__name__}"
    )


@tool(
    name="search_file",
    description="""Search files in the phone's file system.

[IMPORTANT] Usage constraints: run this tool only when the user clearly states to search on the phone, for example:
- "Search for xxxx on my phone"
- "Look for xxxx in the phone's file system"
- "Find file xxxx on the phone"
- "Search files on the phone"

If the user does not explicitly say to search on the phone (e.g. only "search for files" or "find xxxx"), default to querying the local openclaw file system instead; do not call this tool.

Description: search file names or contents by keyword and return the matched file list (including file name, path, size, modification time, etc.).

Note: the operation timeout is 60 seconds. Do not call this tool repeatedly; if it times out or fails, retry at most once.""",
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
            raise ToolInputError("Missing required parameter: query (search keyword)")

        query = query.strip()
        if not query:
            raise ToolInputError("query must not be empty")

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

        raise_if_device_error(outputs, "Failed to search files")

        result = outputs.get("result")
        if not isinstance(result, dict):
            result = {}
        n = len(result.get("items", []))
        logger.info(f"[SEARCH_FILE_TOOL] Found {n} files")

        return format_success_response(dict(outputs), f"Found {n} files")

    except ToolInputError:
        raise
    except Exception as e:
        logger.error(f"[SEARCH_FILE_TOOL] Failed to search files: {e}")
        raise RuntimeError(f"Failed to search files: {str(e)}") from e


@tool(
    name="upload_file",
    description="""Tool capability: Upload local phone files and obtain publicly accessible URLs.

  Prerequisite tool call: before using this tool, you must first call the search_file or query_collection tool to obtain the file's uri

  Parameter description:
  a. Each element of the file_Infos array in the input must contain a mediaUri field (corresponding to the uri in the search_file tool or query_collection results) and must exactly match the corresponding uri in the search_file results; do not modify it yourself.
  b. The timeout field in file_infos is optional; it is the upload timeout in milliseconds, defaulting to 20000 (20 seconds).
  c. file_infos is an array of the files' local information on the phone (obtained from the search_file tool or query_collection response). Limit: at most 5 file entries per call.

  Notes:
  a. The operation timeout is 60 seconds. Do not call this tool repeatedly; if it times out or fails, retry at most once.
  b. The file links returned by this tool are publicly accessible to the user. If further operations on the file are needed, first download the file using the returned url, then proceed with the next step.""",
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
            raise ToolInputError("The fileInfos array must not be empty")

        if len(file_infos_list) > 5:
            raise ToolInputError(
                f"At most 5 file entries are supported; got {len(file_infos_list)}. Please split into batches."
            )

        for i, file_info in enumerate(file_infos_list):
            if not isinstance(file_info, dict):
                raise ToolInputError(
                    f"fileInfos[{i}] must be an object containing mediaUri"
                )
            if not file_info.get("mediaUri") or not isinstance(
                file_info["mediaUri"], str
            ):
                raise ToolInputError(
                    f"fileInfos[{i}] must contain a valid mediaUri string"
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

        raise_if_device_error(outputs, "Failed to get file URLs")

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
            "message": f"Successfully obtained public URLs for {len(decoded_urls)} files",
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
        raise RuntimeError(f"Failed to upload files: {str(e)}") from e


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

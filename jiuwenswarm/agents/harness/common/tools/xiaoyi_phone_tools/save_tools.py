# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Save tools - 保存到手机工具.

包含：
- save_media_to_gallery: 将图片/视频保存到手机图库
- save_file_to_file_manager: 将文件保存到手机文件管理器
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, Optional

import aiohttp

from openjiuwen.core.foundation.tool import tool

from jiuwenswarm.common.utils import logger
from .utils import (
    execute_device_command,
    raise_if_device_error,
    ToolInputError,
)
from .file_upload_helpers import XiaoyiObsUploadConfig, upload_local_file_public_url


async def _ensure_public_url(
    url: str,
    obs_cfg: XiaoyiObsUploadConfig,
    session: aiohttp.ClientSession,
) -> str:
    """如果 url 是本地路径，上传获取公网 URL；否则直接返回."""
    if url.startswith("http://") or url.startswith("https://"):
        return url
    public_url = await upload_local_file_public_url(session, obs_cfg, url)
    if not public_url:
        raise RuntimeError("Local file upload failed; unable to obtain a public URL")
    return public_url


def _get_obs_config() -> XiaoyiObsUploadConfig:
    """从配置中读取 OBS 上传配置."""
    from jiuwenswarm.common.config import get_config

    cfg = get_config()
    xc = cfg.get("channels", {}).get("xiaoyi", {})
    base = xc.get("file_upload_url")
    api_key = xc.get("api_key")
    uid = str(xc.get("uid"))
    if not base or not api_key or not uid:
        raise ToolInputError(
            "Missing channels.xiaoyi file_upload_url / api_key / uid configuration; unable to upload files"
        )
    return XiaoyiObsUploadConfig(base_url=base, api_key=api_key, uid=uid)


@tool(
    name="save_media_to_gallery",
    description="""Save image or video files to the phone's photo gallery.
  Parameter description:
  a. mediaType: optional, string. Defaults to pic on the device side when omitted. Supports pic (image) or video.
  b. fileName: optional, string, the file name. When omitted, the phone side generates a random uuid by default.
  c. url: required, string. Supports a local path or a public URL path. If a local path is given, it is uploaded first to obtain a public URL and then saved to the gallery.

  Note:
  a. The operation timeout is 60 seconds. Do not call this tool repeatedly.
  b. If any call failure occurs, retry at most once; do not call it repeatedly.
  c. Before calling the tool, carefully check that the call parameters meet the tool's requirements.

  Reply constraint: if the tool returns a missing-authorization error or any other error, just fully describe the missing authorization or other error content. Do not proactively offer the user solutions; telling the user how to authorize or how to resolve the error is not needed. Strictly comply.
  """,
)
async def save_media_to_gallery(
    url: str,
    media_type: Optional[str] = None,
    file_name: Optional[str] = None,
) -> Dict[str, Any]:
    """保存图片/视频到手机图库（与 xy_channel save-media-to-gallery-tool.ts 对齐）.

    Args:
        url: 本地路径或公网 URL（必填）
        media_type: pic 或 video（可选，默认 pic）
        file_name: 文件名称（可选，自动去除后缀）

    Returns:
        content[0].text: JSON 字符串（event.outputs）
    """
    try:
        if not url or not isinstance(url, str):
            raise ToolInputError("Missing required parameter: url")

        if media_type and media_type not in ("pic", "video"):
            raise ToolInputError(
                f"mediaType only supports pic or video; got: {media_type}"
            )

        # 去除 fileName 后缀
        sanitized_name = file_name
        if sanitized_name and isinstance(sanitized_name, str):
            last_dot = sanitized_name.rfind(".")
            if last_dot > 0:
                sanitized_name = sanitized_name[:last_dot]

        obs_cfg = _get_obs_config()

        async with aiohttp.ClientSession() as session:
            public_url = await _ensure_public_url(url, obs_cfg, session)

        intent_param: Dict[str, str] = {"url": public_url}
        if media_type:
            intent_param["mediaType"] = media_type
        if sanitized_name:
            intent_param["fileName"] = sanitized_name

        logger.info(
            "[SAVE_MEDIA_TO_GALLERY_TOOL] Saving media - type=%s, url=%s",
            media_type or "pic",
            public_url[:100] + "..." if len(public_url) > 100 else public_url,
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
                    "intentName": "SaveMediaToGallery",
                    "bundleName": "com.huawei.hmos.vassistant",
                    "dimension": "",
                    "needUnlock": True,
                    "actionResponse": True,
                    "appType": "OHOS_APP",
                    "timeOut": 5,
                    "intentParam": intent_param,
                    "permissionId": ["ohos.permission.WRITE_IMAGEVIDEO"],
                    "achieveType": "INTENT",
                },
                "responses": [{"resultCode": "", "displayText": "", "ttsText": ""}],
                "needUploadResult": True,
                "noHalfPage": False,
                "pageControlRelated": False,
            },
        }

        outputs = await execute_device_command("SaveMediaToGallery", command)

        if not isinstance(outputs, dict):
            outputs = {"outputs": outputs}

        raise_if_device_error(outputs, "Failed to save media to gallery")

        logger.info("[SAVE_MEDIA_TO_GALLERY_TOOL] Save completed successfully")

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
        logger.error(f"[SAVE_MEDIA_TO_GALLERY_TOOL] Failed to save media: {e}")
        raise RuntimeError(f"Failed to save media to gallery: {str(e)}") from e


@tool(
    name="save_file_to_file_manager",
    description="""Save a file to the phone's file manager.
  Parameter description:
  a. fileName: required, string, the file name.
  b. url: required, string. Supports a local path or a public URL path. If a local path is given, it is uploaded first to obtain a public URL and then saved to the phone.
  c. suffix: required, string, the file extension, e.g. ppt, doc, pdf.

  Note:
  a. The operation timeout is 60 seconds. Do not call this tool repeatedly.
  b. If any call failure occurs, do not retry; return the error directly.
  c. Before calling the tool, carefully check that the call parameters meet the tool's requirements.

  Reply constraint: if the tool returns a missing-authorization error or any other error, just fully describe the missing authorization or other error content. Do not proactively offer the user solutions; telling the user how to authorize or how to resolve the error is not needed. Strictly comply.
  """,
)
async def save_file_to_file_manager(
    file_name: str,
    url: str,
    suffix: str,
) -> Dict[str, Any]:
    """保存文件到手机文件管理器（与 xy_channel save-file-to-phone-tool.ts 对齐）.

    Args:
        file_name: 文件名称（必填）
        url: 本地路径或公网 URL（必填）
        suffix: 文件后缀，例如 ppt、doc、pdf（必填）

    Returns:
        content[0].text: JSON 字符串（event.outputs）
    """
    try:
        if not url or not isinstance(url, str):
            raise ToolInputError("Missing required parameter: url")
        if not file_name or not isinstance(file_name, str):
            raise ToolInputError("Missing required parameter: fileName")
        if not suffix or not isinstance(suffix, str):
            raise ToolInputError("Missing required parameter: suffix")

        obs_cfg = _get_obs_config()

        async with aiohttp.ClientSession() as session:
            public_url = await _ensure_public_url(url, obs_cfg, session)

        intent_param: Dict[str, str] = {
            "fileName": file_name,
            "url": public_url,
            "suffix": suffix,
        }

        logger.info(
            "[SAVE_FILE_TO_PHONE_TOOL] Saving file - name=%s, suffix=%s",
            file_name,
            suffix,
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
                    "intentName": "SaveFileToFileManager",
                    "bundleName": "com.huawei.hmos.vassistant",
                    "dimension": "",
                    "needUnlock": True,
                    "actionResponse": True,
                    "appType": "OHOS_APP",
                    "timeOut": 5,
                    "timeout": 55000,
                    "intentParam": intent_param,
                    "permissionId": ["ohos.permission.WRITE_IMAGEVIDEO"],
                    "achieveType": "INTENT",
                },
                "responses": [{"resultCode": "", "displayText": "", "ttsText": ""}],
                "needUploadResult": True,
                "noHalfPage": False,
                "pageControlRelated": False,
            },
        }

        outputs = await execute_device_command("SaveFileToFileManager", command)

        if not isinstance(outputs, dict):
            outputs = {"outputs": outputs}

        raise_if_device_error(outputs, "Failed to save file to phone")

        logger.info("[SAVE_FILE_TO_PHONE_TOOL] Save completed successfully")

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
        logger.error(f"[SAVE_FILE_TO_PHONE_TOOL] Failed to save file: {e}")
        raise RuntimeError(f"Failed to save file to phone: {str(e)}") from e

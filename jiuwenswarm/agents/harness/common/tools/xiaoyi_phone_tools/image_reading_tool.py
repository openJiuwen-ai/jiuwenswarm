# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""image_reading：

依赖配置 channels.xiaoyi：file_upload_url、api_key、uid
"""

from __future__ import annotations

import json
import os
import tempfile
import uuid
from typing import Any, AsyncIterator, Dict, Optional
from urllib.parse import unquote, urlparse

import aiohttp
import httpx

from openjiuwen.core.foundation.tool import tool

from jiuwenswarm.common.local_proxy_auth import with_local_proxy_bearer
from jiuwenswarm.common.np_transport import is_named_pipe_url, named_pipe_transport_for
from jiuwenswarm.common.utils import logger
from jiuwenswarm.server.xiaoyi_invocation import export_current_xiaoyi_trace_headers

from .file_upload_helpers import XiaoyiObsUploadConfig, upload_local_file_public_url
from .utils import ToolInputError


def _is_remote_url(value: str) -> bool:
    try:
        u = urlparse(value.strip())
        return u.scheme in ("http", "https")
    except Exception:
        return False


def _suffix_from_url(url: str) -> str:
    """从 URL 取扩展名，缺省 .jpg。"""
    try:
        path = urlparse(url).path
        name = unquote(path.rsplit("/", 1)[-1] or "downloaded_image")
        name = name.split("?")[0]
        ext = os.path.splitext(name)[1]
        return ext if ext else ".jpg"
    except Exception:
        return ".jpg"


async def _download_remote_to_temp(url: str) -> str:
    """下载远程图片到临时文件；调用方负责删除。"""
    suffix = _suffix_from_url(url)
    async with aiohttp.ClientSession() as session:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=120)) as resp:
            if not resp.ok:
                raise RuntimeError(f"HTTP {resp.status}: {resp.reason}")
            data = await resp.read()

    tmp = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
    path = tmp.name
    try:
        tmp.write(data)
        tmp.flush()
    except Exception:
        tmp.close()
        try:
            os.unlink(path)
        except OSError:
            pass
        raise
    tmp.close()
    return path


async def _extract_caption_from_sse(chunks: AsyncIterator[bytes]) -> str:
    """从 SSE 字节流提取最后一个 streamContent 作为图像理解结果。"""
    last_caption = ""
    buffer = ""
    async for chunk in chunks:
        if not chunk:
            continue
        buffer += chunk.decode("utf-8", errors="replace")
        while "\n" in buffer:
            line, buffer = buffer.split("\n", 1)
            line = line.rstrip("\r")
            if not line or not line.startswith("data:"):
                continue
            data_content = line[5:].strip()
            if not data_content or data_content == "[DONE]":
                continue
            try:
                data_json = json.loads(data_content)
            except json.JSONDecodeError:
                continue
            for info in data_json.get("abilityInfos") or []:
                reply = (info.get("actionExecutorResult") or {}).get("reply") or {}
                si = reply.get("streamInfo") or {}
                sc = si.get("streamContent")
                if sc:
                    last_caption = sc
    return last_caption


async def _call_image_understanding_api(
    image_url: str, text: str, api_key: str, uid: str, file_upload_url: str
) -> str:
    api_url = (
        f"{file_upload_url}/celia-claw/v1/sse-api/skill/execute"
    )
    trace_headers = export_current_xiaoyi_trace_headers()
    trace_id = trace_headers.get("x-hag-trace-id") or str(uuid.uuid4())
    headers = {
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
        "x-hag-trace-id": trace_id,
        "x-api-key": api_key,
        "x-request-from": "openclaw",
        "x-uid": uid,
        "x-skill-id": "xiaoyi_image_comprehension",
        "x-prd-pkg-name": "com.huawei.hag",
    }
    headers.update(trace_headers)
    # 打本地代理（np:// 管道 / loopback 直连形态）补 uploadToken Bearer；
    # 取不到令牌则不带（兼容旧版桌面零鉴权代理）
    headers = with_local_proxy_bearer(headers, api_url)
    payload: Dict[str, Any] = {
        "version": "1.0",
        "session": {
            "isNew": True,
            "sessionId": str(uuid.uuid4()),
            "interactionId": 0,
        },
        "endpoint": {
            "device": {
                "sid": "uuid.uuid4().hex",
                "deviceId": "",
                "prdVer": "99.0.64.303",
                "phoneType": "WLZ-AL10",
                "sysVer": "HarmonyOS_2.0.0",
                "deviceType": 0,
                "timezone": "GMT+08:00",
            },
            "locale": "zh-CN",
            "sysLocale": "zh",
            "countryCode": "CN",
        },
        "utterance": {"type": "text", "original": text},
        "actions": [
            {
                "actionSn": str(uuid.uuid4()),
                "actionExecutorTask": {
                    "pluginId": "",
                    "agentState": "OnShelf",
                    "actionName": "imageUnderStandStream",
                    "content": {"imageUrls": [image_url], "text": text},
                },
            }
        ],
    }

    if is_named_pipe_url(file_upload_url):
        # np:// base（桌面命名管道形态）：SSE 经 httpx + 命名管道 transport 流式读取
        async with httpx.AsyncClient(
            transport=named_pipe_transport_for(file_upload_url),
            timeout=httpx.Timeout(120.0, connect=10.0),
        ) as client:
            async with client.stream(
                "POST", api_url, json=payload, headers=headers
            ) as resp:
                if resp.status_code >= 400:
                    body = (await resp.aread()).decode("utf-8", errors="replace")
                    raise RuntimeError(f"API request failed: {resp.status_code} {body[:500]}")
                last_caption = await _extract_caption_from_sse(resp.aiter_bytes(4096))
    else:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                api_url,
                json=payload,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=120),
            ) as resp:
                if not resp.ok:
                    body = await resp.text()
                    raise RuntimeError(f"API request failed: {resp.status} {body[:500]}")
                last_caption = await _extract_caption_from_sse(resp.content.iter_chunked(4096))
    if not last_caption:
        raise RuntimeError("No caption received from image understanding API")
    return last_caption


def _content_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    """与 TS 一致：content[0].text 为 JSON 字符串。"""
    return {
        "content": [
            {
                "type": "text",
                "text": json.dumps(payload, ensure_ascii=False),
            }
        ]
    }


@tool(
    name="image_reading",
    description="""
Tool usage scenarios:
【Situations where this tool MUST be called】
1. The user message contains a mediaPath field that is not empty (indicating the user sent an image)
2. The user wants to understand the image content or asks what the image is, for example:
   - "What is this?"
   - "What is in the image?"
   - "Take a look at this image for me"
   - "Describe this image"
   - "Analyze this photo"
   - "What does this image mean?"
   - "Recognize the content of the image"
   - Or any question about understanding, recognizing, or analyzing image content

When both conditions above are met, you must call this tool first to perform image understanding.

Tool capability: understands and analyzes images, returning a description of the image content.

Tool parameter description:
a. local_url: local image file path (optional, usually obtained from the mediaPath field of the user message)
b. remote_url: public internet image URL (optional)
c. prompt: the prompt question about the image; defaults to "描述这张图片内容" and can be customized
   based on the user's specific question
d. Either local_url or remote_url must be non-empty; local_url takes priority

Notes:
a. Common image formats are supported (jpg, png, gif, etc.)
b. Remote images are downloaded locally before being processed
c. The operation timeout is 2 minutes (120 seconds)
d. Returns the text description of the image understanding result
""",
)
async def image_reading(
    local_url: Optional[str] = None,
    remote_url: Optional[str] = None,
    prompt: Optional[str] = None,
) -> Dict[str, Any]:
    """图像理解（与 image-reading-tool.ts 对齐）."""
    lu_sel = local_url if isinstance(local_url, str) and local_url else ""
    ru_sel = remote_url if isinstance(remote_url, str) and remote_url else ""
    if not lu_sel and not ru_sel:
        raise ToolInputError("At least one of localUrl or remoteUrl must be provided")

    # 与 TS：params.prompt || "描述这张图片内容"
    text = prompt if isinstance(prompt, str) and prompt else "描述这张图片内容"

    from jiuwenswarm.common.config import get_config

    cfg = get_config()
    xc = cfg.get("channels", {}).get("xiaoyi", {})
    base = xc.get("file_upload_url")
    api_key = xc.get("api_key")
    uid = str(xc.get("uid"))
    if not base or not api_key or not uid:
        raise ToolInputError(
            "Missing channels.xiaoyi configuration for file_upload_url / api_key / uid; "
            "cannot upload the image"
        )

    obs_cfg = XiaoyiObsUploadConfig(base_url=base, api_key=api_key, uid=uid)
    image_input = lu_sel or ru_sel
    image_source = "local" if lu_sel else "remote"

    downloaded: Optional[str] = None
    image_obs_url: Optional[str] = None
    try:
        async with aiohttp.ClientSession() as session:
            # 与 processImageInput：先远程 URL，再本地文件
            if _is_remote_url(image_input):
                logger.info("[IMAGE_READING_TOOL] remote URL, download then upload")
                downloaded = await _download_remote_to_temp(image_input)
                image_obs_url = await upload_local_file_public_url(
                    session, obs_cfg, downloaded
                )
            elif os.path.isfile(image_input):
                logger.info("[IMAGE_READING_TOOL] local file upload")
                image_obs_url = await upload_local_file_public_url(
                    session, obs_cfg, image_input
                )
            else:
                raise RuntimeError(
                    f"Invalid image input: must be a remote URL or local file path, "
                    f"got: {image_input}"
                )

        if not image_obs_url:
            raise RuntimeError("Image upload failed: unable to obtain the image access URL")

        caption = await _call_image_understanding_api(
            image_obs_url, text, api_key, uid, base
        )
        return _content_payload(
            {
                "caption": caption,
                "prompt": text,
                "imageSource": image_source,
                "success": True,
            }
        )
    except ToolInputError:
        raise
    except Exception as e:
        logger.error("[IMAGE_READING_TOOL] execution failed: %s", e)
        msg = str(e) if str(e) else "Image analysis failed"
        return _content_payload(
            {
                "error": msg,
                "prompt": text,
                "imageSource": image_source,
                "success": False,
            }
        )
    finally:
        if downloaded and os.path.isfile(downloaded):
            try:
                os.unlink(downloaded)
            except OSError:
                pass

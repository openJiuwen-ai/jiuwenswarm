# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import logging
import asyncio
import base64
import mimetypes
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from openjiuwen.core.foundation.tool import tool

from jiuwenswarm.common.config import get_config
from jiuwenswarm.common.utils import get_config_file
from jiuwenswarm.agents.harness.common.tools.multimodal_config import apply_video_model_config_from_yaml
from jiuwenswarm.agents.harness.common.tools.ssl_config import get_requests_verify
from jiuwenswarm.common.http_proxy_config import requests_post
from jiuwenswarm.common.local_env_config import get_local_config


logger = logging.getLogger(__name__)
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
_REQUEST_HEADERS = {
    "User-Agent": _USER_AGENT,
    "Content-Type": "application/json",
}

_SUPPORTED_VIDEO_MODEL_ALIASES = {
    "video_understanding",
    "video_tools.py",
    "jiuwenswarm/agentserver/tools/video_tools.py",
}


def _normalize_video_model_selection(value: str) -> str:
    value = (value or "").strip()
    if value.startswith("@"):
        value = value[1:]
    value = value.replace("\\", "/")
    return value.lower()


def _is_video_model_supported(selection: str) -> bool:
    normalized = _normalize_video_model_selection(selection)
    if not normalized:
        return True
    if normalized in _SUPPORTED_VIDEO_MODEL_ALIASES:
        return True
    return any(normalized.endswith(alias) for alias in _SUPPORTED_VIDEO_MODEL_ALIASES)


@dataclass(frozen=True)
class VideoUnderstandingRequest:
    query: str
    video_path: str
    model: str = "glm-4.6v"
    timeout_seconds: int = 120
    max_tokens: int = 2048
    temperature: float = 0.2
    thinking_enabled: bool = False


def _http_post(url: str, **kwargs) -> Any:
    kwargs.setdefault("verify", get_requests_verify())
    return requests_post(url, **kwargs)


def _guess_video_mime(path: str) -> str:
    mime, _ = mimetypes.guess_type(path)
    if mime and mime.startswith("video/"):
        return mime
    ext = Path(path).suffix.lower()
    mapping = {
        ".mp4": "video/mp4", ".mov": "video/quicktime", ".avi": "video/x-msvideo",
        ".mkv": "video/x-matroska", ".webm": "video/webm", ".mpeg": "video/mpeg",
        ".mpg": "video/mpeg", ".m4v": "video/x-m4v",
    }
    return mapping.get(ext, "video/mp4")


def _video_path_to_url(video_path: str) -> str:
    value = (video_path or "").strip()
    if not value:
        raise ValueError("video_path cannot be empty")
    if value.startswith(("http://", "https://")):
        return value
    path = Path(value).expanduser().resolve()
    if not path.exists():
        raise FileNotFoundError(f"video file does not exist: {path}")
    if not path.is_file():
        raise ValueError(f"video_path is not a file: {path}")
    mime = _guess_video_mime(str(path))
    with open(path, "rb") as f:
        encoded = base64.b64encode(f.read()).decode("utf-8")
    return f"data:{mime};base64,{encoded}"


def _extract_answer(data: dict[str, Any]) -> str:
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    first = choices[0]
    if not isinstance(first, dict):
        return ""
    message = first.get("message", {})
    if not isinstance(message, dict):
        return ""
    content = message.get("content", "")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        texts = [str(item.get("text")) for item in content if isinstance(item, dict) and item.get("text")]
        return "\n".join(texts).strip()
    return str(content).strip()


def _normalize_request(inputs: dict[str, Any]) -> VideoUnderstandingRequest:
    query = str(inputs.get("query", "") or "").strip()
    video_path = str(inputs.get("video_path", "") or "").strip()
    default_model = (str(get_local_config("VIDEO_MODEL_NAME", "") or "") or "glm-4.6v").strip() or "glm-4.6v"
    model = str(inputs.get("model", default_model) or default_model).strip()
    timeout_seconds = max(10, min(int(inputs.get("timeout_seconds", 120)), 600))
    max_tokens = max(128, min(int(inputs.get("max_tokens", 2048)), 8192))
    temperature = max(0.0, min(float(inputs.get("temperature", 0.2)), 2.0))
    thinking_enabled = bool(inputs.get("thinking_enabled", False))
    
    if not query:
        raise ValueError("query cannot be empty.")
    if not video_path:
        raise ValueError("video_path cannot be empty.")
    
    return VideoUnderstandingRequest(
        query=query, video_path=video_path, model=model,
        timeout_seconds=timeout_seconds, max_tokens=max_tokens,
        temperature=temperature, thinking_enabled=thinking_enabled,
    )


def _resolve_chat_completions_url(base: str) -> str:
    b = (base or "").strip().rstrip("/")
    if not b:
        return ""
    return b if b.endswith("/chat/completions") else f"{b}/chat/completions"


def _glm_video_understanding_sync(req: VideoUnderstandingRequest) -> str:
    yaml_key = str(get_local_config("VIDEO_API_KEY", "") or "").strip()
    yaml_base = str(get_local_config("VIDEO_API_BASE", "") or "").strip()
    
    if yaml_key and yaml_base:
        api_key = yaml_key
        api_url = _resolve_chat_completions_url(yaml_base)
    elif yaml_key and not yaml_base:
        raise ValueError("VIDEO_API_BASE is required when VIDEO_API_KEY is set.")
    else:
        api_key = str(get_local_config("ZHIPU_API_KEY", "") or "").strip()
        if not api_key:
            raise ValueError(
                f"No video API credentials. Config file: {get_config_file()}\n"
                "Set models.video.model_config with api_key and api_base, or set ZHIPU_API_KEY."
            )
        api_url = str(
            get_local_config(
                "ZHIPU_API_URL",
                "https://open.bigmodel.cn/api/paas/v4/chat/completions",
            )
            or "https://open.bigmodel.cn/api/paas/v4/chat/completions"
        ).strip()
    
    video_url = _video_path_to_url(req.video_path)
    
    payload = {
        "model": req.model,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "video_url", "video_url": {"url": video_url}},
                {"type": "text", "text": req.query},
            ],
        }],
        "stream": False,
        "max_tokens": req.max_tokens,
        "temperature": req.temperature,
    }
    
    if req.thinking_enabled:
        payload["thinking"] = {"type": "enabled"}
    
    headers = {**_REQUEST_HEADERS, "Authorization": f"Bearer {api_key}"}
    response = _http_post(api_url, headers=headers, json=payload, timeout=req.timeout_seconds)
    
    if not response.ok:
        try:
            error_data = response.json()
            error_msg = error_data.get("error", {}).get("message", response.text[:200])
        except Exception:
            error_msg = response.text[:200]
        raise ValueError(f"API error {response.status_code}: {error_msg}")
    
    answer = _extract_answer(response.json())
    return answer if answer else "[ERROR]: GLM returned empty answer."


@tool(
    name="video_understanding",
    description=(
        "分析并理解视频内容。用户提供视频文件路径（如 .mp4、.mov、.avi）"
        "或视频 URL 并询问视频内容时使用，例如描述场景、动作、人物或物体。"
        "输入：query（关于视频的问题）和 video_path（本地文件路径或 HTTP/HTTPS URL）。"
    ),
)
async def video_understanding(inputs: dict[str, Any], **kwargs) -> str:
    _ = kwargs
    try:
        try:
            apply_video_model_config_from_yaml(get_config())
        except Exception as e:
            logger.warning("[video_understanding] refresh config failed: %s", e)
        req = _normalize_request(inputs or {})
        logger.info(
            "[video_understanding] using model: %s (api_base: %s)",
            req.model, 
            str(get_local_config("VIDEO_API_BASE", "") or ""),
        )
        return await asyncio.to_thread(_glm_video_understanding_sync, req)
    except Exception as exc:
        return f"[ERROR]: glm video understanding failed: {exc}"

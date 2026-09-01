# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import logging
import asyncio
import base64
import mimetypes
import os
import random
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from openjiuwen.core.foundation.tool import tool

from jiuwenswarm.common.config import get_config
from jiuwenswarm.common.utils import env_url, get_agent_workspace_dir, get_config_file
from jiuwenswarm.agents.harness.common.tools.multimodal_config import (
    apply_video_gen_model_config_from_yaml,
    apply_video_model_config_from_yaml,
    _get_model_config,
)
from jiuwenswarm.agents.harness.common.tools.ssl_config import get_requests_verify


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


def _http_post(url: str, **kwargs) -> requests.Response:
    kwargs.setdefault("verify", get_requests_verify())
    try:
        return requests.post(url, **kwargs)
    except requests.exceptions.ProxyError:
        with requests.Session() as session:
            session.trust_env = False
            return session.post(url, **kwargs)


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
    default_model = (os.environ.get("VIDEO_MODEL_NAME") or "glm-4.6v").strip() or "glm-4.6v"
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
    yaml_key = os.environ.get("VIDEO_API_KEY", "").strip()
    yaml_base = os.environ.get("VIDEO_API_BASE", "").strip()
    
    if yaml_key and yaml_base:
        api_key = yaml_key
        api_url = _resolve_chat_completions_url(yaml_base)
    elif yaml_key and not yaml_base:
        raise ValueError("VIDEO_API_BASE is required when VIDEO_API_KEY is set.")
    else:
        api_key = os.environ.get("ZHIPU_API_KEY", "").strip()
        if not api_key:
            raise ValueError(
                f"No video API credentials. Config file: {get_config_file()}\n"
                "Set models.video.model_config with api_key and api_base, or set ZHIPU_API_KEY."
            )
        api_url = env_url("ZHIPU_API_URL", "https://open.bigmodel.cn/api/paas/v4/chat/completions")
    
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
        "Analyze and understand video content. "
        "Use this tool when the user provides a video file path (e.g., .mp4, .mov, .avi) "
        "or video URL and asks questions about the video content, such as describing "
        "scenes, actions, people, or objects in the video. "
        "Input: query (question about the video) and video_path (local file path or HTTP/HTTPS URL)."
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
            os.environ.get("VIDEO_API_BASE", "")
        )
        return await asyncio.to_thread(_glm_video_understanding_sync, req)
    except Exception as exc:
        return f"[ERROR]: glm video understanding failed: {exc}"


def _normalize_video_size(size: str | None) -> str | None:
    """Normalize size to DashScope ``W*H`` form (also accepts ``WxH``)."""
    if not size:
        return None
    value = str(size).strip().replace("x", "*").replace("X", "*")
    return value or None


async def _invoke_model_video_generation(
    prompt: str,
    *,
    size: str = "1280*720",
    duration: int = 5,
    resolution: str | None = None,
) -> dict[str, Any]:
    """Generate a video via the same Model client stack as image generation."""
    from openjiuwen.core.foundation.llm import (
        Model,
        ModelClientConfig,
        ModelRequestConfig,
        UserMessage,
    )

    cfg = get_config() or {}
    mc = _get_model_config(cfg, "video_gen")
    image_mc = _get_model_config(cfg, "image_gen")

    api_key = str(
        mc.get("api_key")
        or os.getenv("VIDEO_GEN_API_KEY")
        or image_mc.get("api_key")
        or os.getenv("IMAGE_GEN_API_KEY")
        or os.getenv("API_KEY")
        or ""
    ).strip()
    api_base = str(
        mc.get("api_base")
        or os.getenv("VIDEO_GEN_API_BASE")
        or image_mc.get("api_base")
        or os.getenv("IMAGE_GEN_API_BASE")
        or os.getenv("API_BASE")
        or "https://dashscope.aliyuncs.com/api/v1"
    ).strip()
    if not api_key:
        return {
            "error": (
                "[ERROR]: VIDEO_GEN_API_KEY / IMAGE_GEN_API_KEY / API_KEY "
                "is not configured for video generation."
            )
        }

    model = str(
        mc.get("model_name")
        or mc.get("model")
        or os.getenv("VIDEO_GEN_MODEL_NAME")
        or "wan2.6-t2v"
    ).strip()
    provider = str(
        mc.get("client_provider")
        or mc.get("model_provider")
        or os.getenv("VIDEO_GEN_PROVIDER")
        or image_mc.get("client_provider")
        or image_mc.get("model_provider")
        or os.getenv("IMAGE_GEN_PROVIDER")
        or "DashScope"
    ).strip()

    try:
        model_client_config = ModelClientConfig(
            client_id="video_gen_client",
            client_provider=provider,
            api_key=api_key,
            api_base=api_base,
            verify_ssl=mc.get("verify_ssl", image_mc.get("verify_ssl", True)),
            ssl_cert=mc.get("ssl_cert", image_mc.get("ssl_cert")),
            timeout=mc.get("timeout", image_mc.get("timeout", 1800)),
        )
        model_config = ModelRequestConfig(model=model)
        model_instance = Model(
            model_config=model_config,
            model_client_config=model_client_config,
        )
        messages = [UserMessage(content=prompt)]
        normalized_size = _normalize_video_size(size)

        result = await model_instance.generate_video(
            messages=messages,
            model=model,
            size=normalized_size,
            resolution=resolution,
            duration=duration,
        )

        output_dir = get_agent_workspace_dir()
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        random_suffix = random.randint(1000, 9999)
        output_path = output_dir / f"generated_{timestamp}_{random_suffix}.mp4"

        video_url = getattr(result, "video_url", None)
        video_data = getattr(result, "video_data", None)

        if video_data:
            with open(output_path, "wb") as f:
                f.write(video_data)
            return {
                "video_path": str(output_path.absolute()),
                "revised_prompt": prompt,
            }

        if video_url:
            ua = (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
            )
            response = requests.get(
                video_url,
                headers={"User-Agent": ua},
                verify=get_requests_verify(),
                timeout=300,
            )
            response.raise_for_status()
            with open(output_path, "wb") as f:
                f.write(response.content)
            return {
                "video_path": str(output_path.absolute()),
                "revised_prompt": prompt,
                "original_url": video_url,
            }

        return {"error": "[ERROR]: No valid video data in response"}
    except Exception as ex:
        return {"error": f"[ERROR]: Video generation failed: {ex}"}


@tool(
    name="generate_video",
    description=(
        "Generate a video from a text description using AI video generation models. "
        "Use this tool when the user wants to create a short video / clip / animation "
        "based on a text prompt. Returns the path to the saved generated video file "
        "and automatically delivers it to the user chat."
    ),
)
async def generate_video(
    prompt: str,
    size: str = "1280*720",
    duration: int = 5,
    resolution: str | None = None,
    save_dir: str | None = None,
) -> str:
    """Generate a video from a text description and deliver it via chat.file."""
    try:
        apply_video_gen_model_config_from_yaml(get_config())
    except Exception:
        logger.debug("Failed to apply video_gen model config from yaml", exc_info=True)

    model = (os.environ.get("VIDEO_GEN_MODEL_NAME") or "wan2.6-t2v").strip()
    provider = (os.environ.get("VIDEO_GEN_PROVIDER") or "DashScope").strip()
    logger.info(
        "[generate_video] using model: %s, provider: %s, size: %s, duration: %s",
        model,
        provider,
        size,
        duration,
    )

    try:
        duration_int = int(duration)
    except (TypeError, ValueError):
        duration_int = 5
    duration_int = max(1, min(duration_int, 15))

    result = await _invoke_model_video_generation(
        prompt,
        size=size,
        duration=duration_int,
        resolution=resolution,
    )
    if "error" in result:
        return result["error"]

    video_path = result["video_path"]
    if save_dir:
        save_path = Path(save_dir)
        save_path.mkdir(parents=True, exist_ok=True)
        new_path = save_path / Path(video_path).name
        Path(video_path).rename(new_path)
        video_path = str(new_path.absolute())

    response_parts = [
        "Video generated successfully!",
        f"Saved to: {video_path}",
        f"Prompt: {prompt}",
    ]
    original_url = result.get("original_url", "")
    if original_url:
        response_parts.append(f"Original URL: {original_url}")

    try:
        from jiuwenswarm.agents.harness.common.tools.send_file_to_user import (
            deliver_file_to_user,
        )

        delivery = await deliver_file_to_user(video_path)
        if delivery:
            response_parts.append(f"Delivered to user: {delivery}")
    except Exception as deliver_err:
        logger.warning(
            "[generate_video] auto chat.file delivery failed: %s", deliver_err
        )
        response_parts.append(
            "Note: video was saved but automatic delivery failed; "
            "use send_file_to_user with the saved path if needed."
        )

    return "\n".join(response_parts)

# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import logging
import asyncio
import base64
import mimetypes
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests
from openjiuwen.core.foundation.tool import tool

from jiuwenswarm.common.config import get_config
from jiuwenswarm.common.utils import env_url, get_config_file
from jiuwenswarm.agents.harness.common.tools.multimodal_config import apply_video_model_config_from_yaml
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


_KNOWN_VIDEO_RATIOS: tuple[tuple[int, int], ...] = (
    (21, 9),
    (16, 9),
    (4, 3),
    (1, 1),
    (3, 4),
    (9, 16),
)
_VIDEO_POLL_INTERVAL_SECONDS = 5.0
_VIDEO_POLL_TIMEOUT_SECONDS = 1800.0


def _normalize_video_size(size: str | None) -> str | None:
    """Normalize size to DashScope ``W*H`` form (also accepts ``WxH``)."""
    if not size:
        return None
    value = str(size).strip().replace("x", "*").replace("X", "*")
    return value or None


def _wan_480p_size_for(size: str | None) -> str:
    """Map any size onto a documented Wan 480P W*H."""
    ratio = _size_to_ratio(size, default="16:9")
    if ratio == "9:16":
        return "480*832"
    if ratio == "1:1":
        return "480*480"
    return "832*480"


def _apply_requested_resolution(
    size: str | None, resolution: str | None
) -> tuple[str | None, str | None]:
    """When caller asks 480P, coerce unofficial sizes (e.g. 854*480) to Wan 480P."""
    resol = (resolution or "").strip().upper().replace(" ", "")
    if resol in {"480P", "480"}:
        return _wan_480p_size_for(size), "480P"
    return _normalize_video_size(size), (str(resolution).strip() if resolution else None)


def _parse_video_size(size: str | None) -> tuple[int, int] | None:
    normalized = _normalize_video_size(size)
    if not normalized or "*" not in normalized:
        return None
    try:
        width_s, height_s = normalized.split("*", 1)
        width, height = int(width_s), int(height_s)
    except ValueError:
        return None
    if width <= 0 or height <= 0:
        return None
    return width, height


def _size_to_ratio(size: str | None, *, default: str = "16:9") -> str:
    parsed = _parse_video_size(size)
    if parsed is None:
        return default
    width, height = parsed
    target = width / height
    best = default
    best_err = float("inf")
    for rw, rh in _KNOWN_VIDEO_RATIOS:
        err = abs(target - (rw / rh))
        if err < best_err:
            best_err = err
            best = f"{rw}:{rh}"
    return best


def _size_height(size: str | None) -> int | None:
    parsed = _parse_video_size(size)
    return parsed[1] if parsed else None


def _normalize_minimax_resolution(resolution: str | None, size: str | None) -> str:
    value = (resolution or "").strip().upper().replace(" ", "")
    if value in {"2K", "2k"}:
        return "2K"
    if value in {"768P", "768", "720P", "720"}:
        return "768P"
    if value in {"480P", "480"}:
        # H3 Max supports 480P; H3 callers should prefer 768P.
        return "480P"
    height = _size_height(size)
    if height is not None and height >= 1440:
        return "2K"
    return "768P"


def _normalize_ark_resolution(resolution: str | None, size: str | None) -> str:
    value = (resolution or "").strip().lower().replace(" ", "")
    if value in {"1080p", "1080"}:
        return "1080p"
    if value in {"720p", "720", "768p", "768"}:
        return "720p"
    if value in {"480p", "480"}:
        return "480p"
    if value in {"2k"}:
        return "1080p"
    height = _size_height(size)
    if height is not None:
        if height >= 1080:
            return "1080p"
        if height >= 720:
            return "720p"
        return "480p"
    return "720p"


def _clamp_duration(duration: int, *, minimum: int, maximum: int) -> int:
    return max(minimum, min(int(duration), maximum))


def _resolve_video_gen_backend(
    *,
    provider: str,
    endpoint_profile: str,
    vendor_key: str,
    api_base: str,
    model: str,
) -> str:
    """Pick a model for text-to-video."""
    vendor = (vendor_key or "").strip().lower()
    profile = (endpoint_profile or "").strip().lower().replace("_", "-")
    prov = (provider or "").strip().lower().replace("_", "")
    base = (api_base or "").strip().lower()
    model_l = (model or "").strip().lower()

    # Self-deployed vLLM-Omni: identified only by explicit config identity.
    # The settings UI is a preset dropdown that only ever writes the canonical
    # vendor_key/endpoint_profile "vllm-omni"; a local api_base carries no heuristic.
    if vendor == "vllm-omni" or profile == "vllm-omni":
        return "vllm-omni"

    # Checked before the volcengine model-name heuristic below: OpenRouter's own
    # namespaced model ids (e.g. "bytedance/seedance-2.0-fast") can contain
    # "seedance" and would otherwise false-positive as volcengine, sending an
    # OpenRouter key to Volcengine's real endpoint. An explicit vendor/profile/
    # provider/api_base signal for OpenRouter always wins over that heuristic.
    if (
        vendor == "openrouter"
        or profile == "openrouter"
        or prov == "openrouter"
        or "openrouter.ai" in base
    ):
        return "openrouter"

    if (
        vendor == "minimax"
        or profile == "minimax"
        or prov == "minimax"
        or "minimax" in base
        or model_l.startswith("minimax-h")
    ):
        return "minimax"

    if (
        vendor in {"volcengine", "volc", "ark"}
        or profile in {"volcengine", "ark"}
        or prov == "volcengine"
        or "volces.com" in base
        or "volcengine" in base
        or "seedance" in model_l
        or model_l.startswith("doubao-seedance")
    ):
        return "volcengine"

    if (
        vendor in {"alibaba", "dashscope"}
        or profile == "dashscope"
        or prov == "dashscope"
        or "dashscope" in base
    ):
        return "dashscope"

    return "dashscope"


def _video_gen_is_vllm_omni() -> bool:
    """Whether the configured video_gen vendor is a self-deployed vLLM-Omni."""
    try:
        mc = _get_model_config(get_config() or {}, "video_gen")
    except Exception:
        mc = {}
    vendor = str(mc.get("vendor_key") or os.getenv("VIDEO_GEN_VENDOR_KEY") or "").strip().lower()
    profile = str(
        mc.get("endpoint_profile") or os.getenv("VIDEO_GEN_ENDPOINT_PROFILE") or ""
    ).strip().lower()
    # Canonical preset values only (see _resolve_video_gen_backend).
    return vendor == "vllm-omni" or profile == "vllm-omni"


def _video_api_error_message(response: requests.Response) -> str:
    try:
        data = response.json()
    except Exception:
        return response.text[:300]
    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict):
            msg = err.get("message") or err.get("msg")
            if msg:
                return str(msg)
        for key in ("message", "msg", "detail"):
            if data.get(key):
                return str(data[key])
    return response.text[:300]


def _download_generated_video(video_url: str, prompt: str) -> dict[str, Any]:
    output_dir = get_agent_workspace_dir()
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    random_suffix = random.randint(1000, 9999)
    output_path = output_dir / f"generated_{timestamp}_{random_suffix}.mp4"
    response = _http_get_resilient(
        video_url,
        headers={"User-Agent": _USER_AGENT},
        timeout=300,
    )
    response.raise_for_status()
    if not response.content:
        raise ValueError("video download returned an empty file")
    with open(output_path, "wb") as f:
        f.write(response.content)
    return {
        "video_path": str(output_path.absolute()),
        "revised_prompt": prompt,
        "original_url": video_url,
    }


def _poll_until_video_url(
    *,
    query_url: str,
    headers: dict[str, str],
    extract_status_and_url,
    timeout_seconds: float = _VIDEO_POLL_TIMEOUT_SECONDS,
    interval_seconds: float = _VIDEO_POLL_INTERVAL_SECONDS,
) -> str:
    deadline_ts = time.monotonic() + timeout_seconds
    last_status = "unknown"
    while time.monotonic() < deadline_ts:
        try:
            response = _http_get_resilient(query_url, headers=headers, timeout=60)
        except requests.exceptions.RequestException as exc:
            last_status = f"transport:{type(exc).__name__}"
            logger.warning("video poll transport error, retrying: %s", exc)
            time.sleep(interval_seconds)
            continue
        if not response.ok:
            if response.status_code >= 500 or response.status_code in {408, 429}:
                last_status = f"http {response.status_code}"
                logger.warning(
                    "video poll HTTP %s, retrying until the task finishes",
                    response.status_code,
                )
                time.sleep(interval_seconds)
                continue
            raise ValueError(
                f"poll failed {response.status_code}: {_video_api_error_message(response)}"
            )
        payload = response.json()
        status, video_url, err_msg = extract_status_and_url(payload)
        last_status = status or last_status
        if status in {"succeeded", "success", "completed"}:
            if not video_url:
                raise ValueError("video generation succeeded but no video URL was returned")
            return video_url
        if status in {"failed", "cancelled", "canceled", "expired"}:
            raise ValueError(err_msg or f"video generation {status}")
        time.sleep(interval_seconds)
    raise TimeoutError(f"video generation timed out (last status={last_status})")


def _minimax_api_root(api_base: str) -> str:
    root = (api_base or "").strip().rstrip("/")
    if root.endswith("/v1") or root.endswith("/v2"):
        root = root.rsplit("/", 1)[0]
    return root or "https://api.minimaxi.com"


def _ark_api_root(api_base: str) -> str:
    root = (api_base or "").strip().rstrip("/")
    if not root:
        return "https://ark.cn-beijing.volces.com/api/v3"
    # Accept both /api/v3 and /api/coding/v3 chat bases; video tasks use /api/v3.
    if root.endswith("/api/coding/v3"):
        return root[: -len("/api/coding/v3")] + "/api/v3"
    return root


def _split_reference_media(
    *,
    first_frame: str | None,
    reference_images: list[str] | None,
    force_reference_mode: bool,
) -> tuple[str | None, list[str]]:
    """Character sheets and the scene specs, shared by every video backend."""
    refs = _unique_dashscope_image_urls(reference_images)
    frame = _as_dashscope_media_url(first_frame)
    if force_reference_mode and frame and frame not in refs:
        refs.append(frame)
        frame = None
    if frame and frame in refs:
        frame = None
    return frame, refs


def _video_content_parts(
    prompt: str,
    *,
    first_frame: str | None = None,
    reference_images: list[str] | None = None,
    force_reference_mode: bool = False,
) -> list[dict[str, Any]]:
    """Text plus optional first frame and reference stills (Seedance / MiniMax-H3)."""
    frame, refs = _split_reference_media(
        first_frame=first_frame,
        reference_images=reference_images,
        force_reference_mode=force_reference_mode,
    )
    content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
    if frame:
        content.append(
            {
                "type": "image_url",
                "image_url": {"url": frame},
                "role": "first_frame",
            }
        )
    for url in refs:
        content.append(
            {
                "type": "image_url",
                "image_url": {"url": url},
                "role": "reference_image",
            }
        )
    return content


def _invoke_minimax_video_generation_sync(
    prompt: str,
    *,
    api_key: str,
    api_base: str,
    model: str,
    size: str | None,
    duration: int,
    resolution: str | None,
    first_frame: str | None = None,
    reference_images: list[str] | None = None,
    audio: bool | None = None,
    force_reference_mode: bool = False,
) -> dict[str, Any]:
    """MiniMax V2 async video generation (MiniMax-H3 / MiniMax-H3-Max)."""
    root = _minimax_api_root(api_base)
    create_url = f"{root}/v2/video_generation"
    model_name = (model or "").strip()
    if not model_name:
        raise ValueError("video model is required (configure models.video_gen)")
    is_max = model_name.lower().endswith("-max")
    duration_int = max(5 if is_max else 4, int(duration))
    ratio = _size_to_ratio(size, default="16:9")
    resol = _normalize_minimax_resolution(resolution, size)
    if is_max and resol == "2K":
        resol = "768P"

    frame, _refs = _split_reference_media(
        first_frame=first_frame,
        reference_images=reference_images,
        force_reference_mode=force_reference_mode,
    )
    payload: dict[str, Any] = {
        "model": model_name,
        "content": _video_content_parts(
            prompt,
            first_frame=first_frame,
            reference_images=reference_images,
            force_reference_mode=force_reference_mode,
        ),
        "resolution": resol,
        "duration": duration_int,
        "ratio": ratio,
    }
    if frame:
        payload["first_frame_image"] = frame
    if audio is not None:
        payload["generate_audio"] = bool(audio)
    headers = {**_REQUEST_HEADERS, "Authorization": f"Bearer {api_key}"}
    response = _http_post(create_url, headers=headers, json=payload, timeout=60)
    if not response.ok:
        raise ValueError(
            f"MiniMax create failed {response.status_code}: {_video_api_error_message(response)}"
        )
    body = response.json()
    task_id = str(body.get("task_id") or "").strip()
    if not task_id:
        raise ValueError(f"MiniMax create response missing task_id: {body}")

    query_url = f"{root}/v2/query/video_generation/{task_id}"

    def _extract(data: dict[str, Any]) -> tuple[str, str | None, str | None]:
        task = data.get("task") if isinstance(data.get("task"), dict) else data
        if not isinstance(task, dict):
            return "unknown", None, "invalid MiniMax poll payload"
        status = str(task.get("status") or "").strip().lower()
        content = task.get("content") if isinstance(task.get("content"), dict) else {}
        video_url = str(content.get("url") or "").strip() or None
        err = task.get("error") if isinstance(task.get("error"), dict) else {}
        err_msg = str(err.get("message") or "").strip() or None
        return status, video_url, err_msg

    video_url = _poll_until_video_url(
        query_url=query_url,
        headers=headers,
        extract_status_and_url=_extract,
    )
    return _download_generated_video(video_url, prompt)


def _invoke_volcengine_video_generation_sync(
    prompt: str,
    *,
    api_key: str,
    api_base: str,
    model: str,
    size: str | None,
    duration: int,
    resolution: str | None,
    first_frame: str | None = None,
    reference_images: list[str] | None = None,
    audio: bool | None = None,
    force_reference_mode: bool = False,
) -> dict[str, Any]:
    """火山方舟 Seedance async video generation."""
    root = _ark_api_root(api_base)
    create_url = f"{root}/contents/generations/tasks"
    model_name = (model or "").strip()
    if not model_name:
        raise ValueError("video model is required (configure models.video_gen)")
    duration_int = _clamp_duration(duration, minimum=2, maximum=12)
    ratio = _size_to_ratio(size, default="16:9")
    resol = _normalize_ark_resolution(resolution, size)

    payload: dict[str, Any] = {
        "model": model_name,
        "content": _video_content_parts(
            prompt,
            first_frame=first_frame,
            reference_images=reference_images,
            force_reference_mode=force_reference_mode,
        ),
        "ratio": ratio,
        "duration": duration_int,
        "resolution": resol,
        "watermark": False,
    }
    if audio is not None:
        payload["generate_audio"] = bool(audio)
    headers = {**_REQUEST_HEADERS, "Authorization": f"Bearer {api_key}"}
    response = _http_post(create_url, headers=headers, json=payload, timeout=60)
    if not response.ok:
        raise ValueError(
            f"Volcengine create failed {response.status_code}: {_video_api_error_message(response)}"
        )
    body = response.json()
    task_id = str(body.get("id") or body.get("task_id") or "").strip()
    if not task_id:
        raise ValueError(f"Volcengine create response missing id: {body}")

    query_url = f"{root}/contents/generations/tasks/{task_id}"

    def _extract(data: dict[str, Any]) -> tuple[str, str | None, str | None]:
        status = str(data.get("status") or "").strip().lower()
        content = data.get("content") if isinstance(data.get("content"), dict) else {}
        video_url = str(content.get("video_url") or content.get("url") or "").strip() or None
        err = data.get("error") if isinstance(data.get("error"), dict) else {}
        err_msg = str(err.get("message") or data.get("message") or "").strip() or None
        return status, video_url, err_msg

    video_url = _poll_until_video_url(
        query_url=query_url,
        headers=headers,
        extract_status_and_url=_extract,
    )
    return _download_generated_video(video_url, prompt)


def _normalize_openrouter_resolution(resolution: str | None) -> str | None:
    """OpenRouter's resolution enum is lowercase ("480p"/"720p"/.../"1080p",
    but "1K"/"2K"/"4K" keep their capital K) and case-sensitive. Designer's
    shared 480P cost-lock (lock_clip_480p) hands every backend a DashScope-
    style uppercase "480P" — normalize just the "<digits>P" shape, leave any
    "1K"/"2K"/"4K"-style token untouched.
    """
    value = (resolution or "").strip()
    if not value:
        return None
    if re.fullmatch(r"\d+P", value):
        return value.lower()
    return value


def _invoke_openrouter_video_generation_sync(
    prompt: str,
    *,
    api_key: str,
    api_base: str,
    model: str,
    size: str | None,
    duration: int,
    resolution: str | None,
    first_frame: str | None = None,
    reference_images: list[str] | None = None,
    audio: bool | None = None,
) -> dict[str, Any]:
    """OpenRouter async video generation: POST {api_base}/videos -> poll
    polling_url until status=="completed" -> download unsigned_urls[0] (still
    needs the same bearer auth despite the name). See
    https://openrouter.ai/docs/guides/overview/multimodal/video-generation.
    """
    root = (api_base or "").strip().rstrip("/") or "https://openrouter.ai/api/v1"
    create_url = f"{root}/videos"
    model_name = (model or "").strip()
    if not model_name:
        raise ValueError("video model is required (configure models.video_gen)")

    frame, refs = _split_reference_media(
        first_frame=first_frame,
        reference_images=reference_images,
        force_reference_mode=False,
    )
    payload: dict[str, Any] = {
        "model": model_name,
        "prompt": str(prompt or "").strip(),
        "aspect_ratio": _size_to_ratio(size, default="16:9"),
    }
    if duration:
        payload["duration"] = int(duration)
    normalized_resolution = _normalize_openrouter_resolution(resolution)
    if normalized_resolution:
        payload["resolution"] = normalized_resolution
    # OpenRouter wants each image reference as an object, not a bare URL string
    # (frame_images additionally needs frame_type; input_references does not).
    if frame:
        payload["frame_images"] = [
            {"type": "image_url", "image_url": {"url": frame}, "frame_type": "first_frame"}
        ]
    if refs:
        payload["input_references"] = [
            {"type": "image_url", "image_url": {"url": url}} for url in refs
        ]
    if audio is not None:
        payload["generate_audio"] = bool(audio)

    headers = {**_REQUEST_HEADERS, "Authorization": f"Bearer {api_key}"}
    response = _http_post(create_url, headers=headers, json=payload, timeout=60)
    if not response.ok:
        raise ValueError(
            f"OpenRouter video create failed {response.status_code}: "
            f"{_video_api_error_message(response)}"
        )
    body = response.json()
    job_id = str(body.get("id") or "").strip()
    polling_url = str(body.get("polling_url") or "").strip() or (
        f"{root}/videos/{job_id}" if job_id else ""
    )
    if not polling_url:
        raise ValueError(f"OpenRouter video response missing id/polling_url: {body}")

    def _extract(data: dict[str, Any]) -> tuple[str, str | None, str | None]:
        status = str(data.get("status") or "").strip().lower()
        urls = data.get("unsigned_urls") if isinstance(data.get("unsigned_urls"), list) else []
        video_url = str(urls[0]).strip() if urls else None
        err_msg = str(data.get("error") or "").strip() or None
        return status, video_url, err_msg

    video_url = _poll_until_video_url(
        query_url=polling_url,
        headers=headers,
        extract_status_and_url=_extract,
    )

    output_dir = get_agent_workspace_dir()
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    random_suffix = random.randint(1000, 9999)
    output_path = output_dir / f"generated_{timestamp}_{random_suffix}.mp4"
    dl_response = _http_get_resilient(video_url, headers=headers, timeout=300)
    dl_response.raise_for_status()
    if not dl_response.content:
        raise ValueError("video download returned an empty file")
    with open(output_path, "wb") as f:
        f.write(dl_response.content)
    return {
        "video_path": str(output_path.absolute()),
        "revised_prompt": prompt,
        "original_url": video_url,
    }


_TASK_ID_RE = re.compile(
    r"(?:/tasks/|task_id['\"=: ]+)"
    r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})"
)


def _task_id_from_error(exc: BaseException) -> str:
    """Pull a provider task id out of an SSL/retry error so the file can still be saved."""
    parts: list[str] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        parts.append(str(current))
        current = current.__cause__ or current.__context__
    match = _TASK_ID_RE.search("\n".join(parts))
    return match.group(1) if match else ""


def _dashscope_task_status(payload: dict[str, Any]) -> tuple[str, str | None, str | None]:
    output = payload.get("output") if isinstance(payload.get("output"), dict) else payload
    if not isinstance(output, dict):
        return "unknown", None, "invalid task payload"
    status = str(output.get("task_status") or output.get("status") or "").strip().lower()
    video_url = str(output.get("video_url") or output.get("url") or "").strip() or None
    if not video_url:
        results = output.get("results")
        if isinstance(results, list) and results and isinstance(results[0], dict):
            video_url = str(
                results[0].get("url") or results[0].get("video_url") or ""
            ).strip() or None
    err = str(output.get("message") or output.get("code") or "").strip() or None
    return status, video_url, err


def _recover_provider_task_file(
    *,
    api_base: str,
    api_key: str,
    task_id: str,
    prompt: str,
) -> dict[str, Any]:
    """Poll a task that already exists and save the mp4 once the provider marks it done."""
    root = (api_base or "").strip().rstrip("/") or _CHINA_DASHSCOPE_API_BASE
    query_url = f"{root}/tasks/{task_id}"
    headers = {**_REQUEST_HEADERS, "Authorization": f"Bearer {api_key}"}
    video_url = _poll_until_video_url(
        query_url=query_url,
        headers=headers,
        extract_status_and_url=_dashscope_task_status,
    )
    return _download_generated_video(video_url, prompt)


async def _invoke_dashscope_video_generation(
    prompt: str,
    *,
    api_key: str,
    api_base: str,
    model: str,
    provider: str,
    endpoint_profile: str,
    mc: dict[str, Any],
    size: str | None,
    duration: int,
    resolution: str | None,
    first_frame: str | None = None,
    reference_images: list[str] | None = None,
    reference_file: str | None = None,
    audio: bool | None = None,
    force_reference_mode: bool = False,
) -> dict[str, Any]:
    """Generate a video via DashScope (openjiuwen Model client)."""
    from openjiuwen.core.foundation.llm import (
        Model,
        ModelClientConfig,
        ModelRequestConfig,
        UserMessage,
    )

    client_provider = provider
    profile = endpoint_profile
    # DashScope text-to-video uses OpenAI client_provider + endpoint_profile=dashscope.
    if client_provider in ("DashScope", "dashscope"):
        client_provider = "OpenAI"
        profile = profile or "dashscope"
    if not profile:
        profile = "dashscope"

    _mcc_kwargs: dict[str, Any] = dict(
        client_id="video_gen_client",
        client_provider=client_provider,
        api_key=api_key,
        api_base=api_base,
        verify_ssl=mc.get("verify_ssl", True),
        ssl_cert=mc.get("ssl_cert"),
        timeout=mc.get("timeout", 1800),
        endpoint_profile=profile,
    )
    model_client_config = ModelClientConfig(**_mcc_kwargs)
    model_config = ModelRequestConfig(model=model)
    model_instance = Model(
        model_config=model_config,
        model_client_config=model_client_config,
    )
    # Reference mode: put all images in reference_images for multimodal content too.
    msg_refs = list(reference_images or [])
    msg_ff = first_frame
    if force_reference_mode and first_frame:
        if first_frame not in msg_refs:
            msg_refs.append(first_frame)
        msg_ff = None
    messages = [
        UserMessage(
            content=video_generation_message_content(
                prompt,
                first_frame=msg_ff,
                reference_images=msg_refs or None,
            )
        )
    ]
    video_call = _build_dashscope_video_call(
        model,
        size=size,
        duration=duration,
        resolution=resolution,
        first_frame=first_frame,
        reference_images=reference_images,
        reference_file=reference_file,
        audio=audio,
        force_reference_mode=force_reference_mode,
    )
    media = video_call.get("media") or []
    logger.info(
        "Designer video generation model=%s img_url=%s media=%s reference_urls=%s shot_type=%s api_base=%s",
        video_call.get("model"),
        bool(video_call.get("img_url")),
        [(item.get("type") if isinstance(item, dict) else item) for item in media],
        len(video_call.get("reference_urls") or []),
        video_call.get("shot_type"),
        api_base,
    )

    def _generate_video_blocking() -> Any:
        return asyncio.run(
            model_instance.generate_video(messages=messages, **video_call)
        )

    try:
        result = await asyncio.to_thread(_generate_video_blocking)
    except Exception as exc:
        task_id = _task_id_from_error(exc)
        if not task_id:
            raise
        logger.warning(
            "video status poll failed after task %s was created; downloading the finished file",
            task_id,
        )
        return await asyncio.to_thread(
            _recover_provider_task_file,
            api_base=api_base,
            api_key=api_key,
            task_id=task_id,
            prompt=prompt,
        )

    video_url = getattr(result, "video_url", None)
    video_data = getattr(result, "video_data", None)

    if video_data:
        output_dir = get_agent_workspace_dir()
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        random_suffix = random.randint(1000, 9999)
        output_path = output_dir / f"generated_{timestamp}_{random_suffix}.mp4"
        with open(output_path, "wb") as f:
            f.write(video_data)
        return {
            "video_path": str(output_path.absolute()),
            "revised_prompt": prompt,
        }

    if video_url:
        return await asyncio.to_thread(_download_generated_video, video_url, prompt)

    return {"error": "[ERROR]: No valid video data in response"}


_CHINA_DASHSCOPE_API_BASE = "https://dashscope.aliyuncs.com/api/v1"
_INTL_DASHSCOPE_API_BASE = "https://dashscope-intl.aliyuncs.com/api/v1"


def _local_media_path(path: str) -> Path | None:
    value = str(path or "").strip()
    if not value:
        return None
    if value.startswith("file:"):
        parsed = urlparse(value)
        local = unquote(parsed.path)
        if len(local) >= 3 and local[0] == "/" and local[2] == ":":
            local = local[1:]
        candidate = Path(local)
    else:
        candidate = Path(value).expanduser()
    if not candidate.is_file():
        return None
    return candidate.resolve()


def _as_dashscope_media_url(path: str | None) -> str | None:
    """DashScope video I2V rejects file://; use http(s) or data: Base64."""
    value = str(path or "").strip()
    if not value:
        return None
    if value.startswith(("http://", "https://", "data:")):
        return value
    local = _local_media_path(value)
    if local is None:
        return None
    mime, _ = mimetypes.guess_type(str(local))
    if not mime or not mime.startswith("image/"):
        mime = "image/png"
    encoded = base64.b64encode(local.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def _as_dashscope_file_url(path: str | None) -> str | None:
    """Storyboard markdown is a DashScope ``file`` asset; the SDK uploads local paths."""
    value = str(path or "").strip()
    if not value:
        return None
    if value.startswith(("http://", "https://", "data:")):
        return value
    local = _local_media_path(value)
    if local is None:
        return None
    return str(local)


def _is_wan3_video(model: str) -> bool:
    return "wan3" in (model or "").strip().lower()


def _unique_dashscope_image_urls(paths: list[str] | None) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()
    for item in paths or []:
        url = _as_dashscope_media_url(item)
        if not url or url in seen:
            continue
        seen.add(url)
        urls.append(url)
    return urls


def video_generation_message_content(
    prompt: str,
    *,
    first_frame: str | None = None,
    reference_images: list[str] | None = None,
) -> str | list[dict[str, str]]:
    """Build explicit multimodal video input: reference images, then first frame, then text."""
    urls: list[str] = []
    seen: set[str] = set()

    def add(path: str | None) -> None:
        url = _as_dashscope_media_url(path)
        if not url or url in seen:
            return
        seen.add(url)
        urls.append(url)

    for item in reference_images or []:
        add(item)
    add(first_frame)
    text = str(prompt or "").strip()
    if not urls:
        return text
    content: list[dict[str, str]] = [{"image": url} for url in urls]
    content.append({"text": text})
    return content


def _align_dashscope_video_api_base(api_base: str, api_key: str) -> str:
    """Keep video calls on the same DashScope region as a working image_gen key."""
    base = (api_base or "").strip().strip("'\"").rstrip("/")
    lowered = base.lower()
    if "compatible-mode" in lowered and "dashscope" in lowered:
        base = (
            _INTL_DASHSCOPE_API_BASE
            if "dashscope-intl" in lowered
            else _CHINA_DASHSCOPE_API_BASE
        )
        lowered = base.lower()

    image_base = str(os.getenv("IMAGE_GEN_API_BASE") or "").strip().strip("'\"").lower()
    image_key = str(os.getenv("IMAGE_GEN_API_KEY") or "").strip().strip("'\"")
    same_key = bool(api_key and image_key and api_key == image_key)
    china = "dashscope.aliyuncs.com" in lowered and "dashscope-intl" not in lowered
    intl = "dashscope-intl" in lowered
    image_intl = "dashscope-intl" in image_base
    image_china = "dashscope.aliyuncs.com" in image_base and "dashscope-intl" not in image_base
    if same_key and image_intl and china:
        logger.info("Aligning DashScope video api_base to international host used by image_gen")
        return _INTL_DASHSCOPE_API_BASE
    if same_key and image_china and intl:
        logger.info("Aligning DashScope video api_base to China host used by image_gen")
        return _CHINA_DASHSCOPE_API_BASE
    return base or _CHINA_DASHSCOPE_API_BASE


def _build_dashscope_video_call(
    model: str,
    *,
    size: str | None = "1280*720",
    duration: int = 5,
    resolution: str | None = None,
    first_frame: str | None = None,
    reference_images: list[str] | None = None,
    reference_file: str | None = None,
    audio: bool | None = None,
    force_reference_mode: bool = False,
) -> dict[str, Any]:
    """Map clip inputs onto DashScope media (refs / first frame / text-only).

    Keeps the model id as configured — no task-suffix rewriting. Mode is chosen
    by inputs: reference images → reference call; lone first_frame → img_url;
    neither → text-only.
    """
    refs = _unique_dashscope_image_urls(reference_images)
    img_url = _as_dashscope_media_url(first_frame)
    # Reference-mode clips: fold any accidental first_frame into refs.
    if force_reference_mode and img_url and img_url not in refs:
        refs = [*refs, img_url]
        img_url = None
    extra_refs = [item for item in refs if item != img_url]
    file_url = _as_dashscope_file_url(reference_file)
    chosen = model.strip() or ""
    if not chosen:
        raise ValueError("video model is required (configure models.video_gen)")
    params: dict[str, Any] = {"duration": duration}
    wan3 = _is_wan3_video(chosen)
    locked_size, locked_res = _apply_requested_resolution(size, resolution)
    use_reference_mode = bool(
        force_reference_mode
        or extra_refs
        or (refs and not img_url)
        or (file_url and wan3)
    )
    if force_reference_mode and not refs and not file_url:
        # Nothing to reference — fall through to text-only rather than empty img_url.
        use_reference_mode = False

    if _is_wan3_video(chosen):
        want_audio = False if audio is None else bool(audio)
        if use_reference_mode:
            media: list[dict[str, str]] = [
                {"type": "reference_image", "url": item} for item in refs
            ]
            if img_url and img_url not in refs:
                media.append({"type": "reference_image", "url": img_url})
            if file_url:
                media.append({"type": "file", "url": file_url})
            params["model"] = chosen
            params["media"] = media
            params["size"] = locked_size or "1280*720"
            params["ratio"] = "16:9" if not locked_size else _size_to_ratio(locked_size)
            params["audio"] = want_audio
            return params
        if img_url and not force_reference_mode:
            params["model"] = chosen
            params["img_url"] = img_url
            params["resolution"] = (locked_res or resolution or "720P").strip() or "720P"
            params["audio"] = want_audio
            return params
        params["model"] = chosen
        params["size"] = locked_size or "1280*720"
        params["audio"] = want_audio
        return params

    if use_reference_mode:
        all_refs = list(refs)
        if img_url and img_url not in all_refs:
            all_refs.append(img_url)
        params["model"] = chosen
        params["reference_urls"] = all_refs[:5]
        params["size"] = locked_size or "1280*720"
        if any(token in chosen for token in ("2.2", "2.5", "2.6", "2.7")):
            params["shot_type"] = "multi"
        return params
    if img_url:
        params["model"] = chosen
        params["img_url"] = img_url
        params["resolution"] = (locked_res or resolution or "720P").strip() or "720P"
        if any(token in chosen for token in ("2.2", "2.5", "2.6", "2.7")):
            params["shot_type"] = "single"
        return params
    params["model"] = chosen
    params["size"] = locked_size or "1280*720"
    return params


async def _invoke_model_video_generation(
    prompt: str,
    *,
    size: str = "1280*720",
    duration: int | float = 5,
    resolution: str | None = None,
    first_frame: str | None = None,
    reference_images: list[str] | None = None,
    reference_file: str | None = None,
    audio: bool | None = None,
    model: str | None = None,
    force_reference_mode: bool = False,
    provider_override: Literal["vllm-omni"] | None = None,
    api_base_override: str | None = None,
    **backend_options: Any,
) -> dict[str, Any]:
    """Generate a video via DashScope / MiniMax / 火山方舟 / vLLM-Omni backends.

    ``provider_override`` pins the backend regardless of ``models.video_gen``;
    the configured key / base / model are only reused when they belong to that
    same backend. ``backend_options`` are forwarded to the vLLM-Omni request
    (references, fps, negative prompt, sampling / model params).
    """
    cfg = get_config() or {}
    mc = _get_model_config(cfg, "video_gen")

    api_key = str(mc.get("api_key") or os.getenv("VIDEO_GEN_API_KEY") or "").strip().strip("'\"")
    api_base = str(
        mc.get("api_base")
        or os.getenv("VIDEO_GEN_API_BASE")
        or _CHINA_DASHSCOPE_API_BASE
    ).strip().strip("'\"")

    explicit_model = model
    model = str(
        model
        or mc.get("model_name")
        or mc.get("model")
        or os.getenv("VIDEO_GEN_MODEL_NAME")
        or ""
    ).strip()
    provider = str(
        mc.get("client_provider")
        or mc.get("model_provider")
        or os.getenv("VIDEO_GEN_PROVIDER")
        or "DashScope"
    ).strip()
    endpoint_profile = str(
        mc.get("endpoint_profile") or os.getenv("VIDEO_GEN_ENDPOINT_PROFILE") or ""
    ).strip().lower()
    vendor_key = str(
        mc.get("vendor_key") or os.getenv("VIDEO_GEN_VENDOR_KEY") or ""
    ).strip()

    backend = _resolve_video_gen_backend(
        provider=provider,
        endpoint_profile=endpoint_profile,
        vendor_key=vendor_key,
        api_base=api_base,
        model=model,
    )
    if provider_override and provider_override != backend:
        # Credentials of another vendor must never reach the pinned backend.
        api_key, api_base = "", ""
        model = str(explicit_model or "").strip()
        backend = provider_override
    if api_base_override and api_base_override.strip():
        api_base = api_base_override.strip()
    if backend_options and backend != "vllm-omni":
        return {"error": "[ERROR]: extra video request options are only supported by vLLM-Omni."}
    if backend != "vllm-omni":
        # vLLM-Omni is self-deployed: the API key is optional and the model
        # name may be omitted (resolved via GET {api_base}/models instead).
        if not api_key:
            return {"error": "[ERROR]: VIDEO_GEN_API_KEY is not configured for video generation."}
        if not model:
            return {
                "error": "[ERROR]: VIDEO_GEN_MODEL_NAME is not configured. "
                "Set models.video_gen in Settings — no hard-coded video model fallback."
            }
    if backend == "dashscope":
        api_base = _align_dashscope_video_api_base(api_base, api_key)
        os.environ["VIDEO_GEN_API_BASE"] = api_base
    logger.info(
        "[generate_video] backend=%s model=%s provider=%s profile=%s vendor=%s api_base=%s",
        backend,
        model,
        provider,
        endpoint_profile,
        vendor_key,
        api_base,
    )

    try:
        if backend == "vllm-omni":
            return await asyncio.to_thread(
                invoke_vllm_omni_video_generation_sync,
                prompt,
                api_key=api_key,
                api_base=api_base,
                model=model,
                size=size,
                duration=duration,
                resolution=resolution,
                first_frame=first_frame,
                reference_images=reference_images,
                **backend_options,
            )
        if backend == "minimax":
            return await asyncio.to_thread(
                _invoke_minimax_video_generation_sync,
                prompt,
                api_key=api_key,
                api_base=api_base,
                model=model,
                size=size,
                duration=duration,
                resolution=resolution,
                first_frame=first_frame,
                reference_images=reference_images,
                audio=audio,
                force_reference_mode=force_reference_mode,
            )
        if backend == "volcengine":
            return await asyncio.to_thread(
                _invoke_volcengine_video_generation_sync,
                prompt,
                api_key=api_key,
                api_base=api_base,
                model=model,
                size=size,
                duration=duration,
                resolution=resolution,
                first_frame=first_frame,
                reference_images=reference_images,
                audio=audio,
                force_reference_mode=force_reference_mode,
            )
        if backend == "openrouter":
            return await asyncio.to_thread(
                _invoke_openrouter_video_generation_sync,
                prompt,
                api_key=api_key,
                api_base=api_base,
                model=model,
                size=size,
                duration=duration,
                resolution=resolution,
                first_frame=first_frame,
                reference_images=reference_images,
                audio=audio,
            )
        return await _invoke_dashscope_video_generation(
            prompt,
            api_key=api_key,
            api_base=api_base,
            model=model,
            provider=provider,
            endpoint_profile=endpoint_profile,
            mc=mc,
            size=size,
            duration=duration,
            resolution=resolution,
            first_frame=first_frame,
            reference_images=reference_images,
            reference_file=reference_file,
            audio=audio,
            force_reference_mode=force_reference_mode,
        )
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

    model = (os.environ.get("VIDEO_GEN_MODEL_NAME") or "").strip()
    if not model and not _video_gen_is_vllm_omni():
        # vLLM-Omni may leave the model unset: it is resolved from the
        # server's GET /models at generation time.
        return (
            "[ERROR]: VIDEO_GEN_MODEL_NAME is not configured. "
            "Set models.video_gen in Settings — no hard-coded video model fallback."
        )
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
    duration_int = max(1, duration_int)

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

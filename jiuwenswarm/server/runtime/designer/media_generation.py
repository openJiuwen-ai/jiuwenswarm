# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Designer image / video generation on the shared generation backends.

Design reads the same Settings > Agent slots as the chat tools
(``VIDEO_GEN_*`` / ``VISUAL_GEN_*``, including ``*_PROTOCOL`` and the
``*_ENABLED`` switch) and calls ``gen_toolkits`` directly. Unlike the chat
tool, a clip waits for its video job: the submit / check_video pair is polled
here until the file is saved or the deadline passes.

Vendor-native backends (MiniMax, ModelArk, DashScope, vLLM-Omni) go through
``gen_toolkits``. OpenRouter endpoints are driven through the chat tools'
OpenRouter-style path (``visual_gen_tools`` / ``video_gen_tools``). Any other
endpoint is refused by :func:`generation_problem` with a message naming the
supported ones, rather than being attempted and failing at request time. The
OpenRouter image path is text-to-image only, so image requests drop references
with a warning; the video path does accept them (``input_references``).
"""

from __future__ import annotations

import asyncio
import base64
import logging
import mimetypes
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import unquote, urlparse

from jiuwenswarm.agents.harness.common.tools import gen_toolkits
from jiuwenswarm.agents.harness.common.tools import video_gen_tools, visual_gen_tools
from jiuwenswarm.agents.harness.common.tools.video_gen_tools import video_gen_enabled
from jiuwenswarm.agents.harness.common.tools.visual_gen_tools import visual_gen_enabled

logger = logging.getLogger(__name__)

Kind = Literal["image", "video"]

_ENV_PREFIX: dict[Kind, str] = {"image": "VISUAL_GEN", "video": "VIDEO_GEN"}
_SETTINGS_LABEL: dict[Kind, str] = {"image": "Image generation", "video": "Video generation"}
_IMAGE_RATIOS = ("1:1", "16:9", "4:3", "3:2", "2:3", "3:4", "9:16", "21:9")
_VIDEO_RATIOS = ("16:9", "21:9", "4:3", "1:1", "3:4", "9:16")
_VIDEO_POLL_SECONDS = 15.0
_VIDEO_TIMEOUT_SECONDS = 1800.0
# A 50-step MiniMax-H3 render on a self-deployed server can take far longer.
_VLLM_OMNI_VIDEO_TIMEOUT_SECONDS = 7200.0
_SAVED_TO = re.compile(r"^Saved to: (.+)$", re.MULTILINE)
_PENDING_JOB = re.compile(r"^Video job (\S+) (?:submitted and still|is still) ")
# api_base hosts that are OpenRouter itself; the chat tools drive these.
_OPENROUTER_HOST = re.compile(r"(^|\.)openrouter\.ai$", re.IGNORECASE)


@dataclass(frozen=True)
class SlotSettings:
    """One generation slot as saved in Settings > Agent."""

    api_key: str
    api_base: str
    model: str
    backend: str | None

    @property
    def complete(self) -> bool:
        if self.backend in gen_toolkits.KEYLESS_BACKENDS:
            return bool(self.api_base)
        return bool(self.api_key and self.api_base and self.model)


def slot_settings(kind: Kind) -> SlotSettings:
    prefix = _ENV_PREFIX[kind]
    api_base = os.environ.get(f"{prefix}_API_BASE", "").strip()
    return SlotSettings(
        api_key=os.environ.get(f"{prefix}_API_KEY", "").strip(),
        api_base=api_base,
        model=os.environ.get(f"{prefix}_MODEL_NAME", "").strip(),
        backend=gen_toolkits.detect_backend(f"{prefix}_PROTOCOL", api_base),
    )


def configured_model(kind: Kind) -> str:
    """User-configured model only; never a fallback model id."""
    return slot_settings(kind).model


def generation_enabled(kind: Kind) -> bool:
    return visual_gen_enabled() if kind == "image" else video_gen_enabled()


def _openrouter_style(settings: SlotSettings, kind: Kind) -> bool:
    """Whether the slot is an OpenRouter endpoint the chat tools can drive.

    Recognised by host, or by an explicit ``*_ENDPOINT_PROFILE=openrouter``
    declaration for an OpenRouter-compatible proxy on some other host.
    """
    profile = os.environ.get(f"{_ENV_PREFIX[kind]}_ENDPOINT_PROFILE", "").strip().lower()
    if profile.replace("_", "-") == "openrouter":
        return True
    return bool(_OPENROUTER_HOST.search(urlparse(settings.api_base).hostname or ""))


def generation_problem(kind: Kind) -> str | None:
    """Why Design cannot generate this media kind, or None when it can.

    A vendor-native backend and an OpenRouter endpoint are both accepted; the
    path that runs is decided per call by :func:`slot_settings`. Anything else
    is refused here, so an unsupported slot fails with a message that names the
    supported endpoints instead of an opaque provider error.
    """
    label = _SETTINGS_LABEL[kind]
    if not generation_enabled(kind):
        return f"{label} is switched off in Settings > Agent."
    settings = slot_settings(kind)
    if not settings.complete:
        return f"{label} is not configured: set the API URL, API key and model in Settings > Agent."
    if settings.backend is None and not _openrouter_style(settings, kind):
        return (
            f"{label} for Design needs a MiniMax, BytePlus ModelArk / Volcengine, Alibaba DashScope or "
            f"vLLM-Omni endpoint, or an OpenRouter endpoint; {settings.api_base} is not one of them. "
            f"Change it in Settings > Agent."
        )
    return None


def _short_edge_px(size: str | None, default: str = "1024") -> str:
    """Short-edge pixel size for the OpenRouter-style image call, e.g. "1024"."""
    match = re.match(r"^\s*(\d+)\s*[x*]\s*(\d+)\s*$", str(size or ""))
    if not match:
        return default
    return str(min(int(match.group(1)), int(match.group(2))))


def _local_path(value: str) -> Path | None:
    if value.startswith("file:"):
        local = unquote(urlparse(value).path)
        if len(local) >= 3 and local[0] == "/" and local[2] == ":":
            local = local[1:]
        candidate = Path(local)
    else:
        candidate = Path(value).expanduser()
    return candidate.resolve() if candidate.is_file() else None


def image_uri(path_or_url: str | None) -> str | None:
    """An http(s) URL / data: URI as-is, a readable local image as a data: URI, else None."""
    value = str(path_or_url or "").strip()
    if not value:
        return None
    if value.startswith(("http://", "https://", "data:")):
        return value
    local = _local_path(value)
    if local is None:
        return None
    mime, _ = mimetypes.guess_type(str(local))
    if not mime or not mime.startswith("image/"):
        mime = "image/png"
    return f"data:{mime};base64,{base64.b64encode(local.read_bytes()).decode('ascii')}"


def _reference_uris(paths: list[str] | None) -> tuple[tuple[str, ...], str | None]:
    """(data URIs / URLs, error) for the readable references."""
    uris = [uri for uri in (image_uri(item) for item in paths or []) if uri]
    if paths and not uris:
        return (), "[ERROR]: reference images were provided but none could be read as local files or URLs."
    if len(uris) < len(paths or []):
        logger.warning("Designer generation skipped %d unreadable reference(s)", len(paths or []) - len(uris))
    return tuple(dict.fromkeys(uris)), None


def _saved_path(result: str) -> str | None:
    match = _SAVED_TO.search(result)
    return match.group(1).split(", ")[0].strip() if match else None


async def generate_image(
    prompt: str,
    *,
    size: str | None = None,
    reference_images: list[str] | None = None,
    save_dir: str | None = None,
) -> dict[str, str]:
    """Generate one still; returns ``{"image_path": ...}`` or ``{"error": ...}``."""
    problem = generation_problem("image")
    if problem:
        return {"error": f"[ERROR]: {problem}"}
    settings = slot_settings("image")
    refs, error = _reference_uris(reference_images)
    if error:
        return {"error": error}
    aspect_ratio = gen_toolkits.aspect_ratio_for_size(size, _IMAGE_RATIOS, "1:1")

    if settings.backend is None:
        # OpenRouter-style endpoint: the chat tool's path. It is text-to-image
        # only, so references are dropped with a warning instead of failing.
        if refs:
            logger.warning(
                "Designer image generation via %s ignores %d reference image(s): the "
                "OpenRouter-style path is text-to-image only",
                settings.api_base, len(refs),
            )
        logger.info(
            "Designer image generation backend=openrouter model=%s size=%s references=0/%d",
            settings.model, size, len(refs),
        )
        # ``generate_visual`` is an openjiuwen ``@tool``: the module attribute is a
        # LocalFunction, not the coroutine. Reach through ``_func`` for the plain
        # callable (same convention as readonly_tool_bindings.py).
        result = await visual_gen_tools.generate_visual._func(  # pylint: disable=protected-access
            prompt, aspect_ratio, _short_edge_px(size), save_dir
        )
        path = None if result.startswith("[ERROR]") else _saved_path(result)
        return {"image_path": path} if path else {"error": result}

    target = gen_toolkits.GenerationTarget(
        settings.backend, settings.api_key, settings.api_base, settings.model
    )
    options = gen_toolkits.ImageOptions(size=size, reference_image_uris=refs)
    logger.info(
        "Designer image generation backend=%s model=%s size=%s references=%d",
        target.backend, target.model, size, len(refs),
    )
    result = await gen_toolkits.generate_image(target, prompt, aspect_ratio, save_dir, options)
    path = None if result.startswith("[ERROR]") else _saved_path(result)
    return {"image_path": path} if path else {"error": result}


@dataclass(frozen=True)
class DesignerVideoRequest:
    """One clip for ``generate_video``; paths may be local files or URLs."""

    prompt: str
    duration: int = 5
    size: str | None = None
    resolution: str | None = None
    first_frame: str | None = None
    reference_images: tuple[str, ...] = ()
    reference_file: str | None = None
    audio: bool = False
    reference_mode: bool = False
    model: str | None = None


async def generate_video(request: DesignerVideoRequest, *, save_dir: str | None = None) -> dict[str, str]:
    """Generate one clip and wait for it; returns ``{"video_path": ...}`` or ``{"error": ...}``."""
    problem = generation_problem("video")
    if problem:
        return {"error": f"[ERROR]: {problem}"}
    settings = slot_settings("video")
    model = (request.model or "").strip() or settings.model
    refs, error = _reference_uris(list(request.reference_images))
    if error:
        return {"error": error}
    first_frame = image_uri(request.first_frame)
    if request.first_frame and not first_frame:
        return {"error": f"[ERROR]: first frame {request.first_frame!r} is not a readable image file or URL."}
    aspect_ratio = gen_toolkits.aspect_ratio_for_size(request.size, _VIDEO_RATIOS, "16:9")
    resolution = (request.resolution or "720p").strip().lower()

    if settings.backend is None:
        # OpenRouter-style endpoint: the chat tool's path (submit, then poll).
        # Reference stills ride ``input_references`` there, so the character/
        # scene references Design always passes are honoured.
        logger.info(
            "Designer video generation backend=openrouter model=%s size=%s resolution=%s first_frame=%s "
            "references=%d reference_mode=%s",
            model, request.size, resolution, bool(request.first_frame), len(refs), request.reference_mode,
        )
        # Same ``@tool`` caveat as the image path above.
        result = await video_gen_tools.generate_video._func(  # pylint: disable=protected-access
            request.prompt,
            aspect_ratio,
            resolution,
            int(request.duration),
            request.first_frame,
            bool(request.audio),
            save_dir,
            list(request.reference_images) or None,
            request.reference_mode,
        )
        deadline = time.monotonic() + _VIDEO_TIMEOUT_SECONDS
        while (pending := _PENDING_JOB.match(result)) and time.monotonic() < deadline:
            await asyncio.sleep(_VIDEO_POLL_SECONDS)
            result = await video_gen_tools.check_video_status._func(  # pylint: disable=protected-access
                pending.group(1), save_dir
            )
        if pending:
            return {
                "error": f"[ERROR]: video job {pending.group(1)} did not finish within "
                f"{int(_VIDEO_TIMEOUT_SECONDS)}s."
            }
        path = None if result.startswith("[ERROR]") else _saved_path(result)
        return {"video_path": path} if path else {"error": result}

    target = gen_toolkits.GenerationTarget(settings.backend, settings.api_key, settings.api_base, model)
    video_request = gen_toolkits.VideoRequest(
        prompt=request.prompt,
        aspect_ratio=aspect_ratio,
        resolution=resolution,
        duration_seconds=int(request.duration),
        generate_audio=bool(request.audio),
        first_frame_data_uri=first_frame,
        size=request.size,
        reference_image_uris=refs,
        reference_mode=request.reference_mode,
        reference_file_path=request.reference_file,
    )
    logger.info(
        "Designer video generation backend=%s model=%s size=%s resolution=%s first_frame=%s references=%d "
        "reference_mode=%s file=%s",
        target.backend, target.model, request.size, video_request.resolution, bool(first_frame), len(refs),
        request.reference_mode, bool(request.reference_file),
    )
    result = await gen_toolkits.submit_video(target, video_request, save_dir)
    timeout = (
        _VLLM_OMNI_VIDEO_TIMEOUT_SECONDS if target.backend == gen_toolkits.VLLM_OMNI else _VIDEO_TIMEOUT_SECONDS
    )
    deadline = time.monotonic() + timeout
    while (pending := _PENDING_JOB.match(result)) and time.monotonic() < deadline:
        await asyncio.sleep(_VIDEO_POLL_SECONDS)
        result = await gen_toolkits.check_video(target, pending.group(1), save_dir)
    if pending:
        return {"error": f"[ERROR]: video job {pending.group(1)} did not finish within {int(timeout)}s."}
    path = None if result.startswith("[ERROR]") else _saved_path(result)
    return {"video_path": path} if path else {"error": result}


def vllm_omni_endpoint(kind: Kind, api_base: str | None, model: str | None) -> tuple[str, str, str]:
    """(api_key, api_base, model) for a ComfyUI node pinned to vLLM-Omni.

    The node's own URL / model win; the Settings slot fills the gaps only when it
    is itself a vLLM-Omni endpoint, so another vendor's key never reaches the node's server.
    """
    settings = slot_settings(kind)
    if settings.backend != gen_toolkits.VLLM_OMNI:
        settings = SlotSettings("", "", "", None)
    return (
        settings.api_key,
        (api_base or "").strip() or settings.api_base,
        (model or "").strip() or settings.model,
    )
